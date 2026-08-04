"""Unit tests for the index backends."""
from __future__ import annotations

from pathlib import Path

import pytest

from server import index
from server.index import NullIndex, SQLiteIndex


def test_null_index_is_all_misses():
    ix = NullIndex()
    assert ix.list_repos() == []
    assert ix.list_commits("o/r") == []
    assert ix.has_blob("a" * 64) is False


def test_open_index_factory_returns_right_type(tmp_path: Path):
    assert isinstance(index.open_index("sqlite", tmp_path), SQLiteIndex)
    assert isinstance(index.open_index("none", tmp_path), NullIndex)
    assert isinstance(index.open_index("", tmp_path), SQLiteIndex)  # default
    with pytest.raises(ValueError):
        index.open_index("redis", tmp_path)


def test_sqlite_rebuild_indexes_repos_commits_blobs(storage, repo, tmp_path: Path):
    # Stage a repo with one revision and two blobs via the core.
    from server import manifests
    from server.manifests import FileEntry, License, Manifest, RevisionInfo, Source

    b1 = repo.put_blob_bytes(b"blob one")
    b2 = repo.put_blob_bytes(b"blob two")
    m = Manifest(
        schema_version=1,
        repo_id="org/repo",
        source=Source(kind="huggingface", ref="u"),
        revision=RevisionInfo(
            commit="deadbeef", fetched_at="2026-08-04T12:00:00+00:00",
            license=License(name="mit"),
        ),
        files=[
            FileEntry(path="a.txt", size=8, blob=f"sha256:{b1.digest}",
                      checksums={"sha256": b1.digest}),
            FileEntry(path="b.txt", size=8, blob=f"sha256:{b2.digest}",
                      checksums={"sha256": b2.digest}),
        ],
    )
    repo.publish_revision(m)
    repo.update_reference("org/repo", "main", "deadbeef")

    ix = SQLiteIndex(tmp_path / "index.sqlite")
    ix.rebuild(storage.repos_dir)

    assert ix.list_repos() == ["org/repo"]
    assert ix.list_commits("org/repo") == ["deadbeef"]
    assert ix.has_blob(b1.digest)
    assert ix.has_blob(b2.digest)
    assert not ix.has_blob("0" * 64)
    ix.close()


def test_sqlite_rebuild_is_idempotent(storage, repo, tmp_path: Path):
    ix = SQLiteIndex(tmp_path / "index.sqlite")
    ix.rebuild(storage.repos_dir)   # empty
    ix.rebuild(storage.repos_dir)   # again
    assert ix.list_repos() == []
    ix.close()
