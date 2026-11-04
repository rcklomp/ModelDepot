"""Downloads through the real runner process. A second server instance is the 'upstream' Hugging Face."""
from __future__ import annotations

import time

from tests.compat.conftest import COMMIT, FILES, stage_repo
from .conftest import ADMIN, Api


def _pair(make_live, monkeypatch):
    upstream = make_live(admin_token="u")
    stage_repo(upstream.repo)
    stage_repo(upstream.repo, "datasets/acme/tiny-ds", {"README.md": b"# d\n", "data/train.jsonl": b'{"a": 1}\n'}, commit="d" * 40)
    monkeypatch.setenv("MIRROR_UPSTREAM_HF", upstream.url)          # inherited by the runner subprocess
    monkeypatch.setenv("HF_HUB_DISABLE_XET", "1")
    mine = make_live(admin_token=ADMIN)
    return upstream, mine, Api(mine)


def test_a_download_runs_to_done_and_the_repo_is_served(make_live, monkeypatch):
    _, mine, api = _pair(make_live, monkeypatch)
    r = api.post("/api/hub/jobs", {"source": "hf", "id": "acme/tiny", "type": "model", "files": ["config.json", "model.safetensors"],
                                   "total_bytes": len(FILES["config.json"]) + len(FILES["model.safetensors"]), "file_count": 2})
    assert r.status_code == 200, r.text
    job = api.wait_job(r.json()["job"]["id"])
    assert job["status"] == "done", job
    assert job["result"]["commit"] == COMMIT and job["files_done"] == 2
    d = api.get("/api/hub/repo", type="model", id="acme/tiny").json()
    assert sorted(f["path"] for f in d["revisions"][0]["files"]) == ["config.json", "model.safetensors"]      # only what was picked
    assert mine.repo.open_blob(mine.repo.get_file("acme/tiny", "main", "config.json").digest).read() == FILES["config.json"]
    assert not list((mine.root / "staging").glob("hf-*"))            # staging cleaned up on success
    assert api.get("/api/hub/wanted").json()["items"] == []


def test_a_dataset_download(make_live, monkeypatch):
    _, mine, api = _pair(make_live, monkeypatch)
    r = api.post("/api/hub/jobs", {"source": "hf", "id": "acme/tiny-ds", "type": "dataset"})
    job = api.wait_job(r.json()["job"]["id"])
    assert job["status"] == "done", job
    assert [i["id"] for i in api.get("/api/hub/library", type="dataset").json()["items"]] == ["acme/tiny-ds"]
    assert (mine.root / "datasets" / "acme" / "tiny-ds" / "refs.json").is_file()


def test_a_missing_repo_fails_with_a_readable_message(make_live, monkeypatch):
    _, mine, api = _pair(make_live, monkeypatch)
    job = api.wait_job(api.post("/api/hub/jobs", {"source": "hf", "id": "acme/does-not-exist"}).json()["job"]["id"])
    assert job["status"] == "failed" and job["message"]
    assert "Traceback" not in job["message"]


def test_the_same_download_is_not_queued_twice(make_live, monkeypatch):
    mine = make_live(admin_token=ADMIN)
    api = Api(mine)
    # the scheduler is running: hold the job in the queue with a download window that is closed now
    api.put("/api/hub/settings", {"download_window": "00:00-00:01" if time.localtime().tm_hour or time.localtime().tm_min > 1 else "12:00-12:01"})
    a = api.post("/api/hub/jobs", {"source": "hf", "id": "acme/tiny"}).json()
    b = api.post("/api/hub/jobs", {"source": "hf", "id": "acme/tiny"}).json()
    assert a["already_queued"] is False and b["already_queued"] is True and a["job"]["id"] == b["job"]["id"]
    time.sleep(1.5)
    j = api.get("/api/hub/jobs").json()["jobs"][0]
    assert j["status"] == "queued" and "download window" in j["note"]            # held back by the window, visibly


def test_pause_resume_and_remove_of_a_queued_job(make_live, monkeypatch):
    mine = make_live(admin_token=ADMIN)
    api = Api(mine)
    now = time.localtime()
    away = f"{(now.tm_hour + 6) % 24:02d}:00-{(now.tm_hour + 7) % 24:02d}:00"          # a window that is not now
    api.put("/api/hub/settings", {"download_window": away})
    jid = api.post("/api/hub/jobs", {"source": "hf", "id": "acme/tiny"}).json()["job"]["id"]
    assert api.post(f"/api/hub/jobs/{jid}/pause").status_code == 200
    assert api.get("/api/hub/jobs").json()["jobs"][0]["status"] == "paused"
    assert api.post(f"/api/hub/jobs/{jid}/resume").status_code == 200
    assert api.get("/api/hub/jobs").json()["jobs"][0]["status"] == "queued"
    assert api.post(f"/api/hub/jobs/{jid}/remove").status_code == 200
    assert api.get("/api/hub/jobs").json()["jobs"] == []
    assert api.post(f"/api/hub/jobs/{jid}/resume").status_code == 409


def test_bad_requests_and_not_enough_disk_are_refused_up_front(make_live, monkeypatch):
    mine = make_live(admin_token=ADMIN)
    api = Api(mine)
    assert api.post("/api/hub/jobs", {"source": "hf", "id": "not a repo!"}).status_code == 400
    assert api.post("/api/hub/jobs", {"source": "civitai", "id": "abc"}).status_code == 400
    assert api.post("/api/hub/jobs", {"source": "ftp", "id": "a/b"}).status_code == 400
    r = api.post("/api/hub/jobs", {"source": "hf", "id": "acme/huge", "total_bytes": 10**18})
    assert r.status_code == 400 and "not enough free space" in r.json()["detail"]
    assert api.get("/api/hub/jobs").json()["jobs"] == []


def test_a_restart_requeues_a_running_job_and_keeps_finished_ones(make_live):
    from server.hub.jobs import JobManager
    from server.hub.state import HubSettings
    mine = make_live(admin_token=ADMIN)
    import json
    jobs_dir = mine.root / "jobs"
    (jobs_dir / "deadbeef0001.json").write_text(json.dumps({"id": "deadbeef0001", "kind": "download", "label": "a/b", "spec": {"source": "hf", "id": "a/b", "type": "model"}, "status": "running"}))
    (jobs_dir / "deadbeef0002.json").write_text(json.dumps({"id": "deadbeef0002", "kind": "download", "label": "c/d", "spec": {"source": "hf", "id": "c/d", "type": "model"}, "status": "done"}))
    storage = mine.repo._storage
    m = JobManager(mine.repo, storage, HubSettings(mine.root))
    by_id = {j["id"]: j for j in m.list()}
    assert by_id["deadbeef0001"]["status"] == "queued" and "restart" in by_id["deadbeef0001"]["note"]
    assert by_id["deadbeef0002"]["status"] == "done"


def test_verify_finds_a_damaged_blob(make_live):
    mine = make_live(admin_token=ADMIN)
    api = Api(mine)
    stage_repo(mine.repo)
    ok = api.wait_job(api.post("/api/hub/ops/verify", {}).json()["job"]["id"])
    assert ok["status"] == "done" and ok["result"]["checked"] == len(FILES)
    digest = mine.repo.get_file("acme/tiny", "main", "config.json").digest
    path = mine.repo._storage.blobs.path_for(digest)
    path.write_bytes(b"corrupted")
    bad = api.wait_job(api.post("/api/hub/ops/verify", {"type": "model", "id": "acme/tiny"}).json()["job"]["id"])
    assert bad["status"] == "failed" and "damaged" in bad["message"] and f"sha256:{digest}" in bad["result"]["bad"]
