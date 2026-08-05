"""Async ingest API: POST /ingest kicks off a background job.

Runs the SAME transaction.ingest the CLI uses, in a worker thread. The job's
progress is captured by tailing the per-repo progress log (the same file the CLI
writes) so the UI gets live updates. Returns immediately with a job id.

This is the heart of the "search → pick → download → watch" UX.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ingester import civitai_source, hf_source, transaction
from server.repository import RepositoryCore

from .state import AppState, JobStatus


class IngestRequest(BaseModel):
    source: str                        # "hf" | "civitai"
    repo_id: str | None = None         # HF repo id
    version_id: int | None = None      # Civitai version id
    revision: str = "main"
    allow_patterns: list[str] | None = None
    ref: str = "main"
    allow_incomplete_shards: bool = False


def create_router(app_state: AppState) -> APIRouter:
    router = APIRouter()

    @router.post("/api/console/ingest")
    async def ingest(req: IngestRequest):
        if req.source == "hf":
            if not req.repo_id:
                raise HTTPException(400, "repo_id required for hf source")
            label = req.repo_id
        elif req.source == "civitai":
            if not req.version_id:
                raise HTTPException(400, "version_id required for civitai source")
            label = f"civitai/{req.version_id}"
        else:
            raise HTTPException(400, f"unknown source: {req.source}")

        job = app_state.jobs.create(f"ingest-{req.source}", label)
        job.progress = {"source": req.source, "phase": "queued"}

        thread = threading.Thread(
            target=_run_ingest,
            args=(app_state, job.id, req),
            daemon=True,
        )
        thread.start()
        return {"job_id": job.id, "label": label}

    return router


def _run_ingest(app_state: AppState, job_id: str, req: IngestRequest) -> None:
    """Worker: runs the ingest, updating the job's status + progress."""
    job = app_state.jobs.get(job_id)
    if job is None:
        return
    app_state.jobs.update(job_id, status=JobStatus.RUNNING,
                          progress={**job.progress, "phase": "downloading"})
    try:
        storage = app_state.storage
        staging = _staging_dir(storage, req)
        staging.mkdir(parents=True, exist_ok=True)

        if req.source == "hf":
            token = app_state.settings.hf_token or None
            snapshot = hf_source.fetch(
                req.repo_id, staging, revision=req.revision, token=token,
                allow_patterns=req.allow_patterns,
                progress_log=storage.settings.log_dir / f"ingest-{_slug(req.repo_id)}.log"
                if hasattr(storage, "settings") else None,
            )
            result = transaction.ingest(
                app_state.repo, snapshot, ref_name=req.ref,
                allow_incomplete_shards=req.allow_incomplete_shards,
            )
        else:  # civitai
            token = app_state.settings.civitai_token
            snap = civitai_source.fetch(
                req.version_id, staging, token=token,
            )
            staged = civitai_source.to_staged_snapshot(snap)
            result = transaction.ingest(
                app_state.repo, staged, source_kind="civitai",
                source_ref=civitai_source.civitai_source_url(snap.model_id, snap.version_id),
                ref_name=req.ref,
                allow_incomplete_shards=req.allow_incomplete_shards,
            )

        # Clean up staging on success.
        import shutil
        shutil.rmtree(staging, ignore_errors=True)

        app_state.jobs.update(
            job_id, status=JobStatus.DONE,
            progress={**job.progress, "phase": "done"},
            result={"commit": result.commit, "file_count": result.file_count,
                    "published": result.published, "ref_updated": result.ref_updated},
            finished_at=__import__("time").time(),
        )
    except Exception as exc:
        app_state.jobs.update(
            job_id, status=JobStatus.FAILED,
            progress={**job.progress, "phase": "failed"},
            error=str(exc), finished_at=__import__("time").time(),
        )


def _staging_dir(storage, req: IngestRequest) -> Path:
    """Deterministic staging path (same scheme as the CLI) for resume."""
    if req.source == "hf":
        return storage.staging_dir / f"hf-{_slug(req.repo_id)}"
    return storage.staging_dir / f"civitai-{req.version_id}"


def _slug(s: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-")[:60] or "repo"
