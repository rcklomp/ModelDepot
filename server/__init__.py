"""ModelDepot server. `__version__` is the one place the version is written in code (pyproject.toml carries the same number; tests/unit/test_version.py keeps them equal).
It shows in `/openapi.json` (info.version), `GET /api/hub/status` and the GUI, so a running container tells which build it is."""
__version__ = "0.2.1"
