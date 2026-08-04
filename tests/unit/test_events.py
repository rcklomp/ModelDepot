"""Unit tests for the append-only event log."""
from __future__ import annotations

import json
from pathlib import Path

from server.events import EventLog, BLOB_STORED, REVISION_PUBLISHED


def test_emit_appends_jsonl_line(tmp_path: Path):
    log = EventLog(tmp_path / "events.jsonl")
    log.emit(BLOB_STORED, digest="a" * 64, size=10)
    log.emit(REVISION_PUBLISHED, repo_id="o/r", commit="c")

    lines = (tmp_path / "events.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    e0 = json.loads(lines[0])
    assert e0["type"] == BLOB_STORED
    assert e0["payload"]["digest"] == "a" * 64
    assert e0["payload"]["size"] == 10
    assert "id" in e0 and "ts" in e0


def test_iter_events_roundtrip(tmp_path: Path):
    log = EventLog(tmp_path / "events.jsonl")
    ids = [log.emit(BLOB_STORED, digest=str(i)) for i in range(3)]
    parsed = list(log.iter_events())
    assert [e["id"] for e in parsed] == ids
    assert [e["type"] for e in parsed] == [BLOB_STORED] * 3


def test_iter_events_tolerates_partial_trailing_line(tmp_path: Path):
    p = tmp_path / "events.jsonl"
    p.write_text(json.dumps({"type": BLOB_STORED, "payload": {}}) + "\n")
    # Append a broken line (simulating a crash mid-write).
    with open(p, "a") as f:
        f.write('{"type": "Partial')
    log = EventLog(p)
    events = list(log.iter_events())
    assert len(events) == 1  # broken trailing line skipped


def test_iter_events_empty_when_absent(tmp_path: Path):
    log = EventLog(tmp_path / "absent.jsonl")
    assert list(log.iter_events()) == []
