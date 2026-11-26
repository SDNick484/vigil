"""A hub's read API (`mode = "hub"`): `/api/v1/sessions`, `/api/v1/projects` and
`/api/v1/machines` serve what the machines pushed to `PUT /ingest/{name}/sessions`."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import (
    FULL,
    PUSH_DESK,
    PUSH_LAPTOP,
    READ,
    HubEnv,
    snapshot_body,
)
from vigil.config import load_config
from vigil.hub import HubStore, canonical_digest
from vigil.web.app import create_app

ClientFor = Callable[..., TestClient]
LAPTOP_URL = "http://laptop.example.com:7787"


def push(
    client_for: ClientFor,
    machine: str,
    token: str,
    times: tuple[str, str],
    **changes: dict[str, Any],
) -> list[dict[str, Any]]:
    """Push `machine`'s two placeholder sessions with the given activity times; `changes` maps
    a session's position (`s0`, `s1`) to fields to set on it. Returns the sessions pushed."""
    body = snapshot_body(machine)
    for number, (session, at) in enumerate(zip(body["sessions"], times, strict=True)):
        session["last_activity_at"] = at
        session.update(changes.get(f"s{number}", {}))
    response = client_for(token).put(f"/ingest/{machine}/sessions", json=body)
    assert response.status_code == 200, response.text
    return body["sessions"]  # type: ignore[no-any-return]


def fill(client_for: ClientFor) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Both machines' sessions, interleaved by activity: laptop 18:00 and 17:50, desk 17:55
    and 17:45 (UTC, 2026-11-25). Desk's are in another project and folder."""
    laptop = push(
        client_for, "laptop", PUSH_LAPTOP, ("2026-11-25T18:00:00Z", "2026-11-25T17:50:00Z")
    )
    desk = push(
        client_for,
        "desk",
        PUSH_DESK,
        ("2026-11-25T17:55:00Z", "2026-11-25T17:45:00Z"),
        s0={
            "title": "desk work",
            "project_key": "/home/user/src/other",
            "project_label": "other",
            "cwd": "/home/user/src/other",
            "status": "running",
        },
        s1={"title": "desk notes", "project_key": "/home/user/src/other"},
    )
    return laptop, desk


def listing(
    client: TestClient, *, headers: dict[str, str] | None = None, **params: Any
) -> list[dict[str, Any]]:
    response = client.get("/api/v1/sessions", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


def test_lists_across_machines_newest_first(hub_client_for: ClientFor) -> None:
    laptop, desk = fill(hub_client_for)
    items = listing(hub_client_for(READ))
    assert [(item["machine"], item["id"]) for item in items] == [
        ("laptop", laptop[0]["id"]),
        ("desk", desk[0]["id"]),
        ("laptop", laptop[1]["id"]),
        ("desk", desk[1]["id"]),
    ]
    assert {"stale", "received_at", "resume_url", "machine_url"} <= set(items[0])
    assert items[0]["received_at"] is not None


def test_filters_across_machines(hub_client_for: ClientFor) -> None:
    laptop, desk = fill(hub_client_for)
    client = hub_client_for(READ)
    assert [i["id"] for i in listing(client, q="desk")] == [desk[0]["id"], desk[1]["id"]]
    assert [i["id"] for i in listing(client, q="placeholder desk")] == []
    other = listing(client, project="/home/user/src/other")
    assert {i["machine"] for i in other} == {"desk"}
    assert [i["id"] for i in listing(client, cwd="/home/user/src/other")] == [desk[0]["id"]]
    assert [i["id"] for i in listing(client, since="2026-11-25T17:52:00Z")] == [
        laptop[0]["id"],
        desk[0]["id"],
    ]
    assert [i["id"] for i in listing(client, status="running")] == [desk[0]["id"]]
    assert [i["id"] for i in listing(client, limit=1)] == [laptop[0]["id"]]


def test_stale_rewrite(hub_app: FastAPI, hub_client_for: ClientFor) -> None:
    link = "https://claude.ai/code/session_PLACEHOLDER"
    laptop, desk = fill(hub_client_for)
    # Both sessions of each machine are live with a link, as pushed.
    for machine, token, sessions in (("laptop", PUSH_LAPTOP, laptop), ("desk", PUSH_DESK, desk)):
        for session in sessions:
            session.update(status="running", busy=True, remote_url=link, open_url=link)
        body = snapshot_body(machine, sessions)
        assert (
            hub_client_for(token).put(f"/ingest/{machine}/sessions", json=body).status_code == 200
        )
    store = hub_app.state.vigil.hub
    assert isinstance(store, HubStore)
    pushed = datetime.now(UTC)
    # Laptop heartbeats 181 s on, desk does not: only desk is past `stale_after` (180).
    store._clock = lambda: pushed + timedelta(seconds=100)
    digest = canonical_digest(laptop)
    assert store.heartbeat("laptop", digest)
    store._clock = lambda: pushed + timedelta(seconds=181)
    items = listing(hub_client_for(READ))
    by_machine: dict[str, list[dict[str, Any]]] = {"laptop": [], "desk": []}
    for item in items:
        by_machine[item["machine"]].append(item)
    for item in by_machine["desk"]:
        assert item["stale"] is True
        assert item["open_url"] is None
        assert item["busy"] is False
        assert item["status"] == "unknown"
        assert item["remote_url"] == link
    for item in by_machine["laptop"]:
        assert item["stale"] is False
        assert item["open_url"] == link
        assert item["busy"] is True
        assert item["status"] == "running"


def test_resume_and_machine_url(hub_client_for: ClientFor) -> None:
    laptop, desk = fill(hub_client_for)
    items = {item["id"]: item for item in listing(hub_client_for(READ))}
    first = items[laptop[0]["id"]]
    assert first["resume_url"] == f"https://hub.example.com/machines/laptop/sessions/{first['id']}"
    assert first["machine_url"] == LAPTOP_URL
    assert items[desk[0]["id"]]["machine_url"] is None
    assert items[desk[0]["id"]]["resume_url"].startswith("https://hub.example.com/machines/desk/")


def test_no_public_url_no_resume_url(hub_env: HubEnv) -> None:
    text = hub_env.config_path.read_text().replace('public_url = "https://hub.example.com"\n', "")
    hub_env.config_path.write_text(text)
    app = create_app(load_config(hub_env.config_path))
    assert app.state.vigil.config.hub_mode.public_url is None
    with TestClient(app, base_url="http://hub.example.com", client=("192.0.2.10", 50000)) as client:
        push_headers = {"Authorization": f"Bearer {PUSH_LAPTOP}"}
        put = client.put("/ingest/laptop/sessions", json=snapshot_body(), headers=push_headers)
        assert put.status_code == 200
        items = listing(client, headers={"Authorization": f"Bearer {READ}"})
    assert items and all(item["resume_url"] is None for item in items)


def test_session_detail_on_hub(hub_client_for: ClientFor) -> None:
    laptop, _ = fill(hub_client_for)
    client = hub_client_for(READ)
    ident = laptop[0]["id"]
    for query in (ident, ident[:12]):
        response = client.get(f"/api/v1/sessions/{query}")
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["id"] == ident
        assert [link["id"] for link in data["chain"]] == [ident]
        assert data["resume_command"].startswith("cd ")
        assert data["resume_command"].endswith(f"claude --resume {ident}")
    assert client.get("/api/v1/sessions/nothing-like-this").status_code == 404


def test_projects_on_hub(hub_client_for: ClientFor) -> None:
    fill(hub_client_for)
    response = hub_client_for(READ).get("/api/v1/projects")
    assert response.status_code == 200
    assert response.json() == [
        {
            "project_key": "/home/user/src/app",
            "label": "app",
            "session_count": 2,
            "last_activity_at": "2026-11-25T18:00:00.000000+00:00",
        },
        {
            "project_key": "/home/user/src/other",
            "label": "other",
            "session_count": 2,
            "last_activity_at": "2026-11-25T17:55:00.000000+00:00",
        },
    ]


def test_peers_empty_on_hub(hub_client_for: ClientFor) -> None:
    assert hub_client_for(READ).get("/api/v1/peers").json() == []


def test_machines_endpoint(hub_client_for: ClientFor) -> None:
    body = snapshot_body()
    body["live_checked_at"] = "2026-11-25T18:03:00Z"
    laptop = hub_client_for(PUSH_LAPTOP)
    assert laptop.put("/ingest/laptop/sessions", json=body).status_code == 200
    digest = canonical_digest(body["sessions"])
    beat = {"generated_at": "2026-11-25T18:05:00Z", "digest": digest}
    assert laptop.post("/ingest/laptop/heartbeat", json=beat).status_code == 200
    response = hub_client_for(READ).get("/api/v1/machines")
    assert response.status_code == 200
    first, second = response.json()
    assert first["name"] == "laptop"
    assert first["url"] == LAPTOP_URL
    assert first["last_snapshot_at"] is not None
    assert first["last_seen_at"] >= first["last_snapshot_at"]
    assert first["live_checked_at"] == "2026-11-25T18:03:00.000000+00:00"
    assert first["session_count"] == 2
    assert first["stale"] is False
    assert second == {
        "name": "desk",
        "url": None,
        "last_snapshot_at": None,
        "last_seen_at": None,
        "live_checked_at": None,
        "stale": True,
        "session_count": 0,
    }


def test_health_in_hub_mode(hub_env: HubEnv, hub_client_for: ClientFor) -> None:
    fill(hub_client_for)
    response = hub_client_for(READ).get("/api/v1/health")
    assert response.status_code == 200
    health = response.json()
    assert health["mode"] == "hub"
    assert health["indexed_sessions"] == 4
    assert health["claude_version"] is None
    assert health["last_scan_at"] is None
    assert hub_env.bin.calls("claude") == []


def test_full_token_reads_listing(hub_client_for: ClientFor) -> None:
    fill(hub_client_for)
    assert len(listing(hub_client_for(FULL))) == 4
