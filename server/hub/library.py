"""The library as the GUI sees it: summaries, one repo's detail, and what deleting it would free."""
from __future__ import annotations

import os
import threading
import time

from ..repository import RepositoryCore
from ..storage import core_repo_id, public_repo_id, repo_type_of

_FORMATS = {"gguf": "gguf", "safetensors": "safetensors", "bin": "pytorch", "pt": "pytorch", "pth": "pytorch", "onnx": "onnx",
            "ckpt": "checkpoint", "parquet": "parquet", "jsonl": "jsonl", "json": None, "csv": "csv", "arrow": "arrow"}
_README_MAX = 200_000


def _formats(paths: list[str]) -> list[str]:
    out = []
    for p in paths:
        ext = p.rsplit(".", 1)[-1].lower() if "." in p else ""
        f = _FORMATS.get(ext)
        if f and f not in out:
            out.append(f)
    return out


def _latest_commit(repo: RepositoryCore, core_id: str) -> str | None:
    refs = repo.list_references(core_id)
    if refs.get("main"):
        return refs["main"]
    revs = repo.list_revisions(core_id)
    return revs[-1] if revs else None


def summarize(repo: RepositoryCore, core_id: str) -> dict | None:
    try:
        commit = _latest_commit(repo, core_id)
        if not commit:
            return None
        m = repo.get_manifest(core_id, commit)
    except Exception:                                   # noqa: BLE001 - one broken repo must not blank the library
        return None
    paths = [f.path for f in m.files]
    return {"id": public_repo_id(core_id), "type": repo_type_of(core_id), "core_id": core_id, "commit": commit,
            "files": len(m.files), "size_bytes": sum(f.size for f in m.files), "revisions": len(repo.list_revisions(core_id)),
            "updated": m.revision.fetched_at, "source": m.source.kind, "source_ref": m.source.ref,
            "license": m.revision.license.name if m.revision.license else None, "formats": _formats(paths)}


def list_library(repo: RepositoryCore, *, type_: str = "all", q: str = "", sort: str = "name") -> list[dict]:
    items = []
    for core_id in repo.list_repos():
        if type_ in ("model", "dataset") and repo_type_of(core_id) != type_:
            continue
        s = summarize(repo, core_id)
        if s:
            items.append(s)
    if q:
        ql = q.lower().strip()
        items = [i for i in items if ql in i["id"].lower() or any(ql in f for f in i["formats"]) or ql in (i["license"] or "").lower()]
    keys = {"name": lambda i: i["id"].lower(), "size": lambda i: -i["size_bytes"], "updated": lambda i: i["updated"] or ""}
    items.sort(key=keys.get(sort, keys["name"]), reverse=(sort == "updated"))
    return items


def detail(repo: RepositoryCore, core_id: str) -> dict | None:
    base = summarize(repo, core_id)
    if not base:
        return None
    revisions = []
    for commit in reversed(repo.list_revisions(core_id)):
        m = repo.get_manifest(core_id, commit)
        revisions.append({"commit": commit, "fetched_at": m.revision.fetched_at,
                          "files": [{"path": f.path, "size": f.size, "sha256": f.blob.split(":", 1)[1]} for f in m.files]})
    readme = None
    latest = next((r for r in revisions if r["commit"] == base["commit"]), None)
    if latest:
        for f in latest["files"]:
            if f["path"].lower() == "readme.md" and f["size"] <= _README_MAX:
                try:
                    with repo.open_blob(f["sha256"]) as fh:
                        readme = fh.read().decode("utf-8", "replace")
                except OSError:
                    pass
                break
    return {**base, "refs": repo.list_references(core_id), "revisions": revisions, "readme": readme}


class StoreUsage:
    """Bytes the blob directory really occupies (deduplicated). Walking 100k files is slow on a NAS: cached for a minute."""

    def __init__(self, repo: RepositoryCore):
        self._repo, self._lock, self._at, self._val = repo, threading.Lock(), 0.0, {"blobs": 0, "bytes": 0}

    def get(self) -> dict:
        with self._lock:
            if time.time() - self._at > 60:
                n = b = 0
                for digest in self._repo.iter_blobs():
                    try:
                        b += os.stat(self._repo._storage.blobs.path_for(digest)).st_size
                        n += 1
                    except OSError:
                        pass
                self._val, self._at = {"blobs": n, "bytes": b}, time.time()
            return self._val

    def invalidate(self) -> None:
        with self._lock:
            self._at = 0.0
