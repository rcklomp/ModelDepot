"""Crash-safe writes of small files (manifests, refs).

A power cut on a NAS must never leave a half-written or empty manifest/refs file: write to a unique temp file in the same
directory, fsync it, rename it over the destination, then fsync the directory so the rename itself is durable.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_text(dest: Path, text: str) -> None:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)          # mkstemp makes 0600; manifests are plain data other tools (rsync, backups) read
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    fsync_dir(dest.parent)
