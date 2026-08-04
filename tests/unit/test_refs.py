"""Unit tests for mutable references."""
from __future__ import annotations

from pathlib import Path

import pytest

from server import refs
from server.refs import RefsError


def test_load_returns_empty_when_absent(tmp_path: Path):
    assert refs.load(tmp_path / "refs.json") == {}


def test_set_and_load(tmp_path: Path):
    p = tmp_path / "refs.json"
    refs.set_ref(p, "main", "abc1234")
    refs.set_ref(p, "v1", "def5678")
    assert refs.load(p) == {"main": "abc1234", "v1": "def5678"}


def test_set_overwrites(tmp_path: Path):
    p = tmp_path / "refs.json"
    refs.set_ref(p, "main", "old")
    refs.set_ref(p, "main", "new")
    assert refs.load(p) == {"main": "new"}


def test_delete_ref(tmp_path: Path):
    p = tmp_path / "refs.json"
    refs.set_ref(p, "main", "abc")
    refs.set_ref(p, "v1", "def")
    refs.delete_ref(p, "v1")
    assert refs.load(p) == {"main": "abc"}


def test_resolve_known_ref(tmp_path: Path):
    r = {"main": "abc1234", "v1": "def5678"}
    assert refs.resolve(r, "main") == "abc1234"
    assert refs.resolve(r, "v1") == "def5678"


def test_resolve_falls_back_to_literal_commit():
    r = {"main": "abc"}
    # Unknown ref name that looks like a commit is returned as-is.
    assert refs.resolve(r, "deadbeef") == "deadbeef"


def test_resolve_unknown_ref_returns_none():
    assert refs.resolve({"main": "x"}, "nope") is None


def test_resolve_rejects_short_non_commit():
    # 'main' is known; a short non-hex string is not a commit -> None.
    assert refs.resolve({"main": "x"}, "zz") is None


def test_load_raises_on_corrupt_file(tmp_path: Path):
    p = tmp_path / "refs.json"
    p.write_text("{not json")
    with pytest.raises(RefsError):
        refs.load(p)
