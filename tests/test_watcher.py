"""The watcher keeps the index current. These tests use the real inotify watch; they wait for
events with a deadline, never a fixed sleep."""

import asyncio
import json
import shutil
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import (
    FIXTURE_PROJECT_DIR,
    FIXTURE_TRANSCRIPTS,
    LOOPBACK_URL,
    VigilEnv,
    freeze_tree,
)
from vigil.config import load_config
from vigil.index.db import connect
from vigil.index.scanner import ScanStats
from vigil.web import watcher
from vigil.web.app import create_app

NAMED = "e47d2def-0bb3-4d50-9f9c-8e38e4cdcfba"
LATER = "2026-11-18T09:30:00.000Z"


def appended_line(transcript: Path) -> str:
    """A new user message for `transcript`, at LATER: its first user line with a new uuid."""
    for line in transcript.read_text().splitlines():
        record = json.loads(line)
        if record.get("type") == "user":
            record.update(uuid="00000000-0000-4000-8000-000000000001", timestamp=LATER)
            record["parentUuid"] = None
            record["message"] = {"role": "user", "content": "one more thing"}
            return json.dumps(record) + "\n"
    raise AssertionError(f"no user line in {transcript}")


def wait_for(condition: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def test_watcher_rescans_on_append(vigil_env: VigilEnv) -> None:
    app = create_app(load_config(), conn_factory=lambda: connect(vigil_env.index))
    web = app.state.vigil
    with TestClient(app, base_url=LOOPBACK_URL) as client:
        # Startup: the watcher scans, then watches.
        assert web.watcher_ready.wait(timeout=30), "the watcher never started watching"
        before = client.get(f"/api/v1/sessions/{NAMED}").json()["last_activity_at"]
        assert before is not None and before < "2026-11-18"
        assert web.last_scan_at is not None
        assert client.get("/api/v1/health").json()["indexed_sessions"] == 7

        transcript = vigil_env.claude_home / "projects" / FIXTURE_PROJECT_DIR / f"{NAMED}.jsonl"
        with transcript.open("a") as handle:
            handle.write(appended_line(transcript))
        appended_at = time.monotonic()

        def updated() -> bool:
            data = client.get(f"/api/v1/sessions/{NAMED}").json()
            return bool(data["last_activity_at"] > before)

        assert wait_for(updated, timeout=3.0), "the append was not indexed within 3 s"
        assert time.monotonic() - appended_at < 3.0
        detail = client.get(f"/api/v1/sessions/{NAMED}").json()
        assert detail["last_activity_at"].startswith("2026-11-18T09:30:00")
        assert detail["last_user"] == "one more thing"


def run_watcher(
    conn_factory: Callable[[], sqlite3.Connection],
    claude_home: Path,
    until: Callable[[list[ScanStats]], bool],
    timeout: float = 30.0,
    **options: Any,
) -> list[ScanStats]:
    """Run `watcher.run` until `until(scans so far)` holds after a scan; fail after `timeout`."""
    scans: list[ScanStats] = []

    async def main() -> None:
        done = asyncio.Event()

        def on_scan(stats: ScanStats) -> None:
            scans.append(stats)
            if until(scans):
                done.set()

        task = asyncio.create_task(
            watcher.run(conn_factory, claude_home, on_scan=on_scan, **options)
        )
        try:
            await asyncio.wait_for(done.wait(), timeout)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())
    return scans


def test_watcher_rescans_periodically(claude_home: Path, tmp_path: Path) -> None:
    shutil.copytree(FIXTURE_TRANSCRIPTS / "2.1.312", claude_home / "projects" / FIXTURE_PROJECT_DIR)
    freeze_tree(claude_home)  # the watcher only reads the Claude home
    db = tmp_path / "index.db"
    scans = run_watcher(
        lambda: connect(db), claude_home, lambda s: len(s) >= 4, rescan_every=0.0, tick_ms=50
    )
    assert scans[0].sessions_updated == 6
    assert all(stats.sessions_updated == 0 for stats in scans[1:])


def test_watcher_waits_for_the_projects_folder(claude_home: Path, tmp_path: Path) -> None:
    db = tmp_path / "index.db"

    def appear_after_two_scans(scans: list[ScanStats]) -> bool:
        if len(scans) == 2:
            shutil.copytree(
                FIXTURE_TRANSCRIPTS / "2.1.312", claude_home / "projects" / FIXTURE_PROJECT_DIR
            )
        return any(stats.sessions_updated for stats in scans)

    scans = run_watcher(
        lambda: connect(db), claude_home, appear_after_two_scans, retry_after=0.01, tick_ms=50
    )
    assert scans[0].files_seen == 0
    assert scans[-1].sessions_updated == 6


def test_watcher_survives_a_failing_scan(
    claude_home: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    shutil.copytree(FIXTURE_TRANSCRIPTS / "2.1.312", claude_home / "projects" / FIXTURE_PROJECT_DIR)
    db = tmp_path / "index.db"
    calls = 0

    def flaky() -> sqlite3.Connection:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database is locked")
        return connect(db)

    scans = run_watcher(flaky, claude_home, lambda s: len(s) >= 1, rescan_every=0.0, tick_ms=50)
    assert scans[0].sessions_updated == 6
    assert "failed; will retry" in caplog.text
