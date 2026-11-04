"""The GUI's JSON API (/api/hub/...). Separate from the Hub-compatible routes the AI tools use.

Reads need the read token when MIRROR_AUTH_TOKEN is set (the admin token always works too); every change needs the admin token
(`Authorization: Bearer`). Bearer tokens are not sent by a browser on its own, so a page on another site cannot make a request
with one: there is no CSRF to guard against.
"""
from __future__ import annotations

import hmac
import shutil
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import __version__
from ..locks import StoreBusy
from ..repository import RepositoryNotFound
from ..storage import core_repo_id
from . import library, upstream


class JobSpec(BaseModel):
    source: str = "hf"
    id: str
    type: str = "model"
    revision: str = "main"
    files: list[str] | None = None
    file_type: str | None = None
    file_format: str | None = None
    allow_incomplete_shards: bool = False
    total_bytes: int = 0
    file_count: int = 0


class RepoRef(BaseModel):
    type: str = "model"
    id: str


class GcReq(BaseModel):
    dry_run: bool = True


class VerifyReq(BaseModel):
    type: str | None = None
    id: str | None = None


class SettingsReq(BaseModel):
    hf_token: str | None = None
    civitai_token: str | None = None
    max_concurrent: int | None = None
    download_window: str | None = None


def _bearer(request: Request) -> bytes:
    h = request.headers.get("authorization", "")
    return h[7:].strip().encode() if h[:7].lower() == "bearer " else b""


def create_router(ctx) -> APIRouter:
    """ctx: server.main.HubContext (repo, storage, settings, jobs, hub_settings, wanted, admin_token, usage)."""
    router = APIRouter(prefix="/api/hub")
    admin = ctx.admin_token.encode()
    read = (ctx.settings.auth_token or "").encode()

    def is_admin(request: Request) -> bool:
        return bool(admin) and hmac.compare_digest(_bearer(request), admin)

    def require_read(request: Request) -> None:
        if read and not (hmac.compare_digest(_bearer(request), read) or is_admin(request)):
            raise HTTPException(401, "a token is required to look at this library")

    def require_admin(request: Request) -> None:
        if ctx.settings.read_only:
            raise HTTPException(403, "this server is read-only (MIRROR_READ_ONLY): nothing can be changed")
        if not is_admin(request):
            raise HTTPException(401, "the admin token is required for this action")

    R, A = [Depends(require_read)], [Depends(require_admin)]

    @router.get("/auth/check")
    def auth_check(request: Request):
        tok = _bearer(request)
        return {"admin": is_admin(request), "read": (not read) or bool(read and hmac.compare_digest(tok, read)) or is_admin(request),
                "read_token_required": bool(read), "admin_token_required": True}

    @router.get("/status", dependencies=R)
    def status():
        du = shutil.disk_usage(ctx.settings.mirror_root)
        sid = ctx.settings.store_id_path
        repos = [r for r in ctx.repo.list_repos() if ctx.repo.list_revisions(r)]      # a created-but-empty repo is not in the library yet
        return {"store_id": sid.read_text().strip() if sid.is_file() else None, "store_path": str(ctx.settings.mirror_root),
                "free_bytes": du.free, "total_bytes": du.total, "stored": ctx.usage.get(),
                "models": sum(1 for r in repos if not r.startswith("datasets/")), "datasets": sum(1 for r in repos if r.startswith("datasets/")),
                "queue": ctx.jobs.active_summary(), "hf_token_set": bool(ctx.hub_settings.get("hf_token")),
                "read_token_required": bool(read), "namespace": ctx.settings.namespace, "read_only": ctx.settings.read_only,
                "version": __version__}

    # ---- library -------------------------------------------------------
    @router.get("/library", dependencies=R)
    def lib(type: str = "all", q: str = "", sort: str = "name"):
        return {"items": library.list_library(ctx.repo, type_=type, q=q, sort=sort)}

    @router.get("/repo", dependencies=R)
    def repo_detail(type: str = "model", id: str = ""):
        d = library.detail(ctx.repo, core_repo_id(type, id))
        if not d:
            raise HTTPException(404, "not in the library")
        return d

    @router.get("/repo/delete-preview", dependencies=R)
    def delete_preview(type: str = "model", id: str = ""):
        cid = core_repo_id(type, id)
        if cid not in ctx.repo.list_repos():
            raise HTTPException(404, "not in the library")
        u = ctx.repo.repo_blob_usage(cid)
        return {"frees_bytes": u["exclusive_bytes"], "shared_bytes": u["shared_bytes"]}

    @router.post("/repo/delete", dependencies=A)
    def delete_repo(req: RepoRef):
        try:
            out = ctx.repo.delete_repository(core_repo_id(req.type, req.id))
        except RepositoryNotFound:
            raise HTTPException(404, "not in the library")
        except StoreBusy as exc:
            raise HTTPException(409, str(exc))
        ctx.usage.invalidate()
        return out

    # ---- upstream search and the file picker -----------------------------
    @router.get("/search", dependencies=R)
    def search(source: str = "hf", type: str = "model", q: str = "", sort: str = "downloads", filter: str = "", nsfw: bool = False, limit: int = 30):
        try:
            if source == "hf":
                res = upstream.hf_search(kind=type, q=q, sort=sort, fmt=filter, limit=min(limit, 100), token=ctx.hub_settings.get("hf_token"))
            elif source == "civitai":
                res = upstream.civitai_search(q=q, model_type=filter, sort=sort if sort not in ("downloads", "likes") else "Most Downloaded",
                                              nsfw=nsfw, limit=min(limit, 100), token=ctx.hub_settings.get("civitai_token"))
            else:
                raise HTTPException(400, "source must be hf or civitai")
        except upstream.UpstreamError as exc:
            raise HTTPException(exc.status if exc.status in (403, 404) else 502, str(exc))
        have = set(ctx.repo.list_repos())
        for r in res:
            r["in_library"] = r["source"] == "hf" and core_repo_id(r["type"], r["id"]) in have
        return {"results": res}

    @router.get("/upstream/files", dependencies=R)
    def upstream_files(source: str = "hf", type: str = "model", id: str = "", revision: str = "main"):
        try:
            if source == "hf":
                d = upstream.hf_describe(kind=type, repo_id=id, revision=revision, token=ctx.hub_settings.get("hf_token"))
                d["in_library"] = core_repo_id(type, id) in set(ctx.repo.list_repos())
                return d
            if source == "civitai":
                return upstream.civitai_describe(version_id=int(id), token=ctx.hub_settings.get("civitai_token"))
        except upstream.UpstreamError as exc:
            raise HTTPException(exc.status if exc.status in (403, 404) else 502, str(exc))
        except ValueError:
            raise HTTPException(400, "bad id")
        raise HTTPException(400, "source must be hf or civitai")

    # ---- jobs --------------------------------------------------------------
    @router.get("/jobs", dependencies=R)
    def jobs():
        return {"jobs": ctx.jobs.list(), "summary": ctx.jobs.active_summary(), "window": ctx.hub_settings.get("download_window") or "",
                "max_concurrent": int(ctx.hub_settings.get("max_concurrent") or 1)}

    @router.post("/jobs", dependencies=A)
    def submit(spec: JobSpec):
        try:
            job, already = ctx.jobs.submit_download(spec.model_dump())
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"job": job.public(), "already_queued": already}

    @router.post("/jobs/{jid}/{action}", dependencies=A)
    def job_action(jid: str, action: str):
        ok = {"pause": ctx.jobs.pause, "resume": ctx.jobs.resume, "remove": ctx.jobs.remove}.get(action)
        if not ok:
            raise HTTPException(400, "action must be pause, resume or remove")
        if not ok(jid):
            raise HTTPException(409, "that job cannot do that now")
        return {"ok": True}

    # ---- maintenance ---------------------------------------------------------
    @router.post("/ops/verify", dependencies=A)
    def verify(req: VerifyReq):
        rid = core_repo_id(req.type or "model", req.id) if req.id else None
        return {"job": ctx.jobs.submit_verify(rid).public()}

    @router.post("/ops/gc", dependencies=A)
    def gc(req: GcReq):
        from ingester import jobs as ij
        try:
            rep = ij.gc(ctx.repo, dry_run=req.dry_run)
        except StoreBusy as exc:
            raise HTTPException(409, str(exc))
        ctx.usage.invalidate()
        return {"dry_run": req.dry_run, "blobs": rep.acted, "checked": rep.checked, "bytes": rep.bytes}

    # ---- wanted / settings ----------------------------------------------------
    @router.get("/wanted", dependencies=R)
    def wanted():
        have = set(ctx.repo.list_repos())
        return {"items": [w for w in ctx.wanted.list() if core_repo_id(w["type"], w["id"]) not in have]}

    @router.post("/wanted/dismiss", dependencies=A)
    def dismiss(req: RepoRef):
        ctx.wanted.dismiss(req.type, req.id)
        return {"ok": True}

    @router.get("/settings", dependencies=R)
    def get_settings():
        return {**ctx.hub_settings.public(), "admin_token_file": str(ctx.settings.mirror_root / "config" / "admin-token")}

    @router.put("/settings", dependencies=A)
    def put_settings(req: SettingsReq):
        try:
            ctx.hub_settings.update({k: v for k, v in req.model_dump().items() if v is not None})
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return ctx.hub_settings.public()

    return router
