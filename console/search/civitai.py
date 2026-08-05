"""Civitai search adapter.

Proxies Civitai's /api/v1/models search, normalizing results into ModelResult.
Civitai returns modelVersions[0].files[] inline, so unlike HF we CAN populate
quant_options (file choices) at search time. Format filtering (SafeTensor etc.)
is post-filtered client-side — Civitai has no server-side format param.
"""
from __future__ import annotations

from .. import upstream_proxy
from .normalizer import ModelResult, QuantOption, SearchPage


def search(*, q: str = "", model_type: str = "", fmt: str = "",
           sort: str = "Highest Rated", nsfw: bool = False,
           limit: int = 30, cursor: str | None = None,
           token: str = "") -> SearchPage:
    """Search Civitai models. `model_type` is Checkpoint|LORA|VAE|...;
    `fmt` is SafeTensor|PickleTensor|... (post-filtered)."""
    params: dict = {
        "query": q or None,
        "types": model_type or None,
        "sort": sort,
        "nsfw": "true" if nsfw else "false",
        "limit": min(limit, 100),
    }
    if cursor:
        params["cursor"] = cursor

    body, next_cursor = upstream_proxy.civitai_get_json("/api/v1/models", params, token)
    items = (body or {}).get("items", []) if isinstance(body, dict) else []
    results = [_normalize(item, fmt) for item in items]
    # Post-filter by format if requested (Civitai has no server-side format param).
    if fmt:
        results = [r for r in results if r.quant_options]
    return SearchPage(results=results, next_cursor=next_cursor)


def fetch_files(version_id: int, *, token: str = "") -> list[QuantOption]:
    """Fetch the full file list for a Civitai model version."""
    body, _ = upstream_proxy.civitai_get_json(
        f"/api/v1/model-versions/{version_id}", None, token,
    )
    if not isinstance(body, dict):
        return []
    options: list[QuantOption] = []
    for f in body.get("files", []) or []:
        name = f.get("name", "")
        size_kb = f.get("sizeKB", 0) or 0
        options.append(QuantOption(
            label=name, path=name, size_bytes=int(size_kb * 1024),
        ))
    options.sort(key=lambda o: o.size_bytes, reverse=True)
    return options


def _normalize(item: dict, fmt_filter: str) -> ModelResult:
    versions = item.get("modelVersions") or []
    latest = versions[0] if versions else {}
    files = latest.get("files") or []
    # Build quant options; if a format filter is set, keep only matching files
    # (so results with no matching file get dropped by the caller).
    options: list[QuantOption] = []
    for f in files:
        meta_fmt = (f.get("metadata") or {}).get("format", "")
        if fmt_filter and meta_fmt != fmt_filter:
            continue
        options.append(QuantOption(
            label=f.get("name", ""),
            path=f.get("name", ""),
            size_bytes=int((f.get("sizeKB") or 0) * 1024),
        ))
    version_id = latest.get("id")
    model_id = item.get("id")
    return ModelResult(
        source="civitai",
        id=f"{model_id}/{version_id}" if version_id else str(model_id),
        name=item.get("name", ""),
        author=(item.get("creator") or {}).get("username", ""),
        type=item.get("type", "Model").lower(),
        downloads=(item.get("stats") or {}).get("downloadCount", 0) or 0,
        likes=(item.get("stats") or {}).get("thumbsUpCount", 0) or 0,
        description=(item.get("description") or "")[:500],
        url=f"https://civitai.com/models/{model_id}" if model_id else "",
        nsfw=bool(item.get("nsfw")),
        quant_options=options,
        total_size_bytes=sum(o.size_bytes for o in options),
    )
