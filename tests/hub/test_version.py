"""One version number: pyproject.toml, the code constant, /openapi.json and /api/hub/status must agree, so a running container tells which build it is."""
from __future__ import annotations

import tomllib
from pathlib import Path

import httpx

from server import __version__


def test_pyproject_and_code_carry_the_same_version():
    meta = tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text())
    assert meta["project"]["version"] == __version__


def test_the_running_server_reports_it(hub):
    live, api = hub
    assert httpx.get(live.url + "/openapi.json", timeout=30).json()["info"]["version"] == __version__
    assert api.get("/api/hub/status", token=None).json()["version"] == __version__
