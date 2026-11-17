import asyncio
from collections.abc import MutableMapping
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import VigilEnv
from vigil import cli
from vigil.web.security import (
    BindRefused,
    LoopbackGuard,
    check_bind_address,
    host_name,
    is_loopback_host,
    is_same_origin,
)


@pytest.mark.parametrize(
    ("host", "name"),
    [
        ("127.0.0.1:7787", "127.0.0.1"),
        ("LOCALHOST", "localhost"),
        ("[::1]:7787", "[::1]"),
        ("[::1]", "[::1]"),
        ("localhost:", None),
        ("localhost:http", None),
        ("[::1", None),
        ("[::1]x", None),
        (":7787", None),
        ("", None),
    ],
)
def test_host_name(host: str, name: str | None) -> None:
    assert host_name(host) == name


@pytest.mark.parametrize(
    ("host", "ok"),
    [
        ("127.0.0.1:7787", True),
        ("localhost:7787", True),
        ("[::1]:7787", True),
        (None, False),
        ("evil.example", False),
        ("127.0.0.1.evil.example", False),
        ("localhost.evil.example:7787", False),
        ("127.0.0.2", False),  # loopback range, but not a name vigil answers on
        ("0.0.0.0", False),
        ("[::ffff:127.0.0.1]", False),
    ],
)
def test_is_loopback_host(host: str | None, ok: bool) -> None:
    assert is_loopback_host(host) is ok


@pytest.mark.parametrize(
    ("origin", "ok"),
    [
        ("http://127.0.0.1:7787", True),
        ("HTTP://127.0.0.1:7787", True),
        ("https://127.0.0.1:7787", True),
        ("http://127.0.0.1:7787/", True),
        (None, False),
        ("", False),
        ("null", False),
        ("http://127.0.0.1", False),
        ("http://127.0.0.1:7788", False),
        ("http://evil.example", False),
        ("http://127.0.0.1:7787.evil.example", False),
        ("http://127.0.0.1:7787/path", False),
        ("ftp://127.0.0.1:7787", False),
        ("http://[::1", False),
    ],
)
def test_is_same_origin(origin: str | None, ok: bool) -> None:
    assert is_same_origin(origin, "127.0.0.1:7787") is ok


def guarded_app() -> FastAPI:
    app = FastAPI()

    @app.get("/thing")
    def read() -> dict[str, bool]:
        return {"read": True}

    @app.post("/thing")
    def write() -> dict[str, bool]:
        return {"written": True}

    @app.delete("/thing")
    def remove() -> dict[str, bool]:
        return {"removed": True}

    app.add_middleware(LoopbackGuard)
    return app


def test_guard_checks_every_route() -> None:
    client = TestClient(guarded_app(), base_url="http://127.0.0.1:7787")
    assert client.get("/thing").json() == {"read": True}
    assert client.head("/thing").status_code not in (400, 403)  # passes the guard
    # Unknown routes are guarded too: nothing reaches the app from a foreign Host.
    assert client.get("/nothing", headers={"Host": "evil.example"}).status_code == 400
    assert client.post("/thing").status_code == 403
    assert client.delete("/thing").status_code == 403
    same_origin = {"Origin": "http://127.0.0.1:7787"}
    assert client.post("/thing", headers=same_origin).json() == {"written": True}
    assert client.delete("/thing", headers=same_origin).json() == {"removed": True}


def test_host_rebinding_rejected_without_a_host_header() -> None:
    sent: list[MutableMapping[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    async def app(scope: Any, receive: Any, send: Any) -> None:
        raise AssertionError("the guard let a request without Host through")

    scope = {"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""}
    asyncio.run(LoopbackGuard(app)(scope, receive, send))
    assert sent[0]["status"] == 400


def test_check_bind_address() -> None:
    check_bind_address("127.0.0.1")
    for host in ("0.0.0.0", "::", "192.0.2.1", "localhost", "::1"):
        with pytest.raises(BindRefused, match="127.0.0.1 only"):
            check_bind_address(host)


def test_serve_refuses_non_loopback(
    vigil_env: VigilEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runs: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: runs.append(kwargs))
    for host in ("0.0.0.0", "192.0.2.1", "::"):
        assert cli.main(["serve", "--host", host]) == 2
        assert f"refusing to listen on {host}" in capsys.readouterr().err
    assert runs == []


def test_serve_binds_loopback_on_the_configured_port(
    vigil_env: VigilEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: runs.append(kwargs))
    assert cli.main(["serve"]) == 0
    assert cli.main(["serve", "--port", "9000"]) == 0
    expected = [("127.0.0.1", 7787), ("127.0.0.1", 9000)]
    assert [(run["host"], run["port"]) for run in runs] == expected
