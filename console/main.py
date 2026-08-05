"""Console FastAPI app (port 8001).

A SEPARATE app from the HF mirror (port 8000). Holds the management surface:
upstream search proxy, local catalog, async ingest jobs, ops. Shares the same
store (MIRROR_ROOT). Upstream tokens are held server-side.

Run:  uvicorn console.main:app --port 8001 --reload
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import catalog
from .config import settings
from .search import civitai as civitai_search
from .search import hf as hf_search
from .state import AppState
from .upstream_proxy import UpstreamError


def create_app() -> FastAPI:
    app = FastAPI(title="LLM Repository Console", version="0.1.0",
                  docs_url="/api/docs")

    state = AppState(settings)
    app.state.state = state

    # ---- healthz ------------------------------------------------------
    @app.get("/api/console/healthz")
    async def healthz():
        return {"ok": True, "repos": len(state.repo.list_repos())}

    # ---- catalog (local mirror) --------------------------------------
    @app.get("/api/console/catalog")
    async def catalog_list(q: str = "", type: str = Query("", alias="type"),
                           sort: str = "name"):
        return catalog.list_catalog(state.repo, q=q, type_filter=type, sort=sort)

    @app.get("/api/console/models/{repo_id:path}")
    async def model_detail(repo_id: str):
        detail = catalog.model_detail(state.repo, repo_id)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"{repo_id} not found")
        return detail

    # ---- search (upstream proxy) -------------------------------------
    @app.get("/api/console/search")
    async def search_models(
        source: str = "hf",
        q: str = "",
        filter: str = Query("", alias="filter"),   # gguf|mlx|safetensors (hf) or model type (civitai)
        fmt: str = "",                               # SafeTensor etc. (civitai post-filter)
        sort: str = "downloads",
        nsfw: bool = False,
        limit: int = 30,
        cursor: str | None = None,
    ):
        token = settings.hf_token if source == "hf" else settings.civitai_token
        try:
            if source == "hf":
                page = hf_search.search(
                    q=q, fmt=filter, sort=sort, limit=limit, cursor=cursor, token=token,
                )
            elif source == "civitai":
                page = civitai_search.search(
                    q=q, model_type=filter, fmt=fmt, sort=sort or "Highest Rated",
                    nsfw=nsfw, limit=limit, cursor=cursor, token=token,
                )
            else:
                raise HTTPException(status_code=400, detail=f"unknown source: {source}")
        except UpstreamError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        return page.to_dict()

    # ---- upstream file list (pre-download picker) --------------------
    @app.get("/api/console/upstream/files")
    async def upstream_files(source: str = "hf", repo_id: str = "",
                             version_id: int | None = None,
                             revision: str = "main"):
        token = settings.hf_token if source == "hf" else settings.civitai_token
        try:
            if source == "hf":
                if not repo_id:
                    raise HTTPException(status_code=400, detail="repo_id required for hf")
                options = hf_search.fetch_files(repo_id, revision=revision, token=token)
            elif source == "civitai":
                if not version_id:
                    raise HTTPException(status_code=400, detail="version_id required for civitai")
                options = civitai_search.fetch_files(version_id, token=token)
            else:
                raise HTTPException(status_code=400, detail=f"unknown source: {source}")
        except UpstreamError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        return {"options": [
            {"label": o.label, "path": o.path, "size_bytes": o.size_bytes,
             "shard_count": o.shard_count}
            for o in options
        ]}

    # ---- ingest + jobs + ops (C2) ------------------------------------
    from .ingest_api import create_router as ingest_router
    from .jobs_api import create_router as jobs_router
    from .ops_api import create_router as ops_router
    app.include_router(ingest_router(state))
    app.include_router(jobs_router(state))
    app.include_router(ops_router(state))

    # ---- serve the built SPA (if present) ----------------------------
    if settings.web_dist and settings.web_dist.is_dir():
        app.mount("/ui", StaticFiles(directory=str(settings.web_dist), html=True),
                  name="ui")

        @app.get("/")
        async def index():
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url="/ui/")

    return app


app = create_app()
