"""Remote access (v2): token and cookie auth, the server config, and that loopback is unchanged."""

import base64
import hashlib
import hmac
import os
import re
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient

from tests.conftest import LOOPBACK_URL, VigilEnv
from vigil import cli
from vigil.config import ConfigError, ServerConfig, TokenConfig, load_config
from vigil.index.db import connect
from vigil.index.scanner import scan
from vigil.paths import data_dir
from vigil.resume import TERMINAL_ALLOWED
from vigil.web.app import create_app
from vigil.web.auth import (
    FULL,
    SESSION_COOKIE,
    AuthError,
    AuthSettings,
    Credential,
    Grant,
    load_auth,
    load_signing_key,
    load_token,
    safe_next,
    token_matches,
)
from vigil.web.security import BindRefused, authorize, check_bind_address

TOKEN = "test-token-" + "0123456789abcdef" * 2  # a placeholder, not a real secret
REMOTE_HOST = "vigil.example"
REMOTE_URL = f"http://{REMOTE_HOST}:7787"
REMOTE_PEER = ("192.0.2.10", 50000)  # TEST-NET-1: never a real client
LOCAL_PEER = ("127.0.0.1", 50000)
NAMED = "e47d2def-0bb3-4d50-9f9c-8e38e4cdcfba"
CLAUDE_BG = "2.1.312 (Claude Code)\n  --bg  Start in the background\n  attach\n  agents\n"
BROWSER = {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}


def write_token(path: Path, token: str = TOKEN, mode: int = 0o600) -> Path:
    path.write_text(token + "\n")
    path.chmod(mode)
    return path


def write_config(env: VigilEnv, server: str) -> None:
    config = Path(os.environ["XDG_CONFIG_HOME"]) / "vigil" / "config.toml"
    config.write_text(f'claude_home = "{env.claude_home}"\n\n[server]\n{server}\n')


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    return write_token(tmp_path / "token")


@pytest.fixture
def remote_app(vigil_env: VigilEnv, token_file: Path) -> FastAPI:
    """The service with remote access on (a token, `REMOTE_HOST` allowed), over the fixture
    transcripts."""
    write_config(vigil_env, f'token_file = "{token_file}"\nallowed_hosts = ["{REMOTE_HOST}"]')
    conn = connect(vigil_env.index)
    scan(conn, vigil_env.claude_home)
    conn.close()
    return create_app(
        load_config(), conn_factory=lambda: connect(vigil_env.index), start_watcher=False
    )


ClientFactory = Callable[..., TestClient]


@pytest.fixture
def client_for(remote_app: FastAPI) -> Iterator[ClientFactory]:
    """Make clients of `remote_app` from a given peer address and base URL."""
    clients: list[TestClient] = []

    def make(
        peer: tuple[str, int] = REMOTE_PEER, base_url: str = REMOTE_URL, **kwargs: Any
    ) -> TestClient:
        kwargs.setdefault("follow_redirects", False)
        client = TestClient(remote_app, base_url=base_url, client=peer, **kwargs)
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


def bearer(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def label(env: VigilEnv) -> object:
    conn = sqlite3.connect(env.index)
    row = conn.execute("SELECT label FROM session_meta WHERE session_id = ?", (NAMED,)).fetchone()
    conn.close()
    return row[0] if row else None


def full_settings(token: str, key: bytes) -> AuthSettings:
    return AuthSettings(credentials=(Credential(token, FULL),), signing_key=key)


def log_in(client: TestClient, origin: str = REMOTE_URL) -> Any:
    return client.post("/login", data={"token": TOKEN, "next": "/"}, headers={"Origin": origin})


# --- starting the service -------------------------------------------------------------------


def test_non_loopback_without_token_refuses_start(
    vigil_env: VigilEnv,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: runs.append(kwargs))

    def refused(*argv: str) -> str:
        assert cli.main(["serve", *argv]) == 2
        return capsys.readouterr().err

    # No token file configured: only 127.0.0.1, from the flag or from server.bind.
    assert "server.token_file" in refused("--host", "192.0.2.1")
    write_config(vigil_env, 'bind = "0.0.0.0"')
    assert "refusing to listen on 0.0.0.0" in refused()

    # A configured token file that is missing, readable by others, or too short.
    token = tmp_path / "token"
    write_config(vigil_env, f'bind = "0.0.0.0"\ntoken_file = "{token}"')
    assert "cannot read token file" in refused()
    write_token(token, mode=0o644)
    assert "0644; it must be 0600" in refused()
    write_token(token, mode=0o640)
    assert "must be 0600" in refused()
    write_token(token, token="short")
    assert "32 to 4096 characters" in refused()
    write_token(token, token="has a space " + TOKEN)
    assert "without spaces" in refused()
    # A token file in place but a host that is not an address.
    write_token(token)
    assert "not an IP address" in refused("--host", "localhost")
    assert runs == []

    # A good token file: listens where asked, and never trusts forwarding headers.
    assert cli.main(["serve"]) == 0
    assert cli.main(["serve", "--host", "192.0.2.1", "--port", "9000"]) == 0
    assert [(run["host"], run["port"], run["proxy_headers"]) for run in runs] == [
        ("0.0.0.0", 7787, False),
        ("192.0.2.1", 9000, False),
    ]
    # The signing key was created private.
    key = data_dir() / "session.key"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600


def test_token_file_mode_0400_accepted(tmp_path: Path) -> None:
    assert load_token(write_token(tmp_path / "token", mode=0o400)) == TOKEN


def test_token_file_must_be_regular(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(AuthError, match="not a regular file"):
        load_token(fifo)  # refused at once, without waiting for a writer
    with pytest.raises(AuthError, match="not a regular file"):
        load_token(tmp_path)


def test_check_bind_address_with_token(tmp_path: Path) -> None:
    server = ServerConfig(token_file=write_token(tmp_path / "token"))
    check_bind_address("127.0.0.1", ServerConfig())
    for host in ("0.0.0.0", "::", "192.0.2.1", "::1", "fd00::1"):
        check_bind_address(host, server)
    with pytest.raises(BindRefused, match="not an IP address"):
        check_bind_address("vigil.example", server)
    with pytest.raises(BindRefused, match="127.0.0.1 only"):
        check_bind_address("0.0.0.0", ServerConfig())


def test_server_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    assert load_config(tmp_path / "absent.toml").server == ServerConfig()
    path.write_text(
        '[server]\nbind = "::"\ntoken_file = "~/vigil-token"\n'
        'allowed_hosts = ["Vigil.Example", "192.0.2.1", "[fd00::1]"]\n'
    )
    server = load_config(path).server
    assert server.bind == "::"
    assert server.token_file == Path.home() / "vigil-token"
    assert server.allowed_hosts == ("vigil.example", "192.0.2.1", "[fd00::1]")


@pytest.mark.parametrize(
    "text",
    [
        "server = 1",
        "[server]\nport = 1",
        "[server]\nbind = 1",
        '[server]\nbind = "localhost"',
        '[server]\nbind = "0.0.0.0:7787"',
        "[server]\ntoken_file = 1",
        '[server]\ntoken_file = ""',
        '[server]\ntoken_file = "relative/token"',
        '[server]\ntoken_file = "/t"\nallowed_hosts = "vigil.example"',
        '[server]\ntoken_file = "/t"\nallowed_hosts = ["vigil.example:7787"]',
        '[server]\ntoken_file = "/t"\nallowed_hosts = ["http://vigil.example"]',
        '[server]\ntoken_file = "/t"\nallowed_hosts = ["*.example"]',
        '[server]\ntoken_file = "/t"\nallowed_hosts = [""]',
        '[server]\ntoken_file = "/t"\nallowed_hosts = ["[nope]"]',
        '[server]\nallowed_hosts = ["vigil.example"]',  # useless without a token
    ],
)
def test_bad_server_config_raises(tmp_path: Path, text: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(text + "\n")
    with pytest.raises(ConfigError):
        load_config(path)


def test_signing_key_created_private_and_reused(tmp_path: Path) -> None:
    path = tmp_path / "data" / "session.key"
    key = load_signing_key(path)
    assert len(key) == 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert load_signing_key(path) == key
    assert [p.name for p in path.parent.iterdir()] == ["session.key"]  # no temporary left
    path.chmod(0o644)
    with pytest.raises(AuthError, match="must be 0600"):
        load_signing_key(path)
    path.unlink()
    target = tmp_path / "elsewhere"
    target.write_bytes(b"k" * 32)
    target.chmod(0o600)
    path.symlink_to(target)
    with pytest.raises(AuthError):
        load_signing_key(path)  # never follows a link


def test_load_auth_off_without_token(tmp_path: Path) -> None:
    assert load_auth(ServerConfig(), tmp_path) is None
    assert not (tmp_path / "session.key").exists()


def test_load_auth_allows_the_bind_address(tmp_path: Path) -> None:
    token = write_token(tmp_path / "token")
    hosts = {
        bind: load_auth(ServerConfig(bind=bind, token_file=token), tmp_path)
        for bind in ("192.0.2.1", "fd00::1", "0.0.0.0", "127.0.0.1")
    }
    assert {bind: s.allowed_hosts if s else None for bind, s in hosts.items()} == {
        "192.0.2.1": frozenset({"192.0.2.1"}),
        "fd00::1": frozenset({"[fd00::1]"}),
        "0.0.0.0": frozenset(),
        "127.0.0.1": frozenset(),
    }


# --- remote requests --------------------------------------------------------------------------


def test_bearer_required_remotely(vigil_env: VigilEnv, client_for: ClientFactory) -> None:
    client = client_for()
    response = client.get("/api/v1/sessions")
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")
    assert client.get("/api/v1/sessions", headers=bearer("wrong" + TOKEN)).status_code == 401
    assert client.get("/api/v1/sessions", headers={"Authorization": TOKEN}).status_code == 401
    assert client.get("/api/v1/sessions", headers=bearer()).status_code == 200
    lowercase = {"Authorization": f"bearer {TOKEN}"}
    assert client.get("/api/v1/health", headers=lowercase).status_code == 200

    # A Host that is neither loopback nor allowed is refused before the token is looked at.
    response = client.get("/api/v1/health", headers={**bearer(), "Host": "evil.example"})
    assert response.status_code == 400

    # Actions need the token too; with it, no Origin is needed (browsers cannot add the header).
    assert client.post(f"/sessions/{NAMED}/meta", data={"label": "x"}).status_code == 401
    assert label(vigil_env) is None
    response = client.post(f"/sessions/{NAMED}/meta", data={"label": "x"}, headers=bearer())
    assert response.status_code == 200
    assert label(vigil_env) == "x"

    # A browser loading a page goes to the login page; htmx and API calls get 401.
    response = client.get("/?q=a%20b", headers=BROWSER)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2F%3Fq%3Da%2520b"
    assert client.get("/", headers={**BROWSER, "HX-Request": "true"}).status_code == 401
    assert client.get("/api/v1/health", headers=BROWSER).status_code == 401
    # Unknown paths are guarded too; static files and the login page are not.
    assert client.get("/nothing").status_code == 401
    assert client.get("/static/vigil.css").status_code == 200
    assert client.get("/login").status_code == 200


def test_proxied_requests_are_remote(client_for: ClientFactory) -> None:
    """A reverse proxy on this machine connects from loopback; its requests still need the token,
    whatever Host it passes on."""
    client = client_for(peer=LOCAL_PEER, base_url=LOOPBACK_URL)
    assert client.get("/api/v1/health").status_code == 200  # local, no token
    for header in ("X-Forwarded-For", "X-Forwarded-Host", "X-Real-IP", "Forwarded"):
        response = client.get("/api/v1/health", headers={header: "192.0.2.10"})
        assert response.status_code == 401, header
        response = client.get("/api/v1/health", headers={header: "192.0.2.10", **bearer()})
        assert response.status_code == 200, header
    # From loopback but under a remote name: remote too.
    assert client.get("/api/v1/health", headers={"Host": REMOTE_HOST}).status_code == 401


def test_remote_peer_with_loopback_host_still_needs_token(client_for: ClientFactory) -> None:
    client = client_for(base_url=LOOPBACK_URL)
    assert client.get("/api/v1/health").status_code == 401
    assert client.get("/api/v1/health", headers=bearer()).status_code == 200
    # An unknown peer (no IP address) is not trusted as local either.
    client = client_for(peer=("testclient", 50000), base_url=LOOPBACK_URL)
    assert client.get("/api/v1/health").status_code == 401


def test_login_sets_cookie_and_pages_work(vigil_env: VigilEnv, client_for: ClientFactory) -> None:
    client = client_for()
    page = client.get("/login", params={"next": "/sessions/x"})
    assert page.status_code == 200
    assert 'name="token"' in page.text and 'value="/sessions/x"' in page.text

    response = log_in(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    attributes = {part.strip().lower() for part in cookie.split(";")[1:]}
    assert {"httponly", "samesite=lax", "path=/"} <= attributes
    assert "secure" not in attributes  # plain http
    assert any(a.startswith("max-age=") for a in attributes)

    # Pages, the API and htmx requests work with the cookie.
    page = client.get("/", headers=BROWSER)
    assert page.status_code == 200
    assert 'action="/logout"' in page.text
    assert client.get(f"/sessions/{NAMED}", headers=BROWSER).status_code == 200
    assert client.get("/api/v1/sessions").status_code == 200
    assert client.get("/", headers={"HX-Request": "true"}).status_code == 200

    # A POST with the cookie must still come from this service's own pages.
    assert client.post(f"/sessions/{NAMED}/meta", data={"label": "y"}).status_code == 403
    for origin in ("http://evil.example", "null", LOOPBACK_URL, "http://vigil.example:7788"):
        response = client.post(
            f"/sessions/{NAMED}/meta", data={"label": "y"}, headers={"Origin": origin}
        )
        assert response.status_code == 403, origin
    assert label(vigil_env) is None
    response = client.post(
        f"/sessions/{NAMED}/meta", data={"label": "y"}, headers={"Origin": REMOTE_URL}
    )
    assert response.status_code == 200
    assert label(vigil_env) == "y"

    # Logging out clears the cookie; pages need the login again.
    response = client.post("/logout", headers={"Origin": REMOTE_URL})
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert "max-age=0" in response.headers["set-cookie"].lower()
    assert client.get("/", headers=BROWSER).status_code == 303


def test_login_refusals(client_for: ClientFactory) -> None:
    client = client_for()
    response = client.post("/login", data={"token": "wrong"}, headers={"Origin": REMOTE_URL})
    assert response.status_code == 401
    assert "not the token" in response.text
    assert "set-cookie" not in response.headers
    response = client.post("/login", data={}, headers={"Origin": REMOTE_URL})
    assert response.status_code == 401
    # Login is a POST like any other: another site cannot log the browser in.
    assert log_in(client, origin="http://evil.example").status_code == 403
    assert client.post("/login", data={"token": TOKEN}).status_code == 403
    assert "set-cookie" not in client.post("/login", data={"token": TOKEN}).headers
    # After login, only paths on this service.
    for target in ("//evil.example/", "http://evil.example/", "/\\evil.example", "evil"):
        response = client.post(
            "/login", data={"token": TOKEN, "next": target}, headers={"Origin": REMOTE_URL}
        )
        assert response.headers["location"] == "/", target


def test_cookie_secure_over_https(client_for: ClientFactory) -> None:
    client = client_for(base_url=f"https://{REMOTE_HOST}")
    response = log_in(client, origin=f"https://{REMOTE_HOST}")
    assert "secure" in {p.strip().lower() for p in response.headers["set-cookie"].split(";")}
    # Behind a TLS proxy that says so.
    client = client_for()
    response = client.post(
        "/login",
        data={"token": TOKEN},
        headers={"Origin": REMOTE_URL, "X-Forwarded-Proto": "https"},
    )
    assert "secure" in {p.strip().lower() for p in response.headers["set-cookie"].split(";")}


def test_forged_or_expired_cookie_rejected(client_for: ClientFactory, tmp_path: Path) -> None:
    client = client_for()
    cookie = log_in(client).cookies[SESSION_COOKIE]
    client.cookies.clear()
    expires, _, mac = cookie.partition(".")
    for value in (
        cookie[:-2] + ("AA" if not cookie.endswith("AA") else "BB"),  # tampered signature
        f"{int(expires) + 10}.{mac}",  # extended expiry
        f"{int(time.time()) - 10}.{mac}",
        "garbage",
        "",
    ):
        client.cookies.set(SESSION_COOKIE, value)
        assert client.get("/api/v1/health").status_code == 401, value
        client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, cookie)
    assert client.get("/api/v1/health").status_code == 200

    settings = full_settings(TOKEN, b"k" * 32)
    now = time.time()
    value = settings.issue_cookie(now)
    assert settings.cookie_ok(value, now)
    assert not settings.cookie_ok(value, now + settings.max_age + 1)  # expired
    # Another key, or another token, voids it.
    assert not full_settings(TOKEN, b"j" * 32).cookie_ok(value, now)
    assert not full_settings(TOKEN + "x", b"k" * 32).cookie_ok(value, now)


def test_bad_token_rejected_constant_time(
    client_for: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    compared: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a: Any, b: Any) -> bool:
        compared.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)  # the module vigil.web.auth calls
    client = client_for()
    assert client.get("/api/v1/health", headers=bearer("x" * len(TOKEN))).status_code == 401
    assert len(compared) == 1
    response = client.post("/login", data={"token": "nope"}, headers={"Origin": REMOTE_URL})
    assert response.status_code == 401
    assert len(compared) == 2
    # Compared as equal-length digests, so not even the token's length shows in the timing.
    assert all(len(a) == len(b) == 32 for a, b in compared)
    assert token_matches(TOKEN, TOKEN) and not token_matches(TOKEN[:-1], TOKEN)
    assert not token_matches("é" * 40, TOKEN)  # non-ASCII input is just wrong, not an error


def test_safe_next() -> None:
    assert safe_next("/sessions/abc?q=1") == "/sessions/abc?q=1"
    for value in (None, 1, "", "evil", "//evil.example", "/\\evil.example", "/\nx", "https://x"):
        assert safe_next(value) == "/"


def test_remote_resume_does_not_open_terminal(
    vigil_env: VigilEnv,
    remote_app: FastAPI,
    client_for: ClientFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(vigil_env.index)
    with conn:
        conn.execute("UPDATE sessions SET cwd = ? WHERE session_id = ?", (str(tmp_path), NAMED))
    conn.close()
    vigil_env.bin.install("claude", stdout=CLAUDE_BG)
    vigil_env.bin.install("xterm")
    monkeypatch.setenv("DISPLAY", ":99")  # a terminal could be opened here

    remote = client_for()
    web = remote_app.state.vigil
    web.config = replace(web.config, resume=replace(web.config.resume, remote_link_timeout=0.1))
    response = remote.post(f"/sessions/{NAMED}/resume", headers=bearer())
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "attach": f"claude attach {NAMED[:8]}",
        "opened_terminal": False,
        "remote_url": None,
        "pending": True,  # no Remote Control link turned up in the (short) wait
        "at_desk": False,
    }
    # From the browser, with the cookie: the same.
    log_in(remote)
    response = remote.post(f"/sessions/{NAMED}/resume", headers={"Origin": REMOTE_URL})
    assert response.json()["opened_terminal"] is False
    assert vigil_env.bin.calls("xterm") == []
    assert TERMINAL_ALLOWED.get() is True  # only ever off for the length of a request

    # The same service, asked from this machine, opens one.
    local = client_for(peer=LOCAL_PEER, base_url=LOOPBACK_URL)
    response = local.post(f"/sessions/{NAMED}/resume", headers={"Origin": LOOPBACK_URL})
    assert response.json()["opened_terminal"] is True
    # The terminal starts detached; once it has, it is the only one ever started.
    deadline = time.monotonic() + 5
    while not vigil_env.bin.calls("xterm") and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(vigil_env.bin.calls("xterm")) == 1


# --- loopback -----------------------------------------------------------------------------------


def test_loopback_still_works_without_token(
    vigil_env: VigilEnv, web_client: TestClient, client_for: ClientFactory
) -> None:
    # No token configured (the `web_client` app): exactly v1.
    assert web_client.get("/api/v1/health").status_code == 200
    assert web_client.get("/", headers=BROWSER).status_code == 200
    assert web_client.get("/login").status_code == 404  # nothing to log in to
    response = web_client.post(f"/sessions/{NAMED}/meta", data={"label": "v1"})
    assert response.status_code == 200
    assert "Log out" not in web_client.get("/", headers=BROWSER).text
    local = TestClient(web_client.app, base_url=LOOPBACK_URL, client=LOCAL_PEER)
    assert local.get("/api/v1/health").status_code == 200
    assert local.post(f"/sessions/{NAMED}/meta", data={"label": "v1"}).status_code == 403
    # ...and never reachable from elsewhere, directly or through a proxy, even with a token.
    for peer, headers in (
        (REMOTE_PEER, {}),
        (REMOTE_PEER, bearer()),
        (LOCAL_PEER, {"X-Forwarded-For": "192.0.2.10"}),
        (("::ffff:192.0.2.10", 1), {}),
    ):
        client = TestClient(web_client.app, base_url=LOOPBACK_URL, client=peer)
        response = client.get("/api/v1/health", headers=headers)
        assert response.status_code == 403, (peer, headers)
        assert "server.token_file" in response.json()["detail"]

    # With a token configured, loopback (IPv4, IPv6, IPv4-mapped) needs none, and keeps the
    # v1 Origin rule for POST.
    for peer, url in (
        (LOCAL_PEER, LOOPBACK_URL),
        (("::1", 50000), "http://[::1]:7787"),
        (("::ffff:127.0.0.1", 50000), "http://localhost:7787"),
    ):
        local = client_for(peer=peer, base_url=url)
        assert local.get("/api/v1/health").status_code == 200, peer
        assert local.get("/", headers=BROWSER).status_code == 200, peer
        response = local.post(f"/sessions/{NAMED}/meta", data={"label": "local"})
        assert response.status_code == 403, peer
        response = local.post(
            f"/sessions/{NAMED}/meta", data={"label": "local"}, headers={"Origin": url}
        )
        assert response.status_code == 200, peer
    assert label(vigil_env) == "local"
    # The DNS rebinding defence still holds for loopback.
    local = client_for(peer=LOCAL_PEER, base_url=LOOPBACK_URL)
    assert local.get("/api/v1/health", headers={"Host": "evil.example"}).status_code == 400


# --- scoped tokens ------------------------------------------------------------------------------


def test_check_bind_address_with_scoped_tokens(tmp_path: Path) -> None:
    read = write_token(tmp_path / "read", token="read-" + TOKEN)
    check_bind_address("192.0.2.5", ServerConfig(tokens=(TokenConfig(read, "read"),)))
    loose = write_token(tmp_path / "loose", token="loose-" + TOKEN, mode=0o644)
    with pytest.raises(BindRefused, match="0644; it must be 0600"):
        check_bind_address(
            "192.0.2.5",
            ServerConfig(tokens=(TokenConfig(read, "read"), TokenConfig(loose, "full"))),
        )
    with pytest.raises(BindRefused, match="127.0.0.1 only"):
        check_bind_address("192.0.2.5", ServerConfig())


READ_TOKEN = "read-token-" + "0123456789abcdef" * 2  # placeholders, not real secrets
PUSH_TOKEN = "push-token-" + "0123456789abcdef" * 2
DESK_TOKEN = "desk-token-" + "0123456789abcdef" * 2


@pytest.fixture
def scoped_app(vigil_env: VigilEnv, tmp_path: Path, token_file: Path) -> FastAPI:
    """The service with a full `token_file`, a read token and push tokens for `laptop` and
    `desk`, `REMOTE_HOST` allowed."""
    read = write_token(tmp_path / "read", READ_TOKEN)
    push = write_token(tmp_path / "push", PUSH_TOKEN)
    desk = write_token(tmp_path / "desk", DESK_TOKEN)
    write_config(
        vigil_env,
        f'token_file = "{token_file}"\nallowed_hosts = ["{REMOTE_HOST}"]\n\n'
        f'[[server.tokens]]\nfile = "{read}"\nscope = "read"\n\n'
        f'[[server.tokens]]\nfile = "{push}"\nscope = "push"\nmachine = "laptop"\n\n'
        f'[[server.tokens]]\nfile = "{desk}"\nscope = "push"\nmachine = "desk"',
    )
    conn = connect(vigil_env.index)
    scan(conn, vigil_env.claude_home)
    conn.close()
    return create_app(
        load_config(), conn_factory=lambda: connect(vigil_env.index), start_watcher=False
    )


def test_scope_matrix(scoped_app: FastAPI) -> None:
    """Every route of the app, from every kind of caller: the guard refuses exactly what
    `authorize` refuses, with the scope needed, and lets the rest reach the route."""
    remote = TestClient(scoped_app, base_url=REMOTE_URL, client=REMOTE_PEER)
    local = TestClient(scoped_app, base_url=LOOPBACK_URL, client=LOCAL_PEER)
    callers: dict[str, tuple[TestClient, Grant | None, dict[str, str]]] = {
        "none": (remote, None, {"Origin": REMOTE_URL}),
        "read": (remote, Grant("read"), bearer(READ_TOKEN)),
        "push laptop": (remote, Grant("push", "laptop"), bearer(PUSH_TOKEN)),
        "push desk": (remote, Grant("push", "desk"), bearer(DESK_TOKEN)),
        "full": (remote, FULL, bearer(TOKEN)),
        "local": (local, FULL, {"Origin": LOOPBACK_URL}),
    }
    # Every route, the included routers' and FastAPI's own (/docs, /openapi.json) alike. A
    # route without methods (a WebSocket route, a mount) would need its own rows here.
    routes = list(iter_route_contexts(scoped_app.routes))
    assert len(routes) > 30
    assert any(route.path == "/sessions/{session_id}/resume" for route in routes)
    verdicts: set[bool] = set()
    checked = 0
    for route in routes:
        assert route.path is not None and route.methods is not None, route
        path = re.sub(r"\{[^}]*\}", "x", route.path)
        for method in sorted({*route.methods, *(["HEAD"] if "GET" in route.methods else [])}):
            for who, (client, grant, headers) in callers.items():
                response = client.request(method, path, headers=headers, follow_redirects=False)
                where = f"{who} {method} {path}"
                # The guard's 401 asks for a token; a route's own 401 (a wrong token posted to
                # /login) does not. No route answers 403 itself.
                passed = response.status_code != 403 and "www-authenticate" not in response.headers
                if grant is None:
                    public = path in ("/login", "/logout") or path.startswith("/static/")
                    if public:
                        assert passed, where
                    else:
                        assert response.status_code == 401, where
                        assert "www-authenticate" in response.headers, where
                    continue
                refusal = authorize(grant, method, path)
                verdicts.add(refusal is None)
                if refusal is None:
                    assert passed, where
                else:
                    assert response.status_code == 403, where
                    if method != "HEAD":
                        assert response.json() == {"detail": refusal.reason}, where
                    assert refusal.reason.startswith("needs scope "), where
                checked += 1
    assert checked > 150
    assert verdicts == {True, False}


def test_read_token_cannot_log_in(client_for_scoped: ClientFactory) -> None:
    client = client_for_scoped()
    response = client.post(
        "/login", data={"token": READ_TOKEN, "next": "/"}, headers={"Origin": REMOTE_URL}
    )
    assert response.status_code == 401
    assert "not the token" in response.text
    assert "set-cookie" not in response.headers
    response = client.post(
        "/login", data={"token": PUSH_TOKEN, "next": "/"}, headers={"Origin": REMOTE_URL}
    )
    assert response.status_code == 401
    assert log_in(client).status_code == 303
    # The cookie is full access.
    assert client.get("/", headers=BROWSER).status_code == 200


def test_scoped_bearers(vigil_env: VigilEnv, client_for_scoped: ClientFactory) -> None:
    client = client_for_scoped()
    assert client.get("/api/v1/sessions", headers=bearer(READ_TOKEN)).status_code == 200
    response = client.get("/", headers={**BROWSER, **bearer(READ_TOKEN)})
    assert response.status_code == 403
    assert response.json() == {"detail": "needs scope full"}
    response = client.post(
        f"/sessions/{NAMED}/meta", data={"label": "read"}, headers=bearer(READ_TOKEN)
    )
    assert response.status_code == 403
    response = client.post(
        f"/sessions/{NAMED}/meta", data={"label": "push"}, headers=bearer(PUSH_TOKEN)
    )
    assert response.status_code == 403
    assert label(vigil_env) is None
    response = client.get("/api/v1/sessions", headers=bearer(PUSH_TOKEN))
    assert response.status_code == 403
    assert response.json() == {"detail": "needs scope read"}
    response = client.post(f"/sessions/{NAMED}/meta", data={"label": "full"}, headers=bearer())
    assert response.status_code == 200
    assert label(vigil_env) == "full"


def test_bearer_compares_every_credential(
    client_for_scoped: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    compared: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a: Any, b: Any) -> bool:
        compared.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    client = client_for_scoped()
    # Four credentials: each is compared, whether the token matches the first, the last or none.
    for token in (TOKEN, DESK_TOKEN, "x" * len(TOKEN)):
        compared.clear()
        client.get("/api/v1/health", headers=bearer(token))
        assert len(compared) == 4, token
        assert all(len(a) == len(b) == 32 for a, b in compared)


@pytest.fixture
def client_for_scoped(scoped_app: FastAPI) -> Iterator[ClientFactory]:
    clients: list[TestClient] = []

    def make(peer: tuple[str, int] = REMOTE_PEER, base_url: str = REMOTE_URL) -> TestClient:
        client = TestClient(scoped_app, base_url=base_url, client=peer, follow_redirects=False)
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


def test_duplicate_tokens_refused(vigil_env: VigilEnv, tmp_path: Path, token_file: Path) -> None:
    again = write_token(tmp_path / "again")
    write_config(
        vigil_env,
        f'token_file = "{token_file}"\n\n[[server.tokens]]\nfile = "{again}"\nscope = "read"',
    )
    with pytest.raises(AuthError, match=re.escape("server.tokens[1] holds the same token as")):
        create_app(load_config(), start_watcher=False)
    read = write_token(tmp_path / "read", READ_TOKEN)
    write_config(
        vigil_env,
        f'[[server.tokens]]\nfile = "{read}"\nscope = "read"\n\n'
        f'[[server.tokens]]\nfile = "{again}"\nscope = "full"\n\n'
        f'[[server.tokens]]\nfile = "{read}"\nscope = "push"\nmachine = "laptop"',
    )
    with pytest.raises(AuthError, match=re.escape("server.tokens[3] holds the same token as")):
        create_app(load_config(), start_watcher=False)


def test_existing_cookie_survives_upgrade(tmp_path: Path, token_file: Path) -> None:
    """A cookie signed the way vigil signed it before scopes (over the one token's digest) is
    still valid, also once other tokens are configured."""
    key = load_signing_key(tmp_path / "session.key")
    expires = int(time.time()) + 3600
    message = b"|".join(
        (b"vigil-session-v1", str(expires).encode(), hashlib.sha256(TOKEN.encode()).digest())
    )
    mac = base64.urlsafe_b64encode(hmac.new(key, message, hashlib.sha256).digest()).rstrip(b"=")
    cookie = f"{expires}.{mac.decode()}"
    read = write_token(tmp_path / "read", READ_TOKEN)
    for server in (
        ServerConfig(token_file=token_file),
        ServerConfig(token_file=token_file, tokens=(TokenConfig(read, "read"),)),
    ):
        auth = load_auth(server, tmp_path)
        assert auth is not None
        assert auth.cookie_ok(cookie, time.time())
    # With two full tokens the cookie is signed over both, so adding one logs browsers out.
    other = write_token(tmp_path / "other", DESK_TOKEN)
    auth = load_auth(
        ServerConfig(token_file=token_file, tokens=(TokenConfig(other, "full"),)), tmp_path
    )
    assert auth is not None
    assert not auth.cookie_ok(cookie, time.time())
    assert auth.cookie_ok(auth.issue_cookie(time.time()), time.time())


def test_no_full_token_no_login(vigil_env: VigilEnv, tmp_path: Path) -> None:
    read = write_token(tmp_path / "read", READ_TOKEN)
    write_config(
        vigil_env,
        f'allowed_hosts = ["{REMOTE_HOST}"]\n\n[[server.tokens]]\nfile = "{read}"\nscope = "read"',
    )
    app = create_app(load_config(), start_watcher=False)
    client = TestClient(app, base_url=REMOTE_URL, client=REMOTE_PEER, follow_redirects=False)
    detail = {"detail": "no vigil login here: sign in through Cloudflare Access"}
    response = client.get("/login")
    assert (response.status_code, response.json()) == (404, detail)
    response = client.post("/login", data={"token": READ_TOKEN}, headers={"Origin": REMOTE_URL})
    assert (response.status_code, response.json()) == (404, detail)
    # A browser is not sent to a login page that is not there.
    assert client.get("/", headers=BROWSER).status_code == 401
    assert client.get("/api/v1/health", headers=bearer(READ_TOKEN)).status_code == 200
    # No cookie is ever good without a full token.
    auth = app.state.vigil_auth
    assert not auth.can_log_in
    assert not auth.cookie_ok(auth.issue_cookie(time.time()), time.time())


def test_credential_repr_hides_token() -> None:
    credential = Credential(TOKEN, Grant("read"))
    assert TOKEN not in repr(credential)
    assert TOKEN not in repr(AuthSettings(credentials=(credential,), signing_key=b"k" * 32))


# --- the login cookie -------------------------------------------------------------------------


def test_cookie_is_samesite_lax(client_for: ClientFactory) -> None:
    client = client_for()
    response = log_in(client)
    assert response.status_code == 303
    assert "samesite=lax" in response.headers["set-cookie"].lower()
    response = client.post("/logout", headers={"Origin": REMOTE_URL})
    assert "samesite=lax" in response.headers["set-cookie"].lower()


def test_cross_site_post_with_cookie_refused(
    vigil_env: VigilEnv, client_for: ClientFactory
) -> None:
    """Lax sends the cookie on a top-level navigation from another site; a POST from there is
    still refused for its Origin."""
    client = client_for()
    log_in(client)
    response = client.post(
        f"/sessions/{NAMED}/meta",
        data={"label": "cross-site"},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert label(vigil_env) is None
    # A link from another site opens pages signed in.
    assert (
        client.get("/", headers={**BROWSER, "Referer": "https://hub.example.com/"}).status_code
        == 200
    )
