"""Environment-driven configuration for the mirror server.

All settings are read once at import time and exposed as a singleton `settings`.
Modules depend on this rather than reading os.environ directly, so the system
has exactly one place where configuration is interpreted.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value is not None and value != "" else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "1" if default else "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # Root of the mirror store. The only path that matters.
    mirror_root: Path

    # Network bind for the shim (Caddy proxies to this).
    host: str
    port: int

    # URL the mirror advertises in metadata / Location headers.
    base_url: str
    public_url: str

    # Operational flags.
    read_only: bool
    auth_token: str

    # Index backend: "sqlite" (default) or "none".
    index_backend: str

    @property
    def blobs_dir(self) -> Path:
        return self.mirror_root / "blobs" / "sha256"

    @property
    def repos_dir(self) -> Path:
        return self.mirror_root / "repos"

    @property
    def staging_dir(self) -> Path:
        return self.mirror_root / "staging"

    @property
    def index_dir(self) -> Path:
        return self.mirror_root / "index"

    @property
    def events_path(self) -> Path:
        return self.mirror_root / "events.jsonl"

    @property
    def log_dir(self) -> Path:
        return self.mirror_root / "log"

    def ensure_dirs(self) -> None:
        """Create the directory skeleton. Safe to call repeatedly."""
        for d in (
            self.blobs_dir,
            self.repos_dir,
            self.staging_dir,
            self.index_dir,
            self.log_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    root = Path(_env("MIRROR_ROOT", "./data/mirror-root")).expanduser().resolve()
    return Settings(
        mirror_root=root,
        host=_env("MIRROR_HOST", "0.0.0.0"),
        port=_env_int("MIRROR_PORT", 8000),
        base_url=_env("MIRROR_BASE_URL", "http://localhost:8000").rstrip("/"),
        public_url=_env("MIRROR_PUBLIC_URL", "http://localhost:8000").rstrip("/"),
        read_only=_env_bool("MIRROR_READ_ONLY", False),
        auth_token=_env("MIRROR_AUTH_TOKEN", ""),
        index_backend=_env("MIRROR_INDEX_BACKEND", "sqlite"),
    )


# Singleton imported across the server package.
settings = load_settings()
