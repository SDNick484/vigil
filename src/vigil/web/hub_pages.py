"""The hub's session page (`router`): `GET /machines/{name}/sessions/{session_id}`.

A hub holds what each machine last pushed (`vigil.hub`) and runs nothing, so the page offers
hand-offs instead of actions: join the session when it is live (its Remote Control link), resume
it on the machine that owns it (that machine's own vigil, when the hub knows its URL) and the
command that resumes it by hand. It answers 404 on a vigil that is not a hub.
"""

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from vigil.hub import find_hub_session, hub_rows
from vigil.web import hub_only, state
from vigil.web.pages import _render

router = APIRouter(tags=["hub"], dependencies=[Depends(hub_only)])


@router.get("/machines/{name}/sessions/{session_id}", response_class=HTMLResponse)
def hub_session_page(request: Request, name: str, session_id: str) -> Any:
    """One session of machine `name` as it last pushed it: what it holds, its last exchange and
    where to continue it. An unknown session is a 404 page."""
    web = state(request)
    hub = web.hub
    row = find_hub_session(hub_rows(hub, web.config), session_id, name) if hub else None
    if hub is None or row is None:
        return _render(request, "hub_session.html", {"row": None, "ident": session_id}, 404)
    seen = next((m.last_seen_at for m in hub.machines() if m.name == row.machine), None)
    context = {"row": row, "last_seen": seen or row.received_at}
    return _render(request, "hub_session.html", context)
