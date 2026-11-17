"""Keep the index current while the service runs.

`run` scans once at startup, then watches `<claude_home>/projects` with `watchfiles` (inotify) and
scans again when a transcript changes, at most about once per `debounce_ms` while files keep
changing. A full rescan also runs every `rescan_every` seconds as a safety net for missed events.
Scans are incremental (see `vigil.index.scanner`), so a rescan that finds nothing new is cheap.

Scans run in a worker thread with their own index connection, so the event loop never blocks on
SQLite or file reads. A failing scan is logged and retried on the next change or rescan; the
watcher itself only stops when its task is cancelled. It only reads under the Claude home.
"""

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from watchfiles import Change, awatch

from vigil.index.scanner import ScanStats, scan

log = logging.getLogger(__name__)

DEBOUNCE_MS = 1000
RESCAN_EVERY = 600.0
TICK_MS = 1000
"""How often (ms) the watch wakes up with no changes, to check whether a rescan is due."""
RETRY_AFTER = 30.0
"""How long (s) to wait before watching again when `projects` is missing or the watch failed."""


def _is_transcript(change: Change, path: str) -> bool:
    return path.endswith(".jsonl")


def _scan_once(conn_factory: Callable[[], sqlite3.Connection], claude_home: Path) -> ScanStats:
    conn = conn_factory()
    try:
        return scan(conn, claude_home)
    finally:
        conn.close()


async def _scan(
    conn_factory: Callable[[], sqlite3.Connection],
    claude_home: Path,
    on_scan: Callable[[ScanStats], None] | None,
) -> None:
    try:
        stats = await asyncio.to_thread(_scan_once, conn_factory, claude_home)
    except Exception:
        log.exception("scanning %s failed; will retry", claude_home)
        return
    if stats.sessions_updated or stats.errors:
        log.info(
            "scanned %d transcripts: %d sessions updated, %d errors",
            stats.files_seen,
            stats.sessions_updated,
            stats.errors,
        )
    if on_scan is not None:
        on_scan(stats)


async def run(
    conn_factory: Callable[[], sqlite3.Connection],
    claude_home: Path,
    *,
    debounce_ms: int = DEBOUNCE_MS,
    rescan_every: float = RESCAN_EVERY,
    tick_ms: int = TICK_MS,
    retry_after: float = RETRY_AFTER,
    on_scan: Callable[[ScanStats], None] | None = None,
    on_ready: Callable[[], None] | None = None,
) -> None:
    """Scan, then watch `claude_home/projects` and rescan on change, until cancelled.

    `on_scan` is called after every successful scan. `on_ready` is called once, when the watch is
    in place: a transcript written after that is indexed within about `debounce_ms` plus the time
    the scan takes.
    """
    projects = claude_home / "projects"
    await _scan(conn_factory, claude_home, on_scan)
    ready = False
    while True:
        if not projects.is_dir():
            # Nothing to watch yet (Claude Code has not run); look again later.
            await asyncio.sleep(retry_after)
            await _scan(conn_factory, claude_home, on_scan)
            continue
        last_scan = time.monotonic()
        first = True
        try:
            async for changes in awatch(
                projects,
                watch_filter=_is_transcript,
                debounce=debounce_ms,
                rust_timeout=tick_ms,
                yield_on_timeout=True,
            ):
                # The first yield (a change or a tick) proves the watch exists; scan once more to
                # catch anything written between the startup scan and the watch starting.
                due = time.monotonic() - last_scan >= rescan_every
                if changes or first or due:
                    await _scan(conn_factory, claude_home, on_scan)
                    last_scan = time.monotonic()
                first = False
                if not ready:
                    ready = True
                    if on_ready is not None:
                        on_ready()
        except OSError as error:
            log.warning("cannot watch %s: %s; retrying in %s s", projects, error, retry_after)
        # The watch ended (the folder went away, or an error); scan, wait, and watch again.
        await asyncio.sleep(retry_after)
        await _scan(conn_factory, claude_home, on_scan)
