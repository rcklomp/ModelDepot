"""Background jobs for the repository.

Collapsed into one module per the v4 design — split into separate files only
when a job grows past a few hundred lines. Each job is a pure function over a
RepositoryCore that returns a Report; scheduling (cron, DSM Task Scheduler) is
the caller's concern. All jobs emit events as they work.

Jobs:
  - integrity: re-hash every reachable blob, report any mismatch.
  - gc:        delete unreferenced blobs (reachability derived from manifests).
  - index:     rebuild the derived index from the store.
  - replication: (placeholder) report what would rsync to a target.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from server import events
from server.index import IndexBackend
from server.locks import StoreBusy
from server.repository import RepositoryCore


@dataclass
class Report:
    job: str
    ok: bool
    checked: int = 0
    acted: int = 0
    problems: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        status = "OK" if self.ok else "PROBLEMS"
        line = f"[{self.job}] {status}: checked={self.checked} acted={self.acted}"
        if self.problems:
            line += f" problems={len(self.problems)}"
        return line


# ---- integrity --------------------------------------------------------

def integrity(repo: RepositoryCore) -> Report:
    """Re-hash every reachable blob; report mismatches. Emits BLOB_VERIFIED /
    INTEGRITY_FAILED per blob."""
    reachable = repo.collect_reachable_blobs_with_refs()
    report = Report(job="integrity", ok=True, checked=len(reachable))
    for digest, refs in reachable.items():
        ok = repo.verify_blob(digest)
        report.checked += 1
        if not ok:
            report.ok = False
            where = ",".join(f"{r[0]}@{r[1]}:{r[2]}" for r in refs[:3])
            report.problems.append(f"sha256:{digest} corrupted (ref: {where})")
    return report


# ---- garbage collection ----------------------------------------------

GC_MIN_AGE_SECONDS = 3600


def gc(repo: RepositoryCore, *, dry_run: bool = False, min_age_seconds: int = GC_MIN_AGE_SECONDS) -> Report:
    """Delete unreferenced blobs. Reachability is derived from manifests (no stored refcounts). Emits GCDeletedBlob per
    deleted blob.

    Safe against a download in progress: it takes the store lock exclusively and does not wait (StoreBusy while an ingest
    runs), and it never touches a blob younger than `min_age_seconds` (belt and braces for the moment between two locks).
    """
    import time

    with repo.gc_lock():
        reachable = repo.collect_reachable_blobs()
        present = set(repo.iter_blobs())
        orphans = sorted(present - reachable)
        report = Report(job="gc", ok=True, checked=len(present))
        now = time.time()
        for digest in orphans:
            try:
                age = now - repo._storage.blobs.path_for(digest).stat().st_mtime
            except OSError:
                continue
            if age < min_age_seconds:
                continue
            if dry_run:
                report.problems.append(f"would delete sha256:{digest}")
            else:
                repo.delete_blob(digest)
            report.acted += 1
        return report


# ---- index rebuild ----------------------------------------------------

def index_rebuild(repo: RepositoryCore, index: IndexBackend) -> Report:
    """Rebuild the derived index from the store. Emits IndexRebuilt."""
    repos_dir = repo._storage.repos_dir
    index.rebuild(repos_dir)
    repo._events.emit(events.INDEX_REBUILT, repos=str(repos_dir))
    n = len(index.list_repos())
    return Report(job="index", ok=True, acted=n)


# ---- replication (placeholder) ---------------------------------------

def replication_report(repo: RepositoryCore, target_desc: str = "") -> Report:
    """Report what a replication (rsync) job would push. The actual transfer is
    done by scripts/rsync-to-target.sh; this job just summarizes the store
    size for monitoring/scheduling."""
    blobs = list(repo.iter_blobs())
    total = sum(repo._storage.blobs.size(d) for d in blobs if repo._storage.blobs.exists(d))
    report = Report(job="replication", ok=True, checked=len(blobs))
    report.problems.append(f"target={target_desc or 'unspecified'} "
                           f"blobs={len(blobs)} bytes={total}")
    return report


# ---- runner -----------------------------------------------------------

def run_all(repo: RepositoryCore, index: IndexBackend | None = None) -> list[Report]:
    """Run integrity + gc (+ index rebuild if an index is given). Returns the
    reports. Suitable for a nightly cron/Task Scheduler job."""
    reports = [integrity(repo)]
    try:
        reports.append(gc(repo))
    except StoreBusy as exc:
        reports.append(Report(job="gc", ok=True, problems=[f"skipped: {exc}"]))
    if index is not None:
        reports.append(index_rebuild(repo, index))
    return reports
