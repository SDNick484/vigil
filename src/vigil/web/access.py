"""Cloudflare Access: check the assertion Access adds to every request it lets through, and map
the identity in it to a `Grant` (`[server.access]`, `vigil.config.AccessConfig`).

Access signs a JWT for each request and sends it in `Cf-Access-Jwt-Assertion` (only that header
is read; the `CF_Authorization` cookie is not). `AccessVerifier.verify` accepts it only when:

- it is signed with RS256 (no other algorithm, whatever the token or the key says) by one of the
  team's keys, found by the `kid` in its header;
- `aud` names one of `aud`, `iss` is `https://<team_domain>`, and `exp` and `iat` are there;
- `exp` is not past, and `iat` and `nbf` (when present) are not ahead, of the verifier's clock,
  with `LEEWAY` seconds either way;
- its identity is listed: a user's `email` claim (any case) in `emails` gets `full`; a service
  token's `common_name` claim (Access sets it to the token's client ID) gets the scope configured
  for that client ID.

Anything else is refused, with the reason as text (the guard answers 403 with it).

The team's keys come from `https://<team_domain>/cdn-cgi/access/certs`, fetched as peers are
(no redirects followed, no proxy or `.netrc` from the environment, nothing sent but the request),
within `CERTS_TIMEOUT` seconds and `MAX_CERTS_BYTES`. They are kept for `KEYS_TTL` seconds and
never used after that. A fetch runs in a worker thread, so the event loop (and with it every
loopback request) keeps going while the endpoint is slow, and under one lock, so concurrent
assertions trigger one fetch. Keys are fetched when there are none fresh, or when an assertion
names a `kid` not among them, but never sooner than `KID_REFETCH_AFTER` seconds after the last
attempt, so neither made-up `kid`s nor an endpoint that is down cause a fetch per request. With
no keys every assertion is refused (fail closed).
"""

import asyncio
import json
import math
import time
from collections.abc import Callable
from typing import Any, cast

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from jwt.algorithms import RSAAlgorithm

from vigil.config import AccessConfig
from vigil.web.auth import Grant

ASSERTION_HEADER = "cf-access-jwt-assertion"
"""The request header holding the Access assertion (lowercase, as ASGI gives header names)."""

KEYS_TTL = 3600.0
"""Seconds the team's keys are used after they were fetched."""

KID_REFETCH_AFTER = 60.0
"""Seconds after a fetch attempt before the next one, for an unknown `kid` or after a failure."""

LEEWAY = 30
"""Seconds of clock difference allowed on `exp`, `iat` and `nbf`."""

CERTS_TIMEOUT = 3.0
"""Seconds the certs endpoint has to connect, and to send its answer."""

MAX_CERTS_BYTES = 1024 * 1024
"""Largest certs answer read."""

MAX_ASSERTION_LENGTH = 16 * 1024
"""Longest assertion looked at; Access's are a few kilobytes at most."""

MIN_KEY_BITS = 2048
"""Shortest RSA key accepted from the certs endpoint."""

ALGORITHM = "RS256"

_REQUIRED = ["exp", "iat", "iss", "aud"]


class AccessError(Exception):
    """The team's keys could not be fetched or used; the message says why."""


def _shown(text: str) -> str:
    """An identity from a verified assertion, made safe to put in a message."""
    return "".join(char if char.isprintable() else "?" for char in text[:254])


def _rsa_key(entry: object) -> tuple[str, RSAPublicKey] | None:
    """A JWK from the certs answer as `(kid, key)`, when it is an RS256 signing key with a `kid`
    and at least `MIN_KEY_BITS` bits; None for anything else."""
    if not isinstance(entry, dict):
        return None
    jwk = cast(dict[str, Any], entry)
    kid = jwk.get("kid")
    if (
        not isinstance(kid, str)
        or not kid
        or jwk.get("kty") != "RSA"
        or jwk.get("use", "sig") != "sig"
        or jwk.get("alg", ALGORITHM) != ALGORITHM
        or "d" in jwk
    ):
        return None
    try:
        key = RSAAlgorithm.from_jwk(jwk)
    except jwt.PyJWTError, ValueError, TypeError, KeyError:
        return None
    if not isinstance(key, RSAPublicKey) or key.key_size < MIN_KEY_BITS:
        return None
    return kid, key


def _time_claim(claims: dict[str, Any], name: str) -> float | str | None:
    """Claim `name` as a number, None when absent, or the reason it is unusable."""
    value = claims.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return f"claim {name} is not a time"
    return float(value)


class AccessVerifier:
    """Checks Access assertions for one `[server.access]` (see the module docstring).
    `transport` replaces the network for the certs fetch (tests); `clock` is the time the
    assertion's `exp`, `iat` and `nbf` are checked against, and `monotonic` times the key
    cache."""

    def __init__(
        self,
        config: AccessConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.certs_url = f"https://{config.team_domain}/cdn-cgi/access/certs"
        self.issuer = f"https://{config.team_domain}"
        self._transport = transport
        self._clock = clock
        self._monotonic = monotonic
        self._services = {
            token.client_id: Grant(token.scope, token.machine) for token in config.service_tokens
        }
        self._keys: dict[str, RSAPublicKey] = {}
        self._fetched_at: float | None = None
        self._attempted_at: float | None = None
        self._problem: str | None = None
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    def fetch_keys(self) -> dict[str, RSAPublicKey]:
        """The team's RS256 signing keys by `kid`, fetched now (blocking). Raises AccessError
        when the endpoint cannot be reached, does not answer 200 within `CERTS_TIMEOUT` seconds
        and `MAX_CERTS_BYTES`, or answers no usable key."""
        deadline = time.monotonic() + CERTS_TIMEOUT
        body = bytearray()
        try:
            with (
                httpx.Client(
                    transport=self._transport,
                    timeout=httpx.Timeout(CERTS_TIMEOUT),
                    follow_redirects=False,
                    trust_env=False,
                ) as client,
                client.stream(
                    "GET", self.certs_url, headers={"Accept": "application/json"}
                ) as response,
            ):
                if response.status_code != 200:
                    note = " (a redirect, which is not followed)" if response.is_redirect else ""
                    raise AccessError(f"certs endpoint answered HTTP {response.status_code}{note}")
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) > MAX_CERTS_BYTES:
                        raise AccessError(
                            f"certs endpoint answer larger than {MAX_CERTS_BYTES} bytes"
                        )
                    if time.monotonic() > deadline:
                        raise AccessError(f"certs endpoint timed out after {CERTS_TIMEOUT:g} s")
        except httpx.TimeoutException:
            raise AccessError(f"certs endpoint timed out after {CERTS_TIMEOUT:g} s") from None
        except httpx.HTTPError as error:
            raise AccessError(f"certs endpoint unreachable: {error}") from None
        try:
            data = json.loads(bytes(body))
        except ValueError, RecursionError:
            raise AccessError("certs endpoint answered something that is not JSON") from None
        entries = data.get("keys") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            raise AccessError("certs endpoint answered JSON without a list of keys")
        keys: dict[str, RSAPublicKey] = {}
        for entry in entries:
            found = _rsa_key(entry)
            if found is not None and found[0] not in keys:
                keys[found[0]] = found[1]
        if not keys:
            raise AccessError("certs endpoint answered no usable RS256 keys")
        return keys

    def _fresh(self, now: float) -> bool:
        return self._fetched_at is not None and now - self._fetched_at < KEYS_TTL

    def _loop_lock(self) -> asyncio.Lock:
        """The fetch lock. The service runs one event loop; a new loop (each `asyncio.run` in
        tests) gets a new lock, as an asyncio lock belongs to one loop."""
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock, self._lock_loop = asyncio.Lock(), loop
        return self._lock

    async def _refresh(self, now: float) -> None:
        """Fetch the keys in a worker thread and keep them, or why the fetch failed."""
        self._attempted_at = now
        try:
            keys = await asyncio.to_thread(self.fetch_keys)
        except AccessError as error:
            self._problem = str(error)
        except Exception as error:  # any failure leaves no keys: fail closed
            self._problem = f"certs endpoint answer unusable ({type(error).__name__})"
        else:
            self._keys, self._fetched_at, self._problem = keys, now, None

    async def _key(self, kid: str) -> RSAPublicKey | str:
        """The team's key named `kid`, fetching the keys when that is due; else the reason there
        is none."""
        if self._fresh(self._monotonic()) and kid in self._keys:
            return self._keys[kid]
        async with self._loop_lock():
            now = self._monotonic()
            if not self._fresh(now):
                self._keys = {}  # older than KEYS_TTL: never used again
            due = self._attempted_at is None or now - self._attempted_at >= KID_REFETCH_AFTER
            if kid not in self._keys and due:
                # Shielded: a request that goes away does not cut the fetch short for the rest.
                await asyncio.shield(self._refresh(now))
            key = self._keys.get(kid)
        if key is not None:
            return key
        if not self._keys:
            return f"no Access signing keys ({self._problem or 'not fetched yet'})"
        return f"unknown signing key {_shown(kid)!r}"

    def _grant(self, claims: dict[str, Any]) -> Grant | str:
        email = claims.get("email")
        if isinstance(email, str) and email:
            if email.lower() in self.config.emails:
                return Grant("full")
            return f"identity not allowed: {_shown(email)}"
        client_id = claims.get("common_name")
        if isinstance(client_id, str) and client_id:
            grant = self._services.get(client_id)
            if grant is not None:
                return grant
            return f"identity not allowed: {_shown(client_id)}"
        return "assertion has no identity (email or common_name)"

    def _times_problem(self, claims: dict[str, Any]) -> str | None:
        now = self._clock()
        times: dict[str, float | None] = {}
        for name in ("exp", "iat", "nbf"):
            value = _time_claim(claims, name)
            if isinstance(value, str):
                return value
            times[name] = value
        exp, iat, nbf = times["exp"], times["iat"], times["nbf"]
        if exp is None or iat is None:
            return "assertion lacks exp or iat"
        if exp <= now - LEEWAY:
            return "assertion expired (exp)"
        if iat > now + LEEWAY:
            return "assertion issued in the future (iat)"
        if nbf is not None and nbf > now + LEEWAY:
            return "assertion not valid yet (nbf)"
        return None

    def _check(self, assertion: str, key: RSAPublicKey) -> Grant | str:
        try:
            claims = jwt.decode(
                assertion,
                key,
                algorithms=[ALGORITHM],
                audience=list(self.config.aud),
                issuer=self.issuer,
                leeway=LEEWAY,
                # Times are checked against the verifier's clock below (PyJWT reads the system's).
                options={
                    "require": _REQUIRED,
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                },
            )
        except jwt.InvalidSignatureError:
            return "bad signature"
        except jwt.MissingRequiredClaimError as error:
            return f"assertion lacks claim {error.claim}"
        except jwt.InvalidAudienceError:
            return "wrong audience (aud)"
        except jwt.InvalidIssuerError:
            return "wrong issuer (iss)"
        except jwt.PyJWTError as error:
            return f"invalid assertion ({type(error).__name__})"
        problem = self._times_problem(claims)
        if problem is not None:
            return problem
        return self._grant(claims)

    async def verify(self, assertion: str | None) -> Grant | str:
        """The grant for an assertion (`Cf-Access-Jwt-Assertion`'s value), or why it is
        refused."""
        if not assertion:
            return "no Cf-Access-Jwt-Assertion header"
        if len(assertion) > MAX_ASSERTION_LENGTH:
            return "assertion too long"
        try:
            header = jwt.get_unverified_header(assertion)
        except jwt.PyJWTError:
            return "malformed assertion"
        if header.get("alg") != ALGORITHM:
            return f"assertion not signed with {ALGORITHM}"
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            return "assertion names no signing key (kid)"
        key = await self._key(kid)
        if isinstance(key, str):
            return key
        return self._check(assertion, key)
