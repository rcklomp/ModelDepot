"""Request guard for the console (2026-10-13, colibri SECURITY-PLAN F1 / SEC1).

The console listens on 127.0.0.1 only and is reached over an ssh tunnel, so the tunnel is who-may-use-it. What is
left is a BROWSER being used against it: a web page on the same machine as the tunnel can make the browser send
requests to localhost (cross-site request forgery) or rebind its own hostname to 127.0.0.1 (DNS rebinding). Two checks
close both:

* Host: every request must name an allowed host (CONSOLE_ALLOWED_HOSTS, default localhost / 127.0.0.1 / [::1], any
  port). A rebound name carries the attacker's hostname here and is refused.
* Changing requests (anything but GET / HEAD / OPTIONS) must carry `X-Console-Request: 1` -- a header a cross-site page
  cannot add without a CORS preflight this app never grants -- and, when the browser sends an Origin, that Origin must
  be the page's own host.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _hostname(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("["):                      # [::1]:8002
        return value[: value.find("]") + 1]
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def allowed_hosts() -> set[str]:
    raw = os.environ.get("CONSOLE_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


class ConsoleGuard(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        host = request.headers.get("host", "")
        if _hostname(host) not in allowed_hosts():
            return JSONResponse({"detail": "host not allowed"}, status_code=403)
        if request.method not in SAFE_METHODS:
            if request.headers.get("x-console-request") != "1":
                return JSONResponse({"detail": "missing X-Console-Request header"}, status_code=403)
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc.lower() != host.lower():
                return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
        return await call_next(request)
