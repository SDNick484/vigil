"""Reviews in the index (migration 0005): one row per review, `running` until it is `done` or
`failed`. A review goes when its session leaves the index (the foreign key cascades)."""

import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from vigil.index.db import from_db, to_db
from vigil.review.numbers import SessionNumbers, numbers_from_dict, numbers_to_dict
from vigil.review.schema import Advice, Suggestion

ReviewStatus = Literal["running", "done", "failed"]

INTERRUPTED = "vigil restarted during the review"
"""The error of a review that was still running when the service started."""


@dataclass(frozen=True)
class Review:
    id: int
    session_id: str
    machine: str
    created_at: datetime
    finished_at: datetime | None
    claude_version: str | None
    model: str | None
    cost_usd: float | None
    """What Claude Code reported the review cost, in USD."""
    status: ReviewStatus
    numbers: SessionNumbers | None
    """The numbers the review was given; None only if the stored JSON cannot be read."""
    suggestions: list[Suggestion]
    advice: list[Advice]
    error: str | None
    """Why it failed; on a review that is done, a note such as going over the budget."""


def _finite(value: Any) -> bool:
    return not isinstance(value, float) or math.isfinite(value)


def _without_non_finite(value: Any) -> Any:
    """`value` (JSON-like) with every NaN and Infinity, which JSON cannot hold, dropped from the
    objects and lists in it."""
    if isinstance(value, dict):
        return {key: _without_non_finite(item) for key, item in value.items() if _finite(item)}
    if isinstance(value, list):
        return [_without_non_finite(item) for item in value if _finite(item)]
    return value


def _cost(cost_usd: float | None) -> float | None:
    return cost_usd if cost_usd is not None and math.isfinite(cost_usd) else None


def _dump(value: Any) -> str:
    """`value` as strict JSON, for a column: non-finite numbers dropped first."""
    return json.dumps(_without_non_finite(value), allow_nan=False)


def _load(text: str | None) -> Any:
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _numbers(text: str) -> SessionNumbers | None:
    data = _load(text)
    try:
        return numbers_from_dict(data)
    except KeyError, TypeError, ValueError:
        return None


def _suggestions(text: str | None) -> list[Suggestion]:
    items = _load(text)
    found: list[Suggestion] = []
    for item in items if isinstance(items, list) else []:
        try:
            found.append(Suggestion(**item))
        except TypeError:
            continue
    return found


def _advice(text: str | None) -> list[Advice]:
    items = _load(text)
    found: list[Advice] = []
    for item in items if isinstance(items, list) else []:
        try:
            found.append(Advice(**item))
        except TypeError:
            continue
    return found


def _review(row: sqlite3.Row) -> Review:
    created_at = from_db(row["created_at"])
    assert created_at is not None  # NOT NULL in the schema
    return Review(
        id=row["id"],
        session_id=row["session_id"],
        machine=row["machine"],
        created_at=created_at,
        finished_at=from_db(row["finished_at"]),
        claude_version=row["claude_version"],
        model=row["model"],
        cost_usd=row["cost_usd"],
        status=row["status"],
        numbers=_numbers(row["numbers"]),
        suggestions=_suggestions(row["suggestions"]),
        advice=_advice(row["advice"]),
        error=row["error"],
    )


def insert_running(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    machine: str,
    numbers: SessionNumbers,
    claude_version: str | None,
    model: str | None,
    created_at: datetime | None = None,
) -> int:
    """Store a review that is starting now (or at `created_at`); return its id."""
    with conn:
        cursor = conn.execute(
            "INSERT INTO reviews (session_id, machine, created_at, claude_version, model, status, "
            "numbers) VALUES (?, ?, ?, ?, ?, 'running', ?)",
            (
                session_id,
                machine,
                to_db(created_at or datetime.now(UTC)),
                claude_version,
                model,
                _dump(numbers_to_dict(numbers)),
            ),
        )
    review_id = cursor.lastrowid
    assert review_id is not None
    return review_id


def finish(
    conn: sqlite3.Connection,
    review_id: int,
    *,
    suggestions: list[Suggestion],
    advice: list[Advice],
    cost_usd: float | None,
    model: str | None = None,
    error: str | None = None,
) -> None:
    """Mark a review done with what Claude suggested. `model` replaces the stored model when
    given; `error` is a note (the review still counts as done)."""
    with conn:
        conn.execute(
            "UPDATE reviews SET status = 'done', finished_at = ?, suggestions = ?, advice = ?, "
            "cost_usd = ?, model = COALESCE(?, model), error = ? WHERE id = ?",
            (
                to_db(datetime.now(UTC)),
                _dump([asdict(s) for s in suggestions]),
                _dump([asdict(a) for a in advice]),
                _cost(cost_usd),
                model,
                error,
                review_id,
            ),
        )


def fail(
    conn: sqlite3.Connection, review_id: int, error: str, *, cost_usd: float | None = None
) -> None:
    """Mark a review failed, saying why. Its numbers stay."""
    with conn:
        conn.execute(
            "UPDATE reviews SET status = 'failed', finished_at = ?, error = ?, cost_usd = ? "
            "WHERE id = ?",
            (to_db(datetime.now(UTC)), error, _cost(cost_usd), review_id),
        )


def fail_if_running(conn: sqlite3.Connection, review_id: int, error: str) -> bool:
    """Mark a review failed, saying why, unless it has already finished; return whether it was
    still running."""
    with conn:
        cursor = conn.execute(
            "UPDATE reviews SET status = 'failed', finished_at = ?, error = ? "
            "WHERE id = ? AND status = 'running'",
            (to_db(datetime.now(UTC)), error, review_id),
        )
    return cursor.rowcount > 0


def fail_running(conn: sqlite3.Connection) -> int:
    """Mark every review still `running` failed with `INTERRUPTED` (the service calls this when
    it starts); return how many there were."""
    with conn:
        cursor = conn.execute(
            "UPDATE reviews SET status = 'failed', finished_at = ?, error = ? "
            "WHERE status = 'running'",
            (to_db(datetime.now(UTC)), INTERRUPTED),
        )
    return cursor.rowcount


def get_review(conn: sqlite3.Connection, review_id: int) -> Review | None:
    row = conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
    return _review(row) if row is not None else None


def list_reviews(conn: sqlite3.Connection, session_id: str) -> list[Review]:
    """The reviews of `session_id`, newest first."""
    rows = conn.execute(
        "SELECT * FROM reviews WHERE session_id = ? ORDER BY created_at DESC, id DESC",
        (session_id,),
    )
    return [_review(row) for row in rows]


def running_review(conn: sqlite3.Connection) -> Review | None:
    """The newest review still running, of any session."""
    row = conn.execute(
        "SELECT * FROM reviews WHERE status = 'running' ORDER BY created_at DESC, id DESC LIMIT 1"
    ).fetchone()
    return _review(row) if row is not None else None


def review_to_dict(review: Review) -> dict[str, object]:
    """`review` as plain JSON types, as the API serves it and `vigil review --json` prints it."""
    return {
        "id": review.id,
        "session_id": review.session_id,
        "machine": review.machine,
        "created_at": to_db(review.created_at),
        "finished_at": to_db(review.finished_at),
        "claude_version": review.claude_version,
        "model": review.model,
        "cost_usd": review.cost_usd,
        "status": review.status,
        "numbers": numbers_to_dict(review.numbers) if review.numbers is not None else None,
        "suggestions": [asdict(s) for s in review.suggestions],
        "advice": [asdict(a) for a in review.advice],
        "error": review.error,
    }
