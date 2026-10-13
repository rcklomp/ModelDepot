"""Engine compatibility validation matrix.

Each engine consumes the mirror differently. This suite exercises the exact
HTTP contract each one relies on, so a regression that would break an engine
is caught here in CI rather than in the field.

Engines grouped by consumption pattern:
  - huggingface_hub-based (transformers, diffusers, vLLM, TGI, MLX, hf-cli):
    one real huggingface_hub integration test covers all of them.
  - Ollama (hf.co/<repo>:<quant>): pulls via the HF resolve flow.
  - llama.cpp (--model-url): streams a direct /resolve/ URL.
  - LM Studio: documented exception (local-file import), not HTTP-driven.

These run against an in-process TestClient (fast, hermetic). The huggingface_hub
test uses a real socket server so the library's own httpx client drives it.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import events
from server.config import Settings
from server.storage import FilesystemStorage
from server.repository import RepositoryCore

# Reuse the contract helpers' stager.
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from contract.helpers import stage_fake_repo  # noqa: E402

REPO = "tinyorg/tinyconfig"


@pytest.fixture
def client(mirror_root: Path):
    """TestClient backed by a fresh store with a staged fake repo."""
    settings = Settings(
        mirror_root=mirror_root, host="127.0.0.1", port=0,
        base_url="http://test", public_url="http://test",
        read_only=False, auth_token="", index_backend="none",
    )
    storage = FilesystemStorage(settings); storage.ensure_layout()
    elog = events.EventLog(storage.events_path)
    repo = RepositoryCore(storage, elog)
    stage_fake_repo(repo)
    from server.main import create_app
    app = create_app(settings_override=settings, repo_override=repo)
    with TestClient(app, headers={"X-Console-Request": "1"}) as c:
        yield c


@pytest.fixture
def live_server(mirror_root: Path):
    """A real socket server (for huggingface_hub's own httpx client).

    huggingface_hub talks to HF_ENDPOINT with its own httpx transport, which
    won't drive a TestClient. So we spin a uvicorn server on a free port and
    point HF_ENDPOINT at it. The HF env vars MUST be set before importing
    huggingface_hub — done per-test via monkeypatch + importlib.reload.
    """
    import importlib
    settings = Settings(
        mirror_root=mirror_root, host="127.0.0.1", port=0,
        base_url="http://test", public_url="http://test",
        read_only=False, auth_token="", index_backend="none",
    )
    storage = FilesystemStorage(settings); storage.ensure_layout()
    elog = events.EventLog(storage.events_path)
    repo = RepositoryCore(storage, elog)
    stage_fake_repo(repo)
    from server.main import create_app
    app = create_app(settings_override=settings, repo_override=repo)

    # Pick a free port.
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()

    import uvicorn
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(cfg)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    time.sleep(1.0)
    yield f"http://127.0.0.1:{port}", repo
    srv.should_exit = True


# =========================================================================
# Group 1: huggingface_hub-based engines (transformers, vLLM, TGI, MLX, hf-cli)
# One real huggingface_hub download covers all of them.
# =========================================================================

def test_huggingface_hub_download_via_HF_ENDPOINT(live_server, monkeypatch):
    """The contract every huggingface_hub-based engine depends on.

    If this passes, transformers/diffusers/vLLM/TGI/MLX all work, because they
    all funnel through huggingface_hub.snapshot_download/hf_hub_download.
    """
    endpoint, _ = live_server
    monkeypatch.setenv("HF_ENDPOINT", endpoint)
    monkeypatch.setenv("HF_HUB_DISABLE_XET", "1")
    monkeypatch.setenv("HF_HUB_DOWNLOAD_TIMEOUT", "15")

    # huggingface_hub reads HF_ENDPOINT at import of its constants module.
    import huggingface_hub.constants as hc
    importlib = __import__("importlib")
    importlib.reload(hc)

    from huggingface_hub import hf_hub_download
    path = hf_hub_download(REPO, "config.json", revision="main",
                           local_dir=str(__import__("tempfile").mkdtemp()))
    data = json.load(open(path))
    assert data["model_type"] == "llama"


def test_huggingface_hub_metadata_round_trip(live_server, monkeypatch):
    """get_hf_file_metadata reads the three HF headers — the cache-key contract."""
    endpoint, _ = live_server
    monkeypatch.setenv("HF_ENDPOINT", endpoint)
    monkeypatch.setenv("HF_HUB_DISABLE_XET", "1")
    import huggingface_hub.constants as hc
    __import__("importlib").reload(hc)
    from huggingface_hub import get_hf_file_metadata
    url = hc.HUGGINGFACE_CO_URL_TEMPLATE.format(
        repo_id=REPO, revision="main", filename="config.json")
    meta = get_hf_file_metadata(url)
    assert meta.commit_hash, "X-Repo-Commit missing"
    assert meta.etag, "ETag missing"
    assert meta.size is not None, "size missing"


# =========================================================================
# Group 2: Ollama — pulls GGUFs via hf.co/<repo>:<quant>, honors HF_ENDPOINT.
# We test the resolve flow Ollama relies on (HEAD then GET of a GGUF-ish file).
# =========================================================================

def test_ollama_resolve_flow(client):
    """Ollama's hf.co puller issues HEAD then GET on the resolve endpoint.

    We don't run Ollama here (it's a separate binary), but we verify the exact
    HTTP flow its puller performs works against the mirror.
    """
    # HEAD for metadata
    head = client.head(f"/{REPO}/resolve/main/pytorch_model.bin")
    assert head.status_code == 200
    commit = head.headers["X-Repo-Commit"]
    etag = head.headers["ETag"]
    size = int(head.headers["Content-Length"])

    # GET for the bytes
    get = client.get(f"/{REPO}/resolve/main/pytorch_model.bin")
    assert get.status_code == 200
    assert len(get.content) == size
    # Metadata must be consistent (Ollama verifies).
    assert get.headers["X-Repo-Commit"] == commit
    assert get.headers["ETag"] == etag


# =========================================================================
# Group 3: llama.cpp — streams a direct /resolve/ URL via --model-url.
# =========================================================================

def test_llama_cpp_direct_url_stream(client):
    """llama.cpp --model-url just streams bytes from the resolve URL.

    No HF headers needed (it ignores them), but Range support matters for
    resume. Verify a plain GET streams the full file.
    """
    r = client.get(f"/{REPO}/resolve/main/pytorch_model.bin")
    assert r.status_code == 200
    assert len(r.content) == 64  # the fixture weights are 64 bytes
    # Range must work (llama.cpp uses it for partial reads).
    r2 = client.get(f"/{REPO}/resolve/main/pytorch_model.bin",
                    headers={"Range": "bytes=0-31"})
    assert r2.status_code == 206
    assert len(r2.content) == 32


# =========================================================================
# Group 4: LM Studio — documented exception.
# LM Studio does NOT use HF_ENDPOINT. The supported path is local-file import.
# There is no HTTP contract to test; we assert the documented stance holds:
# the mirror serves the raw file URL that `curl` + import consumes.
# =========================================================================

def test_lm_studio_local_import_path_works(client):
    """LM Studio consumes the mirror by curl-ing a GGUF and importing the file.

    Assert that the raw resolve URL returns the bytes a `curl -O` would save.
    """
    r = client.get(f"/{REPO}/resolve/main/pytorch_model.bin")
    assert r.status_code == 200
    assert r.content == b"\x00\x01\x02\x03" * 16  # the fixture payload
