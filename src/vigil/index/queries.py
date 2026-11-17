"""Reading the index, and writing vigil's own data (session metadata, launches) to it."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from vigil.index.db import from_db, to_db

Status = Literal["running", "background", "idle", "unknown"]


class _Unset(Enum):
    UNSET = "unset"


UNSET = _Unset.UNSET
"""Default for `set_meta` fields: leave the field as it is."""


@dataclass(frozen=True)
class SessionRow:
    session_id: str
    cwd: str | None
    git_branch: str | None
    version: str | None
    title: str | None
    first_prompt: str | None
    last_user: str | None
    last_assistant: str | None
    started_at: datetime | None
    last_activity_at: datetime | None
    message_count: int
    previous_session_id: str | None
    machine: str
    project_key: str
    project_label: str
    chain_id: str
    transcript_path: str
    label: str | None = None
    pinned: bool = False
    archived: bool = False
    note: str | None = None
    models: tuple[str, ...] = ()
    status: Status = "unknown"
    remote_url: str | None = None
    remote_url_at: datetime | None = None


@dataclass(frozen=True)
class ProjectRow:
    project_key: str
    label: str
    session_count: int
    last_activity_at: datetime | None


_SEP = "\x1f"

# Every session with its vigil metadata and models, as one row each.
_VIEW = f"""
    SELECT s.*, m.label AS label, COALESCE(m.pinned, 0) AS pinned,
        COALESCE(m.archived, 0) AS archived, m.note AS note,
        (SELECT group_concat(sm.model, '{_SEP}') FROM session_models sm
            WHERE sm.session_id = s.session_id) AS models
    FROM sessions s LEFT JOIN session_meta m ON m.session_id = s.session_id
"""

_NEWEST_FIRST = "last_activity_at IS NULL, last_activity_at DESC, session_id"

_SEARCHED = ("title", "label", "note", "first_prompt", "cwd")


def _row(row: sqlite3.Row) -> SessionRow:
    models = row["models"]
    return SessionRow(
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
        previous_session_id=row["previous_session_id"],
        machine=row["machine"],
        project_key=row["project_key"],
        project_label=row["project_label"],
        chain_id=row["chain_id"],
        transcript_path=row["transcript_path"],
        label=row["label"],
        pinned=bool(row["pinned"]),
        archived=bool(row["archived"]),
        note=row["note"],
        models=tuple(sorted(models.split(_SEP))) if models else (),
        remote_url=row["remote_url"],
        remote_url_at=from_db(row["remote_url_at"]),
    )


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_sessions(
    conn: sqlite3.Connection,
    *,
    query: str | None = None,
    project_key: str | None = None,
    since: datetime | None = None,
    include_archived: bool = False,
    collapse_chains: bool = True,
    limit: int = 200,
) -> list[SessionRow]:
    """Sessions matching every filter given, newest activity first.

    `query` is split on whitespace; each word must appear (case-insensitive for ASCII) in the
    title, label, note, first prompt or working directory. `since` keeps sessions active at or
    after that time. Archived sessions are left out unless `include_archived`.

    With `collapse_chains`, the result has one row per resume chain that has a matching session:
    the chain's newest session (newest non-archived one, unless `include_archived`), which is the
    one to resume.
    """
    where: list[str] = []
    params: list[Any] = []
    for word in (query or "").split():
        pattern = f"%{_like_escape(word)}%"
        where.append("(" + " OR ".join(f"{col} LIKE ? ESCAPE '\\'" for col in _SEARCHED) + ")")
        params.extend([pattern] * len(_SEARCHED))
    if project_key is not None:
        where.append("project_key = ?")
        params.append(project_key)
    if since is not None:
        where.append("last_activity_at >= ?")
        params.append(to_db(since))
    if not include_archived:
        where.append("archived = 0")
    condition = " AND ".join(where) or "1"

    if collapse_chains:
        visible = "1" if include_archived else "archived = 0"
        sql = f"""
            WITH v AS ({_VIEW}),
            hit AS (SELECT DISTINCT chain_id FROM v WHERE {condition}),
            ranked AS (
                SELECT v.*, row_number() OVER (
                    PARTITION BY chain_id ORDER BY {_NEWEST_FIRST}
                ) AS chain_rank
                FROM v WHERE chain_id IN (SELECT chain_id FROM hit) AND {visible}
            )
            SELECT * FROM ranked WHERE chain_rank = 1 ORDER BY {_NEWEST_FIRST} LIMIT ?
        """
    else:
        sql = f"""
            WITH v AS ({_VIEW})
            SELECT * FROM v WHERE {condition} ORDER BY {_NEWEST_FIRST} LIMIT ?
        """
    params.append(limit)
    return [_row(row) for row in conn.execute(sql, params)]


def get_session(conn: sqlite3.Connection, session_id: str) -> SessionRow | None:
    """A session by exact id, else by a unique id prefix, else by exact title (the newest
    session with that title). None when nothing matches or a prefix is ambiguous."""
    ident = session_id.strip()
    if not ident:
        return None
    row = conn.execute(f"WITH v AS ({_VIEW}) SELECT * FROM v WHERE session_id = ?", (ident,))
    found = row.fetchone()
    if found is not None:
        return _row(found)
    matches = conn.execute(
        f"WITH v AS ({_VIEW}) SELECT * FROM v WHERE substr(session_id, 1, ?) = ? LIMIT 2",
        (len(ident), ident),
    ).fetchall()
    if len(matches) == 1:
        return _row(matches[0])
    if matches:
        return None
    titled = conn.execute(
        f"WITH v AS ({_VIEW}) SELECT * FROM v WHERE title = ? ORDER BY {_NEWEST_FIRST} LIMIT 1",
        (ident,),
    ).fetchone()
    return _row(titled) if titled is not None else None


def chain(conn: sqlite3.Connection, chain_id: str) -> list[SessionRow]:
    """Every session in a resume chain, oldest first."""
    rows = conn.execute(
        f"WITH v AS ({_VIEW}) SELECT * FROM v WHERE chain_id = ? "
        "ORDER BY started_at IS NULL, started_at, last_activity_at, session_id",
        (chain_id,),
    )
    return [_row(row) for row in rows]


def list_projects(conn: sqlite3.Connection) -> list[ProjectRow]:
    """Every project with its session count and latest activity, newest activity first."""
    rows = conn.execute(
        """
        SELECT project_key, max(project_label) AS label, count(*) AS session_count,
            max(last_activity_at) AS last_activity_at
        FROM sessions GROUP BY project_key
        ORDER BY last_activity_at IS NULL, last_activity_at DESC, project_key
        """
    )
    return [
        ProjectRow(
            project_key=row["project_key"],
            label=row["label"],
            session_count=row["session_count"],
            last_activity_at=from_db(row["last_activity_at"]),
        )
        for row in rows
    ]


def set_meta(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    label: str | None | Literal[_Unset.UNSET] = UNSET,
    pinned: bool | Literal[_Unset.UNSET] = UNSET,
    archived: bool | Literal[_Unset.UNSET] = UNSET,
    note: str | None | Literal[_Unset.UNSET] = UNSET,
) -> None:
    """Change vigil's own metadata for a session; only the fields given change. Stored in the
    index (never in the Claude home) and kept across rescans. Does not check that the session
    is indexed."""
    changes: dict[str, object] = {}
    if label is not UNSET:
        changes["label"] = label
    if pinned is not UNSET:
        changes["pinned"] = int(pinned)
    if archived is not UNSET:
        changes["archived"] = int(archived)
    if note is not UNSET:
        changes["note"] = note
    now = to_db(datetime.now(UTC))
    with conn:
        conn.execute(
            "INSERT INTO session_meta (session_id, updated_at) VALUES (?, ?) "
            "ON CONFLICT (session_id) DO NOTHING",
            (session_id, now),
        )
        assignments = ", ".join(f"{column} = ?" for column in [*changes, "updated_at"])
        conn.execute(
            f"UPDATE session_meta SET {assignments} WHERE session_id = ?",
            [*changes.values(), now, session_id],
        )


def record_launch(
    conn: sqlite3.Connection,
    *,
    source: Literal["vigil", "shell"],
    subcommand: str | None,
    flags: list[str],
    at: datetime,
    session_id: str | None,
) -> None:
    """Record one launch of `claude`: flag names only, never their values."""
    with conn:
        conn.execute(
            "INSERT INTO launches (at, source, subcommand, flags, session_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (to_db(at), source, subcommand, json.dumps(list(flags)), session_id),
        )
