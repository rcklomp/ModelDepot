"""Unit tests for the content-addressable blob store."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from server import blobstore
from server.blobstore import BlobStore, IntegrityError


def test_put_bytes_is_content_addressed(storage):
    bs = storage.blobs
    data = b"hello world"
    expected = hashlib.sha256(data).hexdigest()

    blob = bs.put_bytes(data)

    assert blob.digest == expected
    assert blob.size == len(data)
    # Sharded layout: blobs/sha256/<2 chars>/<digest>
    assert blob.path == bs.root / expected[:2] / expected
    assert blob.path.is_file()
    assert blob.path.read_bytes() == data


def test_put_bytes_dedups_identical_content(storage):
    bs = storage.blobs
    a = bs.put_bytes(b"same content")
    b = bs.put_bytes(b"same content")

    assert a.digest == b.digest
    assert a.path == b.path  # one file, not two


def test_put_file_matches_expected_digest(storage, tmp_path):
    bs = storage.blobs
    src = tmp_path / "staged.bin"
    payload = b"file payload" * 1000
    src.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()

    blob = bs.put_file(src, expected_digest=expected)

    assert blob.digest == expected
    assert not src.exists()  # source consumed
    assert bs.verify(expected)


def test_put_file_rejects_wrong_expected_digest(storage, tmp_path):
    bs = storage.blobs
    src = tmp_path / "staged.bin"
    src.write_bytes(b"actual content")

    with pytest.raises(IntegrityError):
        bs.put_file(src, expected_digest="0" * 64)

    # Source is NOT consumed on integrity failure.
    assert src.exists()


def test_put_file_dedups_existing(storage, tmp_path):
    bs = storage.blobs
    payload = b"duplicate me"
    digest = hashlib.sha256(payload).hexdigest()

    first = tmp_path / "a.bin"
    first.write_bytes(payload)
    bs.put_file(first)

    # Second identical file should be deduped, not stored twice.
    second = tmp_path / "b.bin"
    second.write_bytes(payload)
    result = bs.put_file(second)

    assert result.digest == digest
    assert not second.exists()
    # Only one shard file present.
    files = list(bs.root.glob("*/*"))
    assert len([f for f in files if f.is_file() and not f.name.startswith(".")]) == 1


def test_verify_detects_corruption(storage):
    bs = storage.blobs
    blob = bs.put_bytes(b"clean data")
    assert bs.verify(blob.digest)

    # Corrupt the blob in place.
    blob.path.write_bytes(b"tampered")
    assert not bs.verify(blob.digest)


def test_exists_and_size(storage):
    bs = storage.blobs
    blob = bs.put_bytes(b"measurable")
    assert bs.exists(blob.digest)
    assert bs.size(blob.digest) == len(b"measurable")
    assert not bs.exists("f" * 64)


def test_blob_ref_helpers_roundtrip():
    digest = "a" * 64
    ref = blobstore.make_blob_ref(digest)
    assert ref == "sha256:" + digest
    assert blobstore.parse_blob_ref(ref) == digest


def test_parse_blob_ref_rejects_malformed():
    with pytest.raises(ValueError):
        blobstore.parse_blob_ref("abcdef")  # no prefix
    with pytest.raises(ValueError):
        blobstore.parse_blob_ref("sha256:tooshort")
    with pytest.raises(ValueError):
        blobstore.parse_blob_ref("sha256:" + "z" * 64)  # non-hex


def test_open_streams_bytes(storage):
    bs = storage.blobs
    payload = bytes(range(256)) * 10
    blob = bs.put_bytes(payload)
    with bs.open(blob.digest) as f:
        assert f.read() == payload
