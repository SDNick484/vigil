"""Where a session's tokens went, computed from its transcript with no Claude involved.

Read from the transcript (the schema is undocumented; see `vigil.transcript` for how lines are
decoded tolerantly, and the fields below as found in Claude Code 2.1.312):

- A *turn* is one API response: a non-sidechain `assistant` record with a `message.usage` object.
  Claude Code writes one record per content block of a response, each repeating the response's
  `message.id` and `usage`; records sharing an id are one turn (the latest usage wins), so tokens
  are not counted once per block. Records without an id are one turn each.
- Tool results are `tool_result` blocks in non-sidechain `user` records. The tool name comes from
  the `tool_use` block with the same id; the size is the characters of the text in the result
  (the string, or the `text` blocks of a list). Only names and sizes are kept, never content.
- A compaction is a `system` record whose `subtype` is `compact_boundary`.
- The time span is the earliest to the latest `timestamp` on any record.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from vigil.transcript import _decode, _timestamp

TOP_TOOL_OUTPUTS = 10


@dataclass(frozen=True)
class TurnTokens:
    turn: int  # 1-based; 0 for the totals
    input: int
    output: int
    cache_write: int
    cache_read: int


@dataclass(frozen=True)
class ToolOutput:
    tool: str
    size: int  # characters of the tool result's text


@dataclass(frozen=True)
class SessionNumbers:
    turns: list[TurnTokens]
    totals: TurnTokens
    cache_hit_share: float  # cache_read / (input + cache_write + cache_read); 0.0 without tokens
    largest_tool_outputs: list[ToolOutput]  # the ten largest, biggest first
    compactions: int
    models: list[str]  # first seen first
    started_at: datetime | None
    ended_at: datetime | None
    launch_flags: list[str]  # flag names vigil launched the session with; may be incomplete


def compute_numbers(path: Path, launch_flags: list[str]) -> SessionNumbers:
    """Read `path` (read-only) and add up its tokens. Lines that are not JSON objects are
    skipped and invalid UTF-8 is replaced. Raises OSError when the file cannot be read."""
    usages: dict[str, dict[str, Any]] = {}  # message id (or a per-record key) -> usage, in order
    models: dict[str, None] = {}
    tool_names: dict[str, str] = {}
    results: list[tuple[str, int]] = []  # (tool_use_id, size)
    compactions = 0
    first: datetime | None = None
    last: datetime | None = None
    for number, line in enumerate(path.read_bytes().split(b"\n")):
        record = _decode(line)
        if record is None:
            continue
        at = _timestamp(record.get("timestamp"))
        if at is not None:
            first = at if first is None or at < first else first
            last = at if last is None or at > last else last
        kind = record.get("type")
        if kind == "system":
            compactions += record.get("subtype") == "compact_boundary"
            continue
        if kind not in ("user", "assistant") or record.get("isSidechain") is True:
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        if kind == "assistant":
            for block in blocks:
                if block.get("type") == "tool_use":
                    _remember_tool(tool_names, block)
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            message_id = message.get("id")
            usages[message_id if isinstance(message_id, str) else f"#{number}"] = usage
            model = message.get("model")
            if isinstance(model, str) and model and not model.startswith("<"):
                models[model] = None
        else:
            for block in blocks:
                if block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str):
                    results.append((block["tool_use_id"], _result_size(block.get("content"))))

    turns = [
        TurnTokens(
            turn=index,
            input=_tokens(usage, "input_tokens"),
            output=_tokens(usage, "output_tokens"),
            cache_write=_tokens(usage, "cache_creation_input_tokens"),
            cache_read=_tokens(usage, "cache_read_input_tokens"),
        )
        for index, usage in enumerate(usages.values(), start=1)
    ]
    totals = TurnTokens(
        turn=0,
        input=sum(t.input for t in turns),
        output=sum(t.output for t in turns),
        cache_write=sum(t.cache_write for t in turns),
        cache_read=sum(t.cache_read for t in turns),
    )
    denominator = totals.input + totals.cache_write + totals.cache_read
    outputs = sorted(
        (
            ToolOutput(tool=tool_names.get(tool_id, "unknown"), size=size)
            for tool_id, size in results
        ),
        key=lambda output: output.size,
        reverse=True,
    )
    return SessionNumbers(
        turns=turns,
        totals=totals,
        cache_hit_share=totals.cache_read / denominator if denominator else 0.0,
        largest_tool_outputs=outputs[:TOP_TOOL_OUTPUTS],
        compactions=compactions,
        models=list(models),
        started_at=first,
        ended_at=last,
        launch_flags=list(launch_flags),
    )


def _remember_tool(names: dict[str, str], block: dict[str, Any]) -> None:
    tool_id, name = block.get("id"), block.get("name")
    if isinstance(tool_id, str) and isinstance(name, str):
        names[tool_id] = name


def _tokens(usage: dict[str, Any], key: str) -> int:
    value = usage.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _result_size(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    if not isinstance(content, list):
        return 0
    return sum(
        len(block["text"])
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    )


def wall_clock(n: SessionNumbers) -> str | None:
    """The time from the first record to the last, as "N min" or "H h M min"; None when either
    end is unknown."""
    if n.started_at is None or n.ended_at is None:
        return None
    minutes = int((n.ended_at - n.started_at).total_seconds() // 60)
    return f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"


def numbers_to_dict(n: SessionNumbers) -> dict[str, object]:
    """`n` as plain JSON types, for storing with a review."""
    return {
        "turns": [_turn_to_dict(t) for t in n.turns],
        "totals": _turn_to_dict(n.totals),
        "cache_hit_share": n.cache_hit_share,
        "largest_tool_outputs": [{"tool": o.tool, "size": o.size} for o in n.largest_tool_outputs],
        "compactions": n.compactions,
        "models": list(n.models),
        "started_at": n.started_at.isoformat() if n.started_at else None,
        "ended_at": n.ended_at.isoformat() if n.ended_at else None,
        "launch_flags": list(n.launch_flags),
    }


def numbers_from_dict(d: dict[str, Any]) -> SessionNumbers:
    """The inverse of `numbers_to_dict`; raises KeyError or TypeError on a malformed `d`."""
    return SessionNumbers(
        turns=[_turn_from_dict(t) for t in d["turns"]],
        totals=_turn_from_dict(d["totals"]),
        cache_hit_share=float(d["cache_hit_share"]),
        largest_tool_outputs=[ToolOutput(o["tool"], o["size"]) for o in d["largest_tool_outputs"]],
        compactions=int(d["compactions"]),
        models=list(d["models"]),
        started_at=_timestamp(d["started_at"]),
        ended_at=_timestamp(d["ended_at"]),
        launch_flags=list(d["launch_flags"]),
    )


def _turn_to_dict(t: TurnTokens) -> dict[str, int]:
    return {
        "turn": t.turn,
        "input": t.input,
        "output": t.output,
        "cache_write": t.cache_write,
        "cache_read": t.cache_read,
    }


def _turn_from_dict(d: dict[str, Any]) -> TurnTokens:
    return TurnTokens(d["turn"], d["input"], d["output"], d["cache_write"], d["cache_read"])
