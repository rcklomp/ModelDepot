"""HF HTTP contract tests.

These assert the exact response shape that `huggingface_hub` requires (the three
mandatory headers on /resolve, the metadata JSON shape, the tree listing), then
exercise the full HEAD-then-GET download flow the way `huggingface_hub` does it.

The M2 exit criterion: a client setting
    HF_ENDPOINT=http://localhost:<port>
can resolve and download a model file from the mirror. We use FastAPI's test
client (httpx under the hood) rather than spinning a real socket, which is
faster and deterministic.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import events
from server.config import Settings
from server.main import create_app
from server.storage import FilesystemStorage

from .helpers import stage_fake_repo


@pytest.fixture
def client(mirror_root: Path):
    """A test client backed by a fresh store with one staged fake repo."""
    from server.config import Settings
    from server.storage import FilesystemStorage
    from server.repository import RepositoryCore

    settings_obj = Settings(
        mirror_root=mirror_root, host="127.0.0.1", port=0,
        base_url="http://test", public_url="http://test",
        read_only=False, auth_token="", index_backend="none",
    )
    storage = FilesystemStorage(settings_obj)
    storage.ensure_layout()
    event_log = events.EventLog(storage.events_path)
    repo = RepositoryCore(storage, event_log)
    stage_fake_repo(repo)

    from server.main import create_app
    app = create_app(settings_override=settings_obj, repo_override=repo)

    with TestClient(app) as c:
        yield c


REPO = "tinyorg/tinyconfig"


# ---- /api/models metadata ---------------------------------------------

def test_model_info_returns_required_fields(client):
    r = client.get(f"/api/models/{REPO}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == REPO
    assert body["modelId"] == REPO
    assert body["sha"] and len(body["sha"]) >= 7
    assert body["private"] is False
    assert body["gated"] is False
    siblings = {s["rfilename"] for s in body["siblings"]}
    assert "config.json" in siblings
    assert "pytorch_model.bin" in siblings


def test_model_info_unknown_repo_is_404(client):
    r = client.get("/api/models/no/such")
    assert r.status_code == 404


# ---- /api/models/.../tree ---------------------------------------------

def test_tree_lists_files(client):
    r = client.get(f"/api/models/{REPO}/tree/main")
    assert r.status_code == 200
    entries = r.json()
    paths = {e["path"] for e in entries if e["type"] == "file"}
    assert "config.json" in paths
    assert "pytorch_model.bin" in paths


# ---- /resolve: the load-bearing contract ------------------------------

def test_resolve_get_returns_mandatory_hf_headers(client):
    r = client.get(f"/{REPO}/resolve/main/config.json")
    assert r.status_code == 200, r.text
    # The three headers huggingface_hub hard-requires:
    assert r.headers.get("X-Repo-Commit"), "X-Repo-Commit missing"
    assert r.headers.get("ETag"), "ETag missing"
    assert r.headers.get("Content-Length"), "Content-Length missing"
    # Bonus: the preferred X-Linked-* variants and Range support.
    assert r.headers.get("X-Linked-Etag")
    assert r.headers.get("X-Linked-Size")
    assert r.headers.get("Accept-Ranges") == "bytes"


def test_resolve_get_returns_correct_bytes(client):
    r = client.get(f"/{REPO}/resolve/main/config.json")
    body = r.json()  # config.json is JSON
    assert body["model_type"] == "llama"
    assert r.headers["Content-Length"] == str(len(r.content))


def test_resolve_head_returns_headers_no_body(client):
    r = client.head(f"/{REPO}/resolve/main/config.json")
    assert r.status_code == 200
    assert r.headers.get("X-Repo-Commit")
    assert r.headers.get("ETag")
    assert r.headers.get("Content-Length")
    assert r.content == b""


def test_resolve_unknown_file_is_404(client):
    r = client.get(f"/{REPO}/resolve/main/nope.json")
    assert r.status_code == 404


def test_resolve_honors_range_request(client):
    # Full body is 64 bytes; ask for bytes 0-15.
    r = client.get(f"/{REPO}/resolve/main/pytorch_model.bin",
                   headers={"Range": "bytes=0-15"})
    assert r.status_code == 206
    assert r.headers["Content-Range"] == f"bytes 0-15/64"
    assert r.headers["Content-Length"] == "16"
    assert len(r.content) == 16


def test_resolve_range_suffix(client):
    r = client.get(f"/{REPO}/resolve/main/pytorch_model.bin",
                   headers={"Range": "bytes=-8"})
    assert r.status_code == 206
    assert len(r.content) == 8


def test_resolve_range_open_ended(client):
    r = client.get(f"/{REPO}/resolve/main/pytorch_model.bin",
                   headers={"Range": "bytes=56-"})
    assert r.status_code == 206
    assert len(r.content) == 8  # 64-56


# ---- refs + commits ---------------------------------------------------

def test_refs(client):
    r = client.get(f"/api/models/{REPO}/refs")
    assert r.status_code == 200
    names = {b["name"] for b in r.json()["branches"]}
    assert "main" in names


def test_commits(client):
    r = client.get(f"/api/models/{REPO}/commits/main")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["oid"]


# ---- the full HF client download flow --------------------------------

def test_full_hf_hub_download_flow(client):
    """Mimic what huggingface_hub does: HEAD for metadata, then GET for bytes,
    checking the headers it derives the cache key from."""
    # 1. HEAD to get metadata.
    head = client.head(f"/{REPO}/resolve/main/config.json")
    assert head.status_code == 200
    commit = head.headers["X-Repo-Commit"]
    etag = head.headers["ETag"]
    size = int(head.headers["Content-Length"])

    # 2. GET to download.
    get = client.get(f"/{REPO}/resolve/main/config.json")
    assert get.status_code == 200
    assert len(get.content) == size
    # The GET must return the same metadata (cache key consistency).
    assert get.headers["X-Repo-Commit"] == commit
    assert get.headers["ETag"] == etag

    # huggingface_hub keys its cache on (commit, etag); both must be stable.
    assert commit and etag
