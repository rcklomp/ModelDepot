"""Hugging Face search adapter.

Proxies HF's /api/models search, normalizing results into ModelResult.
Format filtering uses HF's server-side `filter` param (gguf|mlx|safetensors).
Quant options aren't populated at search time (HF's siblings list doesn't
include sizes) — the UI fetches them via /upstream/files when the user expands
a model.
"""
from __future__ import annotations

import re

from .. import upstream_proxy
from .normalizer import ModelResult, QuantOption, SearchPage

# Tag -> normalized type. HF tags include format markers.
_TAG_TO_TYPE = {
    "gguf": "gguf",
    "mlx": "mlx",
    "safetensors": "safetensors",
    "pytorch_model": "safetensors",  # legacy bin often co-listed
    "onnx": "onnx",
}


def search(*, q: str = "", fmt: str = "", sort: str = "downloads",
           limit: int = 30, cursor: str | None = None,
           token: str = "") -> SearchPage:
    """Search HF models. `fmt` is gguf|mlx|safetensors (mapped to filter=)."""
    params: dict = {
        "search": q or None,
        "sort": sort,
        "direction": -1,        # descending (most downloaded / newest)
        "limit": min(limit, 100),
        "full": "true",         # need siblings + tags to infer type
    }
    if fmt:
        params["filter"] = fmt
    if cursor:
        params["cursor"] = cursor

    body, next_cursor = upstream_proxy.hf_get_json("/api/models", params, token)
    results = [_normalize(m, token) for m in body] if isinstance(body, list) else []
    return SearchPage(results=results, next_cursor=next_cursor)


def fetch_files(repo_id: str, *, revision: str = "main",
                token: str = "") -> list[QuantOption]:
    """Fetch the file tree for a repo — used by /upstream/files to show
    per-file sizes + shard groupings before the user picks a quant."""
    body, _ = upstream_proxy.hf_get_json(
        f"/api/models/{repo_id}/tree/{revision}",
        {"recursive": "true", "expand": "false"},
        token,
    )
    if not isinstance(body, list):
        return []
    options: list[QuantOption] = []
    # Group sharded GGUFs by prefix so the UI shows "UD-Q4_K_XL (4 shards, 144 GiB)".
    shards_by_prefix: dict[str, list[dict]] = {}
    for entry in body:
        if entry.get("type") != "file":
            continue
        path = entry.get("path", "")
        size = entry.get("size") or 0
        if entry.get("lfs"):
            size = entry["lfs"].get("size") or size
        m = re.match(r"^(.+)-([0-9]{5})-of-([0-9]{5})(\.[^.]+)?$", path)
        if m:
            prefix = m.group(1)
            shards_by_prefix.setdefault(prefix, []).append({"path": path, "size": size})
        else:
            options.append(QuantOption(label=path, path=path, size_bytes=size))
    for prefix, shards in shards_by_prefix.items():
        total = sum(s["size"] for s in shards)
        # Glob that captures all shards of this prefix.
        options.append(QuantOption(
            label=f"{prefix}-* ({len(shards)} shards)",
            path=f"{prefix}-*",
            size_bytes=total, shard_count=len(shards),
        ))
    options.sort(key=lambda o: o.size_bytes, reverse=True)
    return options


def _normalize(m: dict, token: str) -> ModelResult:
    tags = m.get("tags") or []
    type_ = "model"
    for tag in tags:
        if tag in _TAG_TO_TYPE:
            type_ = _TAG_TO_TYPE[tag]
            break
    siblings = m.get("siblings") or []
    # Best-effort total size: HF siblings don't carry size without files_metadata,
    # so leave 0 — the UI fetches /upstream/files for an accurate number.
    return ModelResult(
        source="hf",
        id=m.get("id", ""),
        name=m.get("id", "").split("/")[-1],
        author=m.get("author", ""),
        type=type_,
        downloads=m.get("downloads", 0) or 0,
        likes=m.get("likes", 0) or 0,
        url=f"https://huggingface.co/{m.get('id', '')}",
        quant_options=[],  # populated on demand via fetch_files
        total_size_bytes=0,
    )
