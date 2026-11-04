"""A real server on a real socket plus a way to run the REAL huggingface_hub client against it.

The unit tests drive the app in-process; huggingface_hub uses its own httpx stack, so it is run in a subprocess with
HF_ENDPOINT pointing at the server. This is the test that was missing when `snapshot_download` (a 404 on
/api/models/{repo}/revision/{rev}) went unnoticed behind 150 green tests.
"""
from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from server.config import Settings

COMMIT = "0123456789abcdef0123456789abcdef01234567"
FILES = {
    "config.json": b'{"model_type": "llama"}',
    "README.md": b"# tiny\n",
    "model.safetensors": bytes(range(256)) * 800,            # ~200 KB
    "onnx/model.onnx": b"\x08\x07" * 1000,                   # a nested path
}


def make_settings(root: Path, **kw) -> Settings:
    base = dict(mirror_root=root, host="127.0.0.1", port=0, base_url="http://test", public_url="http://test",
                read_only=False, auth_token="", index_backend="none")
    base.update(kw)
    return Settings(**base)


def stage_repo(repo, repo_id: str = "acme/tiny", files: dict[str, bytes] | None = None, commit: str = COMMIT,
               repo_type: str | None = None) -> str:
    from server.manifests import FileEntry, Manifest, RevisionInfo, Source, SCHEMA_VERSION

    entries = []
    for path, data in (files or FILES).items():
        blob = repo.put_blob_bytes(data)
        entries.append(FileEntry(path=path, size=len(data), blob=f"sha256:{blob.digest}", checksums={"sha256": blob.digest},
                                 metadata={"hf": {"etag": f'"{blob.digest[:32]}-1"', "x_repo_commit": commit}}))
    repo.publish_revision(Manifest(schema_version=SCHEMA_VERSION, repo_id=repo_id,
                                   source=Source(kind="local", ref="test"),
                                   revision=RevisionInfo(commit=commit, fetched_at="2026-11-04T10:00:00+00:00"), files=entries))
    repo.update_reference(repo_id, "main", commit)
    return commit


@dataclass
class Live:
    url: str
    repo: object
    root: Path
    home: Path

    def client(self, code: str, token: str = "", fresh_cache: bool = True) -> subprocess.CompletedProcess:
        """Run client code with HF_ENDPOINT at this server. A fresh cache per call by default: with a warm cache
        huggingface_hub quietly answers from disk when the server errors, which hides exactly the bugs this tests."""
        import uuid
        home = self.home / uuid.uuid4().hex if fresh_cache else self.home
        env = {k: v for k, v in os.environ.items() if not k.startswith("HF_")}
        env.update(HF_ENDPOINT=self.url, HF_HUB_DISABLE_XET="1", HF_HOME=str(home), HF_HUB_DISABLE_TELEMETRY="1")
        if token:
            env["HF_TOKEN"] = token
        return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120)


@pytest.fixture
def make_live(tmp_path):
    servers = []

    def _make(**settings_kw) -> Live:
        from server.main import create_app
        root = tmp_path / f"store{len(servers)}"
        root.mkdir()
        settings = make_settings(root, **settings_kw)
        app = create_app(settings_override=settings)
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        import uvicorn
        srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        threading.Thread(target=srv.run, daemon=True).start()
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close(); break
            except OSError:
                time.sleep(0.05)
        servers.append(srv)
        home = tmp_path / f"hfhome{len(servers)}"
        return Live(url=f"http://127.0.0.1:{port}", repo=app.state.repository, root=root, home=home)

    yield _make
    for srv in servers:
        srv.should_exit = True
