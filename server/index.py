"""Index backend abstraction.

The index is DERIVED state — a performance cache, never the source of truth.
The source of truth is blobs/ + manifests/ + refs. Deleting the index must never
lose data; it is always rebuildable by walking the repo directories (see
`rebuild`).

Two implementations:
  - SQLiteIndex: the default; gives cheap repo/revision/blob lookups and is the
    cache target for derived reference counts during GC.
  - NullIndex: does nothing. Useful on the most resource-constrained hosts
    (e.g. the DS713+), where the cost of maintaining an index isn't worth it
    and direct directory walks are fine.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Iterable, Protocol


class IndexBackend(Protocol):
    """A derived lookup cache. All methods are best-effort; callers must handle
    a miss (fall back to walking the store)."""

    def rebuild(self, repos_dir: Path) -> None: ...
    def list_repos(self) -> list[str]: ...
    def list_commits(self, repo_id: str) -> list[str]: ...
    def has_blob(self, digest: str) -> bool: ...
    def close(self) -> None: ...


class NullIndex:
    """A no-op index. Every query is a miss; callers walk the store."""

    def rebuild(self, repos_dir: Path) -> None:
        return None

    def list_repos(self) -> list[str]:
        return []

    def list_commits(self, repo_id: str) -> list[str]:
        return []

    def has_blob(self, digest: str) -> bool:
        return False

    def close(self) -> None:
        return None


class SQLiteIndex:
    """A rebuildable SQLite cache of (repo_id, commit, blob digest) reachability.

    Single-writer-safe via a lock; reads are concurrent. This is a cache, so we
    favour simplicity over concurrency throughput.
    """

    def __init__(self, db_path: Path):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS repos (
                repo_id TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS commits (
                repo_id TEXT NOT NULL,
                commit_sha TEXT NOT NULL,
                PRIMARY KEY (repo_id, commit_sha)
            );
            CREATE TABLE IF NOT EXISTS blobs (
                digest TEXT PRIMARY KEY
            );
            """
        )
        self._conn.commit()

    def rebuild(self, repos_dir: Path) -> None:
        """Rebuild the index from scratch by walking `repos_dir`.

        Reads every manifest to collect blob digests. Idempotent.
        """
        # Local import to avoid a hard dependency cycle at module import time.
        from . import manifests, refs

        with self._lock:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM repos")
            cur.execute("DELETE FROM commits")
            cur.execute("DELETE FROM blobs")
            digests: set[str] = set()

            if repos_dir.is_dir():
                for org_path in sorted(repos_dir.iterdir()):
                    if not org_path.is_dir():
                        continue
                    for repo_path in sorted(org_path.iterdir()):
                        if not repo_path.is_dir():
                            continue
                        repo_id = f"{org_path.name}/{repo_path.name}"
                        revisions_dir = repo_path / "revisions"
                        has_any = False
                        if revisions_dir.is_dir():
                            for commit_dir in sorted(revisions_dir.iterdir()):
                                if not commit_dir.is_dir():
                                    continue
                                manifest_file = commit_dir / "manifest.json"
                                if not manifest_file.is_file():
                                    continue
                                try:
                                    m = manifests.read(manifest_file)
                                except manifests.ManifestError:
                                    continue
                                cur.execute(
                                    "INSERT OR IGNORE INTO commits(repo_id, commit_sha) VALUES (?, ?)",
                                    (repo_id, commit_dir.name),
                                )
                                for f in m.files:
                                    try:
                                        digests.add(f.blob.split(":", 1)[1])
                                    except (IndexError, ValueError):
                                        continue
                                has_any = True
                        if has_any:
                            cur.execute(
                                "INSERT OR IGNORE INTO repos(repo_id) VALUES (?)",
                                (repo_id,),
                            )
            cur.executemany(
                "INSERT OR IGNORE INTO blobs(digest) VALUES (?)",
                [(d,) for d in sorted(digests)],
            )
            self._conn.commit()

    def list_repos(self) -> list[str]:
        with self._lock:
            cur = self._conn.execute("SELECT repo_id FROM repos ORDER BY repo_id")
            return [row[0] for row in cur.fetchall()]

    def list_commits(self, repo_id: str) -> list[str]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT commit_sha FROM commits WHERE repo_id = ? ORDER BY commit_sha",
                (repo_id,),
            )
            return [row[0] for row in cur.fetchall()]

    def has_blob(self, digest: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM blobs WHERE digest = ?", (digest,)
            )
            return cur.fetchone() is not None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def open_index(kind: str, index_dir: Path) -> IndexBackend:
    """Factory: 'sqlite' or 'none'."""
    kind = (kind or "sqlite").strip().lower()
    if kind == "none":
        return NullIndex()
    if kind == "sqlite":
        return SQLiteIndex(index_dir / "index.sqlite")
    raise ValueError(f"unknown index backend: {kind!r}")
