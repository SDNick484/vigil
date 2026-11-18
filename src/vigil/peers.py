"""Peers (v2): other machines' vigil services, whose sessions this one lists beside its own.

Each peer in the config (`[[peers]]`: `name`, `url`, `token_file`) runs vigil with remote access
on. This one pulls the peer's `GET /api/v1/sessions` (`fetch_peer_sessions`), shows the sessions
in a merged list with a machine column, and forwards Resume on a peer's session to that peer's
`POST /sessions/{id}/resume` (`forward_resume`), which starts it there and answers with the
attach command. Restart with Remote Control, Review and every action on a review's suggestions
are forwarded the same way (`forward`); the peer runs them and applies its own checks. What a
peer sends about a review is untrusted and is checked and cleaned here again
(`review_from_dict`, `numbers_from_peer`, `change_from_dict`).

How a peer's token is handled:

- It is read from the peer's own `token_file` (checked like `server.token_file`: a private file
  of the user, `vigil.web.auth.load_token`) each time it is needed, and sent in an
  `Authorization: Bearer` header to that peer's `url` and nowhere else. Every request is checked
  against the peer's origin before it is sent.
- Redirects are never followed, so a peer cannot pass its token on to another host.
- Proxy settings and `.netrc` from the environment are ignored (`trust_env=False`): requests go
  straight to the peer.
- It is never logged, cached or put in an error message.

A fetch gives up after `PEER_TIMEOUT` (3) seconds. The last sessions fetched from each peer are kept
in the data dir (`peers/<name>.json`, 0600, no token); when a peer cannot be reached its last-known
sessions are shown, marked stale.
"""

import json
import math
import os
import re
import secrets
import socket
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypeGuard, cast, get_args
from urllib.parse import quote

import httpx

from vigil.config import Config, Peer
from vigil.index.db import from_db, to_db
from vigil.index.queries import SessionRow, Status
from vigil.paths import data_dir
from vigil.review.numbers import TOP_TOOL_OUTPUTS, SessionNumbers, ToolOutput, TurnTokens
from vigil.review.schema import MAX_SUGGESTIONS, MAX_TITLE, Suggestion, clean, parse_output
from vigil.review.store import Review, ReviewStatus
from vigil.serialize import session_to_dict
from vigil.transcript import REMOTE_URL

PEER_TIMEOUT = 3.0
"""Seconds a sessions fetch may spend connecting, and waiting for each read; the whole answer
must also be in within this many seconds of starting (checked as it is read)."""

REVIEW_TIMEOUT = 30.0
"""Seconds a peer has to answer a forwarded review start (it answers at once, and runs the review
in the background), Always use or Apply."""

CHANGE_TIMEOUT = 10.0
"""Seconds a peer has to answer a forwarded change preview: it may ask git for the project folder
(up to 5 s) first."""

RESUME_TIMEOUT = 330.0
"""Seconds a peer has to answer a forwarded resume once connected: it waits for `claude --bg` or
tmux to start (up to 20 s), then for the Remote Control link (up to 300 s, the most
`resume.remote_link_timeout` allows); connecting still has `PEER_TIMEOUT`."""

FETCH_LIMIT = 10_000
"""Most sessions taken from one peer."""

MAX_ANSWER_BYTES = 32 * 1024 * 1024
"""Largest answer read from a peer."""

REFRESH_AFTER = 10.0
"""Seconds a fetched result is reused before the peer is asked again."""

RETRY_AFTER = 30.0
"""Seconds before an unreachable peer is tried again (its last-known sessions are shown
meanwhile), so an offline peer does not slow every page down."""

CACHE_DIR = "peers"
"""Where, in the data dir, the last-known sessions of each peer are kept."""

TEXT_LIMIT = 500
"""Longest transcript snippet kept from a peer (the index's own limit)."""

_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_STATUSES: frozenset[str] = frozenset(get_args(Status))
_SNIPPETS = ("first_prompt", "last_user", "last_assistant")
_REVIEW_STATUSES: frozenset[str] = frozenset(get_args(ReviewStatus))


class PeerError(Exception):
    """A peer could not be asked, or its answer could not be used; the message says why (and
    never holds the token)."""


@dataclass(frozen=True)
class PeerSession(SessionRow):
    """A session on a peer. `machine` is the peer's configured name."""

    peer: str = ""
    stale: bool = False
    """The peer could not be reached; this is its last-known state."""


@dataclass(frozen=True)
class PeerResult:
    """What a peer has: its sessions, newest activity first, when they were fetched (None when
    never), and why the last fetch failed (None when it worked). With an error, `sessions` are
    the last-known ones, marked stale."""

    sessions: list[PeerSession]
    fetched_at: datetime | None
    error: str | None = None

    @property
    def stale(self) -> bool:
        return self.error is not None


def local_machine(config: Config) -> str:
    """What the machine column calls this machine: `machine` from the config, else its host
    name."""
    return config.machine or socket.gethostname()


def _client(timeout: httpx.Timeout, transport: httpx.BaseTransport | None) -> httpx.Client:
    """A client that sends requests as given: no redirects followed, no proxies or `.netrc`
    from the environment, no default credentials."""
    return httpx.Client(
        transport=transport, timeout=timeout, follow_redirects=False, trust_env=False
    )


def _same_origin(url: httpx.URL, peer: Peer) -> bool:
    base = httpx.URL(peer.url)
    return (url.scheme, url.host, url.port) == (base.scheme, base.host, base.port)


def _request(
    client: httpx.Client, peer: Peer, method: str, path: str, **kwargs: Any
) -> httpx.Request:
    """A request to `path` on `peer`, carrying that peer's token. Raises PeerError when the
    token file cannot be used, or when the URL would not be on the peer's own origin."""
    # Imported here: vigil.web imports this module, so a top-level import would be circular.
    from vigil.web.auth import AuthError, load_token

    try:
        token = load_token(peer.token_file)
    except AuthError as error:
        raise PeerError(str(error)) from None
    request = client.build_request(
        method,
        peer.url + path,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        **kwargs,
    )
    if not _same_origin(request.url, peer):
        raise PeerError(f"refusing to send {peer.name}'s token to another host")
    return request


def _status_problem(response: httpx.Response) -> str:
    code = response.status_code
    if response.is_redirect:
        return f"answered {code} (a redirect, which is not followed)"
    if code == 401:
        return "refused the token (401): check this peer's token_file"
    if code in (400, 403):
        return f"refused the request ({code}): is this host in its server.allowed_hosts?"
    return f"answered HTTP {code}"


def _read_capped(response: httpx.Response, deadline: float, limit: float = PEER_TIMEOUT) -> bytes:
    """The body of `response`, refused when it is over `MAX_ANSWER_BYTES` or still coming in
    after `deadline` (`limit` seconds after the request started)."""
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > MAX_ANSWER_BYTES:
            raise PeerError(f"answer larger than {MAX_ANSWER_BYTES} bytes")
        if time.monotonic() > deadline:
            raise PeerError(f"timed out after {limit:g} s")
        chunks.append(chunk)
    return b"".join(chunks)


def _get_sessions(peer: Peer, transport: httpx.BaseTransport | None) -> list[dict[str, Any]]:
    deadline = time.monotonic() + PEER_TIMEOUT
    params = {"all": "true", "limit": str(FETCH_LIMIT)}
    try:
        with _client(httpx.Timeout(PEER_TIMEOUT), transport) as client:
            request = _request(client, peer, "GET", "/api/v1/sessions", params=params)
            response = client.send(request, stream=True)
            try:
                if response.status_code != 200:
                    raise PeerError(_status_problem(response))
                body = _read_capped(response, deadline)
            finally:
                response.close()
    except httpx.TimeoutException:
        raise PeerError(f"timed out after {PEER_TIMEOUT:g} s") from None
    except httpx.HTTPError as error:
        raise PeerError(f"unreachable: {error}") from None
    try:
        data = json.loads(body)
    except ValueError:
        raise PeerError("answered something that is not JSON") from None
    if not isinstance(data, list):
        raise PeerError("answered JSON that is not a list of sessions")
    return [item for item in data[:FETCH_LIMIT] if isinstance(item, dict)]


def _text(data: dict[str, Any], key: str, limit: int | None = None) -> str | None:
    value = data.get(key)
    if not isinstance(value, str):
        return None
    return value[:limit] if limit is not None else value


def _time(data: dict[str, Any], key: str) -> datetime | None:
    value = data.get(key)
    if not isinstance(value, str):
        return None
    try:
        at = from_db(value)
    except ValueError:
        return None
    return at.replace(tzinfo=UTC) if at is not None and at.tzinfo is None else at


def session_from_dict(
    data: dict[str, Any], peer: str, *, stale: bool = False
) -> PeerSession | None:
    """A session as a peer's API serves it (`vigil.serialize.session_to_dict`), or None when it
    has no usable id. Anything else malformed is dropped or defaulted; snippets are cut to
    `TEXT_LIMIT` characters. A stale session's status is `unknown`."""
    session_id = data.get("id")
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        return None
    count = data.get("message_count")
    models = data.get("models")
    status = data.get("status")
    remote_url = data.get("remote_url")
    if not isinstance(remote_url, str) or not REMOTE_URL.fullmatch(remote_url):
        remote_url = None
    open_url = data.get("open_url")
    if not isinstance(open_url, str) or not REMOTE_URL.fullmatch(open_url):
        open_url = None
    snippets = {key: _text(data, key, TEXT_LIMIT) for key in _SNIPPETS}
    return PeerSession(
        session_id=session_id,
        cwd=_text(data, "cwd"),
        git_branch=_text(data, "git_branch"),
        version=_text(data, "version"),
        title=_text(data, "title"),
        first_prompt=snippets["first_prompt"],
        last_user=snippets["last_user"],
        last_assistant=snippets["last_assistant"],
        started_at=_time(data, "started_at"),
        last_activity_at=_time(data, "last_activity_at"),
        message_count=count if isinstance(count, int) and not isinstance(count, bool) else 0,
        previous_session_id=_text(data, "previous_id"),
        machine=peer,
        project_key=_text(data, "project_key") or "",
        project_label=_text(data, "project_label") or _text(data, "project_key") or "",
        chain_id=_text(data, "chain_id") or session_id,
        transcript_path=_text(data, "transcript_path") or "",
        label=_text(data, "label"),
        pinned=data.get("pinned") is True,
        archived=data.get("archived") is True,
        note=_text(data, "note"),
        models=tuple(sorted(m for m in models if isinstance(m, str)))
        if isinstance(models, list)
        else (),
        status=cast(Status, status) if status in _STATUSES and not stale else "unknown",
        remote_url=remote_url,
        remote_url_at=_time(data, "remote_url_at") if remote_url else None,
        open_url=open_url,
        busy=data.get("busy") is True and not stale,
        peer=peer,
        stale=stale,
    )


def _sessions(items: Iterable[dict[str, Any]], peer: str, stale: bool) -> list[PeerSession]:
    sessions = (session_from_dict(item, peer, stale=stale) for item in items)
    return newest_first(session for session in sessions if session is not None)


def _cache_path(cache_dir: Path, peer: Peer) -> Path:
    return cache_dir / f"{peer.name}.json"


def _write_cache(cache_dir: Path, peer: Peer, sessions: list[PeerSession], at: datetime) -> None:
    """Keep `sessions` as `peer`'s last-known ones: written to a temporary file (0600) and moved
    into place. Holds the peer's URL (a cache for another URL is ignored), never its token."""
    data = {
        "url": peer.url,
        "fetched_at": to_db(at),
        "sessions": [session_to_dict(session) for session in sessions],
    }
    cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = _cache_path(cache_dir, peer)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_cache(cache_dir: Path, peer: Peer) -> tuple[list[dict[str, Any]], datetime | None]:
    """`peer`'s last-known sessions and when they were fetched; nothing when there is no usable
    cache for its current URL."""
    try:
        data = json.loads(_cache_path(cache_dir, peer).read_text(encoding="utf-8"))
    except OSError, ValueError:
        return [], None
    if not isinstance(data, dict) or data.get("url") != peer.url:
        return [], None
    sessions = data.get("sessions")
    items = (
        [item for item in sessions if isinstance(item, dict)] if isinstance(sessions, list) else []
    )
    return items, _time(data, "fetched_at")


def fetch_peer_sessions(
    peer: Peer,
    *,
    transport: httpx.BaseTransport | None = None,
    cache_dir: Path | None = None,
) -> PeerResult:
    """`peer`'s sessions (every one, archived and whole chains included), fetched now with a
    `PEER_TIMEOUT` limit and kept as its last-known sessions in `cache_dir` (default: `peers/`
    in the data dir). When the peer cannot be reached or its answer cannot be used, the
    last-known sessions are given instead, marked stale, with the reason in `error`.

    `transport` replaces the network (tests)."""
    cache_dir = cache_dir if cache_dir is not None else data_dir() / CACHE_DIR
    try:
        items = _get_sessions(peer, transport)
    except PeerError as error:
        cached, fetched_at = _read_cache(cache_dir, peer)
        return PeerResult(_sessions(cached, peer.name, True), fetched_at, str(error))
    now = datetime.now(UTC)
    sessions = _sessions(items, peer.name, False)
    try:
        _write_cache(cache_dir, peer, sessions, now)
    except OSError:
        pass  # the list still shows; only the offline fallback is missing
    return PeerResult(sessions, now, None)


class ResumeForwardError(Exception):
    """A forwarded resume did not go ahead: answer `status` with `reason` (user-facing)."""

    def __init__(self, status: int, reason: str, *, restartable: bool = False) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.restartable = restartable
        """The peer says restarting the session with Remote Control would get past a 409."""


def forward_resume(
    peer: Peer, session_id: str, *, transport: httpx.BaseTransport | None = None
) -> dict[str, object]:
    """Ask `peer` to resume its session `session_id` (`POST /sessions/{id}/resume` with the
    peer's token). The peer starts it there; asked from another machine it never opens a
    terminal, and answers with the attach command to run on it.

    Returns `{"ok": True, "attach", "opened_terminal", "remote_url", "pending", "machine"}`
    (`remote_url` is the peer's Remote Control link when it is a Claude one, else None; `pending`
    says it started the session but the link is not there yet), with the peer's `copy_of` (and
    `session_id`, when it has it) when it started a copy of the session, each only when it is a
    session id.
    Raises ResumeForwardError: 404 for an id that is not a session id, the peer's own 404 or 409
    refusal (reason prefixed with the peer's name), else 502 when the peer cannot be reached or
    refuses the request."""
    return _forward_launch(peer, session_id, "resume", None, transport)


def forward_restart(
    peer: Peer, session_id: str, *, transport: httpx.BaseTransport | None = None
) -> dict[str, object]:
    """Ask `peer` to stop its background session `session_id` and start it again with Remote
    Control (`POST /sessions/{id}/restart-remote`, with the confirmation the peer requires, and the
    peer's token). Answers and raises as `forward_resume` does."""
    return _forward_launch(peer, session_id, "restart-remote", {"confirm": "yes"}, transport)


Answer = dict[str, Any] | list[Any] | str
"""What `forward` returns besides the status: parsed JSON, or the text of an HTML answer."""


def _forward_launch(
    peer: Peer,
    session_id: str,
    action: str,
    form: dict[str, str] | None,
    transport: httpx.BaseTransport | None,
) -> dict[str, object]:
    """`forward_resume` and `forward_restart`: post `action` for `session_id` to `peer`."""
    if not is_session_id(session_id):
        raise ResumeForwardError(404, f"not a session id: {session_id!r}")
    path = f"/sessions/{quote(session_id, safe='')}/{action}"
    status, data = forward(peer, "POST", path, form, timeout=RESUME_TIMEOUT, transport=transport)
    return launch_answer(peer.name, status, data)


def launch_answer(name: str, status: int, data: Answer) -> dict[str, object]:
    """What `forward` got from the peer called `name` for something that launches a session (a
    resume, a restart, or a review suggestion's resume or new session), as `forward_resume`
    returns it. Raises ResumeForwardError with the peer's refusal, or 502 when the answer is not
    a launch's."""
    if not 200 <= status < 300:
        refusal = data if isinstance(data, dict) else {}
        reason = refusal.get("reason")
        raise ResumeForwardError(
            status,
            reason if isinstance(reason, str) else f"{name}: refused",
            restartable=status == 409 and refusal.get("restartable") is True,
        )
    attach = data.get("attach") if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get("ok") is not True or not isinstance(attach, str):
        raise ResumeForwardError(502, f"{name}: answered something that is not a resume")
    remote_url = data.get("remote_url")
    if not isinstance(remote_url, str) or not REMOTE_URL.fullmatch(remote_url):
        remote_url = None  # it goes into the user's browser: only ever a Claude link
    answer: dict[str, object] = {
        "ok": True,
        "attach": clean(attach),
        "opened_terminal": data.get("opened_terminal") is True,
        "remote_url": remote_url,
        "pending": data.get("pending") is True,
        "machine": name,
    }
    copy_of, session_id = data.get("copy_of"), data.get("session_id")
    if isinstance(copy_of, str) and is_session_id(copy_of):
        answer["copy_of"] = copy_of
        if isinstance(session_id, str) and is_session_id(session_id):
            answer["session_id"] = session_id
    return answer


_PASSED_ON = (400, 404, 409)
"""The refusals of a peer that are passed on as they are (with its name before the reason)."""


def _unusable(peer: Peer, problem: str) -> tuple[int, dict[str, Any]]:
    return 502, {"ok": False, "reason": f"{peer.name}: {problem}"}


def forward(
    peer: Peer,
    method: Literal["GET", "POST"],
    path: str,
    form: Mapping[str, str] | None = None,
    *,
    timeout: float,
    transport: httpx.BaseTransport | None = None,
) -> tuple[int, Answer]:
    """Send `method` `path` (with the form fields `form`) to `peer`, with its token, and return
    its status and answer. The peer has `timeout` seconds to answer in full (`PEER_TIMEOUT` to
    connect), in at most `MAX_ANSWER_BYTES`.

    A 2xx answer is returned as the peer sent it: parsed JSON (an object or a list), or the text
    of an HTML answer. A 400, 404 or 409 refusal with a reason (`reason`, or a 404's `detail`)
    comes back with that status as `{"ok": False, "reason"}`, the reason prefixed with the peer's
    name and cleaned (`vigil.review.schema.clean`), keeping only a boolean `restartable` and an
    integer `review_id` from the rest. Anything else (unreachable, timed out, a redirect, a
    refused token, an answer that is not JSON) is 502 with the reason. Nothing is raised."""
    deadline = time.monotonic() + timeout
    data = dict(form) if form is not None else None
    try:
        with _client(httpx.Timeout(timeout, connect=PEER_TIMEOUT), transport) as client:
            response = client.send(_request(client, peer, method, path, data=data), stream=True)
            try:
                body = _read_capped(response, deadline, timeout)
            finally:
                response.close()
    except PeerError as error:
        return _unusable(peer, str(error))
    except httpx.TimeoutException:
        return _unusable(peer, "timed out")
    except httpx.HTTPError as error:
        return _unusable(peer, f"unreachable: {error}")
    status = response.status_code
    try:
        parsed: Any = json.loads(body)
    except ValueError, RecursionError:
        parsed = None
    if 200 <= status < 300:
        if isinstance(parsed, dict | list):
            return status, parsed
        if response.headers.get("content-type", "").startswith("text/html"):
            return status, body.decode("utf-8", errors="replace")
        return _unusable(peer, "answered something that is not JSON")
    if status in _PASSED_ON and isinstance(parsed, dict):
        reason = parsed.get("reason")
        if not isinstance(reason, str) and status == 404:
            reason = parsed.get("detail")
        if isinstance(reason, str):
            refusal: dict[str, Any] = {"ok": False, "reason": f"{peer.name}: {clean(reason)}"}
            if isinstance(parsed.get("restartable"), bool):
                refusal["restartable"] = parsed["restartable"]
            if is_count(parsed.get("review_id")):
                refusal["review_id"] = parsed["review_id"]
            return status, refusal
    return _unusable(peer, _status_problem(response))


def is_session_id(value: str) -> bool:
    """Whether `value` can be a session id, to put in a path sent to a peer."""
    return _SESSION_ID.fullmatch(value) is not None


@dataclass(frozen=True)
class PeerReview(Review):
    """A review of a peer's session, as the peer sent it (`review_from_dict`)."""

    indices: tuple[int, ...] = ()
    """The peer's position of each of `suggestions`: a suggestion the peer sent that is not valid
    here is left out, and acting on the others names them by the peer's position."""


def _number(value: Any) -> float | None:
    """`value` as a float when it is a finite number (not a bool) of at least 0; None otherwise,
    an integer too large for a float included."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


MAX_INT = 2**63
"""Counts and ids from a peer must be below this (and at least 0)."""


def is_count(value: Any) -> TypeGuard[int]:
    """Whether `value` (from a peer) is an integer, not a bool, from 0 to below `MAX_INT`."""
    return type(value) is int and 0 <= value < MAX_INT


def _count(value: Any) -> int:
    """`value` when `is_count`; raises ValueError otherwise."""
    if not is_count(value):
        raise ValueError("not a count")
    return value


def _turn(data: Any) -> TurnTokens:
    if not isinstance(data, dict):
        raise ValueError("not a turn")
    fields = ("turn", "input", "output", "cache_write", "cache_read")
    return TurnTokens(*(_count(data.get(key)) for key in fields))


def _texts(value: Any) -> list[str]:
    return (
        [clean(item) for item in value if isinstance(item, str)] if isinstance(value, list) else []
    )


def numbers_from_peer(data: Any) -> SessionNumbers | None:
    """A session's numbers as a peer serves them (`vigil.review.numbers.numbers_to_dict`), or
    None when they are not valid: counts must be integers, texts are cleaned
    (`vigil.review.schema.clean`)."""
    if not isinstance(data, dict):
        return None
    try:
        turns, outputs = data.get("turns"), data.get("largest_tool_outputs")
        if not isinstance(turns, list) or not isinstance(outputs, list):
            return None
        share = _number(data.get("cache_hit_share"))
        if share is None or share > 1:
            return None
        return SessionNumbers(
            turns=[_turn(item) for item in turns],
            totals=_turn(data.get("totals")),
            cache_hit_share=share,
            largest_tool_outputs=[
                ToolOutput(clean(item["tool"], MAX_TITLE), _count(item.get("size")))
                for item in outputs[:TOP_TOOL_OUTPUTS]
                if isinstance(item, dict) and isinstance(item.get("tool"), str)
            ],
            compactions=_count(data.get("compactions")),
            models=_texts(data.get("models")),
            started_at=_time(data, "started_at"),
            ended_at=_time(data, "ended_at"),
            launch_flags=_texts(data.get("launch_flags")),
        )
    except ValueError:
        return None


def _peer_suggestion(item: Any) -> Suggestion | None:
    """A suggestion as a peer serves it (flat, `dataclasses.asdict`), checked and cleaned as
    Claude's answer is (`vigil.review.schema.parse_output`); None when it is not valid."""
    if not isinstance(item, dict):
        return None
    answer = {
        "kind": item.get("kind"),
        "title": item.get("title"),
        "why": item.get("why"),
        "evidence": item.get("evidence"),
        "flag": {"name": item.get("flag_name"), "value": item.get("flag_value")},
        "file": {
            "target": item.get("file_target"),
            "text": item.get("file_text"),
            "settings": item.get("file_settings"),
        },
    }
    found, _ = parse_output({"suggestions": [answer]})
    return found[0] if found else None


def _optional_text(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    return clean(value) if isinstance(value, str) else None


def review_from_dict(data: Any, peer: str) -> PeerReview | None:
    """A review as a peer serves it (`vigil.review.store.review_to_dict`), or None when it has no
    usable id, session id, creation time or status. It is untrusted: its suggestions and advice
    are checked and cleaned as Claude's answer is (an invalid one is left out; at most
    `MAX_SUGGESTIONS` of the peer's first positions are looked at), other texts are cleaned, and
    `machine` is the peer's configured name."""
    if not isinstance(data, dict):
        return None
    review_id, session_id, status = data.get("id"), data.get("session_id"), data.get("status")
    created_at = _time(data, "created_at")
    if not is_count(review_id) or not isinstance(session_id, str):
        return None
    if not is_session_id(session_id) or status not in _REVIEW_STATUSES or created_at is None:
        return None
    items = data.get("suggestions")
    found = [
        (index, _peer_suggestion(item))
        for index, item in enumerate(items[:MAX_SUGGESTIONS] if isinstance(items, list) else [])
    ]
    kept = [(index, suggestion) for index, suggestion in found if suggestion is not None]
    _, advice = parse_output({"token_advice": data.get("advice")})
    numbers = data.get("numbers")
    return PeerReview(
        id=review_id,
        session_id=session_id,
        machine=peer,
        created_at=created_at,
        finished_at=_time(data, "finished_at"),
        claude_version=_optional_text(data, "claude_version"),
        model=_optional_text(data, "model"),
        cost_usd=_number(data.get("cost_usd")),
        status=cast(ReviewStatus, status),
        numbers=numbers_from_peer(numbers) if numbers is not None else None,
        suggestions=[suggestion for _, suggestion in kept],
        advice=advice,
        error=_optional_text(data, "error"),
        indices=tuple(index for index, _ in kept),
    )


HASH = re.compile(r"[0-9a-f]{64}")
"""A `vigil.apply.file_hash`, as a preview carries it."""

MAX_DIFF = 1_000_000
"""Most characters of a peer's diff shown."""

_DIFF_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


@dataclass(frozen=True)
class PeerChange:
    """The change a file suggestion would make on a peer, as it previewed it."""

    target: str
    new_file: bool
    diff: str
    skipped: list[tuple[str, str]]
    """Settings keys left alone, each with its current value as JSON text."""
    before_hash: str


def change_from_dict(data: Any) -> PeerChange | None:
    """A preview as a peer serves it (`GET /api/v1/reviews/{rid}/suggestions/{i}/change`), or
    None when it is not one. Texts lose their control characters (the diff keeps tabs and
    newlines) and are cut; the hash must be a hash."""
    if not isinstance(data, dict):
        return None
    target, diff, before_hash = data.get("target"), data.get("diff"), data.get("before_hash")
    skipped = data.get("skipped")
    if not isinstance(target, str) or not isinstance(diff, str) or not isinstance(skipped, list):
        return None
    if not isinstance(before_hash, str) or not HASH.fullmatch(before_hash):
        return None
    return PeerChange(
        target=clean(target, 4096),
        new_file=data.get("new_file") is True,
        diff=_DIFF_CONTROL.sub("", diff)[:MAX_DIFF],
        skipped=[
            (clean(item["key"]), clean(item["current"]))
            for item in skipped
            if isinstance(item, dict)
            and isinstance(item.get("key"), str)
            and isinstance(item.get("current"), str)
        ],
        before_hash=before_hash,
    )


class PeerHub:
    """The configured peers and their latest results, shared by the service's handlers. A result
    is reused for `refresh_after` seconds (`retry_after` after a failure); `results` fetches the
    rest, all peers at once. Safe to use from several threads."""

    def __init__(
        self,
        peers: Sequence[Peer],
        *,
        cache_dir: Path | None = None,
        transport: httpx.BaseTransport | None = None,
        refresh_after: float = REFRESH_AFTER,
        retry_after: float = RETRY_AFTER,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.peers = tuple(peers)
        self.cache_dir = cache_dir
        self.transport = transport
        self.refresh_after = refresh_after
        self.retry_after = retry_after
        self._clock = clock
        self._lock = threading.Lock()
        self._results: dict[str, tuple[float, PeerResult]] = {}

    def peer(self, name: str) -> Peer | None:
        return next((peer for peer in self.peers if peer.name == name), None)

    def _due(self, name: str, now: float) -> bool:
        held = self._results.get(name)
        if held is None:
            return True
        at, result = held
        return now - at >= (self.retry_after if result.stale else self.refresh_after)

    def results(self) -> dict[str, PeerResult]:
        """Every peer's result, by name, in config order."""
        with self._lock:
            now = self._clock()
            due = [peer for peer in self.peers if self._due(peer.name, now)]
            if due:
                with ThreadPoolExecutor(max_workers=len(due)) as pool:
                    fetched = pool.map(
                        lambda peer: fetch_peer_sessions(
                            peer, transport=self.transport, cache_dir=self.cache_dir
                        ),
                        due,
                    )
                    for peer, result in zip(due, fetched, strict=True):
                        self._results[peer.name] = (self._clock(), result)
            return {peer.name: self._results[peer.name][1] for peer in self.peers}

    def forget(self, name: str | None = None) -> None:
        """Drop the held result of peer `name` (every peer's when None), so it is asked again."""
        with self._lock:
            if name is None:
                self._results.clear()
            else:
                self._results.pop(name, None)

    def resume(self, name: str, session_id: str) -> dict[str, object]:
        """`forward_resume` to the peer called `name`; 404 (ResumeForwardError) when there is
        none. The peer's sessions are fetched again next time, for their new status."""
        peer = self._peer_or_404(name)
        try:
            return forward_resume(peer, session_id, transport=self.transport)
        finally:
            self.forget(name)

    def restart(self, name: str, session_id: str) -> dict[str, object]:
        """`forward_restart` to the peer called `name`, as `resume` does."""
        peer = self._peer_or_404(name)
        try:
            return forward_restart(peer, session_id, transport=self.transport)
        finally:
            self.forget(name)

    def forward(
        self,
        name: str,
        method: Literal["GET", "POST"],
        path: str,
        form: Mapping[str, str] | None = None,
        *,
        timeout: float,
    ) -> tuple[int, Answer]:
        """`forward` to the peer called `name`; 404 when there is none. After a POST the peer's
        sessions are fetched again next time, for their new status."""
        peer = self.peer(name)
        if peer is None:
            return 404, {"ok": False, "reason": f"no peer called {name!r}"}
        try:
            return forward(peer, method, path, form, timeout=timeout, transport=self.transport)
        finally:
            if method == "POST":
                self.forget(name)

    def _peer_or_404(self, name: str) -> Peer:
        peer = self.peer(name)
        if peer is None:
            raise ResumeForwardError(404, f"no peer called {name!r}")
        return peer


def _sort_key(row: SessionRow) -> tuple[bool, float, str]:
    at = row.last_activity_at
    return (at is None, -at.timestamp() if at is not None else 0.0, row.session_id)


def newest_first[R: SessionRow](rows: Iterable[R]) -> list[R]:
    """`rows` by newest activity first (no activity last), as the index orders them."""
    return sorted(rows, key=_sort_key)


def _matches(row: SessionRow, words: list[str]) -> bool:
    fields = [row.title, row.label, row.note, row.first_prompt, row.cwd]
    text = "\x1f".join(field.lower() for field in fields if field)
    return all(word in text for word in words)


def filter_sessions(
    rows: Sequence[PeerSession],
    *,
    query: str | None = None,
    project_key: str | None = None,
    include_archived: bool = False,
    collapse_chains: bool = True,
) -> list[PeerSession]:
    """A peer's sessions (newest first) filtered as `vigil.index.queries.list_sessions` filters
    the index: every word of `query` in the title, label, note, first prompt or folder
    (case-insensitive), `project_key`, archived ones only with `include_archived`, and with
    `collapse_chains` one per chain that has a match: its newest visible session."""
    words = [word.lower() for word in (query or "").split()]
    visible = [row for row in rows if include_archived or not row.archived]
    hits = [
        row
        for row in visible
        if _matches(row, words) and (project_key is None or row.project_key == project_key)
    ]
    if not collapse_chains:
        return hits
    chains = {row.chain_id for row in hits}
    seen: set[str] = set()
    result: list[PeerSession] = []
    for row in visible:
        if row.chain_id in chains and row.chain_id not in seen:
            seen.add(row.chain_id)
            result.append(row)
    return result


def with_machine(rows: Iterable[SessionRow], machine: str) -> list[SessionRow]:
    """Local `rows` labelled with this machine's name for the machine column."""
    return [replace(row, machine=machine) for row in rows]
