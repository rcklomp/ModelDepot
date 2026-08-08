"""Tests for the async ingest + jobs + ops APIs."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient


def _wait_for_job(c, job_id, timeout=10):
    """Poll a job until it's DONE or FAILED."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = c.get(f"/api/console/jobs/{job_id}")
        body = r.json()
        if body["status"] in ("done", "failed"):
            return body
        time.sleep(0.1)
    pytest.fail(f"job {job_id} did not finish in {timeout}s")


def test_ingest_hf_via_http_with_stubbed_source(console_app, monkeypatch):
    """POST /ingest runs a background ingest (source stubbed) → publishes."""
    app, state = console_app
    import hashlib
    from ingester.hf_source import StagedFile, StagedSnapshot

    def fake_fetch(repo_id, staging_dir, *, revision="main", token=None,
                   allow_patterns=None, ignore_patterns=None, endpoint=None,
                   max_retries=6, progress_log=None):
        staging_dir.mkdir(parents=True, exist_ok=True)
        content = b"fake model"
        p = staging_dir / "config.json"; p.write_bytes(content)
        sha = hashlib.sha256(content).hexdigest()
        return StagedSnapshot(
            repo_id=repo_id, revision_requested=revision, commit="z" * 40,
            staging_dir=staging_dir,
            files=[StagedFile(path="config.json", local_path=p, size=len(content),
                              etag=sha, sha256=sha)],
        )
    monkeypatch.setattr("console.ingest_api.hf_source.fetch", fake_fetch)

    with TestClient(app) as c:
        r = c.post("/api/console/ingest", json={
            "source": "hf", "repo_id": "fakeorg/model", "revision": "main",
        })
        assert r.status_code == 200
        job_id = r.json()["job_id"]

        body = _wait_for_job(c, job_id)
        assert body["status"] == "done"
        assert body["result"]["published"] is True
        assert body["result"]["file_count"] == 1

        # The model is now in the catalog.
        items = c.get("/api/console/catalog").json()
        assert any(i["repo_id"] == "fakeorg/model" for i in items)


def test_ingest_records_failure(console_app, monkeypatch):
    """A failing ingest marks the job FAILED with the error message."""
    app, _ = console_app

    def fake_fetch(*a, **kw):
        raise RuntimeError("simulated network failure")
    monkeypatch.setattr("console.ingest_api.hf_source.fetch", fake_fetch)

    with TestClient(app) as c:
        r = c.post("/api/console/ingest", json={
            "source": "hf", "repo_id": "broken/model",
        })
        job_id = r.json()["job_id"]
        body = _wait_for_job(c, job_id)
        assert body["status"] == "failed"
        assert "simulated network failure" in body["error"]


def test_list_jobs(populated_console, monkeypatch):
    app, _ = populated_console
    import hashlib
    from ingester.hf_source import StagedFile, StagedSnapshot

    def fake_fetch(repo_id, staging_dir, **kw):
        staging_dir.mkdir(parents=True, exist_ok=True)
        p = staging_dir / "f.txt"; p.write_bytes(b"x")
        sha = hashlib.sha256(b"x").hexdigest()
        return StagedSnapshot(repo_id=repo_id, revision_requested="main", commit="a" * 40,
                              staging_dir=staging_dir,
                              files=[StagedFile(path="f.txt", local_path=p, size=1, etag=sha, sha256=sha)])
    monkeypatch.setattr("console.ingest_api.hf_source.fetch", fake_fetch)

    with TestClient(app) as c:
        c.post("/api/console/ingest", json={"source": "hf", "repo_id": "a/b"})
        c.post("/api/console/ingest", json={"source": "hf", "repo_id": "c/d"})
        jobs = c.get("/api/console/jobs").json()
        assert len(jobs) >= 2
        # Newest first.
        assert jobs[0]["started_at"] >= jobs[1]["started_at"]


def test_job_404(console_app):
    app, _ = console_app
    with TestClient(app) as c:
        r = c.get("/api/console/jobs/nonexistent")
    assert r.status_code == 404


def test_ops_verify(populated_console):
    app, _ = populated_console
    with TestClient(app) as c:
        r = c.post("/api/console/ops/verify")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["checked"] >= 2  # the two staged repos' blobs


def test_ops_gc_dry_run(populated_console):
    app, state = populated_console
    # Drop an orphan blob so GC has something to find.
    state.repo.put_blob_bytes(b"orphan-not-referenced")
    with TestClient(app) as c:
        r = c.post("/api/console/ops/gc", json={"dry_run": True})
    body = r.json()
    assert body["dry_run"] is True
    assert body["acted"] == 1  # the orphan


def test_ops_gc_deletes(populated_console):
    app, state = populated_console
    state.repo.put_blob_bytes(b"orphan")
    before = len(list(state.repo.iter_blobs()))
    with TestClient(app) as c:
        c.post("/api/console/ops/gc", json={"dry_run": False})
    after = len(list(state.repo.iter_blobs()))
    assert after == before - 1


def test_ingest_validates_source(console_app):
    app, _ = console_app
    with TestClient(app) as c:
        r = c.post("/api/console/ingest", json={"source": "bogus"})
    assert r.status_code == 400
