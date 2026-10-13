"""Tests for the catalog (local mirror browse) endpoint."""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_catalog_lists_local_repos(populated_console):
    app, _ = populated_console
    with TestClient(app, headers={"X-Console-Request": "1"}) as c:
        r = c.get("/api/console/catalog")
    assert r.status_code == 200
    items = r.json()
    repo_ids = {i["repo_id"] for i in items}
    assert "org/gguf-model" in repo_ids
    assert "org/safetensors-model" in repo_ids


def test_catalog_includes_type_and_size(populated_console):
    app, _ = populated_console
    with TestClient(app, headers={"X-Console-Request": "1"}) as c:
        items = c.get("/api/console/catalog").json()
    by_id = {i["repo_id"]: i for i in items}
    gguf = by_id["org/gguf-model"]
    assert gguf["type"] == "gguf"
    assert gguf["total_size_bytes"] == 300  # 100 + 200
    assert set(gguf["quants"]) == {"Q4_K_M", "Q8_0"}

    st = by_id["org/safetensors-model"]
    assert st["type"] == "safetensors"


def test_catalog_filter_by_type(populated_console):
    app, _ = populated_console
    with TestClient(app, headers={"X-Console-Request": "1"}) as c:
        items = c.get("/api/console/catalog?type=gguf").json()
    assert len(items) == 1
    assert items[0]["repo_id"] == "org/gguf-model"


def test_catalog_text_query(populated_console):
    app, _ = populated_console
    with TestClient(app, headers={"X-Console-Request": "1"}) as c:
        items = c.get("/api/console/catalog?q=safetensors").json()
    assert len(items) == 1
    assert items[0]["repo_id"] == "org/safetensors-model"


def test_catalog_sort_by_size(populated_console):
    app, _ = populated_console
    with TestClient(app, headers={"X-Console-Request": "1"}) as c:
        items = c.get("/api/console/catalog?sort=size").json()
    # safetensors (500 bytes) is bigger than gguf (300)
    assert items[0]["repo_id"] == "org/safetensors-model"


def test_model_detail(populated_console):
    app, _ = populated_console
    with TestClient(app, headers={"X-Console-Request": "1"}) as c:
        r = c.get("/api/console/models/org/gguf-model")
    assert r.status_code == 200
    detail = r.json()
    assert detail["repo_id"] == "org/gguf-model"
    assert "main" in detail["refs"]
    assert len(detail["revisions"]) == 1
    rev = detail["revisions"][0]
    paths = {f["path"] for f in rev["files"]}
    assert "model.Q4_K_M.gguf" in paths


def test_model_detail_404(populated_console):
    app, _ = populated_console
    with TestClient(app, headers={"X-Console-Request": "1"}) as c:
        r = c.get("/api/console/models/no/such")
    assert r.status_code == 404


def test_healthz(populated_console):
    app, _ = populated_console
    with TestClient(app, headers={"X-Console-Request": "1"}) as c:
        r = c.get("/api/console/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["repos"] == 2
