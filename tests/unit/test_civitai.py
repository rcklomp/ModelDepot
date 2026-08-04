"""Unit tests for the Civitai source adapter.

Network calls (_get_json, _download_following_redirects) are stubbed so the
tests are hermetic. The full fetch -> to_staged_snapshot -> ingest flow is
exercised with real on-disk files.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ingester import civitai_source, transaction


def _fake_metadata(version_id: int, files: list[dict]) -> dict:
    return {
        "modelId": 12345,
        "name": f"v{version_id}",
        "baseModel": "SDXL 1.0",
        "model": {"name": "Test Checkpoint", "type": "Checkpoint", "nsfw": False, "poi": False},
        "files": files,
    }


def test_fetch_downloads_files_and_captures_provenance(tmp_path: Path, monkeypatch):
    payload = b"safetensors weights here"
    sha = hashlib.sha256(payload).hexdigest()

    meta = _fake_metadata(987, [
        {"name": "model.safetensors", "type": "Model", "format": "SafeTensor",
         "sizeKB": len(payload) / 1024, "downloadUrl": "https://civitai.com/api/download/models/987",
         "hashes": {"SHA256": sha}},
    ])

    monkeypatch.setattr(civitai_source, "_get_json", lambda url, token=None: meta)
    monkeypatch.setattr(civitai_source, "_download_following_redirects",
                        lambda url, dest, token=None: dest.write_bytes(payload))

    staging = tmp_path / "stage"
    snap = civitai_source.fetch(987, staging, token="fake-token")

    assert snap.model_id == 12345
    assert snap.model_name == "Test Checkpoint"
    assert len(snap.files) == 1
    f = snap.files[0]
    assert f.name == "model.safetensors"
    assert f.sha256 == sha
    assert f.local_path.read_bytes() == payload
    assert f.size_bytes == len(payload)


def test_fetch_filters_by_type_and_format(tmp_path: Path, monkeypatch):
    meta = _fake_metadata(1, [
        {"name": "model.safetensors", "type": "Model", "format": "SafeTensor",
         "sizeKB": 1, "downloadUrl": "u1", "hashes": {}},
        {"name": "config.yaml", "type": "Config", "format": "Other",
         "sizeKB": 1, "downloadUrl": "u2", "hashes": {}},
    ])
    monkeypatch.setattr(civitai_source, "_get_json", lambda url, token=None: meta)
    monkeypatch.setattr(civitai_source, "_download_following_redirects",
                        lambda url, dest, token=None: dest.write_bytes(b"x"))

    snap = civitai_source.fetch(1, tmp_path / "s", token="t",
                                file_type="Model", file_format="SafeTensor")
    assert len(snap.files) == 1
    assert snap.files[0].name == "model.safetensors"


def test_fetch_requires_token(tmp_path: Path):
    with pytest.raises(civitai_source.CivitaiSourceError):
        civitai_source.fetch(1, tmp_path / "s", token="")


def test_fetch_raises_on_no_files(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(civitai_source, "_get_json",
                        lambda url, token=None: {"modelId": 1, "files": []})
    with pytest.raises(civitai_source.CivitaiSourceError):
        civitai_source.fetch(1, tmp_path / "s", token="t")


def test_synthesize_commit_is_deterministic(tmp_path: Path):
    f1 = civitai_source.CivitaiFile("a.safetensors", "Model", "SafeTensor", 100,
                                    "u", "a" * 64, tmp_path / "a")
    f2 = civitai_source.CivitaiFile("b.safetensors", "Model", "SafeTensor", 100,
                                    "u", "b" * 64, tmp_path / "b")
    c1 = civitai_source.synthesize_commit(42, [f1, f2])
    c2 = civitai_source.synthesize_commit(42, [f2, f1])  # order-insensitive
    assert c1 == c2
    assert len(c1) == 40


def test_to_staged_snapshot_adapts_for_transaction(tmp_path: Path):
    payload = b"data"
    sha = hashlib.sha256(payload).hexdigest()
    local = tmp_path / "m.safetensors"; local.write_bytes(payload)
    snap = civitai_source.CivitaiSnapshot(
        model_id=99, model_name="My Model!", version_id=5, version_name="v5",
        base_model="SD 1.5", license_name="base:SD 1.5", staging_dir=tmp_path,
        files=[civitai_source.CivitaiFile(
            name="m.safetensors", type="Model", format="SafeTensor",
            size_bytes=len(payload), download_url="u", sha256=sha, local_path=local)],
    )
    staged = civitai_source.to_staged_snapshot(snap)

    assert staged.repo_id == "civitai/My-Model"  # slugified
    assert len(staged.commit) == 40
    assert staged.files[0].path == "m.safetensors"
    assert staged.files[0].sha256 == sha
    assert staged.files[0].etag == sha  # sha256 echoed as etag


def test_full_civitai_ingest_publishes_and_serves_via_resolve(
    repo, tmp_path: Path, monkeypatch
):
    """End-to-end: fetch (stubbed) -> ingest -> served at /resolve."""
    from server import manifests

    payload = b"civitai model bytes" * 100
    sha = hashlib.sha256(payload).hexdigest()
    meta = _fake_metadata(777, [
        {"name": "ckpt.safetensors", "type": "Model", "format": "SafeTensor",
         "sizeKB": len(payload) / 1024, "downloadUrl": "https://civitai.com/dl/777",
         "hashes": {"SHA256": sha}},
    ])
    monkeypatch.setattr(civitai_source, "_get_json", lambda url, token=None: meta)
    monkeypatch.setattr(civitai_source, "_download_following_redirects",
                        lambda url, dest, token=None: dest.write_bytes(payload))

    staging = tmp_path / "civ-stage"
    snap = civitai_source.fetch(777, staging, token="t")
    staged = civitai_source.to_staged_snapshot(snap)
    result = transaction.ingest(repo, staged, source_kind="civitai",
                                source_ref="https://civitai.com/models/12345?modelVersionId=777")
    assert result.published

    # The Civitai model is now resolvable via the same /resolve/ path an HF
    # client or ComfyUI would use.
    rid = staged.repo_id
    files = repo.list_files(rid, "main")
    assert [f.path for f in files] == ["ckpt.safetensors"]

    resolved = repo.get_file(rid, "main", "ckpt.safetensors")
    assert resolved.digest == sha  # content-addressed by the real sha256
    with repo.open_blob(resolved.digest) as f:
        assert f.read() == payload
