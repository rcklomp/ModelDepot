"""Command-line entrypoint for the ingester.

Usage:
    repo ingest hf <repo_id> [--rev main] [--token ...] [--allow ...] [--ignore ...]
    repo ingest hf <repo_id> ... --no-update-ref        # publish without moving main
    repo list
    repo refs <repo_id>
    repo verify <repo_id> [--rev main]
    repo gc [--dry-run]

The CLI wires the repository core (server side) to a source adapter (HF for
now). It reads MIRROR_ROOT from the environment (same as the server), so by
default it writes into the store the server serves.
"""
from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

from server import events
from server.config import Settings, load_settings
from server.repository import RepositoryCore
from server.storage import FilesystemStorage, core_repo_id


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # surface a clean message, not a traceback
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="repo", description="ModelDepot ingester")
    sub = p.add_subparsers(dest="cmd", required=True)

    # ---- ingest hf ----
    hf = sub.add_parser("ingest-hf", help="ingest a Hugging Face repo")
    hf.add_argument("repo_id", help="e.g. 'tinyllama/TinyLlama-1.1B-Chat-v1.0'")
    hf.add_argument("--type", dest="repo_type", choices=["model", "dataset"], default="model",
                    help="what the repository is (default: model). A dataset is stored as 'datasets/<org>/<name>'")
    hf.add_argument("--rev", default="main", help="revision (branch/tag/commit)")
    hf.add_argument("--token", default=os.environ.get("HF_TOKEN", ""),
                    help="HF token (default: $HF_TOKEN)")
    hf.add_argument("--allow", action="append", default=None,
                    help="glob pattern to include (repeatable)")
    hf.add_argument("--ignore", action="append", default=None,
                    help="glob pattern to exclude (repeatable)")
    hf.add_argument("--ref", default="main",
                    help="local reference name to point at the new commit")
    hf.add_argument("--no-update-ref", action="store_true",
                    help="publish the revision without moving the ref")
    hf.add_argument("--allow-incomplete-shards", action="store_true",
                    help="publish even if a sharded model is missing some shards "
                         "(normally refused — partial shards fail at load time)")
    hf.add_argument("--license", default=None, help="license name to record")
    hf.add_argument("--endpoint", default=os.environ.get("HF_ENDPOINT", ""),
                    help="HF endpoint (default: $HF_ENDPOINT or huggingface.co)")
    hf.add_argument("--retries", type=int, default=6,
                    help="max retries on transient/network errors (default 6). "
                         "Each retry resumes per-file from where it left off.")
    hf.add_argument("--progress-log", default=None,
                    help="path to append structured progress lines to (tail-able). "
                         "Default: <store>/log/ingest-<repo>.log")
    hf.set_defaults(func=_cmd_ingest_hf)

    # ---- ingest civitai ----
    cv = sub.add_parser("ingest-civitai",
                        help="ingest a Civitai model version")
    cv.add_argument("version_id", type=int,
                    help="Civitai model version id (the numeric version, not model id)")
    cv.add_argument("--token", default=os.environ.get("CIVITAI_API_KEY", ""),
                    help="Civitai API token (default: $CIVITAI_API_KEY)")
    cv.add_argument("--type", default=None,
                    help="only download files of this type (e.g. 'Model', 'VAE')")
    cv.add_argument("--format", default=None,
                    help="only download files of this format (e.g. 'SafeTensor')")
    cv.add_argument("--ref", default="main",
                    help="local reference name to point at the new commit")
    cv.add_argument("--no-update-ref", action="store_true",
                    help="publish without moving the ref")
    cv.add_argument("--allow-incomplete-shards", action="store_true",
                    help="publish even if a sharded model is missing some shards")
    cv.set_defaults(func=_cmd_ingest_civitai)

    # ---- list ----
    ls = sub.add_parser("list", help="list repositories")
    ls.set_defaults(func=_cmd_list)

    # ---- refs ----
    rf = sub.add_parser("refs", help="show references for a repo")
    rf.add_argument("repo_id")
    rf.set_defaults(func=_cmd_refs)

    # ---- verify ----
    vf = sub.add_parser("verify", help="verify blob integrity for a revision")
    vf.add_argument("repo_id")
    vf.add_argument("--rev", default="main")
    vf.set_defaults(func=_cmd_verify)

    # ---- gc ----
    gc = sub.add_parser("gc", help="delete unreferenced blobs")
    gc.add_argument("--dry-run", action="store_true", help="report only, don't delete")
    gc.add_argument("--min-age", type=int, default=3600, metavar="SECONDS",
                    help="never delete a blob younger than this (default 3600)")
    gc.set_defaults(func=_cmd_gc)

    # ---- jobs ----
    jb = sub.add_parser("jobs", help="run background maintenance jobs")
    jb.add_argument("which", choices=["all", "integrity", "gc", "index"],
                    help="which job to run")
    jb.add_argument("--dry-run", action="store_true",
                    help="for gc: report only, don't delete")
    jb.set_defaults(func=_cmd_jobs)

    # ---- ingest local ----
    lc = sub.add_parser("ingest-local",
                        help="ingest a local directory of model files (GGUF, safetensors, etc.)")
    lc.add_argument("repo_id",
                    help="repository id to publish as (e.g. 'Huihui/Qwen3.6-35B-GGUF')")
    lc.add_argument("--type", dest="repo_type", choices=["model", "dataset"], default="model",
                    help="what the repository is (default: model)")
    lc.add_argument("path", help="local directory containing the model files")
    lc.add_argument("--ref", default="main",
                    help="local reference name to point at the new commit")
    lc.add_argument("--no-update-ref", action="store_true",
                    help="publish without moving the ref")
    lc.add_argument("--allow-incomplete-shards", action="store_true",
                    help="publish even if a sharded model is missing some shards")
    lc.add_argument("--license", default=None, help="license name to record")
    lc.add_argument("--include", action="append", default=None,
                    help="glob pattern to include (repeatable). Default: all files.")
    lc.add_argument("--exclude", action="append", default=None,
                    help="glob pattern to exclude (repeatable). "
                         "Default: common junk files (.DS_Store, .cache/, etc.)")
    lc.add_argument("--move", action="store_true",
                    help="MOVE the files into the store instead of copying them: fast and needs no extra disk space, "
                         "but the source folder is emptied. Default: copy, the source is left untouched.")
    lc.set_defaults(func=_cmd_ingest_local)

    return p


# ---- commands --------------------------------------------------------

def _cmd_ingest_hf(args) -> int:
    from . import hf_source, transaction

    settings = load_settings()
    storage = FilesystemStorage(settings)
    storage.ensure_layout()
    event_log = events.EventLog(storage.events_path)
    repo = RepositoryCore(storage, event_log)

    # Deterministic staging dir per repo_id, so an interrupted ingest RESUMES
    # on re-run (HF's resume cache lives under staging/.cache/huggingface/ and
    # must be at the same path to be reused). A lockfile prevents two concurrent
    # ingests of the same repo from corrupting each other.
    staging = storage.staging_dir / f"hf-{_slug(core_repo_id(args.repo_type, args.repo_id))}"
    staging.mkdir(parents=True, exist_ok=True)

    token = args.token or None
    endpoint = args.endpoint or None
    # Default progress log: <store>/log/ingest-<repo>.log (tail-able).
    progress_log = Path(args.progress_log) if args.progress_log else (
        settings.log_dir / f"ingest-{_slug(core_repo_id(args.repo_type, args.repo_id))}.log")
    try:
        print(f"resolving and downloading {args.repo_id} @ {args.rev} ...")
        print(f"  (retries={args.retries}; resume-on-interrupt enabled; "
              f"log: {progress_log})")
        snapshot = hf_source.fetch(
            args.repo_id, staging, revision=args.rev, token=token, repo_type=args.repo_type,
            allow_patterns=args.allow, ignore_patterns=args.ignore,
            endpoint=endpoint, max_retries=args.retries,
            progress_log=progress_log,
        )
        print(f"  commit {snapshot.commit}, {len(snapshot.files)} file(s)")

        result = transaction.ingest(
            repo, snapshot, ref_name=args.ref, license_name=args.license,
            update_ref=not args.no_update_ref,
            allow_incomplete_shards=args.allow_incomplete_shards,
        )
        print(f"  {'published' if result.published else 'already present'} "
              f"revision {result.commit} ({result.file_count} files)")
        if result.ref_updated:
            print(f"  {args.ref} -> {result.commit}")
        print("done.")
    finally:
        # Clean up staging regardless of success/failure. The HF cache under
        # staging/.cache/huggingface/ is what powers resume; removing staging
        # between runs would lose it. So: on a FAILED ingest we deliberately
        # keep staging so the next attempt resumes. On success we remove it.
        import sys as _sys
        if _sys.exc_info()[0] is None:
            _rm_tree(staging)
        else:
            print(f"  (staging kept at {staging} — re-run to resume)",
                  file=_sys.stderr)
    return 0


def _cmd_ingest_local(args) -> int:
    """Ingest a local directory of model files into the mirror store.

    Unlike ingest-hf (which downloads from HuggingFace), this works with files
    already on disk — GGUF exports from LM Studio, manually downloaded models,
    safetensors, etc. Files are content-addressed (sha256) into the blob store
    just like HF ingests; the only difference is the source is a local path
    instead of an HTTP download. The source is COPIED (it may be the only copy of
    the model, or another program's library); `--move` consumes it instead.
    """
    import fnmatch
    import uuid
    from .hf_source import StagedFile, StagedSnapshot
    from . import transaction

    settings = load_settings()
    storage = FilesystemStorage(settings)
    storage.ensure_layout()
    event_log = events.EventLog(storage.events_path)
    repo = RepositoryCore(storage, event_log)

    src = Path(args.path).resolve()
    if not src.is_dir():
        print(f"error: {src} is not a directory", file=sys.stderr)
        return 1

    # Default exclusions: junk files we never want in the store.
    default_exclude = [".DS_Store", ".cache", ".cache/**", "*.tmp",
                       "__pycache__", "__pycache__/**"]
    excludes = args.exclude or default_exclude
    includes = args.include or ["*"]

    # Collect matching files (relative paths, sorted for determinism).
    # Note: fnmatch treats ** the same as *, so '*' matches everything
    # (both flat and nested relative paths). For targeted includes, users
    # can pass e.g. --include '*.gguf' (matches at any depth via fnmatch).
    all_files = []
    for f in sorted(src.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(src).as_posix()
        basename = f.name
        # Apply includes — match against both the full relative path and basename
        # so '*.gguf' works for files at any depth.
        if not any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(basename, pat)
                   for pat in includes):
            continue
        # Apply excludes — match against full path, basename, and any parent dir
        # (relative to src, so a source under ~/.cache doesn't self-exclude)
        rel_parts = Path(rel).parts
        if any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(basename, pat)
               or any(fnmatch.fnmatch(part, pat.rstrip("/*")) for part in rel_parts)
               for pat in excludes):
            continue
        all_files.append((rel, f))

    if not all_files:
        print(f"error: no files found in {src} matching the criteria", file=sys.stderr)
        return 1

    total_size = sum(f.stat().st_size for _, f in all_files)
    print(f"ingesting {len(all_files)} file(s) ({total_size / 1e9:.2f} GB) "
          f"from {src}")
    print(f"  repo_id: {core_repo_id(args.repo_type, args.repo_id)}")
    for rel, f in all_files:
        print(f"    {f.stat().st_size / 1e6:8.1f} MB  {rel}")

    # Build a StagedSnapshot. The synthetic commit is deterministic from the file list, sizes and modification times,
    # so re-ingesting the same files is idempotent and a CHANGED file becomes a new revision.
    import hashlib
    import shutil
    stats = [(rel, f.stat()) for rel, f in all_files]
    commit_input = "|".join(f"{rel}:{st.st_size}:{st.st_mtime_ns}" for rel, st in stats)
    commit = hashlib.sha256(commit_input.encode()).hexdigest()[:40]

    # Default: copy into a staging folder inside the store (the blob store then MOVES the copies into place).
    # The source must survive: pointing the blob store straight at it emptied the user's folder.
    staging = None
    if args.move:
        sources = {rel: f for rel, f in all_files}
    else:
        staging = storage.staging_dir / f"local-{uuid.uuid4().hex[:12]}"
        free = shutil.disk_usage(storage.staging_dir).free
        if total_size + (64 << 20) > free:
            print(f"error: copying {total_size / 1e9:.2f} GB needs that much free space in the store's volume "
                  f"({free / 1e9:.2f} GB free); use --move to move the files instead of copying them", file=sys.stderr)
            return 1
        staging.mkdir(parents=True)
        sources = {}
        print("  copying into the store's staging folder (use --move to skip the copy) ...")
        try:
            for rel, f in all_files:
                dest = staging / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(f, dest)
                sources[rel] = dest
        except BaseException:
            _rm_tree(staging)
            raise

    # No etag here: the transaction then records a content-derived one, unique per file. A shared etag (this used to
    # write `local-<commit>` for EVERY file) makes huggingface_hub store all files of the repo under one cache blob.
    staged_files = []
    for rel, st in stats:
        staged_files.append(StagedFile(
            path=rel,
            local_path=sources[rel],
            size=st.st_size,
            etag="",
            sha256=None,                    # let transaction compute it
        ))

    snapshot = StagedSnapshot(
        repo_id=core_repo_id(args.repo_type, args.repo_id),
        revision_requested="local",
        commit=commit,
        staging_dir=staging if staging is not None else src,
        files=staged_files,
    )

    try:
        result = transaction.ingest(
            repo, snapshot,
            source_kind="local",
            source_ref=f"file://{src}",
            ref_name=args.ref,
            license_name=args.license,
            update_ref=not args.no_update_ref,
            allow_incomplete_shards=args.allow_incomplete_shards,
        )
    finally:
        if staging is not None:
            _rm_tree(staging)
    print(f"\n  {'published' if result.published else 'already present'} "
          f"revision {result.commit} ({result.file_count} files)")
    if result.ref_updated:
        print(f"  {args.ref} -> {result.commit}")
    print(f"  serve at: {'/datasets' if args.repo_type == 'dataset' else ''}/{args.repo_id}/resolve/{args.ref}/<file>")
    print("done.")
    return 0


def _slug(s: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-")[:60] or "repo"


def _cmd_ingest_civitai(args) -> int:
    from . import civitai_source, transaction

    settings = load_settings()
    storage = FilesystemStorage(settings)
    storage.ensure_layout()
    event_log = events.EventLog(storage.events_path)
    repo = RepositoryCore(storage, event_log)

    # Deterministic per version_id, so an interrupted Civitai ingest resumes.
    staging = storage.staging_dir / f"civitai-{args.version_id}"
    staging.mkdir(parents=True, exist_ok=True)

    try:
        print(f"fetching Civitai version {args.version_id} ...")
        snap = civitai_source.fetch(
            args.version_id, staging, token=args.token,
            file_type=args.type, file_format=args.format,
        )
        print(f"  model: {snap.model_name} (id {snap.model_id})")
        print(f"  version: {snap.version_name} (id {snap.version_id}), "
              f"{len(snap.files)} file(s)")

        staged = civitai_source.to_staged_snapshot(snap)
        result = transaction.ingest(
            repo, staged, source_kind="civitai",
            source_ref=civitai_source.civitai_source_url(snap.model_id, snap.version_id),
            ref_name=args.ref, license_name=snap.license_name,
            update_ref=not args.no_update_ref,
            allow_incomplete_shards=args.allow_incomplete_shards,
        )
        print(f"  {'published' if result.published else 'already present'} "
              f"revision {result.commit} ({result.file_count} files)")
        if result.ref_updated:
            print(f"  {args.ref} -> {result.commit}")
        print(f"  serve at: /{staged.repo_id}/resolve/{args.ref}/<file>")
        print("done.")
    finally:
        _rm_tree(staging)
    return 0


def _cmd_list(args) -> int:
    settings = load_settings()
    storage = FilesystemStorage(settings)
    repo = RepositoryCore(storage, events.EventLog(storage.events_path))
    repos = repo.list_repos()
    if not repos:
        print("(no repositories)")
        return 0
    for r in repos:
        refs = repo.list_references(r)
        revs = repo.list_revisions(r)
        ref_str = ", ".join(f"{k}→{v[:7]}" for k, v in refs.items()) or "(no refs)"
        print(f"{r:50}  revs={len(revs):>2}  [{ref_str}]")
    return 0


def _cmd_refs(args) -> int:
    settings = load_settings()
    storage = FilesystemStorage(settings)
    repo = RepositoryCore(storage, events.EventLog(storage.events_path))
    refs = repo.list_references(args.repo_id)
    if not refs:
        print(f"(no refs for {args.repo_id})")
        return 0
    for name, commit in sorted(refs.items()):
        print(f"{name:20} {commit}")
    return 0


def _cmd_verify(args) -> int:
    settings = load_settings()
    storage = FilesystemStorage(settings)
    repo = RepositoryCore(storage, events.EventLog(storage.events_path))
    files = repo.list_files(args.repo_id, args.rev)
    bad = 0
    for f in files:
        digest = f.blob.split(":", 1)[1]
        ok = repo.verify_blob(digest)
        mark = "ok" if ok else "FAIL"
        print(f"  [{mark}] {f.path}")
        if not ok:
            bad += 1
    print(f"{len(files) - bad}/{len(files)} verified")
    return 0 if bad == 0 else 1


def _cmd_gc(args) -> int:
    from . import jobs
    from server.locks import StoreBusy

    settings = load_settings()
    storage = FilesystemStorage(settings)
    repo = RepositoryCore(storage, events.EventLog(storage.events_path))
    try:
        report = jobs.gc(repo, dry_run=args.dry_run, min_age_seconds=args.min_age)
    except StoreBusy as exc:
        print(f"gc not run: {exc}", file=sys.stderr)
        return 3
    for line in report.problems:
        print(f"  {line}")
    print(f"{report.acted} unreferenced blob(s) {'reported' if args.dry_run else 'deleted'} "
          f"({report.checked} blobs checked; blobs younger than {args.min_age}s are never touched)")
    return 0


def _cmd_jobs(args) -> int:
    from . import jobs
    from server.index import open_index

    settings = load_settings()
    storage = FilesystemStorage(settings)
    repo = RepositoryCore(storage, events.EventLog(storage.events_path))
    index = open_index(settings.index_backend, storage.index_dir)

    which = args.which
    if which == "all":
        reports = jobs.run_all(repo, index=index)
    elif which == "integrity":
        reports = [jobs.integrity(repo)]
    elif which == "gc":
        reports = [jobs.gc(repo, dry_run=args.dry_run)]
    elif which == "index":
        reports = [jobs.index_rebuild(repo, index)]
    else:
        print(f"unknown job: {which}", file=sys.stderr)
        return 2

    for r in reports:
        print(r)
        for p in r.problems:
            print(f"  {p}")
    return 0 if all(r.ok for r in reports) else 1


def _rm_tree(path: Path) -> None:
    if not path.exists():
        return
    import shutil
    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
