"""Token-injecting HTTP client for upstream Hugging Face / Civitai calls.

Tokens are held SERVER-SIDE (console.config) and injected here — they never
reach the browser. This avoids CORS (the browser only talks to /api/console/*)
and keeps tokens out of client-side code.

Uses urllib (stdlib) so the console has no extra HTTP dependency. Search calls
are read-only GETs; pagination cursors are passed through.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

HF_BASE = "https://huggingface.co"
CIVITAI_BASE = "https://civitai.com"


class UpstreamError(Exception):
    def __init__(self, source: str, status: int, message: str):
        super().__init__(f"[{source}] HTTP {status}: {message}")
        self.source = source
        self.status = status


def hf_get_json(path: str, params: dict[str, Any] | None, token: str,
               *, base: str = HF_BASE) -> tuple[Any, str | None]:
    """GET a JSON object/array from HF. Returns (parsed_body, next_cursor).

    HF paginates via the `Link` header (rel="next", cursor=...). We extract the
    next cursor if present so callers can request the next page.
    """
    url = base + path
    if params:
        # HF accepts repeated params (e.g. filter=gguf&filter=conversational).
        pairs = []
        for k, v in params.items():
            if v is None or v == "":
                continue
            if isinstance(v, (list, tuple)):
                for item in v:
                    pairs.append((k, str(item)))
            else:
                pairs.append((k, str(v)))
        url = url + "?" + urllib.parse.urlencode(pairs)

    headers = {"Accept": "application/json", "User-Agent": "llm-repo-console/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            link = resp.headers.get("Link", "")
            next_cursor = _extract_cursor(link)
            return body, next_cursor
    except urllib.error.HTTPError as exc:
        raise UpstreamError("hf", exc.code, exc.read().decode("utf-8", "replace")[:200]) from exc
    except urllib.error.URLError as exc:
        raise UpstreamError("hf", 0, str(exc)) from exc


def civitai_get_json(path: str, params: dict[str, Any] | None, token: str,
                     *, base: str = CIVITAI_BASE) -> tuple[Any, str | None]:
    """GET a JSON object from Civitai. Returns (parsed_body, next_cursor).

    Civitai wraps results in {items, metadata} and paginates via
    metadata.nextCursor. We surface that as the cursor.
    """
    url = base + path
    if params:
        pairs = [(k, str(v)) for k, v in params.items() if v is not None and v != ""]
        url = url + "?" + urllib.parse.urlencode(pairs)

    headers = {"Accept": "application/json", "User-Agent": "llm-repo-console/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            # Civitai search returns {items, metadata}; detail returns bare object.
            if isinstance(data, dict) and "metadata" in data and isinstance(data.get("items"), list):
                next_cursor = (data.get("metadata") or {}).get("nextCursor")
                return data, next_cursor
            return data, None
    except urllib.error.HTTPError as exc:
        raise UpstreamError("civitai", exc.code, exc.read().decode("utf-8", "replace")[:200]) from exc
    except urllib.error.URLError as exc:
        raise UpstreamError("civitai", 0, str(exc)) from exc


def _extract_cursor(link_header: str) -> str | None:
    """Pull the 'cursor=' value out of a HF Link rel="next" URL."""
    if not link_header:
        return None
    # Link: <https://.../api/models?...&cursor=XYZ&...>; rel="next"
    for part in link_header.split(","):
        if 'rel="next"' in part:
            start = part.find("<")
            end = part.find(">", start)
            if start != -1 and end != -1:
                url = part[start + 1:end]
                parsed = urllib.parse.urlparse(url)
                qs = urllib.parse.parse_qs(parsed.query)
                cursors = qs.get("cursor")
                if cursors:
                    return cursors[0]
    return None
