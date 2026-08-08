"""Tests for the search proxy (HF + Civitai) with the upstream stubbed.

No network: upstream_proxy.hf_get_json / civitai_get_json are monkeypatched to
return canned responses. Verifies normalization, format filtering, and cursor
pass-through.
"""
from __future__ import annotations

from console.search import civitai, hf
from console.search.normalizer import SearchPage


# ---- HF --------------------------------------------------------------

def test_hf_search_normalizes_results(monkeypatch):
    canned = [
        {"id": "org/llama-gguf", "author": "org",
         "tags": ["gguf", "llama"], "downloads": 1000, "likes": 50},
        {"id": "other/mistral", "author": "other",
         "tags": ["safetensors"], "downloads": 500, "likes": 10},
    ]
    monkeypatch.setattr(
        "console.search.hf.upstream_proxy.hf_get_json",
        lambda path, params, token: (canned, "next-cursor-abc"),
    )
    page = hf.search(q="llama", fmt="gguf", token="t")
    assert isinstance(page, SearchPage)
    assert page.next_cursor == "next-cursor-abc"
    assert len(page.results) == 2
    r0 = page.results[0]
    assert r0.source == "hf"
    assert r0.id == "org/llama-gguf"
    assert r0.type == "gguf"  # inferred from tags
    assert r0.name == "llama-gguf"
    assert r0.downloads == 1000
    assert r0.url == "https://huggingface.co/org/llama-gguf"


def test_hf_search_passes_filter_and_sort(monkeypatch):
    captured = {}

    def fake(path, params, token):
        captured["params"] = params
        return [], None
    monkeypatch.setattr("console.search.hf.upstream_proxy.hf_get_json", fake)
    hf.search(q="test", fmt="mlx", sort="likes", limit=10, token="")
    p = captured["params"]
    assert p["search"] == "test"
    assert p["filter"] == "mlx"
    assert p["sort"] == "likes"
    assert p["direction"] == -1
    assert p["limit"] == 10


def test_hf_search_passes_cursor(monkeypatch):
    captured = {}

    def fake(path, params, token):
        captured["cursor"] = params.get("cursor")
        return [], None
    monkeypatch.setattr("console.search.hf.upstream_proxy.hf_get_json", fake)
    hf.search(q="x", cursor="page2-token", token="")
    assert captured["cursor"] == "page2-token"


def test_hf_fetch_files_groups_shards(monkeypatch):
    """Sharded GGUFs collapse into one QuantOption with shard_count."""
    canned_tree = [
        {"type": "file", "path": "config.json", "size": 811},
        {"type": "file", "path": "model-UD-Q4-00001-of-00004.gguf", "size": 5000,
         "lfs": {"size": 5000}},
        {"type": "file", "path": "model-UD-Q4-00002-of-00004.gguf", "size": 5000,
         "lfs": {"size": 5000}},
        {"type": "file", "path": "model-UD-Q4-00003-of-00004.gguf", "size": 5000,
         "lfs": {"size": 5000}},
        {"type": "file", "path": "model-UD-Q4-00004-of-00004.gguf", "size": 1000,
         "lfs": {"size": 1000}},
    ]
    monkeypatch.setattr(
        "console.search.hf.upstream_proxy.hf_get_json",
        lambda path, params, token: (canned_tree, None),
    )
    options = hf.fetch_files("org/repo", token="")
    by_label = {o.label: o for o in options}
    # The 4 shards collapsed into one grouped option.
    shard_opt = by_label["model-UD-Q4-* (4 shards)"]
    assert shard_opt.shard_count == 4
    assert shard_opt.size_bytes == 16000
    # Non-sharded file present too.
    assert "config.json" in by_label
    assert by_label["config.json"].size_bytes == 811


# ---- Civitai ---------------------------------------------------------

def test_civitai_search_normalizes_with_files(monkeypatch):
    """Civitai returns files inline, so quant_options ARE populated."""
    canned = {
        "items": [
            {"id": 5585, "name": "Deliberate", "type": "Checkpoint", "nsfw": False,
             "creator": {"username": "steve"},
             "stats": {"downloadCount": 29000, "thumbsUpCount": 1200},
             "modelVersions": [{
                 "id": 6500,
                 "files": [{
                     "name": "deliberate_v08.safetensors", "sizeKB": 2000000,
                     "metadata": {"format": "SafeTensor"},
                 }],
             }]},
        ],
        "metadata": {"nextCursor": "cursor-xyz"},
    }
    monkeypatch.setattr(
        "console.search.civitai.upstream_proxy.civitai_get_json",
        lambda path, params, token: (canned, "cursor-xyz"),
    )
    page = civitai.search(q="deliberate", token="")
    assert page.next_cursor == "cursor-xyz"
    assert len(page.results) == 1
    r = page.results[0]
    assert r.source == "civitai"
    assert r.id == "5585/6500"
    assert r.type == "checkpoint"
    assert r.downloads == 29000
    assert len(r.quant_options) == 1
    assert r.quant_options[0].label == "deliberate_v08.safetensors"
    assert r.quant_options[0].size_bytes == 2000000 * 1024


def test_civitai_search_post_filters_by_format(monkeypatch):
    canned = {
        "items": [
            {"id": 1, "name": "A", "type": "Checkpoint",
             "modelVersions": [{"id": 10, "files": [
                 {"name": "a.safetensors", "sizeKB": 1, "metadata": {"format": "SafeTensor"}},
             ]}]},
            {"id": 2, "name": "B", "type": "Checkpoint",
             "modelVersions": [{"id": 20, "files": [
                 {"name": "b.ckpt", "sizeKB": 1, "metadata": {"format": "PickleTensor"}},
             ]}]},
        ],
        "metadata": {},
    }
    monkeypatch.setattr(
        "console.search.civitai.upstream_proxy.civitai_get_json",
        lambda path, params, token: (canned, None),
    )
    # Filter for SafeTensor only -> model B (PickleTensor) dropped.
    page = civitai.search(q="", fmt="SafeTensor", token="")
    assert len(page.results) == 1
    assert page.results[0].name == "A"
