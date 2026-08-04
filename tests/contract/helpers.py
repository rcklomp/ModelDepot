"""Helpers shared by contract tests.

`stage_fake_repo` publishes a tiny but realistic Hugging-Face-shaped repository
into a Repository Core so the HTTP contract tests have something to serve. It
writes a real (if tiny) config.json + a small binary 'weights' file, both
content-addressed as blobs, and points the `main` ref at the revision.
"""
from __future__ import annotations

import json

from server.manifests import (
    FileEntry, License, Manifest, RevisionInfo, Source, SCHEMA_VERSION,
)
from server.repository import RepositoryCore


def stage_fake_repo(repo: RepositoryCore, repo_id: str = "tinyorg/tinyconfig") -> str:
    """Stage a minimal HF-shaped repo. Returns the commit SHA."""
    config = {"model_type": "llama", "hidden_size": 8, "num_hidden_layers": 1,
              "num_attention_heads": 1, "vocab_size": 16, "torch_dtype": "float32"}
    config_bytes = json.dumps(config, sort_keys=True).encode("utf-8")

    weights = b"\x00\x01\x02\x03" * 16  # 64 bytes of 'weights'

    config_blob = repo.put_blob_bytes(config_bytes)
    weights_blob = repo.put_blob_bytes(weights)

    commit = "deadbeefcafebabe1234567890abcdefdeadbeef"
    manifest = Manifest(
        schema_version=SCHEMA_VERSION,
        repo_id=repo_id,
        source=Source(kind="huggingface", ref="https://huggingface.co/" + repo_id),
        revision=RevisionInfo(
            commit=commit, fetched_at="2026-08-04T12:00:00+00:00",
            license=License(name="mit"),
        ),
        files=[
            FileEntry(
                path="config.json", size=len(config_bytes),
                blob=f"sha256:{config_blob.digest}",
                checksums={"sha256": config_blob.digest},
                metadata={"hf": {"etag": f'"{config_blob.digest[:32]}-1"',
                                  "x_repo_commit": commit}},
            ),
            FileEntry(
                path="pytorch_model.bin", size=len(weights),
                blob=f"sha256:{weights_blob.digest}",
                checksums={"sha256": weights_blob.digest},
                metadata={"hf": {"etag": f'"{weights_blob.digest[:32]}-1"',
                                  "x_repo_commit": commit}},
            ),
        ],
    )
    repo.publish_revision(manifest)
    repo.update_reference(repo_id, "main", commit)
    repo.repository_created(repo_id)
    return commit
