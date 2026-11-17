import copy
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest

from vigil.transcript import TRUNCATE, FeatureKind, parse_transcript

FIXTURES = Path(__file__).parent / "fixtures" / "transcripts"
REAL = FIXTURES / "2.1.312"
HANDMADE = FIXTURES / "handmade" / "11111111-1111-4111-8111-111111111111.jsonl"

PLAIN = REAL / "7fb815ab-28b5-4123-85d0-64e6fd3b4141.jsonl"  # -p, then -p --resume (same file)
NAMED = REAL / "e47d2def-0bb3-4d50-9f9c-8e38e4cdcfba.jsonl"  # -n fixture-named
FORKED = REAL / "68e04fa1-b95c-4610-8221-261395d0ca5d.jsonl"  # --resume PLAIN --fork-session
COMMAND = REAL / "c8232e85-1b73-42a7-b6af-192de6b33a26.jsonl"  # /greet: Bash, Read, Skill
MCP_AGENT = REAL / "7bf4d32d-3526-48a2-9532-46c8ec375e87.jsonl"  # MCP tool and a subagent
PLAN = REAL / "f26f1b2c-d3b2-4389-b0ed-aba7e19e8594.jsonl"  # --permission-mode plan
SUBAGENT = REAL / MCP_AGENT.stem / "subagents" / "agent-a6baffeca9ef6541c.jsonl"

CWD = "/home/user/src/fixture-repo"
HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"


def features(*items: tuple[FeatureKind, str, int]) -> Counter[tuple[FeatureKind, str]]:
    return Counter({(kind, name): count for kind, name, count in items})


def test_extracts_core_fields() -> None:
    result = parse_transcript(PLAIN)
    facts = result.facts
    assert facts.session_id == PLAIN.stem
    assert facts.cwd == CWD
    assert facts.git_branch == "main"
    assert facts.version == "2.1.312"
    assert facts.title is None
    assert facts.first_prompt == "Reply with exactly: hello fixture"
    assert facts.last_user == "Reply with exactly: resumed fixture"
    assert facts.last_assistant == "resumed fixture"
    assert facts.message_count == 4
    assert facts.models == {HAIKU, SONNET}
    assert facts.started_at == datetime(2026, 11, 17, 15, 7, 10, 176000, tzinfo=UTC)
    assert facts.last_activity_at is not None and facts.last_activity_at > facts.started_at
    assert result.end_offset == PLAIN.stat().st_size


def test_title_from_name() -> None:
    facts = parse_transcript(NAMED).facts
    assert facts.title == "fixture-named"
    assert facts.first_prompt == "Reply with exactly: named fixture"
    assert facts.last_assistant == "named fixture"


def test_resume_links_previous() -> None:
    # 2.1.312 records name no previous session: -p --resume appends to the same file, and
    # --fork-session copies the earlier messages under a new id without naming the source.
    assert parse_transcript(PLAIN).facts.previous_session_id is None
    forked = parse_transcript(FORKED).facts
    assert forked.session_id == FORKED.stem
    assert forked.previous_session_id is None
    assert forked.first_prompt == "Reply with exactly: hello fixture"
    assert forked.last_user == "Reply with exactly: forked fixture"
    assert forked.last_assistant == "forked fixture"
    assert forked.message_count == 6


def test_features_counted() -> None:
    facts = parse_transcript(COMMAND).facts
    assert facts.features == features(
        ("slash_command", "/greet", 1),
        ("tool", "Bash", 1),
        ("tool", "Read", 1),
        ("tool", "Skill", 1),
        ("skill", "fixture-skill", 1),
        ("model", HAIKU, 6),
    )
    assert facts.first_prompt == "/greet"
    assert facts.last_assistant is not None and facts.last_assistant.startswith("done")
    # Meta records (the expanded command and skill bodies) are not messages.
    assert facts.message_count == 10


def test_mcp_and_subagent_features_counted() -> None:
    facts = parse_transcript(MCP_AGENT).facts
    assert facts.features == features(
        ("tool", "ToolSearch", 1),
        ("mcp_server", "fixture", 1),
        ("mcp_tool", "mcp__fixture__echo", 1),
        ("tool", "Agent", 1),
        ("subagent", "general-purpose", 1),
        ("model", HAIKU, 5),
    )
    # The task notification that reports the subagent's result is not a user prompt.
    assert facts.last_user == facts.first_prompt
    assert facts.first_prompt is not None and facts.first_prompt.startswith("First call the echo")
    assert facts.last_assistant == "finished"


def test_plan_mode_counted() -> None:
    facts = parse_transcript(PLAN).facts
    assert facts.features == features(
        ("plan_mode", "plan", 1), ("tool", "Write", 1), ("model", SONNET, 3)
    )


def test_subagent_transcript_carries_parent_id() -> None:
    facts = parse_transcript(SUBAGENT).facts
    assert facts.session_id == MCP_AGENT.stem
    assert facts.cwd == CWD
    assert facts.message_count == 0
    assert facts.first_prompt is None
    assert not facts.features


def test_truncates_to_500(tmp_path: Path) -> None:
    lines = PLAIN.read_text().splitlines()
    user = json.loads(next(line for line in lines if json.loads(line)["type"] == "user"))
    assistant = json.loads(next(line for line in lines if json.loads(line)["type"] == "assistant"))
    user["message"]["content"] = "x" * 2000
    assistant["message"]["content"] = [{"type": "text", "text": "y" * 2000}]
    title = {"type": "custom-title", "customTitle": "t" * 2000, "sessionId": user["sessionId"]}
    path = tmp_path / PLAIN.name
    path.write_text("".join(json.dumps(record) + "\n" for record in (title, user, assistant)))
    facts = parse_transcript(path).facts
    for text, char in [
        (facts.title, "t"),
        (facts.first_prompt, "x"),
        (facts.last_user, "x"),
        (facts.last_assistant, "y"),
    ]:
        assert text is not None
        assert len(text) == TRUNCATE
        assert text.startswith(char * (TRUNCATE - 1))
    assert facts.cwd == CWD  # paths are never truncated


def test_skips_malformed_and_partial_last_line(tmp_path: Path) -> None:
    data = HANDMADE.read_bytes()
    partial_start = data.rfind(b"\n") + 1
    assert partial_start < len(data)
    result = parse_transcript(HANDMADE)
    assert result.end_offset == partial_start
    facts = result.facts
    assert facts.session_id == HANDMADE.stem
    assert facts.cwd == "/home/user/src/broken-repo"
    assert facts.git_branch == "feature/x"
    assert facts.last_assistant == "reply after bad bytes"
    assert facts.features == features(("tool", "Bash", 1), ("model", HAIKU, 1))
    assert facts.message_count == 3  # two user records (one with non-text content), one assistant
    assert facts.last_activity_at == datetime(2026, 11, 17, 10, 0, 3, tzinfo=UTC)

    # Once Claude Code finishes the line, the next parse from end_offset reads it whole.
    path = tmp_path / HANDMADE.name
    rest = json.loads(data[partial_start:] + b'shed"}}\n')
    path.write_bytes(data + b'shed"}}\n')
    assert rest["message"]["content"] == "never finished"
    more = parse_transcript(path, result.end_offset, result.facts)
    assert more.end_offset == path.stat().st_size
    assert more.facts.last_user == "never finished"
    assert more.facts.message_count == 4


def test_invalid_utf8_does_not_crash() -> None:
    facts = parse_transcript(HANDMADE).facts
    assert facts.first_prompt == "first prompt with bad bytes: ��"
    assert facts.last_user == facts.first_prompt


@pytest.mark.parametrize(
    "path", [*sorted(REAL.glob("*.jsonl")), SUBAGENT, HANDMADE], ids=lambda path: path.stem[:8]
)
def test_incremental_parse_equals_full_parse(path: Path, tmp_path: Path) -> None:
    data = path.read_bytes()
    full = parse_transcript(path)
    copy_path = tmp_path / path.name
    copy_path.write_bytes(data[: len(data) // 2])  # usually cuts a line in half
    first = parse_transcript(copy_path)
    snapshot = copy.deepcopy(first.facts)
    assert first.end_offset <= len(data) // 2
    copy_path.write_bytes(data)
    second = parse_transcript(copy_path, first.end_offset, first.facts)
    assert second.facts == full.facts
    assert second.end_offset == full.end_offset
    assert first.facts == snapshot  # prior is not changed in place


def test_parse_at_end_of_file_changes_nothing() -> None:
    full = parse_transcript(PLAIN)
    again = parse_transcript(PLAIN, full.end_offset, full.facts)
    assert again.facts == full.facts
    assert again.end_offset == full.end_offset
