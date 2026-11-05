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


# ---- shard-completeness guard ----------------------------------------

def _make_sharded_snapshot(repo_id: str, commit: str, staging: Path,
                           shard_names: list[str]) -> StagedSnapshot:
    """Build a snapshot with the given shard filenames (1 byte each)."""
    files: dict[str, bytes] = {name: b"x" for name in shard_names}
    return _make_snapshot(repo_id, commit, staging, files)


def test_complete_shards_ingest_ok(repo: RepositoryCore, tmp_path: Path):
    """All 4 shards present -> publishes normally, no error."""
    staging = tmp_path / "s"; staging.mkdir()
    snap = _make_sharded_snapshot("o/r", "a" * 40, staging, [
        "model-00001-of-00004.gguf",
        "model-00002-of-00004.gguf",
        "model-00003-of-00004.gguf",
        "model-00004-of-00004.gguf",
    ])
    result = transaction.ingest(repo, snap)
    assert result.published
    assert result.file_count == 4


def test_partial_shards_refused(repo: RepositoryCore, tmp_path: Path):
    """Only 2 of 4 shards -> IncompleteShards raised, nothing published."""
    staging = tmp_path / "s"; staging.mkdir()
    snap = _make_sharded_snapshot("o/r", "b" * 40, staging, [
        "model-00001-of-00004.gguf",
        "model-00002-of-00004.gguf",
        # 00003 and 00004 missing
    ])
    with pytest.raises(transaction.IncompleteShards) as exc_info:
        transaction.ingest(repo, snap)
    err = exc_info.value
    assert err.have == 2
    assert err.declared == 4
    assert "00003" in err.missing[0] and "00004" in err.missing[1]
    # Nothing was published.
    assert repo.list_revisions("o/r") == []


def test_partial_shards_override_publishes(repo: RepositoryCore, tmp_path: Path):
    """--allow-incomplete-shards publishes the partial set with a warning."""
    staging = tmp_path / "s"; staging.mkdir()
    snap = _make_sharded_snapshot("o/r", "c" * 40, staging, [
        "DeepSeek-V4-UD-IQ3_XXS-00001-of-00004.gguf",
        "DeepSeek-V4-UD-IQ3_XXS-00002-of-00004.gguf",
    ])
    result = transaction.ingest(repo, snap, allow_incomplete_shards=True)
    assert result.published
    assert result.file_count == 2


def test_non_sharded_files_unaffected(repo: RepositoryCore, tmp_path: Path):
    """Normal (non-sharded) filenames never trigger the guard."""
    staging = tmp_path / "s"; staging.mkdir()
    snap = _make_snapshot("o/r", "d" * 40, staging, {
        "config.json": b"{}", "model.safetensors": b"weights", "tokenizer.json": b"tok",
    })
    result = transaction.ingest(repo, snap)  # no error
    assert result.file_count == 3


def test_single_file_not_treated_as_shard(repo: RepositoryCore, tmp_path: Path):
    """A '-of-00001' file (total=1) is not a real shard; no guard triggered."""
    staging = tmp_path / "s"; staging.mkdir()
    snap = _make_sharded_snapshot("o/r", "e" * 40, staging, [
        "model-00001-of-00001.gguf",
    ])
    result = transaction.ingest(repo, snap)  # no error
    assert result.file_count == 1


def test_multiple_shard_groups_checked_independently(repo: RepositoryCore, tmp_path: Path):
    """Two different sharded models in one snapshot — each must be complete."""
    staging = tmp_path / "s"; staging.mkdir()
    # Model A complete (2/2), Model B incomplete (1/3).
    snap = _make_sharded_snapshot("o/r", "f" * 40, staging, [
        "a-00001-of-00002.gguf", "a-00002-of-00002.gguf",
        "b-00001-of-00003.gguf",
    ])
    with pytest.raises(transaction.IncompleteShards) as exc_info:
        transaction.ingest(repo, snap)
    # The error should be about model b (the incomplete one).
    assert exc_info.value.declared == 3
    assert exc_info.value.have == 1


# ---- the same upstream commit pulled in several parts (issue #2) --------------------------------------------------

def _ingest(repo, tmp_path, name, commit, files, **kw):
    staging = tmp_path / name
    staging.mkdir()
    return transaction.ingest(repo, _make_snapshot("o/r", commit, staging, files), **kw)


def test_a_second_part_of_the_same_commit_is_added_to_the_revision(repo: RepositoryCore, tmp_path: Path):
    commit = "a1" * 20
    first = _ingest(repo, tmp_path, "s1", commit, {"README.md": b"readme"})
    second = _ingest(repo, tmp_path, "s2", commit, {"mtp.gguf": b"weights" * 100, "README.md": b"readme"})
    assert first.published is True and first.added == 0
    assert second.published is False and second.added == 1                      # the revision existed; one file arrived
    assert sorted(f.path for f in repo.list_files("o/r", commit)) == ["README.md", "mtp.gguf"]
    assert repo.list_revisions("o/r") == [commit]                               # still one revision, same commit id
    assert repo.get_file("o/r", "main", "mtp.gguf").size == len(b"weights" * 100)
    assert repo.open_blob(repo.get_file("o/r", "main", "mtp.gguf").digest).read() == b"weights" * 100


def test_pulling_what_is_already_there_changes_nothing(repo: RepositoryCore, tmp_path: Path):
    commit = "b2" * 20
    _ingest(repo, tmp_path, "s1", commit, {"a.txt": b"A", "b.txt": b"B"})
    path = repo._storage.manifest_path("o/r", commit)
    before = path.read_bytes()
    again = _ingest(repo, tmp_path, "s2", commit, {"a.txt": b"A"})
    assert again.published is False and again.added == 0
    assert path.read_bytes() == before                                          # not even rewritten


def test_a_path_with_different_content_in_the_same_commit_is_refused(repo: RepositoryCore, tmp_path: Path):
    commit = "c3" * 20
    _ingest(repo, tmp_path, "s1", commit, {"a.txt": b"original", "b.txt": b"B"})
    path = repo._storage.manifest_path("o/r", commit)
    before = path.read_bytes()
    with pytest.raises(transaction.IngestError, match="a.txt"):
        _ingest(repo, tmp_path, "s2", commit, {"a.txt": b"DIFFERENT", "c.txt": b"C"})
    assert path.read_bytes() == before                                          # nothing was half-added


def test_the_missing_shard_can_be_added_later_and_an_old_gap_does_not_block_other_files(repo: RepositoryCore, tmp_path: Path):
    commit = "d4" * 20
    s1, s2 = "m-00001-of-00002.gguf", "m-00002-of-00002.gguf"
    _ingest(repo, tmp_path, "s1", commit, {s1: b"one"}, allow_incomplete_shards=True)       # published with a gap, on purpose
    other = _ingest(repo, tmp_path, "s2", commit, {"notes.txt": b"n"})                       # an unrelated file: not blocked by the old gap
    assert other.added == 1
    done = _ingest(repo, tmp_path, "s3", commit, {s2: b"two"})                               # the other shard alone: judged with what is there
    assert done.added == 1
    assert sorted(f.path for f in repo.list_files("o/r", commit)) == sorted([s1, s2, "notes.txt"])
    with pytest.raises(transaction.IncompleteShards):                                         # a NEW gap is still refused
        _ingest(repo, tmp_path, "s4", commit, {"x-00001-of-00003.gguf": b"x"})


def test_two_ingests_of_one_commit_at_the_same_time_lose_nothing(repo: RepositoryCore, tmp_path: Path):
    import threading
    commit = "e5" * 20
    snaps = []
    for i in range(6):
        staging = tmp_path / f"t{i}"
        staging.mkdir()
        snaps.append(_make_snapshot("o/r", commit, staging, {f"f{i}.bin": bytes([i]) * 50}))
    barrier, errors = threading.Barrier(len(snaps)), []

    def go(s):
        try:
            barrier.wait()
            transaction.ingest(repo, s)
        except Exception as exc:                                                # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(s,)) for s in snaps]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors, errors
    assert sorted(f.path for f in repo.list_files("o/r", commit)) == [f"f{i}.bin" for i in range(6)]
