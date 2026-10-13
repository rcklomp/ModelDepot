"""Shared pytest fixtures.

Every test gets a fresh, isolated store under a tmp dir, so tests are
independent and never touch a real MIRROR_ROOT.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

# Force a clean config before server.config is imported anywhere.
os.environ.setdefault("MIRROR_ROOT", "/tmp/llm-repo-unused-default")
os.environ.setdefault("MIRROR_INDEX_BACKEND", "sqlite")
# console/guard.py: TestClient sends Host "testserver"
os.environ.setdefault("CONSOLE_ALLOWED_HOSTS", "testserver,localhost,127.0.0.1,[::1]")


@pytest.fixture
def mirror_root(tmp_path: Path) -> Path:
    """A fresh mirror root directory for one test."""
    root = tmp_path / "mirror-root"
    root.mkdir()
    return root


@pytest.fixture
def settings(mirror_root: Path):
    """A Settings pointed at the temp mirror root."""
    from server.config import Settings

    return Settings(
        mirror_root=mirror_root,
        host="127.0.0.1",
        port=8000,
        base_url="http://test",
        public_url="http://test",
        read_only=False,
        auth_token="",
        index_backend="sqlite",
    )


@pytest.fixture
def storage(settings):
    from server.storage import FilesystemStorage

    s = FilesystemStorage(settings)
    s.ensure_layout()
    return s


@pytest.fixture
def event_log(storage):
    from server.events import EventLog

    return EventLog(storage.events_path)


@pytest.fixture
def repo(storage, event_log):
    """A RepositoryCore wired to the temp store + event log."""
    from server.repository import RepositoryCore

    return RepositoryCore(storage, event_log)


# ---- console fixtures ------------------------------------------------

@pytest.fixture
def console_app(mirror_root: Path):
    """A console app + state pointed at a fresh store."""
    os.environ["MIRROR_ROOT"] = str(mirror_root)
    os.environ["CONSOLE_PORT"] = "0"
    os.environ["CONSOLE_WEB_DIST"] = ""
    import importlib
    import console.config as cfg
    importlib.reload(cfg)
    import console.main as cm
    importlib.reload(cm)
    app = cm.create_app()
    return app, app.state.state


@pytest.fixture
def populated_console(console_app):
    """Console app with a couple of staged local repos."""
    app, state = console_app
    from ingester import transaction
    from ingester.hf_source import StagedFile, StagedSnapshot
    import hashlib

    def stage(repo_id: str, commit: str, files: dict[str, bytes]):
        staging = state.settings.mirror_root / f"stg-{repo_id.replace('/', '_')}"
        staging.mkdir(parents=True, exist_ok=True)
        staged_files = []
        for path, content in files.items():
            p = staging / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content)
            sha = hashlib.sha256(content).hexdigest()
            staged_files.append(StagedFile(
                path=path, local_path=p, size=len(content), etag=sha, sha256=sha))
        snap = StagedSnapshot(repo_id=repo_id, revision_requested="main",
                              commit=commit, staging_dir=staging, files=staged_files)
        transaction.ingest(state.repo, snap)

    stage("org/gguf-model", "a" * 40, {
        "model.Q4_K_M.gguf": b"\x00" * 100,
        "model.Q8_0.gguf": b"\x00" * 200,
    })
    stage("org/safetensors-model", "b" * 40, {
        "config.json": b"{}",
        "model.safetensors": b"\x00" * 500,
    })
    return app, state
