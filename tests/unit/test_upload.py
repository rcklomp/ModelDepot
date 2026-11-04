"""The upload logic without HTTP: names and paths, the signed LFS URLs, publish_commit, and its place next to GC."""
from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from pathlib import Path

import pytest

from server.hub.upload import (
    Addition, Op, UploadError, check_signature, git_blob_sha1, is_lfs, locate, publish_commit, sign, validate_repo_name,
    validate_repo_path)


def add_op(storage, path: str, data: bytes, name: str | None = None) -> Op:
    """A staged regular file, the way the commit reader makes one."""
    src = Path(storage.staging_dir) / (name or hashlib.md5(path.encode()).hexdigest())
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(data)
    return Op("add", path, Addition(path=path, sha256=hashlib.sha256(data).hexdigest(), size=len(data), src=src,
                                    git_sha1=git_blob_sha1(data)))


def files_of(repo, cid, rev="main") -> dict[str, bytes]:
    return {f.path: repo.open_blob(f.blob.split(":", 1)[1]).read() for f in repo.list_files(cid, rev)}


# ---- names, paths, the large-file rule --------------------------------------------------------------------------------------

def test_the_large_file_rule():
    assert is_lfs("a.txt", (1 << 20) + 1) and not is_lfs("a.txt", 1 << 20) and not is_lfs("a.json", 10)
    for name in ("m.safetensors", "m.gguf", "m.bin", "m.pt", "m.pth", "m.ckpt", "m.onnx", "d.parquet", "d.arrow", "m.h5", "m.npz", "a.zip",
                 "a.tar", "a.tar.gz", "a.zst", "UPPER.SAFETENSORS"):
        assert is_lfs(name, 1), name


@pytest.mark.parametrize("path", ["a", "a/b/c.txt", "dir/with.dots/f.tar.gz", ".gitattributes", "a/.hidden/x", "ünï.txt"])
def test_good_repo_paths(path):
    assert validate_repo_path(path) == path


@pytest.mark.parametrize("path", ["", "/abs", "a/", "a//b", ".", "..", "a/./b", "a/../b", "../a", "a\\b", "a\0b", "a\nb", ".git/config", "x" * 300,
                                  "a/" + "y" * 256, None, 5])
def test_bad_repo_paths(path):
    with pytest.raises(UploadError) as e:
        validate_repo_path(path)
    assert e.value.status == 400


def test_the_browser_door_refuses_every_dot_segment():
    for path in (".hidden", "a/.hidden", "a/.b/c", ".meta.json"):
        with pytest.raises(UploadError):
            validate_repo_path(path, allow_dot_segments=False)
    assert validate_repo_path("a/b.c/d", allow_dot_segments=False) == "a/b.c/d"


def test_new_repository_names():
    validate_repo_name("acme", "Llama-3.2_1B.v2")
    for ns, name in (("acme", ""), ("acme", "a b"), ("acme", "-x"), ("acme", ".x"), ("acme", "a..b"), ("acme", "a--b"), ("acme", "x" * 97),
                     ("", "x"), ("api", "x"), ("Datasets", "x"), ("a/b", "x"), ("acme", "a/b"), ("acme", None)):
        with pytest.raises(UploadError):
            validate_repo_name(ns, name)


def test_locate_gives_the_core_id_and_whether_it_exists(repo):
    assert locate(repo, "lab", "model", "acme/x") == ("acme/x", False)
    assert locate(repo, "lab", "dataset", "acme/x") == ("datasets/acme/x", False)
    assert locate(repo, "lab", "model", "bare") == ("lab/bare", False)             # a bare name gets the namespace ...
    repo.create_repository("bare")
    assert locate(repo, "lab", "model", "bare") == ("bare", True)                  # ... unless a repo of that name exists (legacy ids)
    repo.create_repository("acme/x")
    assert locate(repo, "lab", "model", "acme/x") == ("acme/x", True)
    for args in (("space", "acme/x"), ("kernel", "acme/x"), ("model", "a/b/c"), ("model", ""), ("model", "../x"), ("model", "acme/.x")):
        with pytest.raises(UploadError):
            locate(repo, "lab", *args)


# ---- the signed LFS URLs ----------------------------------------------------------------------------------------------------------

def test_signature_binds_purpose_object_size_expiry_and_secret():
    oid, exp = "a" * 64, int(time.time()) + 60
    sig = sign("secret", "put", oid, 10, exp)
    assert check_signature("secret", "put", oid, 10, exp, sig)
    assert not check_signature("secret", "verify", oid, 10, exp, sig)               # another purpose
    assert not check_signature("secret", "put", "b" * 64, 10, exp, sig)             # another object
    assert not check_signature("secret", "put", oid, 11, exp, sig)                  # another size
    assert not check_signature("secret", "put", oid, 10, exp + 1, sig)              # extended lifetime
    assert not check_signature("other", "put", oid, 10, exp, sig)                   # another admin token
    assert not check_signature("secret", "put", oid, 10, exp, "") and not check_signature("secret", "put", oid, 10, exp, None)
    assert not check_signature("", "put", oid, 10, exp, sign("", "put", oid, 10, exp))   # no secret, no access
    past = int(time.time()) - 1
    assert not check_signature("secret", "put", oid, 10, past, sign("secret", "put", oid, 10, past))


# ---- publish_commit ------------------------------------------------------------------------------------------------------------

def test_a_commit_is_the_head_plus_its_changes_and_old_revisions_stay(repo, storage):
    repo.create_repository("acme/x")
    r1 = publish_commit(repo, "acme/x", [add_op(storage, "a.txt", b"A1"), add_op(storage, "d/b.txt", b"B1")], summary="one")
    assert r1.created and r1.parent is None and files_of(repo, "acme/x") == {"a.txt": b"A1", "d/b.txt": b"B1"}
    r2 = publish_commit(repo, "acme/x", [add_op(storage, "a.txt", b"A2"), add_op(storage, "c.txt", b"C")], summary="two")
    assert r2.created and r2.parent == r1.commit and r2.commit != r1.commit
    assert files_of(repo, "acme/x") == {"a.txt": b"A2", "d/b.txt": b"B1", "c.txt": b"C"}
    assert files_of(repo, "acme/x", r1.commit) == {"a.txt": b"A1", "d/b.txt": b"B1"}
    r3 = publish_commit(repo, "acme/x", [Op("delete_folder", "d"), Op("delete_file", "c.txt"), add_op(storage, "c.txt", b"C again")],
                        summary="ops run in order")                                  # deleted, then added again
    assert files_of(repo, "acme/x") == {"a.txt": b"A2", "c.txt": b"C again"}
    m = repo.get_manifest("acme/x", "main")
    assert m.revision.commit == r3.commit and {f.metadata["hf"]["x_repo_commit"] for f in m.files} == {r3.commit}
    assert repo.list_references("acme/x") == {"main": r3.commit} and len(repo.list_revisions("acme/x")) == 3


def test_a_commit_that_changes_nothing_publishes_nothing(repo, storage):
    repo.create_repository("acme/x")
    r1 = publish_commit(repo, "acme/x", [add_op(storage, "a.txt", b"A")], summary="one")
    same = add_op(storage, "a.txt", b"A", name="again")
    r2 = publish_commit(repo, "acme/x", [same], summary="the same")
    assert not r2.created and r2.commit == r1.commit and repo.list_revisions("acme/x") == [r1.commit]
    assert not same.addition.src.exists()                                            # the redundant staged copy was dropped
    r3 = publish_commit(repo, "acme/x", [], summary="nothing at all")
    assert not r3.created and r3.commit == r1.commit
    # ... but a first commit to an empty repository is a revision even when it is empty.
    repo.create_repository("acme/y")
    r4 = publish_commit(repo, "acme/y", [], summary="init")
    assert r4.created and files_of(repo, "acme/y") == {}


def test_deleting_what_is_not_there_is_a_404_and_changes_nothing(repo, storage):
    repo.create_repository("acme/x")
    publish_commit(repo, "acme/x", [add_op(storage, "a.txt", b"A")], summary="one")
    for op in (Op("delete_file", "nope.txt"), Op("delete_folder", "nope"), Op("delete_file", "a.txt/inner")):
        with pytest.raises(UploadError) as e:
            publish_commit(repo, "acme/x", [add_op(storage, "z.txt", b"Z"), op], summary="x")
        assert e.value.status == 404
    assert files_of(repo, "acme/x") == {"a.txt": b"A"} and len(repo.list_revisions("acme/x")) == 1
    # Nothing stored for the refused commits either: only the first commit's blob exists.
    assert len(list(repo.iter_blobs())) == 1


def test_the_parent_commit_must_be_the_head(repo, storage):
    repo.create_repository("acme/x")
    with pytest.raises(UploadError) as e:
        publish_commit(repo, "acme/x", [add_op(storage, "a.txt", b"A")], summary="x", parent="f" * 40)      # an empty repo has no parent
    assert e.value.status == 409
    r1 = publish_commit(repo, "acme/x", [add_op(storage, "a.txt", b"A")], summary="one")
    for parent in ("0" * 40, "abcdef0"):
        with pytest.raises(UploadError) as e:
            publish_commit(repo, "acme/x", [add_op(storage, "b.txt", b"B")], summary="x", parent=parent)
        assert e.value.status == 409
    assert publish_commit(repo, "acme/x", [add_op(storage, "b.txt", b"B")], summary="x", parent=r1.commit[:7]).created


def test_a_file_cannot_become_a_folder_or_the_reverse(repo, storage):
    repo.create_repository("acme/x")
    publish_commit(repo, "acme/x", [add_op(storage, "a", b"file")], summary="one")
    with pytest.raises(UploadError) as e:
        publish_commit(repo, "acme/x", [add_op(storage, "a/b", b"x")], summary="x")
    assert e.value.status == 400


def test_keep_only_is_the_replace_mode(repo, storage):
    repo.create_repository("acme/x")
    publish_commit(repo, "acme/x", [add_op(storage, "old.txt", b"o"), add_op(storage, "keep.txt", b"k")], summary="one")
    publish_commit(repo, "acme/x", [add_op(storage, "keep.txt", b"k2"), add_op(storage, "new.txt", b"n")], summary="two",
                   keep_only={"keep.txt", "new.txt"})
    assert files_of(repo, "acme/x") == {"keep.txt": b"k2", "new.txt": b"n"}


def test_every_staged_file_ends_up_in_the_store_or_is_gone(repo, storage):
    """put_blob takes the staged file: no copy is left in staging, and two paths with the same content share one blob."""
    repo.create_repository("acme/x")
    ops = [add_op(storage, "a.txt", b"same"), add_op(storage, "b.txt", b"same"), add_op(storage, "c.txt", b"other")]
    publish_commit(repo, "acme/x", ops, summary="one")
    assert len(list(repo.iter_blobs())) == 2
    assert not any(op.addition.src.exists() for op in ops)


def test_gc_cannot_run_while_a_commit_is_storing_its_blobs(repo, storage, monkeypatch):
    """The regression guard for the store lock: between the first stored blob and the published manifest the blobs look unreferenced.
    A GC in that window must be refused (StoreBusy), not delete them."""
    from ingester import jobs
    from server.locks import StoreBusy

    repo.create_repository("acme/x")
    inside, release = threading.Event(), threading.Event()
    real_put = repo.put_blob

    def slow_put(*a, **kw):
        out = real_put(*a, **kw)
        inside.set()                                                  # a blob is in the store, its manifest is not
        assert release.wait(30)
        return out

    monkeypatch.setattr(repo, "put_blob", slow_put)
    done = {}
    t = threading.Thread(target=lambda: done.update(r=publish_commit(repo, "acme/x", [add_op(storage, "a.txt", b"precious")], summary="x")))
    t.start()
    assert inside.wait(30)
    assert len(list(repo.iter_blobs())) == 1
    with pytest.raises(StoreBusy):
        jobs.gc(repo, min_age_seconds=0)
    with pytest.raises(StoreBusy):
        repo.delete_repository("acme/x")
    release.set()
    t.join(30)
    assert done["r"].created and files_of(repo, "acme/x") == {"a.txt": b"precious"}
    assert jobs.gc(repo, min_age_seconds=0).acted == 0


def test_commits_to_different_repositories_do_not_wait_for_each_other(repo, storage):
    repo.create_repository("acme/a")
    repo.create_repository("acme/b")
    results = {}

    def go(cid):
        results[cid] = publish_commit(repo, cid, [add_op(storage, "f.txt", cid.encode(), name=cid.replace("/", "_"))], summary="x")

    threads = [threading.Thread(target=go, args=(c,)) for c in ("acme/a", "acme/b")]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert files_of(repo, "acme/a") == {"f.txt": b"acme/a"} and files_of(repo, "acme/b") == {"f.txt": b"acme/b"}


# ---- repository creation, settings, the read gate ------------------------------------------------------------------------------------

def test_create_repository_is_empty_exclusive_and_visible(repo, storage):
    assert not repo.repository_exists("acme/x")
    assert repo.create_repository("acme/x") is True
    assert repo.create_repository("acme/x") is False
    assert repo.repository_exists("acme/x") and "acme/x" in repo.list_repos() and repo.list_revisions("acme/x") == []
    assert repo.create_repository("datasets/acme/x") is True and "datasets/acme/x" in repo.list_repos()
    assert repo.delete_repository("acme/x")["blobs_deleted"] == 0 and not repo.repository_exists("acme/x")
    assert repo.create_repository("acme/x") is True                   # the name is free again


def test_settings_have_a_namespace_that_defaults_to_local(settings, monkeypatch):
    assert settings.namespace == "local"                               # existing code constructs Settings without it
    from server.config import load_settings
    monkeypatch.delenv("MIRROR_NAMESPACE", raising=False)
    assert load_settings().namespace == "local"
    monkeypatch.setenv("MIRROR_NAMESPACE", "lab")
    assert load_settings().namespace == "lab"


def _gate(token, admin_token, authorization):
    from server.auth import ReadTokenGate
    sent = []

    async def app(scope, receive, send):
        sent.append("app")

    async def send(message):
        sent.append(message.get("status", "body"))

    scope = {"type": "http", "path": "/api/models/acme/x", "headers": [(b"authorization", authorization.encode())] if authorization else []}
    asyncio.run(ReadTokenGate(app, token=token, admin_token=admin_token)(scope, None, send))
    return sent


def test_the_read_gate_lets_the_read_and_the_admin_token_through_and_nothing_else():
    assert _gate("read", "adm", "Bearer read") == ["app"]
    assert _gate("read", "adm", "Bearer adm") == ["app"]
    assert _gate("read", "adm", "Bearer nope")[0] == 401 and _gate("read", "adm", "")[0] == 401
    assert _gate("read", "", "Bearer ")[0] == 401                       # no admin token configured: an empty one matches nothing
    assert _gate("read", "adm", "Basic adm")[0] == 401
