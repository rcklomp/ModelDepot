"""The one application: Hugging Face-compatible routes, the GUI's JSON API and the GUI itself, in one process on one port.

    uvicorn server.main:app          (the Docker image does exactly this)
"""
from __future__ import annotations

import shutil
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import events
from .adapters.hf_adapter import HfAdapter
from .auth import ReadTokenGate
from .config import Settings, settings
from .hub import api as hub_api
from .hub.jobs import JobManager
from .hub.library import StoreUsage
from .hub.state import HubSettings, WantedLog, load_admin_token
from .index import open_index
from .repository import RepositoryCore
from .storage import FilesystemStorage, InvalidRepoId

UI_DIR = Path(__file__).parent / "ui"


@dataclass
class HubContext:
    settings: Settings
    storage: FilesystemStorage
    repo: RepositoryCore
    jobs: JobManager
    hub_settings: HubSettings
    wanted: WantedLog
    admin_token: str
    usage: StoreUsage


def create_app(settings_override: "Settings | None" = None,
               repo_override: "RepositoryCore | None" = None, *, run_jobs: bool = True) -> FastAPI:
    """Build the app. `settings_override` points tests at a temp store; `repo_override` supplies a pre-populated core;
    `run_jobs=False` leaves the download scheduler off (tests that only look at the API)."""
    active_settings = settings_override or settings
    storage = FilesystemStorage(active_settings)
    storage.ensure_layout()
    event_log = events.EventLog(storage.events_path)
    repo = repo_override if repo_override is not None else RepositoryCore(storage, event_log)

    import os
    hub_settings = HubSettings(active_settings.mirror_root, env_hf=os.environ.get("MIRROR_HF_TOKEN") or os.environ.get("HF_TOKEN", ""),
                               env_civitai=os.environ.get("MIRROR_CIVITAI_TOKEN") or os.environ.get("CIVITAI_API_KEY", ""))
    ctx = HubContext(settings=active_settings, storage=storage, repo=repo, jobs=JobManager(repo, storage, hub_settings),
                     hub_settings=hub_settings, wanted=WantedLog(active_settings.mirror_root),
                     admin_token=load_admin_token(active_settings.mirror_root, active_settings.admin_token), usage=StoreUsage(repo))

    @asynccontextmanager
    async def lifespan(app):
        if run_jobs:
            ctx.jobs.start()
        yield
        ctx.jobs.stop()

    app = FastAPI(title="LLM Repository", version="0.2.0", docs_url="/docs", lifespan=lifespan)
    app.state.hub = ctx
    app.state.index = open_index(active_settings.index_backend, active_settings.index_dir)
    app.state.repository = repo
    app.state.storage = storage
    app.state.events = event_log

    HfAdapter(repo, wanted=ctx.wanted).mount(app)
    app.include_router(hub_api.create_router(ctx))
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

    if UI_DIR.is_dir():
        @app.get("/", include_in_schema=False)
        async def ui_index():
            return FileResponse(UI_DIR / "index.html", headers={"Cache-Control": "no-cache"})

        app.mount("/ui", StaticFiles(directory=str(UI_DIR)), name="ui")

    return app


app = create_app()
