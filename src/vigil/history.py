"""Opt-in launch history, read from shell history files.

Transcripts do not record the flags `claude` was started with, so with `usage.shell_history` on,
vigil reads bash, zsh (plain and extended) and fish history. Privacy rules, enforced here:

- Off by default: with the option off nothing is opened or read.
- Only `claude` invocations are kept. Every other line is dropped as it is parsed, never stored.
- From a kept line only the subcommand and the flag names are stored (never values or prompts)
  plus a timestamp.

History files are read incrementally by byte offset, only whole lines, with invalid UTF-8 decoded
with replacement characters so one bad line cannot stop the rest from being read.
"""

import json
import os
import re
import shlex
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from vigil.config import UsageConfig
from vigil.index.db import to_db
from vigil.index.queries import record_launch

_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# A flag as typed: `-p`, `--model`, `--model=opus` (the `=value` part is cut off and dropped).
_FLAG = re.compile(r"^(--?[A-Za-z][A-Za-z0-9-]*)(=.*)?$", re.DOTALL)
# What a subcommand looks like: one short lowercase word.
_SUBCOMMAND = re.compile(r"^[a-z][a-z0-9-]{1,19}$")
# Shell control operators (as shlex yields them with punctuation_chars): the command ends there.
_OPERATOR_CHARS = frozenset("();<>|&")
_QUOTES = frozenset("\"'`")
# Options of the wrappers that take a value as the next token.
_SUDO_VALUE_OPTIONS = frozenset({"-u", "-g", "-h", "-p", "-C", "-r", "-t", "-D", "-U", "-T"})
_ENV_VALUE_OPTIONS = frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"})

_BASH_TIMESTAMP = re.compile(r"^#(\d{9,})$")
_ZSH_EXTENDED = re.compile(r"^: (\d+):\d+;(.*)$", re.DOTALL)
_FISH_COMMAND = "- cmd: "
_FISH_WHEN = re.compile(r"^\s+when:\s*(\d+)\s*$")


@dataclass(frozen=True)
class Invocation:
    """One `claude` launch: the subcommand (if any) and the flag names, as typed."""

    subcommand: str | None
    flags: list[str]


def _tokenize(command_line: str) -> list[str]:
    """Split a command line like a shell, stopping at the first control operator.

    On a parse error (an unterminated quote) it falls back to whitespace splitting and keeps
    only the tokens before the first one containing a quote: what follows may be prompt text.
    """
    lexer = shlex.shlex(command_line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    tokens: list[str]
    try:
        tokens = list(lexer)
    except ValueError:
        tokens = []
        for token in command_line.split():
            if _QUOTES & set(token):
                break
            tokens.append(token)
    for index, token in enumerate(tokens):
        if _OPERATOR_CHARS.issuperset(token):
            return tokens[:index]
    return tokens


def _skip_wrappers(tokens: list[str]) -> int | None:
    """Index of the command after leading `VAR=value`, `env`, `sudo` and `command`, or None when
    the line is not a launch (`command -v claude` only looks the program up)."""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if _ASSIGNMENT.match(token):
            index += 1
        elif token in ("env", "sudo"):
            value_options = _ENV_VALUE_OPTIONS if token == "env" else _SUDO_VALUE_OPTIONS
            index += 1
            while index < len(tokens):
                option = tokens[index]
                if option in value_options:
                    index += 2
                elif option.startswith("-") or (token == "env" and _ASSIGNMENT.match(option)):
                    index += 1
                else:
                    break
        elif token == "command":
            index += 1
            while index < len(tokens) and tokens[index].startswith("-"):
                if tokens[index] in ("-v", "-V"):
                    return None
                index += 1
        else:
            return index
    return None


def parse_claude_invocation(command_line: str, aliases: Sequence[str]) -> Invocation | None:
    """The launch of `claude` in one shell command line, or None for any other command.

    Accepts `claude`, any path ending in `/claude`, and the listed aliases, after any leading
    `VAR=value`, `env`, `sudo` or `command`. Only a subcommand and flag names come back, as typed
    (`--model sonnet` gives `--model`, `-n x` gives `-n`, `--model=opus` gives `--model`).

    A subcommand is the first word after `claude` when it is one short lowercase word and the
    next token, if any, is a flag. This cannot tell a one-word prompt from a subcommand without
    a hardcoded list; a prompt with more words is never taken for one.
    """
    tokens = _tokenize(command_line)
    start = _skip_wrappers(tokens)
    if start is None:
        return None
    program = tokens[start]
    if program != "claude" and not program.endswith("/claude") and program not in aliases:
        return None
    rest = tokens[start + 1 :]
    subcommand: str | None = None
    if rest and _SUBCOMMAND.match(rest[0]) and (len(rest) == 1 or _FLAG.match(rest[1])):
        subcommand = rest[0]
    flags: list[str] = []
    for token in rest:
        if token == "--":
            break
        match = _FLAG.match(token)
        if match:
            flags.append(match.group(1))
    return Invocation(subcommand=subcommand, flags=flags)


def default_history_files(env: Mapping[str, str]) -> list[Path]:
    """Where shell history usually is: `$HISTFILE`, `~/.bash_history`, `~/.zsh_history` and
    `${XDG_DATA_HOME}/fish/fish_history`. Nothing is opened or checked here."""
    home = Path(env["HOME"]) if env.get("HOME") else Path.home()
    data_home = env.get("XDG_DATA_HOME", "")
    # Per the XDG spec a relative path in the variable is invalid and ignored.
    data_dir = Path(data_home) if data_home and os.path.isabs(data_home) else home / ".local/share"
    candidates = [
        Path(env["HISTFILE"]) if env.get("HISTFILE") else None,
        home / ".bash_history",
        home / ".zsh_history",
        data_dir / "fish" / "fish_history",
    ]
    files: list[Path] = []
    for candidate in candidates:
        if candidate is not None and candidate not in files:
            files.append(candidate)
    return files


def _fish_unescape(text: str) -> str:
    return re.sub(r"\\(.)", lambda m: "\n" if m.group(1) == "n" else m.group(1), text)


def _continues(command: str) -> bool:
    """Whether a zsh history command ends in an unescaped backslash (the next line continues it)."""
    return (len(command) - len(command.rstrip("\\"))) % 2 == 1


@dataclass
class _Open:
    """A history entry that may continue on later lines: a fish entry (its `when:` follows) or a
    zsh command with a backslash continuation."""

    command: str
    timestamp: int | None
    start: int
    fish: bool


def _read_launches(
    path: Path, start: int, end: int, aliases: Sequence[str]
) -> tuple[list[tuple[Invocation, int | None]], int]:
    """Parse whole lines of the file between byte `start` and `end`.

    Returns the launches found, each with its epoch timestamp when the format has one, and the
    offset to resume from: just past the last complete entry. A half-written last line, a `#time`
    line waiting for its command, and an entry that may still continue are left for next time.
    """
    found: list[tuple[Invocation, int | None]] = []

    def keep(command: str, timestamp: int | None) -> None:
        invocation = parse_claude_invocation(command, aliases)
        if invocation is not None:
            found.append((invocation, timestamp))

    resume = start
    position = start
    bash_timestamp: int | None = None
    pending: _Open | None = None
    with path.open("rb") as handle:
        handle.seek(start)
        for raw in handle:
            if position + len(raw) > end or not raw.endswith(b"\n"):
                break
            line_start = position
            position += len(raw)
            line = raw.decode("utf-8", errors="replace").rstrip("\n").removesuffix("\r")

            if pending is not None and not pending.fish:
                pending.command = pending.command.removesuffix("\\") + "\n" + line
                if not _continues(pending.command):
                    keep(pending.command, pending.timestamp)
                    pending = None
            elif pending is not None and line[:1] in (" ", "\t"):
                when = _FISH_WHEN.match(line)
                if when:
                    pending.timestamp = int(when.group(1))
            else:
                if pending is not None:  # A fish entry ends at the next unindented line.
                    keep(pending.command, pending.timestamp)
                    pending = None
                zsh = _ZSH_EXTENDED.match(line)
                bash = _BASH_TIMESTAMP.match(line)
                if line.startswith(_FISH_COMMAND):
                    command = _fish_unescape(line[len(_FISH_COMMAND) :])
                    pending = _Open(command, None, line_start, fish=True)
                elif zsh:
                    timestamp, command = int(zsh.group(1)), zsh.group(2)
                    if _continues(command):
                        pending = _Open(command, timestamp, line_start, fish=False)
                    else:
                        keep(command, timestamp)
                elif bash:
                    bash_timestamp = int(bash.group(1))
                elif line.strip():
                    keep(line.strip(), bash_timestamp)
                    bash_timestamp = None
            if pending is None and bash_timestamp is None:
                resume = position
        if pending is not None and pending.fish and pending.timestamp is not None:
            keep(pending.command, pending.timestamp)
            resume = position
    return found, resume


def _epoch(timestamp: int) -> datetime | None:
    try:
        return datetime.fromtimestamp(timestamp, UTC)
    except OverflowError, OSError, ValueError:
        return None


def _already_recorded(conn: sqlite3.Connection, at: datetime, invocation: Invocation) -> bool:
    """Whether this timestamped shell launch is in the index (a rewritten history file is read
    again from the start; this keeps that from counting its launches twice)."""
    row = conn.execute(
        "SELECT 1 FROM launches WHERE source = 'shell' AND at = ? AND subcommand IS ? "
        "AND flags = ? LIMIT 1",
        (to_db(at), invocation.subcommand, json.dumps(invocation.flags)),
    ).fetchone()
    return row is not None


def _import_file(conn: sqlite3.Connection, path: Path, aliases: Sequence[str]) -> int:
    try:
        info = path.stat()
    except OSError:
        return 0
    key = str(path)
    state = conn.execute(
        "SELECT size, mtime_ns, offset FROM history_state WHERE path = ?", (key,)
    ).fetchone()
    if state is not None and (state["size"], state["mtime_ns"]) == (info.st_size, info.st_mtime_ns):
        return 0

    start = 0
    if state is not None and 0 < state["offset"] <= state["size"] <= info.st_size:
        # Only appended: resume where we stopped. A file that shrank was rewritten: read it all.
        start = state["offset"]
    try:
        # Read no further than the size seen here, so the stored offset never passes the stored
        # size, even when the file is appended to while it is read.
        launches, resume = _read_launches(path, start, info.st_size, aliases)
    except OSError:
        return 0

    fallback = _epoch(info.st_mtime_ns // 1_000_000_000) or datetime.now(UTC)
    recorded = 0
    for invocation, timestamp in launches:
        at = _epoch(timestamp) if timestamp is not None else None
        if at is not None and _already_recorded(conn, at, invocation):
            continue
        record_launch(
            conn,
            source="shell",
            subcommand=invocation.subcommand,
            flags=invocation.flags,
            # A format without timestamps (plain bash or zsh): the file's modification time is
            # the best bound there is.
            at=at or fallback,
            session_id=None,
        )
        recorded += 1
    with conn:
        conn.execute(
            "INSERT INTO history_state (path, size, mtime_ns, offset) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (path) DO UPDATE SET size = excluded.size, "
            "mtime_ns = excluded.mtime_ns, offset = excluded.offset",
            (key, info.st_size, info.st_mtime_ns, resume),
        )
    return recorded


def import_history(conn: sqlite3.Connection, cfg: UsageConfig, env: Mapping[str, str]) -> int:
    """Record the `claude` launches found in shell history since the last import.

    Does nothing, and opens nothing, unless `cfg.shell_history` is on. Reads `cfg.history_files`,
    or the default files for `env` when none are configured; a file that is missing or unreadable
    is skipped. Returns how many launches were recorded.
    """
    if not cfg.shell_history:
        return 0
    files = list(cfg.history_files) or default_history_files(env)
    return sum(_import_file(conn, path, cfg.claude_aliases) for path in files)
