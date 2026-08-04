"""Shared pytest fixtures.

Every test gets a fresh, isolated mirror store under a tmp dir, so tests are
independent and never touch a real MIRROR_ROOT.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

# Force a clean config before server.config is imported anywhere.
os.environ.setdefault("MIRROR_ROOT", "/tmp/llm-repo-unused-default")
os.environ.setdefault("MIRROR_INDEX_BACKEND", "sqlite")


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
