"""The push client (`vigil.push`): this machine's snapshot, sent to a hub."""

import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import vigil
from tests.conftest import FakeBin
from vigil.config import Config
from vigil.index.queries import set_meta
from vigil.index.scanner import link_chains
from vigil.push import SNAPSHOT_DAYS, build_snapshot
from vigil.web import LiveStatus

NAMED = "e47d2def-0bb3-4d50-9f9c-8e38e4cdcfba"
NEWEST = "f26f1b2c-d3b2-4389-b0ed-aba7e19e8594"
PLAIN = "7fb815ab-28b5-4123-85d0-64e6fd3b4141"
FORKED = "68e04fa1-b95c-4610-8221-261395d0ca5d"
HANDMADE = "11111111-1111-4111-8111-111111111111"
NEWEST_ACTIVITY = datetime(2026, 11, 17, 15, 8, 21, 286000, tzinfo=UTC)
"""When the newest fixture session was last active."""
JUST_AFTER = NEWEST_ACTIVITY + timedelta(minutes=1)
LAPTOP = Config(machine="laptop")

_HELP = "2.1.312 (Claude Code)\n  --bg  Start in the background\n  attach\n  agents\n"
_AGENTS_CLAUDE = """#!{python}
import json, os, sys
with open({log!r}, "a") as handle:
    handle.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd()}}) + "\\n")
if "agents" in sys.argv[1:]:
    with open({agents!r}) as handle:
        sys.stdout.write(handle.read())
else:
    sys.stdout.write({help!r})
"""


def install_agents_claude(fake_bin: FakeBin, live: list[dict[str, Any]]) -> Path:
    """A fake `claude` whose `agents --json` prints what the returned file holds (`live` at
    first; rewrite the file to change it). It records its argv like every FakeBin program."""
    fake_bin.install("claude")
    agents = fake_bin.directory / ".agents.json"
    agents.write_text(json.dumps(live))
    (fake_bin.directory / "claude").write_text(
        _AGENTS_CLAUDE.format(
            python=sys.executable,
            log=str(fake_bin.directory / ".calls" / "claude.jsonl"),
            agents=str(agents),
            help=_HELP,
        )
    )
    return agents


def ids(snapshot: dict[str, Any]) -> list[str]:
    return [session["id"] for session in snapshot["sessions"]]


def test_snapshot_contents(index_db: sqlite3.Connection, fake_bin: FakeBin) -> None:
    with index_db:
        index_db.execute(
            "UPDATE sessions SET previous_session_id = ? WHERE session_id = ?", (PLAIN, FORKED)
        )
        link_chains(index_db)
    set_meta(index_db, NAMED, archived=True)
    live = LiveStatus()

    snapshot = build_snapshot(index_db, LAPTOP, live, JUST_AFTER)
    assert snapshot["format"] == 1
    assert snapshot["machine"] == "laptop"
    assert snapshot["generated_at"].startswith("2026-11-17T15:09:21")
    assert snapshot["vigil_version"] == vigil.__version__
    assert snapshot["live_checked_at"] is None
    sent = ids(snapshot)
    assert sent[0] == NEWEST  # newest activity first
    assert FORKED in sent and PLAIN not in sent  # one per chain, by its newest session
    assert NAMED not in sent  # archived
    assert HANDMADE in sent and len(sent) == 5
    assert all(session["machine"] == "laptop" for session in snapshot["sessions"])
    assert all(isinstance(session["remote_urls"], list) for session in snapshot["sessions"])
    assert {session["status"] for session in snapshot["sessions"]} == {"unknown"}  # no probe
    assert fake_bin.calls("claude") == []

    # Only the last 30 days: HANDMADE (10:00 that day) falls out when `now` is 30 days after noon.
    noon = datetime(2026, 11, 17, 12, 0, tzinfo=UTC) + timedelta(days=SNAPSHOT_DAYS)
    assert HANDMADE not in ids(build_snapshot(index_db, LAPTOP, live, noon))
    assert len(ids(build_snapshot(index_db, LAPTOP, live, noon))) == 4
    assert build_snapshot(index_db, LAPTOP, live, JUST_AFTER + timedelta(days=31))["sessions"] == []


def test_snapshot_uses_page_probe(index_db: sqlite3.Connection, fake_bin: FakeBin) -> None:
    install_agents_claude(fake_bin, [{"sessionId": NEWEST, "kind": "background", "status": "busy"}])
    wall = datetime(2026, 11, 17, 15, 9, tzinfo=UTC)
    live = LiveStatus(wall=lambda: wall)
    live.get()  # as a page would
    calls = len(fake_bin.calls("claude"))

    snapshot = build_snapshot(index_db, LAPTOP, live, JUST_AFTER)
    assert snapshot["live_checked_at"] == "2026-11-17T15:09:00.000000+00:00"
    by_id = {session["id"]: session for session in snapshot["sessions"]}
    assert by_id[NEWEST]["status"] == "background" and by_id[NEWEST]["busy"] is True
    assert by_id[NAMED]["status"] == "idle"
    assert len(fake_bin.calls("claude")) == calls  # no new probe
