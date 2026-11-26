"""The hub's ingest endpoints (`vigil.web.ingest`): `PUT /ingest/{name}/sessions` and
`POST /ingest/{name}/heartbeat`, for `push` credentials only, each bound to one machine."""

import asyncio
import os
from collections.abc import Callable, Iterator, MutableMapping
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import (
    FULL,
    HUB_HOST,
    HUB_REMOTE_PEER,
    LOOPBACK_URL,
    PUSH_DESK,
    PUSH_GHOST,
    PUSH_LAPTOP,
    READ,
    VigilEnv,
    snapshot_body,
)
from vigil.config import load_config
from vigil.hub import MAX_SNAPSHOT_BYTES, HubStore, canonical_digest
from vigil.web.app import create_app

ClientFor = Callable[..., TestClient]
SESSIONS = "/ingest/laptop/sessions"
HEARTBEAT = "/ingest/laptop/heartbeat"
MIB = 1024 * 1024


def store(app: FastAPI) -> HubStore:
    hub = app.state.vigil.hub
    assert isinstance(hub, HubStore)
    return hub


def ids(app: FastAPI) -> list[str]:
    return [row.session_id for row in store(app).sessions()]


def test_ingest_valid_snapshot(hub_app: FastAPI, hub_client_for: ClientFor) -> None:
    client = hub_client_for(PUSH_LAPTOP)
    body = snapshot_body()
    response = client.put(SESSIONS, json=body)
    assert response.status_code == 200, response.text
    answer = response.json()
    assert answer["ok"] is True
    assert answer["received_at"].startswith("20")
    assert ids(hub_app) == [session["id"] for session in body["sessions"]]
    assert {row.machine for row in store(hub_app).sessions()} == {"laptop"}


def test_ingest_wrong_machine_for_credential(hub_app: FastAPI, hub_client_for: ClientFor) -> None:
    response = hub_client_for(PUSH_DESK).put(SESSIONS, json=snapshot_body())
    assert response.status_code == 403
    assert response.json() == {"detail": "needs scope push for laptop"}
    assert ids(hub_app) == []


def test_ingest_unknown_machine(hub_app: FastAPI, hub_client_for: ClientFor) -> None:
    client = hub_client_for(PUSH_GHOST)
    response = client.put("/ingest/ghost/sessions", json=snapshot_body("ghost"))
    assert response.status_code == 403
    assert response.json() == {"ok": False, "reason": "no machine called 'ghost' on this hub"}
    response = client.post("/ingest/ghost/heartbeat", json={"generated_at": "x", "digest": "0"})
    assert response.status_code == 403
    assert "no machine called 'ghost'" in response.json()["reason"]


def test_ingest_refused_for_read_and_full(hub_app: FastAPI, hub_client_for: ClientFor) -> None:
    for token in (READ, FULL):
        response = hub_client_for(token).put(SESSIONS, json=snapshot_body())
        assert response.status_code == 403, token
        assert response.json() == {"detail": "needs scope push for laptop"}
    local = TestClient(
        hub_app,
        base_url=LOOPBACK_URL,
        client=("127.0.0.1", 50000),
        headers={"Origin": LOOPBACK_URL},
    )
    with local:
        response = local.put(SESSIONS, json=snapshot_body())
        assert response.status_code == 403
        assert response.json() == {"detail": "needs scope push for laptop"}
        response = local.post(HEARTBEAT, json={})
        assert response.status_code == 403
    assert ids(hub_app) == []


def test_ingest_malformed_field(hub_app: FastAPI, hub_client_for: ClientFor) -> None:
    client = hub_client_for(PUSH_LAPTOP)
    good = snapshot_body()
    assert client.put(SESSIONS, json=good).status_code == 200
    bad = snapshot_body(sessions=[{**good["sessions"][0], "busy": "yes"}])
    response = client.put(SESSIONS, json=bad)
    assert response.status_code == 422
    answer = response.json()
    assert answer["ok"] is False
    assert answer["reason"].startswith("sessions[0].busy")
    assert ids(hub_app) == [session["id"] for session in good["sessions"]]


def test_ingest_extra_fields_accepted(hub_app: FastAPI, hub_client_for: ClientFor) -> None:
    body = snapshot_body()
    body["sessions"] = [{**session, "newer_field": [1, 2]} for session in body["sessions"]]
    body["newer_top_level"] = {"x": 1}
    response = hub_client_for(PUSH_LAPTOP).put(SESSIONS, json=body)
    assert response.status_code == 200, response.text
    assert len(ids(hub_app)) == 2


def call_asgi(
    app: FastAPI, path: str, headers: dict[str, str], chunks: Iterator[bytes]
) -> tuple[int, bytes]:
    """PUT `path` straight through the ASGI app, its body read from `chunks` only as the app
    asks for it (the test client reads a body whole before sending it)."""
    pulled = iter(chunks)
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "PUT",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": HUB_REMOTE_PEER,
        "server": (HUB_HOST, 80),
        "state": {},
    }
    status = 0
    body = bytearray()

    async def receive() -> dict[str, Any]:
        chunk = next(pulled, None)
        if chunk is None:
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.request", "body": chunk, "more_body": True}

    async def send(message: MutableMapping[str, Any]) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))

    asyncio.run(app(scope, receive, send))
    return status, bytes(body)


def test_ingest_oversized_chunked(hub_app: FastAPI) -> None:
    chunk = 64 * 1024
    total = 9 * MIB // chunk
    sent = 0

    def nine_mib() -> Iterator[bytes]:
        nonlocal sent
        for _ in range(total):
            sent += 1
            yield b" " * chunk

    headers = {"Host": HUB_HOST, "Authorization": f"Bearer {PUSH_LAPTOP}"}
    status, body = call_asgi(hub_app, SESSIONS, headers, nine_mib())
    assert status == 413
    assert body == b'{"ok":false,"reason":"snapshot larger than 8 MiB"}'
    # Reading stopped at the chunk that took it past 8 MiB: nothing after it was pulled.
    assert sent == MAX_SNAPSHOT_BYTES // chunk + 1
    assert sent < total
    assert ids(hub_app) == []


def test_ingest_oversized_content_length_refused_unread(hub_app: FastAPI) -> None:
    sent = 0

    def chunks() -> Iterator[bytes]:
        nonlocal sent
        sent += 1
        yield b"{}"

    headers = {
        "Host": HUB_HOST,
        "Authorization": f"Bearer {PUSH_LAPTOP}",
        "Content-Length": str(MAX_SNAPSHOT_BYTES + 1),
    }
    status, _ = call_asgi(hub_app, SESSIONS, headers, chunks())
    assert status == 413
    assert sent == 0


def test_ingest_exactly_the_limit_is_read(hub_app: FastAPI) -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(8):
            yield b" " * MIB

    headers = {"Host": HUB_HOST, "Authorization": f"Bearer {PUSH_LAPTOP}"}
    status, body = call_asgi(hub_app, SESSIONS, headers, chunks())
    assert status == 422  # read whole (not too large), then refused as not JSON
    assert b"not JSON" in body


def test_ingest_deeply_nested_is_422(hub_app: FastAPI, hub_client_for: ClientFor) -> None:
    client = hub_client_for(PUSH_LAPTOP)
    response = client.put(SESSIONS, content=b"[" * 100_000)
    assert response.status_code == 422
    assert response.json()["reason"].startswith("not JSON")
    nested = b'{"format": 1, "sessions": ' + b"[" * 100_000 + b"]" * 100_000 + b"}"
    assert client.put(SESSIONS, content=nested).status_code == 422


def test_ingest_too_many_sessions(hub_app: FastAPI, hub_client_for: ClientFor) -> None:
    one = snapshot_body()["sessions"][0]
    body = snapshot_body(sessions=[{**one, "id": f"s{number}"} for number in range(501)])
    response = hub_client_for(PUSH_LAPTOP).put(SESSIONS, json=body)
    assert response.status_code == 422
    assert "at most 500" in response.json()["reason"]
    assert ids(hub_app) == []


def test_ingest_atomic_on_failed_write(
    hub_app: FastAPI, hub_client_for: ClientFor, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = hub_client_for(PUSH_LAPTOP)
    first = snapshot_body()
    assert client.put(SESSIONS, json=first).status_code == 200

    def refuse(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", refuse)
    second = snapshot_body(sessions=first["sessions"][:1])
    response = client.put(SESSIONS, json=second)
    assert response.status_code == 500
    assert response.json() == {"ok": False, "reason": "could not store the snapshot"}
    assert ids(hub_app) == [session["id"] for session in first["sessions"]]
    heartbeat = {
        "generated_at": "2026-11-25T18:05:00Z",
        "digest": canonical_digest(first["sessions"]),
    }
    assert client.post(HEARTBEAT, json=heartbeat).status_code == 200


def test_heartbeat_matching_and_stale(hub_app: FastAPI, hub_client_for: ClientFor) -> None:
    client = hub_client_for(PUSH_LAPTOP)
    body = snapshot_body()
    digest = canonical_digest(body["sessions"])
    heartbeat = {"generated_at": "2026-11-25T18:05:00Z", "digest": digest}
    response = client.post(HEARTBEAT, json=heartbeat)
    assert response.status_code == 409
    assert response.json() == {"ok": False, "need_snapshot": True}
    assert client.put(SESSIONS, json=body).status_code == 200
    response = client.post(HEARTBEAT, json=heartbeat)
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    other = canonical_digest(body["sessions"][:1])
    response = client.post(HEARTBEAT, json={**heartbeat, "digest": other})
    assert response.status_code == 409
    assert response.json() == {"ok": False, "need_snapshot": True}


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"[",
        b"[]",
        b'{"generated_at": "2026-11-25T18:05:00Z"}',
        b'{"generated_at": "2026-11-25T18:05:00Z", "digest": "ABC"}',
        b'{"generated_at": "2026-11-25T18:05:00Z", "digest": "' + b"A" * 64 + b'"}',
        b'{"generated_at": "soon", "digest": "' + b"0" * 64 + b'"}',
        b'{"digest": "' + b"0" * 64 + b'"}',
    ],
)
def test_heartbeat_malformed_is_422(hub_client_for: ClientFor, body: bytes) -> None:
    response = hub_client_for(PUSH_LAPTOP).post(HEARTBEAT, content=body)
    assert response.status_code == 422
    assert response.json()["ok"] is False


def test_ingest_404_on_local_vigil(
    web_client: TestClient, vigil_env: VigilEnv, tmp_path: Path
) -> None:
    # From this machine the guard already refuses: nothing but a push credential may ingest.
    response = web_client.put(SESSIONS, json=snapshot_body())
    assert response.status_code == 403
    # A push credential on a vigil that is not a hub reaches the route, which is not there.
    token = tmp_path / "push-token"
    token.write_text(PUSH_LAPTOP + "\n")
    token.chmod(0o600)
    config = Path(os.environ["XDG_CONFIG_HOME"]) / "vigil" / "config.toml"
    config.write_text(
        f'claude_home = "{vigil_env.claude_home}"\n\n[server]\n'
        f'allowed_hosts = ["{HUB_HOST}"]\n\n'
        f'[[server.tokens]]\nfile = "{token}"\nscope = "push"\nmachine = "laptop"\n'
    )
    app = create_app(load_config(), start_watcher=False)
    with TestClient(app, base_url=f"http://{HUB_HOST}", client=HUB_REMOTE_PEER) as client:
        headers = {"Authorization": f"Bearer {PUSH_LAPTOP}"}
        response = client.put(SESSIONS, json=snapshot_body(), headers=headers)
        assert response.status_code == 404
        assert response.json() == {"detail": "only available on a hub"}
        response = client.post(HEARTBEAT, json={}, headers=headers)
        assert response.status_code == 404
