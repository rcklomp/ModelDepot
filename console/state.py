"""Shared state for the console app.

Owns the RepositoryCore (same one the mirror uses, reading the same store) and
the in-memory JobRegistry for async ingest jobs. Both are created once at app
startup and attached to app.state.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from server import events
from server.repository import RepositoryCore
from server.storage import FilesystemStorage

from .config import ConsoleSettings


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Job:
    """An async ingest job (or op). In-memory only; the actual ingest state
    survives in staging + manifests, so a lost job just means re-resume."""

    id: str
    kind: str                      # "ingest-hf" | "ingest-civitai" | "verify" | ...
    label: str                     # human-readable, e.g. "unsloth/DeepSeek-..."
    status: JobStatus = JobStatus.QUEUED
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    progress: dict[str, Any] = field(default_factory=dict)  # updated by the worker
    result: dict[str, Any] | None = None
    error: str | None = None
    log_lines: list[str] = field(default_factory=list)      # recent progress-log tail

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "status": self.status.value,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
            "log": self.log_lines[-50:],   # last 50 lines for the UI
        }


class JobRegistry:
    """Thread-safe in-memory registry of background jobs.

    Jobs are lost on restart — that's fine because ingest state lives in the
    store (staging for in-flight, manifests for published). A restarted job can
    re-resume by re-POSTing.
    """

    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, kind: str, label: str) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, label=label)
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.started_at, reverse=True)

    def update(self, job_id: str, **fields) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            for k, v in fields.items():
                setattr(job, k, v)
            return job


class AppState:
    """Holds the shared collaborators, created once at startup."""

    def __init__(self, settings: ConsoleSettings):
        self.settings = settings
        self.storage = FilesystemStorage(_server_settings_view(settings))
        self.storage.ensure_layout()
        self.event_log = events.EventLog(self.storage.events_path)
        self.repo = RepositoryCore(self.storage, self.event_log)
        self.jobs = JobRegistry()


def _server_settings_view(c: ConsoleSettings):
    """Adapt ConsoleSettings -> server.config.Settings shape for FilesystemStorage.

    FilesystemStorage reads paths off a Settings-like object. We give it just the
    bits it needs (mirror_root), reusing the same path resolution.
    """
    from server.config import Settings

    return Settings(
        mirror_root=c.mirror_root,
        host=c.host, port=c.port,
        base_url=f"http://localhost:{c.port}",
        public_url=f"http://localhost:{c.port}",
        read_only=False, auth_token=c.auth_token,
        index_backend="none",   # console doesn't need the index
    )
