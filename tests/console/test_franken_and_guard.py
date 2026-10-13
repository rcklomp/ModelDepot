"""The console's request guard (console/guard.py) and the Franken model switch (console/franken_api.py)."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

H = {"X-Console-Request": "1"}


@pytest.fixture
def fake_service(tmp_path, monkeypatch):
    """A stand-in franken_service.sh that records its argv and answers like the real one."""
    calls = tmp_path / "calls"
    script = tmp_path / "franken_service.sh"
    script.write_text(f"""#!/bin/sh
echo "$@" >> {calls}
case "$1" in
  status) echo '{{"service": {{"serving": "franken-glm"}}, "queued": null, "running": null, "last": null}}' ;;
  request) [ -e {tmp_path}/busy ] && {{ echo "refused: an action is running" >&2; exit 3; }}; echo 20261013T190000Z ;;
esac
""")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr("console.franken_api.SERVICE_SCRIPT", script)
    monkeypatch.setenv("FRANKEN_SERVICE_DIR", str(tmp_path))
    return tmp_path, calls


# ---- guard -------------------------------------------------------------------------------------------------------
def test_a_foreign_host_is_refused(populated_console):
    app, _ = populated_console
    with TestClient(app, base_url="http://evil.example:8002") as c:
        assert c.get("/api/console/healthz").status_code == 403


def test_localhost_with_any_port_is_allowed(populated_console):
    app, _ = populated_console
    with TestClient(app, base_url="http://localhost:18002") as c:
        assert c.get("/api/console/healthz").status_code == 200


def test_a_change_without_the_console_header_is_refused(populated_console, fake_service):
    app, _ = populated_console
    _, calls = fake_service
    with TestClient(app) as c:
        r = c.post("/api/console/franken/action", json={"action": "stop"})
    assert r.status_code == 403
    assert not calls.exists()


def test_a_cross_origin_change_is_refused(populated_console, fake_service):
    app, _ = populated_console
    _, calls = fake_service
    with TestClient(app, headers=H) as c:
        r = c.post("/api/console/franken/action", json={"action": "stop"},
                   headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert not calls.exists()


def test_a_same_origin_change_passes(populated_console, fake_service):
    app, _ = populated_console
    with TestClient(app, base_url="http://localhost:8002", headers=H) as c:
        r = c.post("/api/console/franken/action", json={"action": "stop"},
                   headers={"Origin": "http://localhost:8002"})
    assert r.status_code == 200


# ---- franken ------------------------------------------------------------------------------------------------------
def test_models_lists_the_three_arms(populated_console):
    app, _ = populated_console
    with TestClient(app, headers=H) as c:
        ms = c.get("/api/console/franken/models").json()["models"]
    assert [m["action"] for m in ms] == ["franken-glm", "franken-ds4", "franken"]
    assert ms[0]["model_id"] == "glm-5.3-flash"


def test_status_passes_the_service_json_through(populated_console, fake_service):
    app, _ = populated_console
    with TestClient(app, headers=H) as c:
        assert c.get("/api/console/franken/status").json()["service"]["serving"] == "franken-glm"


def test_an_action_queues_exactly_that_word(populated_console, fake_service):
    app, _ = populated_console
    _, calls = fake_service
    with TestClient(app, headers=H) as c:
        r = c.post("/api/console/franken/action", json={"action": "franken-ds4"})
    assert r.status_code == 200 and r.json()["job_id"] == "20261013T190000Z"
    assert calls.read_text().split("\n")[0] == "request franken-ds4"


@pytest.mark.parametrize("action", ["glm", "rm -rf /", "franken-glm; id", "", "status"])
def test_an_unknown_action_never_reaches_the_script(populated_console, fake_service, action):
    app, _ = populated_console
    _, calls = fake_service
    with TestClient(app, headers=H) as c:
        r = c.post("/api/console/franken/action", json={"action": action})
    assert r.status_code == 400
    assert not calls.exists()


def test_a_refusal_from_the_rig_is_a_409(populated_console, fake_service):
    app, _ = populated_console
    tmp, _ = fake_service
    (tmp / "busy").touch()
    with TestClient(app, headers=H) as c:
        r = c.post("/api/console/franken/action", json={"action": "stop"})
    assert r.status_code == 409 and "running" in r.json()["detail"]


def test_the_log_reads_only_job_logs(populated_console, fake_service):
    app, _ = populated_console
    tmp, _ = fake_service
    (tmp / "job-20261013T190000Z.log").write_text("line1\nline2\n")
    with TestClient(app, headers=H) as c:
        assert c.get("/api/console/franken/log/20261013T190000Z").json()["lines"] == ["line1", "line2"]
        assert c.get("/api/console/franken/log/..%2F..%2Fetc%2Fpasswd").status_code in (400, 404)
        assert c.get("/api/console/franken/log/notanid").status_code == 400


def test_without_the_script_the_switch_says_so(populated_console, monkeypatch, tmp_path):
    app, _ = populated_console
    monkeypatch.setattr("console.franken_api.SERVICE_SCRIPT", tmp_path / "absent.sh")
    with TestClient(app, headers=H) as c:
        assert c.get("/api/console/franken/status").status_code == 503
