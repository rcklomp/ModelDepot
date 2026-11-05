"""One download job, run as its own process: `python -m server.hub.runner <spec.json>`.

A separate process, not a thread, on purpose: it can be killed to pause (the staging directory keeps the partial files, so a
resume continues where it stopped), a crash or an out-of-memory in a 100 GB download cannot take the web server with it, and
the HF token travels in the environment, never on a command line or in the spec file.

It prints one JSON object per line on stdout: {"phase": "resolving" | "downloading" | "storing" | "done" | "error", ...}.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

from server import events
from server.config import load_settings
from server.repository import RepositoryCore
from server.storage import FilesystemStorage, core_repo_id


def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-")[:60] or "repo"


def staging_path(staging_root: Path, spec: dict) -> Path:
    """The same fixed path every time for one repo, so a re-run (resume) finds its partial files. Same names as the CLI uses."""
    if spec["source"] == "civitai":
        return Path(staging_root) / f"civitai-{spec['id']}"
    return Path(staging_root) / f"hf-{slug(core_repo_id(spec.get('type', 'model'), spec['id']))}"


def emit(**kw) -> None:
    print(json.dumps(kw), flush=True)


def run(spec: dict) -> int:
    from ingester import transaction

    settings = load_settings()
    storage = FilesystemStorage(settings)
    storage.ensure_layout()
    repo = RepositoryCore(storage, events.EventLog(storage.events_path))
    staging = staging_path(storage.staging_dir, spec)
    staging.mkdir(parents=True, exist_ok=True)
    progress = lambda i, n, path: emit(phase="storing", done=i, of=n, file=path)
    try:
        emit(phase="resolving")
        if spec["source"] == "hf":
            from ingester import hf_source
            from .upstream import HF_BASE
            emit(phase="downloading")
            snapshot = hf_source.fetch(spec["id"], staging, revision=spec.get("revision") or "main",
                                       token=os.environ.get("HF_TOKEN") or None, allow_patterns=spec.get("patterns") or None,
                                       repo_type=spec.get("type", "model"), endpoint=HF_BASE if HF_BASE != "https://huggingface.co" else None)
            emit(phase="storing", done=0, of=len(snapshot.files))
            result = transaction.ingest(repo, snapshot, ref_name="main", progress=progress,
                                        allow_incomplete_shards=bool(spec.get("allow_incomplete_shards")))
        else:
            from ingester import civitai_source
            emit(phase="downloading")
            snap = civitai_source.fetch(int(spec["id"]), staging, token=os.environ.get("CIVITAI_API_KEY", ""),
                                        file_type=spec.get("file_type"), file_format=spec.get("file_format"))
            staged = civitai_source.to_staged_snapshot(snap)
            emit(phase="storing", done=0, of=len(staged.files))
            result = transaction.ingest(repo, staged, source_kind="civitai",
                                        source_ref=civitai_source.civitai_source_url(snap.model_id, snap.version_id),
                                        ref_name="main", license_name=snap.license_name, progress=progress,
                                        allow_incomplete_shards=bool(spec.get("allow_incomplete_shards")))
        shutil.rmtree(staging, ignore_errors=True)
        emit(phase="done", repo=result.repo_id, commit=result.commit, files=result.file_count, published=result.published, added=result.added)
        return 0
    except Exception as exc:                          # noqa: BLE001 - the job record needs the message, whatever it was
        emit(phase="error", message=str(exc))
        return 1


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    return run(json.loads(Path(argv[0]).read_text()))


if __name__ == "__main__":
    raise SystemExit(main())
