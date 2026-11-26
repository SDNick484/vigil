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
