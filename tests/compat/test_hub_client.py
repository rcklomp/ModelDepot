"""The real huggingface_hub client against a live server (see conftest.py)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from .conftest import COMMIT, FILES, stage_repo

SNAP = """
import json, os
from huggingface_hub import snapshot_download
p = snapshot_download("acme/tiny"{extra})
out = {}
for root, _, files in os.walk(p):
    for f in files:
        full = os.path.join(root, f)
        out[os.path.relpath(full, p)] = os.path.getsize(full)
print(json.dumps(out))
"""


def snap(extra: str) -> str:
    return SNAP.replace("{extra}", extra)


def test_snapshot_download_gets_every_file(make_live):
    live = make_live()
    stage_repo(live.repo)
    r = live.client(snap(""))
    assert r.returncode == 0, r.stderr[-600:]
    got = json.loads(r.stdout.strip().splitlines()[-1])
    assert got == {k: len(v) for k, v in FILES.items()}


def test_snapshot_download_with_a_cold_cache_and_allow_patterns(make_live):
    live = make_live()
    stage_repo(live.repo)
    r = live.client(snap(', allow_patterns=["*.json"]'))
    assert r.returncode == 0, r.stderr[-600:]
    assert list(json.loads(r.stdout.strip().splitlines()[-1])) == ["config.json"]


def test_model_info_with_a_revision_resolves_branch_and_commit(make_live):
    live = make_live()
    stage_repo(live.repo)
    code = ("from huggingface_hub import HfApi\na = HfApi()\n"
            "print(a.model_info('acme/tiny', revision='main').sha)\n"
            f"print(a.model_info('acme/tiny', revision='{COMMIT}').sha)\n"
            "print(sorted(s.rfilename for s in a.model_info('acme/tiny', files_metadata=True).siblings))\n")
    r = live.client(code)
    assert r.returncode == 0, r.stderr[-600:]
    lines = r.stdout.strip().splitlines()
    assert lines[0] == COMMIT and lines[1] == COMMIT
    assert "onnx/model.onnx" in lines[2]


def test_download_pinned_to_a_commit(make_live):
    live = make_live()
    stage_repo(live.repo)
    r = live.client(f"from huggingface_hub import hf_hub_download\nprint(hf_hub_download('acme/tiny', 'onnx/model.onnx', revision='{COMMIT}'))\n")
    assert r.returncode == 0, r.stderr[-600:]


def test_unknown_repo_is_a_clean_not_found(make_live):
    live = make_live()
    r = live.client("from huggingface_hub import snapshot_download\n"
                    "try:\n    snapshot_download('acme/nope')\nexcept Exception as e:\n    print(type(e).__name__)\n")
    assert r.stdout.strip().splitlines()[-1] in ("RepositoryNotFoundError", "LocalEntryNotFoundError")


def _head(url: str, token: str = ""):
    req = urllib.request.Request(url, method="HEAD", headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        return urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as e:
        return e


def test_a_moving_ref_is_not_cached_forever_but_a_commit_is(make_live):
    live = make_live()
    stage_repo(live.repo)
    moving = _head(f"{live.url}/acme/tiny/resolve/main/config.json")
    pinned = _head(f"{live.url}/acme/tiny/resolve/{COMMIT}/config.json")
    assert moving.status == 200 and "immutable" not in moving.headers["Cache-Control"]
    assert pinned.status == 200 and "immutable" in pinned.headers["Cache-Control"]


def test_the_auth_token_is_enforced_on_the_hub_routes(make_live):
    live = make_live(auth_token="s3cret")
    stage_repo(live.repo)
    assert _head(f"{live.url}/acme/tiny/resolve/main/config.json").status == 401
    assert _head(f"{live.url}/acme/tiny/resolve/main/config.json", "wrong").status == 401
    assert _head(f"{live.url}/acme/tiny/resolve/main/config.json", "s3cret").status == 200
    assert json.load(urllib.request.urlopen(f"{live.url}/healthz"))["ok"] is True          # monitoring stays open
    ok = live.client(snap(""), token="s3cret")
    assert ok.returncode == 0, ok.stderr[-400:]
    bad = live.client(snap(""))
    assert bad.returncode != 0


def test_a_bad_repo_id_is_a_404_not_a_500(make_live):
    live = make_live()
    for path in ("/api/models/.hidden/x", "/.hidden/x/resolve/main/f", "/api/models/org/.x/revision/main"):
        r = _head(live.url + path) if "resolve" in path else urllib.request.Request(live.url + path)
        status = r.status if hasattr(r, "status") else None
        if status is None:
            try:
                status = urllib.request.urlopen(r, timeout=10).status
            except urllib.error.HTTPError as e:
                status = e.code
        assert status == 404, (path, status)


def test_tree_listing_follows_the_hub_semantics(make_live):
    """recursive=true lists every nested file; without it only the direct children; a path lists that folder."""
    live = make_live()
    stage_repo(live.repo)
    code = ("from huggingface_hub import HfApi\na = HfApi()\n"
            "f = lambda **kw: sorted((e.path, type(e).__name__) for e in a.list_repo_tree('acme/tiny', **kw))\n"
            "print(f(recursive=True))\nprint(f())\nprint(f(path_in_repo='onnx'))\n")
    r = live.client(code)
    assert r.returncode == 0, r.stderr[-600:]
    rec, top, sub = [eval(x) for x in r.stdout.strip().splitlines()[-3:]]
    assert ("onnx/model.onnx", "RepoFile") in rec and ("onnx", "RepoFolder") in rec and len(rec) == 5
    assert ("onnx", "RepoFolder") in top and ("onnx/model.onnx", "RepoFile") not in top and len(top) == 4
    assert sub == [("onnx/model.onnx", "RepoFile")]


def test_file_system_listing_works(make_live):
    """HfFileSystem (what `datasets`, pandas and fsspec use) lists and reads through the same routes."""
    live = make_live()
    stage_repo(live.repo)
    code = ("from huggingface_hub import HfFileSystem\nfs = HfFileSystem()\n"
            "print(sorted(fs.ls('acme/tiny', detail=False)))\nprint(fs.cat('acme/tiny/config.json'))\n"
            "print(sorted(fs.glob('acme/tiny/**/*.onnx')))\n")
    r = live.client(code)
    assert r.returncode == 0, r.stderr[-600:]
    out = r.stdout.strip().splitlines()
    assert "acme/tiny/onnx" in out[0] and "acme/tiny/config.json" in out[0]
    assert "llama" in out[1] and "acme/tiny/onnx/model.onnx" in out[2]
