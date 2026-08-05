"""Ops API: verify, gc, sync — wraps the existing ingester.jobs module.

These run synchronously (they're fast compared to ingest) and return a report.
GC and verify are read-mostly; sync (rsync) is a placeholder that reports what
would transfer.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ingester import jobs as job_runner
from .state import AppState


class OpRequest(BaseModel):
    dry_run: bool = False


def create_router(app_state: AppState) -> APIRouter:
    router = APIRouter()

    @router.post("/api/console/ops/verify")
    async def verify():
        report = job_runner.integrity(app_state.repo)
        return {"ok": report.ok, "checked": report.checked,
                "problems": report.problems}

    @router.post("/api/console/ops/gc")
    async def gc(req: OpRequest):
        report = job_runner.gc(app_state.repo, dry_run=req.dry_run)
        return {"acted": report.acted, "checked": report.checked,
                "problems": report.problems, "dry_run": req.dry_run}

    @router.post("/api/console/ops/sync")
    async def sync():
        # Sync is rsync-to-target, run from the host — here we just report store
        # size so the UI can show "X blobs, Y bytes ready to sync".
        report = job_runner.replication_report(app_state.repo, "nas")
        return {"checked": report.checked, "detail": report.problems}

    return router
