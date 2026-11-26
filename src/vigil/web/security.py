"""Who may talk to the service. This module is the one place access is decided; route handlers
never check it themselves.

Without remote access (no `server.token_file` or `[[server.tokens]]`; the default, as in v1):

- The service binds to 127.0.0.1 only (`check_bind_address`).
- Every request's `Host` must name loopback (`127.0.0.1`, `localhost` or `[::1]`, any port), so a
  page on another site cannot reach the service through DNS rebinding (400 otherwise).
- Every request other than GET and HEAD must carry an `Origin` whose host and port are the
  request's `Host`, so another site cannot make the browser post to the service (403 otherwise).
- A request that came through a proxy (it carries `Forwarded`, `X-Forwarded-*` or `X-Real-IP`) or
  from a non-loopback address is refused (403): remote access needs a token.

With remote access (v2), the service may also bind to another address, and requests are of two
kinds, decided by the address the connection actually comes from (never by forwarding headers,
which a client can write):

- Local: from a loopback address, with a loopback `Host` and no forwarding headers. The v1 rules
  above apply and no token is needed, so loopback keeps working as before.
- Remote: everything else, including anything relayed by a reverse proxy on this machine. `Host`
  may also be one of `server.allowed_hosts` or the bind address. The request must carry
  `Authorization: Bearer <token>` with a configured token (then no `Origin` is needed: browsers
  never add that header on their own) or the session cookie from `/login` (then the `Origin` rule
  above still applies to anything but GET and HEAD). Without either, a browser asking for a page
  is sent to `/login` (when there is a `full` token to log in with) and anything else gets 401.
  The login page, logout and static files need no credentials. Resume from a remote request
  never opens a terminal on this machine; it returns the attach command
  (`vigil.resume.TERMINAL_ALLOWED`).

Every admitted request has a `Grant`: local requests, the cookie and the public paths are
`full`; a bearer token has the scope it was configured with. `authorize` then decides, from one
table, whether the grant covers the method and path (403 naming the scope needed otherwise):

- `read`: GET and HEAD on `/api/v1/*` only.
- `push`: `PUT /ingest/{name}/sessions` and `POST /ingest/{name}/heartbeat` for its own `name`
  only.
- `full`: everything else vigil's pages can do; not ingest.

The guard records how a request was let in as `scope["vigil.access"]` (`local`, `bearer`,
`cookie` or `public`) and its grant as `scope["vigil.grant"]`.
"""

import ipaddress
import re
import time
from dataclasses import dataclass, field
from typing import Literal, NamedTuple
from urllib.parse import quote, urlsplit

from starlette.datastructures import Headers
from starlette.requests import cookie_parser
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from vigil.config import ServerConfig
from vigil.resume import TERMINAL_ALLOWED
from vigil.web.auth import FULL, SESSION_COOKIE, AuthError, AuthSettings, Grant, load_token

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

GRANT_KEY = "vigil.grant"
"""Where the guard records, in the ASGI scope, the `Grant` a request was let in with."""

API_PREFIX = "/api/v1/"

INGEST_PREFIX = "/ingest/"

_INGEST = re.compile(r"/ingest/([^/]+)/(sessions|heartbeat)")
_INGEST_METHODS = {"sessions": "PUT", "heartbeat": "POST"}


class Admitted(NamedTuple):
    """A request the guard lets in: how, and what it may do."""

    how: Access
    grant: Grant


class BindRefused(Exception):
    """The service may not bind to the address asked for."""


def check_bind_address(host: str, server: ServerConfig) -> None:
    """Raise BindRefused unless vigil may listen on `host`: 127.0.0.1 always; any other IP
    address only with remote access configured (`server.remote_enabled`) and every configured
    token file one that `vigil.web.auth.load_token` accepts (it exists, is private to the user,
    mode 0600, and holds a long enough token)."""
    if host == BIND_ADDRESS:
        return
    if not server.remote_enabled:
        raise BindRefused(
            f"refusing to listen on {host}: vigil serves on {BIND_ADDRESS} only unless remote "
            "access is configured (server.token_file, [[server.tokens]] or [server.access]; see "
            "Remote access in the README)"
        )
    try:
        ipaddress.ip_address(host)
    except ValueError:
        raise BindRefused(f"refusing to listen on {host}: not an IP address") from None
    files = [server.token_file] if server.token_file is not None else []
    files += [token.file for token in server.tokens]
    for path in files:
        try:
            load_token(path)
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


def _local(scope: Scope, headers: Headers, host: str) -> Admitted | Refusal:
    """The v1 rules, for a request from this machine."""
    if scope["type"] == "http" and scope["method"] not in SAFE_METHODS:
        if not is_same_origin(headers.get("origin"), host):
            return _json(403, _CROSS_ORIGIN)
    return Admitted("local", FULL)


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


def _remote(scope: Scope, headers: Headers, host: str, auth: AuthSettings) -> Admitted | Refusal:
    """The rules for a request from another machine (or through a proxy)."""
    authorization = headers.get("authorization")
    if authorization is not None:
        credential = auth.bearer(authorization)
        if credential is not None:
            return Admitted("bearer", credential.grant)
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
        return Admitted("cookie" if signed_in else "public", FULL)
    if auth.can_log_in and _wants_page(scope, headers):
        return _login_redirect(scope)
    return _json(
        401,
        "authentication required: send Authorization: Bearer <token>, or log in at /login",
        {"WWW-Authenticate": 'Bearer realm="vigil"'},
    )


def check_request(scope: Scope, auth: AuthSettings | None) -> Admitted | Refusal:
    """How the request in `scope` is let in and with what grant, or why it is refused (the rules
    in this module's docstring). Whether the grant covers the request is `authorize`'s part."""
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
                403,
                "remote access is not enabled: set server.token_file or [[server.tokens]] "
                "(see the README)",
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


def _needs(scope: str) -> Refusal:
    return _json(403, f"needs scope {scope}")


def authorize(grant: Grant, method: str, path: str) -> Refusal | None:
    """None when `grant` covers `method` on `path`, else a 403 naming the scope needed:

    - Ingest (anything under `/ingest/`) is only ever `PUT /ingest/{name}/sessions` and
      `POST /ingest/{name}/heartbeat` with a `push` grant bound to `name`; `full` does not cover
      it either.
    - `full` covers everything else.
    - `read` covers GET and HEAD under `/api/v1/` only.
    - `push` covers nothing else.
    """
    if path.startswith(INGEST_PREFIX):
        name = path[len(INGEST_PREFIX) :].partition("/")[0]
        ingest = _INGEST.fullmatch(path)
        if (
            ingest is not None
            and method == _INGEST_METHODS[ingest[2]]
            and grant.scope == "push"
            and grant.machine == name
        ):
            return None
        return _needs(f"push for {name}" if name else "push")
    if grant.scope == "full":
        return None
    api_read = method in SAFE_METHODS and path.startswith(API_PREFIX)
    if grant.scope == "read" and api_read:
        return None
    if grant.scope == "push" and api_read:
        return _needs("read")
    return _needs("full")


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
        admitted = check_request(scope, self.auth)
        if not isinstance(admitted, Refusal):
            # A WebSocket handshake is a GET.
            method = scope["method"] if scope["type"] == "http" else "GET"
            refusal = authorize(admitted.grant, method, scope["path"])
            if refusal is not None:
                admitted = refusal
        if isinstance(admitted, Refusal):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008, "reason": admitted.reason})
                return
            await admitted.response()(scope, receive, send)
            return
        scope[ACCESS_KEY] = admitted.how
        scope[GRANT_KEY] = admitted.grant
        reset = TERMINAL_ALLOWED.set(admitted.how == "local")
        try:
            await self.app(scope, receive, send)
        finally:
            TERMINAL_ALLOWED.reset(reset)
