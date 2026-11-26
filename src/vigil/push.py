"""The push client: `vigil serve` on a machine with `[hub]` sends this machine's sessions to a hub
(`PUT /ingest/{name}/sessions`, `vigil.web.ingest` on the hub).

`build_snapshot` makes the body: the sessions a hub lists (one per resume chain, unarchived,
active in the last `SNAPSHOT_DAYS` days, newest first, at most `MAX_SNAPSHOT_SESSIONS`), each as
`GET /api/v1/sessions` serves it, with live state from the last probe of `claude agents --json`
(`vigil.web.LiveStatus.peek`; building a snapshot never runs `claude`).

`HubPusher` sends it: when it differs from the one the hub last accepted, at most once every
`PUSH_MIN_INTERVAL` seconds; with nothing new, a heartbeat (`POST /ingest/{name}/heartbeat`, the
digest of the sessions last accepted) every `HEARTBEAT_EVERY` seconds, and the whole snapshot at
once when the hub answers 409 (it restarted with no or older data). A failure backs off from
`BACKOFF_START` seconds, doubling up to `BACKOFF_MAX`.

The credential (`[hub]`: an Access service token, else a bearer token) is read from its file for
each request and sent to `hub.url` only, as the peers' tokens are (`vigil.peers`): every request
is checked against that origin before it is sent, redirects are never followed, and proxy settings
and `.netrc` are ignored. It is never logged or put in `last_error`.
"""

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote

import httpx

import vigil
from vigil.config import Config, HubClientConfig
from vigil.hub import MAX_SNAPSHOT_SESSIONS, SNAPSHOT_FORMAT, canonical_digest
from vigil.index.db import to_db
from vigil.index.queries import list_sessions
from vigil.peers import _client, local_machine, with_machine
from vigil.serialize import apply_status, session_to_dict

if TYPE_CHECKING:  # vigil.web imports this module
    from vigil.web import LiveStatus

SNAPSHOT_DAYS = 30
"""A snapshot holds the sessions active in this many days before it was built."""


def build_snapshot(
    conn: sqlite3.Connection, config: Config, live: LiveStatus, now: datetime
) -> dict[str, Any]:
    """This machine's snapshot as of `now`, as `PUT /ingest/{name}/sessions` takes it: `format`,
    `machine` (`config.machine`), `generated_at`, `vigil_version`, `live_checked_at` (when the
    live state in it was probed; null when it never was, and every status is then `unknown`)
    and `sessions`."""
    rows = list_sessions(
        conn,
        since=now - timedelta(days=SNAPSHOT_DAYS),
        include_archived=False,
        collapse_chains=True,
        limit=MAX_SNAPSHOT_SESSIONS,
    )
    cached = live.peek()
    checked_at: datetime | None = None
    if cached is not None:
        caps, sessions, checked_at = cached
        rows = apply_status(rows, caps, sessions)
    machine = local_machine(config)
    return {
        "format": SNAPSHOT_FORMAT,
        "machine": machine,
        "generated_at": to_db(now),
        "vigil_version": vigil.__version__,
        "live_checked_at": to_db(checked_at),
        "sessions": [session_to_dict(row) for row in with_machine(rows, machine)],
    }


PUSH_MIN_INTERVAL = 5.0
"""Least seconds between two snapshots sent."""
HEARTBEAT_EVERY = 60.0
"""Seconds after the hub last accepted something before a heartbeat is sent."""
BACKOFF_START = 5.0
"""Seconds to wait after a first failure; each further failure doubles it."""
BACKOFF_MAX = 300.0
"""Most seconds to wait between two tries."""
CONNECT_TIMEOUT = 3.0
"""Seconds a request may spend connecting to the hub."""
TOTAL_TIMEOUT = 10.0
"""Seconds a request may spend waiting on each step of the exchange with the hub (connecting
apart): getting a connection, sending the body, receiving the answer."""


class PushError(Exception):
    """A request to the hub failed; the message says why (never with the credential)."""


@dataclass(frozen=True)
class PushStatus:
    """How pushing goes: the hub's URL, when it last accepted a snapshot (None when it has not
    since the service started), and why the last request failed (None when it worked)."""

    url: str
    last_push_at: datetime | None
    last_error: str | None


class HubPusher:
    """Sends this machine's snapshots to the hub in `config` as `machine` (`/ingest/{machine}/`).

    `offer` hands it the latest snapshot; `step` does whatever is due, one request or two (a
    heartbeat answered 409 is followed by the snapshot), and says how long until the next thing
    is due. `transport` replaces the network (tests); `clock` (monotonic seconds) times pushes,
    heartbeats and back-off; `wall` stamps `last_push_at` and heartbeats. `offer` and `status`
    may be called from any thread while `step` runs in another."""

    def __init__(
        self,
        config: HubClientConfig,
        machine: str,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.config = config
        self.machine = machine
        self._transport = transport
        self._clock = clock
        self._wall = wall
        self._origin = httpx.URL(config.url)
        self._lock = threading.Lock()
        """Guards everything below; never held during a request."""
        self._stepping = threading.Lock()
        """Held while `step` runs, so two steps never overlap."""
        self._offered: dict[str, Any] | None = None
        self._offered_digest: str | None = None
        self._accepted_digest: str | None = None
        """Digest of the sessions the hub last accepted (by a push or a heartbeat answered 200)."""
        self._last_put: float | None = None
        self._last_contact: float | None = None
        """When the hub last accepted something (`clock`)."""
        self._backoff = 0.0
        """The current back-off in seconds; 0 when the last request worked."""
        self._retry_at: float | None = None
        self._last_push_at: datetime | None = None
        self._last_error: str | None = None

    def offer(self, snapshot: dict[str, Any]) -> None:
        """Make `snapshot` (`build_snapshot`) the one to send; it goes out from `step` when its
        sessions differ from those the hub last accepted."""
        digest = canonical_digest(snapshot["sessions"])
        with self._lock:
            self._offered = snapshot
            self._offered_digest = digest

    def status(self) -> PushStatus:
        with self._lock:
            return PushStatus(self.config.url, self._last_push_at, self._last_error)

    def step(self) -> float:
        """Do what is due and return the seconds until the next thing is: nothing during
        back-off; the offered snapshot when it differs from the one the hub last accepted and
        none was sent in the last `PUSH_MIN_INTERVAL` seconds; else a heartbeat when the hub
        accepted nothing for `HEARTBEAT_EVERY` seconds, and on its 409 the snapshot at once."""
        with self._stepping:
            action = self._due(self._clock())
            if action == "push":
                self._push()
            elif action == "heartbeat" and self._heartbeat() == "need_snapshot":
                self._push()
            return self._delay(self._clock())

    def _due(self, now: float) -> Literal["push", "heartbeat"] | None:
        with self._lock:
            if self._retry_at is not None and now < self._retry_at:
                return None
            if self._offered_digest is not None and self._offered_digest != self._accepted_digest:
                if self._last_put is None or now - self._last_put >= PUSH_MIN_INTERVAL:
                    return "push"
                return None
            if self._accepted_digest is not None and (
                self._last_contact is None or now - self._last_contact >= HEARTBEAT_EVERY
            ):
                return "heartbeat"
            return None

    def _delay(self, now: float) -> float:
        with self._lock:
            if self._retry_at is not None and now < self._retry_at:
                return self._retry_at - now
            if self._offered_digest is not None and self._offered_digest != self._accepted_digest:
                if self._last_put is None:
                    return 0.0
                return max(0.0, self._last_put + PUSH_MIN_INTERVAL - now)
            if self._accepted_digest is not None and self._last_contact is not None:
                return max(0.0, self._last_contact + HEARTBEAT_EVERY - now)
            return HEARTBEAT_EVERY

    def _push(self) -> None:
        with self._lock:
            snapshot, digest = self._offered, self._offered_digest
            self._last_put = self._clock()
        if snapshot is None or digest is None:
            return
        body = json.dumps(snapshot, ensure_ascii=False).encode()
        try:
            self._send("PUT", "sessions", body)
        except PushError as error:
            self._failed(f"push: {error}")
            return
        with self._lock:
            self._accepted_digest = digest
            self._last_push_at = self._wall()
            self._succeeded()

    def _heartbeat(self) -> Literal["ok", "need_snapshot", "failed"]:
        with self._lock:
            digest = self._accepted_digest
        body = json.dumps({"generated_at": to_db(self._wall()), "digest": digest}).encode()
        try:
            code = self._send("POST", "heartbeat", body, also_ok=(409,))
        except PushError as error:
            self._failed(f"heartbeat: {error}")
            return "failed"
        with self._lock:
            if code == 409:
                # The hub does not have what was last sent: send it again, now.
                self._accepted_digest = None
                self._last_put = None
                return "need_snapshot"
            self._succeeded()
        return "ok"

    def _succeeded(self) -> None:
        """Called with `_lock` held, after the hub accepted a request."""
        self._last_contact = self._clock()
        self._backoff = 0.0
        self._retry_at = None
        self._last_error = None

    def _failed(self, reason: str) -> None:
        with self._lock:
            self._backoff = (
                BACKOFF_START if self._backoff == 0 else min(self._backoff * 2, BACKOFF_MAX)
            )
            self._retry_at = self._clock() + self._backoff
            self._last_error = reason

    def _credentials(self) -> dict[str, str]:
        """The credential headers, read from their files now. Raises PushError."""
        # Imported here: vigil.web imports this module, so a top-level import would be circular.
        from vigil.web.auth import AuthError, load_token

        config = self.config
        try:
            if config.access_client_id is not None and config.access_secret_file is not None:
                return {
                    "CF-Access-Client-Id": config.access_client_id,
                    "CF-Access-Client-Secret": load_token(config.access_secret_file),
                }
            if config.token_file is not None:
                return {"Authorization": f"Bearer {load_token(config.token_file)}"}
        except AuthError as error:
            raise PushError(str(error)) from None
        raise PushError("[hub] has no credential")

    def _send(self, method: str, what: str, body: bytes, *, also_ok: tuple[int, ...] = ()) -> int:
        """Send `body` to `/ingest/{machine}/{what}` on the hub and return the answer's status: a
        2xx, or one in `also_ok`. Raises PushError for anything else, a redirect included, and
        when the request cannot be sent or answered in time."""
        credentials = self._credentials()
        try:
            return self._exchange(method, what, body, credentials, also_ok)
        except PushError as error:
            raise PushError(_scrub(str(error), credentials)) from None
        except httpx.TimeoutException:
            raise PushError(
                f"timed out (connect {CONNECT_TIMEOUT:g} s, then {TOTAL_TIMEOUT:g} s)"
            ) from None
        except httpx.HTTPError as error:
            raise PushError(_scrub(f"cannot reach the hub: {error}", credentials)) from None
        except Exception as error:  # e.g. a header httpx cannot encode: say what, never the value
            raise PushError(f"the request could not be sent ({type(error).__name__})") from None

    def _exchange(
        self,
        method: str,
        what: str,
        body: bytes,
        credentials: Mapping[str, str],
        also_ok: tuple[int, ...],
    ) -> int:
        path = f"/ingest/{quote(self.machine, safe='')}/{what}"
        timeout = httpx.Timeout(TOTAL_TIMEOUT, connect=CONNECT_TIMEOUT)
        with _client(timeout, self._transport) as client:
            request = client.build_request(
                method,
                self.config.url + path,
                content=body,
                headers={
                    **credentials,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            url = request.url
            if (url.scheme, url.host, url.port) != (
                self._origin.scheme,
                self._origin.host,
                self._origin.port,
            ):
                raise PushError("refusing to send the hub's credential to another host")
            response = client.send(request, stream=True)
            response.close()  # the answer's status is all that is used
        code = response.status_code
        if response.is_redirect or 300 <= code < 400:
            raise PushError(f"the hub answered {code}, a redirect, which is not followed")
        if 200 <= code < 300 or code in also_ok:
            return code
        if code in (401, 403):
            raise PushError(
                f"the hub refused the request ({code}): check [hub]'s credential and that the "
                f"hub lists this machine ({self.machine!r})"
            )
        raise PushError(f"the hub answered HTTP {code}")


def _scrub(message: str, credentials: Mapping[str, str]) -> str:
    """`message` with every credential value in it replaced by `***`."""
    for value in credentials.values():
        for secret in (value, value.removeprefix("Bearer ")):
            if secret:
                message = message.replace(secret, "***")
    return message
