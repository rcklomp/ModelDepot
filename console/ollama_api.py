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
    async def list_ollama_models():
        """Proxy to Ollama's /api/tags — what's currently registered."""
        try:
            r = subprocess.run(
                ["ollama", "list"], capture_output=True, text=True, timeout=10)
            # Parse "ollama list" output (NAME, ID, SIZE, MODIFIED)
            lines = r.stdout.strip().split("\n")[1:]  # skip header
            models = []
            for line in lines:
                parts = line.split()
                if parts:
                    models.append({"name": parts[0], "size": " ".join(parts[2:3])})
            return {"models": models}
        except FileNotFoundError:
            raise HTTPException(503, "ollama CLI not found on server")
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "ollama list timed out")

    @router.post("/api/console/ollama/load")
    async def load_into_ollama(req: LoadRequest):
        """Register a GGUF from the mirror into Ollama — zero copy.

        Resolves the manifest file path to its blob on disk, writes a
        temporary Modelfile pointing at it, and runs `ollama create`.
        The blob stays in the mirror; Ollama just gets a reference.
        """
        repo = app_state.repo

        # Find the file in the manifest -> get its blob digest
        try:
            files = repo.list_files(req.repo_id, "main")
        except Exception:
            raise HTTPException(404, f"repo '{req.repo_id}' not found")

        target = None
        for f in files:
            if f.path == req.file_path:
                target = f
                break
        if not target:
            # Try fuzzy match (case-insensitive basename)
            for f in files:
                if Path(f.path).name.lower() == Path(req.file_path).name.lower():
                    target = f
                    break
        if not target:
            available = [f.path for f in files]
            raise HTTPException(
                404, f"file '{req.file_path}' not in {req.repo_id}. "
                     f"Available: {available}")

        # Resolve blob digest -> physical path on disk via BlobStore
        digest = target.blob.split(":", 1)[1]
        blob_path = app_state.storage.blobs.path_for(digest)
        if not blob_path.exists():
            raise HTTPException(500, f"blob {digest[:16]} not on disk at {blob_path}")

        # Write a temporary Modelfile
        modelfile_content = (
            f"FROM {blob_path}\n"
            f"PARAMETER temperature 0.7\n"
            f"PARAMETER num_ctx 8192\n"
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".Modelfile", delete=False, prefix="ollama_load_"
        ) as mf:
            mf.write(modelfile_content)
            modelfile_path = mf.name

        # Run ollama create (this reads the blob, creates an Ollama model layer)
        try:
            result = subprocess.run(
                ["ollama", "create", req.model_name, "-f", modelfile_path],
                capture_output=True, text=True, timeout=600)
            Path(modelfile_path).unlink(missing_ok=True)
            if result.returncode != 0:
                raise HTTPException(
                    500, f"ollama create failed: {result.stderr.strip()}")
        except FileNotFoundError:
            Path(modelfile_path).unlink(missing_ok=True)
            raise HTTPException(503, "ollama CLI not found on server")
        except subprocess.TimeoutExpired:
            Path(modelfile_path).unlink(missing_ok=True)
            raise HTTPException(504, "ollama create timed out (large model?)")

        return {
            "ok": True,
            "model_name": req.model_name,
            "source_repo": req.repo_id,
            "source_file": req.file_path,
            "blob_digest": digest[:16] + "...",
            "message": f"'{req.model_name}' is now available in Ollama",
        }

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
