"""Storage backend abstraction.

Only FilesystemStorage exists today. It covers local disk, an SMB/NFS mount, a
Synology share, or a USB SSD — those are all just mounts of one filesystem
backend, not separate implementations. S3Storage is a future addition, added
only when genuinely needed; the repository core talks to StorageBackend and
does not care which concrete backend is wired in.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator, Protocol

from . import blobstore
from .config import Settings


class StorageBackend(Protocol):
    """What the repository core needs from a storage backend.

    A backend owns the blob store plus the repos/staging/events areas. It is the
    single handle through which the core touches the filesystem.
    """

    @property
    def blobs(self) -> blobstore.BlobStore: ...

    @property
    def repos_dir(self) -> Path: ...

    @property
    def staging_dir(self) -> Path: ...

    @property
    def events_path(self) -> Path: ...

    @property
    def index_dir(self) -> Path: ...

    def ensure_layout(self) -> None:
        """Create the directory skeleton. Idempotent."""
        ...

    def iter_repos(self) -> Iterator[str]:
        """Yield repo_id for each repository present (e.g. 'org/name')."""
        ...


class FilesystemStorage:
    """The only backend: everything under a single root directory.

    Layout (see docs/DESIGN.md):
        <root>/blobs/sha256/<shard>/<digest>
        <root>/repos/<org>/<repo>/revisions/<commit>/manifest.json
        <root>/repos/<org>/<repo>/refs.json
        <root>/staging/
        <root>/index/
        <root>/events.jsonl
    """

    def __init__(self, settings: Settings):
        self._settings = settings
        self._blobs = blobstore.BlobStore(settings.blobs_dir)

    @property
    def blobs(self) -> blobstore.BlobStore:
        return self._blobs

    @property
    def root(self) -> Path:
        return self._settings.mirror_root

    @property
    def repos_dir(self) -> Path:
        return self._settings.repos_dir

    @property
    def staging_dir(self) -> Path:
        return self._settings.staging_dir

    @property
    def events_path(self) -> Path:
        return self._settings.events_path

    @property
    def index_dir(self) -> Path:
        return self._settings.index_dir

    def ensure_layout(self) -> None:
        self._settings.ensure_dirs()

    def repo_dir(self, repo_id: str) -> Path:
        org, name = _split_repo_id(repo_id)
        return self.repos_dir / org / name

    def revisions_dir(self, repo_id: str) -> Path:
        return self.repo_dir(repo_id) / "revisions"

    def manifest_path(self, repo_id: str, commit: str) -> Path:
        return self.revisions_dir(repo_id) / commit / "manifest.json"

    def refs_path(self, repo_id: str) -> Path:
        return self.repo_dir(repo_id) / "refs.json"

    def iter_repos(self) -> Iterator[str]:
        if not self.repos_dir.is_dir():
            return
        for org_path in sorted(self.repos_dir.iterdir()):
            if not org_path.is_dir():
                continue
            for name_path in sorted(org_path.iterdir()):
                if not name_path.is_dir():
                    continue
                # Only yield if it looks like a repo (has revisions/ or refs.json).
                if (name_path / "revisions").is_dir() or (name_path / "refs.json").is_file():
                    yield f"{org_path.name}/{name_path.name}"


def _split_repo_id(repo_id: str) -> tuple[str, str]:
    """'org/name' -> ('org', 'name'). Single-segment names get a leading '_'."""
    if "/" in repo_id:
        org, name = repo_id.split("/", 1)
    else:
        org, name = "_user", repo_id
    _validate_segment(org)
    _validate_segment(name)
    return org, name


class InvalidRepoId(ValueError):
    """A repo id (or segment) that could escape the store or is malformed: the HTTP layer answers 404, never 500."""


def _validate_segment(segment: str) -> None:
    """Reject path traversal and odd characters in repo id segments."""
    if not segment or segment in (".", ".."):
        raise InvalidRepoId(f"invalid repo id segment: {segment!r}")
    if "/" in segment or segment.startswith("."):
        raise InvalidRepoId(f"invalid repo id segment: {segment!r}")
