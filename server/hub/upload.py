"""Publishing your own models and datasets: the commit logic both upload doors share, and the GUI's web-upload API.

Two doors lead here: the Hugging Face protocol (`server/adapters/hf_upload.py`: `hf upload`, `HfApi.upload_folder`) and the
browser (`/api/hub/upload/*`, below). Both end in `publish_commit`, the ONE place that turns staged files into a new revision:

    staged files -> (hash once) -> store lock -> blobs -> manifest -> publish -> move `main`

Rules that matter:
  - The whole store-and-publish step runs under `repo.ingest_lock()` (shared flock; GC needs it exclusive): blobs that no manifest
    names yet are otherwise garbage to a concurrent GC (server/locks.py). Commits to one repo are also serialised in-process, so
    two uploads cannot both start from the same head and lose one another's changes.
  - Every file is hashed exactly once: while the bytes arrive (LFS PUT), before the lock (browser path), or when the commit is read
    (regular files). A file whose hash record is missing is hashed once at verify/commit. The store then takes the file with
    put_blob(..., verified=True).
  - A new revision = the files of the current head with this commit's changes applied. A commit that changes nothing publishes nothing.
  - Nothing here reads a whole file into memory except one regular (<= ~1 MiB by the Hub's own rule) file line at a time.
Staged data lives in <root>/staging/uploads/ (never served, swept after a few days).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .. import events
from ..blobstore import sha256_file
from ..fsutil import atomic_write_text
from ..manifests import FileEntry, Manifest, RevisionInfo, SCHEMA_VERSION, Source
from ..repository import RepositoryCore, RevisionExists
from ..storage import InvalidRepoId, core_repo_id, public_repo_id

# ---- the Hub's rule for what is a "large file" (LFS) ------------------------------------------------------------------

LFS_MIN_BYTES = 1 << 20
LFS_EXTENSIONS = (".safetensors", ".gguf", ".bin", ".pt", ".pth", ".ckpt", ".onnx", ".parquet", ".arrow", ".h5", ".npz",
                  ".zip", ".tar", ".gz", ".zst")


def is_lfs(path: str, size: int) -> bool:
    """LFS when bigger than 1 MiB or a known weight/data format; everything else travels inside the commit."""
    return size > LFS_MIN_BYTES or path.lower().endswith(LFS_EXTENSIONS)


class UploadError(Exception):
    """A refusal with an HTTP status and a sentence for the user. `code` becomes the Hub's X-Error-Code where the client keys off it."""

    def __init__(self, status: int, message: str, code: str | None = None):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


# ---- names and paths ------------------------------------------------------------------------------------------------

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_OID_RE = re.compile(r"^[0-9a-f]{64}$")
_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_RESERVED_NAMESPACES = {"api", "datasets", "spaces", "models", "_lfs"}


def is_oid(value: object) -> bool:
    return isinstance(value, str) and bool(_OID_RE.match(value))


def validate_repo_name(namespace: str, name: str) -> None:
    """The names a NEW repository may have (existing ones, ingested from elsewhere, may be odder). Same shape as the Hub's."""
    for label, seg in (("namespace", namespace), ("name", name)):
        if not isinstance(seg, str) or not _NAME_RE.match(seg) or ".." in seg or "--" in seg:
            raise UploadError(400, f"invalid repository {label} {seg!r}: letters, digits, '-', '_' and '.', at most 96 characters")
    if namespace.lower() in _RESERVED_NAMESPACES:
        raise UploadError(400, f"the namespace {namespace!r} is reserved")


def validate_repo_path(path: object, *, allow_dot_segments: bool = True) -> str:
    """A file path inside a repository: relative, '/'-separated, no '.'/'..'/empty segments, no control characters.
    `allow_dot_segments=False` (the browser door) also refuses any segment that starts with '.', which is what keeps the
    session's own bookkeeping files out of reach of an uploaded name."""
    if not isinstance(path, str) or not path:
        raise UploadError(400, "a file path is required")
    if len(path) > 1024 or "\\" in path or any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise UploadError(400, f"invalid path {path[:80]!r}")
    if path.startswith("/") or path.endswith("/"):
        raise UploadError(400, f"invalid path {path!r}: it must be relative and name a file")
    segments = path.split("/")
    for seg in segments:
        if seg in ("", ".", "..") or len(seg.encode()) > 255:
            raise UploadError(400, f"invalid path {path!r}: no '.', '..' or empty segments")
        if not allow_dot_segments and seg.startswith("."):
            raise UploadError(400, f"invalid path {path!r}: a name may not start with '.'")
    if segments[0] == ".git":
        raise UploadError(400, "paths below .git/ are not allowed")
    return path


def locate(repo: RepositoryCore, namespace: str, repo_type: str, repo_id: str) -> tuple[str, bool]:
    """Core id for a client-style `repo_id` ('org/name', or a bare name which gets `namespace` unless a repo of that bare name
    exists) and whether that repository exists. A repository that does not exist yet must have a name a NEW repo may have."""
    if repo_type == "space":
        raise UploadError(400, "Spaces are not supported")
    if repo_type not in ("model", "dataset"):
        raise UploadError(400, "type must be model or dataset")
    if not isinstance(repo_id, str) or not repo_id or repo_id.count("/") > 1:
        raise UploadError(400, "id must look like 'organisation/name'")
    try:
        cid = core_repo_id(repo_type, repo_id)
        if "/" not in repo_id and not repo.repository_exists(cid):
            cid = core_repo_id(repo_type, f"{namespace}/{repo_id}")
        exists = repo.repository_exists(cid)
    except InvalidRepoId as exc:
        raise UploadError(400, str(exc))
    if not exists:
        org, name = public_repo_id(cid).split("/", 1)
        validate_repo_name(org, name)
    return cid, exists


# ---- staging --------------------------------------------------------------------------------------------------------

LFS_STAGE_TTL = 2 * 86400            # an LFS object nobody committed
WEB_STAGE_TTL = 7 * 86400            # a browser session nobody committed or discarded


def uploads_dir(storage) -> Path:
    d = Path(storage.staging_dir) / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def lfs_path(storage, oid: str) -> Path:
    return uploads_dir(storage) / oid


def _signature(path: Path) -> str:
    st = path.stat()
    return f"{st.st_size}:{st.st_mtime_ns}:{st.st_ino}"


def _sidecar(path: Path) -> Path:
    return path.with_name(path.name + ".sha256")


def remember_digest(path: Path, digest: str) -> None:
    """Note 'this exact file (size, mtime, inode) hashes to `digest`' next to it, so nobody reads a large file twice."""
    atomic_write_text(_sidecar(path), f"{digest}\n{_signature(path)}")


def known_digest(path: Path) -> str | None:
    """The sha256 recorded for `path` if it is still the very file that was hashed, else None."""
    try:
        digest, sig = _sidecar(path).read_text().split("\n", 1)
        return digest if sig == _signature(path) and is_oid(digest) else None
    except (OSError, ValueError):
        return None


def digest_of(path: Path) -> str:
    """sha256 of a staged file: the recorded one (computed while the bytes arrived), else hash it now, once, and record it."""
    digest = known_digest(path)
    if digest is None:
        digest = sha256_file(path)
        remember_digest(path, digest)
    return digest


def drop_staged_lfs(storage, oid: str) -> None:
    p = lfs_path(storage, oid)
    for q in (p, _sidecar(p), p.with_name(p.name + ".part")):
        q.unlink(missing_ok=True)


def sweep_staging(storage) -> None:
    """Delete staged uploads nobody finished. Best effort, cheap (one directory listing); called when a new upload starts."""
    now = time.time()
    try:
        entries = list(uploads_dir(storage).iterdir())
    except OSError:
        return
    for p in entries:
        try:
            ttl = WEB_STAGE_TTL if p.name.startswith("web-") else LFS_STAGE_TTL
            if now - p.stat().st_mtime < ttl:
                continue
            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
        except OSError:
            continue


# ---- short-lived tokens for the URLs the LFS batch hands out (the client does not send its Authorization header to them) --------

TOKEN_TTL = 3600


def sign(secret: str, purpose: str, oid: str, size: int, exp: int) -> str:
    return hmac.new(secret.encode(), f"{purpose}:{oid}:{size}:{exp}".encode(), hashlib.sha256).hexdigest()


def check_signature(secret: str, purpose: str, oid: str, size: int, exp: int, sig: str) -> bool:
    return bool(secret) and exp >= time.time() and hmac.compare_digest(sign(secret, purpose, oid, size, exp), sig or "")


# ---- the commit -----------------------------------------------------------------------------------------------------

@dataclass
class Addition:
    """One file to add. `src` is a staged file that the store takes over (moved, or removed if the blob is already there); None
    means the bytes are already a blob in the store (an LFS object uploaded earlier). `lfs` only decides the etag we advertise."""

    path: str
    sha256: str
    size: int | None
    src: Path | None = None
    git_sha1: str | None = None
    lfs: bool = False
    pending_lfs: bool = False         # an lfsFile line: find its staged file / blob in resolve_lfs()


@dataclass
class Op:
    kind: str                         # "add" | "delete_file" | "delete_folder"
    path: str
    addition: Addition | None = None


@dataclass
class CommitResult:
    commit: str
    created: bool                     # False: nothing changed, `commit` is the head that already was
    files: int
    parent: str | None = None


def git_blob_sha1(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def resolve_lfs(storage, a: Addition) -> None:
    """Find the bytes of an lfsFile line: the staged upload (hashed while it arrived; hashed here once if that record is missing), else an existing blob."""
    staged = lfs_path(storage, a.sha256)
    if staged.is_file():
        size = staged.stat().st_size
        if a.size is not None and size != a.size:
            raise UploadError(400, f"{a.path}: the uploaded object is {size} bytes, the commit says {a.size}")
        if digest_of(staged) != a.sha256:
            drop_staged_lfs(storage, a.sha256)
            raise UploadError(422, f"{a.path}: the uploaded data does not hash to {a.sha256}")
        a.src, a.size = staged, size
    elif storage.blobs.exists(a.sha256):
        size = storage.blobs.size(a.sha256)
        if a.size is not None and size != a.size:
            raise UploadError(400, f"{a.path}: the stored object is {size} bytes, the commit says {a.size}")
        a.src, a.size = None, size
    else:
        raise UploadError(400, f"{a.path}: LFS object {a.sha256} was not uploaded (or it expired): upload it again, then commit")
    a.pending_lfs, a.lfs = False, True


_repo_locks: dict[str, threading.Lock] = {}
_repo_locks_guard = threading.Lock()


def _repo_lock(repo_id: str) -> threading.Lock:
    with _repo_locks_guard:
        return _repo_locks.setdefault(repo_id, threading.Lock())


def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class _Slot:
    digest: str
    size: int
    meta: dict = field(default_factory=dict)
    addition: Addition | None = None


def publish_commit(repo: RepositoryCore, repo_id: str, ops: list[Op], *, summary: str, description: str = "",
                   parent: str | None = None, keep_only: set[str] | None = None, ref: str = "main") -> CommitResult:
    """Apply `ops` (in order) to the files of `ref`'s head and publish the result as a new revision.

    `keep_only`: afterwards drop every file not in this set (the browser's "replace" mode).
    Raises UploadError: 404 delete of a missing file/folder, 409 `parent` is not the head, 400/422 bad or missing LFS data.
    Returns the new commit, or the unchanged head (created=False) when the commit changes nothing."""
    storage = repo._storage
    adds = [op.addition for op in ops if op.kind == "add" and op.addition is not None]
    for a in adds:                                       # may hash a large staged file: before the lock, nobody waits for it
        if a.pending_lfs:
            resolve_lfs(storage, a)

    consumed_oids = {a.sha256 for a in adds if a.lfs}
    try:
        with _repo_lock(repo_id), repo.ingest_lock():
            return _publish_locked(repo, repo_id, ops, adds, summary=summary, description=description, parent=parent,
                                   keep_only=keep_only, ref=ref)
    finally:
        for oid in consumed_oids:                        # the object either became a blob or was a duplicate: its hash record is no use any more
            _sidecar(lfs_path(storage, oid)).unlink(missing_ok=True)


def _publish_locked(repo, repo_id, ops, adds, *, summary, description, parent, keep_only, ref) -> CommitResult:
    storage = repo._storage
    head = repo.list_references(repo_id).get(ref)
    head_files = {f.path: f for f in repo.get_manifest(repo_id, head).files} if head else {}
    if parent and (not head or not (head == parent or (len(parent) >= 7 and head.startswith(parent)))):
        raise UploadError(409, f"the repository changed since {parent[:12]}: the current head is {head or 'empty'}", code="Conflict")

    state: dict[str, _Slot] = {}
    for path, f in head_files.items():
        state[path] = _Slot(digest=f.blob.split(":", 1)[1], size=f.size, meta=f.metadata)
    for op in ops:
        if op.kind == "add" and op.addition is not None:
            a = op.addition
            etag = '"%s"' % ((a.sha256 if a.lfs else a.git_sha1) or a.sha256[:32] + "-1000")
            state[op.path] = _Slot(digest=a.sha256, size=a.size or 0, meta={"hf": {"etag": etag}}, addition=a)
        elif op.kind == "delete_file":
            if op.path not in state:
                raise UploadError(404, "A file with this name doesn't exist", code="EntryNotFound")
            del state[op.path]
        elif op.kind == "delete_folder":
            prefix = op.path.rstrip("/") + "/"
            gone = [p for p in state if p.startswith(prefix)]
            if not gone:
                raise UploadError(404, "A folder with this name doesn't exist", code="EntryNotFound")
            for p in gone:
                del state[p]
    if keep_only is not None:
        state = {p: s for p, s in state.items() if p in keep_only}
    for p in state:                                      # a file and a folder cannot share a name
        parts = p.split("/")
        for i in range(1, len(parts)):
            if "/".join(parts[:i]) in state:
                raise UploadError(400, f"{p!r} cannot be added: {'/'.join(parts[:i])!r} is a file")

    if head and {p: s.digest for p, s in state.items()} == {p: f.blob.split(":", 1)[1] for p, f in head_files.items()}:
        for a in adds:                                   # nothing changed: the staged copies are redundant
            if a.src is not None:
                a.src.unlink(missing_ok=True)
        return CommitResult(commit=head, created=False, files=len(state), parent=head)

    # Everything must be there BEFORE the first blob moves: a missing object then leaves the store untouched.
    for a in adds:
        if not ((a.src is not None and a.src.is_file()) or storage.blobs.exists(a.sha256)):
            raise UploadError(400, f"{a.path}: the uploaded data is gone (another commit used it, or it expired): upload it again")
    for a in adds:
        if a.src is not None and a.src.is_file():
            repo.put_blob(a.src, expected_digest=a.sha256, verified=True)

    stamp = _utcnow_iso()
    listing = "\n".join(f"{p}\0{s.digest}" for p, s in sorted(state.items()))
    commit = ""
    for attempt in range(5):
        seed = "\n".join((parent or head or "", summary, description, listing, str(time.time_ns()), str(attempt)))
        commit = hashlib.sha1(seed.encode()).hexdigest()
        entries = []
        for p, s in sorted(state.items()):
            hf = {**(s.meta.get("hf") or {}), "x_repo_commit": commit}      # resolve() sends this as X-Repo-Commit: it must name THIS revision
            entries.append(FileEntry(path=p, size=s.size, blob=f"sha256:{s.digest}", checksums={"sha256": s.digest},
                                     metadata={**s.meta, "hf": hf}))
        manifest = Manifest(schema_version=SCHEMA_VERSION, repo_id=repo_id, source=Source(kind="upload", ref="upload"),
                            revision=RevisionInfo(commit=commit, fetched_at=stamp, license=None), files=entries)
        try:
            repo.publish_revision(manifest)
            break
        except RevisionExists:
            continue
    else:                                                # pragma: no cover - needs five sha1 collisions
        raise UploadError(409, "could not allocate a commit id; try again")
    repo.update_reference(repo_id, ref, commit)
    repo._events.emit(events.UPLOAD_COMMITTED, repo_id=repo_id, commit=commit, parent=head, summary=summary[:200],
                      files=len(entries), added=len(adds))
    return CommitResult(commit=commit, created=True, files=len(entries), parent=head)


# ---- admin check shared by the two doors ----------------------------------------------------------------------------------

def bearer(request: Request) -> bytes:
    h = request.headers.get("authorization", "")
    return h[7:].strip().encode() if h[:7].lower() == "bearer " else b""


def is_admin(ctx, request: Request) -> bool:
    return bool(ctx.admin_token) and hmac.compare_digest(bearer(request), ctx.admin_token.encode())


READ_ONLY_MESSAGE = "This mirror is read-only (MIRROR_READ_ONLY is set): nothing can be uploaded."


# ---- the GUI's web upload: /api/hub/upload/* --------------------------------------------------------------------------------

class WebCommitReq(BaseModel):
    type: str = "model"
    id: str
    session: str
    summary: str = ""
    replace: bool = False


class DiscardReq(BaseModel):
    session: str


def _session_dir(storage, session: str) -> Path:
    if not isinstance(session, str) or not _SESSION_RE.match(session):
        raise HTTPException(400, "session must be 1-64 letters, digits, '-' or '_'")
    return uploads_dir(storage) / f"web-{session}"


def _as_http(exc: UploadError) -> HTTPException:
    return HTTPException(exc.status, exc.message)


def hash_staged_file(path: Path, rel: str) -> Addition:
    """Hash a browser-uploaded file once (sha256, and the git blob id for the small files the Hub keeps outside LFS)."""
    size = path.stat().st_size
    lfs = is_lfs(rel, size)
    if lfs:
        return Addition(path=rel, sha256=sha256_file(path), size=size, src=path, lfs=True)
    data = path.read_bytes()                              # not LFS => at most 1 MiB
    return Addition(path=rel, sha256=hashlib.sha256(data).hexdigest(), size=len(data), src=path, git_sha1=git_blob_sha1(data))


def create_router(ctx) -> APIRouter:
    """ctx: server.main.HubContext. Mounted by create_app next to the rest of /api/hub; every route here changes something: admin token."""
    router = APIRouter(prefix="/api/hub/upload")
    storage = ctx.storage

    def guard(request: Request) -> None:
        if ctx.settings.read_only:
            raise HTTPException(403, READ_ONLY_MESSAGE)
        if not is_admin(ctx, request):
            raise HTTPException(401, "the admin token is required for this action")

    @router.put("/file")
    async def upload_file(request: Request, type: str = "model", id: str = "", path: str = "", session: str = ""):
        """Stream one file into the session. Asking for the same path again replaces it. The body is the raw file."""
        guard(request)
        try:
            rel = validate_repo_path(path, allow_dot_segments=False)
            core_id, _ = locate(ctx.repo, ctx.settings.namespace, type, id)
        except UploadError as exc:
            raise _as_http(exc)
        sdir = _session_dir(storage, session)
        try:
            await run_in_threadpool(_open_session, sdir, type, core_id)
        except UploadError as exc:
            raise _as_http(exc)
        dest = sdir / rel
        if not dest.resolve().is_relative_to(sdir.resolve()):          # belt and braces next to validate_repo_path
            raise HTTPException(400, "invalid path")
        part = sdir / f".partial-{uuid.uuid4().hex}"
        total = 0
        try:
            with open(part, "wb") as f:
                buf = bytearray()
                async for chunk in request.stream():
                    buf += chunk
                    total += len(chunk)
                    if len(buf) >= 1 << 20:
                        await run_in_threadpool(f.write, bytes(buf))
                        buf.clear()
                if buf:
                    await run_in_threadpool(f.write, bytes(buf))
            await run_in_threadpool(_place, part, dest)
        except UploadError as exc:
            raise _as_http(exc)
        finally:
            part.unlink(missing_ok=True)
        return {"path": rel, "size": total}

    def _open_session(sdir: Path, type_: str, core_id: str) -> None:
        sweep_staging(storage)
        sdir.mkdir(parents=True, exist_ok=True)
        meta = sdir / ".meta.json"
        want = {"type": type_, "core_id": core_id}
        try:
            have = json.loads(meta.read_text())
        except (OSError, ValueError):
            atomic_write_text(meta, json.dumps(want))
            return
        if have != want:
            raise UploadError(409, "this session already holds files for another repository")

    def _place(part: Path, dest: Path) -> None:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.is_dir():
                raise UploadError(409, f"{dest.name!r} is a folder in this session")
            os.replace(part, dest)
        except (NotADirectoryError, FileExistsError):
            raise UploadError(409, "that path collides with another file in this session (a file and a folder with the same name)")

    @router.post("/commit")
    async def commit(req: WebCommitReq, request: Request):
        """Publish the session's files as ONE new revision of `main` (creating the repository if it is new), then delete the session."""
        guard(request)
        sdir = _session_dir(storage, req.session)
        if not sdir.is_dir():
            raise HTTPException(404, "unknown session (already committed, discarded or expired)")
        try:
            core_id, exists = locate(ctx.repo, ctx.settings.namespace, req.type, req.id)
            try:
                meta = json.loads((sdir / ".meta.json").read_text())
            except (OSError, ValueError):
                meta = {}
            if meta.get("core_id") != core_id:
                raise UploadError(409, "this session holds files for another repository")
            additions = await run_in_threadpool(_collect, sdir)
            if not additions:
                raise UploadError(400, "this session holds no files")
            if not exists:
                ctx.repo.create_repository(core_id)
            ops = [Op("add", a.path, a) for a in additions]
            summary = (req.summary or "").strip() or f"Upload {len(additions)} file{'s' if len(additions) != 1 else ''} from the web"
            result = await run_in_threadpool(
                lambda: publish_commit(ctx.repo, core_id, ops, summary=summary,
                                       keep_only={a.path for a in additions} if req.replace else None))
        except UploadError as exc:
            raise _as_http(exc)
        await run_in_threadpool(shutil.rmtree, sdir, True)
        ctx.usage.invalidate()
        return {"id": public_repo_id(core_id), "type": req.type, "commit": result.commit, "created": result.created,
                "files": result.files}

    def _collect(sdir: Path) -> list[Addition]:
        out = []
        for p in sorted(sdir.rglob("*")):
            rel = p.relative_to(sdir).as_posix()
            if p.is_file() and not any(seg.startswith(".") for seg in rel.split("/")):
                out.append(hash_staged_file(p, rel))
        return out

    @router.post("/discard")
    async def discard(req: DiscardReq, request: Request):
        guard(request)
        await run_in_threadpool(shutil.rmtree, _session_dir(storage, req.session), True)
        return {"ok": True}

    return router
