"""Waiting for the Remote Control link of a session that was just started.

Claude Code writes a `bridge_status` record to the session's transcript once Remote Control is
up. vigil reads the transcript itself (`vigil.transcript.find_remote_url`) rather than waiting for
the watcher to index it, and only ever reads it.
"""

import glob
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from vigil.transcript import find_remote_url


def find_transcript(claude_home: Path, session_id: str) -> Path | None:
    """The transcript of `session_id` under `claude_home` (`projects/*/<id>.jsonl`), or None when
    there is none (yet). For a session vigil just started under an ID it chose, before the index
    knows it."""
    found = sorted((claude_home / "projects").glob(f"*/{glob.escape(session_id)}.jsonl"))
    return found[0] if found else None


def wait_for_remote_url(
    path: Path | Callable[[], Path | None],
    after: datetime,
    timeout: float,
    *,
    poll: float = 0.5,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> str | None:
    """The Remote Control link written to the transcript `path` after `after` (timezone-aware,
    UTC), waiting up to `timeout` seconds and looking every `poll` seconds. None when none
    appeared in time. A transcript that does not exist (yet) counts as having no link. `path` may
    be a function giving the transcript, called at each look, for one that may not exist yet
    (`find_transcript`); None from it counts as having no link."""
    deadline = clock() + timeout
    while True:
        current = path() if callable(path) else path
        try:
            url = None if current is None else find_remote_url(current, after)
        except OSError:
            url = None
        if url is not None:
            return url
        remaining = deadline - clock()
        if remaining <= 0:
            return None
        sleep(min(poll, remaining))
