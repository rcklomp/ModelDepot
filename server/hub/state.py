"""Small persistent state of the GUI service: settings (tokens, download window), the admin token, and the 'wanted' list.

Everything lives under <MIRROR_ROOT>/config so it travels with the store (rsync, backup) and survives a container rebuild.
Secrets are written 0600 and never returned by the API (only "is set").
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path

from ..fsutil import atomic_write_text

log = logging.getLogger("modeldepot")


def _write_private(path: Path, text: str) -> None:
    atomic_write_text(path, text)
    os.chmod(path, 0o600)


class HubSettings:
    """User-changeable settings, file-backed. Environment variables win over stored values for the tokens."""

    DEFAULTS = {"hf_token": "", "civitai_token": "", "max_concurrent": 1, "download_window": ""}

    def __init__(self, root: Path, *, env_hf: str = "", env_civitai: str = ""):
        self._path = Path(root) / "config" / "settings.json"
        self._lock = threading.Lock()
        self._env = {"hf_token": env_hf, "civitai_token": env_civitai}
        try:
            self._data = {**self.DEFAULTS, **json.loads(self._path.read_text())}
        except (OSError, ValueError):
            self._data = dict(self.DEFAULTS)

    def get(self, key: str):
        return self._env.get(key) or self._data.get(key, self.DEFAULTS.get(key))

    def public(self) -> dict:
        return {"hf_token_set": bool(self.get("hf_token")), "hf_token_from_env": bool(self._env["hf_token"]),
                "civitai_token_set": bool(self.get("civitai_token")), "civitai_token_from_env": bool(self._env["civitai_token"]),
                "max_concurrent": int(self.get("max_concurrent") or 1), "download_window": self.get("download_window") or ""}

    def update(self, changes: dict) -> None:
        with self._lock:
            for k, v in changes.items():
                if k not in self.DEFAULTS:
                    continue
                if k == "max_concurrent":
                    v = max(1, min(4, int(v)))
                if k == "download_window" and v and not parse_window(v):
                    raise ValueError("download window must look like 01:00-07:00")
                self._data[k] = v
            _write_private(self._path, json.dumps(self._data, indent=2))


def parse_window(text: str):
    """'01:00-07:00' -> (60, 420) minutes since midnight, or None when empty/invalid."""
    try:
        a, b = text.split("-")
        h1, m1 = map(int, a.split(":"))
        h2, m2 = map(int, b.split(":"))
        if not (0 <= h1 < 24 and 0 <= h2 < 24 and 0 <= m1 < 60 and 0 <= m2 < 60):
            return None
        return h1 * 60 + m1, h2 * 60 + m2
    except (ValueError, AttributeError):
        return None


def in_window(text: str, now: float | None = None) -> bool:
    """True when downloads may start now. No window set = always. A window may wrap midnight (22:00-06:00)."""
    w = parse_window(text) if text else None
    if not w:
        return True
    lt = time.localtime(now if now is not None else time.time())
    cur = lt.tm_hour * 60 + lt.tm_min
    start, end = w
    return start <= cur < end if start <= end else (cur >= start or cur < end)


def load_admin_token(root: Path, env_token: str = "") -> str:
    """MIRROR_ADMIN_TOKEN, else the token kept in <root>/config/admin-token, made (once) on first start."""
    if env_token:
        return env_token
    path = Path(root) / "config" / "admin-token"
    try:
        tok = path.read_text().strip()
        if tok:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(24)
    _write_private(path, tok + "\n")
    log.warning("created the admin token for the GUI: it is in %s (the GUI asks for it on the first admin action)", path)
    return tok


class WantedLog:
    """What clients asked this server for and did not find ('model acme/x'): the GUI offers to download them.

    Bounded (200 entries), flushed at most every few seconds, best effort: losing it costs nothing."""

    MAX = 200

    def __init__(self, root: Path):
        self._path = Path(root) / "config" / "wanted.json"
        self._lock = threading.Lock()
        self._last_flush = 0.0
        try:
            self._items = json.loads(self._path.read_text())
        except (OSError, ValueError):
            self._items = {}

    def note(self, kind: str, public_id: str, client: str = "") -> None:
        key = f"{kind}:{public_id}"
        now = int(time.time())
        with self._lock:
            it = self._items.get(key) or {"type": kind, "id": public_id, "first_seen": now, "count": 0}
            it.update(last_seen=now, count=it["count"] + 1, client=client[:80])
            self._items[key] = it
            if len(self._items) > self.MAX:
                for k in sorted(self._items, key=lambda k: self._items[k]["last_seen"])[: len(self._items) - self.MAX]:
                    del self._items[k]
            self._flush(now)

    def list(self) -> list[dict]:
        with self._lock:
            return sorted(self._items.values(), key=lambda i: -i["last_seen"])

    def dismiss(self, kind: str, public_id: str) -> None:
        with self._lock:
            self._items.pop(f"{kind}:{public_id}", None)
            self._flush(time.time(), force=True)

    def _flush(self, now: float, force: bool = False) -> None:
        if not force and now - self._last_flush < 5:
            return
        self._last_flush = now
        try:
            atomic_write_text(self._path, json.dumps(self._items))
        except OSError:
            pass
