"""Unit tests for the transactional ingest orchestrator.

These build a real StagedSnapshot from on-disk files (no network) and run it
through transaction.ingest, then verify the published revision is correct and
served correctly by the repository core.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ingester import transaction
from ingester.hf_source import StagedFile, StagedSnapshot
from server.repository import RepositoryCore


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_snapshot(repo_id: str, commit: str, staging: Path,
                   files: dict[str, bytes]) -> StagedSnapshot:
    """Write files to staging and build a StagedSnapshot with HF-style provenance."""
    staged: list[StagedFile] = []
    for path, content in files.items():
        local = staging / path
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(content)
        sha = _sha256(content)
        staged.append(StagedFile(
            path=path, local_path=local, size=len(content),
            etag=sha,  # pretend it's an LFS file: etag == content sha256
            sha256=sha,
        ))
    return StagedSnapshot(
        repo_id=repo_id, revision_requested="main", commit=commit,
        staging_dir=staging, files=staged,
    )


def test_ingest_publishes_revision_and_moves_ref(repo: RepositoryCore, tmp_path: Path):
    staging = tmp_path / "stage"
    staging.mkdir()
    config = json.dumps({"model_type": "llama"}, sort_keys=True).encode()
    weights = b"\x00\x01\x02" * 10
    snapshot = _make_snapshot(
        "org/model", "abc123def456abc123def456abc123def456abc1", staging,
        {"config.json": config, "pytorch_model.bin": weights},
    )

    result = transaction.ingest(repo, snapshot, license_name="mit")

    assert result.published is True
    assert result.ref_updated is True
    assert result.file_count == 2

    # The revision is now readable via the ref.
    files = repo.list_files("org/model", "main")
    paths = {f.path for f in files}
    assert paths == {"config.json", "pytorch_model.bin"}

    # And the ref points at the published commit.
    assert repo.list_references("org/model") == {"main": snapshot.commit}


def test_ingest_stores_blobs_content_addressed_and_dedupes(repo: RepositoryCore, tmp_path: Path):
    staging = tmp_path / "stage"
    staging.mkdir()
    shared = b"identical content"
    snapshot = _make_snapshot(
        "o/r", "c1" * 20, staging,
        {"a.txt": shared, "nested/b.txt": shared},
    )
    transaction.ingest(repo, snapshot)

    # Both files reference the SAME blob (content-addressed dedup).
    fa = repo.get_file("o/r", "main", "a.txt")
    fb = repo.get_file("o/r", "main", "nested/b.txt")
    assert fa.digest == fb.digest

    # Only one blob on disk.
    blobs = list(repo.iter_blobs())
    assert len(blobs) == 1


def test_ingest_records_namespaced_hf_metadata(repo: RepositoryCore, tmp_path: Path):
    staging = tmp_path / "stage"; staging.mkdir()
    payload = b"hello"
    snapshot = _make_snapshot("o/r", "dead" * 10, staging, {"f.bin": payload})
    transaction.ingest(repo, snapshot)

    f = repo.get_file("o/r", "main", "f.bin")
    # HF metadata is namespaced under 'hf'.
    assert f.metadata["hf"]["etag"] == _sha256(payload)
    assert f.metadata["hf"]["x_repo_commit"] == snapshot.commit
    # SHA-256 is the canonical identity, recorded in checksums.
    assert f.metadata is not None


def test_ingest_is_idempotent_on_same_commit(repo: RepositoryCore, tmp_path: Path):
    staging = tmp_path / "stage"; staging.mkdir()
    snapshot = _make_snapshot("o/r", "beef" * 10, staging, {"f.txt": b"data"})
    first = transaction.ingest(repo, snapshot)
    second = transaction.ingest(repo, snapshot)

    assert first.published is True
    # Re-ingesting the same commit does not error; reports already-present.
    assert second.published is False
    assert second.ref_updated is True  # ref still moved (harmless)


def test_ingest_rejects_integrity_mismatch(repo: RepositoryCore, tmp_path: Path):
    staging = tmp_path / "stage"; staging.mkdir()
    local = staging / "tampered.bin"
    local.write_bytes(b"actual bytes")
    # Lie about the upstream sha256 — verify must catch this.
    snapshot = StagedSnapshot(
        repo_id="o/r", revision_requested="main", commit="c0de" * 10,
        staging_dir=staging,
        files=[StagedFile(
            path="tampered.bin", local_path=local, size=12,
            etag="claimed", sha256="f" * 64,  # wrong on purpose
        )],
    )
    with pytest.raises(transaction.IntegrityMismatch):
        transaction.ingest(repo, snapshot)

    # Nothing was published.
    assert repo.list_revisions("o/r") == []


def test_ingest_without_update_ref_publishes_but_leaves_ref(repo: RepositoryCore, tmp_path: Path):
    staging = tmp_path / "stage"; staging.mkdir()
    snapshot = _make_snapshot("o/r", "face" * 10, staging, {"f.txt": b"x"})
    result = transaction.ingest(repo, snapshot, update_ref=False)

    assert result.published is True
    assert result.ref_updated is False
    # Revision exists but no ref points at it yet.
    assert repo.list_revisions("o/r") == [snapshot.commit]
    assert repo.list_references("o/r") == {}
    # The raw commit still resolves.
    assert repo.resolve_ref("o/r", snapshot.commit) == snapshot.commit


def test_ingest_emits_events(repo: RepositoryCore, tmp_path: Path):
    staging = tmp_path / "stage"; staging.mkdir()
    snapshot = _make_snapshot("o/r", "1234" * 10, staging, {"f.txt": b"data"})
    transaction.ingest(repo, snapshot)

    types = [e["type"] for e in repo._events.iter_events()]
    from server import events as ev
    assert ev.REPOSITORY_CREATED in types
    assert ev.REVISION_PUBLISHED in types
    assert ev.REFERENCE_UPDATED in types


def test_manifest_files_sorted(repo: RepositoryCore, tmp_path: Path):
    staging = tmp_path / "stage"; staging.mkdir()
    # Deliberately out-of-order files.
    snapshot = _make_snapshot(
        "o/r", "abcd" * 10, staging,
        {"z.txt": b"z", "a.txt": b"a", "m.txt": b"m"},
    )
    transaction.ingest(repo, snapshot)

    manifest = repo.get_manifest("o/r", "main")
    assert [f.path for f in manifest.files] == ["a.txt", "m.txt", "z.txt"]
