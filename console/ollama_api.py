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

import re
import subprocess
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

# An Ollama model name as `ollama create` / `ollama rm` take it; it reaches their argv, so it may not start with '-'.
OLLAMA_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")

from .state import AppState


class LoadRequest(BaseModel):
    repo_id: str                 # e.g. "Huihui/Qwen3.6-35B-A3B-Uncensored-GGUF"
    file_path: str               # manifest file path (the GGUF within the repo)
    model_name: str              # Ollama model name, e.g. "qwen3.6-35b"

    @field_validator("model_name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not OLLAMA_NAME.match(v):
            raise ValueError("letters, digits and . _ : / - only, not starting with - (an Ollama model name)")
        return v


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

    @router.post("/api/console/ollama/load")
    async def load_into_ollama(req: LoadRequest):
        """Register a GGUF from the mirror into Ollama — zero copy.

        Resolves the manifest file path to its blob on disk, writes a
        temporary Modelfile pointing at it, and runs `ollama create`.
        The blob stays in the mirror; Ollama just gets a reference.

        For sharded models (filename matches -NNNNN-of-NNNNN), all sibling
        shards are symlinked into a temp dir so Ollama discovers them.
        """
        import re
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

        # Check if this is a sharded model (-NNNNN-of-NNNNN pattern)
        shard_re = re.compile(r"^(.+)-(\d{5})-of-(\d{5})(\.[^.]+)?$")
        basename = Path(target.path).name
        shard_match = shard_re.match(basename)

        if shard_match:
            # Sharded model: symlink ALL sibling shards into a temp dir
            prefix = shard_match.group(1)
            total = int(shard_match.group(3))
            ext = shard_match.group(4) or ""
            siblings = [f for f in files
                        if shard_re.match(Path(f.path).name)
                        and shard_re.match(Path(f.path).name).group(1) == prefix]
            if len(siblings) < total:
                raise HTTPException(
                    400, f"incomplete shard set: {len(siblings)}/{total} found. "
                         f"Load all shards into the mirror first.")

            staging = Path(tempfile.mkdtemp(prefix="ollama_shards_"))
            shard_paths = []
            for s in sorted(siblings, key=lambda f: f.path):
                digest = s.blob.split(":", 1)[1]
                blob = app_state.storage.blobs.path_for(digest)
                if not blob.exists():
                    raise HTTPException(500, f"blob {digest[:16]} missing on disk")
                link = staging / Path(s.path).name
                link.symlink_to(blob.resolve())
                shard_paths.append(str(link))

            # Point Modelfile at the first shard — Ollama finds the rest
            from_path = shard_paths[0]
            cleanup_dir = staging
        else:
            # Single file: point directly at the blob
            digest = target.blob.split(":", 1)[1]
            blob_path = app_state.storage.blobs.path_for(digest)
            if not blob_path.exists():
                raise HTTPException(500, f"blob {digest[:16]} not on disk at {blob_path}")
            from_path = str(blob_path)
            cleanup_dir = None

        # Write a temporary Modelfile
        modelfile_content = (
            f"FROM {from_path}\n"
            f"PARAMETER temperature 0.7\n"
            f"PARAMETER num_ctx 8192\n"
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".Modelfile", delete=False, prefix="ollama_load_"
        ) as mf:
            mf.write(modelfile_content)
            modelfile_path = mf.name

        # Run ollama create (this reads the blob, creates an Ollama model layer)
        def _cleanup():
            Path(modelfile_path).unlink(missing_ok=True)
            if cleanup_dir:
                import shutil
                shutil.rmtree(cleanup_dir, ignore_errors=True)

        try:
            result = subprocess.run(
                ["ollama", "create", req.model_name, "-f", modelfile_path],
                capture_output=True, text=True, timeout=600)
            _cleanup()
            if result.returncode != 0:
                raise HTTPException(
                    500, f"ollama create failed: {result.stderr.strip()}")
        except FileNotFoundError:
            _cleanup()
            raise HTTPException(503, "ollama CLI not found on server")
        except subprocess.TimeoutExpired:
            _cleanup()
            raise HTTPException(504, "ollama create timed out (large model?)")

        return {
            "ok": True,
            "model_name": req.model_name,
            "source_repo": req.repo_id,
            "source_file": req.file_path,
            "shard_count": len(siblings) if shard_match else 1,
            "message": f"'{req.model_name}' is now available in Ollama",
        }

    @router.delete("/api/console/ollama/models/{model_name}")
    async def remove_from_ollama(model_name: str):
        """Remove a model from Ollama (mirror blob is untouched)."""
        if not OLLAMA_NAME.match(model_name):
            raise HTTPException(400, "not an Ollama model name")
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
