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
from server.storage import FilesystemStorage


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
    p = argparse.ArgumentParser(prog="repo", description="LLM Repository ingester")
    sub = p.add_subparsers(dest="cmd", required=True)

    # ---- ingest hf ----
    hf = sub.add_parser("ingest-hf", help="ingest a Hugging Face repo")
    hf.add_argument("repo_id", help="e.g. 'tinyllama/TinyLlama-1.1B-Chat-v1.0'")
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
    hf.add_argument("--license", default=None, help="license name to record")
    hf.add_argument("--endpoint", default=os.environ.get("HF_ENDPOINT", ""),
                    help="HF endpoint (default: $HF_ENDPOINT or huggingface.co)")
    hf.set_defaults(func=_cmd_ingest_hf)

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
    gc.set_defaults(func=_cmd_gc)

    return p


# ---- commands --------------------------------------------------------

def _cmd_ingest_hf(args) -> int:
    from . import hf_source, transaction

    settings = load_settings()
    storage = FilesystemStorage(settings)
    storage.ensure_layout()
    event_log = events.EventLog(storage.events_path)
    repo = RepositoryCore(storage, event_log)

    # Each ingest gets its own staging txid so concurrent ingests don't collide.
    txid = uuid.uuid4().hex[:12]
    staging = storage.staging_dir / f"hf-{txid}"
    staging.mkdir(parents=True, exist_ok=True)

    token = args.token or None
    endpoint = args.endpoint or None
    try:
        print(f"resolving and downloading {args.repo_id} @ {args.rev} ...")
        snapshot = hf_source.fetch(
            args.repo_id, staging, revision=args.rev, token=token,
            allow_patterns=args.allow, ignore_patterns=args.ignore,
            endpoint=endpoint,
        )
        print(f"  commit {snapshot.commit}, {len(snapshot.files)} file(s)")

        result = transaction.ingest(
            repo, snapshot, ref_name=args.ref, license_name=args.license,
            update_ref=not args.no_update_ref,
        )
        print(f"  {'published' if result.published else 'already present'} "
              f"revision {result.commit} ({result.file_count} files)")
        if result.ref_updated:
            print(f"  {args.ref} -> {result.commit}")
        print("done.")
    finally:
        # Clean up staging regardless of success/failure.
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
    settings = load_settings()
    storage = FilesystemStorage(settings)
    repo = RepositoryCore(storage, events.EventLog(storage.events_path))
    reachable = repo.collect_reachable_blobs()
    present = set(repo.iter_blobs())
    orphans = sorted(present - reachable)
    if not orphans:
        print("no unreferenced blobs")
        return 0
    for digest in orphans:
        if args.dry_run:
            print(f"  would delete sha256:{digest}")
        else:
            repo.delete_blob(digest)
            print(f"  deleted sha256:{digest}")
    print(f"{len(orphans)} unreferenced blob(s) "
          f"{'reported' if args.dry_run else 'deleted'}")
    return 0


def _rm_tree(path: Path) -> None:
    if not path.exists():
        return
    import shutil
    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
