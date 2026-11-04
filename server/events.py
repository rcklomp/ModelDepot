"""Append-only event log.

Every state transition emits an event as one JSON line to `events.jsonl`. The
log is purely derived (append-only) — it is never the source of truth, but it is
invaluable for debugging and post-mortems: you can reconstruct exactly what
happened and when.

The log is append-only and line-oriented, so concurrent appenders are safe on a
POSIX filesystem (writes smaller than PIPE_BUF are atomic) and rsync can ship
new lines incrementally. Future: swap to SQLite or syslog without changing the
event vocabulary.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_event_id() -> str:
    return uuid.uuid4().hex


class EventLog:
    """Thread-safe append-only JSONL event log."""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.Lock()

    def emit(self, event_type: str, **payload: Any) -> str:
        """Append one event line. Returns the event id."""
        event = {
            "id": _new_event_id(),
            "type": event_type,
            "ts": _utcnow(),
            "payload": payload,
        }
        line = json.dumps(event, sort_keys=True, ensure_ascii=False)
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # 'a' mode is atomic for short writes under PIPE_BUF on POSIX.
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return event["id"]

    def iter_events(self):
        """Yield parsed events oldest-first. Tolerates a trailing partial line."""
        if not self._path.is_file():
            return
        with open(self._path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    # Skip a corrupt/partial trailing line rather than crash.
                    continue


# Canonical event type names (see docs/DESIGN.md). Emitting code uses these
# constants so typos surface at import time.
REPOSITORY_CREATED = "RepositoryCreated"
REVISION_IMPORTED = "RevisionImported"
BLOB_STORED = "BlobStored"
BLOB_VERIFIED = "BlobVerified"
REVISION_PUBLISHED = "RevisionPublished"
REFERENCE_UPDATED = "ReferenceUpdated"
GC_DELETED_BLOB = "GCDeletedBlob"
INTEGRITY_FAILED = "IntegrityFailed"
INDEX_REBUILT = "IndexRebuilt"
REPOSITORY_DELETED = "RepositoryDeleted"
UPLOAD_COMMITTED = "UploadCommitted"          # a revision published from an upload (carries the commit summary: manifests have no field for it)
