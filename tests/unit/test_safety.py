"""The 2026-11-04 review findings, each with a test that fails without its fix."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from ingester import jobs, transaction
from ingester.hf_source import StagedFile, StagedSnapshot
from server import blobstore, fsutil
from server.locks import StoreBusy


def _snapshot(tmp_path: Path, files: dict[str, bytes], commit: str = "a" * 40) -> StagedSnapshot:
    staging = tmp_path / "stg"
    staging.mkdir(exist_ok=True)
    staged = []
    for name, data in files.items():
        p = staging / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        staged.append(StagedFile(path=name, local_path=p, size=len(data), etag="x", sha256=None))
    return StagedSnapshot(repo_id="org/m", revision_requested="main", commit=commit, staging_dir=staging, files=staged)


def test_gc_never_deletes_the_blobs_of_a_running_ingest(repo, tmp_path):
    """A blob stored but not yet named by a manifest (the whole hash-and-store phase of a big ingest) must survive GC."""
    f = tmp_path / "shard1"
    f.write_bytes(b"x" * 1000)
    blob = repo.put_blob(f)                                   # what the ingest does for shard 1 of N
    with repo.ingest_lock():                                   # ...while the ingest still holds its lock
        with pytest.raises(StoreBusy):
            jobs.gc(repo, min_age_seconds=0)
    assert repo._storage.blobs.exists(blob.digest)


def test_gc_skips_young_blobs_even_without_a_lock(repo, tmp_path):
    f = tmp_path / "y"
    f.write_bytes(b"y" * 10)
    blob = repo.put_blob(f)
    report = jobs.gc(repo)                                    # default min age: an hour
    assert report.acted == 0 and repo._storage.blobs.exists(blob.digest)


def test_gc_still_collects_old_orphans(repo, tmp_path):
    """Positive control: the guard must not turn GC into a no-op."""
    f = tmp_path / "o"
    f.write_bytes(b"o" * 10)
    blob = repo.put_blob(f)
    assert jobs.gc(repo, min_age_seconds=0).acted == 1
    assert not repo._storage.blobs.exists(blob.digest)


def test_ingest_waits_for_a_running_gc_instead_of_racing_it(repo, tmp_path):
    """The ingest takes the lock shared; while GC holds it exclusive the ingest must not run concurrently."""
    import threading, time
    done = threading.Event()
    snap = _snapshot(tmp_path, {"config.json": b"{}"})
    with repo.gc_lock():
        t = threading.Thread(target=lambda: (transaction.ingest(repo, snap), done.set()), daemon=True)
        t.start()
        time.sleep(0.3)
        assert not done.is_set()                              # blocked behind the GC
    assert done.wait(5)


def test_ingest_hashes_each_file_once(repo, tmp_path, monkeypatch):
    calls = []
    real = blobstore.sha256_file

    def counting(path, **kw):
        calls.append(Path(path).name)
        return real(path, **kw)

    monkeypatch.setattr(blobstore, "sha256_file", counting)
    monkeypatch.setattr(transaction, "_sha256_file", lambda p, **kw: counting(p, **kw))
    transaction.ingest(repo, _snapshot(tmp_path, {"big.bin": os.urandom(4096), "config.json": b"{}"}))
    assert sorted(calls) == ["big.bin", "config.json"]       # one read each (it was two, three for small files)


def test_atomic_write_fsyncs_file_and_directory(tmp_path, monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real(fd))[1])
    dest = tmp_path / "d" / "refs.json"
    fsutil.atomic_write_text(dest, '{"main": "x"}\n')
    assert dest.read_text() == '{"main": "x"}\n'
    assert len(synced) == 2                                   # the temp file, then the directory
    assert [p.name for p in dest.parent.iterdir()] == ["refs.json"]      # no temp file left behind
    assert oct(dest.stat().st_mode & 0o777) == "0o644"


def test_manifest_and_refs_are_written_durably(repo, tmp_path, monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real(fd))[1])
    transaction.ingest(repo, _snapshot(tmp_path, {"config.json": b"{}"}))
    assert len(synced) >= 6        # blob (file + dir) + manifest (file + dir) + refs (file + dir)


def test_gc_reports_the_bytes_it_frees(repo, tmp_path):
    f = tmp_path / "x"
    f.write_bytes(b"x" * 1234)
    repo.put_blob(f)
    assert jobs.gc(repo, dry_run=True, min_age_seconds=0).bytes == 1234
    assert jobs.gc(repo, min_age_seconds=0).bytes == 1234
