"""Hugging Face Hub HTTP compatibility adapter.

Mounts the subset of HF Hub HTTP routes that `huggingface_hub` (and engines
built on it: vLLM, TGI, transformers, diffusers, MLX, Ollama's hf.co/... puller)
require to resolve and download a model. Clients point at the mirror with:

    HF_ENDPOINT=https://mirror.lan
    HF_HUB_DISABLE_XET=1   # avoid the Xet chunk protocol

Implemented routes (all GET; serving is read-only):
    GET  /api/models/{repo_id}                       -> model metadata
    GET  /api/models/{repo_id}/tree/{rev}/{path?}    -> file listing
    GET  /{repo_id}/resolve/{rev}/{filename}         -> blob (200/206 + HF headers)
    GET  /api/models/{repo_id}/refs                  -> branch/tag refs
    GET  /api/models/{repo_id}/commits/{rev}         -> commit history (single entry)
    POST /api/models/{repo_id}/paths-info/{rev}      -> batch file metadata

The three response headers that `huggingface_hub` requires on the resolve route
(verified against huggingface_hub source — see docs/API_COMPATIBILITY.md):
    X-Repo-Commit   (mandatory; HF raises FileMetadataError without it)
    ETag            (used as the cache key; X-Linked-Etag also accepted upstream)
    Content-Length  (X-Linked-Size also accepted upstream)

Range requests are honored with 206 + Content-Range for resumability.
"""
from __future__ import annotations

import json
import re
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from ..config import settings
from ..repository import (
    FileNotFound,
    RepositoryCore,
    RepositoryNotFound,
    RevisionNotFound,
)
from .base import Capabilities, ProtocolAdapter

# repo_id is 'org/name' or just 'name' (urlencoded, no slashes inside segments).
_REPO_ID_RE = re.compile(r"^[\w.\-]+(?:/[\w.\-]+)?$")


def _is_valid_repo_id(repo_id: str) -> bool:
    return bool(_REPO_ID_RE.match(repo_id))


def _parse_range(range_header: str, size: int) -> tuple[int, int]:
    """Parse a 'bytes=start-end' / 'bytes=start-' Range header.

    Returns (start, end_inclusive). end is clamped to size-1.
    """
    m = re.match(r"bytes=(\d*)-(\d*)", range_header.strip())
    if not m:
        raise HTTPException(status_code=416, detail="invalid range")
    start_s, end_s = m.group(1), m.group(2)
    if start_s == "" and end_s == "":
        raise HTTPException(status_code=416, detail="invalid range")
    if start_s == "":
        # suffix range: last N bytes
        n = int(end_s)
        if n == 0:
            raise HTTPException(status_code=416, detail="invalid range")
        start = max(0, size - n)
        end = size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s != "" else size - 1
    if start > end or start >= size:
        raise HTTPException(status_code=416, detail="range not satisfiable")
    end = min(end, size - 1)
    return start, end


def _hf_compat_etag(file_meta: Any) -> str:
    """Pull the upstream HF etag from namespaced metadata, else synthesize a
    stable one from the sha256. huggingface_hub strips a leading 'W/' via
    _normalize_etag; we return a quoted hex string."""
    hf_meta = (file_meta.metadata or {}).get("hf", {}) if file_meta else {}
    etag = hf_meta.get("etag")
    if etag:
        return etag
    # Synthesize: content-derived so identical content => identical etag.
    digest = file_meta.digest if file_meta else "0" * 64
    return f'"{digest[:32]}-1000"'


class HfAdapter(ProtocolAdapter):
    """Hugging Face Hub HTTP compatibility adapter."""

    def __init__(self, repo: RepositoryCore):
        self._repo = repo

    @property
    def name(self) -> str:
        return "huggingface"

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(resolve=True, tree=True, refs=True, commits=True)

    def mount(self, app: FastAPI) -> None:
        app.include_router(self._router())

    def _router(self):
        from fastapi import APIRouter

        router = APIRouter()

        # IMPORTANT: specific sub-resource routes are registered BEFORE the
        # generic model_info catch-all, because FastAPI matches in registration
        # order. The generic route uses explicit two-segment {org}/{repo} (not
        # {repo_id:path}) so it cannot shadow /tree, /refs, /commits, /paths-info.

        # ---- model metadata (specific shapes) --------------------------
        # /api/models/{org}/{repo}  and  /api/models/{repo} (single segment)
        @router.get("/api/models/{org}/{repo}")
        async def model_info_two(org: str, repo: str, request: Request):
            return await self._model_info(f"{org}/{repo}", request)

        @router.get("/api/models/{repo}")
        async def model_info_one(repo: str, request: Request):
            return await self._model_info(repo, request)

        # ---- sub-resources (registered before any catch-all) -----------
        @router.get("/api/models/{org}/{repo}/tree/{rev}")
        @router.get("/api/models/{org}/{repo}/tree/{rev}/{file_path:path}")
        async def tree(org: str, repo: str, rev: str, file_path: str = ""):
            return await self._tree(f"{org}/{repo}", rev, file_path)

        @router.get("/api/models/{org}/{repo}/refs")
        async def refs(org: str, repo: str):
            return await self._refs(f"{org}/{repo}")

        @router.get("/api/models/{org}/{repo}/commits/{rev}")
        async def commits(org: str, repo: str, rev: str):
            return await self._commits(f"{org}/{repo}", rev)

        @router.post("/api/models/{org}/{repo}/paths-info/{rev}")
        async def paths_info(org: str, repo: str, rev: str, request: Request):
            body = await request.json()
            return await self._paths_info(f"{org}/{repo}", rev, body)

        # ---- blob resolve (the load-bearing route) ---------------------
        # GET and HEAD both return the same headers; HEAD has no body.
        # huggingface_hub issues HEAD first to read metadata, then GET.
        async def _resolve_handler(org: str, repo: str, rev: str, filename: str,
                                   request: Request):
            return await self._resolve(f"{org}/{repo}", rev, filename, request)

        @router.get("/{org}/{repo}/resolve/{rev}/{filename:path}")
        async def resolve_get(org: str, repo: str, rev: str, filename: str,
                              request: Request):
            return await _resolve_handler(org, repo, rev, filename, request)

        @router.head("/{org}/{repo}/resolve/{rev}/{filename:path}")
        async def resolve_head(org: str, repo: str, rev: str, filename: str,
                               request: Request):
            return await _resolve_handler(org, repo, rev, filename, request)

        return router

    # ---- route handlers ------------------------------------------------

    async def _model_info(self, repo_id: str, request: Request) -> JSONResponse:
        revision_param = request.query_params.get("revision", "main")
        try:
            commit = self._repo.resolve_ref(repo_id, revision_param)
        except RepositoryNotFound:
            raise HTTPException(status_code=404, detail=f"model {repo_id} not found")
        except RevisionNotFound:
            raise HTTPException(status_code=404, detail=f"revision {revision_param!r} not found")

        try:
            files = self._repo.list_files(repo_id, commit)
        except (RepositoryNotFound, RevisionNotFound) as exc:
            raise HTTPException(status_code=404, detail=str(exc))

        siblings = [{"rfilename": f.path} for f in files]
        body = {
            "id": repo_id,
            "modelId": repo_id,
            "author": repo_id.split("/", 1)[0] if "/" in repo_id else "",
            "sha": commit,
            "lastModified": None,
            "private": False,
            "gated": False,
            "disabled": False,
            "tags": [],
            "downloads": 0,
            "likes": 0,
            "siblings": siblings,
            "spaces": [],
            "createdAt": None,
            "paperArxivId": None,
        }
        return JSONResponse(content=body)

    async def _tree(self, repo_id: str, rev: str, file_path: str) -> JSONResponse:
        try:
            files = self._repo.list_files(repo_id, rev)
        except RepositoryNotFound:
            raise HTTPException(status_code=404, detail=f"model {repo_id} not found")
        except RevisionNotFound:
            raise HTTPException(status_code=404, detail=f"revision {rev!r} not found")

        prefix = file_path.rstrip("/") + "/" if file_path else ""
        entries = []
        for f in files:
            if prefix and not f.path.startswith(prefix):
                continue
            rel = f.path[len(prefix):] if prefix else f.path
            # Only surface direct children (not nested) for a non-recursive listing,
            # but HF clients with recursive=true want everything. We return all
            # matching files as 'file' entries; clients tolerate flat lists.
            if "/" in rel and prefix == "" and not file_path:
                # Top-level listing: show directory entries for nested paths.
                top = rel.split("/", 1)[0]
                if not any(e["path"] == top for e in entries):
                    entries.append({"type": "directory", "path": top, "oid": None, "size": 0})
                continue
            entries.append({
                "type": "file",
                "path": f.path,
                "size": f.size,
                "oid": f.blob.split(":", 1)[1],
                "lfs": None,
            })
        entries.sort(key=lambda e: (e["type"] != "directory", e["path"]))
        return JSONResponse(content=entries)

    async def _refs(self, repo_id: str) -> JSONResponse:
        try:
            refs_map = self._repo.list_references(repo_id)
        except Exception:
            refs_map = {}
        branches = [{"name": n, "ref": n, "targetCommit": c} for n, c in refs_map.items()]
        return JSONResponse(content={"branches": branches, "tags": [], "converts": []})

    async def _commits(self, repo_id: str, rev: str) -> JSONResponse:
        try:
            commit = self._repo.resolve_ref(repo_id, rev)
            manifest = self._repo.get_manifest(repo_id, rev)
        except (RepositoryNotFound, RevisionNotFound) as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return JSONResponse(content=[
            {
                "oid": commit,
                "title": manifest.revision.commit,
                "message": None,
                "authors": [],
                "date": manifest.revision.fetched_at,
            }
        ])

    async def _paths_info(self, repo_id: str, rev: str, body: Any) -> JSONResponse:
        paths = (body or {}).get("paths", [])
        try:
            files = self._repo.list_files(repo_id, rev)
        except (RepositoryNotFound, RevisionNotFound) as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        by_path = {f.path: f for f in files}
        out = []
        for p in paths:
            f = by_path.get(p)
            if f is None:
                continue
            out.append({
                "type": "file",
                "path": f.path,
                "size": f.size,
                "oid": f.blob.split(":", 1)[1],
                "lfs": None,
            })
        return JSONResponse(content=out)

    async def _resolve(self, repo_id: str, rev: str, filename: str,
                       request: Request) -> Response:
        try:
            resolved = self._repo.get_file(repo_id, rev, filename)
        except RepositoryNotFound:
            raise HTTPException(status_code=404, detail=f"model {repo_id} not found")
        except RevisionNotFound:
            raise HTTPException(status_code=404, detail=f"revision {rev!r} not found")
        except FileNotFound:
            raise HTTPException(status_code=404, detail=f"file {filename!r} not found")

        size = resolved.size
        etag = _hf_compat_etag(resolved)
        commit = (resolved.metadata or {}).get("hf", {}).get("x_repo_commit")
        if not commit:
            # Fall back to the resolved revision's commit (manifest commit).
            commit = self._repo.resolve_ref(repo_id, rev)

        # HEAD must return the same headers without a body.
        if request.method == "HEAD":
            return _resolve_head(commit, etag, size)

        range_header = request.headers.get("range")
        if range_header:
            start, end = _parse_range(range_header, size)
            length = end - start + 1

            def iter_chunked():
                with self._repo.open_blob(resolved.digest) as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(1 << 20, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        yield chunk

            headers = _resolve_headers(commit, etag, size, length=length,
                                       content_range=(start, end, size))
            return StreamingResponse(iter_chunked(), status_code=206, headers=headers)

        # Full file: stream the whole blob with 200.
        def iter_full():
            with self._repo.open_blob(resolved.digest) as f:
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    yield chunk

        headers = _resolve_headers(commit, etag, size)
        return StreamingResponse(iter_full(), status_code=200, headers=headers)


def _resolve_headers(commit: str, etag: str, size: int, *,
                     length: int | None = None, content_range=None) -> dict[str, str]:
    h = {
        "X-Repo-Commit": commit,
        "ETag": etag,
        "Content-Length": str(length if length is not None else size),
        "Accept-Ranges": "bytes",
        # X-Linked-* are the preferred HF headers; we set both for safety.
        "X-Linked-Etag": etag,
        "X-Linked-Size": str(size),
        "Cache-Control": "public, max-age=31536000, immutable",
    }
    if content_range is not None:
        start, end, total = content_range
        h["Content-Range"] = f"bytes {start}-{end}/{total}"
    return h


def _resolve_head(commit: str, etag: str, size: int) -> Response:
    return Response(status_code=200, headers=_resolve_headers(commit, etag, size))
