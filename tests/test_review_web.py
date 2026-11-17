import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import LOOPBACK_URL, VigilEnv, freeze_tree
from tests.test_review_runner import ANSWER, claude_result
from tests.test_review_store import NUMBERS, insert
from vigil.config import load_config
from vigil.index.db import connect
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


def test_review_fragment_404s(vigil_env: VigilEnv, web_client: TestClient) -> None:
    conn = connect(vigil_env.index)
    review_id = insert(conn)
    conn.close()
    assert web_client.get(f"/sessions/{NAMED}/reviews/{review_id + 1}").status_code == 404
    other = "f26f1b2c-d3b2-4389-b0ed-aba7e19e8594"
    assert web_client.get(f"/sessions/{other}/reviews/{review_id}").status_code == 404


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
