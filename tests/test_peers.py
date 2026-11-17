"""Peers (v2): a merged session list across machines, stale peers, forwarded resume, and that a
peer's token only ever goes to that peer.

Two `create_app` services play two machines: "laptop" (the one with peers configured, reached
from loopback) and "peer-b" (remote access on, reached through an httpx mock transport that hands
each request to its app as a remote client). "peer-c" is a canned handler. No request leaves the
process.
"""

import json
import os
import re
import shutil
import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import (
    FIXTURE_TRANSCRIPTS,
    HANDMADE_PROJECT_DIR,
    LOOPBACK_URL,
    SKIP_REMOTE_FIXTURE,
    VigilEnv,
)
from vigil import peers
from vigil.config import Config, ConfigError, Peer, ResumeConfig, ServerConfig, load_config
from vigil.index.db import connect
from vigil.index.queries import list_sessions, set_meta
from vigil.index.scanner import scan
from vigil.paths import data_dir
from vigil.peers import (
    PeerHub,
    PeerSession,
    fetch_peer_sessions,
    filter_sessions,
    local_machine,
    session_from_dict,
)
from vigil.serialize import session_to_dict
from vigil.web import WebState
from vigil.web.app import create_app

# Placeholders, not real secrets; each at least 32 characters, and all different.
TOKEN_B = "peer-b-token-" + "0123456789abcdef" * 2
TOKEN_C = "peer-c-token-" + "fedcba9876543210" * 2
TOKEN_D = "peer-d-token-" + "00112233445566778899" * 2
PEER_B_HOST = "peer-b.example"
PEER_C_HOST = "peer-c.example"
PEER_B_URL = f"http://{PEER_B_HOST}:7787"
PEER_C_URL = f"http://{PEER_C_HOST}:7787"
PEER_B_CLIENT = ("192.0.2.20", 50000)  # TEST-NET-1: how peer-b sees laptop's requests
NAMED = "e47d2def-0bb3-4d50-9f9c-8e38e4cdcfba"  # on laptop
HANDMADE = "11111111-1111-4111-8111-111111111111"  # on peer-b only
C_SESSION = "33333333-3333-4333-8333-333333333333"  # on peer-c only
CLAUDE_BG = "2.1.312 (Claude Code)\n  --bg  Start in the background\n  attach\n  agents\n"


def write_token(path: Path, token: str, mode: int = 0o600) -> Path:
    path.write_text(token + "\n")
    path.chmod(mode)
    return path


Handler = Callable[[httpx.Request], httpx.Response]


@dataclass
class Network:
    """Stands in for the network: hands each request to the handler for its host, and records
    it. A host without a handler is unreachable."""

    hosts: dict[str, Handler] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        handler = self.hosts.get(request.url.host)
        if handler is None:
            raise httpx.ConnectError("connection refused", request=request)
        return handler(request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def to(self, host: str) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.host == host]


def app_handler(client: TestClient) -> Handler:
    """Hand a request to a vigil app (as a remote client) and return its answer."""

    def handle(request: httpx.Request) -> httpx.Response:
        headers = {k: v for k, v in request.headers.items() if k not in ("host", "content-length")}
        answer = client.request(
            request.method, str(request.url), headers=headers, content=request.content
        )
        return httpx.Response(
            answer.status_code, headers=answer.headers, content=answer.content, request=request
        )

    return handle


def canned_session(session_id: str = C_SESSION, **extra: Any) -> dict[str, Any]:
    return {
        "id": session_id,
        "title": "peer-c work",
        "cwd": "/home/user/src/peer-c-repo",
        "project_key": "/home/user/src/peer-c-repo",
        "project_label": "peer-c-repo",
        "last_activity_at": "2026-11-01T10:00:00.000000+00:00",
        "message_count": 3,
        "status": "idle",
        "machine": "not-used",
        **extra,
    }


def peer_c_handler(request: httpx.Request) -> httpx.Response:
    """peer-c: lists one session; asked to resume, redirects to peer-b (which must not be
    followed)."""
    if request.method == "GET" and request.url.path == "/api/v1/sessions":
        return httpx.Response(200, json=[canned_session()])
    return httpx.Response(
        302, headers={"Location": f"{PEER_B_URL}/sessions/{HANDMADE}/resume"}, request=request
    )


@dataclass
class Machines:
    env: VigilEnv
    network: Network
    laptop: TestClient
    peer_b_index: Path
    peer_b_cwd: Path
    data: Path

    @property
    def hub(self) -> PeerHub:
        web: WebState = self.laptop.app.state.vigil  # type: ignore[attr-defined]
        return web.peers


def laptop_config(env: VigilEnv, peers_toml: str) -> None:
    config = Path(os.environ["XDG_CONFIG_HOME"]) / "vigil" / "config.toml"
    config.write_text(f'claude_home = "{env.claude_home}"\nmachine = "laptop"\n\n{peers_toml}')


def peer_toml(name: str, url: str, token_file: Path) -> str:
    return f'[[peers]]\nname = "{name}"\nurl = "{url}"\ntoken_file = "{token_file}"\n\n'


@pytest.fixture
def machines(vigil_env: VigilEnv, tmp_path: Path) -> Iterator[Machines]:
    """laptop (this machine: the fixture transcripts but the hand-made one, peer-b and peer-c
    configured) and peer-b (only the hand-made session, in a folder that exists)."""
    stack = ExitStack()
    network = Network()

    # peer-b: its own Claude home, index and token; remote access on.
    home_b = tmp_path / "peer_b_home"
    shutil.copytree(
        FIXTURE_TRANSCRIPTS / "handmade",
        home_b / "projects" / HANDMADE_PROJECT_DIR,
        ignore=SKIP_REMOTE_FIXTURE,
    )
    index_b = tmp_path / "peer_b" / "index.db"
    cwd_b = tmp_path / "peer_b_work"
    cwd_b.mkdir()
    conn = connect(index_b)
    scan(conn, home_b)
    with conn:
        conn.execute("UPDATE sessions SET cwd = ? WHERE session_id = ?", (str(cwd_b), HANDMADE))
    conn.close()
    token_b_server = write_token(tmp_path / "peer_b_server_token", TOKEN_B)
    config_b = Config(
        claude_home=home_b,
        resume=ResumeConfig(remote_link_timeout=0.1),
        server=ServerConfig(token_file=token_b_server, allowed_hosts=(PEER_B_HOST,)),
    )
    app_b = create_app(config_b, conn_factory=lambda: connect(index_b), start_watcher=False)
    client_b = stack.enter_context(
        TestClient(app_b, base_url=PEER_B_URL, client=PEER_B_CLIENT, follow_redirects=False)
    )
    network.hosts[PEER_B_HOST] = app_handler(client_b)
    network.hosts[PEER_C_HOST] = peer_c_handler

    # laptop: the fixture transcripts without the hand-made one, which lives on peer-b.
    shutil.rmtree(vigil_env.claude_home / "projects" / HANDMADE_PROJECT_DIR)
    token_b = write_token(tmp_path / "laptop_peer_b_token", TOKEN_B)
    token_c = write_token(tmp_path / "laptop_peer_c_token", TOKEN_C)
    laptop_config(
        vigil_env,
        peer_toml("peer-b", PEER_B_URL, token_b) + peer_toml("peer-c", PEER_C_URL, token_c),
    )
    conn = connect(vigil_env.index)
    scan(conn, vigil_env.claude_home)
    conn.close()
    app = create_app(
        load_config(),
        conn_factory=lambda: connect(vigil_env.index),
        start_watcher=False,
        peer_transport=network.transport(),
    )
    laptop = stack.enter_context(
        TestClient(app, base_url=LOOPBACK_URL, headers={"Origin": LOOPBACK_URL})
    )
    with stack:
        yield Machines(vigil_env, network, laptop, index_b, cwd_b, data_dir())


def row_html(page: str, row_id: str) -> str:
    match = re.search(rf'<li [^>]*id="{re.escape(row_id)}".*?</li>', page, re.S)
    assert match, f"no row {row_id}"
    return match.group(0)


def machine_cell(row: str) -> str:
    match = re.search(r'<span class="machine">([^<]*)</span>', row)
    assert match, row
    return match.group(1)


# --- the merged list ----------------------------------------------------------------------------


def test_merged_list_shows_machine_in_rows(machines: Machines) -> None:
    laptop = machines.laptop
    page = laptop.get("/").text
    local = row_html(page, f"row-{NAMED}")
    assert machine_cell(local) == "laptop"
    assert f'href="/sessions/{NAMED}"' in local
    remote = row_html(page, f"peer-peer-b-{HANDMADE}")
    assert machine_cell(remote) == "peer-b"
    assert f'href="/peers/peer-b/sessions/{HANDMADE}"' in remote
    assert f'action="/peers/peer-b/sessions/{HANDMADE}/resume"' in remote
    assert f'hx-target="#resume-peer-b-{HANDMADE}"' in remote
    assert "stale" not in remote
    # peer-c's session is listed under the name laptop gives it, not the one it reports.
    other = row_html(page, f"peer-peer-c-{C_SESSION}")
    assert machine_cell(other) == "peer-c"
    assert "not-used" not in page
    # Sessions from different machines in one list, grouped by project as usual.
    assert "broken-repo" in page and "peer-c-repo" in page

    # The search and the project filter apply to every machine's sessions.
    found = laptop.get("/", params={"q": "peer_b_work"}, headers={"HX-Request": "true"}).text
    assert f"peer-peer-b-{HANDMADE}" in found and f"row-{NAMED}" not in found
    found = laptop.get("/", params={"q": "fixture-named"}).text
    assert f"row-{NAMED}" in found and "peer-peer-b-" not in found
    found = laptop.get("/", params={"project": "/home/user/src/peer-c-repo"}).text
    assert f"peer-peer-c-{C_SESSION}" in found and f"row-{NAMED}" not in found

    # A peer's session has a detail page, from what the peer sent; it is edited on the peer.
    detail = laptop.get(f"/peers/peer-b/sessions/{HANDMADE}")
    assert detail.status_code == 200
    assert "<dt>Machine</dt><dd>peer-b</dd>" in detail.text
    assert f'action="/peers/peer-b/sessions/{HANDMADE}/resume"' in detail.text
    assert "/meta" not in detail.text
    assert laptop.get(f"/peers/peer-b/sessions/{NAMED}").status_code == 404
    assert laptop.get(f"/peers/nobody/sessions/{HANDMADE}").status_code == 404
    assert "<dt>Machine</dt><dd>laptop</dd>" in laptop.get(f"/sessions/{NAMED}").text

    # The read API stays this machine's (it is what peers read); peers have their own endpoint.
    ids = [s["id"] for s in laptop.get("/api/v1/sessions", params={"all": "true"}).json()]
    assert NAMED in ids and HANDMADE not in ids and C_SESSION not in ids
    status = laptop.get("/api/v1/peers").json()
    assert [(p["name"], p["url"], p["stale"], p["session_count"]) for p in status] == [
        ("peer-b", PEER_B_URL, False, 1),
        ("peer-c", PEER_C_URL, False, 1),
    ]
    assert all(p["error"] is None and p["fetched_at"] for p in status)


def test_no_machine_column_without_peers(web_client: TestClient) -> None:
    page = web_client.get("/").text
    assert "<th>Machine</th>" not in page and 'class="machine"' not in page
    assert web_client.get("/api/v1/peers").json() == []


def test_local_machine_name_from_config_or_host_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("socket.gethostname", lambda: "host-placeholder")
    assert local_machine(Config()) == "host-placeholder"
    assert local_machine(Config(machine="laptop")) == "laptop"


def test_each_peer_fetched_once_then_reused(machines: Machines) -> None:
    machines.laptop.get("/")
    asked = len(machines.network.requests)
    assert asked == 2  # one fetch per peer
    machines.laptop.get("/", params={"q": "anything"})
    machines.laptop.get("/api/v1/peers")
    assert len(machines.network.requests) == asked  # reused for REFRESH_AFTER seconds
    for request in machines.network.requests:
        assert request.method == "GET" and request.url.path == "/api/v1/sessions"
        assert request.url.params["all"] == "true"
        # 3 s for connecting, sending, waiting and reading (and a deadline over the whole read).
        assert request.extensions["timeout"] == dict.fromkeys(
            ("connect", "read", "write", "pool"), 3.0
        )


# --- offline peers ------------------------------------------------------------------------------


def test_offline_peer_shows_stale(machines: Machines) -> None:
    laptop, network = machines.laptop, machines.network
    laptop.get("/")
    cache = machines.data / "peers" / "peer-b.json"
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600
    assert stat.S_IMODE(cache.parent.stat().st_mode) == 0o700
    cached = json.loads(cache.read_text())
    assert cached["url"] == PEER_B_URL
    assert [s["id"] for s in cached["sessions"]] == [HANDMADE]

    # peer-b goes offline: its last-known sessions are shown, marked stale.
    del network.hosts[PEER_B_HOST]
    machines.hub.forget()
    page = laptop.get("/").text
    row = row_html(page, f"peer-peer-b-{HANDMADE}")
    assert 'class="session stale"' in row and ">stale</span>" in row
    assert "status-unknown" in row  # its status is not known any more
    assert "peer-b cannot be reached (unreachable: connection refused)" in page
    assert "Its sessions as of" in page
    assert "stale" not in row_html(page, f"peer-peer-c-{C_SESSION}")
    detail = laptop.get(f"/peers/peer-b/sessions/{HANDMADE}").text
    assert "peer-b cannot be reached" in detail and "this is what it last sent" in detail
    status = {p["name"]: p for p in laptop.get("/api/v1/peers").json()}
    assert status["peer-b"]["stale"] is True and status["peer-b"]["session_count"] == 1
    assert status["peer-b"]["fetched_at"] == cached["fetched_at"]

    # An unreachable peer is not asked again for RETRY_AFTER seconds.
    asked = len(network.to(PEER_B_HOST))
    laptop.get("/")
    assert len(network.to(PEER_B_HOST)) == asked

    # After a restart (a new hub), the last-known sessions still come from the data dir.
    peer_b = machines.hub.peer("peer-b")
    assert peer_b is not None
    result = fetch_peer_sessions(
        peer_b, transport=network.transport(), cache_dir=machines.data / "peers"
    )
    assert result.stale and [s.session_id for s in result.sessions] == [HANDMADE]
    assert all(s.stale and s.status == "unknown" for s in result.sessions)
    # A cache made for another URL is not used.
    moved = Peer("peer-b", "http://elsewhere.example", peer_b.token_file)
    result = fetch_peer_sessions(
        moved, transport=network.transport(), cache_dir=machines.data / "peers"
    )
    assert result.sessions == [] and result.fetched_at is None

    # A peer never reached shows that nothing is known about it.
    cache.unlink()
    machines.hub.forget()
    page = laptop.get("/").text
    assert "None of its sessions are known yet" in page
    assert f"peer-peer-b-{HANDMADE}" not in page


def failing(error: type[httpx.TransportError]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        raise error("simulated", request=request)

    return httpx.MockTransport(handle)


def answering(*args: Any, **kwargs: Any) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(*args, **kwargs))


@pytest.mark.parametrize(
    ("transport", "reason"),
    [
        (failing(httpx.ConnectTimeout), "timed out after 3 s"),
        (failing(httpx.ReadTimeout), "timed out after 3 s"),
        (failing(httpx.ConnectError), "unreachable: simulated"),
        (answering(401, json={"detail": "no"}), "refused the token (401)"),
        (answering(400, json={"detail": "bad host"}), "server.allowed_hosts"),
        (answering(302, headers={"Location": "http://other.example/"}), "not followed"),
        (answering(500, text="oops"), "answered HTTP 500"),
        (answering(200, text="<html>"), "not JSON"),
        (answering(200, json={"sessions": []}), "not a list"),
    ],
)
def test_failed_fetch_reasons(tmp_path: Path, transport: httpx.MockTransport, reason: str) -> None:
    peer = Peer("peer-b", PEER_B_URL, write_token(tmp_path / "token", TOKEN_B))
    result = fetch_peer_sessions(peer, transport=transport, cache_dir=tmp_path / "peers")
    assert result.stale and result.error is not None and reason in result.error
    assert TOKEN_B not in result.error


def test_oversized_answer_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(peers, "MAX_ANSWER_BYTES", 100)
    peer = Peer("peer-b", PEER_B_URL, write_token(tmp_path / "token", TOKEN_B))
    big = answering(200, json=[canned_session(note="x" * 200)])
    result = fetch_peer_sessions(peer, transport=big, cache_dir=tmp_path / "peers")
    assert result.error == "answer larger than 100 bytes"


def test_unusable_peer_token_file_sends_nothing(tmp_path: Path) -> None:
    network = Network(hosts={PEER_B_HOST: lambda request: httpx.Response(200, json=[])})
    for path, problem in (
        (write_token(tmp_path / "open", TOKEN_B, mode=0o644), "it must be 0600"),
        (write_token(tmp_path / "short", "short"), "32 to 4096 characters"),
        (tmp_path / "missing", "cannot read token file"),
    ):
        peer = Peer("peer-b", PEER_B_URL, path)
        result = fetch_peer_sessions(peer, transport=network.transport(), cache_dir=tmp_path)
        assert result.error is not None and problem in result.error
        with pytest.raises(peers.ResumeForwardError, match=problem):
            peers.forward_resume(peer, HANDMADE, transport=network.transport())
    assert network.requests == []
    # A 0400 token file is fine, as for server.token_file.
    peer = Peer("peer-b", PEER_B_URL, write_token(tmp_path / "ro", TOKEN_B, mode=0o400))
    assert (
        fetch_peer_sessions(peer, transport=network.transport(), cache_dir=tmp_path).error is None
    )


# --- forwarded resume ---------------------------------------------------------------------------


def test_resume_forwarded_to_owner(machines: Machines, monkeypatch: pytest.MonkeyPatch) -> None:
    env, laptop, network = machines.env, machines.laptop, machines.network
    env.bin.install("claude", stdout=CLAUDE_BG)
    env.bin.install("xterm")
    monkeypatch.setenv("DISPLAY", ":99")  # a terminal could be opened on this machine

    response = laptop.post(f"/peers/peer-b/sessions/{HANDMADE}/resume")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "ok": True,
        "attach": f"claude attach {HANDMADE}",
        "opened_terminal": False,
        "remote_url": None,
        "pending": True,  # a link did not turn up within the (short) test wait
        "machine": "peer-b",
    }
    # peer-b started it, in its own folder for the session; nothing opened a terminal.
    started = [call for call in env.bin.calls("claude") if "--resume" in call.argv]
    assert len(started) == 1
    assert started[0].argv == ["--bg", "--resume", HANDMADE, "--remote-control"]
    assert started[0].cwd == str(machines.peer_b_cwd)
    assert env.bin.calls("xterm") == []
    # The forwarded request: peer-b's token, no Origin (a bearer request needs none).
    (forwarded,) = [r for r in network.to(PEER_B_HOST) if r.method == "POST"]
    assert forwarded.url == httpx.URL(f"{PEER_B_URL}/sessions/{HANDMADE}/resume")
    assert forwarded.headers["authorization"] == f"Bearer {TOKEN_B}"
    assert "origin" not in forwarded.headers and "cookie" not in forwarded.headers
    assert forwarded.content == b""
    # peer-b recorded the launch in its own index.
    conn = sqlite3.connect(machines.peer_b_index)
    launches = conn.execute("SELECT count(*) FROM launches").fetchone()[0]
    conn.close()
    assert launches == 1

    # The peer's refusal comes back with its reason, under the peer's name.
    conn = sqlite3.connect(machines.peer_b_index)
    with conn:
        conn.execute(
            "UPDATE sessions SET cwd = '/nonexistent/gone' WHERE session_id = ?", (HANDMADE,)
        )
    conn.close()
    response = laptop.post(f"/peers/peer-b/sessions/{HANDMADE}/resume")
    assert response.status_code == 409
    assert response.json() == {
        "ok": False,
        "reason": "peer-b: folder no longer exists: /nonexistent/gone",
        "restartable": False,
    }
    response = laptop.post(f"/peers/peer-b/sessions/{C_SESSION}/resume")
    assert response.status_code == 404 and response.json()["reason"].startswith("peer-b: ")

    # Only peers in the config, only session ids; neither sends anything.
    asked = len(network.requests)
    response = laptop.post(f"/peers/nobody/sessions/{HANDMADE}/resume")
    assert response.status_code == 404 and "no peer called 'nobody'" in response.json()["reason"]
    assert laptop.post("/peers/peer-b/sessions/..%2Fapi/resume").status_code == 404
    assert laptop.post("/peers/peer-b/sessions/-rf/resume").status_code == 404
    assert len(network.requests) == asked
    # A session that is not on this machine is not resumed here.
    assert laptop.post(f"/sessions/{HANDMADE}/resume").status_code == 404
    # The forwarding action is guarded like any other POST: same origin only.
    assert (
        laptop.post(
            f"/peers/peer-b/sessions/{HANDMADE}/resume", headers={"Origin": "http://evil.example"}
        ).status_code
        == 403
    )
    assert len(network.requests) == asked

    # peer-b offline: 502, with the reason.
    del network.hosts[PEER_B_HOST]
    response = laptop.post(f"/peers/peer-b/sessions/{HANDMADE}/resume")
    assert response.status_code == 502
    assert response.json()["reason"] == "peer-b: unreachable: connection refused"


def test_forward_resume_carries_remote_url_and_restartable(tmp_path: Path) -> None:
    link = "https://claude.ai/code/session_PLACEHOLDER01"
    peer = Peer("peer-b", PEER_B_URL, write_token(tmp_path / "token", TOKEN_B))
    answer: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(answer.pop("status", 200), json=answer)

    network = Network(hosts={PEER_B_HOST: handle})
    answer.update(ok=True, attach="claude attach x", opened_terminal=False, remote_url=link)
    forwarded = peers.forward_resume(peer, HANDMADE, transport=network.transport())
    assert forwarded == {
        "ok": True,
        "attach": "claude attach x",
        "opened_terminal": False,
        "remote_url": link,
        "pending": False,
        "machine": "peer-b",
    }
    # A link that is not a Claude link is dropped: it ends up in the user's browser.
    answer.update(ok=True, attach="claude attach x", remote_url="https://evil.example/x")
    forwarded = peers.forward_resume(peer, HANDMADE, transport=network.transport())
    assert forwarded["remote_url"] is None
    answer.update(ok=True, attach="claude attach x", remote_url=None, pending=True)
    forwarded = peers.forward_resume(peer, HANDMADE, transport=network.transport())
    assert forwarded["pending"] is True
    # A refusal that offers a restart says so.
    answer.update(status=409, ok=False, reason="running without Remote Control", restartable=True)
    with pytest.raises(peers.ResumeForwardError) as refused:
        peers.forward_resume(peer, HANDMADE, transport=network.transport())
    assert (refused.value.status, refused.value.restartable) == (409, True)
    assert refused.value.reason == "peer-b: running without Remote Control"
    answer.update(status=409, ok=False, reason="folder gone")
    answer.pop("restartable")
    with pytest.raises(peers.ResumeForwardError) as refused:
        peers.forward_resume(peer, HANDMADE, transport=network.transport())
    assert refused.value.restartable is False


def test_forward_restart(tmp_path: Path) -> None:
    link = "https://claude.ai/code/session_PLACEHOLDER01"
    peer = Peer("peer-b", PEER_B_URL, write_token(tmp_path / "token", TOKEN_B))
    seen: list[httpx.Request] = []
    answer: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(answer.pop("status", 200), json=answer)

    network = Network(hosts={PEER_B_HOST: handle})
    answer.update(ok=True, attach="claude attach x", opened_terminal=False, remote_url=link)
    forwarded = peers.forward_restart(peer, HANDMADE, transport=network.transport())
    assert forwarded == {
        "ok": True,
        "attach": "claude attach x",
        "opened_terminal": False,
        "remote_url": link,
        "pending": False,
        "machine": "peer-b",
    }
    (request,) = seen
    assert request.method == "POST"
    assert request.url.path == f"/sessions/{HANDMADE}/restart-remote"
    assert request.read() == b"confirm=yes"  # the peer insists on the confirmation
    assert request.headers["authorization"] == f"Bearer {TOKEN_B}"
    # Same handling of the answer as a resume: foreign links dropped, refusals carried over.
    answer.update(ok=True, attach="claude attach x", remote_url="https://evil.example/x")
    forwarded = peers.forward_restart(peer, HANDMADE, transport=network.transport())
    assert forwarded["remote_url"] is None
    answer.update(status=409, ok=False, reason="could not stop the session", restartable=False)
    with pytest.raises(peers.ResumeForwardError) as refused:
        peers.forward_restart(peer, HANDMADE, transport=network.transport())
    assert refused.value.status == 409
    assert refused.value.reason == "peer-b: could not stop the session"
    with pytest.raises(peers.ResumeForwardError) as refused:
        peers.forward_restart(peer, "-rf", transport=network.transport())
    assert refused.value.status == 404
    assert len(seen) == 3  # a bad session id sends nothing


CONFIRM = {"confirm": "yes"}


def test_restart_forwarded_to_owner(machines: Machines) -> None:
    laptop, network = machines.laptop, machines.network
    # Without the confirmation nothing is sent to the peer.
    assert laptop.post(f"/peers/peer-b/sessions/{HANDMADE}/restart-remote").status_code == 400
    assert network.requests == []
    response = laptop.post(f"/peers/peer-b/sessions/{HANDMADE}/restart-remote", data=CONFIRM)
    # peer-b answers for itself: it knows no such live session, so it refuses the restart.
    assert response.status_code == 409, response.text
    assert response.json()["reason"].startswith("peer-b: ")
    assert response.json()["restartable"] is False
    assert any(r.url.path == f"/sessions/{HANDMADE}/restart-remote" for r in network.requests)
    assert laptop.post("/peers/nobody/sessions/x/restart-remote", data=CONFIRM).status_code == 404


def test_resume_forward_refused_by_peer(machines: Machines, tmp_path: Path) -> None:
    # laptop holds a wrong token for peer-b: peer-b refuses it, and laptop says so.
    write_token(tmp_path / "laptop_peer_b_token", TOKEN_D)
    response = machines.laptop.post(f"/peers/peer-b/sessions/{HANDMADE}/resume")
    assert response.status_code == 502
    assert "refused the token (401)" in response.json()["reason"]
    assert TOKEN_D not in response.text


# --- tokens -------------------------------------------------------------------------------------


def test_peer_token_never_sent_to_other_peers(
    machines: Machines, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, laptop, network = machines.env, machines.laptop, machines.network
    env.bin.install("claude", stdout=CLAUDE_BG)

    # Everything that talks to peers: the list, the peers API, detail pages, and resume on
    # each peer (peer-c answers with a redirect to peer-b, which must not be followed).
    pages = [laptop.get("/").text, laptop.get("/api/v1/peers").text]
    pages.append(laptop.get(f"/peers/peer-b/sessions/{HANDMADE}").text)
    pages.append(laptop.get(f"/peers/peer-c/sessions/{C_SESSION}").text)
    assert laptop.post(f"/peers/peer-b/sessions/{HANDMADE}/resume").status_code == 200
    response = laptop.post(f"/peers/peer-c/sessions/{C_SESSION}/resume")
    assert response.status_code == 502
    assert response.json()["reason"] == ("peer-c: answered 302 (a redirect, which is not followed)")
    pages.append(response.text)
    machines.hub.forget()
    pages.append(laptop.get("/").text)

    tokens = {PEER_B_HOST: TOKEN_B, PEER_C_HOST: TOKEN_C}
    assert {request.url.host for request in network.requests} == set(tokens)
    for request in network.requests:
        token = tokens[request.url.host]
        assert request.url.scheme == "http" and request.url.port == 7787
        assert request.headers["authorization"] == f"Bearer {token}"
        other = TOKEN_C if token == TOKEN_B else TOKEN_B
        assert other not in str(request.headers) and other not in str(request.url)
        assert other.encode() not in request.content
    # peer-b got exactly laptop's own resume; the redirect was not followed there.
    assert [r.method for r in network.to(PEER_B_HOST)].count("POST") == 1
    # No token appears in any page, answer or cache file.
    caches = [path.read_text() for path in (machines.data / "peers").iterdir()]
    assert len(caches) == 2
    for text in pages + caches:
        assert TOKEN_B not in text and TOKEN_C not in text


def test_requests_go_to_the_peer_only(tmp_path: Path) -> None:
    client = peers._client(httpx.Timeout(1.0), None)
    assert client.trust_env is False  # no proxy or .netrc from the environment
    assert client.follow_redirects is False
    client.close()
    # A request is checked against the peer's origin before it is sent.
    peer = Peer("peer-b", PEER_B_URL, write_token(tmp_path / "token", TOKEN_B))
    with httpx.Client() as plain:
        request = peers._request(plain, peer, "GET", "/api/v1/sessions")
        assert request.url.host == PEER_B_HOST
        with pytest.raises(peers.PeerError, match="another host"):
            peers._request(plain, peer, "GET", "@other.example/api/v1/sessions")


# --- what peers send ----------------------------------------------------------------------------


def test_peer_sessions_are_parsed_defensively() -> None:
    assert session_from_dict({"id": "../etc"}, "peer-b") is None
    assert session_from_dict({"title": "no id"}, "peer-b") is None
    session = session_from_dict(
        canned_session(
            last_user="u" * 900,
            message_count="many",
            models=["opus", 3],
            status="hacked",
            last_activity_at="not a time",
            pinned="yes",
        ),
        "peer-c",
    )
    assert session is not None
    assert session.machine == "peer-c" and session.peer == "peer-c"
    assert session.last_user == "u" * 500
    assert session.message_count == 0 and session.models == ("opus",)
    assert session.status == "unknown" and session.last_activity_at is None
    assert session.pinned is False


def test_peer_markup_is_escaped(machines: Machines) -> None:
    def evil(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[canned_session(title="<script>alert(1)</script>")])

    machines.network.hosts[PEER_C_HOST] = evil
    page = machines.laptop.get("/").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<script>alert(1)" not in page


def test_filter_sessions_matches_the_index(index_db: sqlite3.Connection) -> None:
    set_meta(index_db, NAMED, archived=True, note="Peer Note")
    every = list_sessions(index_db, include_archived=True, collapse_chains=False, limit=-1)
    remote = [session_from_dict(session_to_dict(row), "peer-b") for row in every]
    rows = [row for row in remote if row is not None]
    assert len(rows) == len(every)
    for query in (None, "fixture", "FIXTURE named", "peer note", "broken-repo", "no-such-word"):
        for project in (None, "/home/user/src/broken-repo"):
            for archived in (False, True):
                for collapse in (False, True):
                    expected = list_sessions(
                        index_db,
                        query=query,
                        project_key=project,
                        include_archived=archived,
                        collapse_chains=collapse,
                        limit=-1,
                    )
                    got = filter_sessions(
                        rows,
                        query=query,
                        project_key=project,
                        include_archived=archived,
                        collapse_chains=collapse,
                    )
                    assert [r.session_id for r in got] == [r.session_id for r in expected], (
                        query,
                        project,
                        archived,
                        collapse,
                    )


def test_peer_session_is_a_session_row() -> None:
    session = session_from_dict(canned_session(), "peer-c", stale=True)
    assert isinstance(session, PeerSession) and session.stale
    assert session_to_dict(session)["machine"] == "peer-c"


# --- config -------------------------------------------------------------------------------------


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_peers_config(tmp_path: Path) -> None:
    config = load_config(
        write_config(
            tmp_path,
            'machine = "laptop"\n'
            '[[peers]]\nname = "desk_top-1"\nurl = "http://Peer-B.Example:7787/"\n'
            'token_file = "/tokens/b"\n'
            '[[peers]]\nname = "phone"\nurl = "https://[2001:db8::1]"\n'
            'token_file = "~/tokens/c"\n',
        )
    )
    assert config.machine == "laptop"
    assert config.peers == (
        Peer("desk_top-1", "http://peer-b.example:7787", Path("/tokens/b")),
        Peer("phone", "https://[2001:db8::1]", Path("~/tokens/c").expanduser()),
    )
    assert load_config(write_config(tmp_path, "")).peers == ()
    assert load_config(write_config(tmp_path, "")).machine is None


PEER = 'name = "b"\nurl = "http://b.example"\ntoken_file = "/t"\n'


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('peers = "b"', "array of tables"),
        ("[peers]\nname = 'b'", "array of tables"),
        (f"[[peers]]\n{PEER}colour = 'red'", "unknown config key: peers[1].colour"),
        ('[[peers]]\nname = "b"\nurl = "http://b.example"', "peers[1].token_file` is missing"),
        ("[[peers]]\n" + PEER.replace('"b"', '"has space"'), "letters, digits"),
        ("[[peers]]\n" + PEER.replace('"b"', '"-b"'), "letters, digits"),
        ("[[peers]]\n" + PEER.replace('"b"', '"a.b"'), "letters, digits"),
        (f"[[peers]]\n{PEER}[[peers]]\n{PEER}", "two peers are called 'b'"),
        (f'machine = "b"\n[[peers]]\n{PEER}', "this machine's name"),
        ("[[peers]]\n" + PEER.replace("http://b.example", "ftp://b.example"), "http(s)://"),
        ("[[peers]]\n" + PEER.replace("http://b.example", "b.example"), "http(s)://"),
        ("[[peers]]\n" + PEER.replace("http://b.example", "http://u:p@b.example"), "user"),
        ("[[peers]]\n" + PEER.replace("http://b.example", "http://b.example/vigil"), "path"),
        ("[[peers]]\n" + PEER.replace("http://b.example", "http://b.example?x=1"), "query"),
        ("[[peers]]\n" + PEER.replace("http://b.example", "http://b.example:99999"), "http(s)"),
        ("[[peers]]\n" + PEER.replace('"/t"', '"relative/t"'), "absolute path"),
        ("[[peers]]\n" + PEER.replace('"/t"', "3"), "must be a path"),
        ('machine = ""', "`machine` must be"),
        ("machine = 3", "`machine` must be"),
    ],
)
def test_bad_peers_config_raises(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=re.escape(message)):
        load_config(write_config(tmp_path, text))


def test_create_app_without_peers_has_an_empty_hub(vigil_env: VigilEnv) -> None:
    app: FastAPI = create_app(Config(claude_home=vigil_env.claude_home), start_watcher=False)
    assert app.state.vigil.peers.peers == ()


def test_peer_remote_url_validated() -> None:
    good = "https://claude.ai/code/session_PLACEHOLDER01"
    at = "2026-11-17T11:05:00.000000+00:00"
    session = session_from_dict(canned_session(remote_url=good, remote_url_at=at), "peer-c")
    assert session is not None
    assert session.remote_url == good and session.remote_url_at is not None
    for bad in ("https://evil.example/x", good + "/../x", good + "?a=1", 7, None):
        session = session_from_dict(canned_session(remote_url=bad, remote_url_at=at), "peer-c")
        assert session is not None and session.remote_url is None


def test_peer_open_url_validated() -> None:
    good = "https://claude.ai/code/session_PLACEHOLDER01"
    session = session_from_dict(canned_session(open_url=good), "peer-c")
    assert session is not None and session.open_url == good
    assert session_to_dict(session)["open_url"] == good
    for bad in ("https://evil.example/x", good + "?a=1", 7, None):
        session = session_from_dict(canned_session(open_url=bad), "peer-c")
        assert session is not None and session.open_url is None
    session = session_from_dict(canned_session(), "peer-c")
    assert session is not None and session.open_url is None


def test_peer_row_shows_open_only_from_open_url(machines: Machines) -> None:
    link = "https://claude.ai/code/session_PLACEHOLDER01"
    answer = canned_session(status="background", remote_url=link, open_url=None)

    def peer_c(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[answer])

    machines.network.hosts[PEER_C_HOST] = peer_c
    row_id = f"peer-peer-c-{C_SESSION}"
    # A link and a live session, but the owner did not say it is joinable: no guess.
    machines.hub.forget("peer-c")
    page = machines.laptop.get("/").text
    assert 'class="open"' not in row_html(page, row_id)
    assert link not in machines.laptop.get(f"/peers/peer-c/sessions/{C_SESSION}").text
    # The owner says it is.
    answer["open_url"] = link
    machines.hub.forget("peer-c")
    page = machines.laptop.get("/").text
    assert f'<a class="open" href="{link}" rel="noopener">Open in Claude</a>' in row_html(
        page, row_id
    )
    machines.hub.forget("peer-c")
    assert link in machines.laptop.get(f"/peers/peer-c/sessions/{C_SESSION}").text


def test_peer_busy_is_read_as_a_bool() -> None:
    for value, expected in (
        (True, True),
        (False, False),
        ("yes", False),
        (1, False),
        (None, False),
    ):
        session = session_from_dict(canned_session(busy=value), "peer-c")
        assert session is not None and session.busy is expected
    session = session_from_dict(canned_session(), "peer-c")
    assert session is not None and session.busy is False
    # What a stale peer last sent says nothing about now.
    session = session_from_dict(canned_session(busy=True), "peer-c", stale=True)
    assert session is not None and session.busy is False
    assert session_from_dict(canned_session(busy=True), "peer-c") is not None
    busy = session_from_dict(canned_session(busy=True), "peer-c")
    assert busy is not None and session_to_dict(busy)["busy"] is True


def test_peer_row_shows_the_busy_warning_from_the_owner(machines: Machines) -> None:
    answer = canned_session(status="background", busy=True)

    def peer_c(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[answer])

    machines.network.hosts[PEER_C_HOST] = peer_c
    row_id = f"peer-peer-c-{C_SESSION}"
    busy_copy = "This session is working right now. Restarting stops it mid-task."
    machines.hub.forget("peer-c")
    page = machines.laptop.get("/").text
    row = row_html(page, row_id)
    assert f'action="/peers/peer-c/sessions/{C_SESSION}/restart-remote"' in row
    assert busy_copy in row
    machines.hub.forget("peer-c")
    assert busy_copy in machines.laptop.get(f"/peers/peer-c/sessions/{C_SESSION}").text
    answer["busy"] = False
    machines.hub.forget("peer-c")
    assert busy_copy not in row_html(machines.laptop.get("/").text, row_id)
