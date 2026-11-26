"""The push client (`vigil.push`): this machine's snapshot, sent to a hub."""

import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import vigil
from tests.conftest import FakeBin
from vigil.config import Config
from vigil.index.queries import set_meta
from vigil.index.scanner import link_chains
from vigil.push import SNAPSHOT_DAYS, build_snapshot
from vigil.web import LiveStatus

NAMED = "e47d2def-0bb3-4d50-9f9c-8e38e4cdcfba"
NEWEST = "f26f1b2c-d3b2-4389-b0ed-aba7e19e8594"
PLAIN = "7fb815ab-28b5-4123-85d0-64e6fd3b4141"
FORKED = "68e04fa1-b95c-4610-8221-261395d0ca5d"
HANDMADE = "11111111-1111-4111-8111-111111111111"
NEWEST_ACTIVITY = datetime(2026, 11, 17, 15, 8, 21, 286000, tzinfo=UTC)
"""When the newest fixture session was last active."""
JUST_AFTER = NEWEST_ACTIVITY + timedelta(minutes=1)
LAPTOP = Config(machine="laptop")

_HELP = "2.1.312 (Claude Code)\n  --bg  Start in the background\n  attach\n  agents\n"
_AGENTS_CLAUDE = """#!{python}
import json, os, sys
with open({log!r}, "a") as handle:
    handle.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd()}}) + "\\n")
if "agents" in sys.argv[1:]:
    with open({agents!r}) as handle:
        sys.stdout.write(handle.read())
else:
    sys.stdout.write({help!r})
"""


def install_agents_claude(fake_bin: FakeBin, live: list[dict[str, Any]]) -> Path:
    """A fake `claude` whose `agents --json` prints what the returned file holds (`live` at
    first; rewrite the file to change it). It records its argv like every FakeBin program."""
    fake_bin.install("claude")
    agents = fake_bin.directory / ".agents.json"
    agents.write_text(json.dumps(live))
    (fake_bin.directory / "claude").write_text(
        _AGENTS_CLAUDE.format(
            python=sys.executable,
            log=str(fake_bin.directory / ".calls" / "claude.jsonl"),
            agents=str(agents),
            help=_HELP,
        )
    )
    return agents


def ids(snapshot: dict[str, Any]) -> list[str]:
    return [session["id"] for session in snapshot["sessions"]]


def test_snapshot_contents(index_db: sqlite3.Connection, fake_bin: FakeBin) -> None:
    with index_db:
        index_db.execute(
            "UPDATE sessions SET previous_session_id = ? WHERE session_id = ?", (PLAIN, FORKED)
        )
        link_chains(index_db)
    set_meta(index_db, NAMED, archived=True)
    live = LiveStatus()

    snapshot = build_snapshot(index_db, LAPTOP, live, JUST_AFTER)
    assert snapshot["format"] == 1
    assert snapshot["machine"] == "laptop"
    assert snapshot["generated_at"].startswith("2026-11-17T15:09:21")
    assert snapshot["vigil_version"] == vigil.__version__
    assert snapshot["live_checked_at"] is None
    sent = ids(snapshot)
    assert sent[0] == NEWEST  # newest activity first
    assert FORKED in sent and PLAIN not in sent  # one per chain, by its newest session
    assert NAMED not in sent  # archived
    assert HANDMADE in sent and len(sent) == 5
    assert all(session["machine"] == "laptop" for session in snapshot["sessions"])
    assert all(isinstance(session["remote_urls"], list) for session in snapshot["sessions"])
    assert {session["status"] for session in snapshot["sessions"]} == {"unknown"}  # no probe
    assert fake_bin.calls("claude") == []

    # Only the last 30 days: HANDMADE (10:00 that day) falls out when `now` is 30 days after noon.
    noon = datetime(2026, 11, 17, 12, 0, tzinfo=UTC) + timedelta(days=SNAPSHOT_DAYS)
    assert HANDMADE not in ids(build_snapshot(index_db, LAPTOP, live, noon))
    assert len(ids(build_snapshot(index_db, LAPTOP, live, noon))) == 4
    assert build_snapshot(index_db, LAPTOP, live, JUST_AFTER + timedelta(days=31))["sessions"] == []


def test_snapshot_uses_page_probe(index_db: sqlite3.Connection, fake_bin: FakeBin) -> None:
    install_agents_claude(fake_bin, [{"sessionId": NEWEST, "kind": "background", "status": "busy"}])
    wall = datetime(2026, 11, 17, 15, 9, tzinfo=UTC)
    live = LiveStatus(wall=lambda: wall)
    live.get()  # as a page would
    calls = len(fake_bin.calls("claude"))

    snapshot = build_snapshot(index_db, LAPTOP, live, JUST_AFTER)
    assert snapshot["live_checked_at"] == "2026-11-17T15:09:00.000000+00:00"
    by_id = {session["id"]: session for session in snapshot["sessions"]}
    assert by_id[NEWEST]["status"] == "background" and by_id[NEWEST]["busy"] is True
    assert by_id[NAMED]["status"] == "idle"
    assert len(fake_bin.calls("claude")) == calls  # no new probe


# HubPusher (vigil hub, Task 18).
import shutil  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from tests.conftest import PUSH_LAPTOP, FakeHub, HubEnv, HubRelay, snapshot_body  # noqa: E402
from vigil.config import HubClientConfig, load_config  # noqa: E402
from vigil.hub import HubStore, canonical_digest  # noqa: E402
from vigil.index.db import to_db  # noqa: E402
from vigil.push import (  # noqa: E402
    BACKOFF_MAX,
    BACKOFF_START,
    HEARTBEAT_EVERY,
    PUSH_MIN_INTERVAL,
    HubPusher,
)
from vigil.web.app import create_app  # noqa: E402

HUB = "https://hub.example.com"
WALL = datetime(2026, 11, 25, 18, 5, tzinfo=UTC)
ACCESS_ID = "client-id-placeholder.access"
ACCESS_SECRET = "access-secret-placeholder-" + "5" * 16
PUT = ("PUT", "/ingest/laptop/sessions")
HEARTBEAT = ("POST", "/ingest/laptop/heartbeat")


class FakeClock:
    """A monotonic clock that moves only when told to."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def private_file(path: Path, text: str) -> Path:
    path.write_text(text + "\n")
    path.chmod(0o600)
    return path


def bearer(tmp_path: Path) -> HubClientConfig:
    return HubClientConfig(url=HUB, token_file=private_file(tmp_path / "hub-token", PUSH_LAPTOP))


def access(tmp_path: Path) -> HubClientConfig:
    secret = private_file(tmp_path / "hub-secret", ACCESS_SECRET)
    return HubClientConfig(url=HUB, access_client_id=ACCESS_ID, access_secret_file=secret)


def make_pusher(
    config: HubClientConfig, transport: httpx.BaseTransport, clock: FakeClock
) -> HubPusher:
    return HubPusher(config, "laptop", transport=transport, clock=clock, wall=lambda: WALL)


def snapshot(title: str = "placeholder 0") -> dict[str, Any]:
    """`snapshot_body()` with its first session titled `title`."""
    body = snapshot_body()
    body["sessions"][0]["title"] = title
    body["sessions"][0]["name"] = title
    return body


def test_push_on_change(tmp_path: Path) -> None:
    hub, clock = FakeHub(), FakeClock()
    pusher = make_pusher(bearer(tmp_path), hub, clock)
    a = snapshot()
    pusher.offer(a)
    pusher.step()
    assert hub.sent() == [PUT]
    assert json.loads(hub.requests[0].content) == a
    assert hub.requests[0].headers["content-type"] == "application/json"
    pusher.offer(json.loads(json.dumps(a)))  # the same sessions again
    clock.advance(10)
    pusher.step()
    assert hub.sent() == [PUT]
    status = pusher.status()
    assert status.url == HUB and status.last_push_at == WALL and status.last_error is None


def test_push_debounced(tmp_path: Path) -> None:
    hub, clock = FakeHub(), FakeClock()
    pusher = make_pusher(bearer(tmp_path), hub, clock)
    pusher.offer(snapshot())
    pusher.step()
    clock.advance(2)
    b = snapshot("changed")
    pusher.offer(b)
    assert pusher.step() == pytest.approx(PUSH_MIN_INTERVAL - 2)
    assert hub.sent() == [PUT]
    clock.advance(2.9)
    pusher.step()
    assert hub.sent() == [PUT]
    clock.advance(0.1)
    pusher.step()
    assert hub.sent() == [PUT, PUT]
    assert json.loads(hub.requests[1].content) == b


def test_heartbeat_when_idle(tmp_path: Path) -> None:
    hub, clock = FakeHub(), FakeClock()
    pusher = make_pusher(bearer(tmp_path), hub, clock)
    a = snapshot()
    pusher.offer(a)
    assert pusher.step() == pytest.approx(HEARTBEAT_EVERY)
    clock.advance(59)
    assert pusher.step() == pytest.approx(1)
    assert hub.sent() == [PUT]
    clock.advance(1)
    assert pusher.step() == pytest.approx(HEARTBEAT_EVERY)
    assert hub.sent() == [PUT, HEARTBEAT]
    assert json.loads(hub.requests[1].content) == {
        "generated_at": to_db(WALL),
        "digest": canonical_digest(a["sessions"]),
    }
    clock.advance(HEARTBEAT_EVERY)
    pusher.step()
    assert hub.sent() == [PUT, HEARTBEAT, HEARTBEAT]


def test_heartbeat_409_sends_snapshot(tmp_path: Path) -> None:
    hub, clock = FakeHub(), FakeClock()
    pusher = make_pusher(bearer(tmp_path), hub, clock)
    a = snapshot()
    pusher.offer(a)
    pusher.step()
    clock.advance(HEARTBEAT_EVERY)
    hub.script = [409]
    pusher.step()
    assert hub.sent() == [PUT, HEARTBEAT, PUT]
    assert json.loads(hub.requests[2].content) == a
    assert pusher.status().last_error is None
    clock.advance(HEARTBEAT_EVERY)
    pusher.step()
    assert hub.sent()[-1] == HEARTBEAT


@pytest.mark.parametrize("failure", [503, httpx.ConnectError, httpx.ConnectTimeout])
def test_backoff(tmp_path: Path, failure: int | type[httpx.TransportError]) -> None:
    hub, clock = FakeHub(), FakeClock()
    pusher = make_pusher(bearer(tmp_path), hub, clock)
    hub.fail = failure
    pusher.offer(snapshot())
    assert pusher.step() == pytest.approx(BACKOFF_START)
    assert hub.sent() == [PUT]
    first_error = pusher.status().last_error
    assert first_error is not None and first_error.startswith("push: ")
    assert pusher.status().last_push_at is None
    tries = 1
    for wait in (5, 10, 20, 40, 80, 160, 300, 300, 300):
        clock.advance(wait - 0.5)
        assert pusher.step() == pytest.approx(0.5)  # nothing during back-off
        assert len(hub.requests) == tries
        clock.advance(0.5)
        delay = pusher.step()
        tries += 1
        assert len(hub.requests) == tries
        assert delay == pytest.approx(min(wait * 2, BACKOFF_MAX))
    # The hub is back: the next try works and clears the error.
    hub.fail = None
    clock.advance(BACKOFF_MAX)
    pusher.step()
    assert len(hub.requests) == tries + 1
    status = pusher.status()
    assert status.last_error is None and status.last_push_at == WALL
    # A new failure backs off from the start again.
    hub.fail = failure
    pusher.offer(snapshot("changed"))
    clock.advance(PUSH_MIN_INTERVAL)
    assert pusher.step() == pytest.approx(BACKOFF_START)
    assert pusher.status().last_error is not None


def test_heartbeat_failure_backs_off(tmp_path: Path) -> None:
    hub, clock = FakeHub(), FakeClock()
    pusher = make_pusher(bearer(tmp_path), hub, clock)
    pusher.offer(snapshot())
    pusher.step()
    clock.advance(HEARTBEAT_EVERY)
    hub.fail = 500
    assert pusher.step() == pytest.approx(BACKOFF_START)
    error = pusher.status().last_error
    assert error is not None and error.startswith("heartbeat: ") and "500" in error
    assert pusher.status().last_push_at == WALL  # the last accepted push is still shown
    clock.advance(BACKOFF_START)
    pusher.step()
    assert hub.sent() == [PUT, HEARTBEAT, HEARTBEAT]


def test_redirect_not_followed(tmp_path: Path) -> None:
    hub, clock = FakeHub(), FakeClock()
    pusher = make_pusher(bearer(tmp_path), hub, clock)
    hub.script = [httpx.Response(307, headers={"Location": "https://other.example/"})]
    pusher.offer(snapshot())
    pusher.step()
    error = pusher.status().last_error
    assert error is not None and "redirect" in error
    assert [request.url.host for request in hub.requests] == ["hub.example.com"]
    clock.advance(BACKOFF_START)
    pusher.step()
    assert [request.url.host for request in hub.requests] == ["hub.example.com"] * 2


def test_credentials_only_to_hub(tmp_path: Path) -> None:
    for config in (bearer(tmp_path), access(tmp_path)):
        hub, clock = FakeHub(), FakeClock()
        pusher = make_pusher(config, hub, clock)
        pusher.offer(snapshot())
        pusher.step()
        clock.advance(HEARTBEAT_EVERY)
        pusher.step()
        assert hub.sent() == [PUT, HEARTBEAT]
        for request in hub.requests:
            assert (request.url.scheme, request.url.host, request.url.port) == (
                "https",
                "hub.example.com",
                None,
            )
            headers = request.headers
            if config.token_file is not None:
                assert headers["authorization"] == f"Bearer {PUSH_LAPTOP}"
                assert not [name for name in headers if name.lower().startswith("cf-access")]
            else:
                assert headers["cf-access-client-id"] == ACCESS_ID
                assert headers["cf-access-client-secret"] == ACCESS_SECRET
                assert "authorization" not in headers


def test_last_error_never_holds_the_credential(tmp_path: Path) -> None:
    def echo_credentials(request: httpx.Request) -> httpx.Response:
        sent = request.headers.get("authorization") or request.headers["cf-access-client-secret"]
        raise httpx.ConnectError(f"refused while sending {sent}", request=request)

    for config, secret in ((bearer(tmp_path), PUSH_LAPTOP), (access(tmp_path), ACCESS_SECRET)):
        pusher = make_pusher(config, httpx.MockTransport(echo_credentials), FakeClock())
        pusher.offer(snapshot())
        pusher.step()
        error = pusher.status().last_error
        assert error is not None and "***" in error and secret not in error


def test_unusable_token_file_sends_nothing(tmp_path: Path) -> None:
    hub, clock = FakeHub(), FakeClock()
    config = bearer(tmp_path)
    assert config.token_file is not None
    config.token_file.chmod(0o644)
    pusher = make_pusher(config, hub, clock)
    pusher.offer(snapshot())
    assert pusher.step() == pytest.approx(BACKOFF_START)
    assert hub.requests == []
    error = pusher.status().last_error
    assert error is not None and "token file" in error and PUSH_LAPTOP not in error


def hub_store(app: FastAPI) -> HubStore:
    store = app.state.vigil.hub
    assert isinstance(store, HubStore)
    return store


def laptop_sessions(app: FastAPI) -> int:
    (laptop,) = [state for state in hub_store(app).machines() if state.name == "laptop"]
    assert laptop.last_snapshot_at is not None
    return laptop.session_count


def test_hub_restart_recovers(
    hub_env: HubEnv,
    hub_app: FastAPI,
    hub_transport: HubRelay,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    pusher = HubPusher(bearer(tmp_path), "laptop", transport=hub_transport, clock=clock)
    a = snapshot()
    pusher.offer(a)
    pusher.step()
    assert pusher.status().last_error is None
    assert laptop_sessions(hub_app) == len(a["sessions"])

    # The hub goes down and comes back on an empty data dir.
    hub_transport.down = True
    clock.advance(HEARTBEAT_EVERY)
    assert pusher.step() == pytest.approx(BACKOFF_START)
    error = pusher.status().last_error
    assert error is not None and "cannot reach the hub" in error
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "empty-data"))
    fresh = create_app(load_config(hub_env.config_path))
    hub_transport.restart(fresh)
    hub_transport.down = False
    assert hub_store(fresh).sessions() == []

    clock.advance(BACKOFF_START)
    pusher.step()  # heartbeat, 409, then the snapshot in the same step
    assert pusher.status().last_error is None
    assert laptop_sessions(fresh) == len(a["sessions"])
    assert {row.title for row in hub_store(fresh).sessions()} == {"placeholder 0", "placeholder 1"}


def test_hub_restart_with_older_snapshot_recovers(
    hub_env: HubEnv,
    hub_app: FastAPI,
    hub_transport: HubRelay,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    pusher = HubPusher(bearer(tmp_path), "laptop", transport=hub_transport, clock=clock)
    pusher.offer(snapshot())
    pusher.step()
    older = tmp_path / "older-data"
    shutil.copytree(hub_env.data_dir / "hub", older / "vigil" / "hub")
    pusher.offer(snapshot("newer"))
    clock.advance(PUSH_MIN_INTERVAL)
    pusher.step()
    assert "newer" in {row.title for row in hub_store(hub_app).sessions()}

    monkeypatch.setenv("XDG_DATA_HOME", str(older))
    fresh = create_app(load_config(hub_env.config_path))
    hub_transport.restart(fresh)
    assert "newer" not in {row.title for row in hub_store(fresh).sessions()}
    clock.advance(HEARTBEAT_EVERY)
    pusher.step()
    assert pusher.status().last_error is None
    assert "newer" in {row.title for row in hub_store(fresh).sessions()}
