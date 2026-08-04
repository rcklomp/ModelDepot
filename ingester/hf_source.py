"""Hugging Face source adapter for the ingester.

Wraps `huggingface_hub` (1.x) to:
  1. resolve a revision to a concrete commit hash,
  2. download a repo snapshot into a staging directory (flat files, no symlinks),
  3. capture per-file provenance (size + the etag value HF serves on /resolve).

This module knows about Hugging Face; the transaction layer (transaction.py)
does not. The etag captured here is exactly the value HF returns as
`X-Linked-Etag` on its resolve endpoint — for an LFS file that's the content
SHA-256, for a git-tracked file it's the git blob SHA-1. We store it as
namespaced `metadata.hf.etag` so the HF adapter can echo it back verbatim,
making the mirror indistinguishable from upstream HF to clients.

`snapshot_download` writes a `.cache/huggingface/` metadata folder at the root
of `local_dir`; we exclude that from the manifest.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path

# Import HF lazily inside functions so the rest of the ingester (and tests that
# stub it) don't require huggingface_hub at import time.


@dataclass(frozen=True)
class StagedFile:
    """A file that landed in staging, with its upstream HF provenance."""

    path: str            # repo-relative path (e.g. 'config.json')
    local_path: Path     # absolute path under the staging dir
    size: int
    etag: str            # the X-Linked-Etag value HF serves
    sha256: str | None   # content sha256 if known (LFS files); else None


@dataclass(frozen=True)
class StagedSnapshot:
    """The result of downloading a repo snapshot into staging."""

    repo_id: str
    revision_requested: str
    commit: str          # resolved 40-char commit SHA
    staging_dir: Path
    files: list[StagedFile]


# Patterns we never ingest even if upstream has them.
_DEFAULT_IGNORE = [
    ".cache/**",        # huggingface_hub's local metadata side-effect
    ".gitattributes",
]


class HfSourceError(Exception):
    pass


def fetch(repo_id: str, staging_dir: Path, *, revision: str = "main",
          token: str | None = None, allow_patterns: list[str] | None = None,
          ignore_patterns: list[str] | None = None,
          endpoint: str | None = None) -> StagedSnapshot:
    """Download an HF repo snapshot into `staging_dir` and capture provenance.

    Returns a StagedSnapshot describing what landed. Raises HfSourceError with a
    helpful message on any HF failure (gated, missing, bad revision, network).
    """
    from huggingface_hub import HfApi, snapshot_download
    from huggingface_hub.errors import (
        BadRequestError, EntryNotFoundError, GatedRepoError,
        HfHubHTTPError, LocalEntryNotFoundError, RepositoryNotFoundError,
        RevisionNotFoundError, RevisionResolutionError,
    )

    api = HfApi(endpoint=endpoint)

    # 1. Pin the revision to a concrete commit hash.
    try:
        resolved = api.resolve_revision(repo_id, revision=revision, token=token)
    except RevisionResolutionError as exc:
        raise HfSourceError(f"could not resolve revision {revision!r}: {exc}") from exc
    except (GatedRepoError, RepositoryNotFoundError) as exc:
        raise _unavailable(repo_id, exc) from exc
    except (HfHubHTTPError, BadRequestError) as exc:
        raise HfSourceError(f"error resolving {repo_id}: {exc}") from exc
    commit = str(resolved.resolved)

    # 2. Download into staging (flat files, no symlinks).
    ignore = list(_DEFAULT_IGNORE)
    if ignore_patterns:
        ignore.extend(ignore_patterns)

    try:
        snapshot_download(
            repo_id,
            revision=resolved,
            local_dir=str(staging_dir),
            token=token,
            allow_patterns=allow_patterns,
            ignore_patterns=ignore or None,
            endpoint=endpoint,
        )
    except (GatedRepoError, RepositoryNotFoundError) as exc:
        raise _unavailable(repo_id, exc) from exc
    except RevisionNotFoundError as exc:
        raise HfSourceError(
            f"revision {revision!r} not found in {repo_id}: {exc}"
        ) from exc
    except (EntryNotFoundError, LocalEntryNotFoundError) as exc:
        raise HfSourceError(f"could not fetch {repo_id}: {exc}") from exc
    except HfHubHTTPError as exc:
        raise HfSourceError(f"network error fetching {repo_id}: {exc}") from exc

    # 3. Capture per-file provenance from the repo tree at the resolved commit.
    try:
        tree_files = list(
            api.list_repo_tree(repo_id, recursive=True, revision=resolved,
                               token=token)
        )
    except HfHubHTTPError as exc:
        raise HfSourceError(f"could not list files for {repo_id}: {exc}") from exc

    # Build a provenance map keyed by repo-relative path.
    prov: dict[str, tuple[int, str, str | None]] = {}
    for f in tree_files:
        path = getattr(f, "path", None)
        if path is None:
            continue
        lfs = getattr(f, "lfs", None)
        if lfs is not None:
            etag = getattr(lfs, "sha256", None) or ""
            sha256 = getattr(lfs, "sha256", None)
        else:
            etag = getattr(f, "blob_id", "") or ""
            sha256 = None
        size = getattr(f, "size", None) or 0
        prov[path] = (size, etag, sha256)

    # 4. Walk the staging dir and pair each real file with its provenance.
    staged: list[StagedFile] = []
    ignore_set = list(_DEFAULT_IGNORE) + (ignore_patterns or [])
    for local_path in sorted(_walk_files(staging_dir)):
        rel = local_path.relative_to(staging_dir).as_posix()
        if _matches_any(rel, ignore_set):
            continue
        size, etag, sha256 = prov.get(rel, (local_path.stat().st_size, "", None))
        staged.append(StagedFile(
            path=rel, local_path=local_path, size=size, etag=etag, sha256=sha256,
        ))

    return StagedSnapshot(
        repo_id=repo_id, revision_requested=revision, commit=commit,
        staging_dir=staging_dir, files=staged,
    )


def _unavailable(repo_id: str, exc: Exception) -> HfSourceError:
    name = type(exc).__name__
    if "Gated" in name:
        return HfSourceError(
            f"{repo_id} is gated — pass an HF token with access (got {name})"
        )
    return HfSourceError(
        f"{repo_id} not found or not accessible ({name})"
    )


def _walk_files(root: Path):
    for p in sorted(root.rglob("*")):
        if p.is_file():
            yield p


def _matches_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pat) for pat in patterns)
