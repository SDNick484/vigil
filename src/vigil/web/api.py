"""The JSON API (`router`, read-only, under `/api/v1`) and the state-changing actions (`actions`:
resume and metadata edits, POST only, outside `/api`).

Sessions are served exactly as `vigil.serialize.session_to_dict` makes them (`SessionOut` mirrors
it), so `vigil ls --json` and `vigil show --json` print the same shapes. Every session served has
its status from the live sessions, cached for a few seconds (`vigil.web.LiveStatus`).
"""

import os
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import vigil
from vigil.commands import resume_command
from vigil.index.db import to_db
from vigil.index.queries import (
    UNSET,
    SessionRow,
    Status,
    _Unset,
    chain,
    get_session,
    list_projects,
    list_sessions,
    set_meta,
)
from vigil.resume import ResumeRefused, execute, plan_resume
from vigil.serialize import apply_status, session_to_dict
from vigil.web import connection, state

router = APIRouter(prefix="/api/v1", tags=["api"])
actions = APIRouter(tags=["actions"])

_ALL_ROWS = -1
"""SQLite reads a negative LIMIT as "no limit"; used when rows are filtered after the query."""


class SessionOut(BaseModel):
    """One session, as `vigil.serialize.session_to_dict` makes it."""

    id: str
    short_id: str
    name: str | None
    title: str | None
    label: str | None
    note: str | None
    pinned: bool
    archived: bool
    cwd: str | None
    git_branch: str | None
    version: str | None
    first_prompt: str | None
    last_user: str | None
    last_assistant: str | None
    started_at: str | None
    last_activity_at: str | None
    message_count: int
    models: list[str]
    previous_id: str | None
    chain_id: str
    machine: str
    project_key: str
    project_label: str
    transcript_path: str
    status: Status


class SessionDetailOut(SessionOut):
    """One session with its resume chain (oldest first) and the command that resumes it by hand,
    as `vigil show --json` prints it."""

    chain: list[SessionOut]
    resume_command: str


class ProjectOut(BaseModel):
    project_key: str
    label: str
    session_count: int
    last_activity_at: str | None


class HealthOut(BaseModel):
    status: Literal["ok"]
    version: str
    claude_version: str | None
    indexed_sessions: int
    last_scan_at: str | None


class ResumeOut(BaseModel):
    ok: Literal[True]
    attach: str
    opened_terminal: bool


class RefusalOut(BaseModel):
    ok: Literal[False]
    reason: str


def _with_status(request: Request, rows: list[SessionRow]) -> list[SessionRow]:
    caps, live = state(request).live.get()
    return apply_status(rows, caps, live)


def _no_session(ident: str) -> str:
    return f"no session matches {ident!r} (unknown, or an ambiguous id prefix)"


@router.get("/sessions", response_model=list[SessionOut])
def sessions(
    request: Request,
    q: Annotated[str | None, Query(description="words that must all appear")] = None,
    project: Annotated[str | None, Query(description="a project key")] = None,
    cwd: Annotated[str | None, Query(description="this working folder exactly")] = None,
    since: Annotated[datetime | None, Query(description="active at or after (ISO 8601)")] = None,
    status: Annotated[Status | None, Query()] = None,
    all_: Annotated[
        bool, Query(alias="all", description="include archived sessions and whole chains")
    ] = False,
    limit: Annotated[int, Query(ge=1, le=10_000)] = 200,
) -> list[dict[str, object]]:
    """Sessions, newest activity first; one per resume chain unless `all`."""
    filtered_later = status is not None or cwd is not None
    with connection(request) as conn:
        rows = list_sessions(
            conn,
            query=q,
            project_key=project,
            since=since,
            include_archived=all_,
            collapse_chains=not all_,
            limit=_ALL_ROWS if filtered_later else limit,
        )
    if cwd is not None:
        rows = [row for row in rows if row.cwd == cwd]
    rows = _with_status(request, rows)
    if status is not None:
        rows = [row for row in rows if row.status == status]
    return [session_to_dict(row) for row in rows[:limit]]


@router.get("/sessions/{session_id}", response_model=SessionDetailOut)
def session_detail(request: Request, session_id: str) -> dict[str, object]:
    """One session by id, unique id prefix or exact title, with its chain."""
    with connection(request) as conn:
        row = get_session(conn, session_id)
        if row is None:
            raise HTTPException(status_code=404, detail=_no_session(session_id))
        links = chain(conn, row.chain_id)
    row, *links = _with_status(request, [row, *links])
    data = session_to_dict(row)
    data["chain"] = [session_to_dict(link) for link in links]
    data["resume_command"] = resume_command(row)
    return data


@router.get("/projects", response_model=list[ProjectOut])
def projects(request: Request) -> list[dict[str, object]]:
    """Projects (git remote, else folder), newest activity first."""
    with connection(request) as conn:
        rows = list_projects(conn)
    return [
        {
            "project_key": row.project_key,
            "label": row.label,
            "session_count": row.session_count,
            "last_activity_at": to_db(row.last_activity_at),
        }
        for row in rows
    ]


@router.get("/health", response_model=HealthOut)
def health(request: Request) -> dict[str, object]:
    """Whether the service is up, what it runs, and how much it has indexed."""
    web = state(request)
    with connection(request) as conn:
        count = conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
    caps, _ = web.live.get()
    return {
        "status": "ok",
        "version": vigil.__version__,
        "claude_version": caps.version,
        "indexed_sessions": count,
        "last_scan_at": to_db(web.last_scan_at),
    }


def _refusal(status: int, reason: str) -> JSONResponse:
    return JSONResponse({"ok": False, "reason": reason}, status_code=status)


@actions.post(
    "/sessions/{session_id}/resume",
    response_model=ResumeOut,
    responses={404: {"model": RefusalOut}, 409: {"model": RefusalOut}},
)
def resume(request: Request, session_id: str) -> dict[str, object] | JSONResponse:
    """Resume an indexed session in its folder and open a terminal on it when one is available.

    The command is built from the indexed id and folder only. 409 when resume is refused (open in
    a terminal, folder gone, nothing to launch with); the reason says why.
    """
    web = state(request)
    with connection(request) as conn:
        row = get_session(conn, session_id)
        if row is None:
            return _refusal(404, _no_session(session_id))
        # Fresh, not cached: never start a second process on a session that just opened.
        caps, live = web.live.get(fresh=True)
        try:
            plan = plan_resume(row, caps, live, web.config.resume, os.environ)
            attach = execute(plan, conn=conn)
        except ResumeRefused as refused:
            return _refusal(409, refused.reason)
    web.live.clear()
    return {"ok": True, "attach": attach, "opened_terminal": plan.terminal is not None}


_META_FIELDS = ("label", "note", "pinned", "archived")
_TRUE = frozenset({"true", "on", "1", "yes"})
_FALSE = frozenset({"false", "off", "0", "no"})


async def _meta_form(request: Request) -> dict[str, str]:
    """The metadata fields present in the posted form, as text. When a field is sent more than
    once the last value wins (so a hidden `false` before a checkbox gives its state).

    Read by hand because FastAPI's `Form()` treats an empty field as absent, and here an empty
    label or note means "clear it"."""
    form = await request.form()
    fields: dict[str, str] = {}
    for name in _META_FIELDS:
        values = [value for value in form.getlist(name) if isinstance(value, str)]
        if values:
            fields[name] = values[-1]
    return fields


def _flag(fields: dict[str, str], name: str) -> bool | Literal[_Unset.UNSET]:
    if name not in fields:
        return UNSET
    value = fields[name].strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise HTTPException(
        status_code=422, detail=f"{name} must be true or false, not {fields[name]!r}"
    )


def _text(fields: dict[str, str], name: str) -> str | None | Literal[_Unset.UNSET]:
    if name not in fields:
        return UNSET
    return fields[name].strip() or None


@actions.post(
    "/sessions/{session_id}/meta",
    response_model=SessionOut,
    responses={404: {"model": RefusalOut}},
)
def meta(
    request: Request,
    session_id: str,
    fields: Annotated[dict[str, str], Depends(_meta_form)],
) -> dict[str, object] | JSONResponse:
    """Change vigil's own `label`, `note`, `pinned` and `archived` for a session, from form
    fields. Only the fields sent change; an empty label or note clears it; `pinned` and
    `archived` take true/false, on/off, 1/0 or yes/no. Returns the updated session."""
    pinned, archived = _flag(fields, "pinned"), _flag(fields, "archived")
    label, note = _text(fields, "label"), _text(fields, "note")
    with connection(request) as conn:
        row = get_session(conn, session_id)
        if row is None:
            return _refusal(404, _no_session(session_id))
        if fields:
            set_meta(conn, row.session_id, label=label, note=note, pinned=pinned, archived=archived)
        updated = get_session(conn, row.session_id)
    if updated is None:  # removed by a concurrent rescan meanwhile
        return _refusal(404, _no_session(session_id))
    (updated,) = _with_status(request, [updated])
    return session_to_dict(updated)
