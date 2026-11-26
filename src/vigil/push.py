"""The push client: `vigil serve` on a machine with `[hub]` sends this machine's sessions to a hub
(`PUT /ingest/{name}/sessions`, `vigil.web.ingest` on the hub).

`build_snapshot` makes the body: the sessions a hub lists (one per resume chain, unarchived,
active in the last `SNAPSHOT_DAYS` days, newest first, at most `MAX_SNAPSHOT_SESSIONS`), each as
`GET /api/v1/sessions` serves it, with live state from the last probe of `claude agents --json`
(`vigil.web.LiveStatus.peek`; building a snapshot never runs `claude`).
"""

import sqlite3
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import vigil
from vigil.config import Config
from vigil.hub import MAX_SNAPSHOT_SESSIONS, SNAPSHOT_FORMAT
from vigil.index.db import to_db
from vigil.index.queries import list_sessions
from vigil.peers import local_machine, with_machine
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
