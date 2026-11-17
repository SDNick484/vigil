import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import FakeBin, freeze_tree
from vigil.catalog import CatalogEntry
from vigil.config import ReviewConfig
from vigil.index.queries import get_session
from vigil.review.numbers import SessionNumbers, TurnTokens, compute_numbers
from vigil.review.runner import (
    ReviewRefused,
    ReviewSlot,
    build_prompt,
    review_argv,
    run_review,
)
from vigil.review.schema import SCHEMA, Advice, Suggestion, parse_output
from vigil.review.store import get_review

NAMED = "e47d2def-0bb3-4d50-9f9c-8e38e4cdcfba"

ANSWER: dict[str, Any] = {
    "suggestions": [
        {
            "kind": "flag",
            "title": "Plan first",
            "why": "The first turns rewrote the same file three times.",
            "evidence": "turn 3",
            "flag": {"name": "--permission-mode", "value": "plan"},
        },
        {
            "kind": "file",
            "title": "Note the test command",
            "why": "Claude looked for how to run the tests twice.",
            "evidence": "turn 5",
            "file": {"target": "project_claude_md", "text": "Run tests with `make test`."},
        },
        {
            "kind": "habit",
            "title": "Say which file",
            "why": "Naming the file saves a search.",
            "evidence": "turn 1",
        },
    ],
    "token_advice": [{"title": "Trim tool output", "why": "One Bash result was 40k characters."}],
}


def claude_result(**fields: Any) -> str:
    """What `claude -p --output-format json` prints, with `fields` on top of a success."""
    result: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "",
        "total_cost_usd": 0.12,
        "errors": [],
        "num_turns": 1,
        "session_id": "00000000-0000-4000-8000-000000000000",
    }
    result.update(fields)
    return json.dumps(result)


def numbers() -> SessionNumbers:
    totals = TurnTokens(0, 10, 20, 30, 40)
    return SessionNumbers(
        turns=[TurnTokens(1, 10, 20, 30, 40)],
        totals=totals,
        cache_hit_share=0.5,
        largest_tool_outputs=[],
        compactions=0,
        models=["claude-placeholder"],
        started_at=None,
        ended_at=None,
        launch_flags=["--remote-control"],
    )


@pytest.fixture
def session_cwd(tmp_path: Path, index_db: sqlite3.Connection) -> Path:
    """A real folder for the NAMED session to have been run in (the fixture's does not exist)."""
    folder = tmp_path / "work"
    folder.mkdir()
    with index_db:
        index_db.execute("UPDATE sessions SET cwd = ? WHERE session_id = ?", (str(folder), NAMED))
    return folder


def review(conn: sqlite3.Connection, cfg: ReviewConfig | None = None) -> int:
    row = get_session(conn, NAMED)
    assert row is not None
    return run_review(row, cfg or ReviewConfig(), [], ("--model", "opus"), conn)


def test_review_argv_exact() -> None:
    cfg = ReviewConfig(max_budget_usd=1.0, model=None)
    argv = review_argv("SESSION", cfg, '{"type":"object"}', "the prompt")
    assert argv == [
        "claude",
        "-p",
        "--resume",
        "SESSION",
        "--fork-session",
        "--no-session-persistence",
        "--tools",
        "",
        "--output-format",
        "json",
        "--json-schema",
        '{"type":"object"}',
        "--max-budget-usd",
        "1.00",
        "the prompt",
    ]


def test_review_argv_with_model_keeps_the_prompt_last() -> None:
    argv = review_argv("SESSION", ReviewConfig(), "{}", "the prompt")
    assert argv[-5:] == ["--max-budget-usd", "3.00", "--model", "sonnet", "the prompt"]


def test_review_config_defaults() -> None:
    assert ReviewConfig() == ReviewConfig(max_budget_usd=3.0, model="sonnet")


def test_schema_lists_the_three_kinds() -> None:
    item = SCHEMA["properties"]["suggestions"]["items"]
    assert item["properties"]["kind"]["enum"] == ["flag", "file", "habit"]
    assert set(SCHEMA["required"]) == {"suggestions", "token_advice"}


def test_parse_output_drops_invalid_and_truncates() -> None:
    long_why = "w" * 900
    items: list[Any] = [
        {"kind": "other", "title": "dropped", "why": "x", "evidence": "y"},
        {"kind": "habit", "title": "kept", "why": long_why, "evidence": "turn 1"},
        "not an object",
        {"kind": "flag", "title": "no flag name", "why": "x", "evidence": "y"},
        {
            "kind": "file",
            "title": "bad target",
            "why": "x",
            "evidence": "y",
            "file": {"target": "/etc"},
        },
    ]
    items += [{"kind": "habit", "title": f"h{n}", "why": "x", "evidence": "y"} for n in range(11)]
    suggestions, advice = parse_output(
        {"suggestions": items, "token_advice": [{"title": "t", "why": "w"}, {"title": 3}]}
    )
    assert len(suggestions) == 10
    assert suggestions[0].title == "kept"
    assert len(suggestions[0].why) == 500
    assert all(s.kind == "habit" for s in suggestions)
    assert advice == [Advice(title="t", why="w")]


def test_parse_output_flag_and_file_items() -> None:
    suggestions, advice = parse_output(ANSWER)
    flag, file, habit = suggestions
    assert flag == Suggestion(
        kind="flag",
        title="Plan first",
        why="The first turns rewrote the same file three times.",
        evidence="turn 3",
        flag_name="--permission-mode",
        flag_value="plan",
        file_target=None,
        file_text=None,
        file_settings=None,
    )
    assert (file.file_target, file.file_text) == (
        "project_claude_md",
        "Run tests with `make test`.",
    )
    assert habit.kind == "habit" and habit.flag_name is None
    assert advice == [Advice("Trim tool output", "One Bash result was 40k characters.")]


def test_parse_output_settings_file_needs_an_object() -> None:
    items = [
        {"kind": "file", "title": "a", "why": "b", "evidence": "c",
         "file": {"target": "project_settings", "settings": {"model": "x" * 900}}},
        {"kind": "file", "title": "a", "why": "b", "evidence": "c",
         "file": {"target": "project_settings", "text": "not settings"}},
    ]  # fmt: skip
    suggestions, _ = parse_output({"suggestions": items})
    assert len(suggestions) == 1
    assert suggestions[0].file_settings == {"model": "x" * 500}


def test_parse_output_strips_control_characters() -> None:
    item = {
        "kind": "file",
        "title": "Copy \x1b]52;c;cGF5bG9hZA==\x07this",
        "why": "line one\nline\x00 two\x9b31m",
        "evidence": "turn\r 2",
        "file": {"target": "project_settings", "settings": {"model\x1b": ["a\x07b"]}},
    }
    (suggestion,), _ = parse_output({"suggestions": [item]})
    assert suggestion.title == "Copy ]52;c;cGF5bG9hZA==this"
    assert "\x1b" not in suggestion.title and "\x07" not in suggestion.title
    assert suggestion.why == "line one\nline two31m"
    assert suggestion.evidence == "turn 2"
    assert suggestion.file_settings == {"model": ["ab"]}


def test_parse_output_tolerates_garbage() -> None:
    assert parse_output(None) == ([], [])
    assert parse_output({"suggestions": "no"}) == ([], [])


def test_build_prompt_has_numbers_catalog_and_flags() -> None:
    catalog = [
        CatalogEntry("flag", "--model", None, None, "Model for the session"),
        CatalogEntry("subcommand", "agents", None, None, "List agents"),
        CatalogEntry("subcommand_flag", "--json", None, "agents", "Print JSON"),
    ]
    prompt = build_prompt(numbers(), catalog, ("--model", "opus"))
    assert "--model: Model for the session" in prompt
    assert "agents: List agents" in prompt
    assert "--model opus" in prompt
    assert "--remote-control" in prompt
    assert "may be incomplete" in prompt
    assert "claude-placeholder" in prompt
    assert "at most ten" in prompt


def test_run_review_success(
    index_db: sqlite3.Connection, fake_bin: FakeBin, session_cwd: Path
) -> None:
    fake_bin.install("claude", stdout=claude_result(structured_output=ANSWER))
    review_id = review(index_db)
    stored = get_review(index_db, review_id)
    assert stored is not None
    assert stored.status == "done"
    assert stored.cost_usd == 0.12
    assert stored.error is None
    assert stored.session_id == NAMED
    assert stored.model == "sonnet"
    assert [s.title for s in stored.suggestions] == [
        "Plan first",
        "Note the test command",
        "Say which file",
    ]
    assert stored.advice == [Advice("Trim tool output", "One Bash result was 40k characters.")]
    assert stored.finished_at is not None
    (call,) = fake_bin.calls("claude")
    assert call.cwd == str(session_cwd)
    assert call.argv[:4] == ["-p", "--resume", NAMED, "--fork-session"]
    assert json.loads(call.argv[call.argv.index("--json-schema") + 1]) == SCHEMA
    assert "--model opus" in call.argv[-1]  # the default flags are in the prompt


def test_review_failure_is_stored(
    index_db: sqlite3.Connection, fake_bin: FakeBin, session_cwd: Path
) -> None:
    fake_bin.install(
        "claude", stdout=json.dumps({"is_error": True, "result": "budget exceeded"}), exit_code=1
    )
    stored = get_review(index_db, review(index_db))
    assert stored is not None
    assert stored.status == "failed"
    assert stored.error is not None and "budget exceeded" in stored.error
    assert stored.suggestions == []
    # The numbers vigil computed itself are kept even though Claude's step failed.
    row = get_session(index_db, NAMED)
    assert row is not None
    assert stored.numbers == compute_numbers(Path(row.transcript_path), [])


def test_over_budget_with_an_answer_is_done(
    index_db: sqlite3.Connection, fake_bin: FakeBin, session_cwd: Path
) -> None:
    output = claude_result(
        subtype="error_max_budget_usd",
        is_error=True,
        errors=["Reached maximum budget ($3.00)"],
        total_cost_usd=3.4,
        structured_output=ANSWER,
    )
    fake_bin.install("claude", stdout=output, exit_code=1)
    stored = get_review(index_db, review(index_db))
    assert stored is not None
    assert stored.status == "done"
    assert stored.cost_usd == 3.4
    assert stored.error == "went over the budget ($3.40 of $3.00)"
    assert len(stored.suggestions) == 3


def test_answer_read_from_result_text(
    index_db: sqlite3.Connection, fake_bin: FakeBin, session_cwd: Path
) -> None:
    text = "```json\n" + json.dumps(ANSWER) + "\n```"
    fake_bin.install("claude", stdout=claude_result(result=text))
    stored = get_review(index_db, review(index_db))
    assert stored is not None
    assert (stored.status, len(stored.suggestions)) == ("done", 3)


def test_answer_not_in_the_expected_form_fails(
    index_db: sqlite3.Connection, fake_bin: FakeBin, session_cwd: Path
) -> None:
    fake_bin.install("claude", stdout=claude_result(result="Here are my thoughts."))
    stored = get_review(index_db, review(index_db))
    assert stored is not None
    assert stored.status == "failed"
    assert stored.error is not None and "Here are my thoughts." in stored.error


def test_crash_without_json_fails(
    index_db: sqlite3.Connection, fake_bin: FakeBin, session_cwd: Path
) -> None:
    fake_bin.install("claude", stdout="segfault\n", exit_code=139)
    stored = get_review(index_db, review(index_db))
    assert stored is not None
    assert stored.status == "failed"
    assert stored.error is not None and "139" in stored.error


def test_review_refused_when_folder_gone(index_db: sqlite3.Connection, fake_bin: FakeBin) -> None:
    fake_bin.install("claude", stdout=claude_result(structured_output=ANSWER))
    with pytest.raises(ReviewRefused, match="folder no longer exists"):
        review(index_db)
    assert fake_bin.calls("claude") == []
    assert index_db.execute("SELECT count(*) FROM reviews").fetchone()[0] == 0


def test_review_refused_without_claude(
    index_db: sqlite3.Connection, fake_bin: FakeBin, session_cwd: Path
) -> None:
    with pytest.raises(ReviewRefused, match="not on PATH"):
        review(index_db)


def test_review_slot_one_at_a_time() -> None:
    slot = ReviewSlot()
    assert slot.try_start()
    assert not slot.try_start()
    # Finished from another thread, as the web service's background thread does.
    finisher = threading.Thread(target=slot.finish)
    finisher.start()
    finisher.join()
    assert slot.try_start()
    slot.finish()


def test_claude_home_untouched_by_review(
    index_db: sqlite3.Connection, fake_bin: FakeBin, session_cwd: Path, claude_home: Path
) -> None:
    def listing() -> dict[str, tuple[int, int]]:
        return {
            str(path.relative_to(claude_home)): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in sorted(claude_home.rglob("*"))
        }

    freeze_tree(claude_home)
    before = listing()
    fake_bin.install("claude", stdout=claude_result(structured_output=ANSWER))
    stored = get_review(index_db, review(index_db))
    assert stored is not None and stored.status == "done"
    assert listing() == before
