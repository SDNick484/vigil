"""Waiting for the Remote Control link of a session that was just started.

Claude Code writes a `bridge_status` record to the session's transcript once Remote Control is
up. vigil reads the transcript itself (`vigil.transcript.find_remote_url`) rather than waiting for
the watcher to index it, and only ever reads it.
"""

import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from vigil.transcript import find_remote_url


def wait_for_remote_url(
    path: Path,
    after: datetime,
    timeout: float,
    *,
    poll: float = 0.5,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> str | None:
    """The Remote Control link written to the transcript `path` after `after` (timezone-aware,
    UTC), waiting up to `timeout` seconds and looking every `poll` seconds. None when none
    appeared in time. A transcript that does not exist (yet) counts as having no link."""
    deadline = clock() + timeout
    while True:
        try:
            url = find_remote_url(path, after)
        except OSError:
            url = None
        if url is not None:
            return url
        remaining = deadline - clock()
        if remaining <= 0:
            return None
        sleep(min(poll, remaining))
