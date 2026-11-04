"""Unit tests for the background jobs."""
from __future__ import annotations

from pathlib import Path

import pytest

from ingester import jobs
from ingester.hf_source import StagedFile, StagedSnapshot
from ingester import transaction
from server.index import NullIndex, SQLiteIndex


def _stage(repo, tmp_path: Path, commit: str = "a" * 40):
    staging = tmp_path / "s"; staging.mkdir()
    payload = b"kept bytes"
    p = staging / "f.bin"; p.write_bytes(payload)
    import hashlib
    sha = hashlib.sha256(payload).hexdigest()
    snap = StagedSnapshot(
        repo_id="o/r", revision_requested="main", commit=commit,
        staging_dir=staging,
        files=[StagedFile(path="f.bin", local_path=p, size=len(payload),
                          etag=sha, sha256=sha)],
    )
    transaction.ingest(repo, snap)


def test_integrity_passes_on_clean_store(repo, tmp_path):
    _stage(repo, tmp_path)
    report = jobs.integrity(repo)
    assert report.ok is True
    assert report.checked >= 1
    assert report.problems == []


def test_integrity_detects_corruption(repo, tmp_path):
    _stage(repo, tmp_path)
    # Corrupt the one reachable blob in place.
    reachable = repo.collect_reachable_blobs()
    digest = next(iter(reachable))
    repo._storage.blobs.path_for(digest).write_bytes(b"tampered")

    report = jobs.integrity(repo)
    assert report.ok is False
    assert len(report.problems) == 1
    assert "corrupted" in report.problems[0]


def test_gc_deletes_orphans(repo, tmp_path):
    _stage(repo, tmp_path)
    repo.put_blob_bytes(b"orphan-1")
    repo.put_blob_bytes(b"orphan-2")

    report = jobs.gc(repo, min_age_seconds=0)
    assert report.acted == 2
    # Reachable blob still present.
    assert len(list(repo.iter_blobs())) == 1


def test_gc_dry_run_does_not_delete(repo, tmp_path):
    _stage(repo, tmp_path)
    repo.put_blob_bytes(b"orphan")
    before = len(list(repo.iter_blobs()))

    report = jobs.gc(repo, dry_run=True, min_age_seconds=0)
    assert report.acted == 1  # counted but not deleted
    assert len(list(repo.iter_blobs())) == before  # still there


def test_index_rebuild_reports_repo_count(repo, tmp_path):
    _stage(repo, tmp_path)
    ix = SQLiteIndex(tmp_path / "ix.sqlite")
    report = jobs.index_rebuild(repo, ix)
    assert report.ok is True
    assert report.acted == 1
    assert ix.list_repos() == ["o/r"]
    ix.close()


def test_run_all_returns_reports(repo, tmp_path):
    _stage(repo, tmp_path)
    reports = jobs.run_all(repo, index=NullIndex())
    names = [r.job for r in reports]
    assert "integrity" in names
    assert "gc" in names
    assert all(r.ok for r in reports)
