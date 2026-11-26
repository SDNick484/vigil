import json
import os
import sqlite3
import sys
import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import NO_JOB, VigilEnv, freeze_tree, thaw_tree
from vigil.claude_cli import LiveSession
from vigil.config import load_config
from vigil.index.db import connect
from vigil.index.queries import SessionRow, get_session, list_sessions
from vigil.index.scanner import link_chains, scan
from vigil.resume import stop_session
from vigil.serialize import open_link, session_to_dict
from vigil.web.api import SessionOut
from vigil.web.app import create_app

NAMED = "e47d2def-0bb3-4d50-9f9c-8e38e4cdcfba"  # titled "fixture-named"
NEWEST = "f26f1b2c-d3b2-4389-b0ed-aba7e19e8594"
PLAIN = "7fb815ab-28b5-4123-85d0-64e6fd3b4141"
FORKED = "68e04fa1-b95c-4610-8221-261395d0ca5d"
HANDMADE = "11111111-1111-4111-8111-111111111111"

# A fake `claude` that prints this for every call reports version 2.1.312 with `--bg`, `attach`
# and `agents` (and no live sessions: `agents --json` gets this text, which is not JSON).
CLAUDE_BG = "2.1.312 (Claude Code)\n  --bg  Start in the background\n  attach\n  agents\n"


def sql(env: VigilEnv, statement: str, *params: object) -> None:
    conn = sqlite3.connect(env.index)
    with conn:
        conn.execute(statement, params)
    conn.close()


def ids(response: Any) -> list[str]:
    assert response.status_code == 200, response.text
    return [session["id"] for session in response.json()]


def stored(env: VigilEnv, session_id: str) -> dict[str, object]:
    conn = connect(env.index)
    row = get_session(conn, session_id)
    conn.close()
    assert row is not None
    return session_to_dict(row)


def row_of(env: VigilEnv, session_id: str) -> SessionRow:
    conn = connect(env.index)
    row = get_session(conn, session_id)
    conn.close()
    assert row is not None
    return row


def test_session_out_matches_session_to_dict(vigil_env: VigilEnv, web_client: TestClient) -> None:
    assert set(SessionOut.model_fields) == set(stored(vigil_env, NAMED))


def test_sessions_list_and_filters(vigil_env: VigilEnv, web_client: TestClient) -> None:
    response = web_client.get("/api/v1/sessions")
    sessions = response.json()
    assert len(sessions) == 7
    assert sessions[0]["id"] == NEWEST  # newest activity first
    named = next(s for s in sessions if s["id"] == NAMED)
    assert named == stored(vigil_env, NAMED)  # exactly what `vigil ls --json` prints
    assert named["status"] == "unknown"  # no claude on PATH

    assert ids(web_client.get("/api/v1/sessions", params={"q": "fixture named"})) == [NAMED]
    assert ids(web_client.get("/api/v1/sessions", params={"q": "nothing-matches"})) == []
    by_project = {"project": "/home/user/src/broken-repo"}
    assert ids(web_client.get("/api/v1/sessions", params=by_project)) == [HANDMADE]
    by_cwd = {"cwd": "/home/user/src/broken-repo"}
    assert ids(web_client.get("/api/v1/sessions", params=by_cwd)) == [HANDMADE]
    assert ids(web_client.get("/api/v1/sessions", params={"limit": 2})) == [
        s["id"] for s in sessions[:2]
    ]

    since = sessions[2]["last_activity_at"]
    assert ids(web_client.get("/api/v1/sessions", params={"since": since})) == [
        s["id"] for s in sessions[:3]
    ]

    assert len(ids(web_client.get("/api/v1/sessions", params={"status": "unknown"}))) == 7
    assert ids(web_client.get("/api/v1/sessions", params={"status": "idle"})) == []
    assert web_client.get("/api/v1/sessions", params={"status": "asleep"}).status_code == 422
    assert web_client.get("/api/v1/sessions", params={"limit": 0}).status_code == 422

    # Archived sessions only with `all`.
    sql(vigil_env, "INSERT INTO session_meta (session_id, archived, updated_at) VALUES (?, 1, '')",
        NAMED)  # fmt: skip
    assert NAMED not in ids(web_client.get("/api/v1/sessions"))
    assert NAMED in ids(web_client.get("/api/v1/sessions", params={"all": "true"}))


def test_sessions_status_from_live_sessions(vigil_env: VigilEnv, web_client: TestClient) -> None:
    vigil_env.bin.install("claude", stdout=CLAUDE_BG)
    sessions = web_client.get("/api/v1/sessions").json()
    assert {s["status"] for s in sessions} == {"idle"}
    assert len(ids(web_client.get("/api/v1/sessions", params={"status": "idle"}))) == 7
    # Status is cached: a burst of requests probes claude once.
    probes = len(vigil_env.bin.calls("claude"))
    web_client.get("/api/v1/sessions")
    web_client.get(f"/api/v1/sessions/{NAMED}")
    assert len(vigil_env.bin.calls("claude")) == probes


def test_session_detail_has_chain(vigil_env: VigilEnv, web_client: TestClient) -> None:
    conn = connect(vigil_env.index)
    with conn:
        conn.execute(
            "UPDATE sessions SET previous_session_id = ? WHERE session_id = ?", (PLAIN, FORKED)
        )
        link_chains(conn)
    conn.close()

    response = web_client.get(f"/api/v1/sessions/{FORKED}")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == FORKED
    assert data["previous_id"] == PLAIN
    assert data["chain_id"] == PLAIN
    assert [link["id"] for link in data["chain"]] == [PLAIN, FORKED]  # oldest first
    assert data["chain"][0] == stored(vigil_env, PLAIN)
    assert data["resume_command"].endswith(f"claude --resume {FORKED}")
    assert {key: data[key] for key in stored(vigil_env, FORKED)} == stored(vigil_env, FORKED)
    # The list shows the chain once, by its newest session.
    listed = ids(web_client.get("/api/v1/sessions"))
    assert FORKED in listed and PLAIN not in listed
    assert PLAIN in ids(web_client.get("/api/v1/sessions", params={"all": "true"}))


def test_session_detail_by_prefix_and_unknown(web_client: TestClient) -> None:
    detail = web_client.get(f"/api/v1/sessions/{NAMED}").json()
    assert detail["resume_command"] == (
        f"cd /home/user/src/fixture-repo && claude --resume {NAMED}"
    )
    assert web_client.get(f"/api/v1/sessions/{NAMED[:8]}").json() == detail
    response = web_client.get("/api/v1/sessions/no-such-session")
    assert response.status_code == 404
    assert "no-such-session" in response.json()["detail"]


def test_projects(vigil_env: VigilEnv, web_client: TestClient) -> None:
    response = web_client.get("/api/v1/projects")
    assert response.status_code == 200
    projects = response.json()
    assert {p["label"] for p in projects} == {"fixture-repo", "broken-repo"}
    assert sum(p["session_count"] for p in projects) == 7
    broken = next(p for p in projects if p["label"] == "broken-repo")
    assert broken == {
        "project_key": "/home/user/src/broken-repo",
        "label": "broken-repo",
        "session_count": 1,
        "last_activity_at": stored(vigil_env, HANDMADE)["last_activity_at"],
    }


def test_health(vigil_env: VigilEnv, web_client: TestClient) -> None:
    response = web_client.get("/api/v1/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["version"] == "0.1.0"
    assert data["claude_version"] is None  # no claude on PATH
    assert data["indexed_sessions"] == 7
    assert data["last_scan_at"] is None  # this app runs no watcher


def test_health_reports_claude_version(vigil_env: VigilEnv, web_client: TestClient) -> None:
    vigil_env.bin.install("claude", stdout=CLAUDE_BG)
    assert web_client.get("/api/v1/health").json()["claude_version"] == "2.1.312"


def test_host_rebinding_rejected(web_client: TestClient) -> None:
    for host in ("evil.example", "evil.example:7787", "127.0.0.1.evil.example", "localhost.:80"):
        response = web_client.get("/api/v1/health", headers={"Host": host})
        assert response.status_code == 400, host
        assert "Host" in response.json()["detail"]
        response = web_client.post(f"/sessions/{NAMED}/meta", headers={"Host": host})
        assert response.status_code == 400, host
    for host in ("localhost:7787", "[::1]:7787", "127.0.0.1"):
        assert web_client.get("/api/v1/health", headers={"Host": host}).status_code == 200


def test_post_without_origin_rejected(vigil_env: VigilEnv, web_client: TestClient) -> None:
    del web_client.headers["origin"]
    response = web_client.post(f"/sessions/{NAMED}/meta", data={"label": "x"})
    assert response.status_code == 403
    assert stored(vigil_env, NAMED)["label"] is None
    vigil_env.bin.install("claude", stdout=CLAUDE_BG)
    assert web_client.post(f"/sessions/{NAMED}/resume").status_code == 403
    assert vigil_env.bin.calls("claude") == []
    # Reads need no Origin.
    assert web_client.get("/api/v1/health").status_code == 200


def test_post_cross_origin_rejected(vigil_env: VigilEnv, web_client: TestClient) -> None:
    for origin in (
        "http://evil.example",
        "null",
        "http://localhost:7787",  # loopback, but not the Host this request was sent to
        "http://127.0.0.1:7788",
        "file://",
    ):
        response = web_client.post(
            f"/sessions/{NAMED}/meta", data={"label": "x"}, headers={"Origin": origin}
        )
        assert response.status_code == 403, origin
    assert stored(vigil_env, NAMED)["label"] is None
    # The same origin under another loopback name is fine.
    response = web_client.post(
        f"/sessions/{NAMED}/meta",
        data={"label": "x"},
        headers={"Host": "localhost:7787", "Origin": "http://localhost:7787"},
    )
    assert response.status_code == 200


def test_resume_ok_returns_attach(
    vigil_env: VigilEnv, web_client: TestClient, tmp_path: Path
) -> None:
    folder = tmp_path / "work"
    folder.mkdir()
    sql(vigil_env, "UPDATE sessions SET cwd = ? WHERE session_id = ?", str(folder), NAMED)
    vigil_env.bin.install("claude", stdout=CLAUDE_BG)

    response = web_client.post(f"/sessions/{NAMED}/resume")
    assert response.status_code == 200
    # No display in the test environment, so no terminal: the caller shows the attach command.
    assert response.json() == {
        "ok": True,
        "attach": f"claude attach {NAMED[:8]}",
        "opened_terminal": False,
        "remote_url": None,
        "pending": False,
        "at_desk": True,
    }
    started = [c for c in vigil_env.bin.calls("claude") if "--resume" in c.argv]
    assert [(c.argv, c.cwd) for c in started] == [
        (["--bg", "--resume", NAMED, "--remote-control"], str(folder))
    ]
    conn = connect(vigil_env.index)
    launches = conn.execute("SELECT source, session_id FROM launches").fetchall()
    conn.close()
    assert [tuple(row) for row in launches] == [("vigil", NAMED)]


def test_resume_opens_terminal_when_there_is_one(
    vigil_env: VigilEnv, web_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sql(vigil_env, "UPDATE sessions SET cwd = ? WHERE session_id = ?", str(tmp_path), NAMED)
    vigil_env.bin.install("claude", stdout=CLAUDE_BG)
    vigil_env.bin.install("xterm")
    monkeypatch.setenv("DISPLAY", ":99")
    response = web_client.post(f"/sessions/{NAMED}/resume")
    assert response.json() == {
        "ok": True,
        "attach": f"claude attach {NAMED[:8]}",
        "opened_terminal": True,
        "remote_url": None,
        "pending": False,
        "at_desk": True,
    }


def test_resume_refused_409(vigil_env: VigilEnv, web_client: TestClient) -> None:
    vigil_env.bin.install("claude", stdout=CLAUDE_BG)
    response = web_client.post(f"/sessions/{NAMED}/resume")
    assert response.status_code == 409
    assert response.json() == {
        "ok": False,
        "reason": "folder no longer exists: /home/user/src/fixture-repo",
        "restartable": False,
    }
    assert all("--resume" not in call.argv for call in vigil_env.bin.calls("claude"))


def test_resume_without_claude_409(
    vigil_env: VigilEnv, web_client: TestClient, tmp_path: Path
) -> None:
    sql(vigil_env, "UPDATE sessions SET cwd = ? WHERE session_id = ?", str(tmp_path), NAMED)
    response = web_client.post(f"/sessions/{NAMED}/resume")
    assert response.status_code == 409
    assert "`claude` is not on PATH" in response.json()["reason"]


def test_resume_unknown_id_404(vigil_env: VigilEnv, web_client: TestClient) -> None:
    vigil_env.bin.install("claude", stdout=CLAUDE_BG)
    response = web_client.post("/sessions/no-such-session/resume")
    assert response.status_code == 404
    assert response.json()["ok"] is False
    assert all("--resume" not in call.argv for call in vigil_env.bin.calls("claude"))


def test_resume_is_post_only(web_client: TestClient) -> None:
    assert web_client.get(f"/sessions/{NAMED}/resume").status_code == 405


def test_meta_update(vigil_env: VigilEnv, web_client: TestClient) -> None:
    response = web_client.post(
        f"/sessions/{NAMED}/meta", data={"label": " my label ", "pinned": "true"}
    )
    assert response.status_code == 200
    data = response.json()
    assert (data["id"], data["label"], data["name"], data["pinned"]) == (
        NAMED,
        "my label",
        "my label",
        True,
    )
    assert data == stored(vigil_env, NAMED)

    # Only the fields sent change; a checkbox's "on" counts as true.
    data = web_client.post(f"/sessions/{NAMED}/meta", data={"note": "next: tests"}).json()
    assert (data["label"], data["note"], data["pinned"]) == ("my label", "next: tests", True)
    data = web_client.post(f"/sessions/{NAMED}/meta", data={"archived": "on"}).json()
    assert data["archived"] is True and data["note"] == "next: tests"
    assert NAMED not in ids(web_client.get("/api/v1/sessions"))

    # Empty text clears; false unsets.
    data = web_client.post(
        f"/sessions/{NAMED}/meta",
        data={"label": "", "note": " ", "pinned": "false", "archived": "false"},
    ).json()
    assert (data["label"], data["note"], data["pinned"], data["archived"]) == (
        None,
        None,
        False,
        False,
    )
    assert data["name"] == "fixture-named"  # back to Claude Code's title

    bad = web_client.post(f"/sessions/{NAMED}/meta", data={"pinned": "maybe"})
    assert bad.status_code == 422
    unknown = web_client.post("/sessions/no-such-session/meta", data={"label": "x"})
    assert unknown.status_code == 404
    conn = connect(vigil_env.index)
    assert all(row.label is None for row in list_sessions(conn, include_archived=True))
    conn.close()


def test_api_never_writes_under_the_claude_home(
    vigil_env: VigilEnv, web_client: TestClient
) -> None:
    freeze_tree(vigil_env.claude_home)
    for path in ("/api/v1/sessions", f"/api/v1/sessions/{NAMED}", "/api/v1/projects"):
        assert web_client.get(path).status_code == 200
    assert web_client.post(f"/sessions/{NAMED}/meta", data={"label": "x"}).status_code == 200


def test_session_has_remote_url(vigil_env: VigilEnv, web_client: TestClient) -> None:
    link = "https://claude.ai/code/session_PLACEHOLDER01"
    sql(vigil_env, "UPDATE sessions SET remote_url = ?, remote_url_at = ? WHERE session_id = ?",
        link, "2026-11-17T11:05:00.000000+00:00", NAMED)  # fmt: skip
    detail = web_client.get(f"/api/v1/sessions/{NAMED}").json()
    assert detail["remote_url"] == link
    assert detail["remote_url_at"] == "2026-11-17T11:05:00.000000+00:00"
    assert web_client.get(f"/api/v1/sessions/{PLAIN}").json()["remote_url"] is None


def test_open_link_only_when_live_and_newer(vigil_env: VigilEnv, web_client: TestClient) -> None:
    link = "https://claude.ai/code/session_PLACEHOLDER01"
    at = datetime(2026, 11, 17, 11, 5, tzinfo=UTC)
    row = replace(row_of(vigil_env, NAMED), remote_url=link, remote_url_at=at)

    def live(started: datetime | None) -> dict[str, LiveSession]:
        return {
            NAMED: LiveSession(NAMED, "interactive", None, None, busy=False, started_at=started)
        }

    assert open_link(row, {}) is None  # idle: not live
    assert open_link(row, live(at)) == link  # at or after the start
    assert open_link(row, live(at - timedelta(minutes=1))) == link
    assert open_link(row, live(None)) == link
    assert open_link(row, live(at + timedelta(minutes=1))) is None  # from an earlier run
    assert open_link(replace(row, remote_url=None), live(None)) is None
    assert open_link(replace(row, remote_url_at=None), live(at)) is None


LINK = "https://claude.ai/code/session_PLACEHOLDER01"
REMOTE_HOST = "vigil.example"
REMOTE_URL = f"http://{REMOTE_HOST}:7787"
REMOTE_PEER = ("192.0.2.10", 50000)  # TEST-NET-1: never a real client
TOKEN = "test-token-" + "0123456789abcdef" * 2  # a placeholder, not a real secret

NEW_BG = "0f1e2d3c-0000-4000-8000-00000000000b"
"""The id of a session a fake `claude --bg` starts without `--resume` (claude, not vigil, picks
it)."""
COPY = "5e6f7a8b-0000-4000-8000-00000000000c"
"""The id of the copy a fake `claude --bg --resume` starts for a session that keeps its own
options."""

_LINKING_CLAUDE = """#!{python}
import datetime, json, os, re, sys, time
args = sys.argv[1:]
with open({log!r}, "a") as handle:
    handle.write(json.dumps({{"argv": args, "cwd": os.getcwd()}}) + "\\n")
stopped = os.path.exists({marker!r})
if args[:1] in (["attach"], ["stop"], ["logs"]):
    job = args[1] if len(args) > 1 else ""
    if not re.fullmatch("[0-9a-f]{{8}}", job):
        sys.stderr.write({no_job!r}.format(job))
        sys.exit(1)
if args[:1] == ["stop"]:
    time.sleep({stop_delay!r})
    if {stop_code!r} == 0:
        open({marker!r}, "w").close()
        sys.stdout.write("stopped " + args[1] + "\\n")
    sys.exit({stop_code!r})
if "agents" in args:
    if stopped and {agents_fail!r}:
        sys.exit(1)
    running = [] if stopped else json.loads({live!r})
    if os.path.exists({started!r}):
        with open({started!r}) as handle:
            running += [json.loads(line) for line in handle]
    sys.stdout.write(json.dumps(running))
    sys.exit(0)
if "--bg" in args and {bg_code!r}:
    sys.exit({bg_code!r})
if "--bg" in args:
    if "--session-id" in args:
        sys.stderr.write("warning: --bg manages the session id; ignoring --session-id "
                         "(use --resume <id> to continue an existing session)\\n")
    if "--resume" in args:
        resumed = args[args.index("--resume") + 1]
        session = {copy_as!r} or resumed
        if session != resumed:
            print(f"note: background session {{resumed[:8]}} keeps its own saved options, so the "
                  f"flags you passed started a copy as {{session[:8]}}. Without flags, the same "
                  f"command continues {{resumed[:8]}} itself.")
    else:
        session = {new_id!r}
    print("Starting background service\\u2026")
    print(f"backgrounded \\u00b7 {{session[:8]}} (idle \\u2014 send a prompt to start)")
    print("  claude agents             list sessions")
    print(f"  claude attach {{session[:8]}}    open in this terminal")
    now = datetime.datetime.now(datetime.UTC)
    if {listed!r}:
        entry = {{"sessionId": session, "kind": "background", "cwd": os.getcwd(),
                 "status": "idle", "name": session[:8],
                 "startedAt": int(now.timestamp() * 1000) - 1}}
        with open({started!r}, "a") as handle:
            handle.write(json.dumps(entry) + "\\n")
    if {link!r}:
        record = {{"type": "system", "subtype": "bridge_status", "url": {link!r},
                  "timestamp": now.isoformat()}}
        with open(os.path.join({transcripts!r}, session + ".jsonl"), "a") as handle:
            handle.write(json.dumps(record) + "\\n")
    sys.exit(0)
sys.stdout.write({help!r})
"""


def install_claude(
    env: VigilEnv,
    *,
    transcript: Path,
    link: str | None,
    live: Sequence[Mapping[str, object]] | None = None,
    stop_code: int = 0,
    stop_delay: float = 0.0,
    agents_fail: bool = False,
    bg_code: int = 0,
    copy_as: str | None = None,
    new_id: str = NEW_BG,
    listed: bool = True,
) -> None:
    """A fake `claude` that behaves as the real one does where vigil depends on it.

    `agents --json` reports `live` plus every session it started (unless `listed` is False).
    `--bg` prints what the real one prints, `backgrounded · <job id>`: `--resume <id>` starts
    that session, or a copy under the id `copy_as` (saying so, as for a session that keeps its own
    options); without `--resume` it starts `new_id` (it ignores `--session-id`, with the real
    warning). Once started it appends a `bridge_status` line with `link` to that session's
    transcript in the folder of `transcript` (as Claude Code would once Remote Control is up).
    `attach`, `stop` and `logs` refuse anything but a job id. `stop` removes the sessions in
    `live` (and exits `stop_code`, after `stop_delay` seconds); once stopped, `agents_fail` makes
    listing fail; `--bg` exits `bg_code`. It records its argv and cwd like every FakeBin
    program."""
    env.bin.install("claude", stdout=CLAUDE_BG)
    script = env.bin.directory / "claude"
    script.write_text(
        _LINKING_CLAUDE.format(
            python=sys.executable,
            log=str(env.bin.directory / ".calls" / "claude.jsonl"),
            live=json.dumps(live or []),
            marker=str(env.bin.directory / ".stopped"),
            started=str(env.bin.directory / ".started"),
            stop_code=stop_code,
            stop_delay=stop_delay,
            agents_fail=agents_fail,
            bg_code=bg_code,
            copy_as=copy_as,
            new_id=new_id,
            listed=listed,
            link=link,
            transcripts=str(transcript.parent),
            no_job=NO_JOB,
            help=CLAUDE_BG,
        )
    )
    if link is not None:  # the fake may append to the transcript
        thaw_tree(transcript.parent)


@pytest.fixture
def remote_client(vigil_env: VigilEnv, tmp_path: Path) -> Iterator[TestClient]:
    """The service with remote access on, and a client that reaches it from another machine with
    the token. Waiting for a Remote Control link gives up after 0.1 s."""
    token = tmp_path / "token"
    token.write_text(TOKEN + "\n")
    token.chmod(0o600)
    config = Path(os.environ["XDG_CONFIG_HOME"]) / "vigil" / "config.toml"
    config.write_text(
        f'claude_home = "{vigil_env.claude_home}"\n\n'
        f'[server]\ntoken_file = "{token}"\nallowed_hosts = ["{REMOTE_HOST}"]\n'
    )
    conn = connect(vigil_env.index)
    scan(conn, vigil_env.claude_home)
    conn.close()
    app = create_app(
        load_config(), conn_factory=lambda: connect(vigil_env.index), start_watcher=False
    )
    web = app.state.vigil
    web.config = replace(web.config, resume=replace(web.config.resume, remote_link_timeout=0.1))
    headers = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(app, base_url=REMOTE_URL, client=REMOTE_PEER, headers=headers) as client:
        yield client


def start_in(env: VigilEnv, folder: Path) -> Path:
    """Give NAMED a working folder that exists; return its transcript."""
    sql(env, "UPDATE sessions SET cwd = ? WHERE session_id = ?", str(folder), NAMED)
    return Path(row_of(env, NAMED).transcript_path)


def started(env: VigilEnv) -> list[list[str]]:
    return [c.argv for c in env.bin.calls("claude") if "--bg" in c.argv]


def test_remote_resume_returns_link(
    vigil_env: VigilEnv, remote_client: TestClient, tmp_path: Path
) -> None:
    transcript = start_in(vigil_env, tmp_path)
    install_claude(vigil_env, transcript=transcript, link=LINK)
    response = remote_client.post(f"/sessions/{NAMED}/resume")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "ok": True,
        "attach": f"claude attach {NAMED[:8]}",
        "opened_terminal": False,
        "remote_url": LINK,
        "pending": False,
        "at_desk": False,
    }
    assert started(vigil_env) == [["--bg", "--resume", NAMED, "--remote-control"]]


def test_remote_resume_started_as_a_copy_answers_with_the_copy(
    vigil_env: VigilEnv, remote_client: TestClient, tmp_path: Path
) -> None:
    transcript = start_in(vigil_env, tmp_path)
    # A session started in the background keeps its own options: `claude --bg --resume` with
    # flags starts a copy, whose transcript gets the link; the original stays stopped.
    install_claude(vigil_env, transcript=transcript, link=LINK, copy_as=COPY)
    response = remote_client.post(f"/sessions/{NAMED}/resume")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "ok": True,
        "attach": f"claude attach {COPY[:8]}",
        "opened_terminal": False,
        "remote_url": LINK,
        "pending": False,
        "at_desk": False,
        "copy_of": NAMED,
        "session_id": COPY,
    }
    assert LINK not in transcript.read_text()  # the link is the copy's, not the original's
    conn = connect(vigil_env.index)
    launches = conn.execute("SELECT session_id FROM launches").fetchall()
    conn.close()
    assert [row[0] for row in launches] == [COPY]


def test_remote_resume_of_a_copy_whose_full_id_is_unknown_is_pending(
    vigil_env: VigilEnv, remote_client: TestClient, tmp_path: Path
) -> None:
    transcript = start_in(vigil_env, tmp_path)
    # `claude --bg` said it started a copy, which is not among the running sessions (yet): its
    # job id is enough to attach to it, but without its full id there is no transcript to wait
    # on, so the link is pending. The session asked for is not running: never attach to it.
    install_claude(vigil_env, transcript=transcript, link=LINK, copy_as=COPY, listed=False)
    response = remote_client.post(f"/sessions/{NAMED}/resume")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "ok": True,
        "attach": f"claude attach {COPY[:8]}",
        "opened_terminal": False,
        "remote_url": None,
        "pending": True,
        "at_desk": False,
        "copy_of": NAMED,
    }


def test_remote_resume_ignores_older_link(
    vigil_env: VigilEnv, remote_client: TestClient, tmp_path: Path
) -> None:
    transcript = start_in(vigil_env, tmp_path)
    old = {"type": "system", "subtype": "bridge_status", "url": LINK}
    old["timestamp"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    thaw_tree(transcript.parent)
    with transcript.open("a") as handle:
        handle.write(json.dumps(old) + "\n")
    install_claude(vigil_env, transcript=transcript, link=None)  # the launch writes no link
    response = remote_client.post(f"/sessions/{NAMED}/resume")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "ok": True,
        "attach": f"claude attach {NAMED[:8]}",
        "opened_terminal": False,
        "remote_url": None,
        "pending": True,
        "at_desk": False,
    }
    assert len(started(vigil_env)) == 1  # it did start


def test_second_remote_tap_opens_existing_link(
    vigil_env: VigilEnv, remote_client: TestClient, tmp_path: Path
) -> None:
    transcript = start_in(vigil_env, tmp_path)
    began = datetime.now(UTC) - timedelta(minutes=10)
    sql(vigil_env, "UPDATE sessions SET remote_url = ?, remote_url_at = ? WHERE session_id = ?",
        LINK, (began + timedelta(minutes=1)).isoformat(), NAMED)  # fmt: skip
    live = [
        {
            "sessionId": NAMED,
            "kind": "background",
            "cwd": str(tmp_path),
            "startedAt": int(began.timestamp() * 1000),
        }
    ]
    install_claude(vigil_env, transcript=transcript, link=LINK, live=live)
    response = remote_client.post(f"/sessions/{NAMED}/resume")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "ok": True,
        "attach": f"claude attach {NAMED[:8]}",
        "opened_terminal": False,
        "remote_url": LINK,
        "pending": False,
        "at_desk": False,
    }
    assert started(vigil_env) == []  # nothing was started


def test_remote_resume_of_background_session_without_link_is_restartable(
    vigil_env: VigilEnv, remote_client: TestClient, tmp_path: Path
) -> None:
    transcript = start_in(vigil_env, tmp_path)
    live = [{"sessionId": NAMED, "kind": "background", "cwd": str(tmp_path)}]
    install_claude(vigil_env, transcript=transcript, link=LINK, live=live)
    response = remote_client.post(f"/sessions/{NAMED}/resume")
    assert response.status_code == 409
    body = response.json()
    assert body["ok"] is False and body["restartable"] is True
    assert "without Remote Control" in body["reason"]
    assert started(vigil_env) == []


def claude_calls(env: VigilEnv) -> list[list[str]]:
    """What the fake `claude` was asked to do, in order, without vigil's probes of what is
    installed and what is live."""
    probes = (["--version"], ["--help"])
    return [
        c.argv for c in env.bin.calls("claude") if "agents" not in c.argv and c.argv not in probes
    ]


def background_live(folder: Path, **extra: object) -> list[dict[str, object]]:
    return [{"sessionId": NAMED, "kind": "background", "cwd": str(folder), **extra}]


def test_restart_requires_confirm(
    vigil_env: VigilEnv, remote_client: TestClient, tmp_path: Path
) -> None:
    transcript = start_in(vigil_env, tmp_path)
    install_claude(vigil_env, transcript=transcript, link=LINK, live=background_live(tmp_path))
    url = f"/sessions/{NAMED}/restart-remote"
    assert remote_client.post(url).status_code == 400
    assert remote_client.post(url, data={"confirm": "no"}).status_code == 400
    assert remote_client.post(url, data={"confirm": ""}).status_code == 400
    assert claude_calls(vigil_env) == []  # nothing was stopped or started


def test_restart_refuses_interactive(
    vigil_env: VigilEnv, remote_client: TestClient, tmp_path: Path
) -> None:
    transcript = start_in(vigil_env, tmp_path)
    live = [{"sessionId": NAMED, "kind": "interactive", "cwd": str(tmp_path)}]
    install_claude(vigil_env, transcript=transcript, link=LINK, live=live)
    response = remote_client.post(f"/sessions/{NAMED}/restart-remote", data={"confirm": "yes"})
    assert response.status_code == 409
    assert response.json()["ok"] is False and response.json()["restartable"] is False
    assert "background" in response.json()["reason"]
    # A session that is not running at all has nothing to restart either.
    install_claude(vigil_env, transcript=transcript, link=LINK, live=[])
    response = remote_client.post(f"/sessions/{NAMED}/restart-remote", data={"confirm": "yes"})
    assert response.status_code == 409
    assert remote_client.post(
        "/sessions/no-such-session/restart-remote", data={"confirm": "yes"}
    ).status_code == 404  # fmt: skip
    assert claude_calls(vigil_env) == []


def test_restart_remote_returns_link(
    vigil_env: VigilEnv, remote_client: TestClient, tmp_path: Path
) -> None:
    transcript = start_in(vigil_env, tmp_path)
    install_claude(vigil_env, transcript=transcript, link=LINK, live=background_live(tmp_path))
    response = remote_client.post(f"/sessions/{NAMED}/restart-remote", data={"confirm": "yes"})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "ok": True,
        "attach": f"claude attach {NAMED[:8]}",
        "opened_terminal": False,
        "remote_url": LINK,
        "pending": False,
        "at_desk": False,
    }
    # Stopped first, then started again with Remote Control, in its folder.
    assert claude_calls(vigil_env) == [
        ["stop", NAMED[:8]],
        ["--bg", "--resume", NAMED, "--remote-control"],
    ]


def test_restart_of_a_background_session_comes_back_as_a_copy(
    vigil_env: VigilEnv, remote_client: TestClient, tmp_path: Path
) -> None:
    transcript = start_in(vigil_env, tmp_path)
    install_claude(
        vigil_env, transcript=transcript, link=LINK, live=background_live(tmp_path), copy_as=COPY
    )
    response = remote_client.post(f"/sessions/{NAMED}/restart-remote", data={"confirm": "yes"})
    assert response.status_code == 200, response.text
    answer = response.json()
    assert (answer["copy_of"], answer["session_id"]) == (NAMED, COPY)
    assert (answer["attach"], answer["remote_url"]) == (f"claude attach {COPY[:8]}", LINK)
    assert claude_calls(vigil_env) == [
        ["stop", NAMED[:8]],
        ["--bg", "--resume", NAMED, "--remote-control"],
    ]


def test_restart_of_a_session_that_is_already_joinable_stops_nothing(
    vigil_env: VigilEnv, remote_client: TestClient, tmp_path: Path
) -> None:
    # Another tab (or a stale page) restarted it already: it now has a link for its current run.
    transcript = start_in(vigil_env, tmp_path)
    began = datetime.now(UTC) - timedelta(minutes=10)
    append_bridge(transcript, began + timedelta(minutes=1))
    live = background_live(tmp_path, startedAt=int(began.timestamp() * 1000))
    install_claude(vigil_env, transcript=transcript, link=LINK, live=live)
    response = remote_client.post(f"/sessions/{NAMED}/restart-remote", data={"confirm": "yes"})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "ok": True,
        "attach": f"claude attach {NAMED[:8]}",
        "opened_terminal": False,
        "remote_url": LINK,
        "pending": False,
        "at_desk": False,
    }
    assert claude_calls(vigil_env) == []  # not stopped, not started


def test_restart_remote_is_pending_without_a_new_link(
    vigil_env: VigilEnv, remote_client: TestClient, tmp_path: Path
) -> None:
    transcript = start_in(vigil_env, tmp_path)
    install_claude(vigil_env, transcript=transcript, link=None, live=background_live(tmp_path))
    response = remote_client.post(f"/sessions/{NAMED}/restart-remote", data={"confirm": "yes"})
    assert response.status_code == 200
    assert response.json()["remote_url"] is None and response.json()["pending"] is True


def test_restart_starts_nothing_when_the_stop_fails(
    vigil_env: VigilEnv, remote_client: TestClient, tmp_path: Path
) -> None:
    transcript = start_in(vigil_env, tmp_path)
    install_claude(
        vigil_env, transcript=transcript, link=LINK, live=background_live(tmp_path), stop_code=1
    )
    response = remote_client.post(f"/sessions/{NAMED}/restart-remote", data={"confirm": "yes"})
    assert response.status_code == 409
    assert response.json()["reason"] == "could not stop the session; nothing was restarted"
    assert started(vigil_env) == []


def test_restart_plans_before_it_stops(
    vigil_env: VigilEnv, remote_client: TestClient, tmp_path: Path
) -> None:
    transcript = start_in(vigil_env, tmp_path)
    install_claude(vigil_env, transcript=transcript, link=LINK, live=background_live(tmp_path))
    web = remote_client.app.state.vigil  # type: ignore[attr-defined]
    good = web.config
    url = f"/sessions/{NAMED}/restart-remote"
    # Nothing that would stop the start afterwards is allowed to get as far as the stop.
    web.config = replace(good, resume=replace(good.resume, default_flags=("--bogus",)))
    response = remote_client.post(url, data={"confirm": "yes"})
    assert response.status_code == 409 and "flag not allowed" in response.json()["reason"]
    web.config = replace(good, resume=replace(good.resume, launcher="tmux"))  # no tmux on PATH
    response = remote_client.post(url, data={"confirm": "yes"})
    assert response.status_code == 409 and "tmux" in response.json()["reason"]
    gone = tmp_path / "gone"
    sql(vigil_env, "UPDATE sessions SET cwd = ? WHERE session_id = ?", str(gone), NAMED)
    web.config = good
    response = remote_client.post(url, data={"confirm": "yes"})
    assert response.status_code == 409 and "folder no longer exists" in response.json()["reason"]
    assert claude_calls(vigil_env) == []  # not stopped, not started


def test_restart_says_so_when_it_stopped_but_could_not_start(
    vigil_env: VigilEnv, remote_client: TestClient, tmp_path: Path
) -> None:
    transcript = start_in(vigil_env, tmp_path)
    install_claude(
        vigil_env, transcript=transcript, link=LINK, live=background_live(tmp_path), bg_code=1
    )
    response = remote_client.post(f"/sessions/{NAMED}/restart-remote", data={"confirm": "yes"})
    assert response.status_code == 409
    assert response.json()["reason"].startswith("stopped, but could not start it again: ")
    assert response.json()["restartable"] is False
    assert [argv[0] for argv in claude_calls(vigil_env)] == ["stop", "--bg"]


def test_restart_unknown_live_state_is_not_gone(
    vigil_env: VigilEnv, remote_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = start_in(vigil_env, tmp_path)
    install_claude(
        vigil_env,
        transcript=transcript,
        link=LINK,
        live=background_live(tmp_path),
        agents_fail=True,  # once stopped, `claude agents --json` fails
    )
    # Do not wait the real 10 s for the stop to be given up on.
    monkeypatch.setattr("vigil.web.api.stop_session", partial(stop_session, timeout=0.6, poll=0.1))
    response = remote_client.post(f"/sessions/{NAMED}/restart-remote", data={"confirm": "yes"})
    assert response.status_code == 409
    assert response.json()["reason"] == "could not stop the session; nothing was restarted"
    assert started(vigil_env) == []


def test_concurrent_restarts_launch_once(
    vigil_env: VigilEnv, remote_client: TestClient, tmp_path: Path
) -> None:
    transcript = start_in(vigil_env, tmp_path)
    install_claude(
        vigil_env,
        transcript=transcript,
        link=LINK,
        live=background_live(tmp_path),
        stop_delay=0.5,  # long enough for the other request to get as far as its own stop
    )
    answers: list[int] = []

    def restart() -> None:
        response = remote_client.post(f"/sessions/{NAMED}/restart-remote", data={"confirm": "yes"})
        answers.append(response.status_code)

    threads = [threading.Thread(target=restart) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    # The second finds the session running again with its link, and joins it as it is.
    assert answers == [200, 200]
    assert started(vigil_env) == [["--bg", "--resume", NAMED, "--remote-control"]]
    assert [argv[0] for argv in claude_calls(vigil_env)].count("stop") == 1


def test_restart_at_the_desk_opens_a_terminal(
    vigil_env: VigilEnv,
    web_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = start_in(vigil_env, tmp_path)
    install_claude(vigil_env, transcript=transcript, link=LINK, live=background_live(tmp_path))
    vigil_env.bin.install("xterm")
    monkeypatch.setenv("DISPLAY", ":99")
    response = web_client.post(f"/sessions/{NAMED}/restart-remote", data={"confirm": "yes"})
    assert response.status_code == 200, response.text
    assert response.json()["opened_terminal"] is True and response.json()["pending"] is False
    assert started(vigil_env) == [["--bg", "--resume", NAMED, "--remote-control"]]


def test_open_button_says_open_for_remote_requests(
    vigil_env: VigilEnv, remote_client: TestClient, tmp_path: Path
) -> None:
    sql(vigil_env, "UPDATE sessions SET remote_url = ?, remote_url_at = ? WHERE session_id = ?",
        LINK, "2026-11-17T11:05:00.000000+00:00", NAMED)  # fmt: skip
    live = [{"sessionId": NAMED, "kind": "background", "cwd": str(tmp_path)}]
    install_claude(vigil_env, transcript=tmp_path / "unused", link=None, live=live)
    freeze_tree(vigil_env.claude_home)  # showing the page only reads
    page = remote_client.get("/", headers={"Accept": "text/html"}).text
    assert f'<a class="open" href="{LINK}" rel="noopener">Open</a>' in page
    assert "Open in Claude" not in page.split("htmx:beforeSwap")[0]
    detail = remote_client.get(f"/sessions/{NAMED}", headers={"Accept": "text/html"}).text
    assert f'<a class="open" href="{LINK}" rel="noopener">Open</a>' in detail


def append_bridge(transcript: Path, at: datetime) -> None:
    record = {
        "type": "system",
        "subtype": "bridge_status",
        "url": LINK,
        "timestamp": at.isoformat(),
    }
    thaw_tree(transcript.parent)
    with transcript.open("a") as handle:
        handle.write(json.dumps(record) + "\n")


def test_remote_tap_finds_a_link_the_index_has_not_caught_up_with(
    vigil_env: VigilEnv, remote_client: TestClient, tmp_path: Path
) -> None:
    transcript = start_in(vigil_env, tmp_path)
    began = datetime.now(UTC) - timedelta(minutes=10)
    assert row_of(vigil_env, NAMED).remote_url is None  # nothing indexed
    append_bridge(transcript, began + timedelta(minutes=1))
    live = [{"sessionId": NAMED, "kind": "background", "startedAt": int(began.timestamp() * 1000)}]
    install_claude(vigil_env, transcript=transcript, link=None, live=live)
    response = remote_client.post(f"/sessions/{NAMED}/resume")
    assert response.status_code == 200, response.text
    assert response.json()["remote_url"] == LINK and response.json()["pending"] is False
    assert started(vigil_env) == []
    # A link from before this run started is not this run's.
    sql(vigil_env, "UPDATE sessions SET remote_url = NULL WHERE session_id = ?", NAMED)
    live[0]["startedAt"] = int((began + timedelta(minutes=5)).timestamp() * 1000)
    install_claude(vigil_env, transcript=transcript, link=None, live=live)
    remote_client.app.state.vigil.live.clear()  # type: ignore[attr-defined]
    response = remote_client.post(f"/sessions/{NAMED}/resume")
    assert response.status_code == 409 and response.json()["restartable"] is True


def test_remote_tap_on_interactive_session_with_a_link(
    vigil_env: VigilEnv, remote_client: TestClient, tmp_path: Path
) -> None:
    transcript = start_in(vigil_env, tmp_path)
    began = datetime.now(UTC) - timedelta(minutes=10)
    live = [{"sessionId": NAMED, "kind": "interactive", "startedAt": int(began.timestamp() * 1000)}]
    install_claude(vigil_env, transcript=transcript, link=None, live=live)
    # No link: refused as an open terminal, with the hint to make it joinable.
    response = remote_client.post(f"/sessions/{NAMED}/resume")
    assert response.status_code == 409
    assert response.json()["restartable"] is False
    assert "type /remote-control in that terminal" in response.json()["reason"]
    # A link only in the transcript, then one in the index: both are answered, nothing started.
    append_bridge(transcript, began + timedelta(minutes=1))
    for _ in range(2):
        response = remote_client.post(f"/sessions/{NAMED}/resume")
        assert response.status_code == 200, response.text
        assert response.json()["remote_url"] == LINK
        sql(vigil_env, "UPDATE sessions SET remote_url = ?, remote_url_at = ? WHERE session_id = ?",
            LINK, (began + timedelta(minutes=1)).isoformat(), NAMED)  # fmt: skip
    assert started(vigil_env) == []


def test_api_serves_open_url(vigil_env: VigilEnv, web_client: TestClient, tmp_path: Path) -> None:
    began = datetime.now(UTC) - timedelta(minutes=10)
    sql(vigil_env, "UPDATE sessions SET remote_url = ?, remote_url_at = ? WHERE session_id = ?",
        LINK, (began + timedelta(minutes=1)).isoformat(), NAMED)  # fmt: skip
    sql(vigil_env, "UPDATE sessions SET remote_url = ?, remote_url_at = ? WHERE session_id = ?",
        LINK, (began + timedelta(minutes=1)).isoformat(), PLAIN)  # fmt: skip
    live = [{"sessionId": NAMED, "kind": "background", "startedAt": int(began.timestamp() * 1000)}]
    install_claude(vigil_env, transcript=tmp_path / "x", link=None, live=live)
    by_id = {s["id"]: s for s in web_client.get("/api/v1/sessions").json()}
    assert by_id[NAMED]["open_url"] == LINK  # live, with this run's link
    assert by_id[PLAIN]["open_url"] is None  # a link, but not live
    assert web_client.get(f"/api/v1/sessions/{NAMED}").json()["open_url"] == LINK


def test_sessions_have_remote_urls(web_client: TestClient) -> None:
    items = web_client.get("/api/v1/sessions").json()
    assert items
    assert all(isinstance(item["remote_urls"], list) for item in items)


def test_local_sessions_have_hub_fields(web_client: TestClient) -> None:
    items = web_client.get("/api/v1/sessions").json()
    assert items
    for item in items:
        assert item["stale"] is False
        assert item["received_at"] is None
        assert item["resume_url"] is None
        assert item["machine_url"] is None
    detail = web_client.get(f"/api/v1/sessions/{items[0]['id']}").json()
    assert detail["stale"] is False and detail["machine_url"] is None


def test_machines_empty_and_mode_local_on_local_vigil(web_client: TestClient) -> None:
    assert web_client.get("/api/v1/machines").json() == []
    assert web_client.get("/api/v1/health").json()["mode"] == "local"


# Live-state timestamp for the push client (vigil hub, Task 17).
from vigil.web import LIVE_TTL, LiveStatus  # noqa: E402


def test_live_status_checked_at_and_peek(vigil_env: VigilEnv, tmp_path: Path) -> None:
    live = [{"sessionId": NAMED, "kind": "background", "status": "busy"}]
    install_claude(vigil_env, transcript=tmp_path / "x", link=None, live=live)
    ticks = [100.0]
    wall = datetime(2026, 11, 25, 18, 0, tzinfo=UTC)
    status = LiveStatus(clock=lambda: ticks[0], wall=lambda: wall)
    assert status.checked_at is None
    assert status.peek() is None  # nothing cached, and peek never probes
    assert vigil_env.bin.calls("claude") == []

    caps, sessions = status.get()
    assert caps.has_agents_json and sessions[NAMED].busy
    assert status.checked_at == wall
    calls = len(vigil_env.bin.calls("claude"))
    ticks[0] += LIVE_TTL * 100  # long past the TTL
    assert status.peek() == (caps, sessions, wall)
    assert len(vigil_env.bin.calls("claude")) == calls

    later = wall + timedelta(minutes=5)
    status = LiveStatus(clock=lambda: ticks[0], wall=lambda: later)
    assert status.probe() is not None
    assert status.checked_at == later
    peeked = status.peek()
    assert peeked is not None and peeked[2] == later
