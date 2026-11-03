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
          endpoint: str | None = None,
          max_retries: int = 6,
          progress_log: Path | None = None) -> StagedSnapshot:
    """Download an HF repo snapshot into `staging_dir` and capture provenance.

    Robustness for large files / flaky links:
      - The download is wrapped in a retry loop. On a transient failure (network
        drop, timeout, 429, 5xx) it backs off and retries, up to `max_retries`.
      - `snapshot_download` resumes per-file from its internal cache, so each
        retry CONTINUES where it left off — completed shards are never
        re-downloaded. This is what makes a 50 GiB shard survivable on a bad
        link: just let it retry until it gets through.
      - Set `progress_log` to a file path to get structured, tail-able progress
        lines (one per attempt + completion).

    Returns a StagedSnapshot describing what landed. Raises HfSourceError with a
    helpful message on any fatal HF failure (gated, missing, bad revision) or
    after exhausting retries on a persistent transient failure.
    """
    from huggingface_hub import HfApi, snapshot_download
    from huggingface_hub.errors import (
        BadRequestError, EntryNotFoundError, GatedRepoError,
        HfHubHTTPError, LocalEntryNotFoundError, RepositoryNotFoundError,
        RevisionNotFoundError,
    )
    try:
        from huggingface_hub.errors import RevisionResolutionError
    except ImportError:  # older huggingface_hub releases lack this class
        class RevisionResolutionError(Exception):  # type: ignore[no-redef]
            pass

    api = HfApi(endpoint=endpoint)

    # 1. Pin the revision to a concrete commit hash.
    try:
        if hasattr(api, "resolve_revision"):
            resolved = api.resolve_revision(repo_id, revision=revision, token=token)
            commit = str(getattr(resolved, "resolved", None)
                         or getattr(resolved, "commit_sha", None) or resolved)
        else:  # older huggingface_hub (< 0.35) has no resolve_revision
            info = api.model_info(repo_id, revision=revision, token=token)
            commit = str(getattr(info, "sha", "") or "")
            if not commit:
                raise HfSourceError(f"no commit for {repo_id}@{revision}")
    except RevisionResolutionError as exc:
        raise HfSourceError(f"could not resolve revision {revision!r}: {exc}") from exc
    except (GatedRepoError, RepositoryNotFoundError) as exc:
        raise _unavailable(repo_id, exc) from exc
    except (HfHubHTTPError, BadRequestError) as exc:
        raise HfSourceError(f"error resolving {repo_id}: {exc}") from exc

    # 2. Download into staging (flat files, no symlinks), with retry+resume.
    ignore = list(_DEFAULT_IGNORE)
    if ignore_patterns:
        ignore.extend(ignore_patterns)

    _download_with_resume(
        snapshot_download,
        repo_id=repo_id,
        revision=commit,
        staging_dir=staging_dir,
        token=token,
        allow_patterns=allow_patterns,
        ignore_patterns=ignore or None,
        endpoint=endpoint,
        max_retries=max_retries,
        progress_log=progress_log,
    )

    # 3. Capture per-file provenance from the repo tree at the resolved commit.
    try:
        tree_files = list(
            api.list_repo_tree(repo_id, recursive=True, revision=commit,
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


# ---- robust download: retry + resume ----------------------------------
#
# snapshot_download itself does NOT retry the file-byte download (only the
# metadata HEAD does, internally). And hf_transfer is deprecated in 1.x (replaced
# by the Xet backend, which we deliberately disable). So the proven, correct
# approach for big files over a flaky link is a retry loop AROUND
# snapshot_download: it resumes per-file from its internal cache via HTTP Range,
# so each retry continues where it left off — completed shards are not
# re-fetched. We classify errors so a 404/gated/bad-revision fails fast (retrying
# cannot help) while a network drop / 429 / 5xx backs off and retries.

# Errors that are NOT worth retrying (the repo/revision/file is wrong or forbidden).
_FATAL_ERROR_NAMES = (
    "GatedRepoError", "RepositoryNotFoundError", "RevisionNotFoundError",
    "RevisionResolutionError", "EntryNotFoundError", "RemoteEntryNotFoundError",
    "HFValidationError", "BadRequestError",
)


def _is_retriable(exc: Exception) -> bool:
    """Should we retry, or is this terminal? Retried: network/timeout/429/5xx."""
    name = type(exc).__name__
    if name in _FATAL_ERROR_NAMES:
        return False
    # 404-shaped errors are fatal.
    if "NotFound" in name:
        return False
    # HfHubHTTPError: retry on 408/429/5xx, fail on 4xx otherwise.
    status = getattr(exc, "response", None)
    status = getattr(status, "status_code", None)
    if isinstance(status, int):
        if status in (408, 429) or 500 <= status < 600:
            return True
        if 400 <= status < 500:
            return False
    # httpx network/timeout errors -> retriable.
    if name in ("ConnectError", "ReadTimeout", "ConnectTimeout",
                "ReadError", "WriteError", "PoolTimeout", "RemoteProtocolError",
                "NetworkError", "ChunkedEncodingError", "ConnectionError",
                "TimeoutError", "OSError"):
        return True
    # LocalEntryNotFoundError can mean "offline + not fully cached" — retry,
    # because a transient outage could have set HF_HUB_OFFLINE or dropped cache.
    if name == "LocalEntryNotFoundError":
        return True
    # Default: don't retry unknown errors (safer than spinning on a hard failure).
    return False


def _log_progress(progress_log: Path | None, line: str) -> None:
    if progress_log is None:
        return
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    progress_log.parent.mkdir(parents=True, exist_ok=True)
    with open(progress_log, "a", encoding="utf-8") as f:
        f.write(f"{ts} {line}\n")


def _download_with_resume(snapshot_download_fn, *, repo_id, revision,
                          staging_dir, token, allow_patterns, ignore_patterns,
                          endpoint, max_retries: int,
                          progress_log: Path | None = None) -> None:
    """Call snapshot_download with retry+resume.

    Each retry resumes per-file (snapshot_download's internal cache + HTTP Range
    means a partially-downloaded shard continues rather than restarts). Fatal
    errors propagate immediately. Exhausting retries on a transient error raises
    HfSourceError.
    """
    import time

    attempt = 0
    while True:
        attempt += 1
        try:
            _log_progress(progress_log,
                          f"attempt={attempt} repo={repo_id} status=downloading")
            snapshot_download_fn(
                repo_id,
                revision=revision,
                local_dir=str(staging_dir),
                token=token,
                allow_patterns=allow_patterns,
                ignore_patterns=ignore_patterns,
                endpoint=endpoint,
            )
            _log_progress(progress_log,
                          f"attempt={attempt} repo={repo_id} status=complete")
            return
        except Exception as exc:
            if not _is_retriable(exc):
                # Fatal — surface with context, do not retry.
                from huggingface_hub.errors import HfHubHTTPError
                if isinstance(exc, HfHubHTTPError):
                    raise HfSourceError(
                        f"fatal error fetching {repo_id}: {exc}"
                    ) from exc
                raise HfSourceError(f"fatal error fetching {repo_id}: {exc}") from exc
            if attempt > max_retries:
                raise HfSourceError(
                    f"{repo_id}: gave up after {max_retries} retries on "
                    f"transient error ({type(exc).__name__}: {exc})"
                ) from exc
            # Exponential backoff with jitter: 2^attempt + small random, capped.
            backoff = min(2 ** attempt, 60)
            status = getattr(getattr(exc, "response", None), "status_code", None)
            reason = f"{type(exc).__name__}({status})" if status else type(exc).__name__
            _log_progress(
                progress_log,
                f"attempt={attempt} repo={repo_id} status=retry "
                f"reason={reason} wait={backoff}s "
                f"(will resume from HF cache)",
            )
            time.sleep(backoff)


def _walk_files(root: Path):
    for p in sorted(root.rglob("*")):
        if p.is_file():
            yield p


def _matches_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pat) for pat in patterns)
