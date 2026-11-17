"""Who may talk to the service. This module is the one place access is decided: v2 adds token and
cookie login here, without touching route handlers.

v1 rules:

- The service binds to 127.0.0.1 only (`check_bind_address`).
- Every request's `Host` must name loopback (`127.0.0.1`, `localhost` or `[::1]`, any port), so a
  page on another site cannot reach the service through DNS rebinding (400 otherwise).
- Every request other than GET and HEAD must carry an `Origin` whose host and port are the
  request's `Host`, so another site cannot make the browser post to the service (403 otherwise).
"""

from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

BIND_ADDRESS = "127.0.0.1"
"""The only address v1 binds to."""

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})

SAFE_METHODS = frozenset({"GET", "HEAD"})


class BindRefused(Exception):
    """The service may not bind to the address asked for."""


def check_bind_address(host: str) -> None:
    """Raise BindRefused unless `host` is the loopback address v1 binds to."""
    if host != BIND_ADDRESS:
        raise BindRefused(
            f"refusing to listen on {host}: vigil serves on {BIND_ADDRESS} only "
            "(remote access is not supported yet)"
        )


def host_name(host: str) -> str | None:
    """The host name of a `Host` header value (`name[:port]` or `[v6]:port`), lowercased, or None
    when it is malformed."""
    host = host.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        if end == -1:
            return None
        name, rest = host[: end + 1], host[end + 1 :]
    else:
        name, sep, port = host.partition(":")
        rest = sep + port
    if rest and not (rest.startswith(":") and rest[1:].isdigit()):
        return None
    return name or None


def is_loopback_host(host: str | None) -> bool:
    """Whether a `Host` header value names this machine's loopback interface."""
    return host is not None and host_name(host) in LOOPBACK_HOSTS


def is_same_origin(origin: str | None, host: str) -> bool:
    """Whether an `Origin` header value is an http(s) origin on exactly `host` (name and port)."""
    if not origin:
        return False
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    return (
        parts.scheme in ("http", "https")
        and parts.netloc.lower() == host.strip().lower()
        and parts.path in ("", "/")
        and not parts.query
        and not parts.fragment
    )


def _refusal(scope: Scope) -> tuple[int, str] | None:
    """`(status, reason)` when the request must be refused, else None."""
    headers = Headers(scope=scope)
    host = headers.get("host")
    if host is None or not is_loopback_host(host):
        return 400, "invalid Host header: vigil only answers on 127.0.0.1, localhost or [::1]"
    if scope["type"] == "http" and scope["method"] not in SAFE_METHODS:
        if not is_same_origin(headers.get("origin"), host):
            return 403, "cross-origin request refused: Origin is missing or not this service"
    return None


class LoopbackGuard:
    """ASGI middleware applying the rules above to every HTTP and WebSocket request, before any
    route sees it."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            refusal = _refusal(scope)
            if refusal is not None:
                status, reason = refusal
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008, "reason": reason})
                    return
                await JSONResponse({"detail": reason}, status_code=status)(scope, receive, send)
                return
        await self.app(scope, receive, send)
