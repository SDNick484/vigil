"""Who may talk to the service. This module is the one place access is decided; route handlers
never check it themselves.

Without `server.token_file` (the default, as in v1):

- The service binds to 127.0.0.1 only (`check_bind_address`).
- Every request's `Host` must name loopback (`127.0.0.1`, `localhost` or `[::1]`, any port), so a
  page on another site cannot reach the service through DNS rebinding (400 otherwise).
- Every request other than GET and HEAD must carry an `Origin` whose host and port are the
  request's `Host`, so another site cannot make the browser post to the service (403 otherwise).
- A request that came through a proxy (it carries `Forwarded`, `X-Forwarded-*` or `X-Real-IP`) or
  from a non-loopback address is refused (403): remote access needs a token.

With `server.token_file` (remote access, v2), the service may also bind to another address, and
requests are of two kinds, decided by the address the connection actually comes from (never by
forwarding headers, which a client can write):

- Local: from a loopback address, with a loopback `Host` and no forwarding headers. The v1 rules
  above apply and no token is needed, so loopback keeps working as before.
- Remote: everything else, including anything relayed by a reverse proxy on this machine. `Host`
  may also be one of `server.allowed_hosts` or the bind address. The request must carry
  `Authorization: Bearer <token>` (then no `Origin` is needed: browsers never add that header on
  their own) or the session cookie from `/login` (then the `Origin` rule above still applies to
  anything but GET and HEAD). Without either, a browser asking for a page is sent to `/login`
  and anything else gets 401. The login page, logout and static files need no credentials.
  Resume from a remote request never opens a terminal on this machine; it returns the attach
  command (`vigil.resume.TERMINAL_ALLOWED`).

The guard records how a request was let in as `scope["vigil.access"]`: `local`, `bearer`,
`cookie` or `public`.
"""

import ipaddress
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit

from starlette.datastructures import Headers
from starlette.requests import cookie_parser
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from vigil.resume import TERMINAL_ALLOWED
from vigil.web.auth import SESSION_COOKIE, AuthError, AuthSettings, load_token

BIND_ADDRESS = "127.0.0.1"
"""The address vigil binds to by default, and the only one it binds to without a token."""

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})

SAFE_METHODS = frozenset({"GET", "HEAD"})

FORWARDING_HEADERS = frozenset(
    {"forwarded", "x-real-ip", "x-client-ip", "true-client-ip", "cf-connecting-ip"}
)
"""Headers a reverse proxy adds (besides any `X-Forwarded-*`). A request carrying one came
through a proxy, so it is never treated as local, whatever address it arrives from."""

PUBLIC_PATHS = frozenset({"/login", "/logout"})
"""Paths a remote request may use without credentials (with the `Origin` rule for POST), along
with GET and HEAD under `/static/`."""

Access = Literal["local", "bearer", "cookie", "public"]

ACCESS_KEY = "vigil.access"
"""Where the guard records, in the ASGI scope, how a request was let in."""


class BindRefused(Exception):
    """The service may not bind to the address asked for."""


def check_bind_address(host: str, token_file: Path | None = None) -> None:
    """Raise BindRefused unless vigil may listen on `host`: 127.0.0.1 always; any other IP
    address only with a `token_file` that `vigil.web.auth.load_token` accepts (it exists, is
    private to the user, mode 0600, and holds a long enough token)."""
    if host == BIND_ADDRESS:
        return
    if token_file is None:
        raise BindRefused(
            f"refusing to listen on {host}: vigil serves on {BIND_ADDRESS} only "
            "unless server.token_file is set (see Remote access in the README)"
        )
    try:
        ipaddress.ip_address(host)
    except ValueError:
        raise BindRefused(f"refusing to listen on {host}: not an IP address") from None
    try:
        load_token(token_file)
    except AuthError as error:
        raise BindRefused(f"refusing to listen on {host}: {error}") from None


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


def is_loopback_address(address: str) -> bool:
    """Whether an IP address (as text) is a loopback address, IPv4-mapped ones included."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped.is_loopback
    return ip.is_loopback


def peer_address(scope: Scope) -> str | None:
    """The IP address the connection comes from, as the server saw it; None when the server
    gave none, or something other than an IP address."""
    client = scope.get("client")
    if not client:
        return None
    try:
        return str(ipaddress.ip_address(client[0]))
    except ValueError:
        return None


def is_forwarded(headers: Headers) -> bool:
    """Whether the request carries a header a reverse proxy adds."""
    return any(
        name in FORWARDING_HEADERS or name.startswith("x-forwarded-") for name in headers.keys()
    )


@dataclass(frozen=True)
class Refusal:
    """A request the guard turns away: `status` with `{"detail": reason}`, or, with `location`,
    a redirect there."""

    status: int
    reason: str
    headers: dict[str, str] = field(default_factory=dict)
    location: str | None = None

    def response(self) -> Response:
        if self.location is not None:
            return RedirectResponse(self.location, status_code=self.status)
        return JSONResponse({"detail": self.reason}, status_code=self.status, headers=self.headers)


def _json(status: int, reason: str, headers: dict[str, str] | None = None) -> Refusal:
    return Refusal(status, reason, headers or {})


_CROSS_ORIGIN = "cross-origin request refused: Origin is missing or not this service"


def _local(scope: Scope, headers: Headers, host: str) -> Access | Refusal:
    """The v1 rules, for a request from this machine."""
    if scope["type"] == "http" and scope["method"] not in SAFE_METHODS:
        if not is_same_origin(headers.get("origin"), host):
            return _json(403, _CROSS_ORIGIN)
    return "local"


def _wants_page(scope: Scope, headers: Headers) -> bool:
    """Whether an unauthenticated request is a browser loading a page (sent to the login page),
    rather than an API call or an htmx request (which get 401)."""
    return (
        scope["type"] == "http"
        and scope["method"] in SAFE_METHODS
        and not scope["path"].startswith("/api/")
        and headers.get("hx-request") is None
        and "text/html" in headers.get("accept", "")
    )


def _login_redirect(scope: Scope) -> Refusal:
    target = scope["path"]
    if scope.get("query_string"):
        target += "?" + scope["query_string"].decode("latin-1")
    return Refusal(303, "login required", location="/login?next=" + quote(target, safe=""))


def _remote(scope: Scope, headers: Headers, host: str, auth: AuthSettings) -> Access | Refusal:
    """The rules for a request from another machine (or through a proxy)."""
    authorization = headers.get("authorization")
    if authorization is not None:
        if auth.bearer_ok(authorization):
            return "bearer"
        return _json(
            401,
            "invalid token in the Authorization header",
            {"WWW-Authenticate": 'Bearer realm="vigil", error="invalid_token"'},
        )
    cookie = cookie_parser(headers.get("cookie", "")).get(SESSION_COOKIE)
    signed_in = auth.cookie_ok(cookie, time.time())
    is_http = scope["type"] == "http"
    safe = is_http and scope["method"] in SAFE_METHODS
    public = is_http and (
        scope["path"] in PUBLIC_PATHS or (safe and scope["path"].startswith("/static/"))
    )
    if signed_in or public:
        # The browser sends the cookie on its own, so it is checked against cross-site requests.
        if not safe and not is_same_origin(headers.get("origin"), host):
            return _json(403, _CROSS_ORIGIN)
        return "cookie" if signed_in else "public"
    if _wants_page(scope, headers):
        return _login_redirect(scope)
    return _json(
        401,
        "authentication required: send Authorization: Bearer <token>, or log in at /login",
        {"WWW-Authenticate": 'Bearer realm="vigil"'},
    )


def check_request(scope: Scope, auth: AuthSettings | None) -> Access | Refusal:
    """How the request in `scope` is let in, or why it is refused (the rules in this module's
    docstring)."""
    headers = Headers(scope=scope)
    host = headers.get("host")
    if auth is None:
        if host is None or not is_loopback_host(host):
            return _json(
                400, "invalid Host header: vigil only answers on 127.0.0.1, localhost or [::1]"
            )
        peer = peer_address(scope)
        # The server only listens on loopback here, so a peer that is not an IP address (a test
        # client) is local; a proxied or non-loopback one is not.
        if is_forwarded(headers) or (peer is not None and not is_loopback_address(peer)):
            return _json(
                403, "remote access is not enabled: set server.token_file (see the README)"
            )
        return _local(scope, headers, host)

    name = host_name(host) if host is not None else None
    if host is None or name is None or not (name in LOOPBACK_HOSTS or name in auth.allowed_hosts):
        return _json(
            400,
            "invalid Host header: vigil only answers on 127.0.0.1, localhost, [::1] "
            "and server.allowed_hosts",
        )
    peer = peer_address(scope)
    if (
        peer is not None
        and is_loopback_address(peer)
        and name in LOOPBACK_HOSTS
        and not is_forwarded(headers)
    ):
        return _local(scope, headers, host)
    return _remote(scope, headers, host, auth)


class LoopbackGuard:
    """ASGI middleware applying the rules above to every HTTP and WebSocket request, before any
    route sees it. `auth` is None unless remote access is configured (`vigil.web.auth`)."""

    def __init__(self, app: ASGIApp, auth: AuthSettings | None = None) -> None:
        self.app = app
        self.auth = auth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        access = check_request(scope, self.auth)
        if isinstance(access, Refusal):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008, "reason": access.reason})
                return
            await access.response()(scope, receive, send)
            return
        scope[ACCESS_KEY] = access
        reset = TERMINAL_ALLOWED.set(access == "local")
        try:
            await self.app(scope, receive, send)
        finally:
            TERMINAL_ALLOWED.reset(reset)
