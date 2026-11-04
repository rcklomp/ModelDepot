"""Dataset repositories in the core: layout, ids, and (the dangerous one) garbage collection."""
from __future__ import annotations

from pathlib import Path

from ingester import cli, jobs
from server.storage import core_repo_id, public_repo_id, repo_type_of
from tests.compat.conftest import stage_repo


def test_id_helpers_round_trip():
    assert core_repo_id("model", "acme/x") == "acme/x" and core_repo_id("model", "gpt2") == "gpt2"
    assert core_repo_id("dataset", "acme/x") == "datasets/acme/x"
    assert core_repo_id("dataset", "squad") == "datasets/_user/squad"
    assert public_repo_id("datasets/acme/x") == "acme/x" and public_repo_id("datasets/_user/squad") == "squad"
    assert repo_type_of("datasets/acme/x") == "dataset" and repo_type_of("acme/x") == "model"


def test_datasets_have_their_own_directory_and_both_kinds_are_listed(repo, storage):
    stage_repo(repo, "acme/m", {"f": b"1"}, commit="a" * 40)
    stage_repo(repo, "datasets/acme/d", {"f": b"2"}, commit="b" * 40)
    assert (storage.repos_dir / "acme" / "m").is_dir()
    assert (storage.datasets_dir / "acme" / "d").is_dir()
    assert not (storage.repos_dir / "datasets").exists()                      # the model layout is untouched
    assert repo.list_repos() == ["acme/m", "datasets/acme/d"]


def test_gc_keeps_the_blobs_of_datasets(repo):
    """If GC only walked models it would delete every dataset file."""
    stage_repo(repo, "datasets/acme/d", {"train.jsonl": b"keep me"}, commit="b" * 40)
    stage_repo(repo, "acme/m", {"config.json": b"{}"}, commit="a" * 40)
    orphan = repo.put_blob_bytes(b"nobody names me")
    report = jobs.gc(repo, min_age_seconds=0)
    assert report.acted == 1                                                    # exactly the orphan
    assert repo._storage.blobs.exists(orphan.digest) is False
    assert repo.get_file("datasets/acme/d", "main", "train.jsonl").size == len(b"keep me")


def test_ingest_local_can_publish_a_dataset(mirror_root, tmp_path, capsys):
    src = tmp_path / "ds"
    src.mkdir()
    (src / "train.jsonl").write_text('{"a": 1}\n')
    import os
    os.environ["MIRROR_ROOT"] = str(mirror_root)
    assert cli.main(["ingest-local", "acme/mine", str(src), "--type", "dataset"]) == 0
    out = capsys.readouterr().out
    assert "datasets/acme/mine" in out and "/datasets/acme/mine/resolve/main/" in out
    assert (mirror_root / "datasets" / "acme" / "mine" / "refs.json").is_file()
    assert cli.main(["list"]) == 0 and "datasets/acme/mine" in capsys.readouterr().out
