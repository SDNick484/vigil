"""The HTML pages (`router`): the sessions list, the session detail page and the static files.

Pages are server-rendered Jinja2 templates over the same queries the JSON API uses; htmx (vendored
in `static/`) only adds the live search filter and the inline results of Resume and metadata
edits. Viewing works without JavaScript. The state-changing POSTs are the ones in `vigil.web.api`.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from vigil.commands import resume_command
from vigil.index.queries import SessionRow, chain, get_session, list_projects, list_sessions
from vigil.serialize import apply_status, display_name
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


templates = Jinja2Templates(directory=WEB_DIR / "templates")
templates.env.filters["when"] = _when
templates.env.globals["session_name"] = display_name
templates.env.globals["preview"] = _preview


@dataclass(frozen=True)
class ProjectGroup:
    """The sessions of one project, in the order they were listed (newest activity first)."""

    key: str
    label: str
    sessions: list[SessionRow]


def group_by_project(rows: list[SessionRow]) -> list[ProjectGroup]:
    """`rows` grouped by project; groups are ordered by their newest session, which comes first
    in `rows`."""
    groups: dict[str, ProjectGroup] = {}
    for row in rows:
        group = groups.setdefault(
            row.project_key, ProjectGroup(row.project_key, row.project_label, [])
        )
        group.sessions.append(row)
    return list(groups.values())


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
    caps, live = state(request).live.get()
    rows = apply_status(rows, caps, live)
    context = {
        "groups": group_by_project(rows),
        "count": len(rows),
        "q": q,
        "project": project,
        "all": all_,
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
    caps, live = state(request).live.get()
    row, *links = apply_status([row, *links], caps, live)
    context = {
        "row": row,
        "chain": links if len(links) > 1 else [],
        "command": resume_command(row),
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
