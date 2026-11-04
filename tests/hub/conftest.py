from __future__ import annotations

import time

import httpx
import pytest

from tests.compat.conftest import make_live, stage_repo   # noqa: F401  (fixtures re-exported for this directory)

ADMIN = "adm-token"


class Api:
    def __init__(self, live, token: str | None = ADMIN):
        self.live, self.token = live, token

    def _h(self, token):
        t = self.token if token == "default" else token
        return {"Authorization": f"Bearer {t}"} if t else {}

    def get(self, path, token="default", **params):
        return httpx.get(self.live.url + path, params=params, headers=self._h(token), timeout=30)

    def post(self, path, json=None, token="default"):
        return httpx.post(self.live.url + path, json=json or {}, headers=self._h(token), timeout=30)

    def put(self, path, json=None, token="default"):
        return httpx.put(self.live.url + path, json=json or {}, headers=self._h(token), timeout=30)

    def wait_job(self, jid, states=("done", "failed"), timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            jobs = self.get("/api/hub/jobs").json()["jobs"]
            j = next((x for x in jobs if x["id"] == jid), None)
            if j and j["status"] in states:
                return j
            time.sleep(0.3)
        raise AssertionError(f"job {jid} did not reach {states}: {j}")


@pytest.fixture
def hub(make_live):
    live = make_live(admin_token=ADMIN)
    return live, Api(live)
