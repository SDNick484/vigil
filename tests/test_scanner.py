import json
import shutil
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import (
    FIXTURE_PROJECT_DIR,
    FIXTURE_TRANSCRIPTS,
    HANDMADE_PROJECT_DIR,
    freeze_tree,
    install_fixture_transcripts,
    thaw_tree,
)
from vigil.index import scanner
from vigil.index.db import connect
from vigil.index.queries import get_session
from vigil.index.scanner import ScanStats, scan
from vigil.transcript import ParseResult, SessionFacts, parse_transcript

PLAIN = "7fb815ab-28b5-4123-85d0-64e6fd3b4141"
NAMED = "e47d2def-0bb3-4d50-9f9c-8e38e4cdcfba"
COMMAND = "c8232e85-1b73-42a7-b6af-192de6b33a26"
MCP_AGENT = "7bf4d32d-3526-48a2-9532-46c8ec375e87"
HANDMADE = "11111111-1111-4111-8111-111111111111"
REMOTE = "22222222-2222-4222-8222-222222222222"
REAL_IDS = sorted(p.stem for p in (FIXTURE_TRANSCRIPTS / "2.1.312").glob("*.jsonl"))


@pytest.fixture
def home(claude_home: Path) -> Path:
    install_fixture_transcripts(claude_home)
    return claude_home


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(tmp_path / "data" / "index.db")
    yield conn
    conn.close()


def transcript(home: Path, session_id: str) -> Path:
    folder = HANDMADE_PROJECT_DIR if session_id == HANDMADE else FIXTURE_PROJECT_DIR
    return home / "projects" / folder / f"{session_id}.jsonl"


Call = tuple[Path, int, SessionFacts | None]


@pytest.fixture
def parse_calls(monkeypatch: pytest.MonkeyPatch) -> list[Call]:
    """Record every call the scanner makes to the parser."""
    calls: list[Call] = []

    def spy(path: Path, start_offset: int = 0, prior: SessionFacts | None = None) -> ParseResult:
        calls.append((path, start_offset, prior))
        return parse_transcript(path, start_offset, prior)

    monkeypatch.setattr(scanner, "parse_transcript", spy)
    return calls


def dump(conn: sqlite3.Connection, session_id: str) -> dict[str, Any]:
    """Everything the index holds about one session, except where its file lives."""
    row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    assert row is not None, session_id
    facts = {k: row[k] for k in row.keys() if k not in ("transcript_path", "machine")}
    models = conn.execute(
        "SELECT model FROM session_models WHERE session_id = ? ORDER BY model", (session_id,)
    ).fetchall()
    features = conn.execute(
        "SELECT kind, name, count FROM session_features WHERE session_id = ? ORDER BY kind, name",
        (session_id,),
    ).fetchall()
    facts["models"] = [tuple(r) for r in models]
    facts["features"] = [tuple(r) for r in features]
    return facts


def offset_of(conn: sqlite3.Connection, path: Path) -> int:
    row = conn.execute("SELECT offset FROM file_state WHERE path = ?", (str(path),)).fetchone()
    assert row is not None
    return int(row["offset"])


def full_scan_dump(tmp_path: Path, path: Path, session_id: str) -> dict[str, Any]:
    """The index rows for `path` scanned in one go, in a fresh home and index."""
    home = tmp_path / "fresh_home"
    target = home / "projects" / "p" / path.name
    target.parent.mkdir(parents=True)
    target.write_bytes(path.read_bytes())
    fresh = connect(tmp_path / "fresh.db")
    try:
        scan(fresh, home)
        return dump(fresh, session_id)
    finally:
        fresh.close()


def test_scan_indexes_all_fixtures(home: Path, conn: sqlite3.Connection) -> None:
    stats = scan(conn, home)
    assert stats == ScanStats(files_seen=7, files_parsed=7, sessions_updated=7, errors=0)
    ids = [r["session_id"] for r in conn.execute("SELECT session_id FROM sessions ORDER BY 1")]
    assert ids == sorted([*REAL_IDS, HANDMADE])

    named = dump(conn, NAMED)
    assert named["title"] == "fixture-named"
    assert named["cwd"] == "/home/user/src/fixture-repo"
    assert named["chain_id"] == NAMED
    assert named["previous_session_id"] is None
    assert named["started_at"] == "2026-11-17T15:07:41.810000+00:00"
    command = dump(conn, COMMAND)
    assert ("slash_command", "/greet", 1) in command["features"]
    assert ("skill", "fixture-skill", 1) in command["features"]
    assert dump(conn, PLAIN)["models"] == [("claude-haiku-5-5",), ("claude-sonnet-5-5",)]
    assert dump(conn, HANDMADE)["cwd"] == "/home/user/src/broken-repo"


def test_subagent_transcript_does_not_replace_parent(home: Path, conn: sqlite3.Connection) -> None:
    subagents = transcript(home, MCP_AGENT).with_suffix("") / "subagents"
    assert any(subagents.glob("agent-*.jsonl"))  # the layout under test is really there
    scan(conn, home)
    row = conn.execute(
        "SELECT transcript_path, message_count FROM sessions WHERE session_id = ?", (MCP_AGENT,)
    ).fetchone()
    assert row["transcript_path"] == str(transcript(home, MCP_AGENT))
    assert row["message_count"] == parse_transcript(transcript(home, MCP_AGENT)).facts.message_count
    assert row["message_count"] > 0
    paths = [r["path"] for r in conn.execute("SELECT path FROM file_state")]
    assert not [p for p in paths if "subagents" in p]


def test_rescan_unchanged_parses_nothing(
    home: Path, conn: sqlite3.Connection, parse_calls: list[Call]
) -> None:
    scan(conn, home)
    parse_calls.clear()
    stats = scan(conn, home)
    assert stats == ScanStats(files_seen=7, files_parsed=0, sessions_updated=0, errors=0)
    assert parse_calls == []


def test_append_reads_only_tail(
    home: Path, conn: sqlite3.Connection, parse_calls: list[Call]
) -> None:
    path = transcript(home, PLAIN)
    scan(conn, home)
    before = dump(conn, PLAIN)
    size = path.stat().st_size
    assert offset_of(conn, path) == size
    record = {
        "type": "user",
        "sessionId": PLAIN,
        "timestamp": "2026-11-17T16:00:00.000Z",
        "message": {"role": "user", "content": "one more prompt"},
    }
    with path.open("a") as handle:
        handle.write(json.dumps(record) + "\n")
    parse_calls.clear()

    stats = scan(conn, home)

    assert stats.files_parsed == 1
    [(called_path, start, prior)] = parse_calls
    assert called_path == path
    assert start == size
    assert prior is not None and prior.message_count == before["message_count"]
    after = dump(conn, PLAIN)
    assert after["message_count"] == before["message_count"] + 1
    assert after["last_user"] == "one more prompt"
    assert after["first_prompt"] == before["first_prompt"]
    assert after["last_activity_at"] == "2026-11-17T16:00:00.000000+00:00"
    assert offset_of(conn, path) == path.stat().st_size


def test_large_growing_file_reads_only_appended_bytes(
    claude_home: Path, conn: sqlite3.Connection, parse_calls: list[Call]
) -> None:
    session_id = "22222222-2222-4222-8222-222222222222"
    path = claude_home / "projects" / "p" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True)

    def record(n: int) -> bytes:
        return (
            json.dumps(
                {
                    "type": "user",
                    "sessionId": session_id,
                    "timestamp": f"2026-11-17T10:{n // 60 % 60:02d}:{n % 60:02d}.000Z",
                    "message": {"role": "user", "content": f"prompt {n} " + "x" * 10_000},
                }
            ).encode()
            + b"\n"
        )

    with path.open("wb") as handle:
        for n in range(3_000):  # about 30 MB
            handle.write(record(n))
    size = path.stat().st_size
    assert size > 30_000_000
    scan(conn, claude_home)
    tail = record(3_000) + record(3_001)
    with path.open("ab") as handle:
        handle.write(tail)
    parse_calls.clear()

    scan(conn, claude_home)

    [(_, start, prior)] = parse_calls
    assert start == size
    assert prior is not None
    assert offset_of(conn, path) - start == len(tail)
    assert dump(conn, session_id)["message_count"] == 3_002


def test_partial_last_line_reread_next_scan(
    home: Path, conn: sqlite3.Connection, parse_calls: list[Call], tmp_path: Path
) -> None:
    path = transcript(home, COMMAND)
    data = path.read_bytes()
    last_line_start = data.rstrip(b"\n").rfind(b"\n") + 1
    cut = last_line_start + (len(data) - last_line_start) // 2
    path.write_bytes(data[:cut])  # Claude Code is mid-write

    scan(conn, home)
    assert offset_of(conn, path) == last_line_start
    partial = dump(conn, COMMAND)
    assert partial["message_count"] == parse_transcript(path).facts.message_count

    with path.open("ab") as handle:
        handle.write(data[cut:])
    parse_calls.clear()
    scan(conn, home)

    [(_, start, _)] = parse_calls
    assert start == last_line_start
    assert offset_of(conn, path) == len(data)
    assert dump(conn, COMMAND) == full_scan_dump(tmp_path, path, COMMAND)


@pytest.mark.parametrize("session_id", REAL_IDS)
def test_incremental_scan_matches_full_scan(
    session_id: str, home: Path, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    path = transcript(home, session_id)
    data = path.read_bytes()
    lines = data.splitlines(keepends=True)
    for end in (len(lines) // 3, 2 * len(lines) // 3, len(lines)):
        path.write_bytes(b"".join(lines[:end]))
        scan(conn, home)
    assert dump(conn, session_id) == full_scan_dump(tmp_path, path, session_id)


def test_truncated_file_reparsed(
    home: Path, conn: sqlite3.Connection, parse_calls: list[Call]
) -> None:
    path = transcript(home, COMMAND)
    scan(conn, home)
    full_count = dump(conn, COMMAND)["message_count"]
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(b"".join(lines[: len(lines) // 2]))
    parse_calls.clear()

    scan(conn, home)

    [(_, start, prior)] = parse_calls
    assert start == 0
    assert prior is None
    count = dump(conn, COMMAND)["message_count"]
    assert count == parse_transcript(path).facts.message_count
    assert count < full_count


def test_rewritten_file_reparsed(
    home: Path, conn: sqlite3.Connection, parse_calls: list[Call]
) -> None:
    """A file replaced by longer content that does not extend the old is parsed from the start."""
    path = transcript(home, NAMED)
    scan(conn, home)
    size = path.stat().st_size
    path.write_bytes(b"x" * size + path.read_bytes())  # no newline where the old content ended
    parse_calls.clear()

    scan(conn, home)

    [(_, start, _)] = parse_calls
    assert start == 0


def test_unreadable_file_counted_and_others_indexed(
    home: Path, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = transcript(home, NAMED)
    real_parse: Callable[..., ParseResult] = parse_transcript

    def parse(path: Path, start_offset: int = 0, prior: SessionFacts | None = None) -> ParseResult:
        if path == broken:
            raise PermissionError(path)
        return real_parse(path, start_offset, prior)

    monkeypatch.setattr(scanner, "parse_transcript", parse)
    stats = scan(conn, home)
    assert stats.errors == 1
    assert stats.sessions_updated == 6
    assert (
        conn.execute("SELECT 1 FROM file_state WHERE path = ?", (str(broken),)).fetchone() is None
    )

    monkeypatch.setattr(scanner, "parse_transcript", real_parse)
    assert scan(conn, home).sessions_updated == 1


def snapshot(root: Path) -> dict[str, tuple[int, int, bytes | None]]:
    """Every path under root with its mode, mtime and (for files) content."""
    result: dict[str, tuple[int, int, bytes | None]] = {}
    for path in sorted([root, *root.rglob("*")]):
        info = path.lstat()
        content = path.read_bytes() if path.is_file() else None
        result[str(path.relative_to(root))] = (info.st_mode, info.st_mtime_ns, content)
    return result


def test_never_writes_claude_home(home: Path, tmp_path: Path) -> None:
    freeze_tree(home)
    before = snapshot(home)
    conn = connect(tmp_path / "data" / "index.db")
    try:
        assert scan(conn, home).sessions_updated == 7
        assert scan(conn, home).files_parsed == 0
    finally:
        conn.close()
    after = snapshot(home)
    thaw_tree(home)
    assert after == before


def test_missing_projects_folder(claude_home: Path, conn: sqlite3.Connection) -> None:
    assert scan(conn, claude_home) == ScanStats()


def test_concurrent_scanners_do_not_count_twice(
    home: Path, conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The service's watcher and a CLI command may scan the same index at once."""
    path = transcript(home, PLAIN)
    scan(conn, home)
    before = dump(conn, PLAIN)["message_count"]
    record = {"type": "user", "sessionId": PLAIN, "message": {"role": "user", "content": "again"}}
    with path.open("a") as handle:
        handle.write(json.dumps(record) + "\n")
    other = connect(tmp_path / "data" / "index.db")
    real_parse = parse_transcript

    def parse_while_other_scans(
        path: Path, start_offset: int = 0, prior: SessionFacts | None = None
    ) -> ParseResult:
        monkeypatch.setattr(scanner, "parse_transcript", real_parse)
        assert scan(other, home).sessions_updated == 1  # the other scanner gets there first
        return real_parse(path, start_offset, prior)

    monkeypatch.setattr(scanner, "parse_transcript", parse_while_other_scans)
    try:
        stats = scan(conn, home)
    finally:
        other.close()
    assert stats.sessions_updated == 0
    assert dump(conn, PLAIN)["message_count"] == before + 1


def test_remote_url_stored_and_listed(home: Path, conn: sqlite3.Connection) -> None:
    shutil.copy(
        FIXTURE_TRANSCRIPTS / "handmade" / "remote-control.jsonl",
        home / "projects" / HANDMADE_PROJECT_DIR / "remote-control.jsonl",
    )
    scan(conn, home)
    row = get_session(conn, REMOTE)
    assert row is not None
    assert row.remote_url == "https://claude.ai/code/session_PLACEHOLDER02"
    assert row.remote_url_at == datetime(2026, 11, 17, 11, 5, tzinfo=UTC)
    assert get_session(conn, PLAIN).remote_url is None  # type: ignore[union-attr]
    count = row.message_count
    # An incremental scan picks up a newer link.
    path = home / "projects" / HANDMADE_PROJECT_DIR / "remote-control.jsonl"
    newer = {
        "type": "system",
        "subtype": "bridge_status",
        "url": "https://claude.ai/code/session_PLACEHOLDER03",
        "timestamp": "2026-11-17T12:00:00.000Z",
        "sessionId": REMOTE,
    }
    with path.open("a") as handle:
        handle.write(json.dumps(newer) + "\n")
    scan(conn, home)
    row = get_session(conn, REMOTE)
    assert row is not None
    assert row.remote_url == "https://claude.ai/code/session_PLACEHOLDER03"
    assert row.remote_url_at == datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
    assert row.message_count == count
