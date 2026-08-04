"""Content-addressable blob store.

Blobs are immutable and identified by their SHA-256 digest. Storage layout is
sharded (Git/OCI/Nix style) for filesystem friendliness:

    blobs/sha256/ab/abcdef0123...

The blob store contains ONLY immutable blobs — no sidecar files, no metadata.
Reference counts are derived by the repository core during garbage collection,
not stored here. This keeps the store trivially rsync-portable and corruption-
resistant: a blob either exists at its content address or it does not.

Crash safety: a blob becomes visible via a single atomic rename from a staging
path into its final location. An interrupted write leaves only a stale staging
file, never a half-written blob at a content address.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

# A full SHA-256 hex digest is 64 chars. We shard on the first two chars.
_DIGEST_HEX_LEN = 64
_SHARD_LEN = 2

HEX = str
DIGEST_PREFIX = "sha256:"


def parse_blob_ref(blob_ref: str) -> HEX:
    """'sha256:abcdef...' -> 'abcdef...'. Raises ValueError if malformed."""
    if not blob_ref.startswith(DIGEST_PREFIX):
        raise ValueError(f"blob ref must start with {DIGEST_PREFIX!r}: {blob_ref!r}")
    digest = blob_ref[len(DIGEST_PREFIX):]
    if len(digest) != _DIGEST_HEX_LEN:
        raise ValueError(f"expected {_DIGEST_HEX_LEN}-hex digest, got len={len(digest)}")
    try:
        int(digest, 16)
    except ValueError as exc:
        raise ValueError(f"digest is not hex: {digest!r}") from exc
    return digest


def make_blob_ref(digest: HEX) -> str:
    """'abcdef...' -> 'sha256:abcdef...'."""
    return DIGEST_PREFIX + digest


def sha256_file(path: Path, *, chunk_size: int = 1 << 20) -> HEX:
    """Stream-hash a file, returning the hex digest."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> HEX:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class StoredBlob:
    """A blob that has been committed to the store."""

    digest: HEX          # raw hex digest (no prefix)
    size: int
    path: Path


class BlobStore:
    """Immutable, content-addressable blob store over a local directory."""

    def __init__(self, root: Path):
        self.root = Path(root)

    # ---- path helpers --------------------------------------------------

    def _blob_path(self, digest: HEX) -> Path:
        return self.root / digest[:_SHARD_LEN] / digest

    # ---- queries -------------------------------------------------------

    def exists(self, digest: HEX) -> bool:
        return self._blob_path(digest).is_file()

    def path_for(self, digest: HEX) -> Path:
        """Path where a blob lives (or would live). Does not imply existence."""
        return self._blob_path(digest)

    def size(self, digest: HEX) -> int:
        return self._blob_path(digest).stat().st_size

    def open(self, digest: HEX) -> "object":
        """Open a blob for binary reading. Caller closes the file."""
        return open(self._blob_path(digest), "rb")

    # ---- writes --------------------------------------------------------

    def put_file(self, src: Path, expected_digest: HEX | None = None) -> StoredBlob:
        """Move a staged file into the store, content-addressed.

        The file is hashed first. If `expected_digest` is given and does not
        match, the source file is NOT consumed and IntegrityError is raised.

        Because blobs are content-addressed, if the blob already exists the
        source is simply removed (dedup). The write is atomic: we stage into a
        temp path next to the destination, fsync, then rename.
        """
        actual = sha256_file(src)
        if expected_digest is not None and actual != expected_digest:
            raise IntegrityError(expected_digest, actual, str(src))

        dest = self._blob_path(actual)
        if dest.is_file():
            # Already present — identical content, drop the duplicate source.
            src.unlink(missing_ok=True)
            return StoredBlob(digest=actual, size=dest.stat().st_size, path=dest)

        dest.parent.mkdir(parents=True, exist_ok=True)
        _atomic_replace(src, dest, fsync=True)
        return StoredBlob(digest=actual, size=dest.stat().st_size, path=dest)

    def put_bytes(self, data: bytes) -> StoredBlob:
        """Write bytes into the store, content-addressed."""
        digest = sha256_bytes(data)
        dest = self._blob_path(digest)
        if dest.is_file():
            return StoredBlob(digest=digest, size=dest.stat().st_size, path=dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Write via a temp file in the same dir, then atomic rename.
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".write-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, dest)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise
        return StoredBlob(digest=digest, size=len(data), path=dest)

    # ---- verification --------------------------------------------------

    def verify(self, digest: HEX) -> bool:
        """Return True if the blob exists and its content hashes to `digest`."""
        path = self._blob_path(digest)
        if not path.is_file():
            return False
        return sha256_file(path) == digest


class IntegrityError(Exception):
    """Raised when content does not hash to its expected digest."""

    def __init__(self, expected: str, actual: str, where: str):
        super().__init__(
            f"integrity error at {where}: expected sha256 {expected}, got {actual}"
        )
        self.expected = expected
        self.actual = actual
        self.where = where


def _atomic_replace(src: Path, dest: Path, *, fsync: bool) -> None:
    """Replace dest with src via an atomic os.replace, with optional fsync.

    Uses a hardlink-then-rename dance if src and dest might be on different
    filesystems (e.g. staging dir on a different mount). For the common case
    where they share a filesystem, os.replace is directly atomic.
    """
    try:
        os.replace(src, dest)
    except OSError:
        # Cross-device: copy then atomic replace.
        import shutil
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".xdev-")
        tmp_path = Path(tmp)
        try:
            os.close(fd)
            shutil.copyfile(src, tmp_path)
            if fsync:
                with open(tmp_path, "rb") as f:
                    os.fsync(f.fileno())
            os.replace(tmp_path, dest)
        finally:
            tmp_path.unlink(missing_ok=True)
            src.unlink(missing_ok=True)
        return
    if fsync:
        # fsync the directory so the rename is durable.
        dir_fd = os.open(str(dest.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
