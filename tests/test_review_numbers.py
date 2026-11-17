import json
from datetime import UTC, datetime
from pathlib import Path

from vigil.index.db import connect
from vigil.index.queries import record_launch, vigil_launch_flags
from vigil.review.numbers import (
    SessionNumbers,
    ToolOutput,
    TurnTokens,
    compute_numbers,
    numbers_from_dict,
    numbers_to_dict,
)

USAGE = Path(__file__).parent / "fixtures" / "transcripts" / "handmade" / "usage.jsonl"


def test_totals_and_turns() -> None:
    n = compute_numbers(USAGE, [])
    # Turn 1 spans two records of one API response (same message id): counted once.
    assert n.turns == [
        TurnTokens(turn=1, input=10, output=100, cache_write=2000, cache_read=0),
        TurnTokens(turn=2, input=5, output=50, cache_write=500, cache_read=2000),
        TurnTokens(turn=3, input=3, output=30, cache_write=0, cache_read=2500),
    ]
    assert n.totals == TurnTokens(turn=0, input=18, output=180, cache_write=2500, cache_read=4500)
    assert n.models == ["claude-sonnet-5-5", "claude-opus-5-5"]
    assert n.started_at == datetime(2026, 11, 17, 12, 0, 0, tzinfo=UTC)
    assert n.ended_at == datetime(2026, 11, 17, 12, 0, 12, tzinfo=UTC)


def test_cache_hit_share() -> None:
    assert compute_numbers(USAGE, []).cache_hit_share == 4500 / (18 + 2500 + 4500)


def test_cache_hit_share_is_zero_without_tokens(tmp_path: Path) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.write_text('{"type":"user","message":{"role":"user","content":"hi"}}\n')
    n = compute_numbers(empty, [])
    assert n.cache_hit_share == 0.0
    assert n.turns == [] and n.totals == TurnTokens(0, 0, 0, 0, 0)
    assert n.started_at is None and n.ended_at is None


def test_largest_tool_outputs_named() -> None:
    # A list of text blocks adds up; a sidechain tool result (90 000 characters) is ignored.
    assert compute_numbers(USAGE, []).largest_tool_outputs == [
        ToolOutput(tool="Bash", size=40000),
        ToolOutput(tool="Read", size=1200),
    ]


def test_largest_tool_outputs_top_ten_and_unknown_tool(tmp_path: Path) -> None:
    lines = [
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": f"t{i}", "content": "x" * (i + 1)}
                ]
            },
        }
        for i in range(12)
    ]
    path = tmp_path / "many.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    outputs = compute_numbers(path, []).largest_tool_outputs
    assert [o.size for o in outputs] == list(range(12, 2, -1))
    assert {o.tool for o in outputs} == {"unknown"}


def test_compactions_counted() -> None:
    assert compute_numbers(USAGE, []).compactions == 1


def test_sidechain_ignored() -> None:
    n = compute_numbers(USAGE, [])
    assert len(n.turns) == 3
    assert n.totals.input == 18  # not 18 + 999
    assert "claude-haiku-5-5" not in n.models


def test_bad_lines_skipped(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    good = USAGE.read_bytes().splitlines()[1]
    path.write_bytes(b"not json\n\xff\xfe\n[1]\n" + good + b"\n")
    assert len(compute_numbers(path, []).turns) == 1


def test_launch_flags_passed_through() -> None:
    assert compute_numbers(USAGE, ["--model", "--effort"]).launch_flags == ["--model", "--effort"]


def test_numbers_roundtrip() -> None:
    n = compute_numbers(USAGE, ["--model"])
    data = numbers_to_dict(n)
    assert json.loads(json.dumps(data)) == data  # plain JSON types only
    assert numbers_from_dict(data) == n
    empty = SessionNumbers([], TurnTokens(0, 0, 0, 0, 0), 0.0, [], 0, [], None, None, [])
    assert numbers_from_dict(numbers_to_dict(empty)) == empty


def test_vigil_launch_flags_only_vigil_source(tmp_path: Path) -> None:
    conn = connect(tmp_path / "index.db")
    at = datetime(2026, 11, 17, tzinfo=UTC)

    def launch(source: str, flags: list[str], session_id: str | None) -> None:
        record_launch(
            conn,
            source=source,  # type: ignore[arg-type]
            subcommand=None,
            flags=flags,
            at=at,
            session_id=session_id,
        )

    launch("vigil", ["--model", "--remote-control"], "s1")
    launch("shell", ["--effort"], "s1")
    launch("vigil", ["--model", "--name"], "s1")
    launch("vigil", ["--permission-mode"], "s2")
    assert vigil_launch_flags(conn, "s1") == ["--model", "--remote-control", "--name"]
    assert vigil_launch_flags(conn, "nope") == []
    conn.close()
