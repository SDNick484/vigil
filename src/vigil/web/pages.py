"""The HTML pages (`router`): the sessions list, the session detail page and the static files.

With peers configured (v2, `vigil.peers`), the sessions list merges in the peers' sessions with a
machine column, filtered here as the index filters this machine's own, and a peer's session has a
detail page under `/peers/{name}/sessions/{id}` built from what the peer last sent.

Pages are server-rendered Jinja2 templates over the same queries the JSON API uses; htmx (vendored
in `static/`) only adds the live search filter and the inline results of Resume and metadata
edits. Viewing works without JavaScript. The state-changing POSTs are the ones in `vigil.web.api`.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from vigil.claude_cli import LiveSession
from vigil.commands import resume_command
from vigil.index.queries import SessionRow, chain, get_session, list_projects, list_sessions
from vigil.peers import (
    PeerSession,
    filter_sessions,
    local_machine,
    newest_first,
    with_machine,
)
from vigil.resume import TERMINAL_ALLOWED
from vigil.serialize import apply_status, display_name, open_link
from vigil.usage import build_report, refresh_sources
from vigil.web import connection, state

router = APIRouter(tags=["pages"])

WEB_DIR = Path(__file__).parent
STATIC_DIR = WEB_DIR / "static"

LIST_LIMIT = 500
"""Most sessions the sessions page lists."""

PREVIEW_CHARS = 120
"""How much of a session's first prompt a list row shows when it has no name."""


def _when(value: datetime | None) -> str:
    """A time as `YYYY-MM-DD HH:MM` in the machine's local zone, or a dash."""
    return value.astimezone().strftime("%Y-%m-%d %H:%M") if value else "-"


def _preview(text: str | None) -> str:
    """The first line of `text`, shortened for a list row."""
    first = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return first if len(first) <= PREVIEW_CHARS else first[: PREVIEW_CHARS - 1] + "…"


def peer_of(row: SessionRow) -> str | None:
    """The peer a session is on, or None for this machine's own."""
    return row.peer if isinstance(row, PeerSession) else None


def session_url(row: SessionRow, session_id: str | None = None) -> str:
    """The detail page of `row` (or of `session_id` on the same machine)."""
    ident = quote(session_id or row.session_id, safe="")
    peer = peer_of(row)
    return f"/peers/{quote(peer, safe='')}/sessions/{ident}" if peer else f"/sessions/{ident}"


def row_key(row: SessionRow) -> str:
    """A name for `row` that is unique on the page (ids repeat across machines), for element ids
    and for `open_links`."""
    peer = peer_of(row)
    return f"{peer}-{row.session_id}" if peer else row.session_id


def open_links(rows: list[SessionRow], live: dict[str, LiveSession]) -> dict[str, str]:
    """The Remote Control link to offer for each of `rows` that has one, by `row_key`: this
    machine's sessions by `vigil.serialize.open_link`. A peer's session shows the link its
    owner last sent while it is running or in the background and the peer is reachable; its
    live start time is not known here."""
    links: dict[str, str] = {}
    for row in rows:
        if isinstance(row, PeerSession):
            link = row.remote_url if is_active(row) else None
        else:
            link = open_link(row, live)
        if link is not None:
            links[row_key(row)] = link
    return links


def resume_url(row: SessionRow) -> str:
    """Where Resume on `row` posts: this machine's resume action, or the one forwarding to its
    peer."""
    return session_url(row) + "/resume"


templates = Jinja2Templates(directory=WEB_DIR / "templates")
templates.env.filters["when"] = _when
templates.env.globals["session_name"] = display_name
templates.env.globals["preview"] = _preview
templates.env.globals["peer_of"] = peer_of
templates.env.globals["session_url"] = session_url
templates.env.globals["resume_url"] = resume_url
templates.env.globals["row_key"] = row_key


def is_active(row: SessionRow) -> bool:
    """Whether `row` is running or in the background right now. A stale peer row is what the peer
    last sent, not what is happening, so it is never active."""
    stale = isinstance(row, PeerSession) and row.stale
    return row.status in ("running", "background") and not stale


templates.env.globals["is_active"] = is_active


@dataclass(frozen=True)
class ProjectGroup:
    """The sessions of one project, in the order they were listed (newest activity first)."""

    key: str
    label: str
    sessions: list[SessionRow]
    active: bool = False
    """Some session is running or in the background (and not stale)."""
    idle_count: int = 0
    """How many of the sessions are not active."""

    @property
    def running(self) -> int:
        return sum(1 for s in self.sessions if is_active(s) and s.status == "running")

    @property
    def background(self) -> int:
        return sum(1 for s in self.sessions if is_active(s) and s.status == "background")


@dataclass(frozen=True)
class StatusCounts:
    """What the sessions page's summary line shows. `idle` is every session that is not active,
    `unknown` ones included."""

    running: int
    background: int
    idle: int
    projects: int


def group_by_project(rows: list[SessionRow]) -> list[ProjectGroup]:
    """`rows` grouped by project; groups are ordered by their newest session, which comes first
    in `rows`."""
    grouped: dict[str, list[SessionRow]] = {}
    for row in rows:
        grouped.setdefault(row.project_key, []).append(row)
    return [
        ProjectGroup(
            key,
            sessions[0].project_label,
            sessions,
            active=any(is_active(s) for s in sessions),
            idle_count=sum(1 for s in sessions if not is_active(s)),
        )
        for key, sessions in grouped.items()
    ]


def split_groups(groups: list[ProjectGroup]) -> tuple[list[ProjectGroup], list[ProjectGroup]]:
    """`groups` as `(active, quiet)`, each in the order given."""
    return [g for g in groups if g.active], [g for g in groups if not g.active]


def count_statuses(rows: list[SessionRow]) -> StatusCounts:
    """The running, background and idle sessions in `rows`, and how many projects they span."""
    running = sum(1 for r in rows if is_active(r) and r.status == "running")
    background = sum(1 for r in rows if is_active(r) and r.status == "background")
    return StatusCounts(
        running,
        background,
        len(rows) - running - background,
        len({r.project_key for r in rows}),
    )


def _render(request: Request, name: str, context: dict[str, Any], status_code: int = 200) -> Any:
    return templates.TemplateResponse(request, name, context, status_code=status_code)


@router.get("/", response_class=HTMLResponse)
def sessions_page(
    request: Request,
    q: Annotated[str, Query()] = "",
    project: Annotated[str, Query()] = "",
    all_: Annotated[bool, Query(alias="all")] = False,
) -> Any:
    """Sessions grouped by project, newest activity first. A request from htmx (the live search)
    gets just the results, to swap into the page."""
    with connection(request) as conn:
        rows = list_sessions(
            conn,
            query=q or None,
            project_key=project or None,
            include_archived=all_,
            collapse_chains=not all_,
            limit=LIST_LIMIT,
        )
    web = state(request)
    caps, live = web.live.get()
    rows = apply_status(rows, caps, live)
    offline: list[tuple[str, datetime | None, str]] = []
    if web.config.peers:
        merged = with_machine(rows, local_machine(web.config))
        for name, result in web.peers.results().items():
            merged += filter_sessions(
                result.sessions,
                query=q or None,
                project_key=project or None,
                include_archived=all_,
                collapse_chains=not all_,
            )
            if result.error is not None:
                offline.append((name, result.fetched_at, result.error))
        rows = newest_first(merged)[:LIST_LIMIT]
    groups = group_by_project(rows)
    active, quiet = split_groups(groups)
    context = {
        "groups": groups,
        "active": active,
        "quiet": quiet,
        "counts": count_statuses(rows),
        "searching": bool(q),
        "count": len(rows),
        "q": q,
        "project": project,
        "all": all_,
        "machines": bool(web.config.peers),
        "offline": offline,
        "open_links": open_links(rows, live),
        "at_desk": TERMINAL_ALLOWED.get(),
    }
    if request.headers.get("hx-request") == "true":
        # Just the results block, to swap into the page without reloading it.
        template = templates.get_template("sessions.html")
        results = template.blocks["results"](template.new_context({"request": request, **context}))
        return HTMLResponse("".join(results))
    return _render(request, "sessions.html", context)


@router.get("/sessions/{session_id}", response_class=HTMLResponse)
def session_page(request: Request, session_id: str) -> Any:
    """Everything indexed about one session, its last exchange, the command that resumes it by
    hand, its resume chain, and forms to edit its label, note, pin and archive state."""
    with connection(request) as conn:
        row = get_session(conn, session_id)
        links = chain(conn, row.chain_id) if row is not None else []
    if row is None:
        return _render(request, "session_detail.html", {"row": None, "ident": session_id}, 404)
    web = state(request)
    caps, live = web.live.get()
    row, *links = apply_status([row, *links], caps, live)
    context = {
        "row": row,
        "chain": links if len(links) > 1 else [],
        "command": resume_command(row),
        "machine": local_machine(web.config),
        "open_links": open_links([row], live),
        "at_desk": TERMINAL_ALLOWED.get(),
    }
    return _render(request, "session_detail.html", context)


@router.get("/peers/{peer}/sessions/{session_id}", response_class=HTMLResponse)
def peer_session_page(request: Request, peer: str, session_id: str) -> Any:
    """A peer's session as the peer last sent it: what it holds, the command that resumes it by
    hand on that machine, and Resume (forwarded to the peer). Label and note are edited on the
    peer itself."""
    hub = state(request).peers
    result = hub.results().get(peer) if hub.peer(peer) is not None else None
    row = None
    if result is not None:
        row = next((s for s in result.sessions if s.session_id == session_id), None)
    if result is None or row is None:
        context: dict[str, Any] = {"row": None, "ident": session_id}
        return _render(request, "session_detail.html", context, 404)
    context = {
        "row": row,
        "chain": [],
        "command": resume_command(row),
        "machine": peer,
        "result": result,
        "open_links": open_links([row], {}),
        "at_desk": TERMINAL_ALLOWED.get(),
    }
    return _render(request, "session_detail.html", context)


@router.get("/usage", response_class=HTMLResponse)
def usage_page(request: Request, project: Annotated[str, Query()] = "") -> Any:
    """What you use inside sessions (optionally for one project), how you launch `claude`, and the
    flags and subcommands of the installed version you have never tried."""
    web = state(request)
    with connection(request) as conn:
        refresh_sources(conn, web.config)
        report = build_report(
            conn, project_key=project or None, history_enabled=web.config.usage.shell_history
        )
        projects = list_projects(conn)
    context = {"report": report, "project": project, "projects": projects}
    return _render(request, "usage.html", context)


@router.get("/static/{path:path}")
def static_file(path: str) -> Response:
    """A file from the package's `static` folder (stylesheet, htmx), and nothing outside it."""
    target = (STATIC_DIR / path).resolve()
    if not target.is_relative_to(STATIC_DIR.resolve()) or not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(target)
