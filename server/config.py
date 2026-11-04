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

    # Token for the GUI's admin API (download, delete, GC). Empty: the server makes one and keeps it in <root>/admin-token.
    admin_token: str = ""

    @property
    def blobs_dir(self) -> Path:
        return self.mirror_root / "blobs" / "sha256"

    @property
    def repos_dir(self) -> Path:
        return self.mirror_root / "repos"

    @property
    def datasets_dir(self) -> Path:
        """Dataset repositories. A separate top-level dir, not a sub-dir of repos/: the model layout (repos/<org>/<name>) is
        unchanged, so existing stores keep working untouched and an org called "datasets" cannot collide."""
        return self.mirror_root / "datasets"

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

    @property
    def store_id_path(self) -> Path:
        return self.mirror_root / ".store-id"

    def ensure_dirs(self) -> None:
        """Create the directory skeleton. Safe to call repeatedly.

        The root itself is created only when its PARENT exists: a NAS volume that is not mounted must stop the server, not
        make it quietly create the store on the small system disk. A new store gets an id (`.store-id`); the id is shown in
        /healthz and the GUI, so an unexpectedly empty library is noticed at once.
        """
        import logging
        import uuid

        root = self.mirror_root
        if not root.is_dir():
            try:
                root.mkdir(parents=False)
            except FileNotFoundError:
                raise StoreNotAvailable(
                    f"MIRROR_ROOT {root}: its parent directory does not exist. Is the volume mounted? "
                    "Refusing to create the store on another disk.") from None
        had_content = any(p.name not in (".store-id", ".store.lock") for p in root.iterdir())
        if not self.store_id_path.exists():
            self.store_id_path.write_text(uuid.uuid4().hex + "\n")
            if not had_content:
                logging.getLogger("llm_repo").warning(
                    "initialised a NEW EMPTY store at %s: if you expected existing models here, the volume is not mounted", root)
        for d in (
            self.blobs_dir,
            self.repos_dir,
            self.datasets_dir,
            self.staging_dir,
            self.index_dir,
            self.log_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)


class StoreNotAvailable(Exception):
    """MIRROR_ROOT cannot be used (missing volume)."""


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
        admin_token=_env("MIRROR_ADMIN_TOKEN", ""),
    )


# Singleton imported across the server package.
settings = load_settings()
