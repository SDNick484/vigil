"""The hub's pages (`mode = "hub"`): the sessions list across machines and the session page at
`/machines/{name}/sessions/{id}`, which hand off to the owning machine instead of acting."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import FULL, PUSH_DESK, PUSH_LAPTOP, READ, snapshot_body
from vigil.hub import HubStore

ClientFor = Callable[..., TestClient]
LINK = "https://claude.ai/code/session_PLACEHOLDER"
OTHER_LINK = "https://claude.ai/code/session_EARLIER"


def push(client_for: ClientFor, machine: str, token: str, **changes: Any) -> list[dict[str, Any]]:
    """Push `machine`'s two placeholder sessions, `changes` set on the first; returns them."""
    body = snapshot_body(machine)
    body["sessions"][0].update(changes)
    response = client_for(token).put(f"/ingest/{machine}/sessions", json=body)
    assert response.status_code == 200, response.text
    return body["sessions"]  # type: ignore[no-any-return]


def page(client: TestClient, machine: str, session: dict[str, Any]) -> str:
    response = client.get(f"/machines/{machine}/sessions/{session['id']}")
    assert response.status_code == 200, response.text
    return str(response.text)


def test_hub_list_has_machine_column_and_no_resume_forms(hub_client_for: ClientFor) -> None:
    push(hub_client_for, "laptop", PUSH_LAPTOP)
    push(hub_client_for, "desk", PUSH_DESK)
    html = hub_client_for(FULL).get("/").text
    assert 'class="machine">laptop<' in html
    assert 'class="machine">desk<' in html
    assert 'action="/sessions/' not in html
    assert "/resume" not in html
    assert 'href="/machines/laptop/sessions/' in html


def test_hub_list_links_hand_offs(hub_client_for: ClientFor) -> None:
    first = push(hub_client_for, "laptop", PUSH_LAPTOP, status="running", open_url=LINK)[0]
    push(hub_client_for, "desk", PUSH_DESK)
    html = hub_client_for(FULL).get("/").text
    assert f'href="{LINK}"' in html
    assert f'href="http://laptop.example.com:7787/sessions/{first["id"]}"' in html
    assert "Resume on laptop" in html
    assert "Resume on desk" not in html


def test_hub_session_page_actions(hub_client_for: ClientFor) -> None:
    first = push(hub_client_for, "laptop", PUSH_LAPTOP, status="running", open_url=LINK)[0]
    html = page(hub_client_for(FULL), "laptop", first)
    assert f'<a class="open" href="{LINK}" rel="noopener">Join the running session</a>' in html
    href = f'href="http://laptop.example.com:7787/sessions/{first["id"]}"'
    assert f'{href} rel="noopener">Resume on laptop</a>' in html
    command = f"cd /home/user/src/app &amp;&amp; claude --resume {first['id']}"
    assert command in html
    assert 'data-copy="cd /home/user/src/app &amp;&amp; claude --resume' in html
    assert 'action="' not in html  # nothing to post to: it is a hand-off
    assert "hello" in html  # where you left off


def test_remote_link_shown_only_when_different(hub_client_for: ClientFor) -> None:
    same = push(hub_client_for, "laptop", PUSH_LAPTOP, open_url=LINK, remote_url=LINK)[0]
    html = page(hub_client_for(FULL), "laptop", same)
    assert html.count(LINK) == 1
    assert "works only if the session is still running" not in html
    different = push(hub_client_for, "laptop", PUSH_LAPTOP, open_url=LINK, remote_url=OTHER_LINK)
    html = page(hub_client_for(FULL), "laptop", different[0])
    assert html.count(LINK) == 1
    assert f'href="{OTHER_LINK}"' in html
    assert "works only if the session is still running" in html


def test_stale_machine_marked(hub_app: FastAPI, hub_client_for: ClientFor) -> None:
    first = push(hub_client_for, "laptop", PUSH_LAPTOP, status="running", open_url=LINK)[0]
    store = hub_app.state.vigil.hub
    assert isinstance(store, HubStore)
    store._clock = lambda: datetime.now(UTC) + timedelta(seconds=181)
    client = hub_client_for(FULL)
    html = page(client, "laptop", first)
    assert "laptop last seen" in html
    assert "Join the running session" not in html
    listing = client.get("/").text
    assert "laptop last seen" in listing
    assert 'class="status status-unknown"' in listing


def test_no_machine_url_no_resume_link(hub_client_for: ClientFor) -> None:
    desk = push(hub_client_for, "desk", PUSH_DESK)[0]
    html = page(hub_client_for(FULL), "desk", desk)
    assert "Resume on desk" not in html
    assert f"claude --resume {desk['id']}" in html


def test_read_token_cannot_see_pages(hub_client_for: ClientFor) -> None:
    first = push(hub_client_for, "laptop", PUSH_LAPTOP)[0]
    client = hub_client_for(READ)
    response = client.get(f"/machines/laptop/sessions/{first['id']}")
    assert response.status_code == 403
    assert "needs scope full" in response.text
    assert client.get("/").status_code == 403


def test_unknown_session_and_wrong_machine_are_404_pages(hub_client_for: ClientFor) -> None:
    first = push(hub_client_for, "laptop", PUSH_LAPTOP)[0]
    client = hub_client_for(FULL)
    for path in (
        "/machines/laptop/sessions/no-such-session",
        f"/machines/desk/sessions/{first['id']}",
        f"/machines/nobody/sessions/{first['id']}",
    ):
        response = client.get(path)
        assert response.status_code == 404, path
        assert "No such session" in response.text


def test_session_page_by_prefix(hub_client_for: ClientFor) -> None:
    first = push(hub_client_for, "laptop", PUSH_LAPTOP)[0]
    response = hub_client_for(FULL).get(f"/machines/laptop/sessions/{first['id'][:12]}")
    assert response.status_code == 200
    assert first["id"] in response.text


def test_hub_pages_404_on_a_local_vigil(web_client: TestClient) -> None:
    assert web_client.get("/machines/laptop/sessions/anything").status_code == 404


@pytest.mark.parametrize("path", ["/sessions/x", "/usage"])
def test_local_pages_stay_404_on_hub(hub_client_for: ClientFor, path: str) -> None:
    assert hub_client_for(FULL).get(path).status_code == 404
