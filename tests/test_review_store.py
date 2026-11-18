import json
import sqlite3
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from typing import cast

from vigil.review.numbers import SessionNumbers, TurnTokens
from vigil.review.schema import Advice, Suggestion
from vigil.review.store import (
    INTERRUPTED,
    fail,
    fail_running,
    finish,
    get_review,
    insert_running,
    list_reviews,
    review_to_dict,
    running_review,
)

NAMED = "e47d2def-0bb3-4d50-9f9c-8e38e4cdcfba"
NEWEST = "f26f1b2c-d3b2-4389-b0ed-aba7e19e8594"
AT = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)

NUMBERS = SessionNumbers(
    turns=[TurnTokens(1, 1, 2, 3, 4)],
    totals=TurnTokens(0, 1, 2, 3, 4),
    cache_hit_share=0.5,
    largest_tool_outputs=[],
    compactions=1,
    models=["claude-placeholder"],
    started_at=AT,
    ended_at=AT + timedelta(minutes=5),
    launch_flags=[],
)

FLAG = Suggestion(
    kind="flag",
    title="Plan first",
    why="why",
    evidence="turn 3",
    flag_name="--permission-mode",
    flag_value="plan",
    file_target=None,
    file_text=None,
    file_settings=None,
)


def insert(conn: sqlite3.Connection, session_id: str = NAMED, at: datetime = AT) -> int:
    return insert_running(
        conn,
        session_id=session_id,
        machine="placeholder-host",
        numbers=NUMBERS,
        claude_version="2.1.312",
        model="sonnet",
        created_at=at,
    )


def test_insert_and_finish(index_db: sqlite3.Connection) -> None:
    review_id = insert(index_db)
    running = get_review(index_db, review_id)
    assert running is not None
    assert (running.status, running.suggestions, running.finished_at) == ("running", [], None)
    assert running.numbers == NUMBERS
    assert running_review(index_db) == running

    finish(
        index_db,
        review_id,
        suggestions=[FLAG],
        advice=[Advice("a", "b")],
        cost_usd=0.25,
        error="went over the budget ($0.25 of $0.20)",
    )
    done = get_review(index_db, review_id)
    assert done is not None
    assert done.status == "done"
    assert done.suggestions == [FLAG]
    assert done.advice == [Advice("a", "b")]
    assert done.cost_usd == 0.25
    assert done.error == "went over the budget ($0.25 of $0.20)"
    assert done.finished_at is not None
    assert (done.machine, done.claude_version, done.model) == (
        "placeholder-host",
        "2.1.312",
        "sonnet",
    )
    assert running_review(index_db) is None


def strict(text: str) -> object:
    """`text` as JSON, refusing NaN and Infinity."""

    def refuse(name: str) -> object:
        raise ValueError(name)

    return json.loads(text, parse_constant=refuse)


def test_non_finite_numbers_are_dropped_before_storing(index_db: sqlite3.Connection) -> None:
    numbers = replace(NUMBERS, cache_hit_share=float("nan"))
    review_id = insert_running(
        index_db,
        session_id=NAMED,
        machine="placeholder-host",
        numbers=numbers,
        claude_version=None,
        model=None,
    )
    settings = {"model": "opus", "autoCompactThreshold": float("inf"), "x": [float("nan"), 1]}
    card = replace(
        FLAG, kind="file", flag_name=None, flag_value=None, file_target="project_settings",
        file_settings=settings,
    )  # fmt: skip
    finish(index_db, review_id, suggestions=[card], advice=[], cost_usd=float("nan"))
    stored = index_db.execute(
        "SELECT numbers, suggestions, cost_usd FROM reviews WHERE id = ?", (review_id,)
    ).fetchone()
    assert "cache_hit_share" not in cast(dict[str, object], strict(stored["numbers"]))
    assert strict(stored["suggestions"]) == [
        {**asdict(card), "file_settings": {"model": "opus", "x": [1]}}
    ]
    assert stored["cost_usd"] is None


def test_fail_keeps_numbers(index_db: sqlite3.Connection) -> None:
    review_id = insert(index_db)
    fail(index_db, review_id, "claude: boom", cost_usd=0.01)
    failed = get_review(index_db, review_id)
    assert failed is not None
    assert (failed.status, failed.error, failed.cost_usd) == ("failed", "claude: boom", 0.01)
    assert failed.numbers == NUMBERS


def test_list_newest_first_per_session(index_db: sqlite3.Connection) -> None:
    first = insert(index_db, at=AT)
    second = insert(index_db, at=AT + timedelta(hours=1))
    insert(index_db, session_id=NEWEST)
    assert [r.id for r in list_reviews(index_db, NAMED)] == [second, first]
    assert get_review(index_db, 9999) is None


def test_fail_running(index_db: sqlite3.Connection) -> None:
    running = insert(index_db)
    done = insert(index_db)
    finish(index_db, done, suggestions=[], advice=[], cost_usd=0.1)
    assert fail_running(index_db) == 1
    stored = get_review(index_db, running)
    assert stored is not None
    assert (stored.status, stored.error) == ("failed", INTERRUPTED)
    assert INTERRUPTED == "vigil restarted during the review"
    other = get_review(index_db, done)
    assert other is not None and other.status == "done"


def test_reviews_go_with_their_session(index_db: sqlite3.Connection) -> None:
    insert(index_db)
    with index_db:
        index_db.execute("DELETE FROM sessions WHERE session_id = ?", (NAMED,))
    assert index_db.execute("SELECT count(*) FROM reviews").fetchone()[0] == 0


def test_review_to_dict(index_db: sqlite3.Connection) -> None:
    review_id = insert(index_db)
    finish(index_db, review_id, suggestions=[FLAG], advice=[], cost_usd=0.5)
    stored = get_review(index_db, review_id)
    assert stored is not None
    data = review_to_dict(stored)
    assert data["id"] == review_id
    assert data["status"] == "done"
    assert data["created_at"] == "2026-11-17T12:00:00.000000+00:00"
    assert data["suggestions"] == [
        {
            "kind": "flag",
            "title": "Plan first",
            "why": "why",
            "evidence": "turn 3",
            "flag_name": "--permission-mode",
            "flag_value": "plan",
            "file_target": None,
            "file_text": None,
            "file_settings": None,
        }
    ]
    assert data["numbers"]["compactions"] == 1  # type: ignore[index]
