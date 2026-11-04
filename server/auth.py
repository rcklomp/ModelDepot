"""Optional read gate for the Hugging Face routes.

MIRROR_AUTH_TOKEN (read by config.py since the first commit and, until 2026-11-04, never checked): when set, every request to the
Hub-compatible routes needs `Authorization: Bearer <token>`: what `huggingface_hub` sends when HF_TOKEN is set on the client. The
GUI and the admin API are NOT behind this token (they have their own admin token); /healthz is open for monitoring.
Pure ASGI on purpose: a BaseHTTPMiddleware would sit in the path of every multi-gigabyte download.
"""
from __future__ import annotations

import hmac

_HUB_PREFIXES = ("/api/models", "/api/datasets", "/datasets/")


def is_hub_path(path: str) -> bool:
    return path.startswith(_HUB_PREFIXES) or "/resolve/" in path


class ReadTokenGate:
    def __init__(self, app, token: str = ""):
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.token or not is_hub_path(scope["path"]):
            return await self.app(scope, receive, send)
        auth = dict(scope["headers"]).get(b"authorization", b"")
        if auth[:7].lower() == b"bearer " and hmac.compare_digest(auth[7:].strip(), self.token):
            return await self.app(scope, receive, send)
        body = b'{"detail":"a valid bearer token is required (set HF_TOKEN on the client)"}'
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                                (b"www-authenticate", b"Bearer")]})
        await send({"type": "http.response.body", "body": body})
