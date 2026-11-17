"""Session reviews on the web (`router`): start one, show one, list them, and act on a flag
suggestion (resume or start a new session with it, or add it to `resume.default_flags`).

The handlers are thin over `vigil.review.runner` and `vigil.review.store`. A review runs in a
background thread with its own index connection; the session page polls its fragment (every 2 s,
htmx) until it is done. One review runs at a time: the service's `ReviewSlot`, and the index for
a review that `vigil review` is running in another process.

Acting on a suggestion takes only the review's id and the suggestion's position from the client;
the flag is the stored one, checked with `vigil.flags.check` against the installed claude's
catalog before anything is launched or written.
"""

import functools
import logging
import os
import shlex
import sqlite3
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from vigil.catalog import CatalogEntry, current_catalog, refresh_catalog
from vigil.config import ConfigError
from vigil.config_edit import add_default_flags
from vigil.flags import FlagRefused, check, parse_suggested
from vigil.index.queries import SessionRow, get_session
from vigil.paths import config_path
from vigil.remote import find_transcript
from vigil.resume import ResumeRefused, plan_start_new, session_lock
from vigil.review.runner import (
    PendingReview,
    ReviewBusy,
    ReviewRefused,
    begin_review,
    check_reviewable,
    claim_slot,
    complete_review,
)
from vigil.review.schema import Suggestion
from vigil.review.store import get_review, list_reviews, review_to_dict
from vigil.web import WebState, connection, state
from vigil.web.api import RefusalOut, ResumeOut, _answer, _launch, _start
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


class AlwaysUseOut(BaseModel):
    """`default_flags`: `resume.default_flags` as the config file now has them."""

    ok: Literal[True]
    default_flags: list[str]


class ReviewRefusalOut(BaseModel):
    """`review_id` (when busy): the review that is running."""

    ok: Literal[False]
    reason: str
    review_id: int | None = None


def _refusal(status: int, reason: str, **extra: object) -> JSONResponse:
    return JSONResponse({"ok": False, "reason": reason, **extra}, status_code=status)


def _no_session(ident: str) -> str:
    return f"no session matches {ident!r} (unknown, or an ambiguous id prefix)"


@dataclass(frozen=True)
class FlagCard:
    """What a flag suggestion's card offers (`_suggestion_actions.html`)."""

    command: str
    """The suggested command line, `claude <flag> [value]`, quoted for a shell."""
    refused: str | None
    """Why vigil will not launch the flag (then the card only offers `command` to copy)."""


def flag_card(suggestion: Suggestion, catalog: Sequence[CatalogEntry]) -> FlagCard:
    """The card of a flag suggestion: launchable when `vigil.flags.check` allows its flag for the
    installed claude (`catalog`), copy-only otherwise."""
    tokens = parse_suggested(suggestion.flag_name or "", suggestion.flag_value)
    try:
        check(tokens, catalog)
    except FlagRefused as refused:
        return FlagCard(shlex.join(["claude", *tokens]), refused.reason)
    return FlagCard(shlex.join(["claude", *tokens]), None)


templates.env.globals["flag_card"] = flag_card


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
        catalog = current_catalog(conn)
    if review is None or review.session_id != session_id:
        return HTMLResponse(
            f'<article class="review" id="review-{review_id}"><p class="muted">'
            "This review is no longer available.</p></article>",
            status_code=STOP_POLLING,
        )
    return templates.TemplateResponse(
        request, "_review.html", {"review": review, "catalog": catalog}
    )


@router.get("/api/v1/sessions/{session_id}/reviews", response_model=list[ReviewOut], tags=["api"])
def reviews(request: Request, session_id: str) -> list[dict[str, object]]:
    """The reviews of a session (id, unique id prefix or exact title), newest first."""
    with connection(request) as conn:
        row = get_session(conn, session_id)
        if row is None:
            raise HTTPException(status_code=404, detail=_no_session(session_id))
        found = list_reviews(conn, row.session_id)
    return [review_to_dict(review) for review in found]


def _flag_suggestion(
    conn: sqlite3.Connection, review_id: int, index: int
) -> tuple[SessionRow, list[str]] | JSONResponse:
    """The session of review `review_id` and the flags of its suggestion `index`, checked against
    the installed claude's catalog; or the refusal to answer with (404 when there is no such
    suggestion or the session is not in the index, 409 when it is not a flag suggestion or its
    flag is refused)."""
    review = get_review(conn, review_id)
    if review is None or not 0 <= index < len(review.suggestions):
        return _refusal(404, f"review {review_id} has no suggestion {index}")
    suggestion = review.suggestions[index]
    if suggestion.kind != "flag" or not suggestion.flag_name:
        return _refusal(409, "not a flag suggestion")
    row = get_session(conn, review.session_id)
    if row is None or row.session_id != review.session_id:
        return _refusal(404, _no_session(review.session_id))
    try:
        tokens = check(
            parse_suggested(suggestion.flag_name, suggestion.flag_value), current_catalog(conn)
        )
    except FlagRefused as refused:
        return _refusal(409, refused.reason)
    return row, tokens


_ACTION_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": RefusalOut},
    409: {"model": RefusalOut},
}


@router.post(
    "/reviews/{review_id}/suggestions/{index}/resume",
    response_model=ResumeOut,
    responses=_ACTION_RESPONSES,
)
def resume_with_suggestion(
    request: Request, review_id: int, index: int
) -> dict[str, object] | JSONResponse:
    """Resume the reviewed session with the flag of suggestion `index` added for this launch
    only. Otherwise exactly a resume (`POST /sessions/{id}/resume`): a live session is joined,
    attached to or refused as it is, without the flag; the answer is a resume's. 409 when the
    flag is refused (before anything is launched) or the resume is."""
    web = state(request)
    with connection(request) as conn:
        found = _flag_suggestion(conn, review_id, index)
        if isinstance(found, JSONResponse):
            return found
        row, tokens = found
        with session_lock(row.session_id):
            caps, live = web.live.get(fresh=True)
            try:
                started = _start(web, conn, row, caps, live, tokens)
            except ResumeRefused as refused:
                return _refusal(409, refused.reason, restartable=refused.restartable)
    return _answer(web, Path(row.transcript_path), started)


@router.post(
    "/reviews/{review_id}/suggestions/{index}/start-new",
    response_model=ResumeOut,
    responses=_ACTION_RESPONSES,
)
def start_new_with_suggestion(
    request: Request, review_id: int, index: int
) -> dict[str, object] | JSONResponse:
    """Start a new session in the reviewed session's folder with the flag of suggestion `index`
    (`vigil.resume.plan_start_new`), under a new id vigil chooses. At the desk a terminal is
    opened on it; a request from another machine gets none, and waits for its Remote Control
    link as a resume does (its transcript is found by that id). Answers as a resume does; 409
    when the flag is refused (before anything is launched) or the start is."""
    web = state(request)
    new_id = str(uuid.uuid4())
    with connection(request) as conn:
        found = _flag_suggestion(conn, review_id, index)
        if isinstance(found, JSONResponse):
            return found
        row, tokens = found
        caps, _ = web.live.get()
        try:
            plan = plan_start_new(row.cwd, tokens, caps, web.config.resume, os.environ, new_id)
            started = _launch(plan, conn)
        except ResumeRefused as refused:
            return _refusal(409, refused.reason)
    transcript = functools.partial(find_transcript, web.config.claude_home, new_id)
    return _answer(web, transcript, started)


@router.post(
    "/reviews/{review_id}/suggestions/{index}/always-use",
    response_model=AlwaysUseOut,
    responses=_ACTION_RESPONSES,
)
def always_use_suggestion(
    request: Request, review_id: int, index: int
) -> dict[str, object] | JSONResponse:
    """Add the flag of suggestion `index` to `resume.default_flags` in vigil's config file
    (`vigil.config_edit.add_default_flags`; the rest of the file is kept as written), and use it
    from now on. 409 when the flag is refused or the config would not load (nothing is written
    then)."""
    web = state(request)
    with connection(request) as conn:
        found = _flag_suggestion(conn, review_id, index)
    if isinstance(found, JSONResponse):
        return found
    _, tokens = found
    try:
        add_default_flags(config_path(), tokens)
        web.reload_config()
    except ConfigError as error:
        return _refusal(409, str(error))
    return {"ok": True, "default_flags": list(web.config.resume.default_flags)}
