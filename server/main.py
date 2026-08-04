"""FastAPI application: wires the HF adapter to the Repository Core.

The app is intentionally tiny. It owns:
  - a FilesystemStorage (the only storage backend),
  - a Repository Core (pure domain logic),
  - an EventLog,
  - one or more protocol adapters (HF first).

Adapters mount their own routes; main.py only adds cross-cutting middleware and
the /healthz endpoint. Run with:

    uvicorn server.main:app --reload
"""
from __future__ import annotations

from fastapi import FastAPI

from . import events
from .adapters.hf_adapter import HfAdapter
from .config import Settings, settings
from .index import open_index
from .repository import RepositoryCore
from .storage import FilesystemStorage


def create_app(settings_override: "Settings | None" = None,
               repo_override: "RepositoryCore | None" = None) -> FastAPI:
    """Build the FastAPI app.

    `settings_override` is used by tests to point at a temp mirror root without
    touching the module-level singleton. `repo_override` lets a test supply a
    pre-populated Repository Core (e.g. one with a staged fake repo).
    """
    app = FastAPI(title="LLM Repository", version="0.1.0", docs_url="/docs")

    active_settings = settings_override or settings
    storage = FilesystemStorage(active_settings)
    storage.ensure_layout()
    event_log = events.EventLog(storage.events_path)

    if repo_override is not None:
        repo = repo_override
    else:
        repo = RepositoryCore(storage, event_log)

    index = open_index(active_settings.index_backend, active_settings.index_dir)
    app.state.index = index
    app.state.repository = repo
    app.state.storage = storage
    app.state.events = event_log

    HfAdapter(repo).mount(app)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "repos": len(repo.list_repos())}

    return app


app = create_app()
