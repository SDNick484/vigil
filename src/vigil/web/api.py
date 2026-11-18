"""The JSON API (`router`, read-only, under `/api/v1`) and the state-changing actions (`actions`:
resume and metadata edits, POST only, outside `/api`).

Sessions are served exactly as `vigil.serialize.session_to_dict` makes them (`SessionOut` mirrors
it), so `vigil ls --json` and `vigil show --json` print the same shapes. Every session served has
its status from the live sessions, cached for a few seconds (`vigil.web.LiveStatus`).
"""

import functools
import os
import shlex
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated, Literal, NamedTuple

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import vigil
from vigil.catalog import current_catalog
from vigil.claude_cli import Capabilities, LiveSession
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
from vigil.peers import ResumeForwardError
from vigil.remote import find_transcript, wait_for_remote_url
from vigil.resume import (
    TERMINAL_ALLOWED,
    Launched,
    ResumePlan,
    ResumeRefused,
    attach_argv,
    execute,
    joinable_link,
    plan_resume,
    session_lock,
    stop_session,
)
from vigil.serialize import apply_status, session_to_dict
from vigil.usage import build_report, catalog_entry_to_dict, refresh_sources, report_to_dict
from vigil.web import WebState, connection, state

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
    remote_url: str | None
    remote_url_at: str | None
    open_url: str | None
    busy: bool


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


class FeatureUseOut(BaseModel):
    kind: str
    name: str
    count: int
    last_used_at: str | None


class CatalogEntryOut(BaseModel):
    kind: Literal["flag", "subcommand", "subcommand_flag"]
    name: str
    short: str | None
    parent: str | None
    description: str


class UsageOut(BaseModel):
    """`features` from transcripts (overall, or one `project`); `launch_flags` from launches
    (vigil's resumes and, when enabled, shell history), short flags as their long names;
    `never_used` the installed claude's flags and subcommands no launch used."""

    features: list[FeatureUseOut]
    launch_flags: list[FeatureUseOut]
    never_used: list[CatalogEntryOut]
    history_enabled: bool
    catalog_available: bool


class HealthOut(BaseModel):
    status: Literal["ok"]
    version: str
    claude_version: str | None
    indexed_sessions: int
    last_scan_at: str | None


class ResumeOut(BaseModel):
    """`remote_url` is the session's Remote Control link, set for a request from another machine
    (which gets no terminal) once the session has one; `pending` says the session was started for
    that request but no link had appeared within `resume.remote_link_timeout`. `at_desk` says the
    request was made at this machine (loopback): the page shows a link there rather than going
    to it. `copy_of` is only there when `claude --bg` started a copy of the session asked for
    (`copy_of`) under a new id (`session_id`, when it could be found; else `attach` names the
    copy by its job id), as it does for a session that was itself started in the background:
    that session keeps its own options."""

    ok: Literal[True]
    attach: str
    opened_terminal: bool
    remote_url: str | None = None
    pending: bool = False
    at_desk: bool = False
    copy_of: str | None = None
    session_id: str | None = None


class RefusalOut(BaseModel):
    """`restartable` (resume refusals only): stopping the session and starting it again with
    Remote Control would get past the refusal."""

    ok: Literal[False]
    reason: str
    restartable: bool = False


class ForwardedResumeOut(ResumeOut):
    """A resume forwarded to a peer: `attach` is to be run on `machine`."""

    machine: str


class PeerOut(BaseModel):
    """A configured peer (v2) and how its last fetch went: `stale` when it could not be reached
    (then `error` says why, and `fetched_at` is when its last-known sessions were fetched)."""

    name: str
    url: str
    fetched_at: str | None
    stale: bool
    error: str | None
    session_count: int


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


@router.get("/usage", response_model=UsageOut)
def usage(
    request: Request,
    project: Annotated[str | None, Query(description="a project key (limits `features`)")] = None,
    since: Annotated[datetime | None, Query(description="active at or after (ISO 8601)")] = None,
) -> dict[str, object]:
    """What is used (counts and recency per feature), how `claude` is launched, and what the
    installed version offers that was never used. Brings the catalog and, when
    `usage.shell_history` is on, the launch history up to date first."""
    web = state(request)
    with connection(request) as conn:
        refresh_sources(conn, web.config)
        report = build_report(
            conn, project_key=project, since=since, history_enabled=web.config.usage.shell_history
        )
    return report_to_dict(report)


@router.get("/catalog", response_model=list[CatalogEntryOut])
def catalog(request: Request) -> list[dict[str, object]]:
    """The flags and subcommands the installed `claude` lists, from its own `--help` (the latest
    version seen); empty when no `claude` has been found."""
    with connection(request) as conn:
        refresh_sources(conn, state(request).config)
        entries = current_catalog(conn)
    return [catalog_entry_to_dict(entry) for entry in entries]


@router.get("/peers", response_model=list[PeerOut])
def peers(request: Request) -> list[dict[str, object]]:
    """The configured peers, each asked for its sessions unless asked moments ago. Their sessions
    are not part of `/api/v1/sessions`, which lists this machine's only (it is what peers read)."""
    hub = state(request).peers
    results = hub.results()
    return [
        {
            "name": peer.name,
            "url": peer.url,
            "fetched_at": to_db(results[peer.name].fetched_at),
            "stale": results[peer.name].stale,
            "error": results[peer.name].error,
            "session_count": len(results[peer.name].sessions),
        }
        for peer in hub.peers
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


def _refusal(status: int, reason: str, *, restartable: bool | None = None) -> JSONResponse:
    body: dict[str, object] = {"ok": False, "reason": reason}
    if restartable is not None:
        body["restartable"] = restartable
    return JSONResponse(body, status_code=status)


@actions.post(
    "/sessions/{session_id}/resume",
    response_model=ResumeOut,
    response_model_exclude_unset=True,
    responses={404: {"model": RefusalOut}, 409: {"model": RefusalOut}},
)
def resume(request: Request, session_id: str) -> dict[str, object] | JSONResponse:
    """Resume an indexed session in its folder and open a terminal on it when one is available.

    The command is built from the indexed id and folder only. 409 when resume is refused (open in
    a terminal, folder gone, nothing to launch with); the reason says why, and `restartable`
    whether restarting the session with Remote Control would help.

    A request from another machine gets no terminal. It gets the session's Remote Control link
    instead: the one a live session already has (looked for in the index, then in the transcript),
    else the one that
    shows up (waiting up to `resume.remote_link_timeout`) after the session is started here.
    """
    web = state(request)
    with connection(request) as conn:
        row = get_session(conn, session_id)
        if row is None:
            return _refusal(404, _no_session(session_id))
        # One launch of a session at a time, and a fresh look at what is live (not the cached
        # one) under that lock: never start a second process on a session that just opened.
        with session_lock(row.session_id):
            caps, live = web.live.get(fresh=True)
            try:
                started = _start(web, conn, row, caps, live)
            except ResumeRefused as refused:
                return _refusal(409, refused.reason, restartable=refused.restartable)
    return _answer(web, started)


class _Started(NamedTuple):
    launched: Launched
    launched_at: datetime | None
    """When the session was started, or None when it was already running and nothing was."""
    link: str | None
    """The link of a session that was already running."""


def _start(
    web: WebState,
    conn: sqlite3.Connection,
    row: SessionRow,
    caps: Capabilities,
    live: dict[str, LiveSession],
    extra_flags: Sequence[str] = (),
) -> _Started:
    """Plan the resume of `row` against `live` (with `extra_flags` if it is started) and carry it
    out. Raises ResumeRefused."""
    plan = plan_resume(row, caps, live, web.config.resume, os.environ, extra_flags)
    if not TERMINAL_ALLOWED.get() and plan.start is None:
        # Live with a link (plan_resume refuses it without one): start nothing.
        return _Started(_as_it_is(plan), None, joinable_link(row, live))
    return _launch(plan, conn)


def _as_it_is(plan: ResumePlan) -> Launched:
    """What answering with `plan` without carrying it out leaves (a live session's link)."""
    return Launched(shlex.join(plan.attach), plan.terminal, plan.session_id)


def _launch(plan: ResumePlan, conn: sqlite3.Connection) -> _Started:
    """Carry out `plan`, which starts a session. Raises ResumeRefused."""
    launched_at = datetime.now(UTC)
    return _Started(execute(plan, conn=conn), launched_at, None)


def _without(live: dict[str, LiveSession], row: SessionRow) -> dict[str, LiveSession]:
    """`live` as it will be once `row` is stopped."""
    return {session_id: item for session_id, item in live.items() if session_id != row.session_id}


def _answer(web: WebState, started: _Started) -> dict[str, object]:
    """The answer to a resume once `started` happened: from another machine, wait for the link of
    a session that was just started, in the transcript of the session that runs (found under the
    Claude home by its id; it may be a copy, or new). A new session whose id could not be found
    has no transcript to wait on: it is pending."""
    launched, launched_at, link = started
    if launched_at is None:
        return _resumed(launched, link)
    web.live.clear()
    if launched.session_id is None:
        return _resumed(launched, None, pending=True)
    if TERMINAL_ALLOWED.get():
        return _resumed(launched, None)
    transcript = functools.partial(find_transcript, web.config.claude_home, launched.session_id)
    link = wait_for_remote_url(transcript, launched_at, web.config.resume.remote_link_timeout)
    return _resumed(launched, link, pending=link is None)


@actions.post(
    "/sessions/{session_id}/restart-remote",
    response_model=ResumeOut,
    response_model_exclude_unset=True,
    responses={
        400: {"model": RefusalOut},
        404: {"model": RefusalOut},
        409: {"model": RefusalOut},
    },
)
def restart_remote(
    request: Request, session_id: str, confirm: Annotated[str | None, Form()] = None
) -> dict[str, object] | JSONResponse:
    """Stop a session running in the background and start it again with Remote Control, for a
    session that has no link to join by. Interrupts whatever it is doing, so the form must say
    `confirm=yes` (400 otherwise). 409 unless the session is running in the background, or when
    the restart could not go ahead (the session is planned for before anything is stopped, so a
    bad config, a missing folder or tmux and the like refuse with it untouched), or when it
    cannot be stopped (nothing is started). If it is stopped but then cannot be started, the
    reason says so.

    After the stop it is the resume of a session that is not running: from another machine no
    terminal is opened, and the answer is that of a resume (the link, or `pending`). A session
    that was itself started in the background keeps its own options, so it comes back as a copy
    under a new id (`copy_of`, `session_id`). A session that can already be joined (restarted
    from another tab, say) is not stopped: the answer is the resume answer with its link."""
    if confirm != "yes":
        return _refusal(400, "restarting needs confirm=yes")
    web = state(request)
    with connection(request) as conn:
        row = get_session(conn, session_id)
        if row is None:
            return _refusal(404, _no_session(session_id))
        with session_lock(row.session_id):
            caps, live = web.live.get(fresh=True)
            running = live.get(row.session_id)
            link = joinable_link(row, live) if running is not None else None
            if link is not None:
                attach = shlex.join(attach_argv(row.session_id))
                return _resumed(Launched(attach, None, row.session_id), link)
            if running is None or running.kind != "background":
                return _refusal(
                    409,
                    "only a session running in the background can be restarted",
                    restartable=False,
                )
            try:
                # Everything that can refuse a start is checked before anything is stopped.
                plan_resume(row, caps, _without(live, row), web.config.resume, os.environ)
                stop_session(row.session_id, web.live.probe)
            except ResumeRefused as refused:
                return _refusal(409, refused.reason, restartable=False)
            caps, live = web.live.get(fresh=True)
            try:
                started = _start(web, conn, row, caps, live)
            except ResumeRefused as refused:
                reason = f"stopped, but could not start it again: {refused.reason}"
                return _refusal(409, reason, restartable=False)
    return _answer(web, started)


def _resumed(launched: Launched, link: str | None, *, pending: bool = False) -> dict[str, object]:
    """A resume's answer (`ResumeOut`): with `copy_of` only for a copy, and `session_id` too when
    the copy's full id is known."""
    answer: dict[str, object] = {
        "ok": True,
        "attach": launched.attach,
        "opened_terminal": launched.terminal is not None,
        "remote_url": link,
        "pending": pending,
        "at_desk": TERMINAL_ALLOWED.get(),
    }
    if launched.copy_of is not None:
        answer["copy_of"] = launched.copy_of
        if launched.session_id is not None:
            answer["session_id"] = launched.session_id
    return answer


def at_desk(answer: dict[str, object]) -> dict[str, object]:
    """A peer's launch answer (`vigil.peers.launch_answer`) with `at_desk` set for this request:
    the peer's own says only whether the peer's request was at the peer's desk."""
    return {**answer, "at_desk": TERMINAL_ALLOWED.get()}


@actions.post(
    "/peers/{peer}/sessions/{session_id}/resume",
    response_model=ForwardedResumeOut,
    response_model_exclude_unset=True,
    responses={404: {"model": RefusalOut}, 409: {"model": RefusalOut}, 502: {"model": RefusalOut}},
)
def resume_on_peer(
    request: Request, peer: str, session_id: str
) -> dict[str, object] | JSONResponse:
    """Resume a session of the peer called `peer`: forwarded to that peer's own resume action
    with its token (`vigil.peers.forward_resume`). The peer starts it and, since the request
    comes from another machine, answers with the attach command to run there. The peer's 404
    and 409 refusals come back as they are; 502 when it cannot be reached or refuses the
    request."""
    try:
        return at_desk(state(request).peers.resume(peer, session_id))
    except ResumeForwardError as refused:
        return _refusal(
            refused.status,
            refused.reason,
            restartable=refused.restartable if refused.status == 409 else None,
        )


@actions.post(
    "/peers/{peer}/sessions/{session_id}/restart-remote",
    response_model=ForwardedResumeOut,
    response_model_exclude_unset=True,
    responses={
        400: {"model": RefusalOut},
        404: {"model": RefusalOut},
        409: {"model": RefusalOut},
        502: {"model": RefusalOut},
    },
)
def restart_on_peer(
    request: Request, peer: str, session_id: str, confirm: Annotated[str | None, Form()] = None
) -> dict[str, object] | JSONResponse:
    """Restart a session of the peer called `peer` with Remote Control: forwarded, with
    `confirm=yes`, to that peer's own restart action (`vigil.peers.forward_restart`); asked for
    here without that confirmation it is refused with 400 and nothing is sent. Answers as
    `resume_on_peer` does."""
    if confirm != "yes":
        return _refusal(400, "restarting needs confirm=yes")
    try:
        return at_desk(state(request).peers.restart(peer, session_id))
    except ResumeForwardError as refused:
        return _refusal(
            refused.status,
            refused.reason,
            restartable=refused.restartable if refused.status == 409 else None,
        )


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
