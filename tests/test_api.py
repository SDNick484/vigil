import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import VigilEnv, freeze_tree
from vigil.index.db import connect
from vigil.index.queries import get_session, list_sessions
from vigil.index.scanner import link_chains
from vigil.serialize import session_to_dict
from vigil.web.api import SessionOut

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
        "attach": f"claude attach {NAMED}",
        "opened_terminal": False,
    }
    started = [c for c in vigil_env.bin.calls("claude") if "--resume" in c.argv]
    assert [(c.argv, c.cwd) for c in started] == [(["--bg", "--resume", NAMED], str(folder))]
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
        "attach": f"claude attach {NAMED}",
        "opened_terminal": True,
    }


def test_resume_refused_409(vigil_env: VigilEnv, web_client: TestClient) -> None:
    vigil_env.bin.install("claude", stdout=CLAUDE_BG)
    response = web_client.post(f"/sessions/{NAMED}/resume")
    assert response.status_code == 409
    assert response.json() == {
        "ok": False,
        "reason": "folder no longer exists: /home/user/src/fixture-repo",
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
