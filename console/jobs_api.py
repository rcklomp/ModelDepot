"""Jobs API: list jobs and get live progress.

Jobs are in-memory (lost on restart — fine; ingest state survives in the store).
Progress comes from the job's status/progress fields plus a tail of the per-repo
progress log, so the UI can show "downloading... 40 GiB / 97 GiB" updates.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from .state import AppState


def create_router(app_state: AppState) -> APIRouter:
    router = APIRouter()

    @router.get("/api/console/jobs")
    async def list_jobs():
        return [j.to_dict() for j in app_state.jobs.list()]

    @router.get("/api/console/jobs/{job_id}")
    async def get_job(job_id: str):
        job = app_state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        # Refresh the log tail from the progress log if one exists for this job.
        log_path = _log_path_for(app_state, job.label)
        if log_path and log_path.is_file():
            job.log_lines = _tail(log_path, 50)
        return job.to_dict()

    return router


def _log_path_for(app_state: AppState, label: str) -> Path | None:
    """Best-effort: find the progress log for a job's label."""
    if not label:
        return None
    log_dir = app_state.settings.log_dir
    import re
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-")[:60]
    candidate = log_dir / f"ingest-{slug}.log"
    return candidate if candidate else None


def _tail(path: Path, n: int) -> list[str]:
    """Return the last n lines of a file."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [ln.rstrip("\n") for ln in lines[-n:]]
    except OSError:
        return []
