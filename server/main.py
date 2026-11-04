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

import shutil

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import events
from .adapters.hf_adapter import HfAdapter
from .auth import ReadTokenGate
from .config import Settings, settings
from .index import open_index
from .repository import RepositoryCore
from .storage import FilesystemStorage, InvalidRepoId


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
    if active_settings.auth_token:
        app.add_middleware(ReadTokenGate, token=active_settings.auth_token)

    @app.exception_handler(InvalidRepoId)
    async def invalid_repo_id(request, exc):
        return JSONResponse({"detail": "invalid repository id"}, status_code=404)

    @app.get("/healthz")
    async def healthz():
        usage = shutil.disk_usage(active_settings.mirror_root)
        sid = active_settings.store_id_path
        return {"ok": True, "repos": len(repo.list_repos()), "free_bytes": usage.free, "total_bytes": usage.total,
                "store_id": sid.read_text().strip() if sid.is_file() else None}

    return app


app = create_app()
