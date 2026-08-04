"""Unit tests for the Repository Core (publish/read/GC lifecycle).

These exercise the full domain flow without any HTTP or HF knowledge:
  put blobs -> publish revision -> update ref -> read back -> GC.
"""
from __future__ import annotations

import pytest

from server import manifests, events
from server.manifests import (
    FileEntry, License, Manifest, RevisionInfo, Source, SCHEMA_VERSION,
)
from server.repository import (
    FileNotFound,
    RepositoryCore,
    RepositoryNotFound,
    RevisionExists,
    RevisionNotFound,
)


def _make_manifest(repo_id: str, commit: str, files: list[FileEntry]) -> Manifest:
    return Manifest(
        schema_version=SCHEMA_VERSION,
        repo_id=repo_id,
        source=Source(kind="huggingface", ref="https://example/" + repo_id),
        revision=RevisionInfo(
            commit=commit, fetched_at="2026-08-04T12:00:00+00:00",
            license=License(name="mit"),
        ),
        files=files,
    )


def _file(path: str, digest: str, size: int) -> FileEntry:
    return FileEntry(
        path=path, size=size, blob=f"sha256:{digest}",
        checksums={"sha256": digest},
        metadata={"hf": {"etag": f"e-{path}"}},
    )


# ---- publish + read ----------------------------------------------------

def test_publish_then_read_via_ref(repo: RepositoryCore):
    blob = repo.put_blob_bytes(b"config content")
    m = _make_manifest("org/repo", "c0ffee",
                       [_file("config.json", blob.digest, len(b"config content"))])
    repo.publish_revision(m)
    repo.update_reference("org/repo", "main", "c0ffee")

    assert repo.list_repos() == ["org/repo"]
    assert repo.list_references("org/repo") == {"main": "c0ffee"}
    assert repo.list_revisions("org/repo") == ["c0ffee"]

    files = repo.list_files("org/repo", "main")
    assert [f.path for f in files] == ["config.json"]

    resolved = repo.get_file("org/repo", "main", "config.json")
    assert resolved.digest == blob.digest
    assert resolved.size == len(b"config content")
    assert resolved.metadata["hf"]["etag"] == "e-config.json"


def test_resolve_ref_accepts_literal_commit(repo: RepositoryCore):
    blob = repo.put_blob_bytes(b"x")
    commit = "abcdef1234567890abcdef1234567890abcdef12"
    m = _make_manifest("o/r", commit, [_file("f", blob.digest, 1)])
    repo.publish_revision(m)

    # No ref set, but the raw commit resolves.
    assert repo.resolve_ref("o/r", commit) == commit


def test_open_blob_streams_content(repo: RepositoryCore):
    blob = repo.put_blob_bytes(b"streamable body")
    with repo.open_blob(blob.digest) as f:
        assert f.read() == b"streamable body"


def test_publish_is_immutable(repo: RepositoryCore):
    blob = repo.put_blob_bytes(b"x")
    m = _make_manifest("o/r", "c1", [_file("f", blob.digest, 1)])
    repo.publish_revision(m)

    with pytest.raises(RevisionExists):
        repo.publish_revision(m)  # same commit -> refused


def test_unknown_repo_raises(repo: RepositoryCore):
    with pytest.raises(RepositoryNotFound):
        repo.resolve_ref("no/such", "main")


def test_unknown_revision_raises(repo: RepositoryCore):
    blob = repo.put_blob_bytes(b"x")
    repo.publish_revision(_make_manifest("o/r", "c1", [_file("f", blob.digest, 1)]))
    with pytest.raises(RevisionNotFound):
        repo.get_manifest("o/r", "does-not-exist")


def test_unknown_file_raises(repo: RepositoryCore):
    blob = repo.put_blob_bytes(b"x")
    repo.publish_revision(
        _make_manifest("o/r", "c1", [_file("f.txt", blob.digest, 1)])
    )
    with pytest.raises(FileNotFound):
        repo.get_file("o/r", "c1", "other.txt")


# ---- ref updates enable rollback --------------------------------------

def test_rollback_by_moving_ref(repo: RepositoryCore):
    b1 = repo.put_blob_bytes(b"v1 content")
    b2 = repo.put_blob_bytes(b"v2 content")
    repo.publish_revision(_make_manifest("o/r", "c1", [_file("f", b1.digest, 10)]))
    repo.publish_revision(_make_manifest("o/r", "c2", [_file("f", b2.digest, 10)]))
    repo.update_reference("o/r", "main", "c2")
    assert repo.get_file("o/r", "main", "f").digest == b2.digest

    # Rollback: move main back to c1.
    repo.update_reference("o/r", "main", "c1")
    assert repo.get_file("o/r", "main", "f").digest == b1.digest


# ---- events ------------------------------------------------------------

def test_events_emitted_for_full_flow(repo: RepositoryCore, storage):
    blob = repo.put_blob_bytes(b"data")
    m = _make_manifest("o/r", "c1", [_file("f", blob.digest, 4)])
    repo.publish_revision(m)
    repo.update_reference("o/r", "main", "c1")

    types = [e["type"] for e in repo._events.iter_events()]
    assert events.BLOB_STORED in types
    assert events.REVISION_PUBLISHED in types
    assert events.REFERENCE_UPDATED in types


# ---- GC ----------------------------------------------------------------

def test_collect_reachable_and_delete_orphan(repo: RepositoryCore):
    reachable_blob = repo.put_blob_bytes(b"kept")
    orphan_blob = repo.put_blob_bytes(b"orphan")  # never referenced

    repo.publish_revision(
        _make_manifest("o/r", "c1", [_file("f", reachable_blob.digest, 4)])
    )

    reachable = repo.collect_reachable_blobs()
    assert reachable_blob.digest in reachable
    assert orphan_blob.digest not in reachable

    # GC the orphan.
    assert repo.delete_blob(orphan_blob.digest) is True
    assert not repo._storage.blobs.exists(orphan_blob.digest)
    # Reachable blob untouched.
    assert repo._storage.blobs.exists(reachable_blob.digest)


def test_gc_records_with_refs(repo: RepositoryCore):
    b = repo.put_blob_bytes(b"x")
    repo.publish_revision(_make_manifest("o/r", "c1", [_file("f.txt", b.digest, 1)]))
    refs_map = repo.collect_reachable_blobs_with_refs()
    assert refs_map[b.digest] == [("o/r", "c1", "f.txt")]


def test_verify_blob(repo: RepositoryCore):
    b = repo.put_blob_bytes(b"clean")
    assert repo.verify_blob(b.digest) is True
