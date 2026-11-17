"""Session reviews on the web (`router`): start one, show one, list them.

The handlers are thin over `vigil.review.runner` and `vigil.review.store`. A review runs in a
background thread with its own index connection; the session page polls its fragment (every 2 s,
htmx) until it is done. One review runs at a time: the service's `ReviewSlot`, and the index for
a review that `vigil review` is running in another process.
"""

import logging
import threading
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from vigil.catalog import current_catalog, refresh_catalog
from vigil.index.queries import get_session
from vigil.review.runner import (
    PendingReview,
    ReviewBusy,
    ReviewRefused,
    begin_review,
    check_reviewable,
    claim_slot,
    complete_review,
)
from vigil.review.store import get_review, list_reviews, review_to_dict
from vigil.web import WebState, connection, state
from vigil.web.pages import templates

log = logging.getLogger(__name__)

router = APIRouter(tags=["review"])

STOP_POLLING = 286
"""The status that tells htmx to stop polling an element."""


class SuggestionOut(BaseModel):
    kind: Literal["flag", "file", "habit"]
    title: str
    why: str
    evidence: str
    flag_name: str | None
    flag_value: str | None
    file_target: (
        Literal["project_claude_md", "project_settings", "user_claude_md", "user_settings"] | None
    )
    file_text: str | None
    file_settings: dict[str, Any] | None


class AdviceOut(BaseModel):
    title: str
    why: str


class ReviewOut(BaseModel):
    """One review, as `vigil.review.store.review_to_dict` makes it. `numbers` are the ones the
    review was given (`vigil.review.numbers.numbers_to_dict`); `error` says why it failed, or is
    a note on one that is done (such as going over the budget)."""

    id: int
    session_id: str
    machine: str
    created_at: str
    finished_at: str | None
    claude_version: str | None
    model: str | None
    cost_usd: float | None
    status: Literal["running", "done", "failed"]
    numbers: dict[str, Any] | None
    suggestions: list[SuggestionOut]
    advice: list[AdviceOut]
    error: str | None


class ReviewStartedOut(BaseModel):
    ok: Literal[True]
    review_id: int


class ReviewRefusalOut(BaseModel):
    """`review_id` (when busy): the review that is running."""

    ok: Literal[False]
    reason: str
    review_id: int | None = None


def _refusal(status: int, reason: str, **extra: object) -> JSONResponse:
    return JSONResponse({"ok": False, "reason": reason, **extra}, status_code=status)


def _no_session(ident: str) -> str:
    return f"no session matches {ident!r} (unknown, or an ambiguous id prefix)"


def _run_in_background(web: WebState, pending: PendingReview) -> None:
    try:
        conn = web.conn_factory()
        try:
            complete_review(pending, conn, slot=web.review_slot)
        finally:
            conn.close()
    except Exception:
        log.exception("review %d failed", pending.review_id)
    finally:
        web.review_slot.finish()


@router.post(
    "/sessions/{session_id}/review",
    status_code=202,
    response_model=ReviewStartedOut,
    responses={404: {"model": ReviewRefusalOut}, 409: {"model": ReviewRefusalOut}},
)
def start_review(request: Request, session_id: str) -> dict[str, object] | JSONResponse:
    """Start a review of a session: Claude reads a fork of it that is not persisted and has no
    tools, and suggests flags, project file changes and habits. Answers 202 with the review's id
    at once; the review runs in the background (follow it with the fragment or the read API).
    409 when a review is already running (with its `review_id`) or the session's folder is gone
    or `claude` is missing."""
    web = state(request)
    with connection(request) as conn:
        row = get_session(conn, session_id)
        if row is None:
            return _refusal(404, _no_session(session_id))
        try:
            check_reviewable(row)
        except ReviewRefused as refused:
            return _refusal(409, refused.reason)
        try:
            claim_slot(web.review_slot, conn)
        except ReviewBusy as busy:
            return _refusal(409, str(busy), review_id=busy.review_id)
        started = False
        try:
            refresh_catalog(conn)
            caps, _ = web.live.get()
            pending = begin_review(
                row,
                web.config.review,
                current_catalog(conn),
                web.config.resume.default_flags,
                conn,
                claude_version=caps.version,
            )
            threading.Thread(
                target=_run_in_background,
                args=(web, pending),
                name=f"vigil-review-{pending.review_id}",
                daemon=True,
            ).start()
            started = True
            return {"ok": True, "review_id": pending.review_id}
        except ReviewRefused as refused:
            return _refusal(409, refused.reason)
        finally:
            if not started:
                web.review_slot.finish()


@router.get("/sessions/{session_id}/reviews/{review_id}", response_class=HTMLResponse)
def review_fragment(request: Request, session_id: str, review_id: int) -> Any:
    """One review of a session as an HTML fragment (`_review.html`), for the session page. While
    the review runs, the fragment reloads itself every 2 s. A review that is gone (or not this
    session's) answers 286, which makes htmx stop polling, with a line saying so."""
    with connection(request) as conn:
        review = get_review(conn, review_id)
    if review is None or review.session_id != session_id:
        return HTMLResponse(
            f'<article class="review" id="review-{review_id}"><p class="muted">'
            "This review is no longer available.</p></article>",
            status_code=STOP_POLLING,
        )
    return templates.TemplateResponse(request, "_review.html", {"review": review})


@router.get("/api/v1/sessions/{session_id}/reviews", response_model=list[ReviewOut], tags=["api"])
def reviews(request: Request, session_id: str) -> list[dict[str, object]]:
    """The reviews of a session (id, unique id prefix or exact title), newest first."""
    with connection(request) as conn:
        row = get_session(conn, session_id)
        if row is None:
            raise HTTPException(status_code=404, detail=_no_session(session_id))
        found = list_reviews(conn, row.session_id)
    return [review_to_dict(review) for review in found]
