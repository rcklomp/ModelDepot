"""Franken engine model switch (2026-10-13).

Which model the Franken engine serves on the rig's port 8081 (the gateway Open WebUI talks to), and switching or
stopping it. The console never runs an engine itself: it asks colibri's `franken_service.sh` to QUEUE one of four
fixed actions, and a cron job on the rig runs it outside this service's cgroup (llm-console.service has
KillMode=control-group, so a gateway started from here would die with every console restart). Nothing from the
request body reaches a command line except a word checked against ACTIONS.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

SERVICE_SCRIPT = Path(os.environ.get(
    "FRANKEN_SERVICE",
    str(Path.home() / "src/colibri/tools/hot-expert/franken_service.sh"))).expanduser()

# The three models the Franken engine runs (colibri tools/hot-expert/serve_alt.sh arms). Speeds are the served numbers
# recorded in colibri's HANDOFF-2026-10-06b.md (2026-10-13); "hardened" = the engine container of SECURITY-PLAN SEC3.
MODELS = [
    {"action": "franken-glm", "name": "GLM-5.3-Flash", "model_id": "glm-5.3-flash",
     "detail": "IQ3_XXS-expert hybrid, 115 GB", "speed": "~24 tok/s decode", "hardened": True,
     "note": "the default service model"},
    {"action": "franken-ds4", "name": "DeepSeek-V4-Flash", "model_id": "deepseek-v4-flash",
     "detail": "0731 UD-IQ2_M", "speed": "~17.5 tok/s decode", "hardened": False,
     "note": "engine container not hardened yet (SEC3)"},
    {"action": "franken", "name": "Qwen3.8-Flash-Next", "model_id": "qwen3.8-flash-next",
     "detail": "UD-IQ4_XS", "speed": "~46 tok/s decode (21.6 ms/token, short context)", "hardened": False,
     "note": "engine container not hardened yet (SEC3)"},
]
ACTIONS = {m["action"] for m in MODELS} | {"stop"}
_JOB_ID = re.compile(r"^\d{8}T\d{6}Z$")


class ActionRequest(BaseModel):
    action: str


def _run(*args: str, timeout: float = 20) -> subprocess.CompletedProcess:
    if not SERVICE_SCRIPT.is_file():
        raise HTTPException(503, f"franken_service.sh not found at {SERVICE_SCRIPT}: the Franken switch only works on the rig")
    try:
        return subprocess.run([str(SERVICE_SCRIPT), *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "franken_service.sh did not answer in time")


def create_router(state) -> APIRouter:  # noqa: ARG001 (same signature as the other routers)
    router = APIRouter()

    @router.get("/api/console/franken/models")
    async def franken_models():
        return {"models": MODELS}

    @router.get("/api/console/franken/status")
    def franken_status():
        r = _run("status")
        try:
            return json.loads(r.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError):
            raise HTTPException(502, f"franken_service.sh status gave no JSON: {r.stderr.strip()[-300:]}")

    @router.post("/api/console/franken/action")
    def franken_action(req: ActionRequest):
        if req.action not in ACTIONS:
            raise HTTPException(400, f"unknown action; one of {sorted(ACTIONS)}")
        r = _run("request", req.action)
        if r.returncode != 0:
            raise HTTPException(409, (r.stderr or r.stdout).strip() or "refused")
        return {"ok": True, "job_id": r.stdout.strip(), "action": req.action,
                "note": "queued: the rig starts it within a minute"}

    @router.get("/api/console/franken/log/{job_id}")
    def franken_log(job_id: str, lines: int = 200):
        if not _JOB_ID.match(job_id):
            raise HTTPException(400, "bad job id")
        log = Path(os.environ.get("FRANKEN_SERVICE_DIR", str(Path.home() / "bench/franken_service"))) / f"job-{job_id}.log"
        if not log.is_file():
            return {"job_id": job_id, "lines": []}
        text = log.read_text(errors="replace").splitlines()
        return {"job_id": job_id, "lines": text[-max(1, min(lines, 2000)):]}

    return router
