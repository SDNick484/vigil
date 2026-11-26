"""The web service: the app factory (`app`), the loopback guard (`security`), the JSON API and the
POST actions (`api`), and the transcript watcher (`watcher`).

What route modules share lives here: `state(request)` gives the service's `WebState`, and
`connection(request)` opens an index connection for the length of a `with` block. Open the
connection inside the handler that uses it: sqlite3 connections belong to the thread that made them.

In hub mode (`mode = "hub"`) there is no index and `claude` is never run: routes that need either
depend on `local_only` (404 on a hub), and the hub's own routes on `hub_only` (404 elsewhere).
"""

import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import cast

from fastapi import HTTPException, Request

from vigil.claude_cli import (
    Capabilities,
    LiveSession,
    capabilities,
    live_sessions,
    live_sessions_or_none,
)
from vigil.config import Config, load_config
from vigil.hub import HubStore
from vigil.peers import PeerHub
from vigil.push import HubPusher
from vigil.review.runner import ReviewSlot

LIVE_TTL = 5.0
"""How long (seconds) live-session status is reused before `claude agents --json` runs again."""


class LiveStatus:
    """The installed claude's capabilities and its live sessions, cached for `ttl` seconds so a
    burst of requests runs `claude` once. Safe to use from several threads.

    `checked_at` is the wall time (`wall`) of the last successful probe, and `peek` gives the
    cached value with that time whatever its age: the push loop (`vigil.push`) uses a probe a
    page made rather than run `claude` again."""

    def __init__(
        self,
        ttl: float = LIVE_TTL,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._ttl = ttl
        self._clock = clock
        self._wall = wall
        self._lock = threading.Lock()
        self._value: tuple[Capabilities, dict[str, LiveSession]] | None = None
        self._at = 0.0
        self._checked_at: datetime | None = None
        """Wall time of the probe that gave `_value`; kept when a later probe fails."""

    @property
    def checked_at(self) -> datetime | None:
        """Wall time of the last successful probe by `get` or `probe`; None before the first."""
        with self._lock:
            return self._checked_at

    def get(self, *, fresh: bool = False) -> tuple[Capabilities, dict[str, LiveSession]]:
        """`(capabilities, live sessions)`; with `fresh`, probed now rather than from the cache."""
        with self._lock:
            now = self._clock()
            if fresh or self._value is None or now - self._at >= self._ttl:
                caps = capabilities()
                self._value = (caps, live_sessions(caps))
                self._at = now
                self._checked_at = self._wall()
            return self._value

    def probe(self) -> dict[str, LiveSession] | None:
        """The live sessions, probed now; None when they could not be listed (the cached value is
        then dropped, not replaced by an empty one)."""
        with self._lock:
            caps = capabilities()
            live = live_sessions_or_none(caps)
            self._value = (caps, live) if live is not None else None
            self._at = self._clock()
            if live is not None:
                self._checked_at = self._wall()
            return live

    def peek(self) -> tuple[Capabilities, dict[str, LiveSession], datetime] | None:
        """The cached `(capabilities, live sessions, checked_at)`, however old; None when nothing
        is cached. Never runs `claude`."""
        with self._lock:
            if self._value is None or self._checked_at is None:
                return None
            caps, live = self._value
            return caps, live, self._checked_at

    def clear(self) -> None:
        """Forget the cached value, so the next `get` probes again."""
        with self._lock:
            self._value = None


@dataclass
class WebState:
    """Everything the service's handlers share; stored as `app.state.vigil`."""

    config: Config
    conn_factory: Callable[[], sqlite3.Connection]
    live: LiveStatus = field(default_factory=LiveStatus)
    last_scan_at: datetime | None = None
    """When the service's watcher last finished a scan; None until it has."""
    watcher_ready: threading.Event = field(default_factory=threading.Event)
    """Set once the watcher is watching: a transcript change after this is picked up."""
    peers: PeerHub = field(default_factory=lambda: PeerHub(()))
    """The configured peers (v2) and their latest sessions."""
    review_slot: ReviewSlot = field(default_factory=ReviewSlot)
    """Taken while a session review runs: one at a time (`vigil.web.review`)."""
    hub: HubStore | None = None
    """The machines' snapshots; set only in hub mode (`mode = "hub"`)."""
    pusher: HubPusher | None = None
    """Sends this machine's sessions to the hub in `[hub]`; None without `[hub]`."""

    def reload_config(self) -> None:
        """Re-read the config file (`vigil.paths.config_path`, where `vigil serve` reads it) after
        vigil changed it (`vigil.config_edit`), and take its `resume` section. The rest stays as
        the service started with it: the watcher, remote access and peers were set up from it
        then. Raises ConfigError when the file does not load."""
        self.config = replace(self.config, resume=load_config().resume)


def state(request: Request) -> WebState:
    """The service state of the app handling `request`."""
    return cast(WebState, request.app.state.vigil)


@contextmanager
def connection(request: Request) -> Iterator[sqlite3.Connection]:
    """An index connection from the app's factory, closed when the block ends."""
    conn = state(request).conn_factory()
    try:
        yield conn
    finally:
        conn.close()


def local_only(request: Request) -> None:
    """A route dependency: 404 on a hub, for routes that run `claude` or need the index."""
    if state(request).config.mode == "hub":
        raise HTTPException(status_code=404, detail="not available on a hub")


def hub_only(request: Request) -> None:
    """A route dependency: 404 unless this vigil is a hub (`mode = "hub"`)."""
    if state(request).config.mode != "hub":
        raise HTTPException(status_code=404, detail="only available on a hub")
