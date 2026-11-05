"""Transactional ingest orchestrator.

Runs the crash-safe ingest pipeline that turns a staged snapshot into a
published, served revision:

    staging → verify sha256 → blobstore → manifest → atomic publish → ref update

Each step emits events. No half-visible repo can ever appear in serving: a
revision becomes visible only at the atomic publish (manifest rename), and the
live 'main' pointer moves only at the ref update. An interrupted ingest leaves
only staging/ dirt and possibly orphan blobs (collected later by GC), never a
partially-visible revision.

This module is source-agnostic: it takes a StagedSnapshot (produced by
hf_source.fetch or a future civitai_source.fetch) and does not import
huggingface_hub.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from server import events
from server.manifests import (
    FileEntry, License, Manifest, RevisionInfo, Source, SCHEMA_VERSION,
)
from server.repository import RepositoryCore, RevisionConflict

from .hf_source import StagedFile, StagedSnapshot


@dataclass
class IngestResult:
    repo_id: str
    commit: str
    file_count: int
    published: bool       # True if this created the revision; False if it already existed
    ref_updated: bool
    added: int = 0        # files this ingest ADDED to a revision that already existed (the same commit pulled in parts)


class IngestError(Exception):
    pass


class IntegrityMismatch(IngestError):
    """A staged file does not hash to its expected upstream sha256 (LFS files)."""
    def __init__(self, path: str, expected: str, actual: str):
        super().__init__(
            f"integrity mismatch for {path}: expected sha256 {expected}, got {actual}"
        )
        self.path = path
        self.expected = expected
        self.actual = actual


class IncompleteShards(IngestError):
    """A sharded model is being published with some shards missing.

    Engines (llama.cpp, etc.) discover sibling shards by filtering the file list
    at load time; a partial shard set causes a late, confusing failure inside
    the engine rather than at ingest time. We refuse by default and require an
    explicit opt-in to publish a partial shard set.
    """
    def __init__(self, prefix: str, have: int, declared: int, missing: list[str]):
        super().__init__(
            f"incomplete shard set for '{prefix}': have {have}/{declared} shards. "
            f"Missing: {', '.join(missing[:5])}"
            + (" ..." if len(missing) > 5 else "")
            + "\n  Re-run with the full quant's files (e.g. --allow '<quant>/*'), "
            "or pass --allow-incomplete-shards to publish anyway."
        )
        self.prefix = prefix
        self.have = have
        self.declared = declared
        self.missing = missing


def ingest(repo: RepositoryCore, snapshot: StagedSnapshot, *,
           source_kind: str = "huggingface", source_ref: str | None = None,
           ref_name: str = "main", license_name: str | None = None,
           update_ref: bool = True,
           allow_incomplete_shards: bool = False,
           progress=None) -> IngestResult:
    """Publish a staged snapshot as a new immutable revision.

    The same commit may be pulled in several parts (first README.md, later one weights file): the files that are new are added
    to the existing revision (RepositoryCore.publish_or_extend); a path whose content differs is refused (IngestError).

    Steps (each emits an event):
      1. For each staged file with a known upstream sha256 (LFS files), verify
         the local content hashes to it — reject on mismatch (IntegrityFailed).
      2. Content-address every file into the blob store (dedup).
      3. Assemble a versioned, sorted, namespaced manifest.
      4. Atomically publish the manifest; if the revision already exists, add the files it does not hold yet
         (nothing to add = no change, so re-ingest of the same commit is idempotent).
      5. Optionally move `ref_name` to the new commit.

    Shard completeness: if the snapshot contains a sharded model (filenames
    matching '<prefix>-NNNNN-of-NNNNN') with some shards missing, this raises
    IncompleteShards — a partial shard set is silently broken at load time, so
    we refuse it unless allow_incomplete_shards=True.

    Returns an IngestResult. Raises IntegrityMismatch / IncompleteShards / IngestError.
    """
    # Hold the store lock (shared) from the first stored blob to the published manifest: a GC in between would see our
    # blobs as unreferenced and delete them (server/locks.py). The download itself happens BEFORE this call, outside the lock.
    with repo.ingest_lock():
        return _ingest_locked(repo, snapshot, source_kind=source_kind, source_ref=source_ref, ref_name=ref_name,
                              license_name=license_name, update_ref=update_ref,
                              allow_incomplete_shards=allow_incomplete_shards, progress=progress)


def _ingest_locked(repo: RepositoryCore, snapshot: StagedSnapshot, *, source_kind: str, source_ref: str | None,
                   ref_name: str, license_name: str | None, update_ref: bool,
                   allow_incomplete_shards: bool, progress=None) -> IngestResult:
    repo_id = snapshot.repo_id
    commit = snapshot.commit
    repo.repository_created(repo_id)

    # Refuse a partial shard set unless explicitly overridden. Shards the library already holds for this commit count.
    if not allow_incomplete_shards:
        _check_shard_completeness(snapshot.files, already_have=repo.revision_paths(repo_id, commit))

    file_entries: list[FileEntry] = []
    for i, staged in enumerate(snapshot.files, 1):
        blob_ref, digest = _store_one(repo, staged)
        file_entries.append(_to_file_entry(staged, blob_ref, digest, commit))
        if progress:
            progress(i, len(snapshot.files), staged.path)

    manifest = Manifest(
        schema_version=SCHEMA_VERSION,
        repo_id=repo_id,
        source=Source(kind=source_kind, ref=source_ref or _upstream_url(repo_id)),
        revision=RevisionInfo(
            commit=commit,
            fetched_at=_utcnow_iso(),
            license=License(name=license_name) if license_name else None,
        ),
        files=file_entries,
    )

    try:
        published, added = repo.publish_or_extend(manifest)
    except RevisionConflict as exc:
        raise IngestError(str(exc)) from exc

    ref_updated = False
    if update_ref:
        repo.update_reference(repo_id, ref_name, commit)
        ref_updated = True

    return IngestResult(
        repo_id=repo_id, commit=commit,
        file_count=len(file_entries), published=published, ref_updated=ref_updated, added=len(added),
    )


def _upstream_url(core_id: str) -> str:
    from server.storage import public_repo_id, repo_type_of
    public = public_repo_id(core_id)
    return "https://huggingface.co/" + ("datasets/" if repo_type_of(core_id) == "dataset" else "") + public


def _store_one(repo: RepositoryCore, staged: StagedFile) -> tuple[str, str]:
    """Verify (if possible) and content-address a single staged file.

    Returns (blob_ref, digest). Raises IntegrityMismatch if an LFS file's local
    content does not match its upstream sha256.

    If the staged file no longer exists (e.g. a previous ingest already moved it
    into the blob store), the blob is assumed already present and we look it up
    by the expected digest. This makes re-ingest of the same commit idempotent.
    """
    if staged.local_path.is_file():
        actual = _sha256_file(staged.local_path)       # the ONE read of the file: put_blob trusts it (verified=True)
        # LFS files carry an authoritative upstream sha256 — verify it.
        if staged.sha256 and staged.sha256.lower() != actual.lower():
            repo._events.emit(
                events.INTEGRITY_FAILED,
                path=staged.path, expected=staged.sha256, actual=actual,
            )
            raise IntegrityMismatch(staged.path, staged.sha256, actual)
        blob = repo.put_blob(staged.local_path, expected_digest=actual, verified=True)
        return f"sha256:{blob.digest}", blob.digest

    # Staged file is gone — it must already be in the blob store (prior ingest).
    expected = staged.sha256
    if expected and repo._storage.blobs.exists(expected):
        return f"sha256:{expected}", expected

    raise IngestError(
        f"staged file vanished and blob sha256:{expected} is not present: {staged.path}"
    )


# Shard pattern matches the one llama.cpp uses (get_gguf_split_info):
#   <prefix>-NNNNN-of-NNNNN.<ext>
# e.g. 'model-00001-of-00004.gguf', 'DeepSeek-V4-...-00002-of-00004.gguf'.
_SHARD_RE = __import__("re").compile(r"^(.+)-([0-9]{5})-of-([0-9]{5})(\.[^.]+)?$")


def _check_shard_completeness(files: list[StagedFile], already_have=()) -> None:
    """Raise IncompleteShards if any sharded model among `files` is missing sibling shards.

    Groups shard files by their prefix and declared total, then verifies every
    index 1..total is present. Non-sharded files are ignored. This catches the
    common footgun of ingesting only some shards of a multi-shard model, which
    would otherwise succeed silently and fail confusingly at load time.
    `already_have`: paths the library already holds for the same commit. They count as present, but only the sets that
    `files` touches are judged, so an old gap elsewhere never blocks an unrelated file.
    """
    import os
    # Build a map: (prefix, total, ext) -> set of indices present.
    groups: dict[tuple[str, int, str], set[int]] = {}
    for f in files:
        m = _SHARD_RE.match(os.path.basename(f.path))
        if not m:
            continue
        prefix, index, total, ext = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4) or ""
        groups.setdefault((prefix, total, ext), set()).add(index)
    for path in already_have:
        m = _SHARD_RE.match(os.path.basename(path))
        if m:
            key = (m.group(1), int(m.group(3)), m.group(4) or "")
            if key in groups:
                groups[key].add(int(m.group(2)))

    for (prefix, total, ext), indices in groups.items():
        if total <= 1:
            continue  # not actually sharded
        have = sorted(indices)
        expected = set(range(1, total + 1))
        missing_idx = sorted(expected - indices)
        if not missing_idx:
            continue  # complete
        # Reconstruct the missing filenames for a helpful error.
        missing_names = [f"{prefix}-{i:05d}-of-{total:05d}{ext}" for i in missing_idx]
        raise IncompleteShards(
            prefix=f"{prefix}-...-of-{total:05d}{ext}",
            have=len(have), declared=total, missing=missing_names,
        )


def _to_file_entry(staged: StagedFile, blob_ref: str, digest: str,
                   commit: str) -> FileEntry:
    return FileEntry(
        path=staged.path,
        size=staged.size,
        blob=blob_ref,
        checksums={"sha256": digest},
        metadata={
            "hf": {
                "etag": staged.etag or f'"{digest[:32]}-1000"',
                "x_repo_commit": commit,
            }
        },
    )


def _sha256_file(path: Path, *, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _sha256_file_or_none(path: Path) -> str | None:
    """Hash a file if it exists, else None."""
    if not path.is_file():
        return None
    return _sha256_file(path)


def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
