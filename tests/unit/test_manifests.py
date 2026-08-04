"""Unit tests for versioned, deterministic manifests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from server import manifests
from server.manifests import (
    FileEntry,
    License,
    Manifest,
    ManifestError,
    RevisionInfo,
    Source,
    SCHEMA_VERSION,
)


def _sample_manifest(file_order: list[str]) -> Manifest:
    return Manifest(
        schema_version=SCHEMA_VERSION,
        repo_id="org/repo",
        source=Source(kind="huggingface", ref="https://example/org/repo"),
        revision=RevisionInfo(
            commit="abc123", fetched_at="2026-08-04T12:00:00+00:00",
            license=License(name="mit"),
        ),
        files=[
            FileEntry(
                path=p,
                size=10,
                blob="sha256:" + (p * 64)[:64],
                checksums={"sha256": (p * 64)[:64]},
                metadata={"hf": {"etag": f"e-{p}"}},
            )
            for p in file_order
        ],
    )


def test_files_sorted_on_serialise():
    m = _sample_manifest(["zeta.txt", "alpha.json", "mid.bin"])
    out = m.to_dict()
    paths = [f["path"] for f in out["files"]]
    assert paths == ["alpha.json", "mid.bin", "zeta.txt"]


def test_dump_json_is_deterministic():
    a = _sample_manifest(["b", "a", "c"])
    b = _sample_manifest(["c", "a", "b"])
    # Regardless of input order, the serialised form is identical.
    assert manifests.dump_json(a) == manifests.dump_json(b)


def test_schema_version_present_in_output():
    m = _sample_manifest(["a"])
    assert m.to_dict()["schema_version"] == SCHEMA_VERSION


def test_namespaced_metadata_preserved():
    entry = FileEntry(
        path="a", size=10, blob="sha256:" + "a" * 64,
        checksums={"sha256": "a" * 64},
        metadata={"hf": {"etag": "x"}, "civitai": {"version_id": 42}},
    )
    m = Manifest(
        schema_version=SCHEMA_VERSION,
        repo_id="org/repo",
        source=Source(kind="huggingface", ref="u"),
        revision=RevisionInfo(commit="c", fetched_at="t"),
        files=[entry],
    )
    out = m.to_dict()
    assert out["files"][0]["metadata"]["hf"]["etag"] == "x"
    assert out["files"][0]["metadata"]["civitai"]["version_id"] == 42


def test_write_atomic_then_read_roundtrip(tmp_path: Path):
    m = _sample_manifest(["a", "b"])
    dest = tmp_path / "manifest.json"
    manifests.write_atomic(m, dest)
    assert dest.is_file()
    loaded = manifests.read(dest)
    assert loaded.repo_id == "org/repo"
    assert loaded.revision.commit == "abc123"
    assert [f.path for f in loaded.files] == ["a", "b"]


def test_read_rejects_wrong_schema_version(tmp_path: Path):
    bad = {
        "schema_version": 99,
        "repo_id": "x/y",
        "source": {"kind": "huggingface", "ref": "u"},
        "revision": {"commit": "c", "fetched_at": "t"},
        "files": [],
    }
    p = tmp_path / "m.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(ManifestError):
        manifests.read(p)


def test_read_rejects_missing_fields(tmp_path: Path):
    bad = {"schema_version": SCHEMA_VERSION, "repo_id": "x/y"}  # no source/revision
    p = tmp_path / "m.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(ManifestError):
        manifests.read(p)
