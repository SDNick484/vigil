"""Incremental scanner: Claude Code transcripts into the index.

Only `<claude_home>/projects/<project dir>/<session id>.jsonl` files are sessions. Subagent
transcripts (`<project dir>/<session id>/subagents/agent-*.jsonl`) carry their parent's session id
and are not indexed, so they can never overwrite the parent's row.

Per file, `file_state` holds the size and mtime seen and `offset`, the byte offset just past the
last complete line parsed. On each scan a file is:

- skipped when its `(size, mtime_ns)` is unchanged;
- parsed from `offset` on, merged into the stored facts, when it grew and the byte before
  `offset` is still a newline (only the appended bytes are read);
- parsed again from 0 otherwise (it shrank, was rewritten, or has no stored facts).

A final line without a newline (Claude Code mid-write) is left unread by the parser, so `offset`
stays at its start and the next scan reads it whole.

Two scanners may run at once (service and CLI): each parses without the write lock, then writes
only if the file's stored state is still what it started from.

The scanner only reads under the Claude home; everything it writes goes to the index.
"""

import logging
import socket
import sqlite3
import stat
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from vigil.index.db import from_db, to_db
from vigil.projects import project_for
from vigil.transcript import FeatureKind, SessionFacts, parse_transcript

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScanStats:
    files_seen: int = 0
    files_parsed: int = 0
    sessions_updated: int = 0
    errors: int = 0


@dataclass(frozen=True)
class _FileState:
    size: int
    mtime_ns: int
    offset: int


def transcript_files(claude_home: Path) -> list[Path]:
    """The session transcripts under `claude_home`: `projects/*/*.jsonl`, sorted."""
    return sorted((claude_home / "projects").glob("*/*.jsonl"))


def scan(conn: sqlite3.Connection, claude_home: Path) -> ScanStats:
    """Bring the index up to date with the transcripts under `claude_home`.

    Each file is committed on its own; a file that cannot be read is logged, counted in `errors`
    and left as it was in the index. Sessions whose files have disappeared stay in the index.
    """
    machine = socket.gethostname()
    seen = parsed = updated = errors = 0
    for path in transcript_files(claude_home):
        try:
            info = path.stat()
        except OSError as error:
            log.warning("cannot stat %s: %s", path, error)
            errors += 1
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        seen += 1
        try:
            with conn:
                outcome = _scan_file(conn, path, info.st_size, info.st_mtime_ns, machine)
        except OSError as error:
            log.warning("cannot read %s: %s", path, error)
            errors += 1
            continue
        parsed += outcome != "skipped"
        updated += outcome == "updated"
    with conn:
        link_chains(conn)
    return ScanStats(files_seen=seen, files_parsed=parsed, sessions_updated=updated, errors=errors)


def _scan_file(
    conn: sqlite3.Connection, path: Path, size: int, mtime_ns: int, machine: str
) -> Literal["skipped", "parsed", "updated"]:
    """Index one file if it changed: `skipped` (unchanged), `parsed` (no complete line yet) or
    `updated` (its session row was written)."""
    key = str(path)
    state = _file_state(conn, key)
    if state is not None and (state.size, state.mtime_ns) == (size, mtime_ns):
        return "skipped"

    old_id = _session_for_path(conn, key)
    prior = _load_facts(conn, old_id) if old_id is not None else None
    start = 0
    if (
        state is not None
        and prior is not None
        and size > state.size
        and 0 < state.offset <= size
        and _ends_line(path, state.offset)
    ):
        start = state.offset
    else:
        prior = None

    result = parse_transcript(path, start_offset=start, prior=prior)
    facts = result.facts
    # Parse without holding the write lock, then take it and check that no other scanner (the
    # service's watcher and a CLI command can run at once) stored this file meanwhile; if one did,
    # its result stands and writing ours from a stale offset could count lines twice.
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    if _file_state(conn, key) != state:
        return "skipped"
    if result.end_offset > 0:
        if old_id is not None and old_id != facts.session_id:
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (old_id,))
        _store_facts(conn, facts, key, machine)
    conn.execute(
        "INSERT INTO file_state (path, size, mtime_ns, offset) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (path) DO UPDATE SET "
        "size = excluded.size, mtime_ns = excluded.mtime_ns, offset = excluded.offset",
        (key, size, mtime_ns, result.end_offset),
    )
    return "updated" if result.end_offset > 0 else "parsed"


def _ends_line(path: Path, offset: int) -> bool:
    """Whether the byte just before `offset` is a newline: the file was appended to, not
    rewritten, since `offset` was stored."""
    with path.open("rb") as handle:
        handle.seek(offset - 1)
        return handle.read(1) == b"\n"


def _file_state(conn: sqlite3.Connection, key: str) -> _FileState | None:
    row = conn.execute(
        "SELECT size, mtime_ns, offset FROM file_state WHERE path = ?", (key,)
    ).fetchone()
    return _FileState(row["size"], row["mtime_ns"], row["offset"]) if row else None


def _session_for_path(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT session_id FROM sessions WHERE transcript_path = ?", (key,)
    ).fetchone()
    return cast(str, row["session_id"]) if row else None


def _load_facts(conn: sqlite3.Connection, session_id: str) -> SessionFacts | None:
    """The stored facts of a session, exactly as the parser left them."""
    row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    if row is None:
        return None
    models = {
        r["model"]
        for r in conn.execute(
            "SELECT model FROM session_models WHERE session_id = ?", (session_id,)
        )
    }
    features: Counter[tuple[FeatureKind, str]] = Counter()
    for r in conn.execute(
        "SELECT kind, name, count FROM session_features WHERE session_id = ?", (session_id,)
    ):
        features[cast(FeatureKind, r["kind"]), r["name"]] = r["count"]
    return SessionFacts(
        session_id=row["session_id"],
        cwd=row["cwd"],
        git_branch=row["git_branch"],
        version=row["version"],
        title=row["title"],
        first_prompt=row["first_prompt"],
        last_user=row["last_user"],
        last_assistant=row["last_assistant"],
        started_at=from_db(row["started_at"]),
        last_activity_at=from_db(row["last_activity_at"]),
        message_count=row["message_count"],
        models=models,
        features=features,
        previous_session_id=row["previous_session_id"],
    )


def _store_facts(conn: sqlite3.Connection, facts: SessionFacts, key: str, machine: str) -> None:
    sid = facts.session_id
    existing = conn.execute(
        "SELECT cwd, project_key, project_label, chain_id FROM sessions WHERE session_id = ?",
        (sid,),
    ).fetchone()
    # Keep a session's project once known, so it stays grouped if its folder later disappears.
    if existing is not None and existing["cwd"] == facts.cwd:
        project_key, project_label = existing["project_key"], existing["project_label"]
    else:
        project_key, project_label = project_for(facts.cwd)
    chain_id = existing["chain_id"] if existing is not None else sid
    conn.execute(
        """
        INSERT INTO sessions (
            session_id, cwd, git_branch, version, title, first_prompt, last_user,
            last_assistant, started_at, last_activity_at, message_count, previous_session_id,
            machine, project_key, project_label, chain_id, transcript_path
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (session_id) DO UPDATE SET
            cwd = excluded.cwd, git_branch = excluded.git_branch, version = excluded.version,
            title = excluded.title, first_prompt = excluded.first_prompt,
            last_user = excluded.last_user, last_assistant = excluded.last_assistant,
            started_at = excluded.started_at, last_activity_at = excluded.last_activity_at,
            message_count = excluded.message_count,
            previous_session_id = excluded.previous_session_id, machine = excluded.machine,
            project_key = excluded.project_key, project_label = excluded.project_label,
            chain_id = excluded.chain_id, transcript_path = excluded.transcript_path
        """,
        (
            sid,
            facts.cwd,
            facts.git_branch,
            facts.version,
            facts.title,
            facts.first_prompt,
            facts.last_user,
            facts.last_assistant,
            to_db(facts.started_at),
            to_db(facts.last_activity_at),
            facts.message_count,
            facts.previous_session_id,
            machine,
            project_key,
            project_label,
            chain_id,
            key,
        ),
    )
    conn.execute("DELETE FROM session_models WHERE session_id = ?", (sid,))
    conn.executemany(
        "INSERT INTO session_models (session_id, model) VALUES (?, ?)",
        [(sid, model) for model in sorted(facts.models)],
    )
    conn.execute("DELETE FROM session_features WHERE session_id = ?", (sid,))
    conn.executemany(
        "INSERT INTO session_features (session_id, kind, name, count) VALUES (?, ?, ?, ?)",
        [(sid, kind, name, count) for (kind, name), count in sorted(facts.features.items())],
    )


def link_chains(conn: sqlite3.Connection) -> int:
    """Set every session's `chain_id` to the root of its `previous_session_id` links: the
    earliest session reachable whose previous session is unknown or not indexed. A session with
    no link is its own chain. Links are never guessed (for example from timing). Returns the
    number of rows changed."""
    rows = conn.execute("SELECT session_id, previous_session_id, chain_id FROM sessions").fetchall()
    previous = {r["session_id"]: r["previous_session_id"] for r in rows}
    roots: dict[str, str] = {}

    def root_of(sid: str) -> str:
        path: list[str] = []
        seen: set[str] = set()
        current = sid
        while current not in roots:
            seen.add(current)
            path.append(current)
            parent = previous.get(current)
            if parent is None or parent not in previous or parent in seen:
                roots[current] = current
                break
            current = parent
        root = roots[current]
        for node in path:
            roots[node] = root
        return root

    changes = [
        (root_of(r["session_id"]), r["session_id"])
        for r in rows
        if root_of(r["session_id"]) != r["chain_id"]
    ]
    conn.executemany("UPDATE sessions SET chain_id = ? WHERE session_id = ?", changes)
    return len(changes)
