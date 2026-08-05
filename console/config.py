"""Configuration for the console app (port 8001).

The console is a SEPARATE app from the HF mirror (port 8000). It shares the
same store (MIRROR_ROOT) but holds the richer, mutating surface: upstream
search, async ingest jobs, ops. Upstream tokens for HF/Civitai are held
SERVER-SIDE here and never sent to the browser — the console proxies upstream
calls and injects the token, which avoids CORS and keeps tokens out of clients.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if v is not None and v != "" else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class ConsoleSettings:
    # Same store the mirror serves (the console reads manifests/refs/blobs and
    # writes via the ingester, sharing MIRROR_ROOT).
    mirror_root: Path

    # Network bind for the console.
    host: str
    port: int

    # Upstream tokens — held server-side only, injected by upstream_proxy.
    # Empty string = anonymous (HF still works for public repos; Civitai search
    # is anonymous too). Set CONSOLE_HF_TOKEN / CONSOLE_CIVITAI_TOKEN to raise
    # rate limits / unlock gated content.
    hf_token: str
    civitai_token: str

    # Optional bearer gate on the console API (LAN-trusted default).
    auth_token: str

    # Where built web/dist is served from (None = UI disabled, API only).
    web_dist: Path | None

    @property
    def repos_dir(self) -> Path:
        return self.mirror_root / "repos"

    @property
    def log_dir(self) -> Path:
        return self.mirror_root / "log"


def load_settings() -> ConsoleSettings:
    root = Path(_env("MIRROR_ROOT", "./data/mirror-root")).expanduser().resolve()
    web_dist_env = _env("CONSOLE_WEB_DIST", "")
    web_dist = Path(web_dist_env).expanduser().resolve() if web_dist_env else None
    # Default web dist to a sibling 'web/dist' if it exists.
    if web_dist is None:
        here = Path(__file__).resolve().parent.parent
        candidate = here / "web" / "dist"
        if candidate.is_dir():
            web_dist = candidate
    return ConsoleSettings(
        mirror_root=root,
        host=_env("CONSOLE_HOST", "0.0.0.0"),
        port=_env_int("CONSOLE_PORT", 8001),
        hf_token=_env("CONSOLE_HF_TOKEN", os.environ.get("HF_TOKEN", "")),
        civitai_token=_env("CONSOLE_CIVITAI_TOKEN", os.environ.get("CIVITAI_API_KEY", "")),
        auth_token=_env("CONSOLE_AUTH_TOKEN", ""),
        web_dist=web_dist,
    )


settings = load_settings()
