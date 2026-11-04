"""The download queue: persistent, one download at a time by default, pausable, resumable.

A job is a small JSON file in <root>/jobs/. Downloads run in a subprocess (runner.py); a `verify` job (re-hash every blob) runs in
a thread. A restart turns running jobs back into queued ones, and because the partial files stay in staging a re-run resumes.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..fsutil import atomic_write_text
from .runner import staging_path
from .state import HubSettings, in_window
from .upstream import patterns_for

_ID_OK = re.compile(r"^[\w.\-]+(?:/[\w.\-]+)?$")
MARGIN_BYTES = 256 * 1024 * 1024
KEEP_FINISHED = 100


@dataclass
class Job:
    id: str
    kind: str                      # "download" | "verify"
    label: str
    spec: dict
    status: str = "queued"         # queued | running | paused | done | failed
    phase: str = ""                # resolving | downloading | storing | (verify) hashing
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    bytes_total: int = 0
    bytes_done: int = 0
    speed_bps: float = 0.0
    files_total: int = 0
    files_done: int = 0
    message: str = ""
    result: dict | None = None
    log: list = field(default_factory=list)
    note: str = ""                 # why a queued job is not running (download window, other job)

    def public(self) -> dict:
        d = asdict(self)
        d["spec"] = {k: v for k, v in self.spec.items() if k != "patterns"}
        d["log"] = self.log[-20:]
        d["eta_s"] = (int((self.bytes_total - self.bytes_done) / self.speed_bps)
                      if self.status == "running" and self.speed_bps > 1 and self.bytes_total > self.bytes_done else None)
        return d


def dir_size(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            if f.endswith((".lock", ".metadata")):
                continue
            try:
                total += os.stat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


class JobManager:
    def __init__(self, repo, storage, hub_settings: HubSettings, *, python: str | None = None, env_extra: dict | None = None,
                 poll_seconds: float = 1.0):
        self.repo, self.storage, self.hs = repo, storage, hub_settings
        self.dir = Path(storage.root) / "jobs"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.python = python or sys.executable
        self.env_extra = env_extra or {}
        self.poll = poll_seconds
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._intent: dict[str, str] = {}
        self._last_sample: dict[str, tuple[float, int]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._load()

    # ---- lifecycle -----------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="job-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            for jid, p in list(self._procs.items()):
                self._intent[jid] = "paused"
                p.terminate()
        if self._thread:
            self._thread.join(timeout=5)

    def _load(self) -> None:
        for f in sorted(self.dir.glob("*.json")):
            if f.name.endswith(".spec.json"):
                continue
            try:
                job = Job(**json.loads(f.read_text()))
            except (OSError, ValueError, TypeError):
                continue
            if job.status == "running":                     # the server died under it: it resumes from staging
                job.status, job.note = "queued", "resumed after a restart"
            self._jobs[job.id] = job

    def _save(self, job: Job) -> None:
        d = asdict(job)
        d["log"] = d["log"][-20:]
        try:
            atomic_write_text(self.dir / f"{job.id}.json", json.dumps(d))
        except OSError:
            pass

    # ---- API used by the web layer ---------------------------------------

    def list(self) -> list[dict]:
        with self._lock:
            order = {"running": 0, "queued": 1, "paused": 2, "failed": 3, "done": 4}
            return [j.public() for j in sorted(self._jobs.values(), key=lambda j: (order.get(j.status, 9), -j.created))]

    def get(self, jid: str) -> Job | None:
        with self._lock:
            return self._jobs.get(jid)

    def active_summary(self) -> dict:
        with self._lock:
            running = [j for j in self._jobs.values() if j.status == "running"]
            return {"running": len(running), "queued": sum(1 for j in self._jobs.values() if j.status == "queued"),
                    "speed_bps": sum(j.speed_bps for j in running)}

    def submit_download(self, spec: dict) -> tuple[Job, bool]:
        """Queue a download. Returns (job, already_queued). Raises ValueError with a message meant for the user."""
        source = spec.get("source")
        if source not in ("hf", "civitai"):
            raise ValueError("source must be hf or civitai")
        sid = str(spec.get("id", "")).strip()
        if source == "hf":
            if not _ID_OK.match(sid):
                raise ValueError("that is not a Hugging Face repository id (expected org/name)")
            if spec.get("type", "model") not in ("model", "dataset"):
                raise ValueError("type must be model or dataset")
        elif not sid.isdigit():
            raise ValueError("a Civitai download needs the numeric version id")
        files = spec.get("files")
        clean = {"source": source, "id": sid, "type": spec.get("type", "model") if source == "hf" else "model",
                 "revision": str(spec.get("revision") or "main"),
                 "patterns": patterns_for([str(f) for f in files]) if files else None,
                 "file_type": spec.get("file_type"), "file_format": spec.get("file_format"),
                 "allow_incomplete_shards": bool(spec.get("allow_incomplete_shards")),
                 "total_bytes": int(spec.get("total_bytes") or 0), "file_count": int(spec.get("file_count") or 0)}
        with self._lock:
            for j in self._jobs.values():
                if j.kind == "download" and j.status in ("queued", "running", "paused") and \
                        (j.spec["source"], j.spec["type"], j.spec["id"]) == (source, clean["type"], sid):
                    return j, True
        need = clean["total_bytes"]
        if need:
            free = shutil.disk_usage(self.storage.root).free
            staged = dir_size(staging_path(self.storage.staging_dir, clean)) if staging_path(self.storage.staging_dir, clean).exists() else 0
            if free + staged < need + MARGIN_BYTES:
                gib = lambda n: f"{n / 2**30:.1f} GiB"
                raise ValueError(f"not enough free space: this needs about {gib(need)} and the store's disk has {gib(free + staged)} available")
        label = sid if source == "hf" else f"civitai {sid}"
        job = Job(id=uuid.uuid4().hex[:12], kind="download", label=label, spec=clean, bytes_total=need, files_total=clean["file_count"])
        with self._lock:
            self._jobs[job.id] = job
            self._save(job)
        return job, False

    def submit_verify(self, repo_id: str | None = None) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind="verify", label=repo_id or "the whole library", spec={"repo_id": repo_id}, status="running",
                  phase="hashing", started=time.time())
        with self._lock:
            self._jobs[job.id] = job
        threading.Thread(target=self._run_verify, args=(job,), name=f"verify-{job.id}", daemon=True).start()
        return job

    def pause(self, jid: str) -> bool:
        with self._lock:
            job = self._jobs.get(jid)
            if not job or job.status not in ("queued", "running"):
                return False
            self._intent[jid] = "paused"
            if job.status == "queued" or jid not in self._procs:
                job.status = "paused" if job.kind == "download" else "failed"
                self._save(job)
            elif jid in self._procs:
                self._procs[jid].terminate()
            return True

    def resume(self, jid: str) -> bool:
        with self._lock:
            job = self._jobs.get(jid)
            if not job or job.status not in ("paused", "failed") or job.kind != "download":
                return False
            job.status, job.message, job.note, job.finished = "queued", "", "", None
            self._intent.pop(jid, None)
            self._save(job)
            return True

    def remove(self, jid: str, *, discard_partial: bool = True) -> bool:
        with self._lock:
            job = self._jobs.get(jid)
            if not job:
                return False
            self._intent[jid] = "removed"
            if jid in self._procs:
                self._procs[jid].terminate()
        if job.kind == "download" and job.status != "done" and discard_partial:
            shutil.rmtree(staging_path(self.storage.staging_dir, job.spec), ignore_errors=True)
        with self._lock:
            self._jobs.pop(jid, None)
            for suffix in (".json", ".spec.json"):
                (self.dir / f"{jid}{suffix}").unlink(missing_ok=True)
        return True

    # ---- scheduler -----------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:                                   # noqa: BLE001 - the scheduler must never die
                pass
            self._stop.wait(self.poll)

    def _tick(self) -> None:
        with self._lock:
            running = [j for j in self._jobs.values() if j.kind == "download" and j.status == "running"]
            for j in running:
                self._sample(j)
            limit = int(self.hs.get("max_concurrent") or 1)
            queued = sorted((j for j in self._jobs.values() if j.kind == "download" and j.status == "queued"), key=lambda j: j.created)
            window = self.hs.get("download_window") or ""
            for i, j in enumerate(queued):
                if not in_window(window):
                    j.note = f"waiting for the download window {window}"
                elif len(running) >= limit:
                    j.note = "waiting for the running download to finish"
                else:
                    self._start(j)
                    running.append(j)
                    j.note = ""
            self._trim()

    def _sample(self, job: Job) -> None:
        now = time.time()
        try:
            done = dir_size(staging_path(self.storage.staging_dir, job.spec))
        except OSError:
            return
        if job.phase == "downloading":
            last = self._last_sample.get(job.id)
            if last and now > last[0]:
                inst = max(0.0, (done - last[1]) / (now - last[0]))
                job.speed_bps = inst if job.speed_bps == 0 else 0.7 * job.speed_bps + 0.3 * inst
            self._last_sample[job.id] = (now, done)
            job.bytes_done = done
        elif job.phase == "storing":
            job.speed_bps = 0.0

    def _start(self, job: Job) -> None:
        spec_path = self.dir / f"{job.id}.spec.json"
        atomic_write_text(spec_path, json.dumps(job.spec))
        env = {**os.environ, **self.env_extra, "MIRROR_ROOT": str(self.storage.root), "PYTHONUNBUFFERED": "1"}
        env.pop("HF_TOKEN", None)
        tok, ctok = self.hs.get("hf_token"), self.hs.get("civitai_token")
        if tok:
            env["HF_TOKEN"] = tok
        if ctok:
            env["CIVITAI_API_KEY"] = ctok
        proc = subprocess.Popen([self.python, "-m", "server.hub.runner", str(spec_path)], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, env=env, cwd=str(Path(__file__).resolve().parents[2]))
        job.status, job.phase, job.started, job.message = "running", "resolving", time.time(), ""
        job.speed_bps, job.bytes_done = 0.0, 0
        self._procs[job.id] = proc
        self._intent.pop(job.id, None)
        self._save(job)
        threading.Thread(target=self._read, args=(job, proc), name=f"job-{job.id}", daemon=True).start()

    def _read(self, job: Job, proc: subprocess.Popen) -> None:
        last: dict = {}
        for line in proc.stdout:                                # type: ignore[union-attr]
            line = line.rstrip("\n")
            try:
                ev = json.loads(line)
                if not isinstance(ev, dict) or "phase" not in ev:
                    raise ValueError
            except ValueError:
                with self._lock:
                    job.log.append(line[-300:])
                continue
            last = ev
            with self._lock:
                job.phase = ev["phase"]
                if ev["phase"] == "storing":
                    job.files_done, job.files_total = ev.get("done", 0), ev.get("of", job.files_total)
                    if job.bytes_total:
                        job.bytes_done = job.bytes_total
        rc = proc.wait()
        with self._lock:
            self._procs.pop(job.id, None)
            intent = self._intent.pop(job.id, "")
            job.finished = time.time()
            job.speed_bps = 0.0
            if intent == "removed":
                return
            if intent == "paused":
                job.status, job.finished, job.note = "paused", None, "paused: the partial download is kept"
            elif last.get("phase") == "done" and rc == 0:
                job.status, job.result, job.message = "done", {k: v for k, v in last.items() if k != "phase"}, ""
                job.bytes_done = job.bytes_total or job.bytes_done
                job.files_done = job.files_total = last.get("files", job.files_total)
            else:
                job.status = "failed"
                job.message = last.get("message") or (job.log[-1] if job.log else f"the download process ended with code {rc}")
            self._save(job)

    def _run_verify(self, job: Job) -> None:
        try:
            digests = (sorted(self.repo.collect_reachable_blobs()) if not job.spec.get("repo_id") else
                       sorted({d for d in self._repo_digests(job.spec["repo_id"])}))
            sizes = {d: self.storage.blobs.path_for(d).stat().st_size for d in digests if self.storage.blobs.path_for(d).is_file()}
            job.bytes_total, job.files_total = sum(sizes.values()), len(digests)
            bad = []
            for i, d in enumerate(digests, 1):
                if self._intent.get(job.id) in ("removed", "paused"):
                    job.status, job.message = "failed", "cancelled"
                    return
                if not self.repo.verify_blob(d):
                    bad.append(d)
                job.files_done = i
                job.bytes_done += sizes.get(d, 0)
            job.status = "failed" if bad else "done"
            job.message = f"{len(bad)} damaged or missing file(s)" if bad else ""
            job.result = {"checked": len(digests), "bad": [f"sha256:{b}" for b in bad]}
        except Exception as exc:                                # noqa: BLE001
            job.status, job.message = "failed", str(exc)
        finally:
            job.finished = time.time()

    def _repo_digests(self, repo_id: str):
        for commit in self.repo.list_revisions(repo_id):
            for f in self.repo.list_files(repo_id, commit):
                yield f.blob.split(":", 1)[1]

    def _trim(self) -> None:
        done = sorted((j for j in self._jobs.values() if j.status in ("done", "failed") and j.kind == "verify" or j.status == "done"),
                      key=lambda j: j.finished or 0, reverse=True)
        for j in done[KEEP_FINISHED:]:
            self._jobs.pop(j.id, None)
            (self.dir / f"{j.id}.json").unlink(missing_ok=True)
