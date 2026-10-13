"""Ollama integration: register mirror models into Ollama directly.

POST /api/console/ollama/load  {repo_id, file_path, model_name}
  → finds the blob on disk, writes a temporary Modelfile pointing at it,
    runs `ollama create <model_name> -f <modelfile>`, returns immediately.
  → NO data copy: Ollama reads the blob from its original path on the NVMe.

GET /api/console/ollama/models
  → lists models currently registered in Ollama (proxy to Ollama's API).

DELETE /api/console/ollama/models/{model_name}
  → removes a model from Ollama (does NOT touch the mirror blob).
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .state import AppState


class LoadRequest(BaseModel):
    repo_id: str                 # e.g. "Huihui/Qwen3.6-35B-A3B-Uncensored-GGUF"
    file_path: str               # manifest file path (the GGUF within the repo)
    model_name: str              # Ollama model name, e.g. "qwen3.6-35b"


def create_router(app_state: AppState) -> APIRouter:
    router = APIRouter()

    @router.get("/api/console/ollama/models")
    def list_ollama_models():
        """What is registered in Ollama, with sizes in bytes (Ollama's /api/tags; the old `ollama list`
        parse dropped the unit and printed "29" for 29 GB)."""
        from .inventory import ollama_models
        models, error = ollama_models()
        if error:
            raise HTTPException(503, error)
        return {"models": models}

    @router.delete("/api/console/ollama/models/{model_name}")
    async def remove_from_ollama(model_name: str):
        """Remove a model from Ollama (mirror blob is untouched)."""
        try:
            result = subprocess.run(
                ["ollama", "rm", model_name],
                capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                raise HTTPException(404, f"ollama rm: {result.stderr.strip()}")
        except FileNotFoundError:
            raise HTTPException(503, "ollama CLI not found")
        return {"ok": True, "removed": model_name}

    return router
