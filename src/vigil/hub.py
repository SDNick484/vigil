"""A hub's sessions (`mode = "hub"`): the snapshots machines push to `PUT /ingest/{name}/sessions`
(`vigil.web.ingest`).

A snapshot is untrusted input. `parse_snapshot` checks it strictly and names the first problem:
a known field with the wrong type, a time that is not ISO 8601 or a session without `id` or
`chain_id` refuses the whole body. Unknown fields are dropped, as are the fields only the hub sets
(`stale`, `received_at`, `resume_url`, `machine_url`); `machine` is the name pushed for; every
string is cut to `TEXT_LIMIT` characters. The digest is taken over the `sessions` array as
received (before any of that), so it matches the one the machine computes for its heartbeat.

`HubStore` keeps each configured machine's latest snapshot in memory and in
`<data dir>/hub/<name>.json` (0600, in a 0700 folder), replaced in one step (a temporary file,
`fsync`, rename). `last_seen_at`, which heartbeats move, is written at most once a minute.
"""

import json
import logging
import os
import re
import secrets
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, cast, get_args

from vigil.config import HubMachine
from vigil.index.db import to_db
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

    received_at: datetime | None = None
    """When the hub accepted the snapshot this session came from."""


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


LAST_SEEN_FLUSH = 60.0
"""Seconds between writes of a machine's `last_seen_at` that only heartbeats moved."""

_log = logging.getLogger(__name__)

_DIGEST = re.compile(r"[0-9a-f]{64}")

_MAX_FILE_BYTES = 2 * MAX_SNAPSHOT_BYTES
"""Most a stored file is read of: its sessions were cut, so it is never larger than this."""


@dataclass(frozen=True)
class MachineState:
    """A configured machine as the hub knows it: `stale` when it was never seen or not within
    `hub_mode.stale_after` seconds."""

    name: str
    url: str | None
    last_snapshot_at: datetime | None
    last_seen_at: datetime | None
    live_checked_at: datetime | None
    stale: bool
    session_count: int


@dataclass
class _Stored:
    """A machine's latest snapshot, as held in memory."""

    snapshot: Snapshot
    rows: list[HubSession]
    received_at: datetime
    last_seen_at: datetime
    written_last_seen: datetime
    """`last_seen_at` as the file holds it."""
    written_at: datetime
    """When the file was last written (or read)."""


def _content(stored: _Stored) -> dict[str, Any]:
    snapshot = stored.snapshot
    return {
        "format": SNAPSHOT_FORMAT,
        "machine": snapshot.machine,
        "digest": snapshot.digest,
        "received_at": to_db(stored.received_at),
        "last_seen_at": to_db(stored.last_seen_at),
        "generated_at": to_db(snapshot.generated_at),
        "live_checked_at": to_db(snapshot.live_checked_at),
        "vigil_version": snapshot.vigil_version,
        "sessions": snapshot.sessions,
    }


def _rows(snapshot: Snapshot, received_at: datetime) -> list[HubSession]:
    return [
        replace(session_from_snapshot(item, snapshot.machine), received_at=received_at)
        for item in snapshot.sessions
    ]


def _from_file(data: Any, name: str) -> tuple[Snapshot, datetime, datetime]:
    """A stored file's snapshot, `received_at` and `last_seen_at`, checked as strictly as a
    push. Raises SnapshotError."""
    if not isinstance(data, dict):
        raise SnapshotError("not a JSON object")
    content = cast(dict[str, Any], data)
    if type(content.get("format")) is not int or content["format"] != SNAPSHOT_FORMAT:
        raise SnapshotError(f"format: must be {SNAPSHOT_FORMAT}")
    if content.get("machine") != name:
        raise SnapshotError(f"machine: must be {name!r}")
    digest = content.get("digest")
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise SnapshotError("digest: must be 64 lowercase hex digits")
    received_at = _time(content.get("received_at"), "received_at")
    seen = content.get("last_seen_at")
    last_seen_at = received_at if seen is None else _time(seen, "last_seen_at")
    generated_at = _time(content.get("generated_at"), "generated_at")
    checked = content.get("live_checked_at")
    live_checked_at = None if checked is None else _time(checked, "live_checked_at")
    vigil_version = content.get("vigil_version")
    if not isinstance(vigil_version, str):
        raise SnapshotError("vigil_version: must be a string")
    sessions = content.get("sessions")
    if not isinstance(sessions, list) or len(sessions) > MAX_SNAPSHOT_SESSIONS:
        raise SnapshotError(f"sessions: must be a list of at most {MAX_SNAPSHOT_SESSIONS}")
    items: list[Any] = sessions
    snapshot = Snapshot(
        machine=name,
        generated_at=generated_at,
        vigil_version=_cut(vigil_version),
        live_checked_at=live_checked_at,
        sessions=[check_session(item, f"sessions[{n}]", name) for n, item in enumerate(items)],
        digest=digest,
    )
    return snapshot, received_at, last_seen_at


class HubStore:
    """Each configured machine's latest snapshot (`[[machines]]`), in memory and in `directory`.
    Safe to use from several threads."""

    def __init__(
        self,
        directory: Path,
        machines: Sequence[HubMachine],
        stale_after: int,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.directory = directory
        self._machines = tuple(machines)
        self._stale_after = timedelta(seconds=stale_after)
        self._clock = clock
        self._lock = threading.Lock()
        """Guards `_stored`."""
        self._writing = {machine.name: threading.Lock() for machine in self._machines}
        """Held while a machine's file is written, and while what to write is decided."""
        self._stored: dict[str, _Stored] = {}

    def load(self) -> None:
        """Read the stored snapshot of every configured machine (`<name>.json`; nothing else in
        the folder is read). A file that cannot be read or checked is logged and left out: that
        machine has no snapshot until it pushes again."""
        for machine in self._machines:
            path = self.directory / f"{machine.name}.json"
            try:
                with path.open("rb") as handle:
                    raw = handle.read(_MAX_FILE_BYTES + 1)
                if len(raw) > _MAX_FILE_BYTES:
                    raise SnapshotError("file too large")
                snapshot, received_at, last_seen_at = _from_file(json.loads(raw), machine.name)
            except FileNotFoundError:
                continue
            except (OSError, RecursionError, ValueError) as error:
                _log.warning("hub: ignoring %s, which cannot be used: %s", path, error)
                continue
            stored = _Stored(
                snapshot=snapshot,
                rows=_rows(snapshot, received_at),
                received_at=received_at,
                last_seen_at=last_seen_at,
                written_last_seen=last_seen_at,
                written_at=self._clock(),
            )
            with self._lock:
                self._stored[machine.name] = stored

    def knows(self, name: str) -> bool:
        """Whether `name` is one of the configured machines."""
        return name in self._writing

    def replace(self, snapshot: Snapshot) -> datetime:
        """Make `snapshot` its machine's stored one and return when it was received. The file is
        replaced in one step; memory changes only once it has been. Raises OSError (file and
        memory as they were) or KeyError for a machine that is not configured."""
        name = snapshot.machine
        with self._writing[name]:
            now = self._clock()
            stored = _Stored(
                snapshot=snapshot,
                rows=_rows(snapshot, now),
                received_at=now,
                last_seen_at=now,
                written_last_seen=now,
                written_at=now,
            )
            self._write(name, _content(stored))
            with self._lock:
                self._stored[name] = stored
        return now

    def heartbeat(self, name: str, digest: str) -> bool:
        """True when `name`'s stored snapshot has `digest`: the machine is seen now (written to
        its file at most once every `LAST_SEEN_FLUSH` seconds). False when there is no such
        snapshot, so the machine should push it."""
        with self._lock:
            stored = self._stored.get(name)
            if stored is None or stored.snapshot.digest != digest:
                return False
            stored.last_seen_at = self._clock()
        self._flush(name, force=False)
        return True

    def flush(self, *, force: bool = False) -> None:
        """Write each machine's `last_seen_at` that changed since its file was written, when that
        was at least `LAST_SEEN_FLUSH` seconds ago or with `force`. A failed write is logged."""
        for machine in self._machines:
            self._flush(machine.name, force=force)

    def _flush(self, name: str, *, force: bool) -> None:
        with self._writing[name]:
            with self._lock:
                stored = self._stored.get(name)
                if stored is None or stored.last_seen_at == stored.written_last_seen:
                    return
                now = self._clock()
                due = (now - stored.written_at).total_seconds() >= LAST_SEEN_FLUSH
                if not (force or due):
                    return
                content = _content(stored)
                seen = stored.last_seen_at
            try:
                self._write(name, content)
            except OSError as error:
                _log.warning("hub: could not write when %s was last seen: %s", name, error)
                return
            with self._lock:
                stored.written_last_seen = seen
                stored.written_at = now

    def _write(self, name: str, content: dict[str, Any]) -> None:
        """Replace `<name>.json` with `content`: a temporary file (0600) in the folder (0700),
        `fsync`, rename. Raises OSError, leaving no temporary file behind."""
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        path = self.directory / f"{name}.json"
        temporary = self.directory / f".{name}.json.{secrets.token_hex(8)}"
        data = json.dumps(content, ensure_ascii=False).encode()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            fd = os.open(temporary, flags, 0o600)
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view) :]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        directory = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def machines(self) -> list[MachineState]:
        """Every configured machine, in the config's order."""
        now = self._clock()
        with self._lock:
            stored = dict(self._stored)
        states: list[MachineState] = []
        for machine in self._machines:
            item = stored.get(machine.name)
            seen = item.last_seen_at if item is not None else None
            states.append(
                MachineState(
                    name=machine.name,
                    url=machine.url,
                    last_snapshot_at=item.received_at if item is not None else None,
                    last_seen_at=seen,
                    live_checked_at=item.snapshot.live_checked_at if item is not None else None,
                    stale=seen is None or now - seen > self._stale_after,
                    session_count=len(item.rows) if item is not None else 0,
                )
            )
        return states

    def sessions(self) -> list[HubSession]:
        """Every stored session, machine by machine in the config's order, as pushed (with
        `received_at`)."""
        with self._lock:
            stored = dict(self._stored)
        return [
            row
            for machine in self._machines
            if (item := stored.get(machine.name)) is not None
            for row in item.rows
        ]
