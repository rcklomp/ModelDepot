"""Unit tests for the ingester CLI.

The network-touching `ingest-hf` is tested with a monkeypatched hf_source.fetch
so no real download happens. list/refs/gc/verify are exercised against a
populated store via MIRROR_ROOT.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ingester import cli, hf_source, transaction
from ingester.hf_source import StagedFile, StagedSnapshot


def _populate(mirror_root: Path) -> str:
    """Populate a store with one repo so list/refs/verify/gc have data."""
    import os
    os.environ["MIRROR_ROOT"] = str(mirror_root)
    os.environ["MIRROR_INDEX_BACKEND"] = "none"
    from server.config import load_settings
    from server.storage import FilesystemStorage
    from server.events import EventLog
    from server.repository import RepositoryCore

    settings = load_settings()
    storage = FilesystemStorage(settings); storage.ensure_layout()
    repo = RepositoryCore(storage, EventLog(storage.events_path))

    # Publish a tiny repo via the transaction layer with a fake snapshot.
    staging = mirror_root / "staging-test"; staging.mkdir()
    content = b"hello"
    (staging / "config.json").write_bytes(content)
    snapshot = StagedSnapshot(
        repo_id="org/model", revision_requested="main",
        commit="a" * 40, staging_dir=staging,
        files=[StagedFile(path="config.json", local_path=staging / "config.json",
                          size=len(content), etag=hashlib.sha256(content).hexdigest(),
                          sha256=hashlib.sha256(content).hexdigest())],
    )
    transaction.ingest(repo, snapshot)
    # Drop an orphan blob for GC to find.
    repo.put_blob_bytes(b"orphan-not-referenced")
    return snapshot.commit


def test_list_shows_repo(mirror_root: Path, capsys):
    _populate(mirror_root)
    rc = cli.main(["list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "org/model" in out


def test_refs_shows_main(mirror_root: Path, capsys):
    commit = _populate(mirror_root)
    rc = cli.main(["refs", "org/model"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "main" in out
    assert commit in out


def test_verify_reports_ok(mirror_root: Path, capsys):
    _populate(mirror_root)
    rc = cli.main(["verify", "org/model"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "1/1 verified" in out


def test_gc_deletes_orphan(mirror_root: Path, capsys):
    _populate(mirror_root)
    rc = cli.main(["gc"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "deleted" in out.lower() or "1 unreferenced" in out


def test_gc_dry_run_does_not_delete(mirror_root: Path, capsys):
    _populate(mirror_root)
    cli.main(["gc", "--dry-run"])
    out = capsys.readouterr().out
    assert "would delete" in out


def test_ingest_hf_with_stubbed_fetch(mirror_root: Path, capsys, monkeypatch):
    """ingest-hf should drive fetch + transaction end to end (fetch stubbed)."""
    import os
    os.environ["MIRROR_ROOT"] = str(mirror_root)

    def fake_fetch(repo_id, staging_dir, *, revision="main", token=None,
                   allow_patterns=None, ignore_patterns=None, endpoint=None, **_):
        # Write a fake file into staging and return a snapshot.
        content = b"fake model bytes"
        path = staging_dir / "config.json"
        path.write_bytes(content)
        return StagedSnapshot(
            repo_id=repo_id, revision_requested=revision, commit="b" * 40,
            staging_dir=staging_dir,
            files=[StagedFile(path="config.json", local_path=path,
                              size=len(content),
                              etag=hashlib.sha256(content).hexdigest(),
                              sha256=hashlib.sha256(content).hexdigest())],
        )

    monkeypatch.setattr(hf_source, "fetch", fake_fetch)
    rc = cli.main(["ingest-hf", "fakeorg/fakemodel", "--rev", "main"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "published" in out
    assert "main" in out

    # The repo is now in the store.
    cli.main(["list"])
    out = capsys.readouterr().out
    assert "fakeorg/fakemodel" in out


def test_ingest_hf_no_update_ref(mirror_root: Path, monkeypatch):
    import os
    os.environ["MIRROR_ROOT"] = str(mirror_root)

    def fake_fetch(repo_id, staging_dir, **kwargs):
        path = staging_dir / "f.txt"; path.write_bytes(b"x")
        return StagedSnapshot(
            repo_id=repo_id, revision_requested="main", commit="c" * 40,
            staging_dir=staging_dir,
            files=[StagedFile(path="f.txt", local_path=path, size=1,
                              etag="e", sha256=hashlib.sha256(b"x").hexdigest())],
        )
    monkeypatch.setattr(hf_source, "fetch", fake_fetch)

    rc = cli.main(["ingest-hf", "o/r", "--no-update-ref"])
    assert rc == 0
    # No ref, but the revision exists.
    rc2 = cli.main(["refs", "o/r"])
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.main(["refs", "o/r"])
    assert "no refs" in buf.getvalue().lower()
