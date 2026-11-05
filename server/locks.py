"""One lock between ingest and garbage collection.

GC deletes every blob no published manifest names. An ingest stores its blobs one by one and publishes the manifest only at
the end, so for the whole hash-and-store phase of a big repo (hours on a NAS) its blobs look unreferenced. Without a lock a
nightly GC deletes them and the published revision then points at missing files.

An ingest holds the lock SHARED (any number may run); GC needs it EXCLUSIVE and does not wait: if an ingest is running it
stops with StoreBusy and the caller tries again later. flock is released by the kernel when the process dies, so a crashed
ingest never blocks GC. (Local filesystems only: the store is a local volume, not NFS.)
"""
from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path


class StoreBusy(Exception):
    """GC wanted the exclusive lock while an ingest (or another GC) holds it."""


@contextmanager
def store_lock(root: Path, *, exclusive: bool, wait: bool):
    fd = os.open(str(Path(root) / ".store.lock"), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        flag = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if not wait:
            flag |= fcntl.LOCK_NB
        try:
            fcntl.flock(fd, flag)
        except BlockingIOError:
            raise StoreBusy("an ingest or another maintenance job is running on this store; try again when it has finished") from None
        yield
    finally:
        os.close(fd)        # closing releases the lock


@contextmanager
def dir_lock(path: Path):
    """Exclusive, waiting flock on a DIRECTORY (opened read-only): serialises the read-modify-write of one repository's
    manifests across threads and processes without leaving a lock file in the store. Released when the descriptor closes."""
    fd = os.open(str(path), os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)
