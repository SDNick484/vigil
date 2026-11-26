"""Remote access (v2): the access tokens, the browser session cookie, and the login page
(`router`).

Remote access is on only when `server.token_file`, `[[server.tokens]]` or `[server.access]` is
set (`ServerConfig.remote_enabled`). Every token is a `Credential` with a `Grant`: a scope (`read`,
`push` for one machine, or `full`; `server.token_file` is `full`). Then:

- API clients send a token in every request: `Authorization: Bearer <token>`. The request may do
  what the token's grant allows (`vigil.web.security.authorize`).
- Browsers post a `full` token once to `/login` and get a session cookie (`vigil_session`):
  HttpOnly, SameSite=Lax, Path=/, Secure when the page was loaded over HTTPS, valid for 30 days,
  and as good as a `full` token. Lax, so that following a link from another site (a hub's
  "Resume on <machine>") arrives signed in; every state-changing request is a POST, and the guard
  refuses a POST made with the cookie unless its `Origin` is this service. Its value is an
  expiry time and an HMAC-SHA256 over that time and a digest of the `full` token (of all of
  them, when there are several), keyed with a random key kept in the data dir (`session.key`,
  created 0600). Changing a `full` token or deleting the key logs every browser out. With no
  `full` token there is no login page.
- With `[server.access]`, requests through a proxy carry a Cloudflare Access assertion instead
  (`vigil.web.access.AccessVerifier`, held in `AuthSettings.access`). They never get the login
  page or a cookie: Access signs every request.

Who needs to authenticate, and how a request is checked, is decided in `vigil.web.security`; this
module only provides the pieces. Tokens and cookies are compared in constant time
(`hmac.compare_digest`), and a token is compared with every credential, without stopping at the
first match.
"""

import base64
import hashlib
import hmac
import ipaddress
import os
import secrets
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import httpx
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from vigil.config import AccessScope, ServerConfig
from vigil.web.pages import templates

if TYPE_CHECKING:
    from vigil.web.access import AccessVerifier

SESSION_COOKIE = "vigil_session"
SESSION_MAX_AGE = 30 * 24 * 3600
"""How long (seconds) a login lasts."""

SIGNING_KEY_FILE = "session.key"
"""The cookie signing key's file name, in the data dir."""

KEY_BYTES = 32

MIN_TOKEN_LENGTH = 32
MAX_TOKEN_LENGTH = 4096

_COOKIE_CONTEXT = b"vigil-session-v1"


class AuthError(Exception):
    """The token file or the signing key cannot be used; the message says why and what to do."""


def _check_private(info: os.stat_result, path: Path, what: str) -> None:
    """Refuse a file that is not a regular file of ours, readable by nobody else."""
    if not stat.S_ISREG(info.st_mode):
        raise AuthError(f"{what} {path} is not a regular file")
    if info.st_uid != os.geteuid():
        raise AuthError(f"{what} {path} is not owned by the user running vigil")
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        raise AuthError(f"{what} {path} has mode {mode:04o}; it must be 0600 (chmod 600 {path})")


def _read_private(path: Path, what: str, flags: int, limit: int) -> bytes:
    """Up to `limit` bytes of `path`, after `_check_private` passes on the open file (so the file
    checked is the file read). O_NONBLOCK: opening a FIFO must not hang; it is then refused as
    not a regular file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | flags)
    except OSError as error:
        raise AuthError(f"cannot read {what} {path}: {error.strerror or error}") from error
    try:
        _check_private(os.fstat(fd), path, what)
        data = b""
        while len(data) < limit and (chunk := os.read(fd, limit - len(data))):
            data += chunk
        return data
    except OSError as error:
        raise AuthError(f"cannot read {what} {path}: {error.strerror or error}") from error
    finally:
        os.close(fd)


def load_token(path: Path) -> str:
    """The access token in `path`: the file's text without surrounding whitespace.

    Raises AuthError unless the file exists, is a regular file owned by the current user with no
    group or other permissions (0600, or 0400), and holds 32 to 4096 printable ASCII characters
    without spaces.
    """
    data = _read_private(path, "token file", 0, MAX_TOKEN_LENGTH + 2)
    try:
        token = data.decode("ascii").strip()
    except UnicodeDecodeError:
        raise AuthError(f"token file {path} must hold printable ASCII only") from None
    if not token.isprintable() or any(char.isspace() for char in token):
        raise AuthError(f"token file {path} must hold one token: printable ASCII without spaces")
    if not MIN_TOKEN_LENGTH <= len(token) <= MAX_TOKEN_LENGTH:
        raise AuthError(
            f"token file {path} must hold {MIN_TOKEN_LENGTH} to {MAX_TOKEN_LENGTH} characters; "
            "make one with: python3 -c 'import secrets; print(secrets.token_urlsafe(32))'"
        )
    return token


def _create_key(path: Path) -> None:
    """Write a new random key to `path` (0600) unless a key is already there. The key is written
    to a temporary file and linked into place, so no reader ever sees a half-written key."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(secrets.token_bytes(KEY_BYTES))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass  # another vigil made one meanwhile; use that
    finally:
        temporary.unlink(missing_ok=True)


def load_signing_key(path: Path) -> bytes:
    """The cookie signing key in `path`, created (mode 0600, in a 0700 folder) when missing.

    Raises AuthError when the key file is not a private regular file of the current user or is
    shorter than 32 bytes, or cannot be created.
    """
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not path.exists() and not path.is_symlink():
            _create_key(path)
    except OSError as error:
        raise AuthError(f"cannot create signing key {path}: {error.strerror or error}") from error
    key = _read_private(path, "signing key", os.O_NOFOLLOW, 1024)
    if len(key) < KEY_BYTES:
        raise AuthError(f"signing key {path} is too short; delete it and vigil makes a new one")
    return key


def _digest(text: str) -> bytes:
    return hashlib.sha256(text.encode("utf-8", "replace")).digest()


def token_matches(given: str, expected: str) -> bool:
    """Whether `given` is the token, compared in constant time. Both are hashed first, so the
    comparison does not reveal the token's length either."""
    return hmac.compare_digest(_digest(given), _digest(expected))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class Grant:
    """What an admitted request may do: a scope, and for `push` the one machine it pushes for."""

    scope: AccessScope
    machine: str | None = None
    browser: bool = field(default=False, compare=False)
    """The credential is one a browser sends on its own (a user signed in through Cloudflare
    Access), so, as for the cookie, a request that changes something needs this service's
    `Origin`. It says how the grant arrived, not what it allows, so it is not part of equality."""


FULL = Grant("full")
"""Everything vigil's own pages can do (not ingest): local requests, the cookie, `full` tokens."""


@dataclass(frozen=True)
class Credential:
    """A configured access token and what it grants."""

    token: str = field(repr=False)
    grant: Grant


@dataclass(frozen=True)
class AuthSettings:
    """What the guard needs to check remote requests. Built by `load_auth`."""

    credentials: tuple[Credential, ...]
    signing_key: bytes = field(repr=False)
    allowed_hosts: frozenset[str] = frozenset()
    """Host names (as `security.host_name` gives them) remote requests may use, besides the
    loopback names."""
    max_age: int = SESSION_MAX_AGE
    access: AccessVerifier | None = None
    """Checks Cloudflare Access assertions on proxied requests (`[server.access]`); None when it
    is not configured."""

    def _match(self, token: str) -> Credential | None:
        """The credential whose token is `token`. Every credential is compared, in constant
        time and without stopping at a match, so the timing shows neither which one matched
        nor whether any did."""
        found: Credential | None = None
        for credential in self.credentials:
            if token_matches(token, credential.token):
                found = credential
        return found

    def bearer(self, authorization: str) -> Credential | None:
        """The credential an `Authorization` header value (`Bearer <token>`) names, if any."""
        scheme, _, token = authorization.strip().partition(" ")
        if scheme.lower() != "bearer":
            return None
        return self._match(token.strip())

    def login_ok(self, token: str) -> bool:
        """Whether `token` may log a browser in: it is a `full` token."""
        credential = self._match(token)
        return credential is not None and credential.grant.scope == "full"

    @property
    def can_log_in(self) -> bool:
        """Whether there is a `full` token to log in with (else no login page and no cookie)."""
        return any(credential.grant.scope == "full" for credential in self.credentials)

    def _cookie_secret(self) -> bytes:
        # With one full token this is its digest, as before scopes, so existing cookies stay
        # valid; with several, a digest of all of them.
        digests = sorted(_digest(c.token) for c in self.credentials if c.grant.scope == "full")
        if len(digests) == 1:
            return digests[0]
        return hashlib.sha256(b"".join(digests)).digest()

    def _mac(self, expires: int) -> str:
        # The full tokens' digest is part of the signed text, so a new token voids old cookies.
        message = b"|".join((_COOKIE_CONTEXT, str(expires).encode(), self._cookie_secret()))
        return _b64(hmac.new(self.signing_key, message, hashlib.sha256).digest())

    def issue_cookie(self, now: float) -> str:
        """A session cookie value that is valid for `max_age` seconds from `now`."""
        expires = int(now) + self.max_age
        return f"{expires}.{self._mac(expires)}"

    def cookie_ok(self, value: str | None, now: float) -> bool:
        """Whether `value` is a session cookie this service issued and that has not expired.
        Never, when there is no `full` token to log in with."""
        if not value or not self.can_log_in:
            return False
        expires_text, _, mac = value.partition(".")
        if not (expires_text.isascii() and expires_text.isdigit() and len(expires_text) <= 12):
            return False
        expires = int(expires_text)
        if not now < expires <= now + self.max_age + 60:
            return False
        return hmac.compare_digest(mac.encode("utf-8", "replace"), self._mac(expires).encode())


def _bind_host(bind: str) -> str | None:
    """The bind address as a `Host` name, when it is one clients can use (not a wildcard and not
    loopback, whose names are always allowed)."""
    try:
        address = ipaddress.ip_address(bind)
    except ValueError:
        return None
    if address.is_unspecified or address.is_loopback:
        return None
    return f"[{address}]" if address.version == 6 else str(address)


def _load_credentials(server: ServerConfig) -> tuple[Credential, ...]:
    """`server.token_file` (full) and every `[[server.tokens]]` entry, read and checked. Raises
    AuthError when a file cannot be used or two of them hold the same token."""
    named: list[tuple[str, Credential]] = []
    if server.token_file is not None:
        named.append(("server.token_file", Credential(load_token(server.token_file), FULL)))
    for number, entry in enumerate(server.tokens, start=1):
        where = f"server.tokens[{number}]"
        credential = Credential(load_token(entry.file), Grant(entry.scope, entry.machine))
        for name, other in named:
            if token_matches(credential.token, other.token):
                raise AuthError(f"{where} holds the same token as {name}; give each its own token")
        named.append((where, credential))
    return tuple(credential for _, credential in named)


def load_auth(
    server: ServerConfig, data_dir: Path, *, access_transport: httpx.BaseTransport | None = None
) -> AuthSettings | None:
    """The settings for remote access, or None when it is not configured
    (`server.remote_enabled`; loopback only, as in v1). Reads the tokens and the signing key
    (creating the key in `data_dir`); raises AuthError when one cannot be used, or when two
    entries hold the same token. The bind address, unless it is a wildcard, is an allowed host
    too. With `server.access`, assertions are checked by an `AccessVerifier`, whose certs fetches
    go through `access_transport` when given (tests)."""
    # Imported here: vigil.web.access imports this module.
    from vigil.web.access import AccessVerifier

    if not server.remote_enabled:
        return None
    credentials = _load_credentials(server)
    hosts = set(server.allowed_hosts)
    bind = _bind_host(server.bind)
    if bind is not None:
        hosts.add(bind)
    return AuthSettings(
        credentials=credentials,
        signing_key=load_signing_key(data_dir / SIGNING_KEY_FILE),
        allowed_hosts=frozenset(hosts),
        access=(
            AccessVerifier(server.access, transport=access_transport)
            if server.access is not None
            else None
        ),
    )


def is_https(scheme: str, headers: Mapping[str, str]) -> bool:
    """Whether the browser reached the service over HTTPS: directly, or through a TLS proxy that
    says so in `X-Forwarded-Proto`. Only used to mark the cookie Secure."""
    forwarded = headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return scheme in ("https", "wss") or forwarded == "https"


def safe_next(value: object) -> str:
    """Where to go after logging in: a path on this service, else `/`. Never another site."""
    if (
        isinstance(value, str)
        and value.startswith("/")
        and not value.startswith("//")
        and "\\" not in value
        and value.isprintable()
        and len(value) <= 2048
    ):
        return value
    return "/"


router = APIRouter(tags=["auth"])


def settings(request: Request) -> AuthSettings | None:
    """The remote access settings of the app handling `request`; None when remote access is off."""
    return getattr(request.app.state, "vigil_auth", None)


def _off() -> JSONResponse:
    return JSONResponse(
        {
            "detail": "remote access is not enabled "
            "(no server.token_file, [[server.tokens]] or [server.access])"
        },
        status_code=404,
    )


def _no_login() -> JSONResponse:
    return JSONResponse(
        {"detail": "no vigil login here: sign in through Cloudflare Access"}, status_code=404
    )


def _through_access(request: Request) -> bool:
    """Whether the guard let `request` in on a Cloudflare Access assertion."""
    # Imported here: vigil.web.security imports this module.
    from vigil.web.security import ACCESS_KEY

    return request.scope.get(ACCESS_KEY) == "access"


def _login_page(request: Request, next_path: str, error: str | None, status: int) -> Any:
    context = {"next": next_path, "error": error}
    return templates.TemplateResponse(request, "login.html", context, status_code=status)


@router.get("/login")
def login_page(request: Request, next_path: Annotated[str, Query(alias="next")] = "/") -> Response:
    """The login form: one field, the token."""
    auth = settings(request)
    if auth is None:
        return _off()
    if not auth.can_log_in or _through_access(request):
        return _no_login()
    response: Response = _login_page(request, safe_next(next_path), None, 200)
    return response


@router.post("/login")
async def login(request: Request) -> Response:
    """Check the posted `token` (a `full` one); on success set the session cookie and go to
    `next`."""
    auth = settings(request)
    if auth is None:
        return _off()
    if not auth.can_log_in or _through_access(request):
        return _no_login()
    form = await request.form()
    token, next_path = form.get("token"), safe_next(form.get("next"))
    if not isinstance(token, str) or not auth.login_ok(token.strip()):
        response: Response = _login_page(request, next_path, "That is not the token.", 401)
        return response
    response = RedirectResponse(next_path, status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        auth.issue_cookie(time.time()),
        max_age=auth.max_age,
        path="/",
        secure=is_https(request.url.scheme, request.headers),
        httponly=True,
        samesite="lax",
    )
    return response


@router.post("/logout")
def logout(request: Request) -> Response:
    """Forget the session cookie and go to the login page."""
    if _through_access(request):
        return _no_login()
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=is_https(request.url.scheme, request.headers),
        httponly=True,
        samesite="lax",
    )
    return response
