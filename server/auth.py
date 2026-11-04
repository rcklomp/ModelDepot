"""Optional read gate for the Hugging Face routes.

MIRROR_AUTH_TOKEN (read by config.py since the first commit and, until 2026-11-04, never checked): when set, every request to the
Hub-compatible routes needs `Authorization: Bearer <token>`: what `huggingface_hub` sends when HF_TOKEN is set on the client. The
GUI and the admin API are NOT behind this token (they have their own admin token); /healthz is open for monitoring.
The admin token also passes this gate (it is the stronger credential, and `huggingface_hub` sends exactly one token: whoever publishes with
HF_TOKEN=<admin token> must get through to the upload routes; those check the admin token themselves, the read token never writes).
Pure ASGI on purpose: a BaseHTTPMiddleware would sit in the path of every multi-gigabyte download.
"""
from __future__ import annotations

import hmac

_HUB_PREFIXES = ("/api/models", "/api/datasets", "/datasets/")


def is_hub_path(path: str) -> bool:
    return path.startswith(_HUB_PREFIXES) or "/resolve/" in path


class ReadTokenGate:
    def __init__(self, app, token: str = "", admin_token: str = ""):
        self.app = app
        self.token = token.encode()
        self.admin_token = admin_token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.token or not is_hub_path(scope["path"]):
            return await self.app(scope, receive, send)
        auth = dict(scope["headers"]).get(b"authorization", b"")
        if auth[:7].lower() == b"bearer ":
            given = auth[7:].strip()
            if hmac.compare_digest(given, self.token) or (self.admin_token and hmac.compare_digest(given, self.admin_token)):
                return await self.app(scope, receive, send)
        # "error" is the Hub's own key (huggingface_hub prints it); "detail" is what this gate always sent.
        msg = "a valid bearer token is required (set HF_TOKEN on the client)"
        body = ('{"error":"%s","detail":"%s"}' % (msg, msg)).encode()
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                                (b"www-authenticate", b"Bearer"),
                                # huggingface_hub reads a 401 on a repo URL as "repository not found" unless it carries this exact message
                                (b"x-error-message", b"Invalid credentials in Authorization header")]})
        await send({"type": "http.response.body", "body": body})
