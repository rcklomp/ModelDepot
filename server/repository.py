"""Repository Core — pure domain logic.

This module knows nothing about HTTP or Hugging Face. Adapters translate
requests into calls here. The core composes four collaborators:

  - storage.StorageBackend : where blobs/manifests/refs live on disk
  - blobstore.BlobStore    : content-addressed blob I/O
  - refs                   : mutable reference (branch/tag) state
  - events.EventLog        : append-only audit log

The core is the only thing that mutates live pointers (refs). Publishing a
revision is the single atomic step that makes new content visible to serving.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from . import events, manifests, refs
from .blobstore import HEX, IntegrityError, StoredBlob
from .storage import FilesystemStorage


@dataclass(frozen=True)
class ResolvedFile:
    """A file within a resolved revision, with a handle to its blob."""

    path: str
    size: int
    blob_ref: str            # 'sha256:<digest>'
    digest: HEX              # raw hex digest
    metadata: dict


class RepositoryNotFound(Exception):
    pass


class RevisionNotFound(Exception):
    pass


class FileNotFound(Exception):
    pass


class RevisionExists(Exception):
    """Raised when trying to publish a revision that already exists."""


class RepositoryCore:
    """Domain operations over the store. Stateless beyond its collaborators."""

    def __init__(self, storage: FilesystemStorage, event_log: events.EventLog):
        self._storage = storage
        self._events = event_log

    # ---- reads ---------------------------------------------------------

    def list_repos(self) -> list[str]:
        return list(self._storage.iter_repos())

    def list_references(self, repo_id: str) -> dict[str, str]:
        return refs.load(self._storage.refs_path(repo_id))

    def list_revisions(self, repo_id: str) -> list[str]:
        rev_dir = self._storage.revisions_dir(repo_id)
        if not rev_dir.is_dir():
            return []
        return sorted(
            d.name for d in rev_dir.iterdir() if d.is_dir() and (d / "manifest.json").is_file()
        )

    def resolve_ref(self, repo_id: str, ref_or_commit: str) -> str:
        """Resolve a ref name or raw commit to a concrete commit hash.

        Tries, in order: (1) a known ref name, (2) the hex-commit heuristic,
        (3) an actual published revision with that name. Raises
        RepositoryNotFound / RevisionNotFound.
        """
        refs_map = self.list_references(repo_id)
        if ref_or_commit in refs_map:
            return refs_map[ref_or_commit]
        # Hex-commit heuristic: a value that looks like a SHA is treated as one.
        commit = refs.resolve(refs_map, ref_or_commit)
        if commit is not None:
            return commit
        # Fallback: a published revision directory literally named ref_or_commit.
        if self._storage.manifest_path(repo_id, ref_or_commit).is_file():
            return ref_or_commit
        # Distinguish "no such repo" from "no such revision" for clear errors.
        if not refs_map and not self._storage.revisions_dir(repo_id).is_dir():
            if not self._storage.refs_path(repo_id).is_file():
                raise RepositoryNotFound(repo_id)
        raise RevisionNotFound(f"{repo_id}: unknown ref {ref_or_commit!r}")

    def get_manifest(self, repo_id: str, ref_or_commit: str) -> manifests.Manifest:
        commit = self.resolve_ref(repo_id, ref_or_commit)
        path = self._storage.manifest_path(repo_id, commit)
        if not path.is_file():
            raise RevisionNotFound(f"{repo_id}@{commit}: manifest missing")
        return manifests.read(path)

    def list_files(self, repo_id: str, ref_or_commit: str) -> list[manifests.FileEntry]:
        return self.get_manifest(repo_id, ref_or_commit).files

    def get_file(self, repo_id: str, ref_or_commit: str, file_path: str) -> ResolvedFile:
        """Look up a single file in a revision. Raises FileNotFound."""
        m = self.get_manifest(repo_id, ref_or_commit)
        for entry in m.files:
            if entry.path == file_path:
                digest = entry.blob.split(":", 1)[1]
                return ResolvedFile(
                    path=entry.path,
                    size=entry.size,
                    blob_ref=entry.blob,
                    digest=digest,
                    metadata=entry.metadata,
                )
        raise FileNotFound(f"{repo_id}: {file_path}")

    def open_blob(self, digest: HEX):
        """Open a blob for binary streaming. Caller closes the file."""
        if not self._storage.blobs.exists(digest):
            raise FileNotFound(f"blob sha256:{digest}")
        return self._storage.blobs.open(digest)

    def blob_path(self, digest: HEX) -> Path:
        """Filesystem path for a blob (for X-Sendfile / range serving)."""
        return self._storage.blobs.path_for(digest)

    # ---- writes --------------------------------------------------------

    def put_blob(self, src: Path, expected_digest: HEX | None = None, *, verified: bool = False) -> StoredBlob:
        """Content-address a staged file into the blob store. Emits BlobStored."""
        blob = self._storage.blobs.put_file(src, expected_digest=expected_digest, verified=verified)
        self._events.emit(
            events.BLOB_STORED, digest=blob.digest, size=blob.size, repo=None
        )
        return blob

    def put_blob_bytes(self, data: bytes) -> StoredBlob:
        blob = self._storage.blobs.put_bytes(data)
        self._events.emit(events.BLOB_STORED, digest=blob.digest, size=blob.size)
        return blob

    def publish_revision(self, manifest: manifests.Manifest) -> Path:
        """Atomically publish a revision's manifest. Emits RevisionPublished.

        The manifest is written to its final path in one step. If it already
        exists, RevisionExists is raised (revisions are immutable — never
        overwritten). This is the only call that makes new content visible to
        serving.
        """
        repo_id = manifest.repo_id
        commit = manifest.revision.commit
        dest = self._storage.manifest_path(repo_id, commit)
        if dest.is_file():
            raise RevisionExists(f"{repo_id}@{commit} already published")
        manifests.write_atomic(manifest, dest)
        self._events.emit(
            events.REVISION_PUBLISHED,
            repo_id=repo_id,
            commit=commit,
            file_count=len(manifest.files),
        )
        return dest

    def update_reference(self, repo_id: str, name: str, commit: str) -> dict[str, str]:
        """Point `name` at `commit` and persist. Emits ReferenceUpdated.

        This is the only mutation of live pointer state. It is safe to call
        before or after publish_revision; the convention is publish-then-ref.
        """
        updated = refs.set_ref(self._storage.refs_path(repo_id), name, commit)
        self._events.emit(
            events.REFERENCE_UPDATED, repo_id=repo_id, name=name, commit=commit
        )
        return updated

    def repository_created(self, repo_id: str) -> None:
        """Emit RepositoryCreated. Idempotent marker that a repo now exists."""
        self._events.emit(events.REPOSITORY_CREATED, repo_id=repo_id)

    def repository_exists(self, repo_id: str) -> bool:
        """True for any repo the store lists, including an empty one (created for an upload, no revision yet)."""
        d = self._storage.repo_dir(repo_id)
        return (d / "revisions").is_dir() or (d / "refs.json").is_file()

    def create_repository(self, repo_id: str) -> bool:
        """Create an EMPTY repository (no revision, no ref): what an upload client's `create_repo` asks for.

        False when it already exists. Holds the store lock shared, so it cannot interleave with a repository delete."""
        from .fsutil import fsync_dir
        with self.ingest_lock():
            if self.repository_exists(repo_id):
                return False
            rev_dir = self._storage.revisions_dir(repo_id)
            try:
                rev_dir.mkdir(parents=True, exist_ok=False)
            except FileExistsError:                      # another create won the race
                return False
            fsync_dir(rev_dir.parent)
            self.repository_created(repo_id)
            return True

    # ---- locks ---------------------------------------------------------

    def ingest_lock(self):
        """Held SHARED by an ingest from its first stored blob to its published manifest (see locks.py)."""
        from .locks import store_lock
        return store_lock(self._storage.root, exclusive=False, wait=True)

    def gc_lock(self):
        """Held EXCLUSIVE by GC; raises StoreBusy at once while an ingest runs."""
        from .locks import store_lock
        return store_lock(self._storage.root, exclusive=True, wait=False)

    # ---- maintenance ---------------------------------------------------

    def verify_blob(self, digest: HEX) -> bool:
        ok = self._storage.blobs.verify(digest)
        self._events.emit(
            events.BLOB_VERIFIED, digest=digest, ok=ok
        )
        if not ok:
            self._events.emit(events.INTEGRITY_FAILED, digest=digest)
        return ok

    def iter_blobs(self) -> Iterator[HEX]:
        """Yield every blob digest present in the store."""
        root = self._storage.blobs.root
        if not root.is_dir():
            return
        for shard in sorted(root.iterdir()):
            if not shard.is_dir():
                continue
            for blob in sorted(shard.iterdir()):
                if blob.is_file() and not blob.name.startswith("."):
                    yield blob.name

    def collect_reachable_blobs(self) -> set[HEX]:
        """Walk all manifests and collect the set of reachable blob digests.

        Used by GC to derive reachability without any stored refcounts.
        """
        reachable: set[HEX] = set()
        for repo_id in self.list_repos():
            for commit in self.list_revisions(repo_id):
                m = manifests.read(self._storage.manifest_path(repo_id, commit))
                for f in m.files:
                    try:
                        reachable.add(f.blob.split(":", 1)[1])
                    except (IndexError, ValueError):
                        continue
        return reachable

    def collect_reachable_blobs_with_refs(self) -> dict[HEX, list[tuple[str, str, str]]]:
        """Like collect_reachable_blobs but tracks which (repo, commit, path)
        references each digest. Used for GC reporting / dry-run output."""
        refs_map: dict[HEX, list[tuple[str, str, str]]] = {}
        for repo_id in self.list_repos():
            for commit in self.list_revisions(repo_id):
                m = manifests.read(self._storage.manifest_path(repo_id, commit))
                for f in m.files:
                    try:
                        digest = f.blob.split(":", 1)[1]
                    except (IndexError, ValueError):
                        continue
                    refs_map.setdefault(digest, []).append((repo_id, commit, f.path))
        return refs_map

    def repo_blob_usage(self, repo_id: str) -> dict:
        """What deleting this repo would free: bytes only this repo holds (exclusive) and bytes other repos also use (shared)."""
        mine: dict[HEX, int] = {}
        for commit in self.list_revisions(repo_id):
            for f in manifests.read(self._storage.manifest_path(repo_id, commit)).files:
                mine[f.blob.split(":", 1)[1]] = f.size
        others: set[HEX] = set()
        for other in self.list_repos():
            if other == repo_id:
                continue
            for commit in self.list_revisions(other):
                for f in manifests.read(self._storage.manifest_path(other, commit)).files:
                    others.add(f.blob.split(":", 1)[1])
        exclusive = {d: n for d, n in mine.items() if d not in others}
        return {"exclusive_bytes": sum(exclusive.values()), "shared_bytes": sum(n for d, n in mine.items() if d in others),
                "exclusive_digests": sorted(exclusive)}

    def delete_repository(self, repo_id: str) -> dict:
        """Remove a repository (every revision and ref) and free the blobs no other repo uses.

        Takes the store lock exclusively and does not wait: while an ingest runs it raises StoreBusy (the ingest's blobs
        must not be judged unreferenced). The repo directory is renamed away first, so a crash leaves either the whole repo
        or none of it visible; the blobs go last (an interrupted delete leaves orphans, which GC collects).
        """
        import shutil
        with self.gc_lock():
            repo_dir = self._storage.repo_dir(repo_id)
            if not repo_dir.is_dir():
                raise RepositoryNotFound(repo_id)
            usage = self.repo_blob_usage(repo_id)
            trash = repo_dir.with_name(repo_dir.name + ".deleting")
            repo_dir.rename(trash)
            shutil.rmtree(trash, ignore_errors=True)
            freed = deleted = 0
            for digest in usage["exclusive_digests"]:
                size = 0
                path = self._storage.blobs.path_for(digest)
                if path.is_file():
                    size = path.stat().st_size
                if self.delete_blob(digest):
                    freed += size
                    deleted += 1
            self._events.emit(events.REPOSITORY_DELETED, repo_id=repo_id, blobs=deleted, bytes=freed)
            return {"blobs_deleted": deleted, "freed_bytes": freed, "shared_bytes_kept": usage["shared_bytes"]}

    def delete_blob(self, digest: HEX) -> bool:
        """Remove an unreferenced blob. Emits GCDeletedBlob. Returns True if removed."""
        path = self._storage.blobs.path_for(digest)
        if not path.is_file():
            return False
        path.unlink()
        self._events.emit(events.GC_DELETED_BLOB, digest=digest)
        return True
