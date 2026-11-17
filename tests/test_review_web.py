import os
import sqlite3
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import LOOPBACK_URL, VigilEnv, freeze_tree
from tests.test_api import (
    CLAUDE_BG,
    LINK,
    install_claude,
    remote_client,  # noqa: F401 - a fixture, used by name
)
from tests.test_review_runner import ANSWER, claude_result
from tests.test_review_store import NUMBERS, insert
from vigil.config import load_config
from vigil.index.db import connect
from vigil.index.scanner import scan
from vigil.review.runner import STOPPED
from vigil.review.schema import Advice, Suggestion
from vigil.review.store import INTERRUPTED, finish, get_review
from vigil.web import WebState
from vigil.web.app import create_app

NAMED = "e47d2def-0bb3-4d50-9f9c-8e38e4cdcfba"
NOTE = "Suggestions come from Claude reading this session. Check them before acting."


def set_cwd(env: VigilEnv, folder: Path) -> None:
    conn = sqlite3.connect(env.index)
    with conn:
        conn.execute("UPDATE sessions SET cwd = ? WHERE session_id = ?", (str(folder), NAMED))
    conn.close()


def web_state(client: TestClient) -> WebState:
    state: WebState = client.app.state.vigil  # type: ignore[attr-defined]
    return state


def wait_done(client: TestClient) -> list[dict[str, Any]]:
    """The session's reviews once none is running (the review runs in a background thread)."""
    deadline = time.monotonic() + 15
    while True:
        reviews: list[dict[str, Any]] = client.get(f"/api/v1/sessions/{NAMED}/reviews").json()
        if all(r["status"] != "running" for r in reviews) or time.monotonic() > deadline:
            return reviews
        time.sleep(0.05)


def test_post_review_runs_in_the_background(
    vigil_env: VigilEnv, web_client: TestClient, tmp_path: Path
) -> None:
    folder = tmp_path / "work"
    folder.mkdir()
    set_cwd(vigil_env, folder)
    vigil_env.bin.install("claude", stdout=claude_result(structured_output=ANSWER))

    response = web_client.post(f"/sessions/{NAMED}/review")
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["ok"] is True
    review_id = body["review_id"]

    (review,) = wait_done(web_client)
    assert review["id"] == review_id
    assert review["status"] == "done"
    assert review["cost_usd"] == 0.12
    assert [s["title"] for s in review["suggestions"]][0] == "Plan first"
    assert review["advice"] == [
        {"title": "Trim tool output", "why": "One Bash result was 40k characters."}
    ]
    runs = [c for c in vigil_env.bin.calls("claude") if "-p" in c.argv]
    assert [c.cwd for c in runs] == [str(folder)]
    # The slot is free again.
    assert web_state(web_client).review_slot.try_start()
    web_state(web_client).review_slot.finish()

    page = web_client.get(f"/sessions/{NAMED}").text
    assert "Review this session" in page
    assert "Plan first" in page
    assert "Trim tool output" in page
    assert NOTE in page


def test_post_review_409_when_busy(
    vigil_env: VigilEnv, web_client: TestClient, tmp_path: Path
) -> None:
    folder = tmp_path / "work"
    folder.mkdir()
    set_cwd(vigil_env, folder)
    vigil_env.bin.install("claude", stdout=claude_result(structured_output=ANSWER))
    conn = connect(vigil_env.index)
    running = insert(conn)
    conn.close()
    slot = web_state(web_client).review_slot
    assert slot.try_start()
    try:
        response = web_client.post(f"/sessions/{NAMED}/review")
    finally:
        slot.finish()
    assert response.status_code == 409
    assert response.json() == {
        "ok": False,
        "reason": "a review is already running",
        "review_id": running,
    }
    assert [c for c in vigil_env.bin.calls("claude") if "-p" in c.argv] == []


def test_post_review_409_when_another_process_runs_one(
    vigil_env: VigilEnv, web_client: TestClient, tmp_path: Path
) -> None:
    folder = tmp_path / "work"
    folder.mkdir()
    set_cwd(vigil_env, folder)
    vigil_env.bin.install("claude", stdout=claude_result(structured_output=ANSWER))
    conn = connect(vigil_env.index)
    running = insert(conn, at=datetime.now(UTC))
    conn.close()
    response = web_client.post(f"/sessions/{NAMED}/review")
    assert response.status_code == 409
    assert response.json()["review_id"] == running
    # The slot was given back.
    assert web_state(web_client).review_slot.try_start()
    web_state(web_client).review_slot.finish()


def test_post_review_409_when_folder_gone(vigil_env: VigilEnv, web_client: TestClient) -> None:
    vigil_env.bin.install("claude", stdout=claude_result(structured_output=ANSWER))
    response = web_client.post(f"/sessions/{NAMED}/review")
    assert response.status_code == 409
    assert response.json() == {
        "ok": False,
        "reason": "folder no longer exists: /home/user/src/fixture-repo",
    }
    assert web_state(web_client).review_slot.try_start()
    web_state(web_client).review_slot.finish()


def test_post_review_404_for_unknown_session(web_client: TestClient) -> None:
    response = web_client.post("/sessions/no-such-session/review")
    assert response.status_code == 404
    assert response.json()["ok"] is False


def test_running_reviews_failed_on_start(vigil_env: VigilEnv, web_client: TestClient) -> None:
    conn = connect(vigil_env.index)
    review_id = insert(conn)
    conn.close()
    app = create_app(
        load_config(), conn_factory=lambda: connect(vigil_env.index), start_watcher=False
    )
    with TestClient(app, base_url=LOOPBACK_URL):
        pass
    conn = connect(vigil_env.index)
    stored = get_review(conn, review_id)
    conn.close()
    assert stored is not None
    assert (stored.status, stored.error) == ("failed", INTERRUPTED)


def test_review_fragment_polls_while_running(vigil_env: VigilEnv, web_client: TestClient) -> None:
    conn = connect(vigil_env.index)
    review_id = insert(conn)
    conn.close()
    url = f"/sessions/{NAMED}/reviews/{review_id}"
    running = web_client.get(url)
    assert running.status_code == 200
    assert 'hx-trigger="every 2s"' in running.text
    assert f'hx-get="{url}"' in running.text
    assert "<html" not in running.text

    conn = connect(vigil_env.index)
    finish(conn, review_id, suggestions=[], advice=[], cost_usd=0.2)
    conn.close()
    done = web_client.get(url)
    assert done.status_code == 200
    assert "hx-trigger" not in done.text
    assert "$0.20" in done.text


def test_review_fragment_stops_polling_when_gone(
    vigil_env: VigilEnv, web_client: TestClient
) -> None:
    conn = connect(vigil_env.index)
    review_id = insert(conn)
    conn.close()
    other = "f26f1b2c-d3b2-4389-b0ed-aba7e19e8594"
    for url in (
        f"/sessions/{NAMED}/reviews/{review_id + 1}",
        f"/sessions/{other}/reviews/{review_id}",
    ):
        gone = web_client.get(url)
        assert gone.status_code == 286  # htmx stops polling
        assert "This review is no longer available." in gone.text
        assert "hx-trigger" not in gone.text
    # The review's session leaves the index while the page polls it.
    conn = connect(vigil_env.index)
    with conn:
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (NAMED,))
    conn.close()
    assert web_client.get(f"/sessions/{NAMED}/reviews/{review_id}").status_code == 286


def test_review_text_is_escaped(vigil_env: VigilEnv, web_client: TestClient) -> None:
    script = "<script>alert(1)</script>"
    card = Suggestion(
        kind="file",
        title=script,
        why=script,
        evidence=script,
        flag_name=None,
        flag_value=None,
        file_target="project_claude_md",
        file_text=script,
        file_settings=None,
    )
    conn = connect(vigil_env.index)
    review_id = insert(conn)
    finish(conn, review_id, suggestions=[card], advice=[Advice(script, script)], cost_usd=0.1)
    conn.close()
    for url in (f"/sessions/{NAMED}/reviews/{review_id}", f"/sessions/{NAMED}"):
        text = web_client.get(url).text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text
        assert "<script>alert" not in text


def test_review_shows_the_numbers_it_used(vigil_env: VigilEnv, web_client: TestClient) -> None:
    conn = connect(vigil_env.index)
    review_id = insert(conn)
    finish(conn, review_id, suggestions=[], advice=[], cost_usd=0.2)
    conn.close()
    text = web_client.get(f"/sessions/{NAMED}/reviews/{review_id}").text
    assert "claude-placeholder" in text  # models used
    assert "5 min" in text  # wall-clock span
    assert "may be incomplete" in text  # launch flags note
    assert "Cache read" in text  # the per-turn series
    assert NUMBERS.turns[0].cache_read == 4


def test_api_lists_reviews(vigil_env: VigilEnv, web_client: TestClient) -> None:
    assert web_client.get(f"/api/v1/sessions/{NAMED}/reviews").json() == []
    conn = connect(vigil_env.index)
    review_id = insert(conn)
    conn.close()
    (review,) = web_client.get(f"/api/v1/sessions/{NAMED}/reviews").json()
    assert (review["id"], review["status"]) == (review_id, "running")
    assert review["numbers"]["compactions"] == 1
    assert web_client.get("/api/v1/sessions/nothing/reviews").status_code == 404


def test_review_never_writes_under_the_claude_home(
    vigil_env: VigilEnv, web_client: TestClient, tmp_path: Path
) -> None:
    folder = tmp_path / "work"
    folder.mkdir()
    set_cwd(vigil_env, folder)
    vigil_env.bin.install("claude", stdout=claude_result(structured_output=ANSWER))
    freeze_tree(vigil_env.claude_home)
    assert web_client.post(f"/sessions/{NAMED}/review").status_code == 202
    (review,) = wait_done(web_client)
    assert review["status"] == "done"


SLEEPING_CLAUDE = """#!{python}
import os, sys, time
if "-p" in sys.argv:
    with open({pid_file!r}, "w") as handle:
        handle.write(str(os.getpid()))
    time.sleep(60)
"""


def test_shutdown_stops_a_running_review(vigil_env: VigilEnv, tmp_path: Path) -> None:
    folder = tmp_path / "work"
    folder.mkdir()
    pid_file = tmp_path / "claude.pid"
    claude = vigil_env.bin.directory / "claude"
    claude.write_text(SLEEPING_CLAUDE.format(python=sys.executable, pid_file=str(pid_file)))
    claude.chmod(0o755)
    conn = connect(vigil_env.index)
    scan(conn, vigil_env.claude_home)
    conn.close()
    set_cwd(vigil_env, folder)
    app = create_app(
        load_config(), conn_factory=lambda: connect(vigil_env.index), start_watcher=False
    )
    started = time.monotonic()
    with TestClient(app, base_url=LOOPBACK_URL, headers={"Origin": LOOPBACK_URL}) as client:
        response = client.post(f"/sessions/{NAMED}/review")
        assert response.status_code == 202, response.text
        review_id = response.json()["review_id"]
        deadline = time.monotonic() + 10
        while not pid_file.exists() or not pid_file.read_text():
            assert time.monotonic() < deadline, "the fake claude never started"
            time.sleep(0.05)
        pid = int(pid_file.read_text())
    assert time.monotonic() - started < 30  # stopped, not waited for
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    conn = connect(vigil_env.index)
    stored = get_review(conn, review_id)
    conn.close()
    assert stored is not None
    assert (stored.status, stored.error) == ("failed", STOPPED)
    assert STOPPED == "vigil stopped during the review"


# Acting on flag suggestions (Task 8). The client sends only the review id and the suggestion's
# position; the flag always comes from the stored suggestion.

PLACEHOLDER_NEW = "0123abcd-0000-4000-8000-0000000000ff"

CATALOG = [
    ("--model", "Model for the current session"),
    ("--permission-mode", 'Permission mode (choices: "default", "plan", "bypassPermissions")'),
    ("--dangerously-skip-permissions", "Bypass all permission checks"),
]


def flag_card(name: str, value: str | None) -> Suggestion:
    return Suggestion(
        kind="flag",
        title="Try a flag",
        why="why",
        evidence="turn 2",
        flag_name=name,
        flag_value=value,
        file_target=None,
        file_text=None,
        file_settings=None,
    )


def flag_review(env: VigilEnv, *cards: Suggestion) -> int:
    """A finished review of NAMED holding `cards`, with a catalog for the installed claude."""
    conn = connect(env.index)
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO catalog (version, kind, name, short, parent, description) "
            "VALUES ('2.1.312', 'flag', ?, NULL, NULL, ?)",
            CATALOG,
        )
    review_id = insert(conn)
    finish(conn, review_id, suggestions=list(cards), advice=[], cost_usd=0.1)
    conn.close()
    return review_id


def launched(env: VigilEnv) -> list[tuple[list[str], str]]:
    return [(c.argv, c.cwd) for c in env.bin.calls("claude") if "--bg" in c.argv]


def test_suggestion_resume_uses_stored_flag(
    vigil_env: VigilEnv, web_client: TestClient, tmp_path: Path
) -> None:
    folder = tmp_path / "work"
    folder.mkdir()
    set_cwd(vigil_env, folder)
    vigil_env.bin.install("claude", stdout=CLAUDE_BG)
    review_id = flag_review(
        vigil_env, flag_card("--permission-mode", "plan"), flag_card("--model", "opus")
    )

    response = web_client.post(f"/reviews/{review_id}/suggestions/1/resume")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "ok": True,
        "attach": f"claude attach {NAMED}",
        "opened_terminal": False,
        "remote_url": None,
        "pending": False,
    }
    assert launched(vigil_env) == [
        (["--bg", "--resume", NAMED, "--remote-control", "--model", "opus"], str(folder))
    ]
    # Unknown review or position: 404, nothing launched.
    assert web_client.post(f"/reviews/{review_id}/suggestions/2/resume").status_code == 404
    assert web_client.post(f"/reviews/{review_id + 1}/suggestions/0/resume").status_code == 404
    assert len(launched(vigil_env)) == 1


def test_suggestion_refused_flag_409(
    vigil_env: VigilEnv, web_client: TestClient, tmp_path: Path
) -> None:
    folder = tmp_path / "work"
    folder.mkdir()
    set_cwd(vigil_env, folder)
    vigil_env.bin.install("claude", stdout=CLAUDE_BG)
    config = Path(os.environ["XDG_CONFIG_HOME"]) / "vigil" / "config.toml"
    before = config.read_text()
    review_id = flag_review(vigil_env, flag_card("--dangerously-skip-permissions", None))

    for action in ("resume", "start-new", "always-use"):
        response = web_client.post(f"/reviews/{review_id}/suggestions/0/{action}")
        assert response.status_code == 409, action
        assert response.json() == {
            "ok": False,
            "reason": "flag not allowed: --dangerously-skip-permissions",
        }
    assert launched(vigil_env) == []
    assert config.read_text() == before

    # The card offers no launch, only the command to copy.
    text = web_client.get(f"/sessions/{NAMED}/reviews/{review_id}").text
    assert "vigil will not launch this flag itself. Copy the command to try it:" in text
    assert "<code>claude --dangerously-skip-permissions</code>" in text
    assert "/suggestions/0/resume" not in text


def test_suggestion_card_buttons(vigil_env: VigilEnv, web_client: TestClient) -> None:
    review_id = flag_review(
        vigil_env, flag_card("--model", "opus"), flag_card("--permission-mode", "bypassPermissions")
    )
    for url in (f"/sessions/{NAMED}/reviews/{review_id}", f"/sessions/{NAMED}"):
        text = web_client.get(url).text
        for action in ("resume", "start-new", "always-use"):
            assert f'hx-post="/reviews/{review_id}/suggestions/0/{action}"' in text
            assert f"/reviews/{review_id}/suggestions/1/{action}" not in text
        assert "Resume with these" in text
        assert "Start new session with these" in text
        assert "Always use" in text
        assert "<code>claude --permission-mode bypassPermissions</code>" in text


def test_start_new_at_the_desk(
    vigil_env: VigilEnv, web_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "work"
    folder.mkdir()
    set_cwd(vigil_env, folder)
    vigil_env.bin.install("claude", stdout=CLAUDE_BG)
    monkeypatch.setattr("vigil.web.review.uuid.uuid4", lambda: uuid.UUID(PLACEHOLDER_NEW))
    review_id = flag_review(vigil_env, flag_card("--model", "opus"))
    response = web_client.post(f"/reviews/{review_id}/suggestions/0/start-new")
    assert response.status_code == 200, response.text
    assert response.json()["attach"] == f"claude attach {PLACEHOLDER_NEW}"
    assert launched(vigil_env) == [
        (
            ["--bg", "--session-id", PLACEHOLDER_NEW, "--remote-control", "--model", "opus"],
            str(folder),
        )
    ]


def test_start_new_remote_waits_for_link(
    vigil_env: VigilEnv,
    remote_client: TestClient,  # noqa: F811 - the fixture imported above
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "work"
    folder.mkdir()
    set_cwd(vigil_env, folder)
    monkeypatch.setattr("vigil.web.review.uuid.uuid4", lambda: uuid.UUID(PLACEHOLDER_NEW))
    # Claude Code makes the new session's transcript in its project folder; vigil finds it there.
    project = vigil_env.claude_home / "projects" / "-placeholder-work"
    project.mkdir()
    install_claude(vigil_env, transcript=project / f"{PLACEHOLDER_NEW}.jsonl", link=LINK)
    review_id = flag_review(vigil_env, flag_card("--model", "opus"))

    response = remote_client.post(f"/reviews/{review_id}/suggestions/0/start-new")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "ok": True,
        "attach": f"claude attach {PLACEHOLDER_NEW}",
        "opened_terminal": False,
        "remote_url": LINK,
        "pending": False,
    }
    assert launched(vigil_env) == [
        (
            ["--bg", "--session-id", PLACEHOLDER_NEW, "--remote-control", "--model", "opus"],
            str(folder),
        )
    ]


def test_start_new_remote_pending_without_a_link(
    vigil_env: VigilEnv,
    remote_client: TestClient,  # noqa: F811 - the fixture imported above
    tmp_path: Path,
) -> None:
    folder = tmp_path / "work"
    folder.mkdir()
    set_cwd(vigil_env, folder)
    install_claude(vigil_env, transcript=tmp_path / "unused.jsonl", link=None)
    review_id = flag_review(vigil_env, flag_card("--model", "opus"))
    response = remote_client.post(f"/reviews/{review_id}/suggestions/0/start-new")
    assert response.status_code == 200, response.text
    assert (response.json()["remote_url"], response.json()["pending"]) == (None, True)


def test_always_use_reloads_config(
    vigil_env: VigilEnv, web_client: TestClient, tmp_path: Path
) -> None:
    folder = tmp_path / "work"
    folder.mkdir()
    set_cwd(vigil_env, folder)
    vigil_env.bin.install("claude", stdout=CLAUDE_BG)
    config = Path(os.environ["XDG_CONFIG_HOME"]) / "vigil" / "config.toml"
    review_id = flag_review(vigil_env, flag_card("--model", "opus"))

    response = web_client.post(f"/reviews/{review_id}/suggestions/0/always-use")
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "default_flags": ["--model", "opus"]}
    assert f'claude_home = "{vigil_env.claude_home}"' in config.read_text()
    assert web_state(web_client).config.resume.default_flags == ("--model", "opus")
    assert launched(vigil_env) == []
    # The next plain resume uses it.
    assert web_client.post(f"/sessions/{NAMED}/resume").status_code == 200
    assert launched(vigil_env) == [
        (["--bg", "--resume", NAMED, "--remote-control", "--model", "opus"], str(folder))
    ]


def test_acting_on_suggestions_never_writes_under_the_claude_home(
    vigil_env: VigilEnv, web_client: TestClient, tmp_path: Path
) -> None:
    folder = tmp_path / "work"
    folder.mkdir()
    set_cwd(vigil_env, folder)
    vigil_env.bin.install("claude", stdout=CLAUDE_BG)
    review_id = flag_review(vigil_env, flag_card("--model", "opus"))
    freeze_tree(vigil_env.claude_home)
    for action in ("resume", "start-new", "always-use"):
        response = web_client.post(f"/reviews/{review_id}/suggestions/0/{action}")
        assert response.status_code == 200, (action, response.text)
