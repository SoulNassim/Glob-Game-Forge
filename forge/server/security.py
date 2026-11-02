"""Keep the local studio API local.

Glob listens on 127.0.0.1 and has no login, so any web page the user visits could otherwise
talk to it: a cross-site form POST (CSRF) or a DNS-rebinding page whose domain resolves to
127.0.0.1. Several endpoints write files, spend provider credits or write into connected game folders,
so every request passes this guard first:

* the Host header must name a loopback address (or the host the user explicitly bound to),
  which defeats DNS rebinding;
* /api and /ws requests coming from another site are refused: a browser marks them with
  ``Sec-Fetch-Site: cross-site`` or an ``Origin`` that differs from the Host. Clients
  without an Origin (MCP agents, curl, tests) keep working.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
GUARDED_PREFIXES = ("/api", "/ws")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def is_loopback(host: str) -> bool:
    return host.strip("[]").lower() in LOOPBACK_HOSTS


def _hostname(netloc: str) -> str:
    return (urlsplit(f"//{netloc}").hostname or "").lower()


class LocalOnlyGuard:
    def __init__(self, app: ASGIApp, bind_host: str = "127.0.0.1") -> None:
        self.app = app
        extra = {h.strip().lower() for h in os.environ.get("FORGE_ALLOWED_HOSTS", "").split(",") if h.strip()}
        self.allowed_hosts = LOOPBACK_HOSTS | extra
        # Bound to all interfaces on purpose (LAN use): any Host is accepted, origin checks still apply.
        self.any_host = bind_host in {"0.0.0.0", "::"}
        if not self.any_host and bind_host:
            self.allowed_hosts.add(bind_host.lower())

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        reason = self.reject_reason(scope, headers)
        if reason is None:
            await self.app(scope, receive, send)
        elif scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4403})
        else:
            await PlainTextResponse(f"Forbidden: {reason}", status_code=403)(scope, receive, send)

    def reject_reason(self, scope: Scope, headers: dict[str, str]) -> str | None:
        host_header = headers.get("host", "")
        if not self.any_host and _hostname(host_header) not in self.allowed_hosts:
            return "unexpected Host header (Glob only answers on localhost)"
        path = scope.get("path", "")
        if not path.startswith(GUARDED_PREFIXES):
            return None
        if headers.get("sec-fetch-site") == "cross-site":
            return "cross-site request"
        origin = headers.get("origin")
        if origin and origin != "null" and urlsplit(origin).netloc.lower() != host_header.lower():
            return "cross-origin request"
        if origin == "null" and (scope["type"] == "websocket" or scope.get("method", "GET") not in SAFE_METHODS):
            return "opaque origin"
        return None
