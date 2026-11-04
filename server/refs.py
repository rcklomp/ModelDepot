"""Mutable references (refs).

A ref maps a name ('main', 'latest', 'v1.0') to a commit. Refs are the ONLY
mutable live-pointer state in the system: revisions and blobs are immutable, but
'main' moves as new revisions are published. This mirrors how Git and Hugging
Face work.

Refs are stored per-repo in `repos/<org>/<repo>/refs.json`. Updates are atomic
(write-to-temp + rename). Crash safety: an interrupted update leaves either the
old refs file or an intact new one, never a partial one.
"""
from __future__ import annotations

import json
from pathlib import Path

DEFAULT_REF = "main"


def load(refs_path: Path) -> dict[str, str]:
    """Load refs for a repo. Returns {} if none exist yet."""
    if not refs_path.is_file():
        return {}
    try:
        data = json.loads(refs_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # A corrupt refs file is a real problem — surface it loudly.
        raise RefsError(f"corrupt refs file: {refs_path}")
    if not isinstance(data, dict):
        raise RefsError(f"refs file is not a JSON object: {refs_path}")
    return {str(k): str(v) for k, v in data.items()}


def save(refs_path: Path, refs: dict[str, str]) -> None:
    """Atomically write refs. Creates parent dirs."""
    from .fsutil import atomic_write_text
    atomic_write_text(refs_path, json.dumps(refs, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def resolve(refs: dict[str, str], ref: str) -> str | None:
    """Resolve a ref name (or a raw commit) to a commit hash.

    If `ref` is not a known ref name, it is treated as a literal commit and
    returned unchanged — callers verify the commit actually exists.
    """
    return refs.get(ref, ref if _looks_like_commit(ref) else None)


def set_ref(refs_path: Path, name: str, commit: str) -> dict[str, str]:
    """Set `name` -> `commit` and persist. Returns the updated refs dict."""
    refs = load(refs_path)
    refs[name] = commit
    save(refs_path, refs)
    return refs


def delete_ref(refs_path: Path, name: str) -> dict[str, str]:
    """Remove a ref. Returns the updated refs dict (possibly empty)."""
    refs = load(refs_path)
    refs.pop(name, None)
    save(refs_path, refs)
    return refs


def _looks_like_commit(value: str) -> bool:
    """Heuristic: a commit is a non-empty hex string (HF/Git SHAs)."""
    if not value:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return len(value) >= 7


class RefsError(Exception):
    pass
