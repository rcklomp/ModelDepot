"""Hugging Face Hub HTTP compatibility adapter.

Mounts the subset of HF Hub HTTP routes that `huggingface_hub` (and engines
built on it: vLLM, TGI, transformers, diffusers, MLX, `datasets`, Ollama's hf.co/... puller)
require to resolve and download a model or a dataset. Clients point at the mirror with:

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

from ..storage import core_repo_id, public_repo_id
from ..repository import (
    FileNotFound,
    RepositoryCore,
    RepositoryNotFound,
    RevisionNotFound,
)
from .base import Capabilities, ProtocolAdapter

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def _hf_datetime(iso: str | None) -> str | None:
    """Our ISO-8601 ('2026-11-04T10:00:00+00:00') -> the Hub's '2026-11-04T10:00:00.000Z': huggingface_hub parses ONLY that."""
    if not iso:
        return None
    from datetime import datetime, timezone
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if d.tzinfo is not None:
        d = d.astimezone(timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


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
    """Hugging Face Hub HTTP compatibility adapter, for models AND datasets.

    The same handlers serve both repo types; `kind` ('model' | 'dataset') only decides the URL shape and the core id:
      models    /api/models/...        /{org}/{repo}/resolve/{rev}/{file}              core id 'org/name'
      datasets  /api/datasets/...      /datasets/{org}/{repo}/resolve/{rev}/{file}     core id 'datasets/org/name'
    Both also take the legacy single-segment names ('gpt2', 'squad'). Spaces are not served.
    """

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
        # Datasets FIRST: '/datasets/acme/x/resolve/main/f' would otherwise also match the model resolve route with org='datasets'
        # (so a MODEL organisation called "datasets" cannot be served; the Hub reserves that name too).
        for kind in ("dataset", "model"):
            self._register(router, kind)
        return router

    def _register(self, router, kind: str) -> None:
        api = "/api/datasets" if kind == "dataset" else "/api/models"
        res = "/datasets" if kind == "dataset" else ""

        def cid(request: Request) -> str:
            pp = request.path_params
            public = f"{pp['org']}/{pp['repo']}" if "org" in pp else pp["repo"]
            return core_repo_id(kind, public)

        # Routes with a literal suffix (revision / tree / refs / commits / paths-info) come BEFORE the bare '/api/x/{org}/{repo}'
        # info route, which would otherwise swallow '/api/datasets/squad/refs' as org='squad', repo='refs'.
        for seg in ("{org}/{repo}", "{repo}"):
            async def revision(request: Request):
                return await self._info(kind, cid(request), request, revision=request.path_params["rev"])

            async def tree(request: Request):
                return await self._tree(cid(request), request.path_params["rev"], request.path_params.get("file_path", ""), request)

            async def refs(request: Request):
                return await self._refs(cid(request))

            async def commits(request: Request):
                return await self._commits(cid(request), request.path_params["rev"])

            async def paths_info(request: Request):
                return await self._paths_info(cid(request), request.path_params["rev"], await request.json())

            router.add_api_route(f"{api}/{seg}/revision/{{rev}}", revision, methods=["GET"])
            router.add_api_route(f"{api}/{seg}/tree/{{rev}}", tree, methods=["GET"])
            router.add_api_route(f"{api}/{seg}/tree/{{rev}}/{{file_path:path}}", tree, methods=["GET"])
            router.add_api_route(f"{api}/{seg}/refs", refs, methods=["GET"])
            router.add_api_route(f"{api}/{seg}/commits/{{rev}}", commits, methods=["GET"])
            router.add_api_route(f"{api}/{seg}/paths-info/{{rev}}", paths_info, methods=["POST"])

        async def info(request: Request):
            return await self._info(kind, cid(request), request)

        router.add_api_route(f"{api}/{{org}}/{{repo}}", info, methods=["GET"])
        router.add_api_route(f"{api}/{{repo}}", info, methods=["GET"])

        # The load-bearing route. huggingface_hub sends HEAD first (metadata), then GET.
        async def resolve(request: Request):
            pp = request.path_params
            return await self._resolve(cid(request), pp["rev"], pp["filename"], request)

        for seg in ("{org}/{repo}", "{repo}"):
            router.add_api_route(f"{res}/{seg}/resolve/{{rev}}/{{filename:path}}", resolve, methods=["GET", "HEAD"])

    # ---- route handlers ------------------------------------------------

    async def _info(self, kind: str, repo_id: str, request: Request, revision: str | None = None) -> JSONResponse:
        revision_param = revision or request.query_params.get("revision", "main")
        public = public_repo_id(repo_id)
        try:
            commit = self._repo.resolve_ref(repo_id, revision_param)
        except RepositoryNotFound:
            raise HTTPException(status_code=404, detail=f"{kind} {public} not found")
        except RevisionNotFound:
            raise HTTPException(status_code=404, detail=f"revision {revision_param!r} not found")

        try:
            manifest = self._repo.get_manifest(repo_id, commit)
        except (RepositoryNotFound, RevisionNotFound) as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        when = _hf_datetime(manifest.revision.fetched_at)
        body = {
            "id": public,
            "author": public.split("/", 1)[0] if "/" in public else "",
            "sha": commit,
            "lastModified": when,
            "private": False,
            "gated": False,
            "disabled": False,
            "tags": [],
            "downloads": 0,
            "likes": 0,
            "siblings": [{"rfilename": f.path, "size": f.size} for f in manifest.files],
            "createdAt": when,
        }
        if kind == "model":
            body.update({"modelId": public, "spaces": [], "paperArxivId": None})
        return JSONResponse(content=body)

    async def _tree(self, repo_id: str, rev: str, file_path: str, request: Request) -> JSONResponse:
        """The Hub's tree listing. Without ?recursive the direct children of `file_path` (files and sub-folders); with
        ?recursive=true EVERY file below it (plus the folders on the way). Paths are always repo-relative. huggingface_hub's
        snapshot_download lists a repo with recursive=true: until 2026-11-04 this route ignored it and returned only the
        top-level folder name, so every file in a sub-folder (GGUF quant folders, diffusers, onnx/) was silently not downloaded."""
        try:
            files = self._repo.list_files(repo_id, rev)
        except RepositoryNotFound:
            raise HTTPException(status_code=404, detail=f"repository {public_repo_id(repo_id)} not found")
        except RevisionNotFound:
            raise HTTPException(status_code=404, detail=f"revision {rev!r} not found")

        recursive = request.query_params.get("recursive", "").lower() in ("1", "true", "yes")
        base = file_path.strip("/")
        base_slash = base + "/" if base else ""
        commit = self._repo.resolve_ref(repo_id, rev)

        def file_entry(f) -> dict:
            return {"type": "file", "path": f.path, "size": f.size, "oid": f.blob.split(":", 1)[1], "lfs": None}

        def dir_entry(path: str) -> dict:
            import hashlib
            return {"type": "directory", "path": path, "size": 0,
                    "oid": hashlib.sha1(f"{commit}:{path}".encode()).hexdigest()}

        exact = [f for f in files if f.path == base]
        if base and exact:                                  # the path names one file
            return JSONResponse(content=[file_entry(exact[0])])
        below = [f for f in files if f.path.startswith(base_slash)] if base else list(files)
        if base and not below:
            raise HTTPException(status_code=404, detail=f"path {base!r} not found in {repo_id}@{rev}")

        out: dict[str, dict] = {}
        for f in below:
            parts = f.path[len(base_slash):].split("/")
            if recursive:
                for i in range(1, len(parts)):
                    d = base_slash + "/".join(parts[:i])
                    out.setdefault("d:" + d, dir_entry(d))
                out["f:" + f.path] = file_entry(f)
            elif len(parts) == 1:
                out["f:" + f.path] = file_entry(f)
            else:
                d = base_slash + parts[0]
                out.setdefault("d:" + d, dir_entry(d))
        return JSONResponse(content=sorted(out.values(), key=lambda e: (e["path"], e["type"])))

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
                "date": _hf_datetime(manifest.revision.fetched_at),
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
            raise HTTPException(status_code=404, detail=f"repository {public_repo_id(repo_id)} not found")
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
        # Only a URL that names a commit is immutable; .../resolve/main/... moves when main moves.
        immutable = bool(_COMMIT_RE.match(rev))
        if request.method == "HEAD":
            return _resolve_head(commit, etag, size, immutable)

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
                                       content_range=(start, end, size), immutable=immutable)
            return StreamingResponse(iter_chunked(), status_code=206, headers=headers)

        # Full file: stream the whole blob with 200.
        def iter_full():
            with self._repo.open_blob(resolved.digest) as f:
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    yield chunk

        headers = _resolve_headers(commit, etag, size, immutable=immutable)
        return StreamingResponse(iter_full(), status_code=200, headers=headers)


def _resolve_headers(commit: str, etag: str, size: int, *,
                     length: int | None = None, content_range=None, immutable: bool = False) -> dict[str, str]:
    h = {
        "X-Repo-Commit": commit,
        "ETag": etag,
        "Content-Length": str(length if length is not None else size),
        "Accept-Ranges": "bytes",
        # X-Linked-* are the preferred HF headers; we set both for safety.
        "X-Linked-Etag": etag,
        "X-Linked-Size": str(size),
        "Cache-Control": "public, max-age=31536000, immutable" if immutable else "no-cache",
    }
    if content_range is not None:
        start, end, total = content_range
        h["Content-Range"] = f"bytes {start}-{end}/{total}"
    return h


def _resolve_head(commit: str, etag: str, size: int, immutable: bool = False) -> Response:
    return Response(status_code=200, headers=_resolve_headers(commit, etag, size, immutable=immutable))
