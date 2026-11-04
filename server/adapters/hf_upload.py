"""Hugging Face Hub write routes: what `HfApi.create_repo`, `upload_file`, `upload_folder`, `delete_file` and `hf upload` call.

    GET  /api/whoami-v2                                          who the token is (HfApi.whoami, `hf auth whoami`, login checks)
    POST /api/repos/create                                       create an EMPTY model or dataset repository
    POST /api/validate-yaml                                      client-side README.md front-matter check (accepts everything)
    POST /api/{models|datasets}/{org}/{repo}/preupload/{rev}     per file: "lfs" or "regular" (+ oid of what is there, to skip no-ops)
    POST [/datasets]/{org}/{repo}.git/info/lfs/objects/batch     git-LFS batch: where to PUT each large file
    PUT  /_lfs/{oid}                                             the large file's bytes (one PUT per file, streamed to disk)
    POST /_lfs/{oid}/verify                                      hash the staged bytes once; refuse a mismatch
    POST /api/{models|datasets}/{org}/{repo}/commit/{rev}        NDJSON commit: file / lfsFile / deletedFile / deletedFolder

Every route needs the ADMIN token (`Authorization: Bearer`, i.e. HF_TOKEN=<admin token> on the client); MIRROR_AUTH_TOKEN (the read
token) never writes. `MIRROR_READ_ONLY` answers 403 on all of them. The two LFS URLs are called by the client WITHOUT its
Authorization header (the Hub's real ones point at S3), so the batch response signs them: an HMAC of (oid, size, expiry) under the
admin token, valid for an hour. Neither lives under a path the read gate (server/auth.py) guards, so they work with or without
MIRROR_AUTH_TOKEN. Only branch `main`: no pull requests, no branches, no `copyFile`. Xet is not implemented: the client must run
with HF_HUB_DISABLE_XET=1. The commit logic itself is in server/hub/upload.py.
"""
from __future__ import annotations

import base64
import binascii
import functools
import hashlib
import json
import os
import shutil
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ..hub.upload import (
    LFS_MIN_BYTES, READ_ONLY_MESSAGE, TOKEN_TTL, Addition, Op, UploadError, check_signature, digest_of, drop_staged_lfs, git_blob_sha1,
    is_admin, is_lfs, is_oid, lfs_path, publish_commit, remember_digest, sign, sweep_staging, uploads_dir,
    validate_repo_name, validate_repo_path)
from ..storage import InvalidRepoId, core_repo_id, public_repo_id

LFS_MEDIA_TYPE = "application/vnd.git-lfs+json"
MAX_LINE = 64 << 20                    # one NDJSON line (a regular file, base64): the Hub keeps files that need more in LFS
INVALID_CREDENTIALS = "Invalid credentials in Authorization header"          # the Hub's own wording; the client keys off it
_inflight: set[str] = set()            # oids with a PUT running (one worker process: a plain set is enough)


def error_response(status: int, message: str, code: str | None = None) -> JSONResponse:
    """The Hub's error shape: {"error": ...} plus X-Error-Message / X-Error-Code, which huggingface_hub reads."""
    headers = {"X-Error-Message": "".join(c if 32 <= ord(c) < 127 else "?" for c in message)}      # a header value: no control characters
    if code:
        headers["X-Error-Code"] = code
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse({"error": message, "message": message}, status_code=status, headers=headers)


def hub_route(fn):
    """Turn an UploadError raised in a handler into the Hub's error response."""
    @functools.wraps(fn)
    async def inner(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except UploadError as exc:
            return error_response(exc.status, exc.message, exc.code)
    return inner


def base_url(request: Request) -> str:
    """scheme://host the client used (honouring a reverse proxy's X-Forwarded-*): the LFS URLs we hand back must be reachable the same way."""
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    if proto not in ("http", "https"):
        proto = request.url.scheme
    host = request.headers.get("x-forwarded-host", "").split(",")[0].strip() or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


class HfUploadAdapter:
    """Mounts the write routes. `ctx` is server.main.HubContext (settings, storage, repo, admin_token, usage)."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.repo = ctx.repo
        self.storage = ctx.storage

    def mount(self, app: FastAPI) -> None:
        app.include_router(self._router())

    # ---- guards ------------------------------------------------------------------------------------------------------------

    def _not_read_only(self) -> None:
        if self.ctx.settings.read_only:
            raise UploadError(403, READ_ONLY_MESSAGE)

    def _write_guard(self, request: Request) -> None:
        self._not_read_only()
        if not is_admin(self.ctx, request):
            raise UploadError(401, INVALID_CREDENTIALS)

    def _lfs_guard(self, request: Request, purpose: str, oid: str, size: int) -> None:
        """The LFS PUT/verify URLs carry their own signature; the admin token in a header works too (git-lfs style clients send it)."""
        self._not_read_only()
        q = request.query_params
        try:
            exp = int(q.get("exp", "0"))
        except ValueError:
            exp = 0
        if not (check_signature(self.ctx.admin_token, purpose, oid, size, exp, q.get("sig", "")) or is_admin(self.ctx, request)):
            raise UploadError(401, INVALID_CREDENTIALS)

    def _existing_repo(self, kind: str, request: Request) -> str:
        pp = request.path_params
        try:
            cid = core_repo_id(kind, f"{pp['org']}/{pp['repo']}")
            ok = self.repo.repository_exists(cid)
        except InvalidRepoId:
            ok = False
        if not ok:
            raise UploadError(404, f"{kind} repository {pp['org']}/{pp['repo']} not found: create it first", code="RepoNotFound")
        return cid

    @staticmethod
    def _main_only(request: Request) -> None:
        if request.path_params.get("rev", "main") != "main":
            raise UploadError(400, "branches and pull requests are not supported: upload to 'main'")
        if request.query_params.get("create_pr", "").lower() in ("1", "true", "yes"):
            raise UploadError(400, "branches and pull requests are not supported: upload to 'main'")

    def _head(self, cid: str) -> tuple[str | None, dict]:
        head = self.repo.list_references(cid).get("main")
        files = {f.path: f for f in self.repo.get_manifest(cid, head).files} if head else {}
        return head, files

    # ---- routes ------------------------------------------------------------------------------------------------------------

    def _router(self) -> APIRouter:
        router = APIRouter()
        ctx = self.ctx

        @hub_route
        async def whoami(request: Request):
            if not is_admin(ctx, request):
                return error_response(401, INVALID_CREDENTIALS)
            ns = ctx.settings.namespace
            return {"type": "user", "id": "admin", "name": ns, "fullname": ns, "orgs": [],         # `hf auth whoami` reads info["orgs"]
                    "auth": {"type": "access_token", "accessToken": {"role": "write"}}}

        @hub_route
        async def create(request: Request):
            self._write_guard(request)
            body = await _json(request)
            kind = body.get("type") or "model"
            if kind == "space":
                raise UploadError(400, "Spaces are not supported")
            if kind not in ("model", "dataset"):
                raise UploadError(400, f"repository type {kind!r} is not supported (model or dataset)")
            if body.get("private") is True or body.get("visibility") == "private":
                raise UploadError(400, "private repositories are not supported: the whole mirror is private when MIRROR_AUTH_TOKEN is set")
            name, org = body.get("name"), body.get("organization") or ctx.settings.namespace
            if isinstance(name, str) and "/" in name:
                org, name = name.split("/", 1)
            validate_repo_name(org, name)
            cid = core_repo_id(kind, f"{org}/{name}")
            url = f"{base_url(request)}/{'datasets/' if kind == 'dataset' else ''}{org}/{name}"
            if not await run_in_threadpool(self.repo.create_repository, cid):
                # create_repo(exist_ok=True) swallows the 409 and then reads "url" from this very body, as the Hub sends it.
                resp = error_response(409, f"You already created this {kind} repo")
                resp.body = json.dumps({"error": f"You already created this {kind} repo", "url": url}).encode()
                resp.headers["content-length"] = str(len(resp.body))
                return resp
            return {"url": url, "name": f"{org}/{name}", "id": uuid.uuid4().hex[:24]}

        async def validate_yaml(request: Request):
            return {"errors": [], "warnings": []}

        router.add_api_route("/api/whoami-v2", whoami, methods=["GET"])
        router.add_api_route("/api/repos/create", create, methods=["POST"])
        router.add_api_route("/api/validate-yaml", validate_yaml, methods=["POST"])

        # Datasets first, as in HfAdapter: '/datasets/a/b.git/...' must not be read as a model repo called datasets/a.
        for kind in ("dataset", "model"):
            self._register(router, kind)

        @hub_route
        async def lfs_put(request: Request):
            return await self._lfs_put(request)

        @hub_route
        async def lfs_verify(request: Request):
            return await self._lfs_verify(request)

        router.add_api_route("/_lfs/{oid}", lfs_put, methods=["PUT"])
        router.add_api_route("/_lfs/{oid}/verify", lfs_verify, methods=["POST"])
        return router

    def _register(self, router: APIRouter, kind: str) -> None:
        api = "/api/datasets" if kind == "dataset" else "/api/models"
        res = "/datasets" if kind == "dataset" else ""

        @hub_route
        async def preupload(request: Request):
            self._write_guard(request)
            cid = self._existing_repo(kind, request)
            self._main_only(request)
            body = await _json(request)
            head, head_files = await run_in_threadpool(self._head, cid)
            out = []
            for f in body.get("files") or []:
                path = validate_repo_path(f.get("path"))
                size = f.get("size")
                if not isinstance(size, int) or size < 0:
                    raise UploadError(400, f"{path}: size must be a number of bytes")
                lfs = is_lfs(path, size)
                entry = {"path": path, "uploadMode": "lfs" if lfs else "regular", "shouldIgnore": False}
                oid = await run_in_threadpool(self._current_oid, head_files.get(path), size, lfs)
                if oid:
                    entry["oid"] = oid
                out.append(entry)
            return {"files": out, "commitOid": head}

        @hub_route
        async def batch(request: Request):
            self._write_guard(request)
            self._existing_repo(kind, request)
            body = await _json(request)
            if body.get("operation") != "upload":
                raise UploadError(400, "only upload is supported by this mirror's LFS endpoint")
            if "basic" not in (body.get("transfers") or ["basic"]):
                raise UploadError(400, "only the basic LFS transfer is supported")
            await run_in_threadpool(sweep_staging, self.storage)
            base, exp = base_url(request), int(time.time()) + TOKEN_TTL
            objects = []
            for o in body.get("objects") or []:
                oid, size = o.get("oid"), o.get("size")
                if not is_oid(oid) or not isinstance(size, int) or size < 0:
                    objects.append({"oid": str(oid), "size": size if isinstance(size, int) else 0,
                                    "error": {"code": 422, "message": "oid must be a sha256 hex digest and size a number of bytes"}})
                elif self.storage.blobs.exists(oid):
                    try:
                        os.utime(self.storage.blobs.path_for(oid))          # a young blob is safe from GC until its commit lands
                    except OSError:
                        pass
                    objects.append({"oid": oid, "size": size, "authenticated": True})       # no "actions": nothing to upload
                else:
                    q = f"size={size}&exp={exp}&sig="
                    objects.append({"oid": oid, "size": size, "authenticated": True, "actions": {
                        "upload": {"href": f"{base}/_lfs/{oid}?{q}{sign(self.ctx.admin_token, 'put', oid, size, exp)}",
                                   "header": {}, "expires_in": TOKEN_TTL},
                        "verify": {"href": f"{base}/_lfs/{oid}/verify?{q}{sign(self.ctx.admin_token, 'verify', oid, size, exp)}",
                                   "header": {}, "expires_in": TOKEN_TTL}}})
            return JSONResponse({"transfer": "basic", "objects": objects, "hash_algo": "sha256"}, media_type=LFS_MEDIA_TYPE)

        @hub_route
        async def commit(request: Request):
            self._write_guard(request)
            cid = self._existing_repo(kind, request)
            self._main_only(request)
            stage = uploads_dir(self.storage) / f"c-{uuid.uuid4().hex}"
            try:
                header, ops = await self._read_commit(request, stage)
                result = await run_in_threadpool(
                    lambda: publish_commit(self.repo, cid, ops, summary=header["summary"], description=header["description"],
                                           parent=header["parent"]))
            finally:
                await run_in_threadpool(shutil.rmtree, stage, True)
            self.ctx.usage.invalidate()
            prefix = "datasets/" if kind == "dataset" else ""
            return {"success": True, "commitOid": result.commit,
                    "commitUrl": f"{base_url(request)}/{prefix}{public_repo_id(cid)}/commit/{result.commit}", "hookOutput": ""}

        router.add_api_route(f"{api}/{{org}}/{{repo}}/preupload/{{rev:path}}", preupload, methods=["POST"])
        router.add_api_route(f"{api}/{{org}}/{{repo}}/commit/{{rev:path}}", commit, methods=["POST"])
        router.add_api_route(f"{res}/{{org}}/{{repo}}.git/info/lfs/objects/batch", batch, methods=["POST"])

    # ---- preupload: what is at this path now (lets the client skip files that did not change) ---------------------------------

    def _current_oid(self, existing, size: int, lfs: bool) -> str | None:
        """The Hub's `oid` is the id of the file already at that path. The client compares it with its own: sha256 for LFS files,
        the git blob sha1 for the others. Equal => it leaves the file out of the commit."""
        if existing is None or existing.size != size:
            return None
        digest = existing.blob.split(":", 1)[1]
        if lfs:
            return digest
        if size > LFS_MIN_BYTES:
            return None
        try:
            with self.repo.open_blob(digest) as fh:
                return git_blob_sha1(fh.read())
        except Exception:                                                 # noqa: BLE001 - blob missing or unreadable: just no hint
            return None

    # ---- the commit body ---------------------------------------------------------------------------------------------------

    async def _read_commit(self, request: Request, stage: Path) -> tuple[dict, list[Op]]:
        """Stream-parse the NDJSON body. A regular file's bytes go to disk (hashed once) as soon as its line is read, so memory
        holds one line, never the whole commit."""
        header: dict | None = None
        ops: list[Op] = []
        n = 0
        async for line in _ndjson_lines(request):
            try:
                item = json.loads(line)
                key, value = item["key"], item.get("value") or {}
                if not isinstance(value, dict):
                    raise TypeError
            except (ValueError, KeyError, TypeError):
                raise UploadError(400, "the commit body must be NDJSON lines of {\"key\": ..., \"value\": {...}}")
            if key == "header":
                summary = value.get("summary")
                header = {"summary": summary.strip() if isinstance(summary, str) and summary.strip() else "Upload with huggingface_hub",
                          "description": value.get("description") or "", "parent": value.get("parentCommit") or None}
                continue
            if header is None:
                raise UploadError(400, "the first line of a commit must be the header")
            if key == "file":
                path = validate_repo_path(value.get("path"))
                try:
                    content = value.get("content", "")
                    data = base64.b64decode(content, validate=True) if value.get("encoding", "base64") == "base64" else str(content).encode()
                except (binascii.Error, ValueError):
                    raise UploadError(400, f"{path}: content is not valid base64")
                n += 1
                ops.append(Op("add", path, await run_in_threadpool(_stage_bytes, stage, n, path, data)))
            elif key == "lfsFile":
                path = validate_repo_path(value.get("path"))
                oid, size = value.get("oid"), value.get("size")
                if value.get("algo", "sha256") != "sha256" or not is_oid(oid):
                    raise UploadError(400, f"{path}: lfsFile needs a sha256 oid")
                if size is not None and (not isinstance(size, int) or size < 0):
                    raise UploadError(400, f"{path}: size must be a number of bytes")
                ops.append(Op("add", path, Addition(path=path, sha256=oid, size=size, lfs=True, pending_lfs=True)))
            elif key == "deletedFile":
                ops.append(Op("delete_file", validate_repo_path(value.get("path"))))
            elif key == "deletedFolder":                      # the client keeps the trailing '/' the caller typed ("old/")
                raw = value.get("path")
                ops.append(Op("delete_folder", validate_repo_path(raw.rstrip("/") if isinstance(raw, str) else raw)))
            elif key == "copyFile":
                raise UploadError(400, "copying files inside a repository is not supported by this mirror: upload the file again")
            else:
                raise UploadError(400, f"unknown commit operation {key!r}")
        if header is None:
            raise UploadError(400, "the commit has no header line")
        return header, ops

    # ---- the LFS PUT and verify ---------------------------------------------------------------------------------------------

    async def _lfs_put(self, request: Request) -> Response:
        oid = request.path_params["oid"]
        if not is_oid(oid):
            raise UploadError(400, "the object id must be a sha256 hex digest")
        size = _query_int(request, "size")
        self._lfs_guard(request, "put", oid, size)
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) != size:
            raise UploadError(400, f"size mismatch: the body is {declared} bytes, the object was announced as {size}")
        if oid in _inflight:
            raise UploadError(409, "an upload of this object is already running")
        _inflight.add(oid)
        final = lfs_path(self.storage, oid)
        part = final.with_name(final.name + ".part")
        total = 0
        digest = hashlib.sha256()
        try:
            with open(part, "wb") as f:
                buf = bytearray()
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > size:
                        raise UploadError(400, f"size mismatch: the body is longer than the announced {size} bytes")
                    buf += chunk
                    if len(buf) >= 1 << 20:                      # 1 MiB writes: a thread hop per 64 KiB chunk would cost more than the disk
                        await run_in_threadpool(_write_and_hash, f, digest, bytes(buf))
                        buf.clear()
                if buf:
                    await run_in_threadpool(_write_and_hash, f, digest, bytes(buf))
            if total != size:
                raise UploadError(400, f"size mismatch: received {total} bytes, the object was announced as {size}")
            # The file is hashed as it arrives (a 50 GB model is not read a second time on a NAS disk). The verdict is still given at
            # verify (and at commit): a PUT of the wrong bytes is accepted here and refused there, as with the Hub's S3 + verify pair.
            await run_in_threadpool(_place_staged, part, final, digest.hexdigest())
        finally:
            _inflight.discard(oid)
            part.unlink(missing_ok=True)                          # an interrupted or refused upload leaves nothing behind
        return Response(status_code=200)

    async def _lfs_verify(self, request: Request) -> Response:
        oid = request.path_params["oid"]
        if not is_oid(oid):
            raise UploadError(400, "the object id must be a sha256 hex digest")
        size = _query_int(request, "size")
        self._lfs_guard(request, "verify", oid, size)
        body = await _json(request)
        if body.get("oid") != oid or (body.get("size") is not None and body.get("size") != size):
            raise UploadError(400, "the verify request does not match the upload it belongs to")
        staged = lfs_path(self.storage, oid)
        if not staged.is_file():
            if self.storage.blobs.exists(oid):                    # stored earlier: nothing to check
                return JSONResponse({})
            raise UploadError(404, "this object was not uploaded")
        digest = await run_in_threadpool(digest_of, staged)       # recorded while the bytes arrived; hashed here (once) only if that record is gone
        if digest != oid or staged.stat().st_size != size:
            await run_in_threadpool(drop_staged_lfs, self.storage, oid)
            raise UploadError(422, f"sha256 mismatch: the uploaded bytes hash to {digest}, not {oid}")
        return JSONResponse({})


# ---- small helpers ------------------------------------------------------------------------------------------------------------

def _query_int(request: Request, name: str) -> int:
    try:
        value = int(request.query_params.get(name, ""))
    except ValueError:
        raise UploadError(400, f"?{name}= is required")
    if value < 0:
        raise UploadError(400, f"?{name}= must not be negative")
    return value


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise UploadError(400, "the request body must be JSON")
    if not isinstance(body, dict):
        raise UploadError(400, "the request body must be a JSON object")
    return body


async def _ndjson_lines(request: Request):
    """Yield the non-empty lines of the body as they complete. Memory: one unfinished line (capped at MAX_LINE)."""
    buf = bytearray()
    scanned = 0
    async for chunk in request.stream():
        buf += chunk
        while True:
            i = buf.find(b"\n", scanned)
            if i < 0:
                scanned = len(buf)
                break
            line = bytes(buf[:i])
            del buf[:i + 1]
            scanned = 0
            if line.strip():
                yield line
        if len(buf) > MAX_LINE:
            raise UploadError(413, "a commit line is larger than 64 MiB: files that big must go through LFS")
    if buf.strip():
        yield bytes(buf)


def _write_and_hash(f, digest, data: bytes) -> None:
    f.write(data)
    digest.update(data)


def _place_staged(part: Path, final: Path, digest: str) -> None:
    """Rename a finished upload into place and record its hash (the old record, if any, is for other bytes)."""
    final.with_name(final.name + ".sha256").unlink(missing_ok=True)
    os.replace(part, final)
    remember_digest(final, digest)


def _stage_bytes(stage: Path, n: int, path: str, data: bytes) -> Addition:
    """Write one regular file's bytes to the commit's staging dir, hashing them on the way (done in a thread)."""
    stage.mkdir(parents=True, exist_ok=True)
    src = stage / str(n)
    src.write_bytes(data)
    return Addition(path=path, sha256=hashlib.sha256(data).hexdigest(), size=len(data), src=src, git_sha1=git_blob_sha1(data), lfs=False)
