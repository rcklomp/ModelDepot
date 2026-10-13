"""The one model list (console/inventory.py): each model once, Ollama linked to its mirror file by size."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from console import inventory

H = {"X-Console-Request": "1"}


def _ollama(monkeypatch, models, error=None):
    monkeypatch.setattr(inventory, "ollama_models", lambda: (models, error))


def _rows(app):
    with TestClient(app, headers=H) as c:
        r = c.get("/api/console/inventory")
    assert r.status_code == 200
    return r.json()


def test_an_ollama_model_made_from_a_mirror_file_sits_on_that_files_row(populated_console, monkeypatch):
    app, _ = populated_console
    _ollama(monkeypatch, [{"name": "model-q8:latest", "size": 200 + 583, "quant": "Q8_0", "params": "1B"},
                          {"name": "model-q8-256k:latest", "size": 200 + 584, "quant": "Q8_0", "params": "1B"}])
    d = _rows(app)
    q8 = [r for r in d["rows"] if r["file"] == "model.Q8_0.gguf"][0]
    assert q8["ollama"] == ["model-q8:latest", "model-q8-256k:latest"] and q8["quant"] == "Q8_0"
    q4 = [r for r in d["rows"] if r["file"] == "model.Q4_K_M.gguf"][0]
    assert q4["ollama"] == [] and q4["can_add"]
    assert not [r for r in d["rows"] if r["kind"] == "ollama"]       # nothing listed twice


def test_an_ollama_only_model_gets_its_own_row(populated_console, monkeypatch):
    app, _ = populated_console
    _ollama(monkeypatch, [{"name": "llama3.2:1b", "size": 1321098329, "quant": "Q8_0", "params": "1.2B"}])
    rows = _rows(app)["rows"]
    own = [r for r in rows if r["kind"] == "ollama"]
    assert [r["label"] for r in own] == ["llama3.2:1b"] and not own[0]["can_add"]


def test_safetensors_is_listed_but_not_offered_to_ollama(populated_console, monkeypatch):
    app, _ = populated_console
    _ollama(monkeypatch, [])
    st = [r for r in _rows(app)["rows"] if r["repo_id"] == "org/safetensors-model"][0]
    assert not st["can_add"] and "GGUF only" in st["note"]


def test_ollama_down_is_said_and_the_mirror_still_lists(populated_console, monkeypatch):
    app, _ = populated_console
    _ollama(monkeypatch, [], "Ollama does not answer")
    d = _rows(app)
    assert d["ollama_error"] and len([r for r in d["rows"] if r["kind"] == "mirror"]) == 3


def test_gguf_targets_groups_shards_and_drops_mmproj():
    files = [{"path": "m-UD-IQ2_M-00001-of-00002.gguf", "size": 10},
             {"path": "m-UD-IQ2_M-00002-of-00002.gguf", "size": 5},
             {"path": "mmproj-m-f16.gguf", "size": 3},
             {"path": "other-Q6_K_P.gguf", "size": 7},
             {"path": "big-Q4_K_M-00001-of-00003.gguf", "size": 1}]
    t = {x["label"]: x for x in inventory.gguf_targets(files)}
    assert set(t) == {"m-UD-IQ2_M", "other-Q6_K_P.gguf", "big-Q4_K_M"}
    assert t["m-UD-IQ2_M"]["size"] == 15 and t["m-UD-IQ2_M"]["complete"] and t["m-UD-IQ2_M"]["quant"] == "UD-IQ2_M"
    assert not t["big-Q4_K_M"]["complete"]
    assert t["other-Q6_K_P.gguf"]["quant"] == "Q6_K_P"


# ---- the Ollama actions the table's buttons call -------------------------------------------------------------
def test_the_ollama_routes_exist(populated_console):
    app, _ = populated_console
    spec = app.openapi()["paths"]          # app.routes nests included routers in newer FastAPI
    paths = {(m.upper(), p) for p, ops in spec.items() for m in ops}
    assert ("POST", "/api/console/ollama/load") in paths          # removed by mistake once (2026-10-13)
    assert ("DELETE", "/api/console/ollama/models/{model_name}") in paths
    assert ("GET", "/api/console/inventory") in paths


def test_add_to_ollama_runs_ollama_create_on_the_mirror_blob(populated_console, monkeypatch):
    import subprocess
    app, _ = populated_console
    seen = []
    def fake_run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr("console.ollama_api.subprocess.run", fake_run)
    with TestClient(app, headers=H) as c:
        r = c.post("/api/console/ollama/load", json={"repo_id": "org/gguf-model", "file_path": "model.Q8_0.gguf",
                                                     "model_name": "model-q8"})
    assert r.status_code == 200 and r.json()["ok"]
    assert seen and seen[0][:3] == ["ollama", "create", "model-q8"]


def test_a_name_that_looks_like_an_option_is_refused(populated_console, monkeypatch):
    app, _ = populated_console
    monkeypatch.setattr("console.ollama_api.subprocess.run", lambda *a, **k: pytest.fail("ran ollama"))
    with TestClient(app, headers=H) as c:
        assert c.post("/api/console/ollama/load", json={"repo_id": "org/gguf-model", "file_path": "model.Q8_0.gguf",
                                                        "model_name": "--help"}).status_code == 422
        assert c.delete("/api/console/ollama/models/-rf").status_code == 400
