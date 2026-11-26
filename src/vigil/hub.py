"""A hub's sessions (`mode = "hub"`): the snapshots machines push to `PUT /ingest/{name}/sessions`
(`vigil.web.ingest`).

A snapshot is untrusted input. `parse_snapshot` checks it strictly and names the first problem:
a known field with the wrong type, a time that is not ISO 8601 or a session without `id` or
`chain_id` refuses the whole body. Unknown fields are dropped, as are the fields only the hub sets
(`stale`, `received_at`, `resume_url`, `machine_url`); `machine` is the name pushed for; every
string is cut to `TEXT_LIMIT` characters. The digest is taken over the `sessions` array as
received (before any of that), so it matches the one the machine computes for its heartbeat.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, cast, get_args

from vigil.index.queries import SessionRow, Status
from vigil.peers import _SESSION_ID
from vigil.transcript import MAX_REMOTE_URLS, REMOTE_URL

SNAPSHOT_FORMAT = 1
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_SNAPSHOT_SESSIONS = 500
TEXT_LIMIT = 500
HUB_DIR = "hub"
"""Where, in the data dir, a hub keeps one `<name>.json` per machine."""

_TEXT_FIELDS = (
    "name",
    "title",
    "label",
    "note",
    "cwd",
    "git_branch",
    "version",
    "first_prompt",
    "last_user",
    "last_assistant",
    "previous_id",
    "project_key",
    "project_label",
    "transcript_path",
    "short_id",
    "machine",
)
"""Fields that are a string or null."""
_BOOL_FIELDS = ("pinned", "archived", "busy")
_TIME_FIELDS = ("started_at", "last_activity_at", "remote_url_at")
_LINK_FIELDS = ("remote_url", "open_url")
_STATUSES: frozenset[str] = frozenset(get_args(Status))


class SnapshotError(ValueError):
    """A pushed snapshot cannot be used; the message names the first problem, e.g.
    `"sessions[3].busy: must be true or false"`."""


@dataclass(frozen=True)
class Snapshot:
    """A checked snapshot: `sessions` hold only known fields, cut to `TEXT_LIMIT`; `digest` is
    `canonical_digest` of the `sessions` array as it was received."""

    machine: str
    generated_at: datetime
    vigil_version: str
    live_checked_at: datetime | None
    sessions: list[dict[str, Any]]
    digest: str


@dataclass(frozen=True)
class HubSession(SessionRow):
    """A session a machine pushed to this hub; `machine` is the name it pushed as."""


def canonical_digest(sessions: list[Any]) -> str:
    """SHA-256 (hex) of `sessions` as canonical JSON: sorted keys, no whitespace, UTF-8."""
    text = json.dumps(sessions, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(text.encode()).hexdigest()


def _time(value: Any, where: str) -> datetime:
    """An ISO 8601 time (naive taken as UTC), else SnapshotError naming `where`."""
    if isinstance(value, str):
        try:
            at = datetime.fromisoformat(value)
        except ValueError:
            pass
        else:
            return at.replace(tzinfo=UTC) if at.tzinfo is None else at
    raise SnapshotError(f"{where}: must be an ISO 8601 time")


def _cut(value: str) -> str:
    return value[:TEXT_LIMIT]


def _text_or_null(value: Any, where: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise SnapshotError(f"{where}: must be a string or null")
    return _cut(value) if value is not None else None


def _strings(value: Any, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SnapshotError(f"{where}: must be a list of strings")
    return [_cut(item) for item in cast(list[str], value)]


def _link(value: Any, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not REMOTE_URL.fullmatch(value):
        raise SnapshotError(f"{where}: must be null or a Claude Remote Control link")
    return value


def check_session(data: Any, where: str, machine: str) -> dict[str, Any]:
    """One session of a snapshot, checked: only its known fields, `machine` set to `machine`,
    every string cut to `TEXT_LIMIT`. A known field that is absent is left out (it reads as null,
    false, 0 or empty). Raises SnapshotError naming the first problem, `where` first."""
    if not isinstance(data, dict):
        raise SnapshotError(f"{where}: must be an object")
    item = cast(dict[str, Any], data)
    session: dict[str, Any] = {}
    session_id = item.get("id")
    if "id" not in item:
        raise SnapshotError(f"{where}.id: is missing")
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        raise SnapshotError(f"{where}.id: must be a session id (letters, digits, - and _)")
    session["id"] = session_id
    chain_id = item.get("chain_id")
    if "chain_id" not in item:
        raise SnapshotError(f"{where}.chain_id: is missing")
    if not isinstance(chain_id, str) or not chain_id:
        raise SnapshotError(f"{where}.chain_id: must be a non-empty string")
    session["chain_id"] = _cut(chain_id)
    for key in _TEXT_FIELDS:
        if key in item:
            session[key] = _text_or_null(item[key], f"{where}.{key}")
    for key in _BOOL_FIELDS:
        if key in item:
            if not isinstance(item[key], bool):
                raise SnapshotError(f"{where}.{key}: must be true or false")
            session[key] = item[key]
    if "message_count" in item:
        count = item["message_count"]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise SnapshotError(f"{where}.message_count: must be a whole number, 0 or more")
        session["message_count"] = count
    if "models" in item:
        session["models"] = _strings(item["models"], f"{where}.models")
    if "status" in item:
        if item["status"] not in _STATUSES:
            raise SnapshotError(f"{where}.status: must be one of {', '.join(sorted(_STATUSES))}")
        session["status"] = item["status"]
    for key in _TIME_FIELDS:
        if key in item:
            value = item[key]
            if value is not None:
                _time(value, f"{where}.{key}")
            session[key] = value
    for key in _LINK_FIELDS:
        if key in item:
            session[key] = _link(item[key], f"{where}.{key}")
    if "remote_urls" in item:
        urls = _strings(item["remote_urls"], f"{where}.remote_urls")
        for number, url in enumerate(urls):
            _link(url, f"{where}.remote_urls[{number}]")
        session["remote_urls"] = urls[-MAX_REMOTE_URLS:]
    session["machine"] = machine
    return session


def parse_snapshot(body: bytes, name: str) -> Snapshot:
    """The snapshot `name` pushed as `body` (JSON), checked as this module's docstring says.
    Raises SnapshotError naming the first problem."""
    try:
        data = json.loads(body)
    except (RecursionError, ValueError) as error:
        raise SnapshotError(f"not JSON: {error}"[:TEXT_LIMIT]) from None
    if not isinstance(data, dict):
        raise SnapshotError("the body must be a JSON object")
    snapshot = cast(dict[str, Any], data)
    snapshot_format = snapshot.get("format")
    if type(snapshot_format) is not int or snapshot_format != SNAPSHOT_FORMAT:
        raise SnapshotError(f"format: must be {SNAPSHOT_FORMAT}")
    if snapshot.get("machine") != name:
        raise SnapshotError(f"machine: must be {name!r}, the machine pushed for")
    generated_at = _time(snapshot.get("generated_at"), "generated_at")
    vigil_version = snapshot.get("vigil_version")
    if not isinstance(vigil_version, str):
        raise SnapshotError("vigil_version: must be a string")
    checked = snapshot.get("live_checked_at")
    live_checked_at = None if checked is None else _time(checked, "live_checked_at")
    sessions = snapshot.get("sessions")
    if not isinstance(sessions, list):
        raise SnapshotError("sessions: must be a list")
    items: list[Any] = sessions
    if len(items) > MAX_SNAPSHOT_SESSIONS:
        raise SnapshotError(f"sessions: at most {MAX_SNAPSHOT_SESSIONS} sessions")
    checked_sessions = [
        check_session(item, f"sessions[{number}]", name) for number, item in enumerate(items)
    ]
    return Snapshot(
        machine=name,
        generated_at=generated_at,
        vigil_version=_cut(vigil_version),
        live_checked_at=live_checked_at,
        sessions=checked_sessions,
        digest=canonical_digest(items),
    )


def _optional_time(data: dict[str, Any], key: str) -> datetime | None:
    value = data.get(key)
    return None if value is None else _time(value, key)


def session_from_snapshot(data: dict[str, Any], machine: str) -> HubSession:
    """A session as `check_session` left it, as a row; `machine` is the name it was pushed as."""
    session_id = cast(str, data["id"])
    project_key = data.get("project_key") or ""
    return HubSession(
        session_id=session_id,
        cwd=data.get("cwd"),
        git_branch=data.get("git_branch"),
        version=data.get("version"),
        title=data.get("title"),
        first_prompt=data.get("first_prompt"),
        last_user=data.get("last_user"),
        last_assistant=data.get("last_assistant"),
        started_at=_optional_time(data, "started_at"),
        last_activity_at=_optional_time(data, "last_activity_at"),
        message_count=data.get("message_count", 0),
        previous_session_id=data.get("previous_id"),
        machine=machine,
        project_key=project_key,
        project_label=data.get("project_label") or project_key,
        chain_id=data["chain_id"],
        transcript_path=data.get("transcript_path") or "",
        label=data.get("label"),
        pinned=data.get("pinned", False),
        archived=data.get("archived", False),
        note=data.get("note"),
        models=tuple(sorted(data.get("models", ()))),
        status=data.get("status", "unknown"),
        remote_url=data.get("remote_url"),
        remote_url_at=_optional_time(data, "remote_url_at"),
        remote_urls=tuple(data.get("remote_urls", ())),
        open_url=data.get("open_url"),
        busy=data.get("busy", False),
    )
