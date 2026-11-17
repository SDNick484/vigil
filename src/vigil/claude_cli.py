"""Detect what the installed ``claude`` offers and which sessions are live.

Everything here degrades to "unknown": a missing binary, an old version, a timeout or garbage
output never raises. Callers treat an empty result as "status unknown".
"""

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

_CAPS_TIMEOUT = 10.0
_LIVE_TIMEOUT = 5.0

# A flag line in `--help`: leading indent, an optional short alias ("-c, "), then the long flag.
_BG_FLAG = re.compile(r"^\s+(?:-\w+,\s+)?--bg\b", re.MULTILINE)
# A subcommand line in `--help`: two-space indent, then the name ("stop|kill <id>" lists aliases).
_COMMAND = r"^  {name}(?:\|\w+)*(?:\s|$)"
_VERSION = re.compile(r"^\s*(\d+\.\d+\.\d+[\w.+-]*)")


@dataclass(frozen=True)
class Capabilities:
    version: str | None
    has_bg: bool
    has_attach: bool
    has_agents_json: bool


@dataclass(frozen=True)
class LiveSession:
    session_id: str
    kind: Literal["background", "interactive"]
    name: str | None
    cwd: str | None
    busy: bool = False
    """The session is working on something now (`status` is `busy`; the other value seen is
    `idle`)."""
    started_at: datetime | None = None
    """When this run of the session started (`startedAt`, epoch milliseconds)."""


_NONE = Capabilities(version=None, has_bg=False, has_attach=False, has_agents_json=False)
_cache: dict[tuple[str, str], Capabilities] = {}


def _run(argv: list[str], timeout: float) -> str | None:
    """Stdout of a successful run, or None when it is missing, slow, or exits non-zero."""
    try:
        result = subprocess.run(  # noqa: S603 - argv is a fixed, vigil-built list
            argv,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None
    return result.stdout if result.returncode == 0 else None


def _has_command(help_text: str, name: str) -> bool:
    return re.search(_COMMAND.format(name=re.escape(name)), help_text, re.MULTILINE) is not None


def parse_help(help_text: str, version: str | None) -> Capabilities:
    """Capabilities advertised by a `claude --help` text.

    ``agents --json`` is inferred from the ``agents`` subcommand being listed (the top-level help
    does not mention ``--json``; it is documented in ``claude agents --help``).
    """
    return Capabilities(
        version=version,
        has_bg=_BG_FLAG.search(help_text) is not None,
        has_attach=_has_command(help_text, "attach"),
        has_agents_json=_has_command(help_text, "agents"),
    )


def clear_cache() -> None:
    """Forget cached capabilities (for tests, or after the binary was upgraded in place)."""
    _cache.clear()


def capabilities(claude: str = "claude") -> Capabilities:
    """Probe ``claude --version`` and ``claude --help``. Cached per (binary, version)."""
    version_out = _run([claude, "--version"], _CAPS_TIMEOUT)
    if version_out is None:
        return _NONE
    match = _VERSION.match(version_out)
    version = match.group(1) if match else None
    key = (claude, version or "")
    if version is not None and key in _cache:
        return _cache[key]
    help_out = _run([claude, "--help"], _CAPS_TIMEOUT)
    if help_out is None:
        return Capabilities(version=version, has_bg=False, has_attach=False, has_agents_json=False)
    caps = parse_help(help_out, version)
    if version is not None:
        _cache[key] = caps
    return caps


def _started_at(value: Any) -> datetime | None:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except OverflowError, OSError, ValueError:
        return None


def _parse_entry(entry: Any) -> LiveSession | None:
    if not isinstance(entry, dict):
        return None
    session_id = entry.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        return None
    name = entry.get("name")
    cwd = entry.get("cwd")
    # Only "interactive" was observed; anything else is treated as a background session.
    kind: Literal["background", "interactive"] = (
        "interactive" if entry.get("kind") == "interactive" else "background"
    )
    return LiveSession(
        session_id=session_id,
        kind=kind,
        name=name if isinstance(name, str) and name else None,
        cwd=cwd if isinstance(cwd, str) and cwd else None,
        busy=entry.get("status") == "busy",
        started_at=_started_at(entry.get("startedAt")),
    )


def parse_live_sessions(text: str) -> dict[str, LiveSession]:
    """Parse `claude agents --json` output. Anything unexpected yields an empty dict or skips
    the bad entry."""
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(data, list):
        return {}
    sessions = (_parse_entry(entry) for entry in data)
    return {s.session_id: s for s in sessions if s is not None}


def live_sessions(caps: Capabilities, claude: str = "claude") -> dict[str, LiveSession]:
    """Sessions running right now, keyed by session ID. Empty when `claude agents --json` is
    unavailable, fails, times out (5 s) or prints something unparseable."""
    if not caps.has_agents_json:
        return {}
    out = _run([claude, "agents", "--json"], _LIVE_TIMEOUT)
    return parse_live_sessions(out) if out is not None else {}
