"""Tolerant, incremental parser for Claude Code session transcripts.

A transcript is `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`: one JSON object per line,
appended to while the session runs. The schema is undocumented and changes between versions, so
this parser reads only what it needs, ignores unknown record types and fields, skips lines that
are not JSON objects, decodes invalid UTF-8 with replacement characters, and never consumes a
final line that has no newline yet (Claude Code may be mid-write).

What is read, as found in real Claude Code 2.1.312 transcripts (see tests/fixtures/README.md):

- `session_id`: the `sessionId` of the first record that carries one; the file name stem until
  then. Subagent transcripts (`<session-id>/subagents/agent-*.jsonl`) carry the parent's
  `sessionId` and contain only sidechain records.
- `cwd`: the first `cwd` field seen on any record (never decoded from the directory name).
  `git_branch` and `version`: the latest `gitBranch` / `version` seen.
- `started_at` / `last_activity_at`: the earliest / latest `timestamp` on any record.
- `title`: the latest `custom-title` record's `customTitle` (written by `claude -n <name>`).
- Messages are `user` and `assistant` records whose `isSidechain` is not true and whose `isMeta`
  is not true (meta records hold expanded slash-command and skill bodies).
  `message_count` counts those records (tool results are `user` records, and one API response
  can span several `assistant` records, one per content block).
- A user *prompt* is a counted `user` record whose `promptSource` is not `"system"` (task
  notifications are system prompts) and whose content is a string or text blocks, not tool
  results. `first_prompt` is the first one, `last_user` the latest. A slash command prompt
  (`<command-name>/greet</command-name>`) is shown as `/greet` plus any `<command-args>`, and a
  bash-mode input (`<bash-input>ls</bash-input>`, typed as `!ls`) as `! ls`. Neither keeps the
  `first_prompt` slot once a typed prompt follows, so a session started with `/model` is named
  after what was asked next. The output of local commands and bash mode
  (`<local-command-stdout>`, `<bash-stdout>` and their `stderr` forms) is recorded as `user`
  records too; those are not prompts.
- `last_assistant`: the latest non-empty `text` block of an assistant record.
- Text fields (`title`, `first_prompt`, `last_user`, `last_assistant`) are cut to `TRUNCATE`
  characters.
- `remote_url` / `remote_url_at`: the `url` and `timestamp` of the newest `system` record whose
  `subtype` is `bridge_status` (written when `/remote-control` is active), when the URL is a
  `https://claude.ai/code/...` link (`REMOTE_URL`). System records are not messages.
- `previous_session_id`: 2.1.312 records carry no reference to a previous session. `claude -p
  --resume <id>` appends to the same file; `--fork-session` writes a new file that copies the
  earlier messages (same `uuid`s, new `sessionId`) without naming the source session. So this is
  only ever the value passed in via `prior`; chains rely on the scanner's fallback.

Feature mapping (`features` counts `(kind, name)` pairs):

- `tool`: each `tool_use` block in an assistant record, by `name`, except MCP tools.
- `mcp_server` and `mcp_tool`: a `tool_use` named `mcp__<server>__<tool>` counts once as
  `("mcp_server", "<server>")` and once as `("mcp_tool", "mcp__<server>__<tool>")`.
- `skill`: a `Skill` tool use, by its `input.skill` (also counted as tool `Skill`).
- `subagent`: an `Agent` tool use, by its `input.subagent_type` (also counted as tool `Agent`).
  The older `Task` tool name with the same input is accepted too; it is not in the fixtures.
- `slash_command`: a user prompt containing `<command-name>/name</command-name>`, by `/name`.
- `plan_mode`: a user prompt whose `permissionMode` is `"plan"`, as `("plan_mode", "plan")`.
- `model`: each counted assistant record, by `message.model`; `models` holds the set. Model names
  starting with `<` (placeholders such as `<synthetic>`) are ignored.
"""

import copy
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

TRUNCATE = 500

FeatureKind = Literal[
    "tool", "slash_command", "skill", "subagent", "mcp_server", "mcp_tool", "plan_mode", "model"
]

_COMMAND_NAME = re.compile(r"<command-name>\s*([^<]+?)\s*</command-name>")
_COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
_BASH_INPUT = re.compile(r"<bash-input>(.*?)</bash-input>", re.DOTALL)
_COMMAND_OUTPUT = re.compile(r"\s*<(?:local-command|bash)-(?:stdout|stderr)>")
_SUBAGENT_TOOLS = frozenset({"Agent", "Task"})

REMOTE_URL = re.compile(r"https://claude\.ai/code/[A-Za-z0-9_-]{1,128}")
"""A Claude Remote Control link; only a `fullmatch` is kept."""


@dataclass
class SessionFacts:
    session_id: str
    cwd: str | None = None
    git_branch: str | None = None
    version: str | None = None
    title: str | None = None
    first_prompt: str | None = None
    last_user: str | None = None
    last_assistant: str | None = None
    started_at: datetime | None = None
    last_activity_at: datetime | None = None
    message_count: int = 0
    models: set[str] = field(default_factory=set)
    features: Counter[tuple[FeatureKind, str]] = field(default_factory=Counter)
    previous_session_id: str | None = None
    remote_url: str | None = None
    remote_url_at: datetime | None = None


@dataclass
class ParseResult:
    facts: SessionFacts
    end_offset: int  # byte offset just past the last complete line


def parse_transcript(
    path: Path, start_offset: int = 0, prior: SessionFacts | None = None
) -> ParseResult:
    """Parse the complete lines of `path` from `start_offset` on.

    With `prior`, the new lines are merged into a copy of it (`prior` itself is not changed), so
    parsing a file in pieces gives the same facts as parsing it whole. A final line without a
    newline is left unread: `end_offset` stays at its start.
    """
    facts = copy.deepcopy(prior) if prior is not None else SessionFacts(session_id=path.stem)
    with path.open("rb") as handle:
        handle.seek(start_offset)
        data = handle.read()
    complete = data.rfind(b"\n") + 1
    for line in data[:complete].split(b"\n"):
        record = _decode(line)
        if record is not None:
            _apply(facts, record, path.stem)
    return ParseResult(facts=facts, end_offset=start_offset + complete)


def _decode(line: bytes) -> dict[str, Any] | None:
    if not line.strip():
        return None
    try:
        record = json.loads(line.decode("utf-8", errors="replace"))
    except ValueError, RecursionError:
        return None
    return record if isinstance(record, dict) else None


def _apply(facts: SessionFacts, record: dict[str, Any], stem: str) -> None:
    session_id = _str(record.get("sessionId"))
    if session_id and facts.session_id == stem:
        facts.session_id = session_id
    if facts.cwd is None:
        facts.cwd = _str(record.get("cwd"))
    facts.git_branch = _str(record.get("gitBranch")) or facts.git_branch
    facts.version = _str(record.get("version")) or facts.version
    at = _timestamp(record.get("timestamp"))
    if at is not None:
        if facts.started_at is None or at < facts.started_at:
            facts.started_at = at
        if facts.last_activity_at is None or at > facts.last_activity_at:
            facts.last_activity_at = at

    kind = record.get("type")
    if kind == "custom-title":
        title = _str(record.get("customTitle"))
        if title:
            facts.title = _clip(title)
        return
    if kind == "system":
        if record.get("subtype") == "bridge_status":
            _apply_bridge_status(facts, record, at)
        return
    if kind not in ("user", "assistant"):
        return
    if record.get("isSidechain") is True or record.get("isMeta") is True:
        return
    facts.message_count += 1
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if kind == "user":
        _apply_user(facts, record, content)
    else:
        model = _str(message.get("model")) if isinstance(message, dict) else None
        _apply_assistant(facts, model, content)


def _bridge_url(record: dict[str, Any]) -> str | None:
    url = _str(record.get("url"))
    return url if url is not None and REMOTE_URL.fullmatch(url) else None


def _apply_bridge_status(facts: SessionFacts, record: dict[str, Any], at: datetime | None) -> None:
    # Without a time there is no telling which link is newer.
    url = _bridge_url(record)
    if url is None or at is None:
        return
    if facts.remote_url_at is None or at >= facts.remote_url_at:
        facts.remote_url = url
        facts.remote_url_at = at


def find_remote_url(path: Path, after: datetime) -> str | None:
    """The URL of the newest valid `bridge_status` record in `path` written strictly after
    `after` (which must be timezone-aware, in UTC), else None. Reads the whole file."""
    newest: tuple[datetime, str] | None = None
    data = path.read_bytes()
    for line in data.split(b"\n"):
        record = _decode(line)
        if record is None or record.get("type") != "system":
            continue
        if record.get("subtype") != "bridge_status":
            continue
        url = _bridge_url(record)
        at = _timestamp(record.get("timestamp"))
        if url is None or at is None or at <= after:
            continue
        if newest is None or at >= newest[0]:
            newest = (at, url)
    return newest[1] if newest is not None else None


def _apply_user(facts: SessionFacts, record: dict[str, Any], content: Any) -> None:
    if record.get("promptSource") == "system":
        return
    text = _prompt_text(content)
    if text is None or _COMMAND_OUTPUT.match(text):
        return
    command = _COMMAND_NAME.search(text)
    bash = _BASH_INPUT.search(text)
    if command is not None:
        name = command.group(1)
        facts.features["slash_command", name] += 1
        args = _COMMAND_ARGS.search(text)
        text = f"{name} {args.group(1).strip()}" if args and args.group(1).strip() else name
    elif bash is not None:
        text = f"! {bash.group(1).strip()}"
    if record.get("permissionMode") == "plan":
        facts.features["plan_mode", "plan"] += 1
    text = _clip(text)
    typed = command is None and bash is None
    if facts.first_prompt is None or (typed and _is_command_prompt(facts, facts.first_prompt)):
        facts.first_prompt = text
    facts.last_user = text


def _is_command_prompt(facts: SessionFacts, text: str) -> bool:
    """Whether `text` came from a slash command or bash-mode input rather than a typed prompt.

    Decided from what the facts already hold, so parsing in pieces gives the same answer.
    """
    if text.startswith("! "):
        return True
    words = text.split(maxsplit=1)
    return bool(words) and ("slash_command", words[0]) in facts.features


def _apply_assistant(facts: SessionFacts, model: str | None, content: Any) -> None:
    if model and not model.startswith("<"):
        facts.models.add(model)
        facts.features["model", model] += 1
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = _str(block.get("text"))
            if text and text.strip():
                facts.last_assistant = _clip(text)
        elif block.get("type") == "tool_use":
            name = _str(block.get("name"))
            if name:
                tool_input = block.get("input")
                _count_tool(facts, name, tool_input if isinstance(tool_input, dict) else {})


def _count_tool(facts: SessionFacts, name: str, tool_input: dict[str, Any]) -> None:
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3 and parts[1] and parts[2]:
            facts.features["mcp_server", parts[1]] += 1
            facts.features["mcp_tool", name] += 1
            return
    facts.features["tool", name] += 1
    if name == "Skill":
        skill = _str(tool_input.get("skill"))
        if skill:
            facts.features["skill", skill] += 1
    elif name in _SUBAGENT_TOOLS:
        subagent = _str(tool_input.get("subagent_type"))
        if subagent:
            facts.features["subagent", subagent] += 1


def _prompt_text(content: Any) -> str | None:
    """The text of a user prompt, or None for tool results and other non-prompt content."""
    if isinstance(content, str):
        return content if content.strip() else None
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            return None
        if block.get("type") == "text":
            text = _str(block.get("text"))
            if text:
                parts.append(text)
    joined = "\n".join(parts)
    return joined if joined.strip() else None


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        return None
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _clip(text: str) -> str:
    text = text.strip()
    return text if len(text) <= TRUNCATE else text[: TRUNCATE - 1] + "…"
