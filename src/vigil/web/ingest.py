"""A hub's ingest endpoints (`router`): machines push their sessions here.

- `PUT /ingest/{name}/sessions`: the machine's whole snapshot (`vigil.hub.parse_snapshot`),
  which replaces the one stored for it.
- `POST /ingest/{name}/heartbeat`: `{"generated_at", "digest"}`; the machine is seen when the
  stored snapshot has that digest, else it is asked for the snapshot (409).

The guard lets only a `push` credential bound to `{name}` this far (`vigil.web.security`), with
no `Origin` needed: these are not browser requests. `{name}` must also be one of the hub's
`[[machines]]` (403 otherwise). The body is read up to `MAX_SNAPSHOT_BYTES` and no further
(413), whatever `Content-Length` says. On a vigil that is not a hub both answer 404.
"""

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any, cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from vigil.hub import MAX_SNAPSHOT_BYTES, HubStore, SnapshotError, parse_snapshot
from vigil.index.db import to_db
from vigil.web import hub_only, state

router = APIRouter(tags=["ingest"], dependencies=[Depends(hub_only)])

HEARTBEAT_MAX_BYTES = 4096
"""Most a heartbeat's body may hold."""

_DIGEST = re.compile(r"[0-9a-f]{64}")

_log = logging.getLogger(__name__)


async def read_capped(request: Request, limit: int) -> bytes | None:
    """The request's body, or None when it is longer than `limit` bytes. A `Content-Length` over
    `limit` is refused before anything is read; otherwise the body is read as it arrives and
    reading stops as soon as it goes over `limit`, so a body without a length (chunked) cannot
    make the service hold more than `limit` plus one chunk."""
    length = request.headers.get("content-length")
    if length is not None and length.strip().isdigit() and int(length) > limit:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            return None
    return bytes(body)


def _answer(status: int, **body: object) -> JSONResponse:
    return JSONResponse(body, status_code=status)


def _refused(status: int, reason: str) -> JSONResponse:
    return _answer(status, ok=False, reason=reason)


def _store(request: Request, name: str) -> HubStore | JSONResponse:
    """The hub's store when `name` is one of its machines, else the 403 to answer with."""
    store = state(request).hub
    if store is None or not store.knows(name):
        return _refused(403, f"no machine called {name!r} on this hub")
    return store


_REFUSALS: dict[int | str, dict[str, Any]] = {
    403: {"description": "`name` is not one of the hub's `[[machines]]`"},
    413: {"description": "the body is larger than the limit"},
    422: {"description": "the body is malformed; `reason` names the first problem"},
}


@router.put("/ingest/{name}/sessions", responses={**_REFUSALS, 500: {}})
async def put_sessions(request: Request, name: str) -> JSONResponse:
    """Replace machine `name`'s stored snapshot with the one in the body. Answers
    `{"ok": true, "received_at": "<ISO>"}`, or `{"ok": false, "reason": "..."}` with 403, 413,
    422 or 500 (the snapshot could not be written; the previous one is kept)."""
    store = _store(request, name)
    if isinstance(store, JSONResponse):
        return store
    body = await read_capped(request, MAX_SNAPSHOT_BYTES)
    if body is None:
        return _refused(413, f"snapshot larger than {MAX_SNAPSHOT_BYTES // (1024 * 1024)} MiB")
    try:
        # Off the event loop: parsing and checking up to 8 MiB takes a moment.
        snapshot = await asyncio.to_thread(parse_snapshot, body, name)
    except SnapshotError as error:
        return _refused(422, str(error))
    try:
        received_at = await asyncio.to_thread(store.replace, snapshot)
    except OSError as error:
        _log.error("hub: could not store the snapshot of %s: %s", name, error)
        return _refused(500, "could not store the snapshot")
    return _answer(200, ok=True, received_at=to_db(received_at))


def _heartbeat_digest(body: bytes) -> str:
    """The digest of a heartbeat body. Raises SnapshotError naming the problem."""
    try:
        data = json.loads(body)
    except RecursionError, ValueError:
        raise SnapshotError("not JSON") from None
    if not isinstance(data, dict):
        raise SnapshotError("the body must be a JSON object")
    heartbeat = cast(dict[str, Any], data)
    generated_at = heartbeat.get("generated_at")
    try:
        datetime.fromisoformat(generated_at if isinstance(generated_at, str) else "")
    except ValueError:
        raise SnapshotError("generated_at: must be an ISO 8601 time") from None
    digest = heartbeat.get("digest")
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise SnapshotError("digest: must be 64 lowercase hex digits (SHA-256)")
    return digest


@router.post(
    "/ingest/{name}/heartbeat",
    responses={**_REFUSALS, 409: {"description": "the hub wants the whole snapshot"}},
)
async def heartbeat(request: Request, name: str) -> JSONResponse:
    """Machine `name` is up and its snapshot has not changed: `{"ok": true}` when the stored
    snapshot has `digest`, else 409 `{"ok": false, "need_snapshot": true}` (the machine then
    pushes it). 403, 413 and 422 as for the snapshot."""
    store = _store(request, name)
    if isinstance(store, JSONResponse):
        return store
    body = await read_capped(request, HEARTBEAT_MAX_BYTES)
    if body is None:
        return _refused(413, f"heartbeat larger than {HEARTBEAT_MAX_BYTES} bytes")
    try:
        digest = _heartbeat_digest(body)
    except SnapshotError as error:
        return _refused(422, str(error))
    # In a worker thread: a heartbeat may write when the machine was last seen.
    if await asyncio.to_thread(store.heartbeat, name, digest):
        return _answer(200, ok=True)
    return _answer(409, ok=False, need_snapshot=True)
