"""`repo ingest-local` against the REAL huggingface_hub client.

Found on 2026-11-04 by looking at what the client wrote, not by the unit tests: every file of a locally ingested repo carried the
same ETag (`local-<commit>`), huggingface_hub names its cache blobs by ETag, so `snapshot_download` returned ONE file's bytes under
every file name. Also: the blob store moved the user's source files away, and the synthetic commit ignored file contents.
"""
from __future__ import annotations

import hashlib
import json
import urllib.request

import pytest

from ingester import cli
from server.adapters.hf_adapter import _hf_compat_etag

from .conftest import FILES, stage_repo

SNAP_HASHES = """
import hashlib, json, os
from huggingface_hub import snapshot_download
p = snapshot_download("{repo}")
print(json.dumps({os.path.relpath(os.path.join(r, f), p): hashlib.sha256(open(os.path.join(r, f), "rb").read()).hexdigest()
                  for r, _, fs in os.walk(p) for f in fs}))
"""

SRC = {"config.json": b'{"model_type": "tiny"}\n', "tokenizer.json": b"t" * 700,
       "model.safetensors": bytes(range(256)) * 400, "sub/notes.txt": b"nested\n"}


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def client_hashes(live, repo: str) -> dict[str, str]:
    r = live.client(SNAP_HASHES.replace("{repo}", repo))
    assert r.returncode == 0, r.stderr[-800:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def make_source(tmp_path, files=SRC, name="src"):
    src = tmp_path / name
    for rel, data in files.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_bytes(data)
    return src


@pytest.fixture
def into(monkeypatch):
    def _into(live):
        monkeypatch.setenv("MIRROR_ROOT", str(live.root))
        monkeypatch.setenv("MIRROR_INDEX_BACKEND", "none")
    return _into


def test_every_file_of_a_local_ingest_comes_back_with_its_own_bytes(make_live, tmp_path, into):
    live = make_live(); into(live)
    src = make_source(tmp_path)
    assert cli.main(["ingest-local", "acme/local-model", str(src)]) == 0
    assert client_hashes(live, "acme/local-model") == {rel: sha(data) for rel, data in SRC.items()}


def test_files_of_one_repo_get_different_etags(make_live, tmp_path, into):
    live = make_live(); into(live)
    assert cli.main(["ingest-local", "acme/local-model", str(make_source(tmp_path))]) == 0
    etags = {}
    for rel in SRC:
        req = urllib.request.Request(f"{live.url}/acme/local-model/resolve/main/{rel}", method="HEAD")
        with urllib.request.urlopen(req) as resp:
            etags[rel] = resp.headers["ETag"]
    assert len(set(etags.values())) == len(SRC), etags
    assert not any(e.startswith("local-") for e in etags.values())


def test_a_store_written_with_the_old_shared_etag_is_served_correctly(make_live):
    """The NAS may hold repos written before the fix: the server must not echo the shared etag."""
    live = make_live()
    stage_repo(live.repo, etag="local-0123456789abcdef")
    assert client_hashes(live, "acme/tiny") == {rel: sha(data) for rel, data in FILES.items()}


def test_etag_helper_ignores_the_old_synthetic_value():
    class M:
        digest = "ab" * 32
        metadata = {"hf": {"etag": "local-0123456789abcdef"}}
    assert _hf_compat_etag(M()) == f'"{"ab" * 16}-1000"'
    M.metadata = {"hf": {"etag": '"real-upstream-etag"'}}
    assert _hf_compat_etag(M()) == '"real-upstream-etag"'


def test_ingest_local_copies_by_default_and_leaves_the_source_alone(make_live, tmp_path, into):
    live = make_live(); into(live)
    src = make_source(tmp_path)
    assert cli.main(["ingest-local", "acme/local-model", str(src)]) == 0
    assert {p.relative_to(src).as_posix(): p.read_bytes() for p in src.rglob("*") if p.is_file()} == SRC
    assert not list((live.root / "staging").glob("local-*")), "the staging copy is cleaned up"


def test_ingest_local_move_consumes_the_source(make_live, tmp_path, into):
    live = make_live(); into(live)
    src = make_source(tmp_path)
    assert cli.main(["ingest-local", "acme/local-model", str(src), "--move"]) == 0
    assert not [p for p in src.rglob("*") if p.is_file()]
    assert client_hashes(live, "acme/local-model") == {rel: sha(data) for rel, data in SRC.items()}


def test_a_changed_file_becomes_a_new_revision(make_live, tmp_path, into):
    live = make_live(); into(live)
    src = make_source(tmp_path)
    assert cli.main(["ingest-local", "acme/local-model", str(src)]) == 0
    first = live.repo.resolve_ref("acme/local-model", "main")
    # same size, different content: only the mtime (and content) tell the two apart
    (src / "config.json").write_bytes(b'{"model_type": "TINY"}\n')
    assert cli.main(["ingest-local", "acme/local-model", str(src)]) == 0
    got = client_hashes(live, "acme/local-model")
    assert got["config.json"] == sha(b'{"model_type": "TINY"}\n')
    assert live.repo.resolve_ref("acme/local-model", "main") != first
