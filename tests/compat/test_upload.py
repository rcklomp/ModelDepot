"""Publishing through the REAL huggingface_hub client: create_repo, upload_file, upload_folder, delete_file, `hf upload`.

The client runs in a subprocess against a live uvicorn server (see conftest.py). Where the client cannot be made to misbehave
(a wrong hash on the LFS verify step, a half-finished upload), the same routes are driven with raw httpx, in the exact sequence the
client uses (preupload -> LFS batch -> PUT -> verify -> commit). Xet is switched off by `Live.client` (HF_HUB_DISABLE_XET=1), as
a user of this server has to do.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

ADMIN = "adm-token"
OID = "a" * 64

PRELUDE = '''
import hashlib, json, os, sys, tempfile
from pathlib import Path
from huggingface_hub import HfApi, hf_hub_download, snapshot_download
from huggingface_hub.errors import HfHubHTTPError, RemoteEntryNotFoundError
api = HfApi()
def sha(b): return hashlib.sha256(b).hexdigest()
def tree(root):
    root = Path(root)
    return {p.relative_to(root).as_posix(): sha(p.read_bytes()) for p in sorted(root.rglob("*")) if p.is_file()}
def status(fn):
    try:
        fn(); return None
    except HfHubHTTPError as e:
        return e.response.status_code
'''

# A folder with a nested path and three kinds of file: small text (travels inside the commit), a >1 MiB file (LFS by size), a small
# .safetensors (LFS by extension).
MAKE_FOLDER = '''
d = Path(tempfile.mkdtemp())
(d / "README.md").write_text("# published\\n")
(d / "configs").mkdir(); (d / "configs" / "base.json").write_text('{"layers": 2}')
(d / "weights").mkdir()
(d / "weights" / "w.bin").write_bytes(bytes(range(256)) * 6000)
(d / "weights" / "small.safetensors").write_bytes(b"tensor" * 10)
(d / "notes.txt").write_bytes(b"x" * 1_200_000)
'''


def run(live, code: str, token: str = ADMIN):
    """Run client code; its LAST stdout line must be a JSON document."""
    r = live.client(PRELUDE + code, token=token)
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def cid_of(rtype: str, rid: str) -> str:
    return ("datasets/" if rtype == "dataset" else "") + rid


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def blob_bytes(live, cid: str, rev: str, path: str) -> bytes:
    with live.repo.open_blob(live.repo.get_file(cid, rev, path).digest) as f:
        return f.read()


# ---- the happy path, for a model and for a dataset -------------------------------------------------------------------------

@pytest.mark.parametrize("rtype", ["model", "dataset"])
def test_create_upload_file_and_folder_then_download_the_same_bytes(make_live, rtype):
    live = make_live(admin_token=ADMIN)
    rid = f"acme/pub-{rtype}"
    out = run(live, MAKE_FOLDER + f'''
rt, rid = {rtype!r}, {rid!r}
url = api.create_repo(rid, repo_type=rt)
empty = api.repo_info(rid, repo_type=rt)
exists = api.repo_exists(rid, repo_type=rt)
c1 = api.upload_file(path_or_fileobj=b"hello world\\n", path_in_repo="hello.txt", repo_id=rid, repo_type=rt)
c2 = api.upload_folder(folder_path=str(d), repo_id=rid, repo_type=rt)
info = api.repo_info(rid, repo_type=rt)
p = snapshot_download(rid, repo_type=rt)
local = tree(d); local["hello.txt"] = sha(b"hello world\\n")
print(json.dumps(dict(url=str(url), repo_id=url.repo_id, repo_type=url.repo_type, empty_sha=empty.sha, empty_files=[s.rfilename for s in (empty.siblings or [])],
    exists=exists, c1=c1.oid, c2=c2.oid, c2_url=c2.commit_url, sha=info.sha, local=local, downloaded=tree(p),
    files=sorted(api.list_repo_files(rid, repo_type=rt)),
    tree=sorted(e.path for e in api.list_repo_tree(rid, repo_type=rt, recursive=True)),
    old=sorted(api.list_repo_files(rid, repo_type=rt, revision=c1.oid)))))
''')
    prefix = "datasets/" if rtype == "dataset" else ""
    assert out["url"] == f"{live.url}/{prefix}{rid}" and out["repo_id"] == rid and out["repo_type"] == rtype
    assert out["empty_sha"] is None and out["empty_files"] == [] and out["exists"] is True
    assert out["sha"] == out["c2"] != out["c1"]                         # model_info().sha IS the commit the upload returned
    assert out["c2_url"] == f"{live.url}/{prefix}{rid}/commit/{out['c2']}"
    assert out["downloaded"] == out["local"]                             # snapshot_download: every byte (sha256) as uploaded
    assert out["files"] == sorted(out["local"]) and set(out["files"]) <= set(out["tree"])
    assert {"configs", "weights"} <= set(out["tree"])                     # the folders show up in the recursive tree
    assert out["old"] == ["hello.txt"]                                    # the first commit still lists only its own file

    cid = cid_of(rtype, rid)
    assert live.repo.list_revisions(cid) == sorted([out["c1"], out["c2"]])
    assert live.repo.list_references(cid) == {"main": out["c2"]}
    m = live.repo.get_manifest(cid, "main")
    assert m.source.kind == "upload" and m.revision.commit == out["c2"] and m.revision.license is None
    # Bytes in the store are the uploaded ones: the 1.5 MB LFS file and the small text file.
    assert blob_bytes(live, cid, "main", "weights/w.bin") == bytes(range(256)) * 6000
    assert blob_bytes(live, cid, "main", "README.md") == b"# published\n"
    # The etag follows the Hub: sha256 for LFS files, the git blob sha1 for the others; X-Repo-Commit names the revision SERVED, also for
    # a file carried over unchanged from the first commit.
    hf = lambda p: live.repo.get_file(cid, "main", p).metadata["hf"]
    assert hf("weights/w.bin")["etag"] == '"%s"' % sha256(bytes(range(256)) * 6000)
    assert hf("weights/small.safetensors")["etag"] == '"%s"' % sha256(b"tensor" * 10)
    assert hf("README.md")["etag"] == '"%s"' % hashlib.sha1(b"blob 12\0# published\n").hexdigest()
    assert hf("hello.txt")["x_repo_commit"] == out["c2"] and hf("README.md")["x_repo_commit"] == out["c2"]
    assert live.repo.get_file(cid, out["c1"], "hello.txt").metadata["hf"]["x_repo_commit"] == out["c1"]
    assert not list((live.root / "staging" / "uploads").iterdir())       # nothing left in staging


def test_a_second_upload_is_a_second_revision_and_the_first_still_serves_its_bytes(make_live):
    live = make_live(admin_token=ADMIN)
    big1, big2 = b"1" * 1_100_000, b"2" * 1_100_000
    out = run(live, f'''
rid = "acme/versions"
api.create_repo(rid)
c1 = api.upload_file(path_or_fileobj=b"version one", path_in_repo="notes.txt", repo_id=rid)
api.upload_file(path_or_fileobj=b"1" * 1_100_000, path_in_repo="data.bin", repo_id=rid, commit_message="v1 data")
c2 = api.upload_file(path_or_fileobj=b"version two", path_in_repo="notes.txt", repo_id=rid)
c3 = api.upload_file(path_or_fileobj=b"2" * 1_100_000, path_in_repo="data.bin", repo_id=rid)
read = lambda rev, f: open(hf_hub_download(rid, f, revision=rev)).read() if f == "notes.txt" else sha(open(hf_hub_download(rid, f, revision=rev), "rb").read())
refs = api.list_repo_refs(rid)
print(json.dumps(dict(c1=c1.oid, c2=c2.oid, c3=c3.oid,
    old_notes=read(c1.oid, "notes.txt"), mid_notes=read(c2.oid, "notes.txt"), main_notes=read("main", "notes.txt"),
    mid_data=read(c2.oid, "data.bin"), main_data=read("main", "data.bin"), main=api.model_info(rid).sha,
    refs=[(b.name, b.target_commit) for b in refs.branches], old_files=sorted(api.list_repo_files(rid, revision=c1.oid)))))
''')
    assert out["old_notes"] == "version one" and out["mid_notes"] == "version two" and out["main_notes"] == "version two"
    assert out["mid_data"] == sha256(big1) and out["main_data"] == sha256(big2)         # old LFS bytes still there next to the new
    assert out["main"] == out["c3"] and out["refs"] == [["main", out["c3"]]]
    assert out["old_files"] == ["notes.txt"]
    assert len(live.repo.list_revisions("acme/versions")) == 4
    assert blob_bytes(live, "acme/versions", out["c2"], "data.bin") == big1


def test_uploading_the_same_content_again_publishes_no_new_revision(make_live):
    live = make_live(admin_token=ADMIN)
    out = run(live, '''
rid = "acme/same"
api.create_repo(rid)
big = b"B" * 1_100_000
a = api.upload_file(path_or_fileobj=b"same", path_in_repo="a.txt", repo_id=rid)
b = api.upload_file(path_or_fileobj=b"same", path_in_repo="a.txt", repo_id=rid)
c = api.upload_file(path_or_fileobj=big, path_in_repo="big.bin", repo_id=rid)
d = api.upload_file(path_or_fileobj=big, path_in_repo="big.bin", repo_id=rid)
print(json.dumps(dict(a=a.oid, b=b.oid, c=c.oid, d=d.oid)))
''')
    assert out["a"] == out["b"] and out["c"] == out["d"] != out["a"]
    assert len(live.repo.list_revisions("acme/same")) == 2


# ---- create_repo ------------------------------------------------------------------------------------------------------------

def test_create_repo_exist_ok_and_the_errors(make_live):
    live = make_live(admin_token=ADMIN, namespace="lab")
    out = run(live, '''
first = api.create_repo("acme/twice")
again = api.create_repo("acme/twice", exist_ok=True)
conflict = status(lambda: api.create_repo("acme/twice"))
conflict_ds = status(lambda: (api.create_repo("acme/twice", repo_type="dataset"), api.create_repo("acme/twice", repo_type="dataset")))
ds_ok = api.repo_exists("acme/twice", repo_type="dataset")
space = status(lambda: api.create_repo("acme/app", repo_type="space", space_sdk="gradio"))
private = status(lambda: api.create_repo("acme/secret", private=True))
bare = api.create_repo("bare-name")                 # no organisation: the server's namespace
print(json.dumps(dict(same=str(first) == str(again), conflict=conflict, conflict_ds=conflict_ds, ds_ok=ds_ok, space=space, private=private,
                      bare=bare.repo_id, missing=api.repo_exists("acme/nope"), who=api.whoami()["name"])))
''')
    assert out["same"] is True and out["conflict"] == 409 and out["conflict_ds"] == 409 and out["ds_ok"] is True
    assert out["space"] == 400 and out["private"] == 400
    for name, org in (("has space", "acme"), ("..", "acme"), ("a--b", "acme"), (".hidden", "acme"), ("x" * 97, "acme"), ("ok", "api"), ("ok", "../x")):
        r = call(live, "POST", "/api/repos/create", ADMIN, json={"name": name, "organization": org, "type": "model"})
        assert r.status_code == 400, (name, org, r.status_code)
    assert out["bare"] == "lab/bare-name" and out["who"] == "lab"
    assert out["missing"] is False                                   # a repo that is not there is False, not an exception
    assert "acme/twice" in live.repo.list_repos() and "datasets/acme/twice" in live.repo.list_repos()
    assert not live.repo.list_revisions("acme/twice")                # created empty: no revision until something is uploaded


def test_an_empty_repository_answers_the_read_routes_sensibly(make_live):
    live = make_live(admin_token=ADMIN)
    live.repo.create_repository("acme/empty")
    live.repo.create_repository("datasets/acme/empty-ds")
    for path in ("/api/models/acme/empty", "/api/models/acme/empty/revision/main", "/api/datasets/acme/empty-ds"):
        r = httpx.get(live.url + path)
        assert r.status_code == 200 and r.json()["sha"] is None and r.json()["siblings"] == [], path
    assert httpx.get(live.url + "/api/models/acme/empty/tree/main?recursive=true").json() == []
    assert httpx.get(live.url + "/api/models/acme/empty/commits/main").json() == []
    assert httpx.get(live.url + "/api/models/acme/empty/refs").json()["branches"] == []
    assert httpx.get(live.url + "/acme/empty/resolve/main/x").status_code == 404
    gone = httpx.get(live.url + "/api/models/acme/nope")
    assert gone.status_code == 404 and gone.headers["x-error-code"] == "RepoNotFound"
    assert httpx.get(live.url + "/api/models/acme/empty/revision/dev").headers["x-error-code"] == "RevisionNotFound"
    # The GUI's library lists repos with content only, but an empty one can still be deleted (a mistaken create).
    r = httpx.post(live.url + "/api/hub/repo/delete", json={"type": "model", "id": "acme/empty"}, headers={"Authorization": f"Bearer {ADMIN}"})
    assert r.status_code == 200 and "acme/empty" not in live.repo.list_repos()


# ---- delete_file / delete_folder / delete_patterns -----------------------------------------------------------------------------

def test_delete_file_delete_folder_and_upload_folder_delete_patterns(make_live):
    live = make_live(admin_token=ADMIN)
    out = run(live, '''
rid = "acme/dels"
api.create_repo(rid)
d = Path(tempfile.mkdtemp())
for name in ("keep.txt", "old/a.txt", "old/b.txt", "logs/1.log", "logs/2.log", "deep/er/x.txt"):
    (d / name).parent.mkdir(parents=True, exist_ok=True); (d / name).write_text(name)
(d / "model.bin").write_bytes(b"M" * 1_100_000)
c1 = api.upload_folder(folder_path=str(d), repo_id=rid)
files = lambda: sorted(api.list_repo_files(rid))
start = files()
api.delete_file("logs/1.log", repo_id=rid); after_file = files()
api.delete_folder("old/", repo_id=rid); after_folder = files()                     # the trailing slash is sent as typed
d2 = Path(tempfile.mkdtemp()); (d2 / "new.txt").write_text("new")
c4 = api.upload_folder(folder_path=str(d2), repo_id=rid, delete_patterns=["logs/*", "deep/**"])      # adds new.txt, removes the matches
after_patterns = files()
api.delete_file("model.bin", repo_id=rid); after_lfs = files()
missing = None
try:
    api.delete_file("never/was/here.txt", repo_id=rid)
except RemoteEntryNotFoundError as e:
    missing = e.response.status_code
old = sorted(api.list_repo_files(rid, revision=c1.oid))
print(json.dumps(dict(start=start, after_file=after_file, after_folder=after_folder, after_patterns=after_patterns, after_lfs=after_lfs,
                      missing=missing, old=old, sha=api.model_info(rid).sha, c1=c1.oid, c4=c4.oid)))
''')
    assert out["start"] == ["deep/er/x.txt", "keep.txt", "logs/1.log", "logs/2.log", "model.bin", "old/a.txt", "old/b.txt"]
    assert "logs/1.log" not in out["after_file"] and "logs/2.log" in out["after_file"]
    assert not [p for p in out["after_folder"] if p.startswith("old/")]
    assert out["after_patterns"] == ["keep.txt", "model.bin", "new.txt"]
    assert out["after_lfs"] == ["keep.txt", "new.txt"]
    assert out["missing"] == 404 and out["old"] == out["start"]           # the first commit is untouched by the later deletions
    assert blob_bytes(live, "acme/dels", out["c1"], "model.bin") == b"M" * 1_100_000      # ... and its large file is still stored


# ---- parent commit, branches, pull requests -------------------------------------------------------------------------------------

def test_parent_commit_branches_and_pull_requests(make_live):
    live = make_live(admin_token=ADMIN)
    out = run(live, '''
rid = "acme/par"
api.create_repo(rid)
c1 = api.upload_file(path_or_fileobj=b"1", path_in_repo="a.txt", repo_id=rid)
stale = status(lambda: api.upload_file(path_or_fileobj=b"2", path_in_repo="b.txt", repo_id=rid, parent_commit="0" * 40))
good = api.upload_file(path_or_fileobj=b"2", path_in_repo="b.txt", repo_id=rid, parent_commit=c1.oid)
short = api.upload_file(path_or_fileobj=b"3", path_in_repo="c.txt", repo_id=rid, parent_commit=good.oid[:10])
branch = status(lambda: api.upload_file(path_or_fileobj=b"x", path_in_repo="x.txt", repo_id=rid, revision="dev"))
pr = status(lambda: api.upload_file(path_or_fileobj=b"x", path_in_repo="x.txt", repo_id=rid, create_pr=True))
nowhere = status(lambda: api.upload_file(path_or_fileobj=b"x", path_in_repo="x.txt", repo_id="acme/never-created"))
print(json.dumps(dict(stale=stale, branch=branch, pr=pr, nowhere=nowhere, files=sorted(api.list_repo_files(rid)), good=good.oid, short=short.oid)))
''')
    assert out["stale"] == 409 and out["branch"] == 400 and out["pr"] == 400
    assert out["nowhere"] == 404                                          # the client turns the Hub's RepoNotFound into a 404 error
    assert out["files"] == ["a.txt", "b.txt", "c.txt"] and out["short"] != out["good"]


# ---- the hf CLI ---------------------------------------------------------------------------------------------------------------------

def test_hf_upload_cli_for_a_model_folder_and_a_dataset_file(make_live, tmp_path):
    live = make_live(admin_token=ADMIN)
    folder = tmp_path / "ckpt"
    (folder / "sub").mkdir(parents=True)
    (folder / "config.json").write_text('{"n": 1}')
    (folder / "sub" / "w.safetensors").write_bytes(bytes(range(256)) * 10)
    data = tmp_path / "train.jsonl"
    data.write_text('{"text": "a"}\n{"text": "b"}\n')
    hf = Path(sys.executable).parent / "hf"
    out = run(live, f'''
import subprocess
def cli(*args):
    r = subprocess.run(["{hf}", *args], capture_output=True, text=True)
    return [r.returncode, (r.stdout + r.stderr)[-400:]]
who = cli("auth", "whoami")
m = cli("upload", "acme/cli-model", "{folder}", ".")
d = cli("upload", "acme/cli-data", "{data}", "data/train.jsonl", "--repo-type", "dataset")
again = cli("upload", "acme/cli-model", "{folder}", ".")
print(json.dumps(dict(who=who, m=m, d=d, again=again, model_files=sorted(api.list_repo_files("acme/cli-model")),
                      data_files=sorted(api.list_repo_files("acme/cli-data", repo_type="dataset")))))
''')
    assert out["who"][0] == 0 and "local" in out["who"][1], out["who"]               # `hf auth whoami` needs orgs[] in the answer
    assert out["m"][0] == 0, out["m"]
    assert out["d"][0] == 0, out["d"]
    assert out["again"][0] == 0, out["again"]                              # exist_ok create + nothing changed
    assert out["model_files"] == ["config.json", "sub/w.safetensors"] and out["data_files"] == ["data/train.jsonl"]
    assert blob_bytes(live, "acme/cli-model", "main", "sub/w.safetensors") == bytes(range(256)) * 10
    assert len(live.repo.list_revisions("acme/cli-model")) == 1           # the second `hf upload` of the same folder changed nothing


# ---- auth, read-only, the read token ---------------------------------------------------------------------------------------------------

def _ndjson(*items) -> bytes:
    return b"".join(json.dumps(i).encode() + b"\n" for i in items)


HEADER = {"key": "header", "value": {"summary": "x", "description": ""}}
WRITE_ROUTES = [
    ("POST", "/api/repos/create", {"json": {"name": "x", "type": "model"}}),
    ("POST", "/api/models/local/x/preupload/main", {"json": {"files": []}}),
    ("POST", "/api/datasets/local/x/preupload/main", {"json": {"files": []}}),
    ("POST", "/local/x.git/info/lfs/objects/batch", {"json": {"operation": "upload", "objects": []}}),
    ("POST", "/datasets/local/x.git/info/lfs/objects/batch", {"json": {"operation": "upload", "objects": []}}),
    ("PUT", f"/_lfs/{OID}?size=1", {"content": b"x"}),
    ("POST", f"/_lfs/{OID}/verify?size=1", {"json": {"oid": OID, "size": 1}}),
    ("POST", "/api/models/local/x/commit/main", {"content": _ndjson(HEADER)}),
    ("POST", "/api/datasets/local/x/commit/main", {"content": _ndjson(HEADER)}),
    ("PUT", "/api/hub/upload/file?type=model&id=local/x&path=a.txt&session=abcdef12", {"content": b"x"}),
    ("POST", "/api/hub/upload/commit", {"json": {"type": "model", "id": "local/x", "session": "abcdef12"}}),
    ("POST", "/api/hub/upload/discard", {"json": {"session": "abcdef12"}}),
]


def call(live, method, path, token=None, **kw):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.request(method, live.url + path, headers=headers, timeout=30, **kw)


@pytest.mark.parametrize("read_token", ["", "readme"])
def test_every_write_route_needs_the_admin_token_and_the_read_token_never_writes(make_live, read_token):
    live = make_live(admin_token=ADMIN, auth_token=read_token)
    tokens = [None, "wrong"] + ([read_token] if read_token else [])
    for method, path, kw in WRITE_ROUTES:
        for tok in tokens:
            r = call(live, method, path, tok, **kw)
            assert r.status_code == 401, (method, path, tok, r.status_code, r.text)
            if not path.startswith("/api/hub/"):
                assert "error" in r.json(), (path, r.text)             # the Hub's error shape (also from the read gate)
    r = call(live, "GET", "/api/whoami-v2", None)
    assert r.status_code == 401 and "error" in r.json()
    assert call(live, "GET", "/api/whoami-v2", "wrong").status_code == 401
    if read_token:
        assert call(live, "GET", "/api/whoami-v2", read_token).status_code == 401
    ok = call(live, "GET", "/api/whoami-v2", ADMIN)
    assert ok.status_code == 200 and ok.json() == {"type": "user", "id": "admin", "name": "local", "fullname": "local", "orgs": [],
                                                    "auth": {"type": "access_token", "accessToken": {"role": "write"}}}
    assert not live.repo.list_repos()                                   # nothing was created by any of the refused calls
    # The same through the client: a wrong token and the read token are refused, the admin token works.
    code = 'print(json.dumps(dict(create=status(lambda: api.create_repo("acme/x")), who=status(lambda: api.whoami()))))'
    for tok in ["wrong"] + ([read_token] if read_token else []):
        assert run(live, code, token=tok) == {"create": 401, "who": 401}


def test_the_whole_upload_flow_works_when_the_server_also_has_a_read_token(make_live):
    """MIRROR_AUTH_TOKEN set: the gate guards /api/models, /api/datasets and /datasets/..., the LFS PUT is not behind it (it carries its
    own signature). The admin token (the only one huggingface_hub can send) must get through, for a model and a dataset."""
    live = make_live(admin_token=ADMIN, auth_token="readme")
    out = run(live, MAKE_FOLDER + '''
res = {}
for rt in ("model", "dataset"):
    rid = "acme/gated-" + rt
    api.create_repo(rid, repo_type=rt)
    c = api.upload_folder(folder_path=str(d), repo_id=rid, repo_type=rt)
    p = snapshot_download(rid, repo_type=rt)
    res[rt] = dict(oid=c.oid, same=tree(p) == tree(d), sha=api.repo_info(rid, repo_type=rt).sha)
print(json.dumps(res))
''')
    for rt in ("model", "dataset"):
        assert out[rt]["same"] is True and out[rt]["sha"] == out[rt]["oid"]
    # The read token alone reads (as before) but cannot write.
    reads = run(live, 'print(json.dumps(dict(files=sorted(api.list_repo_files("acme/gated-model")), write=status(lambda: api.upload_file(path_or_fileobj=b"x", path_in_repo="z.txt", repo_id="acme/gated-model")))))',
                token="readme")
    assert "notes.txt" in reads["files"] and reads["write"] == 401
    assert "z.txt" not in [f.path for f in live.repo.list_files("acme/gated-model", "main")]


def test_read_only_refuses_every_write_with_403_but_reads_and_whoami_work(make_live):
    live = make_live(admin_token=ADMIN, read_only=True)
    for method, path, kw in WRITE_ROUTES:
        r = call(live, method, path, ADMIN, **kw)
        assert r.status_code == 403, (method, path, r.status_code)
        assert "read-only" in (r.json().get("error") or r.json().get("detail")), r.text
    assert call(live, "GET", "/api/whoami-v2", ADMIN).status_code == 200
    out = run(live, 'print(json.dumps(dict(create=status(lambda: api.create_repo("acme/x")), exists=api.repo_exists("acme/x"))))')
    assert out == {"create": 403, "exists": False}
    assert not live.repo.list_repos()


# ---- the LFS steps with a misbehaving client ---------------------------------------------------------------------------------------

def _create(live, rid="acme/lfs", kind="models"):
    r = call(live, "POST", "/api/repos/create", ADMIN, json={"name": rid.split("/")[1], "organization": rid.split("/")[0],
                                                              "type": "dataset" if kind == "datasets" else "model"})
    assert r.status_code == 200, r.text


def _batch(live, data: bytes, rid="acme/lfs", kind="models", **headers):
    oid = sha256(data)
    path = f"/{rid}.git/info/lfs/objects/batch" if kind == "models" else f"/datasets/{rid}.git/info/lfs/objects/batch"
    r = httpx.post(live.url + path, json={"operation": "upload", "transfers": ["basic"], "hash_algo": "sha256",
                                          "objects": [{"oid": oid, "size": len(data)}]},
                   headers={"Authorization": f"Bearer {ADMIN}", "Content-Type": "application/vnd.git-lfs+json", **headers})
    assert r.status_code == 200, r.text
    return oid, r


def _commit(live, items, rid="acme/lfs", kind="models", token=ADMIN):
    return call(live, "POST", f"/api/{kind}/{rid}/commit/main", token, content=_ndjson(HEADER, *items))


def test_the_lfs_batch_answers_like_git_lfs(make_live):
    live = make_live(admin_token=ADMIN)
    _create(live)
    data = b"L" * 5000
    oid, r = _batch(live, data)
    assert r.headers["content-type"].startswith("application/vnd.git-lfs+json")
    body = r.json()
    obj = body["objects"][0]
    assert body["transfer"] == "basic" and obj["oid"] == oid and obj["size"] == 5000
    up, verify = obj["actions"]["upload"]["href"], obj["actions"]["verify"]["href"]
    assert up.startswith(f"{live.url}/_lfs/{oid}?") and verify.startswith(f"{live.url}/_lfs/{oid}/verify?")      # absolute, on THIS server
    # A reverse proxy's forwarded scheme/host are used for the URLs the client will call next.
    _, r2 = _batch(live, data, **{"X-Forwarded-Proto": "https", "X-Forwarded-Host": "nas.lan:8443"})
    assert r2.json()["objects"][0]["actions"]["upload"]["href"].startswith(f"https://nas.lan:8443/_lfs/{oid}?")
    # A malformed object is reported per object, not as a failed request.
    bad = httpx.post(live.url + "/acme/lfs.git/info/lfs/objects/batch", headers={"Authorization": f"Bearer {ADMIN}"},
                     json={"operation": "upload", "objects": [{"oid": "nothex", "size": 3}]})
    assert bad.status_code == 200 and bad.json()["objects"][0]["error"]["code"] == 422
    assert httpx.post(live.url + "/acme/lfs.git/info/lfs/objects/batch", headers={"Authorization": f"Bearer {ADMIN}"},
                      json={"operation": "download", "objects": []}).status_code == 400
    # PUT then verify with the URLs it gave: 200 both; after the commit the same object needs no upload any more.
    assert httpx.put(up, content=data).status_code == 200
    assert httpx.post(verify, json={"oid": oid, "size": 5000}).status_code == 200
    assert _commit(live, [{"key": "lfsFile", "value": {"path": "w.bin", "algo": "sha256", "oid": oid, "size": 5000}}]).status_code == 200
    _, again = _batch(live, data)
    assert "actions" not in again.json()["objects"][0]


def test_a_hash_mismatch_at_verify_publishes_nothing_and_stores_no_blob(make_live):
    live = make_live(admin_token=ADMIN)
    _create(live)
    announced = b"the bytes the client promised"
    sent = b"the bytes the client sends!!!"                       # same length, other content
    assert len(announced) == len(sent)
    oid, r = _batch(live, announced)
    acts = r.json()["objects"][0]["actions"]
    assert httpx.put(acts["upload"]["href"], content=sent).status_code == 200           # the PUT cannot know yet
    v = httpx.post(acts["verify"]["href"], json={"oid": oid, "size": len(announced)})
    assert v.status_code == 422 and "mismatch" in v.json()["error"]
    # The refused bytes are gone, and a client that commits anyway is refused too: nothing is published, nothing is stored.
    assert not (live.root / "staging" / "uploads" / oid).exists()
    c = _commit(live, [{"key": "lfsFile", "value": {"path": "w.bin", "algo": "sha256", "oid": oid, "size": len(announced)}}])
    assert c.status_code == 400
    assert live.repo.list_revisions("acme/lfs") == [] and list(live.repo.iter_blobs()) == []
    assert not list((live.root / "staging" / "uploads").iterdir())


def test_a_commit_that_skips_verify_is_hashed_at_commit_time_and_a_wrong_object_is_refused(make_live):
    live = make_live(admin_token=ADMIN)
    _create(live)
    announced, sent = b"A" * 100, b"B" * 100
    oid, r = _batch(live, announced)
    assert httpx.put(r.json()["objects"][0]["actions"]["upload"]["href"], content=sent).status_code == 200      # no verify call
    c = _commit(live, [{"key": "lfsFile", "value": {"path": "w.bin", "algo": "sha256", "oid": oid, "size": 100}}])
    assert c.status_code == 422 and "hash" in c.json()["error"]
    assert live.repo.list_revisions("acme/lfs") == [] and list(live.repo.iter_blobs()) == []
    # And an object that was never uploaded at all.
    c = _commit(live, [{"key": "lfsFile", "value": {"path": "w.bin", "algo": "sha256", "oid": sha256(b"never sent"), "size": 10}}])
    assert c.status_code == 400 and "not uploaded" in c.json()["error"]


def test_a_large_file_is_hashed_once_while_it_arrives_and_never_trusted_after_it_changed(make_live, monkeypatch):
    """verify and commit use the hash recorded during the PUT (no second pass over a 50 GB file); the record is only valid for the exact
    file it was made for, so bytes changed on disk afterwards are caught at commit."""
    import server.hub.upload as upload
    live = make_live(admin_token=ADMIN)
    _create(live)
    data = bytes(range(256)) * 40
    oid, r = _batch(live, data)
    acts = r.json()["objects"][0]["actions"]
    staged = live.root / "staging" / "uploads" / oid

    def no_second_read(path):
        raise AssertionError("the staged file was read again")

    monkeypatch.setattr(upload, "sha256_file", no_second_read)
    assert httpx.put(acts["upload"]["href"], content=data).status_code == 200
    assert staged.with_name(oid + ".sha256").read_text().split("\n")[0] == oid
    assert httpx.post(acts["verify"]["href"], json={"oid": oid, "size": len(data)}).status_code == 200
    item = {"key": "lfsFile", "value": {"path": "w.bin", "algo": "sha256", "oid": oid, "size": len(data)}}
    assert _commit(live, [item]).status_code == 200                                     # still no second read
    assert blob_bytes(live, "acme/lfs", "main", "w.bin") == data
    assert not list((live.root / "staging" / "uploads").iterdir())                      # staged file and its hash record are gone

    # Without the record (a crash between rename and record) the file is hashed once, at verify.
    monkeypatch.undo()
    other = b"Z" * 500
    oid2, r2 = _batch(live, other)
    acts2 = r2.json()["objects"][0]["actions"]
    httpx.put(acts2["upload"]["href"], content=other)
    (live.root / "staging" / "uploads" / (oid2 + ".sha256")).unlink()
    assert httpx.post(acts2["verify"]["href"], json={"oid": oid2, "size": 500}).status_code == 200
    # Bytes replaced on disk after verification: the record no longer fits the file, the commit hashes again and refuses.
    (live.root / "staging" / "uploads" / oid2).write_bytes(b"Y" * 500)
    c = _commit(live, [{"key": "lfsFile", "value": {"path": "z.bin", "algo": "sha256", "oid": oid2, "size": 500}}])
    assert c.status_code == 422
    assert [f.path for f in live.repo.list_files("acme/lfs", "main")] == ["w.bin"]


def test_the_lfs_put_refuses_wrong_sizes_bad_signatures_and_expired_urls(make_live):
    from server.hub.upload import sign
    live = make_live(admin_token=ADMIN)
    _create(live)
    data = b"D" * 3000
    oid, r = _batch(live, data)
    href = r.json()["objects"][0]["actions"]["upload"]["href"]
    assert httpx.put(href, content=data[:-1]).status_code == 400                     # shorter than announced
    assert httpx.put(href, content=data + b"x").status_code == 400                   # longer than announced
    assert not [p for p in (live.root / "staging" / "uploads").iterdir()]            # neither left a file behind
    assert httpx.put(href.rsplit("=", 1)[0] + "=" + "0" * 64, content=data).status_code == 401           # forged signature
    assert httpx.put(href.replace("size=3000", "size=3001"), content=data).status_code == 401             # signed for another size
    assert httpx.put(live.url + f"/_lfs/{sha256(b'other')}" + href[href.index("?"):], content=data).status_code == 401   # signed for another object
    past = int(time.time()) - 5
    expired = f"{live.url}/_lfs/{oid}?size=3000&exp={past}&sig={sign(ADMIN, 'put', oid, 3000, past)}"
    assert httpx.put(expired, content=data).status_code == 401
    verify_href = r.json()["objects"][0]["actions"]["verify"]["href"]
    assert httpx.put(verify_href.replace("/verify?", "?"), content=data).status_code == 401              # a verify URL is not a PUT URL
    assert httpx.put(href, content=data).status_code == 200
    # The admin token in a header also opens the PUT (clients that do send it), the read token does not.
    assert httpx.put(f"{live.url}/_lfs/{oid}?size=3000", content=data, headers={"Authorization": f"Bearer {ADMIN}"}).status_code == 200


def test_commit_protocol_edge_cases(make_live):
    live = make_live(admin_token=ADMIN)
    _create(live)
    file_item = lambda path, data: {"key": "file", "value": {"path": path, "encoding": "base64", "content": base64.b64encode(data).decode()}}
    assert _commit(live, [{"key": "copyFile", "value": {"path": "b", "srcPath": "a"}}]).status_code == 400
    assert _commit(live, [{"key": "mystery", "value": {}}]).status_code == 400
    for bad in ("../x", "/abs", "a//b", "a/./b", ".git/config", "a\\b", ""):
        assert _commit(live, [file_item(bad, b"x")]).status_code == 400, bad
    assert call(live, "POST", "/api/models/acme/lfs/commit/main", ADMIN, content=b"not json\n").status_code == 400
    assert call(live, "POST", "/api/models/acme/lfs/commit/main", ADMIN, content=_ndjson(file_item("a", b"x"))).status_code == 400   # no header first
    assert _commit(live, [], rid="acme/none").status_code == 404
    assert live.repo.list_revisions("acme/lfs") == []
    # A commit that changes nothing is a success that returns the head and publishes no second revision.
    r1 = _commit(live, [file_item("a.txt", b"hello"), file_item("empty.txt", b"")])
    assert r1.status_code == 200 and r1.json()["success"] is True and r1.json()["hookOutput"] == ""
    head = r1.json()["commitOid"]
    assert len(head) == 40 and r1.json()["commitUrl"] == f"{live.url}/acme/lfs/commit/{head}"
    r2 = _commit(live, [file_item("a.txt", b"hello")])
    assert r2.status_code == 200 and r2.json()["commitOid"] == head and live.repo.list_revisions("acme/lfs") == [head]
    # deletedFolder with and without the trailing slash the client passes through as typed.
    _commit(live, [file_item("d/x.txt", b"x"), file_item("e/y.txt", b"y")])
    assert _commit(live, [{"key": "deletedFolder", "value": {"path": "d"}}, {"key": "deletedFolder", "value": {"path": "e/"}}]).status_code == 200
    assert [f.path for f in live.repo.list_files("acme/lfs", "main")] == ["a.txt", "empty.txt"]
    # A file and a folder cannot share a name.
    assert _commit(live, [file_item("a.txt/inner", b"x")]).status_code == 400
    # utf-8 encoded content is accepted too.
    r3 = _commit(live, [{"key": "file", "value": {"path": "u.txt", "encoding": "utf-8", "content": "grüße"}}])
    assert r3.status_code == 200 and blob_bytes(live, "acme/lfs", "main", "u.txt") == "grüße".encode()


def test_preupload_modes_and_the_oid_of_what_is_already_there(make_live):
    live = make_live(admin_token=ADMIN)
    _create(live)
    files = [("a.json", b"{}"), ("m.safetensors", b"tiny"), ("m.gguf", b"g"), ("d.parquet", b"p"), ("a.tar.gz", b"z"), ("big.txt", b"b" * (1 << 20) + b"!"),
             ("edge.txt", b"e" * (1 << 20))]

    def pre(rid="acme/lfs"):
        body = {"files": [{"path": p, "size": len(d), "sample": base64.b64encode(d[:512]).decode()} for p, d in files]}
        r = call(live, "POST", f"/api/models/{rid}/preupload/main", ADMIN, json=body)
        assert r.status_code == 200, r.text
        return r.json()

    first = pre()
    modes = {f["path"]: f["uploadMode"] for f in first["files"]}
    assert modes == {"a.json": "regular", "m.safetensors": "lfs", "m.gguf": "lfs", "d.parquet": "lfs", "a.tar.gz": "lfs",
                     "big.txt": "lfs", "edge.txt": "regular"}                      # > 1 MiB is LFS, exactly 1 MiB is not
    assert all(f["shouldIgnore"] is False and "oid" not in f for f in first["files"]) and first["commitOid"] is None
    item = lambda p, d: {"key": "file", "value": {"path": p, "encoding": "base64", "content": base64.b64encode(d).decode()}}
    up = [item("a.json", b"{}")]
    oid, r = _batch(live, files[1][1])
    httpx.put(r.json()["objects"][0]["actions"]["upload"]["href"], content=files[1][1])
    up.append({"key": "lfsFile", "value": {"path": "m.safetensors", "algo": "sha256", "oid": oid, "size": 4}})
    head = _commit(live, up).json()["commitOid"]
    second = {f["path"]: f for f in pre()["files"]}
    assert pre()["commitOid"] == head
    assert second["a.json"]["oid"] == hashlib.sha1(b"blob 2\0{}").hexdigest()      # regular: git blob sha1 of what is stored
    assert second["m.safetensors"]["oid"] == oid                                     # LFS: sha256
    assert "oid" not in second["m.gguf"]                                             # nothing there yet
    assert call(live, "POST", "/api/models/acme/lfs/preupload/dev", ADMIN, json={"files": []}).status_code == 400
    inj = call(live, "POST", "/api/models/acme/x%0d%0aSet-Cookie:%20a=b/preupload/main", ADMIN, json={"files": []})
    assert inj.status_code == 404 and "set-cookie" not in inj.headers and "\n" not in inj.headers["x-error-message"]
    assert call(live, "POST", "/api/models/acme/none/preupload/main", ADMIN, json={"files": []}).status_code == 404


# ---- garbage collection and the store lock ---------------------------------------------------------------------------------------------

def test_gc_after_uploads_deletes_nothing_a_revision_references(make_live):
    from ingester import jobs
    live = make_live(admin_token=ADMIN)
    out = run(live, MAKE_FOLDER + '''
api.create_repo("acme/gc"); api.create_repo("acme/gc-ds", repo_type="dataset")
api.upload_folder(folder_path=str(d), repo_id="acme/gc")
(d / "notes.txt").write_bytes(b"y" * 1_200_000)                           # replaces an LFS file: the old blob is now referenced only by the first revision
api.upload_folder(folder_path=str(d), repo_id="acme/gc")
api.upload_folder(folder_path=str(d), repo_id="acme/gc-ds", repo_type="dataset")
print(json.dumps(tree(d)))
''')
    before = set(live.repo.iter_blobs())
    assert len(before) >= 6
    orphan = live.repo.put_blob_bytes(b"nobody references me").digest         # positive control: GC does delete what no manifest names
    report = jobs.gc(live.repo, min_age_seconds=0)
    assert report.acted == 1 and orphan not in set(live.repo.iter_blobs())
    assert set(live.repo.iter_blobs()) == before
    assert live.repo.collect_reachable_blobs() == before
    dl = run(live, 'p = snapshot_download("acme/gc"); q = snapshot_download("acme/gc-ds", repo_type="dataset"); print(json.dumps([tree(p), tree(q)]))')
    assert dl[0] == dl[1] == out                                              # everything still downloads, byte for byte


def test_a_commit_waits_for_a_running_gc_instead_of_racing_it(make_live):
    from ingester import jobs
    live = make_live(admin_token=ADMIN)
    _create(live)
    item = {"key": "file", "value": {"path": "a.txt", "encoding": "base64", "content": base64.b64encode(b"waiting").decode()}}
    result = {}

    def post():
        result["r"] = _commit(live, [item])

    with live.repo.gc_lock():                                                  # a GC (or a repo delete) is running: it holds the lock exclusively
        t = threading.Thread(target=post)
        t.start()
        time.sleep(1.0)
        assert t.is_alive() and live.repo.list_revisions("acme/lfs") == []     # the commit is waiting, it stored and published nothing
    t.join(timeout=30)
    assert not t.is_alive() and result["r"].status_code == 200
    assert blob_bytes(live, "acme/lfs", "main", "a.txt") == b"waiting"
    assert jobs.gc(live.repo, min_age_seconds=0).acted == 0                    # and what it stored is referenced


# ---- the GUI's web upload ---------------------------------------------------------------------------------------------------------------

def web(live, method, path, token=ADMIN, **kw):
    return call(live, method, "/api/hub/upload" + path, token, **kw)


def test_web_upload_two_files_commit_and_read_them_back_through_the_hub_routes(make_live):
    live = make_live(admin_token=ADMIN)
    big = bytes(range(256)) * 5000
    q = "type=model&id=acme/web&session=sess-0001"
    assert web(live, "PUT", f"/file?{q}&path=README.md", content=b"# from the browser\n").json() == {"path": "README.md", "size": 19}
    assert web(live, "PUT", f"/file?{q}&path=weights/model.bin", content=big).status_code == 200
    assert web(live, "PUT", f"/file?{q}&path=weights/model.bin", content=big).status_code == 200       # same path again replaces it
    assert (live.root / "staging" / "uploads" / "web-sess-0001" / "weights" / "model.bin").is_file()
    r = web(live, "POST", "/commit", json={"type": "model", "id": "acme/web", "session": "sess-0001", "summary": "first import"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True and body["files"] == 2 and body["id"] == "acme/web" and len(body["commit"]) == 40
    assert not (live.root / "staging" / "uploads" / "web-sess-0001").exists()                          # the session is gone
    out = run(live, '''
p = snapshot_download("acme/web")
print(json.dumps(dict(files=tree(p), sha=api.model_info("acme/web").sha, listed=sorted(api.list_repo_files("acme/web")))))
''')
    assert out["files"] == {"README.md": sha256(b"# from the browser\n"), "weights/model.bin": sha256(big)}
    assert out["sha"] == body["commit"] and out["listed"] == ["README.md", "weights/model.bin"]
    assert live.repo.get_manifest("acme/web", "main").source.kind == "upload"
    # A second session adds one file and overwrites another: ONE new revision with the old files carried over.
    q2 = "type=model&id=acme/web&session=sess-0002"
    web(live, "PUT", f"/file?{q2}&path=README.md", content=b"# v2\n")
    web(live, "PUT", f"/file?{q2}&path=extra.json", content=b"{}")
    r2 = web(live, "POST", "/commit", json={"type": "model", "id": "acme/web", "session": "sess-0002"}).json()
    assert r2["files"] == 3 and len(live.repo.list_revisions("acme/web")) == 2
    assert blob_bytes(live, "acme/web", "main", "README.md") == b"# v2\n" and blob_bytes(live, "acme/web", "main", "weights/model.bin") == big
    assert blob_bytes(live, "acme/web", body["commit"], "README.md") == b"# from the browser\n"
    # replace=true: the session is the whole new content.
    web(live, "PUT", f"/file?type=model&id=acme/web&session=sess-0003&path=only.txt", content=b"only")
    r3 = web(live, "POST", "/commit", json={"type": "model", "id": "acme/web", "session": "sess-0003", "replace": True}).json()
    assert [f.path for f in live.repo.list_files("acme/web", "main")] == ["only.txt"] and r3["files"] == 1
    # Committing a session that no longer exists, or that holds nothing, is refused; a dataset (new repo) is created by its first commit.
    assert web(live, "POST", "/commit", json={"type": "model", "id": "acme/web", "session": "sess-0003"}).status_code == 404
    web(live, "PUT", "/file?type=dataset&id=acme/web-ds&session=sess-0004&path=data/train.jsonl", content=b'{"a": 1}\n')
    assert web(live, "POST", "/commit", json={"type": "dataset", "id": "acme/web-ds", "session": "sess-0004"}).status_code == 200
    ds = run(live, 'print(json.dumps(tree(snapshot_download("acme/web-ds", repo_type="dataset"))))')
    assert ds == {"data/train.jsonl": sha256(b'{"a": 1}\n')}
    # Discard removes the session's files; nothing is published.
    web(live, "PUT", "/file?type=model&id=acme/web&session=sess-0005&path=junk.txt", content=b"junk")
    assert web(live, "POST", "/discard", json={"session": "sess-0005"}).status_code == 200
    assert not (live.root / "staging" / "uploads" / "web-sess-0005").exists()
    assert web(live, "POST", "/commit", json={"type": "model", "id": "acme/web", "session": "sess-0005"}).status_code == 404
    assert web(live, "POST", "/discard", json={"session": "sess-0005"}).status_code == 200            # idempotent


def test_web_upload_refuses_path_traversal_and_bad_names(make_live):
    live = make_live(admin_token=ADMIN)
    base = "type=model&id=acme/web&session=sess-1111"
    uploads = live.root / "staging" / "uploads"
    for bad in ("../escape.txt", "a/../../escape.txt", "/etc/passwd", "a//b.txt", "./a.txt", "a/./b.txt", ".hidden", "dir/.hidden/x",
                "..", ".", "", "a\\b.txt", "x/", "%2e%2e/escape.txt", "..%2fescape.txt", ".meta.json", ".partial-abc"):
        r = web(live, "PUT", f"/file?{base}&path={bad}", content=b"evil")
        assert r.status_code == 400, (bad, r.status_code, r.text)
    for bad_session in ("../x", "a/b", "", "x" * 65, "has space"):
        assert web(live, "PUT", f"/file?type=model&id=acme/web&session={bad_session}&path=a.txt", content=b"x").status_code in (400, 404, 422), bad_session
    for bad_id in ("../../x", "a/b/c", "acme/.hidden", "acme/has space", "api/x", ""):
        r = web(live, "PUT", f"/file?type=model&id={bad_id}&session=sess-1112&path=a.txt", content=b"x")
        assert r.status_code == 400, (bad_id, r.status_code)
    assert web(live, "PUT", f"/file?type=space&id=acme/web&session=sess-1113&path=a.txt", content=b"x").status_code == 400
    # Nothing was written anywhere outside the session directories, and no session holds a file.
    assert not (live.root / "escape.txt").exists() and not (live.root / "staging" / "escape.txt").exists() and not (uploads / "escape.txt").exists()
    assert not [p for p in live.root.rglob("escape.txt")]
    assert not [p for s in uploads.glob("web-*") for p in s.rglob("*") if p.is_file() and p.name != ".meta.json"]
    # A file and a folder of the same name collide cleanly.
    assert web(live, "PUT", f"/file?{base}&path=a", content=b"1").status_code == 200
    assert web(live, "PUT", f"/file?{base}&path=a/b", content=b"2").status_code == 409
    # A session belongs to one repository.
    assert web(live, "PUT", "/file?type=model&id=acme/other&session=sess-1111&path=b.txt", content=b"1").status_code == 409
    assert web(live, "POST", "/commit", json={"type": "model", "id": "acme/other", "session": "sess-1111"}).status_code == 409
    assert web(live, "POST", "/discard", json={"session": "sess-1111"}).status_code == 200
    assert live.repo.list_repos() == []


def test_web_upload_of_a_large_file_is_streamed_not_buffered(make_live):
    """5 MiB in 4 KiB pieces through a generator: the server never needs the whole body (it takes it as it arrives)."""
    live = make_live(admin_token=ADMIN)
    pieces = [bytes([i % 251]) * 4096 for i in range(1280)]
    r = httpx.put(live.url + "/api/hub/upload/file?type=model&id=acme/stream&session=sess-2222&path=big.bin", content=iter(pieces),
                  headers={"Authorization": f"Bearer {ADMIN}"}, timeout=60)
    assert r.status_code == 200 and r.json()["size"] == 4096 * 1280
    assert web(live, "POST", "/commit", json={"type": "model", "id": "acme/stream", "session": "sess-2222"}).status_code == 200
    assert blob_bytes(live, "acme/stream", "main", "big.bin") == b"".join(pieces)


def test_concurrent_commits_to_one_repository_do_not_lose_each_others_files(make_live):
    live = make_live(admin_token=ADMIN)
    _create(live, "acme/race")
    n, results = 8, []

    def one(i):
        item = {"key": "file", "value": {"path": f"f{i}.txt", "encoding": "base64", "content": base64.b64encode(f"file {i}".encode()).decode()}}
        results.append(_commit(live, [item], rid="acme/race").status_code)

    threads = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert results == [200] * n
    assert sorted(f.path for f in live.repo.list_files("acme/race", "main")) == sorted(f"f{i}.txt" for i in range(n))     # each commit started from the previous head
    assert len(live.repo.list_revisions("acme/race")) == n
