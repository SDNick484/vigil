"""The web service: the app factory (`app`), the loopback guard (`security`), the JSON API and the
POST actions (`api`), and the transcript watcher (`watcher`).

What route modules share lives here: `state(request)` gives the service's `WebState`, and
`connection(request)` opens an index connection for the length of a `with` block. Open the
connection inside the handler that uses it: sqlite3 connections belong to the thread that made them.
"""

import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import cast

from fastapi import Request

from vigil.claude_cli import (
    Capabilities,
    LiveSession,
    capabilities,
    live_sessions,
    live_sessions_or_none,
)
from vigil.config import Config
from vigil.peers import PeerHub
from vigil.review.runner import ReviewSlot

LIVE_TTL = 5.0
"""How long (seconds) live-session status is reused before `claude agents --json` runs again."""


class LiveStatus:
    """The installed claude's capabilities and its live sessions, cached for `ttl` seconds so a
    burst of requests runs `claude` once. Safe to use from several threads."""

    def __init__(self, ttl: float = LIVE_TTL, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._value: tuple[Capabilities, dict[str, LiveSession]] | None = None
        self._at = 0.0

    def get(self, *, fresh: bool = False) -> tuple[Capabilities, dict[str, LiveSession]]:
        """`(capabilities, live sessions)`; with `fresh`, probed now rather than from the cache."""
        with self._lock:
            now = self._clock()
            if fresh or self._value is None or now - self._at >= self._ttl:
                caps = capabilities()
                self._value = (caps, live_sessions(caps))
                self._at = now
            return self._value

    def probe(self) -> dict[str, LiveSession] | None:
        """The live sessions, probed now; None when they could not be listed (the cached value is
        then dropped, not replaced by an empty one)."""
        with self._lock:
            caps = capabilities()
            live = live_sessions_or_none(caps)
            self._value = (caps, live) if live is not None else None
            self._at = self._clock()
            return live

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
