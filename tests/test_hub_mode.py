"""Hub mode (`mode = "hub"`): starts and serves without `claude`, a Claude home or an index, and
answers 404 to everything that would run `claude` or needs the index's sessions."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import FULL, HUB_HOST, LOOPBACK_URL, HubEnv
from tests.test_hub import encode, placeholder_session, snapshot
from vigil.config import load_config
from vigil.hub import HUB_DIR, HubStore, parse_snapshot
from vigil.web import WebState

LOCAL_PEER = ("127.0.0.1", 50000)
REMOTE_PEER = ("192.0.2.10", 50000)  # TEST-NET-1: never a real client


def local(app: FastAPI) -> TestClient:
    """A client on this machine, as a vigil page in the browser."""
    return TestClient(
        app, base_url=LOOPBACK_URL, client=LOCAL_PEER, headers={"Origin": LOOPBACK_URL}
    )


def test_hub_starts_without_claude_or_home(hub_env: HubEnv, hub_app: FastAPI) -> None:
    with local(hub_app) as client:
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["claude_version"] is None
        assert body["indexed_sessions"] == 0
        assert body["last_scan_at"] is None
        assert client.get("/api/v1/peers").json() == []
    assert hub_env.bin.calls("claude") == []
    assert not (hub_env.data_dir / "index.db").exists()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/sessions/x/resume"),
        ("POST", "/sessions/x/restart-remote"),
        ("POST", "/sessions/x/meta"),
        ("POST", "/sessions/x/review"),
        ("POST", "/reviews/1/suggestions/0/resume"),
        ("POST", "/reviews/1/suggestions/0/apply"),
        ("GET", "/api/v1/sessions/x/reviews"),
        ("GET", "/api/v1/reviews/1"),
        ("GET", "/api/v1/usage"),
        ("GET", "/api/v1/catalog"),
        ("GET", "/sessions/x"),
        ("GET", "/usage"),
    ],
)
def test_action_routes_404_in_hub_mode(
    hub_env: HubEnv, hub_app: FastAPI, method: str, path: str
) -> None:
    with local(hub_app) as client:
        response = client.request(method, path, data={"confirm": "yes", "before_hash": "x"})
        assert response.status_code == 404, response.text
        assert response.json() == {"detail": "not available on a hub"}
    assert hub_env.bin.calls("claude") == []


def test_hub_pages_have_no_usage_link(hub_app: FastAPI) -> None:
    with TestClient(hub_app, base_url=f"https://{HUB_HOST}", client=REMOTE_PEER) as client:
        page = client.get("/login")
        assert page.status_code == 200
        assert 'href="/usage"' not in page.text
        response = client.get("/api/v1/health", headers={"Authorization": f"Bearer {FULL}"})
        assert response.status_code == 200


def test_local_pages_keep_usage_link(web_client: TestClient) -> None:
    assert 'href="/usage"' in web_client.get("/").text


def test_hub_store_read_on_startup(hub_env: HubEnv, hub_app: FastAPI) -> None:
    config = load_config(hub_env.config_path)
    earlier = HubStore(hub_env.data_dir / HUB_DIR, config.machines, 180)
    earlier.replace(parse_snapshot(encode(snapshot([placeholder_session()])), "laptop"))
    web: WebState = hub_app.state.vigil
    assert web.hub is not None
    assert web.hub.sessions() == []
    with local(hub_app):
        assert [row.session_id for row in web.hub.sessions()] == [placeholder_session()["id"]]


def test_local_vigil_has_no_hub_store(web_client: TestClient) -> None:
    assert web_client.app.state.vigil.hub is None  # type: ignore[attr-defined]
