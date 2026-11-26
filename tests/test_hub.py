"""The hub's snapshots (`vigil.hub`): validation, digest and storage."""

import hashlib
import json
import sqlite3
from typing import Any

import pytest

from vigil.hub import (
    MAX_SNAPSHOT_SESSIONS,
    SnapshotError,
    canonical_digest,
    parse_snapshot,
    session_from_snapshot,
)
from vigil.index.queries import list_sessions
from vigil.serialize import session_to_dict

SESSION_ID = "0f1e2d3c-4b5a-4968-8776-655443322110"


def placeholder_session(session_id: str = SESSION_ID, **fields: Any) -> dict[str, Any]:
    """A session as `GET /api/v1/sessions` serves it, with placeholder values."""
    session: dict[str, Any] = {
        "id": session_id,
        "short_id": session_id[:8],
        "name": "placeholder",
        "title": "placeholder",
        "label": None,
        "note": None,
        "pinned": False,
        "archived": False,
        "cwd": "/home/user/src/app",
        "git_branch": "main",
        "version": "2.1.312",
        "first_prompt": "hello",
        "last_user": "hello",
        "last_assistant": "hi",
        "started_at": "2026-11-25T17:00:00.000000+00:00",
        "last_activity_at": "2026-11-25T18:00:00.000000+00:00",
        "message_count": 2,
        "models": ["claude-placeholder"],
        "previous_id": None,
        "chain_id": session_id,
        "machine": "laptop",
        "project_key": "/home/user/src/app",
        "project_label": "app",
        "transcript_path": "/home/user/.claude/projects/app/x.jsonl",
        "status": "idle",
        "remote_url": None,
        "remote_url_at": None,
        "remote_urls": [],
        "open_url": None,
        "busy": False,
    }
    session.update(fields)
    return session


def snapshot(sessions: Any, **fields: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "format": 1,
        "machine": "laptop",
        "generated_at": "2026-11-25T18:04:05Z",
        "vigil_version": "0.9.0",
        "live_checked_at": None,
        "sessions": sessions,
    }
    body.update(fields)
    return body


def encode(body: dict[str, Any]) -> bytes:
    return json.dumps(body).encode()


def test_canonical_digest_is_order_independent() -> None:
    digest = canonical_digest([{"b": 1, "a": "é"}])
    assert digest == canonical_digest([{"a": "é", "b": 1}])
    assert digest == hashlib.sha256('[{"a":"é","b":1}]'.encode()).hexdigest()


def test_parse_valid_snapshot(index_db: sqlite3.Connection) -> None:
    rows = list_sessions(index_db)
    assert rows
    sessions = [{**session_to_dict(row), "machine": "laptop", "extra": 1} for row in rows]
    body = snapshot(sessions)
    parsed = parse_snapshot(encode(body), "laptop")
    assert parsed.machine == "laptop"
    assert parsed.vigil_version == "0.9.0"
    assert parsed.generated_at.isoformat() == "2026-11-25T18:04:05+00:00"
    assert parsed.live_checked_at is None
    assert parsed.digest == canonical_digest(body["sessions"])
    assert len(parsed.sessions) == len(rows)
    for got, sent in zip(parsed.sessions, sessions, strict=True):
        assert "extra" not in got
        assert got == {key: value for key, value in sent.items() if key != "extra"}
    hub_rows = [session_from_snapshot(item, "laptop") for item in parsed.sessions]
    assert [row.session_id for row in hub_rows] == [row.session_id for row in rows]
    for hub_row, row in zip(hub_rows, rows, strict=True):
        assert session_to_dict(hub_row) == {**session_to_dict(row), "machine": "laptop"}


def test_parse_live_checked_at_absent_or_set() -> None:
    body = snapshot([placeholder_session()])
    del body["live_checked_at"]
    assert parse_snapshot(encode(body), "laptop").live_checked_at is None
    body["live_checked_at"] = "2026-11-25T18:00:00Z"
    checked = parse_snapshot(encode(body), "laptop").live_checked_at
    assert checked is not None and checked.isoformat() == "2026-11-25T18:00:00+00:00"


_MANY = [placeholder_session(f"s{number}") for number in range(MAX_SNAPSHOT_SESSIONS + 1)]


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        (snapshot([], format=2), "format"),
        (snapshot([], format=True), "format"),
        (snapshot([], machine="desk"), "machine"),
        (snapshot([], generated_at="yesterday"), "generated_at"),
        (snapshot([], vigil_version=9), "vigil_version"),
        (snapshot([], live_checked_at="soon"), "live_checked_at"),
        (snapshot({}), "sessions"),
        (snapshot(["x"]), "sessions[0]"),
        (
            snapshot([{k: v for k, v in placeholder_session().items() if k != "id"}]),
            "sessions[0].id",
        ),
        (snapshot([placeholder_session(chain_id=None)]), "sessions[0].chain_id"),
        (snapshot([placeholder_session("bad id")]), "sessions[0].id"),
        (snapshot([placeholder_session(), placeholder_session(busy="yes")]), "sessions[1].busy"),
        (snapshot([placeholder_session(busy="yes")]), "sessions[0].busy"),
        (snapshot([placeholder_session(message_count=True)]), "sessions[0].message_count"),
        (snapshot([placeholder_session(message_count=-1)]), "sessions[0].message_count"),
        (snapshot([placeholder_session(open_url="https://evil.example/")]), "sessions[0].open_url"),
        (
            snapshot([placeholder_session(remote_url="https://evil.example/")]),
            "sessions[0].remote_url",
        ),
        (
            snapshot([placeholder_session(remote_urls=["https://evil.example/"])]),
            "sessions[0].remote_urls",
        ),
        (snapshot([placeholder_session(models=[1])]), "sessions[0].models"),
        (snapshot([placeholder_session(status="asleep")]), "sessions[0].status"),
        (snapshot([placeholder_session(title=3)]), "sessions[0].title"),
        (snapshot([placeholder_session(started_at="noon")]), "sessions[0].started_at"),
        (snapshot(_MANY), "at most 500"),
    ],
)
def test_parse_refusals(body: dict[str, Any], problem: str) -> None:
    with pytest.raises(SnapshotError, match=problem.replace("[", r"\[").replace("]", r"\]")):
        parse_snapshot(encode(body), "laptop")


@pytest.mark.parametrize("raw", [b"", b"{", b"[1]", b"\xff", b"[" * 100_000, b'"x"'])
def test_parse_not_a_snapshot(raw: bytes) -> None:
    with pytest.raises(SnapshotError):
        parse_snapshot(raw, "laptop")


def test_long_text_cut_after_digest() -> None:
    sessions = [placeholder_session(last_user="x" * 600)]
    parsed = parse_snapshot(encode(snapshot(sessions)), "laptop")
    assert parsed.sessions[0]["last_user"] == "x" * 500
    assert parsed.digest == canonical_digest(sessions)
    assert parsed.digest != canonical_digest(parsed.sessions)


def test_hub_fields_ignored() -> None:
    session = placeholder_session(
        stale=True,
        resume_url="https://evil.example",
        received_at="2026-11-25T18:00:00Z",
        machine_url="https://evil.example",
        machine="desk",
    )
    parsed = parse_snapshot(encode(snapshot([session])), "laptop").sessions[0]
    for key in ("stale", "resume_url", "received_at", "machine_url"):
        assert key not in parsed
    assert parsed["machine"] == "laptop"


# HubStore (Task 11).
import logging  # noqa: E402
import os  # noqa: E402
import stat  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from pathlib import Path  # noqa: E402

from vigil.config import HubMachine  # noqa: E402
from vigil.hub import HubStore, Snapshot  # noqa: E402

MACHINES = (HubMachine("laptop", "http://laptop.example.com:7787"), HubMachine("desk"))
START = datetime(2026, 11, 25, 18, 0, tzinfo=UTC)


class Clock:
    """A wall clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def make_store(directory: Path, clock: Clock, stale_after: int = 180) -> HubStore:
    return HubStore(directory, MACHINES, stale_after, clock=clock)


def parsed(machine: str = "laptop", *ids: str) -> Snapshot:
    sessions = [placeholder_session(session_id) for session_id in ids or (SESSION_ID,)]
    return parse_snapshot(encode(snapshot(sessions, machine=machine)), machine)


def stored(directory: Path, name: str = "laptop") -> dict[str, Any]:
    data: dict[str, Any] = json.loads((directory / f"{name}.json").read_text())
    return data


def test_replace_and_reload(tmp_path: Path) -> None:
    directory = tmp_path / "data" / "hub"
    clock = Clock()
    store = make_store(directory, clock)
    snap = parsed("laptop", "a1", "b2")
    received = store.replace(snap)
    assert received == START
    [row_a, row_b] = store.sessions()
    assert (row_a.session_id, row_b.session_id) == ("a1", "b2")
    assert row_a.received_at == START and row_a.machine == "laptop"
    assert stat.S_IMODE((directory / "laptop.json").stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    content = stored(directory)
    assert set(content) == {
        "format",
        "machine",
        "digest",
        "received_at",
        "last_seen_at",
        "generated_at",
        "live_checked_at",
        "vigil_version",
        "sessions",
    }
    assert content["digest"] == snap.digest
    assert content["sessions"] == snap.sessions

    clock.advance(30)
    again = make_store(directory, clock)
    again.load()
    assert again.sessions() == store.sessions()
    assert again.heartbeat("laptop", snap.digest)
    [laptop, desk] = again.machines()
    assert laptop.last_snapshot_at == START
    assert laptop.session_count == 2
    assert laptop.url == "http://laptop.example.com:7787"
    assert desk.session_count == 0 and desk.last_snapshot_at is None


def test_failed_write_keeps_old_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "hub"
    store = make_store(directory, Clock())
    first = parsed("laptop", "a1")
    store.replace(first)
    before = (directory / "laptop.json").read_bytes()

    def refuse(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError, match="disk full"):
        store.replace(parsed("laptop", "b2"))
    assert [row.session_id for row in store.sessions()] == ["a1"]
    assert store.heartbeat("laptop", first.digest)
    assert (directory / "laptop.json").read_bytes() == before
    assert sorted(path.name for path in directory.iterdir()) == ["laptop.json"]


def test_leftover_temp_file_ignored(tmp_path: Path) -> None:
    directory = tmp_path / "hub"
    make_store(directory, Clock()).replace(parsed("laptop", "a1"))
    (directory / ".laptop.json.deadbeef").write_bytes(b'{"format": 1, "sessio')
    store = make_store(directory, Clock())
    store.load()
    assert [row.session_id for row in store.sessions()] == ["a1"]


def test_corrupt_file_is_no_snapshot(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    directory = tmp_path / "hub"
    directory.mkdir(mode=0o700)
    (directory / "laptop.json").write_text("{")
    store = make_store(directory, Clock())
    with caplog.at_level(logging.WARNING, logger="vigil.hub"):
        store.load()
    assert "laptop.json" in caplog.text
    assert store.machines()[0].session_count == 0
    assert store.sessions() == []


@pytest.mark.parametrize(
    "change",
    [
        {"machine": "desk"},
        {"format": 2},
        {"digest": 3},
        {"received_at": "never"},
        {"sessions": [{"id": "x"}]},
    ],
)
def test_tampered_file_is_no_snapshot(tmp_path: Path, change: dict[str, Any]) -> None:
    directory = tmp_path / "hub"
    make_store(directory, Clock()).replace(parsed("laptop", "a1"))
    path = directory / "laptop.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), **change}))
    store = make_store(directory, Clock())
    store.load()
    assert store.sessions() == []


def test_unlisted_file_ignored(tmp_path: Path) -> None:
    directory = tmp_path / "hub"
    HubStore(directory, (HubMachine("intruder"),), 180).replace(parsed("intruder", "a1"))
    assert (directory / "intruder.json").exists()
    store = make_store(directory, Clock())
    store.load()
    assert store.sessions() == []
    assert not store.knows("intruder")
    assert store.knows("laptop") and store.knows("desk")


def test_heartbeat_digest(tmp_path: Path) -> None:
    clock = Clock()
    store = make_store(tmp_path / "hub", clock)
    snap = parsed("laptop", "a1")
    assert not store.heartbeat("laptop", snap.digest)
    store.replace(snap)
    clock.advance(20)
    assert store.heartbeat("laptop", snap.digest)
    assert store.machines()[0].last_seen_at == START + timedelta(seconds=20)
    assert store.machines()[0].last_snapshot_at == START
    clock.advance(20)
    assert not store.heartbeat("laptop", "0" * 64)
    assert store.machines()[0].last_seen_at == START + timedelta(seconds=20)
    assert not store.heartbeat("desk", snap.digest)
    assert not store.heartbeat("ghost", snap.digest)


def test_last_seen_flushed_once_a_minute(tmp_path: Path) -> None:
    directory = tmp_path / "hub"
    clock = Clock()
    store = make_store(directory, clock)
    snap = parsed("laptop", "a1")
    store.replace(snap)
    written = stored(directory)["last_seen_at"]
    for seconds in (10, 20):  # +10 s, +30 s
        clock.advance(seconds)
        assert store.heartbeat("laptop", snap.digest)
        assert stored(directory)["last_seen_at"] == written
    clock.advance(31)  # +61 s
    assert store.heartbeat("laptop", snap.digest)
    assert stored(directory)["last_seen_at"] == (START + timedelta(seconds=61)).isoformat(
        timespec="microseconds"
    )
    clock.advance(5)
    store.heartbeat("laptop", snap.digest)
    store.flush()
    assert stored(directory)["last_seen_at"].startswith("2026-11-25T18:01:01")
    store.flush(force=True)
    assert stored(directory)["last_seen_at"].startswith("2026-11-25T18:01:06")
    assert stored(directory)["sessions"] == snap.sessions


def test_stale_after(tmp_path: Path) -> None:
    clock = Clock()
    store = make_store(tmp_path / "hub", clock, stale_after=180)
    store.replace(parsed("laptop", "a1"))
    clock.advance(179)
    laptop, desk = store.machines()
    assert not laptop.stale
    assert desk.stale and desk.last_seen_at is None
    clock.advance(2)
    assert store.machines()[0].stale
