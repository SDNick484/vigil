"""Session reviews on the web (`router`): start one, show one, list them, act on a flag
suggestion (resume or start a new session with it, or add it to `resume.default_flags`), and apply
a project file suggestion after previewing it (`vigil.apply`).

The handlers are thin over `vigil.review.runner` and `vigil.review.store`. A review runs in a
background thread with its own index connection; the session page polls its fragment (every 2 s,
htmx) until it is done. One review runs at a time: the service's `ReviewSlot`, and the index for
a review that `vigil review` is running in another process.

Acting on a suggestion takes only the review's id and the suggestion's position from the client;
the flag is the stored one, checked with `vigil.flags.check` against the installed claude's
catalog before anything is launched or written, and a file change is planned again from the
stored suggestion and applied only if the file is still as previewed.

A peer's session (v2, `vigil.peers`) is reviewed by the peer: every endpoint here has a mirror
under `/peers/{name}/...` that forwards the ids (and an Apply's `before_hash`), nothing else, to the
peer's own endpoint with its token (`vigil.peers.forward`), and the peer checks and refuses exactly
as for a local request. The peer's HTML is never shown: the review and the change preview are read
from its JSON (`GET /api/v1/reviews/{rid}`, `GET /api/v1/reviews/{rid}/suggestions/{i}/change`),
checked and cleaned again here (`vigil.peers.review_from_dict`, `change_from_dict`), and rendered
with the templates here, with links to the mirrors.
"""

import functools
import json
import logging
import os
import shlex
import sqlite3
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from markupsafe import escape
from pydantic import BaseModel

from vigil.apply import (
    CHANGED,
    ApplyRefused,
    Change,
    apply_change,
    check_suggestion,
    diff_text,
    plan_change,
    project_folder,
    user_target_path,
)
from vigil.catalog import CatalogEntry, current_catalog, refresh_catalog
from vigil.config import ConfigError
from vigil.config_edit import add_default_flags
from vigil.flags import FlagRefused, check, parse_suggested
from vigil.index.queries import SessionRow, get_session, vigil_launch_flags
from vigil.paths import config_path
from vigil.peers import (
    CHANGE_TIMEOUT,
    HASH,
    PEER_TIMEOUT,
    RESUME_TIMEOUT,
    REVIEW_TIMEOUT,
    Answer,
    PeerReview,
    ResumeForwardError,
    change_from_dict,
    is_count,
    is_session_id,
    launch_answer,
    review_from_dict,
)
from vigil.remote import find_transcript
from vigil.resume import ResumeRefused, _check_cwd, plan_start_new, session_lock
from vigil.review.numbers import numbers_to_dict
from vigil.review.runner import (
    PendingReview,
    ReviewBusy,
    ReviewRefused,
    begin_review,
    check_reviewable,
    claim_slot,
    complete_review,
)
from vigil.review.schema import Kind, Suggestion, clean
from vigil.review.store import Review, get_review, list_reviews, review_to_dict
from vigil.web import WebState, connection, state
from vigil.web.api import (
    ForwardedResumeOut,
    RefusalOut,
    ResumeOut,
    _answer,
    _launch,
    _start,
    at_desk,
)
from vigil.web.pages import _token_numbers, templates

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


class ApplyOut(BaseModel):
    """`path`: the file written (its real path, after symlinks)."""

    ok: Literal[True]
    path: str


class PeerSuggestionOut(SuggestionOut):
    """`index`: the suggestion's position on the peer, which its actions take (a suggestion the
    peer sent that is not valid here is left out of the list)."""

    index: int


class PeerReviewOut(ReviewOut):
    """A review of a peer's session, as the peer sent it, checked and cleaned here."""

    suggestions: list[PeerSuggestionOut]  # type: ignore[assignment]


class SkippedOut(BaseModel):
    """A settings key left alone because it is already set; `current` is its value as JSON."""

    key: str
    current: str


class ChangeOut(BaseModel):
    """The change a file suggestion would make: the file (`new_file` when it does not exist
    yet), the unified diff, the keys left alone, and the hash Apply must send back."""

    target: str
    new_file: bool
    diff: str
    skipped: list[SkippedOut]
    before_hash: str


class ForwardedAlwaysUseOut(AlwaysUseOut):
    """Always use forwarded to a peer: `default_flags` are in `machine`'s config."""

    machine: str


class ForwardedApplyOut(ApplyOut):
    """Apply forwarded to a peer: `path` is on `machine`."""

    machine: str


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


def flag_card(suggestion: Suggestion, catalog: Sequence[CatalogEntry] | None) -> FlagCard:
    """The card of a flag suggestion: launchable when `vigil.flags.check` allows its flag for the
    installed claude (`catalog`; None for a peer's review, whose claude is not known here: the
    peer checks against its own), copy-only otherwise."""
    tokens = parse_suggested(suggestion.flag_name or "", suggestion.flag_value)
    try:
        check(tokens, catalog)
    except FlagRefused as refused:
        return FlagCard(shlex.join(["claude", *tokens]), refused.reason)
    return FlagCard(shlex.join(["claude", *tokens]), None)


templates.env.globals["flag_card"] = flag_card

_PROJECT_FILES = {
    "project_claude_md": "CLAUDE.md in the project folder",
    "project_settings": ".claude/settings.json in the project folder",
}


@dataclass(frozen=True)
class FileCard:
    """What a file suggestion's card offers (`_suggestion_actions.html`, `_file_copy.html`)."""

    path: str
    """Where the file is: under the Claude home for a user target, else which project file."""
    copy: str
    """The text to add: the CLAUDE.md text, or the settings as indented JSON."""
    refused: str | None
    """Why vigil will not apply it (then the card only offers `copy`); None for a project target
    it may apply (a preview can still refuse, `vigil.apply.plan_change`)."""


def file_card(suggestion: Suggestion, request: Request, peer: str | None = None) -> FileCard:
    """The card of a file suggestion: copy-only, with the reason, when
    `vigil.apply.check_suggestion` refuses it (a user target, shown with its path under the
    configured Claude home; settings vigil does not apply; invisible characters). For a review
    of `peer`'s session the path names that machine instead."""
    if suggestion.file_settings is not None:
        copy = json.dumps(suggestion.file_settings, indent=2, ensure_ascii=False)
    else:
        copy = suggestion.file_text or ""
    target = suggestion.file_target
    home = state(request).config.claude_home if peer is None else Path()
    user = user_target_path(target, home) if target else None
    path = str(user) if user is not None else _PROJECT_FILES.get(target or "", "")
    if peer is not None:
        path = f"{path} in the Claude home on {peer}" if user is not None else f"{path} on {peer}"
    try:
        check_suggestion(suggestion)
    except ApplyRefused as error:
        return FileCard(path, copy, error.reason)
    return FileCard(path, copy, None)


templates.env.globals["file_card"] = file_card


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
    session's) answers 286, which makes htmx stop polling, with a line saying so: when it is
    another session's review that is running (the page asks for it after a second press found
    one running), that it is."""
    with connection(request) as conn:
        review = get_review(conn, review_id)
        catalog = current_catalog(conn)
    if review is None:
        return _gone(review_id)
    if review.session_id != session_id:
        return _not_this_sessions(review)
    return templates.TemplateResponse(
        request, "_review.html", {"review": review, "catalog": catalog, "review_peer": None}
    )


def _not_this_sessions(review: Review) -> HTMLResponse:
    """The fragment for `review` asked for under another session: nothing of it is shown."""
    if review.status == "running":
        why = "A review of another session is running; try again when it has finished."
        return _gone(review.id, why)
    return _gone(review.id)


def _gone(review_id: int, why: str = "This review is no longer available.") -> HTMLResponse:
    """The fragment of a review that cannot be shown, saying `why`; 286 stops the polling."""
    return HTMLResponse(
        f'<article class="review" id="review-{review_id}"><p class="muted">{escape(why)}</p>'
        "</article>",
        status_code=STOP_POLLING,
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


@router.get("/api/v1/reviews/{review_id}", response_model=ReviewOut, tags=["api"])
def review_by_id(request: Request, review_id: int) -> dict[str, object]:
    """One review by its id; 404 when there is none (its session left the index, say)."""
    with connection(request) as conn:
        found = get_review(conn, review_id)
    if found is None:
        raise HTTPException(status_code=404, detail=f"no review {review_id}")
    return review_to_dict(found)


@router.get("/api/v1/sessions/{session_id}/numbers", response_model=dict[str, Any], tags=["api"])
def session_numbers(request: Request, session_id: str) -> dict[str, object]:
    """Where a session's tokens went (`vigil.review.numbers.numbers_to_dict`), read from its
    transcript now, as the session page shows them. 404 when there is no such session, or its
    transcript cannot be read or holds no usage."""
    with connection(request) as conn:
        row = get_session(conn, session_id)
        if row is None:
            raise HTTPException(status_code=404, detail=_no_session(session_id))
        flags = vigil_launch_flags(conn, row.session_id)
    found = _token_numbers(row, flags)
    if found is None:
        raise HTTPException(status_code=404, detail="no token numbers for this session")
    return numbers_to_dict(found)


def _suggestion(
    conn: sqlite3.Connection, review_id: int, index: int, kind: Kind
) -> tuple[SessionRow, Suggestion] | JSONResponse:
    """The session of review `review_id` and its suggestion `index`, which must be of `kind`; or
    the refusal to answer with (404 when there is no such suggestion or the session is not in
    the index, 409 when the suggestion is of another kind)."""
    review = get_review(conn, review_id)
    if review is None or not 0 <= index < len(review.suggestions):
        return _refusal(404, f"review {review_id} has no suggestion {index}")
    suggestion = review.suggestions[index]
    if suggestion.kind != kind:
        return _refusal(409, f"not a {kind} suggestion")
    row = get_session(conn, review.session_id)
    if row is None or row.session_id != review.session_id:
        return _refusal(404, _no_session(review.session_id))
    return row, suggestion


def _flag_suggestion(
    conn: sqlite3.Connection, review_id: int, index: int
) -> tuple[SessionRow, list[str]] | JSONResponse:
    """The session of review `review_id` and the flags of its suggestion `index`, checked against
    the installed claude's catalog; or the refusal to answer with (`_suggestion`'s, or 409 when
    its flag is refused)."""
    found = _suggestion(conn, review_id, index, "flag")
    if isinstance(found, JSONResponse):
        return found
    row, suggestion = found
    if not suggestion.flag_name:
        return _refusal(409, "not a flag suggestion")
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


def _plan(row: SessionRow, suggestion: Suggestion, claude_home: Path) -> Change:
    """The change `suggestion` makes in the project folder of session `row`. Raises ResumeRefused
    when the session's folder is gone, ApplyRefused when the change is refused."""
    folder = project_folder(_check_cwd(row.cwd))
    return plan_change(suggestion, folder, claude_home)


def _preview(
    request: Request, review_id: int, index: int
) -> tuple[Suggestion, Change | ApplyRefused] | JSONResponse:
    """File suggestion `index` of review `review_id`, and the change it would make or why vigil
    refuses it; or the refusal to answer with (`_suggestion`'s, or 409 when the session's folder
    is gone)."""
    web = state(request)
    with connection(request) as conn:
        found = _suggestion(conn, review_id, index, "file")
    if isinstance(found, JSONResponse):
        return found
    row, suggestion = found
    try:
        return suggestion, _plan(row, suggestion, web.config.claude_home)
    except ResumeRefused as refused:
        return _refusal(409, refused.reason)
    except ApplyRefused as refused:
        return suggestion, refused


def change_to_dict(change: Change) -> dict[str, object]:
    """`change` as `ChangeOut`: what the preview shows."""
    return {
        "target": str(change.target),
        "new_file": change.before is None,
        "diff": diff_text(change),
        "skipped": [
            {"key": key, "current": json.dumps(change.current[key], ensure_ascii=False)}
            for key in change.skipped
        ],
        "before_hash": change.before_hash,
    }


@router.get(
    "/reviews/{review_id}/suggestions/{index}/change",
    response_class=HTMLResponse,
    responses=_ACTION_RESPONSES,
)
def show_change(request: Request, review_id: int, index: int) -> Any:
    """The change file suggestion `index` would make, as an HTML fragment (`_file_change.html`):
    the diff, the settings keys left alone because they are already set, and an Apply form that
    carries the hash of the file as previewed. Nothing is written. A change vigil refuses answers
    200 with the text to copy and why. 404 when there is no such suggestion; 409 when it is not a
    file suggestion or the session's folder is gone."""
    previewed = _preview(request, review_id, index)
    if isinstance(previewed, JSONResponse):
        return previewed
    suggestion, change = previewed
    base = f"/reviews/{review_id}/suggestions/{index}"
    context: dict[str, Any] = {"base": base, "result_id": f"suggestion-result-{review_id}-{index}"}
    if isinstance(change, ApplyRefused):
        card = file_card(suggestion, request)
        context["card"] = FileCard(card.path, card.copy, change.reason)
    else:
        shown = change_to_dict(change)
        skipped = cast(list[dict[str, str]], shown.pop("skipped"))
        context.update(shown, skipped=[(item["key"], item["current"]) for item in skipped])
    return templates.TemplateResponse(request, "_file_change.html", context)


@router.get(
    "/api/v1/reviews/{review_id}/suggestions/{index}/change",
    response_model=ChangeOut,
    responses=_ACTION_RESPONSES,
    tags=["api"],
)
def change_preview(
    request: Request, review_id: int, index: int
) -> dict[str, object] | JSONResponse:
    """What `GET /reviews/{rid}/suggestions/{i}/change` shows, as JSON (a peer's mirror renders
    it). Nothing is written. 409 with the reason when vigil refuses the change (then the text is
    only to be copied), when it is not a file suggestion or the session's folder is gone; 404
    when there is no such suggestion."""
    previewed = _preview(request, review_id, index)
    if isinstance(previewed, JSONResponse):
        return previewed
    _, planned = previewed
    if isinstance(planned, ApplyRefused):
        return _refusal(409, planned.reason)
    return change_to_dict(planned)


@router.post(
    "/reviews/{review_id}/suggestions/{index}/apply",
    response_model=ApplyOut,
    responses=_ACTION_RESPONSES,
)
def apply_suggestion(
    request: Request, review_id: int, index: int, before_hash: Annotated[str, Form()] = ""
) -> dict[str, object] | JSONResponse:
    """Apply file suggestion `index` as `GET .../change` showed it (`vigil.apply.apply_change`).
    `before_hash` is the hash that preview carried: the change is planned again from the stored
    suggestion, and refused (409) when the file is no longer as previewed, or when the change is
    refused. 404 when there is no such suggestion."""
    web = state(request)
    with connection(request) as conn:
        found = _suggestion(conn, review_id, index, "file")
    if isinstance(found, JSONResponse):
        return found
    row, suggestion = found
    try:
        change = _plan(row, suggestion, web.config.claude_home)
        if change.before_hash != before_hash:
            raise ApplyRefused(CHANGED)
        apply_change(change, web.config.claude_home)
    except (ResumeRefused, ApplyRefused) as refused:
        return _refusal(409, refused.reason)
    return {"ok": True, "path": str(change.target)}


# Mirrors of the endpoints above for a peer's session (`/peers/{peer}/...`). Each forwards the ids
# (and an Apply's hash) to the peer with `vigil.peers.forward`; the peer's 404 and 409 refusals
# come back with its name before the reason, and 502 says when it cannot be reached or its answer
# cannot be used.

_PEER_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": RefusalOut},
    409: {"model": RefusalOut},
    502: {"model": RefusalOut},
}


def _forward(
    request: Request,
    peer: str,
    method: Literal["GET", "POST"],
    path: str,
    form: dict[str, str] | None = None,
    *,
    timeout: float,
) -> tuple[int, Answer]:
    return state(request).peers.forward(peer, method, path, form, timeout=timeout)


def _object(peer: str, status: int, answer: Answer, what: str) -> dict[str, Any] | JSONResponse:
    """A peer's 2xx `answer` when it is a JSON object with `ok` true; else the refusal to answer
    with: the peer's own (as `forward` made it), or 502 saying it did not answer with `what`."""
    if not 200 <= status < 300:
        return JSONResponse(answer, status_code=status)
    if not isinstance(answer, dict) or answer.get("ok") is not True:
        return _refusal(502, f"{peer}: answered something that is not {what}")
    return answer


def _suggestion_path(review_id: int, index: int, action: str) -> str:
    return f"/reviews/{review_id}/suggestions/{index}/{action}"


@router.post(
    "/peers/{peer}/sessions/{session_id}/review",
    status_code=202,
    response_model=ReviewStartedOut,
    responses=_PEER_RESPONSES,
)
def start_review_on_peer(
    request: Request, peer: str, session_id: str
) -> dict[str, object] | JSONResponse:
    """Start a review of a session of the peer called `peer`: forwarded to the peer's
    `POST /sessions/{id}/review`, which runs it there and answers 202 with its id at once (the
    peer's own). Answers as that does; 502 when the peer cannot be reached."""
    if not is_session_id(session_id):
        return _refusal(404, _no_session(session_id))
    path = f"/sessions/{quote(session_id, safe='')}/review"
    status, answer = _forward(request, peer, "POST", path, timeout=REVIEW_TIMEOUT)
    found = _object(peer, status, answer, "a review start")
    if isinstance(found, JSONResponse):
        return found
    review_id = found.get("review_id")
    if not is_count(review_id):
        return _refusal(502, f"{peer}: answered something that is not a review start")
    return {"ok": True, "review_id": review_id}


@router.get("/peers/{peer}/sessions/{session_id}/reviews/{review_id}", response_class=HTMLResponse)
def peer_review_fragment(request: Request, peer: str, session_id: str, review_id: int) -> Any:
    """A review of a session of the peer called `peer`, read from the peer's
    `GET /api/v1/reviews/{rid}` and rendered here (`_review.html`, its actions pointing at the
    mirrors), polling while it runs as the local fragment does. A review the peer no longer has
    answers 286, as does a peer that cannot be asked (saying why; reloading the page asks
    again). 404 when there is no peer called `peer`."""
    if state(request).peers.peer(peer) is None:
        return _refusal(404, f"no peer called {peer!r}")
    path = f"/api/v1/reviews/{review_id}"
    status, answer = _forward(request, peer, "GET", path, timeout=PEER_TIMEOUT)
    if status == 404:
        return _gone(review_id)
    if not 200 <= status < 300:
        reason = answer.get("reason") if isinstance(answer, dict) else None
        return _gone(review_id, f"{reason or peer}. Reload the page to try again.")
    found = review_from_dict(answer, peer)
    if found is None:
        return _gone(review_id, f"{peer}: answered something that is not a review.")
    if found.session_id != session_id:
        return _not_this_sessions(found)
    context = {"review": found, "catalog": None, "review_peer": peer}
    return templates.TemplateResponse(request, "_review.html", context)


def peer_review_to_dict(found: PeerReview) -> dict[str, object]:
    """A `vigil.peers.PeerReview` as `PeerReviewOut`: each suggestion with its peer's `index`."""
    data = review_to_dict(found)
    suggestions = cast(list[dict[str, object]], data["suggestions"])
    data["suggestions"] = [
        {**item, "index": index} for item, index in zip(suggestions, found.indices, strict=True)
    ]
    return data


@router.get(
    "/peers/{peer}/api/v1/sessions/{session_id}/reviews",
    response_model=list[PeerReviewOut],
    responses=_PEER_RESPONSES,
    tags=["api"],
)
def reviews_on_peer(
    request: Request, peer: str, session_id: str
) -> list[dict[str, object]] | JSONResponse:
    """The reviews of a session of the peer called `peer`, newest first, read from the peer's
    `GET /api/v1/sessions/{id}/reviews` and checked and cleaned here (a review or suggestion
    that is not valid is left out; each suggestion keeps the peer's `index`)."""
    if not is_session_id(session_id):
        return _refusal(404, _no_session(session_id))
    path = f"/api/v1/sessions/{quote(session_id, safe='')}/reviews"
    status, answer = _forward(request, peer, "GET", path, timeout=PEER_TIMEOUT)
    if not 200 <= status < 300:
        return JSONResponse(answer, status_code=status)
    if not isinstance(answer, list):
        return _refusal(502, f"{peer}: answered something that is not a list of reviews")
    found = (review_from_dict(item, peer) for item in answer)
    return [peer_review_to_dict(item) for item in found if item is not None]


def _launch_on_peer(
    request: Request, peer: str, review_id: int, index: int, action: str
) -> dict[str, object] | JSONResponse:
    path = _suggestion_path(review_id, index, action)
    status, answer = _forward(request, peer, "POST", path, timeout=RESUME_TIMEOUT)
    if not 200 <= status < 300:
        return JSONResponse(answer, status_code=status)  # the peer's refusal, as `forward` made it
    try:
        return at_desk(launch_answer(peer, status, answer))
    except ResumeForwardError as refused:
        return _refusal(refused.status, refused.reason)


@router.post(
    "/peers/{peer}/reviews/{review_id}/suggestions/{index}/resume",
    response_model=ForwardedResumeOut,
    responses=_PEER_RESPONSES,
)
def resume_with_suggestion_on_peer(
    request: Request, peer: str, review_id: int, index: int
) -> dict[str, object] | JSONResponse:
    """Resume with a flag suggestion of a review of the peer called `peer`: forwarded to the
    peer, which checks the flag against its own claude and resumes there. Answers as a forwarded
    resume does (`POST /peers/{peer}/sessions/{id}/resume`)."""
    return _launch_on_peer(request, peer, review_id, index, "resume")


@router.post(
    "/peers/{peer}/reviews/{review_id}/suggestions/{index}/start-new",
    response_model=ForwardedResumeOut,
    responses=_PEER_RESPONSES,
)
def start_new_with_suggestion_on_peer(
    request: Request, peer: str, review_id: int, index: int
) -> dict[str, object] | JSONResponse:
    """Start a new session with a flag suggestion of a review of the peer called `peer`, on that
    peer (forwarded). Answers as a forwarded resume does."""
    return _launch_on_peer(request, peer, review_id, index, "start-new")


@router.post(
    "/peers/{peer}/reviews/{review_id}/suggestions/{index}/always-use",
    response_model=ForwardedAlwaysUseOut,
    responses=_PEER_RESPONSES,
)
def always_use_suggestion_on_peer(
    request: Request, peer: str, review_id: int, index: int
) -> dict[str, object] | JSONResponse:
    """Add the flag of a suggestion of a review of the peer called `peer` to that peer's
    `resume.default_flags` (forwarded; this machine's config is not changed). Answers with the
    peer's flags and its name."""
    path = _suggestion_path(review_id, index, "always-use")
    status, answer = _forward(request, peer, "POST", path, timeout=REVIEW_TIMEOUT)
    found = _object(peer, status, answer, "a config change")
    if isinstance(found, JSONResponse):
        return found
    flags = found.get("default_flags")
    if not isinstance(flags, list) or not all(isinstance(flag, str) for flag in flags):
        return _refusal(502, f"{peer}: answered something that is not a config change")
    return {"ok": True, "default_flags": [clean(flag) for flag in flags], "machine": peer}


@router.get(
    "/peers/{peer}/reviews/{review_id}/suggestions/{index}/change",
    response_class=HTMLResponse,
    responses=_PEER_RESPONSES,
)
def show_change_on_peer(request: Request, peer: str, review_id: int, index: int) -> Any:
    """The change a file suggestion of a review of the peer called `peer` would make there, read
    from the peer's `GET /api/v1/reviews/{rid}/suggestions/{i}/change` (within `CHANGE_TIMEOUT`)
    and rendered here
    (`_file_change.html`, its Apply form posting to the mirror). The peer's refusal (a change it
    will not make, say) comes back as JSON with its reason."""
    path = f"/api/v1{_suggestion_path(review_id, index, 'change')}"
    status, answer = _forward(request, peer, "GET", path, timeout=CHANGE_TIMEOUT)
    if not 200 <= status < 300:
        return JSONResponse(answer, status_code=status)
    found = change_from_dict(answer)
    if found is None:
        return _refusal(502, f"{peer}: answered something that is not a change")
    context = {
        "base": f"/peers/{peer}/reviews/{review_id}/suggestions/{index}",
        "result_id": f"suggestion-result-{review_id}-{index}",
        "target": found.target,
        "new_file": found.new_file,
        "diff": found.diff,
        "skipped": found.skipped,
        "before_hash": found.before_hash,
    }
    return templates.TemplateResponse(request, "_file_change.html", context)


@router.post(
    "/peers/{peer}/reviews/{review_id}/suggestions/{index}/apply",
    response_model=ForwardedApplyOut,
    responses=_PEER_RESPONSES,
)
def apply_suggestion_on_peer(
    request: Request,
    peer: str,
    review_id: int,
    index: int,
    before_hash: Annotated[str, Form()] = "",
) -> dict[str, object] | JSONResponse:
    """Apply a file suggestion of a review of the peer called `peer` as its preview showed it:
    forwarded with `before_hash` (only that), and the peer plans the change again, checks the
    hash and writes the file. A `before_hash` that is not a hash is refused here (409, as the
    peer would) without asking the peer. Answers with the path written on the peer and its
    name."""
    if not HASH.fullmatch(before_hash):
        return _refusal(409, CHANGED)
    path = _suggestion_path(review_id, index, "apply")
    form = {"before_hash": before_hash}
    status, answer = _forward(request, peer, "POST", path, form, timeout=REVIEW_TIMEOUT)
    found = _object(peer, status, answer, "a written file")
    if isinstance(found, JSONResponse):
        return found
    written = found.get("path")
    if not isinstance(written, str):
        return _refusal(502, f"{peer}: answered something that is not a written file")
    return {"ok": True, "path": clean(written, 4096), "machine": peer}
