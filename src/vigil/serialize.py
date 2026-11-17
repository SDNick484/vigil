"""Sessions as plain dicts: the shape `vigil ... --json` prints and the JSON API serves.

`session_to_dict` is the single definition of the field names (the API's `SessionOut` mirrors it),
so consumers can rely on them. Fields are only ever added, never renamed or removed.
"""

from collections.abc import Iterable
from dataclasses import replace

from vigil.claude_cli import Capabilities, LiveSession
from vigil.index.db import to_db
from vigil.index.queries import SessionRow, Status
from vigil.resume import short_id


def display_name(row: SessionRow) -> str | None:
    """What to call a session: the label vigil holds, else Claude Code's title, else None."""
    return row.label or row.title or None


def session_to_dict(row: SessionRow) -> dict[str, object]:
    """Every field of `row` as JSON-ready values.

    - `id` is the session id (the index's `session_id`); `short_id` is its first 8 characters.
    - `previous_id` is the session this one continued, if known; `chain_id` groups a resume chain.
    - `name` is `label` if set, else `title`, else null (`first_prompt` is a separate field).
    - `started_at` and `last_activity_at` are ISO 8601 strings in UTC (or null).
    - `remote_url` is the session's Claude Remote Control link (or null) and `remote_url_at` when
      it was written, an ISO 8601 string in UTC.
    - `open_url` is the link to join the session by right now (`open_link`: it is live and the
      link is from its current run), else null; null too where the live sessions are not known.
    - `models` is a sorted list; `status` is `running`, `background`, `idle` or `unknown`.
    """
    return {
        "id": row.session_id,
        "short_id": short_id(row.session_id),
        "name": display_name(row),
        "title": row.title,
        "label": row.label,
        "note": row.note,
        "pinned": row.pinned,
        "archived": row.archived,
        "cwd": row.cwd,
        "git_branch": row.git_branch,
        "version": row.version,
        "first_prompt": row.first_prompt,
        "last_user": row.last_user,
        "last_assistant": row.last_assistant,
        "started_at": to_db(row.started_at),
        "last_activity_at": to_db(row.last_activity_at),
        "message_count": row.message_count,
        "models": list(row.models),
        "previous_id": row.previous_session_id,
        "chain_id": row.chain_id,
        "machine": row.machine,
        "project_key": row.project_key,
        "project_label": row.project_label,
        "transcript_path": row.transcript_path,
        "status": row.status,
        "remote_url": row.remote_url,
        "remote_url_at": to_db(row.remote_url_at),
        "open_url": row.open_url,
    }


def apply_status(
    rows: Iterable[SessionRow], caps: Capabilities, live: dict[str, LiveSession]
) -> list[SessionRow]:
    """`rows` with `status` set from the live sessions: `running` (open in a terminal),
    `background`, else `idle`, and `open_url` from `open_link`. When the installed claude cannot
    list live sessions every status stays `unknown`."""
    if not caps.has_agents_json:
        return list(rows)
    result: list[SessionRow] = []
    for row in rows:
        running = live.get(row.session_id)
        status: Status = (
            "idle"
            if running is None
            else "running"
            if running.kind == "interactive"
            else "background"
        )
        result.append(replace(row, status=status, open_url=open_link(row, live)))
    return result


def open_link(row: SessionRow, live: dict[str, LiveSession]) -> str | None:
    """The Remote Control link to send a phone to: `row.remote_url` when the session is live and
    the link was written at or after the live run started (an older link belongs to an earlier
    run, which is over), else None. Without a start time any link counts."""
    running = live.get(row.session_id)
    if running is None or row.remote_url is None:
        return None
    if running.started_at is not None and (
        row.remote_url_at is None or row.remote_url_at < running.started_at
    ):
        return None
    return row.remote_url
