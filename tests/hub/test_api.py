"""The GUI's API: auth, library, delete, wanted, settings."""
from __future__ import annotations

from tests.compat.conftest import COMMIT, FILES, stage_repo
from .conftest import ADMIN, Api


def test_changes_need_the_admin_token_and_reads_are_open_by_default(hub):
    live, api = hub
    stage_repo(live.repo)
    assert api.get("/api/hub/library", token=None).status_code == 200          # LAN-open reads by default
    for method, path in (("post", "/api/hub/ops/gc"), ("post", "/api/hub/repo/delete"), ("post", "/api/hub/jobs"),
                         ("put", "/api/hub/settings"), ("post", "/api/hub/ops/verify")):
        r = getattr(api, method)(path, {"id": "x"}, token=None)
        assert r.status_code == 401, path
        r = getattr(api, method)(path, {"id": "x"}, token="wrong")
        assert r.status_code == 401, path
    assert api.get("/api/hub/auth/check", token=ADMIN).json()["admin"] is True
    assert api.get("/api/hub/auth/check", token=None).json()["admin"] is False


def test_the_read_token_also_guards_the_gui_api(make_live):
    live = make_live(admin_token=ADMIN, auth_token="readme")
    stage_repo(live.repo)
    api = Api(live, token=None)
    assert api.get("/api/hub/library", token=None).status_code == 401
    assert api.get("/api/hub/library", token="readme").status_code == 200
    assert api.get("/api/hub/library", token=ADMIN).status_code == 200          # admin implies read
    assert api.post("/api/hub/ops/gc", {"dry_run": True}, token="readme").status_code == 401    # read token is not admin


def test_library_lists_filters_and_sorts(hub):
    live, api = hub
    stage_repo(live.repo, "acme/tiny", FILES, commit="a" * 40)
    stage_repo(live.repo, "acme/big", {"m.gguf": b"x" * 5000}, commit="b" * 40)
    stage_repo(live.repo, "datasets/acme/ds", {"train.parquet": b"p" * 10}, commit="c" * 40)
    items = api.get("/api/hub/library").json()["items"]
    assert [i["id"] for i in items] == ["acme/big", "acme/ds", "acme/tiny"]          # by name, models and datasets together
    assert [i["id"] for i in api.get("/api/hub/library", type="dataset").json()["items"]] == ["acme/ds"]
    assert [i["id"] for i in api.get("/api/hub/library", q="gguf").json()["items"]] == ["acme/big"]
    assert [i["id"] for i in api.get("/api/hub/library", type="model", sort="size").json()["items"]] == ["acme/tiny", "acme/big"]   # biggest first
    ds = next(i for i in items if i["type"] == "dataset")
    assert ds["core_id"] == "datasets/acme/ds" and ds["formats"] == ["parquet"]


def test_repo_detail_has_files_revisions_and_the_readme(hub):
    live, api = hub
    stage_repo(live.repo)
    d = api.get("/api/hub/repo", type="model", id="acme/tiny").json()
    assert d["commit"] == COMMIT and d["readme"] == "# tiny\n" and d["refs"] == {"main": COMMIT}
    assert {f["path"] for f in d["revisions"][0]["files"]} == set(FILES)
    assert api.get("/api/hub/repo", type="model", id="acme/none").status_code == 404


def test_delete_frees_only_what_nothing_else_uses(hub):
    """Two repos share one file (dedup). Deleting one frees its own bytes and keeps the shared ones."""
    live, api = hub
    shared = b"S" * 1000
    stage_repo(live.repo, "acme/a", {"shared.bin": shared, "only_a.bin": b"A" * 300}, commit="a" * 40)
    stage_repo(live.repo, "acme/b", {"shared.bin": shared, "only_b.bin": b"B" * 200}, commit="b" * 40)
    pre = api.get("/api/hub/repo/delete-preview", type="model", id="acme/a").json()
    assert pre == {"frees_bytes": 300, "shared_bytes": 1000}
    out = api.post("/api/hub/repo/delete", {"type": "model", "id": "acme/a"}).json()
    assert out["freed_bytes"] == 300 and out["shared_bytes_kept"] == 1000
    assert live.repo.list_repos() == ["acme/b"]
    assert live.repo.get_file("acme/b", "main", "shared.bin").size == 1000
    assert live.repo.open_blob(live.repo.get_file("acme/b", "main", "shared.bin").digest).read() == shared      # still readable
    assert api.post("/api/hub/repo/delete", {"type": "model", "id": "acme/a"}).status_code == 404


def test_delete_is_refused_while_an_ingest_runs(hub):
    live, api = hub
    stage_repo(live.repo)
    with live.repo.ingest_lock():
        r = api.post("/api/hub/repo/delete", {"type": "model", "id": "acme/tiny"})
    assert r.status_code == 409 and "try again" in r.json()["detail"]
    assert live.repo.list_repos() == ["acme/tiny"]


def test_gc_preview_and_run_never_touch_referenced_blobs(hub):
    live, api = hub
    stage_repo(live.repo)
    live.repo.put_blob_bytes(b"orphan")
    prev = api.post("/api/hub/ops/gc", {"dry_run": True}).json()
    assert prev["dry_run"] is True and prev["blobs"] == 0          # too young: the age guard protects a fresh blob


def test_wanted_lists_what_clients_asked_for_and_we_lack(hub):
    live, api = hub
    import httpx
    httpx.get(live.url + "/api/models/someone/missing-model", headers={"User-Agent": "huggingface_hub/1.0"})
    httpx.get(live.url + "/api/datasets/someone/missing-ds")
    w = api.get("/api/hub/wanted").json()["items"]
    assert {(i["type"], i["id"]) for i in w} == {("model", "someone/missing-model"), ("dataset", "someone/missing-ds")}
    assert next(i for i in w if i["id"].endswith("model"))["client"].startswith("huggingface_hub")
    api.post("/api/hub/wanted/dismiss", {"type": "model", "id": "someone/missing-model"})
    assert [i["id"] for i in api.get("/api/hub/wanted").json()["items"]] == ["someone/missing-ds"]


def test_settings_never_reveal_a_token(hub):
    live, api = hub
    r = api.put("/api/hub/settings", {"hf_token": "hf_secret123", "max_concurrent": 2, "download_window": "01:00-07:00"})
    assert r.status_code == 200 and r.json()["hf_token_set"] is True and "hf_secret123" not in r.text
    assert "hf_secret123" not in api.get("/api/hub/settings").text
    assert api.get("/api/hub/status").json()["hf_token_set"] is True
    assert api.put("/api/hub/settings", {"download_window": "banana"}).status_code == 400
    import os, stat
    p = live.root / "config" / "settings.json"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    api.put("/api/hub/settings", {"hf_token": ""})
    assert api.get("/api/hub/settings").json()["hf_token_set"] is False


def test_status_reports_space_counts_and_the_store_id(hub):
    live, api = hub
    stage_repo(live.repo)
    s = api.get("/api/hub/status").json()
    assert s["models"] == 1 and s["datasets"] == 0 and s["free_bytes"] > 0 and s["store_id"]
    assert s["stored"]["blobs"] == len(FILES)


def test_the_gui_page_and_its_assets_are_served(hub):
    live, _ = hub
    import httpx
    r = httpx.get(live.url + "/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
