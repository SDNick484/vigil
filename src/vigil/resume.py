"""Resume an indexed session: decide how (`plan_resume`), then do it (`execute`).

The command line is built only from the indexed session's ID and working folder, plus the
allow-listed `resume.default_flags`. Nothing user-supplied reaches an argv, and nothing here runs a
shell.
"""

import logging
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import threading
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from vigil.claude_cli import Capabilities, LiveSession
from vigil.config import ALLOWED_FLAGS, ResumeConfig
from vigil.index.queries import SessionRow, record_launch
from vigil.terminal import terminal_argv

log = logging.getLogger(__name__)

# Session IDs are UUIDs; anything else (a leading "-" in particular) is refused rather than passed
# to `claude` or `tmux`, where it could read as an option or a tmux target pattern.
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")

_START_TIMEOUT = 20.0
"""How long `execute` waits for `claude --bg` / `tmux new-session -d` to return. Both return as
soon as the session is started; on a timeout the session is left running and the terminal is
opened anyway."""


TERMINAL_ALLOWED: ContextVar[bool] = ContextVar("vigil_terminal_allowed", default=True)
"""Whether `plan_resume` may plan a terminal. The web service's guard sets it to False for the
length of a request from another machine (`vigil.web.security`): a terminal opened on this
machine's screen is of no use to that user, so they get the attach command instead."""


class ResumeRefused(Exception):
    """Resume cannot (or will not) go ahead; `reason` is user-facing text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ResumePlan:
    start: list[str] | None
    """argv that starts the session, or None when it is already running in the background."""
    start_cwd: str
    attach: list[str]
    """argv that attaches a terminal to the session."""
    terminal: list[str] | None
    """argv that opens a terminal running `attach`; None when no terminal is available."""
    method: Literal["bg", "tmux"]
    session_id: str | None = None
    """The session being resumed, for recording the launch."""


def short_id(session_id: str) -> str:
    return session_id[:8]


def tmux_session_name(session_id: str) -> str:
    return f"vigil-{short_id(session_id)}"


def _which(name: str, env: Mapping[str, str]) -> str | None:
    return shutil.which(name, path=env.get("PATH", ""))


def _checked_flags(flags: tuple[str, ...]) -> list[str]:
    """The default flags, re-checked against the allow-list (config loading checks them too)."""
    seen_flag = False
    for token in flags:
        if token.startswith("-"):
            if token.split("=", 1)[0] not in ALLOWED_FLAGS:
                raise ResumeRefused(f"resume.default_flags: flag not allowed: {token}")
            seen_flag = True
        elif not seen_flag:
            raise ResumeRefused(f"resume.default_flags: value without a flag: {token}")
    return list(flags)


def _already_open(running: LiveSession, session: SessionRow) -> str:
    details = []
    where = running.cwd or session.cwd
    if where:
        details.append(f"folder {where}")
    if running.name:
        details.append(f"name {running.name}")
    suffix = f" ({', '.join(details)})" if details else ""
    return (
        f"already open in a terminal{suffix}; continue it there, or close it before resuming "
        "(vigil never starts a second process on the same session)"
    )


def _check_cwd(cwd: str | None) -> str:
    if not cwd:
        raise ResumeRefused("no working folder is recorded for this session")
    if not os.path.isabs(cwd) or not os.path.isdir(cwd):
        raise ResumeRefused(f"folder no longer exists: {cwd}")
    return cwd


def _method(caps: Capabilities, cfg: ResumeConfig, env: Mapping[str, str]) -> Literal["bg", "tmux"]:
    if _which("claude", env) is None:
        raise ResumeRefused("cannot resume: `claude` is not on PATH")
    bg_ok = caps.has_bg and caps.has_attach
    tmux_ok = _which("tmux", env) is not None
    if cfg.launcher == "bg":
        if not bg_ok:
            raise ResumeRefused(
                "cannot resume: resume.launcher is `bg`, but the installed claude does not "
                "support `claude --bg` and `claude attach`"
            )
        return "bg"
    if cfg.launcher == "tmux":
        if not tmux_ok:
            raise ResumeRefused(
                "cannot resume: resume.launcher is `tmux`, but `tmux` is not on PATH"
            )
        return "tmux"
    if bg_ok:
        return "bg"
    if tmux_ok:
        return "tmux"
    raise ResumeRefused(
        "cannot resume: the installed claude does not support `claude --bg` "
        "and `tmux` is not on PATH; install tmux or a newer Claude Code"
    )


def plan_resume(
    session: SessionRow,
    caps: Capabilities,
    live: dict[str, LiveSession],
    cfg: ResumeConfig,
    env: Mapping[str, str],
) -> ResumePlan:
    """Decide how to resume `session`. Starts nothing (it only looks at the filesystem and PATH).

    Raises ResumeRefused when the session is open in an ordinary terminal, its folder is gone,
    neither `claude --bg` nor tmux is available, `claude` is not on PATH, or the config is bad.
    A session already running in the background is attached to, never started again. The plan
    has no terminal while `TERMINAL_ALLOWED` is False.
    """
    session_id = session.session_id
    if not _SESSION_ID.fullmatch(session_id):
        raise ResumeRefused(f"not a valid session id: {session_id!r}")
    running = live.get(session_id)
    if running is not None and running.kind == "interactive":
        raise ResumeRefused(_already_open(running, session))
    cwd = _check_cwd(session.cwd)

    start: list[str] | None
    if running is not None:
        method: Literal["bg", "tmux"] = "bg"
        start = None
        attach = ["claude", "attach", session_id]
    else:
        flags = _checked_flags(cfg.default_flags)
        method = _method(caps, cfg, env)
        if method == "bg":
            start = ["claude", "--bg", "--resume", session_id, *flags]
            attach = ["claude", "attach", session_id]
        else:
            name = tmux_session_name(session_id)
            start = ["tmux", "new-session", "-d", "-s", name, "-c", cwd]
            start += ["claude", "--resume", session_id, *flags]
            attach = ["tmux", "attach", "-t", name]

    terminal: list[str] | None = None
    if TERMINAL_ALLOWED.get():
        try:
            terminal = terminal_argv(attach, cwd, cfg, env)
        except ValueError as error:
            raise ResumeRefused(f"resume.terminal_command is not usable: {error}") from error
    return ResumePlan(
        start=start,
        start_cwd=cwd,
        attach=attach,
        terminal=terminal,
        method=method,
        session_id=session_id,
    )


def _spawn(argv: list[str], cwd: str) -> subprocess.Popen[bytes]:
    """Start `argv` detached from vigil: own session, no stdin, output discarded."""
    return subprocess.Popen(  # noqa: S603 - argv is built by plan_resume, never a shell
        argv,
        cwd=cwd,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _flag_names(argv: list[str]) -> list[str]:
    """Flag names only (``--model=x`` → ``--model``); values and positionals are dropped."""
    return [token.split("=", 1)[0] for token in argv if token.startswith("-")]


def _claude_argv(plan: ResumePlan, start: list[str]) -> list[str]:
    if plan.method == "tmux":
        # tmux's own arguments come first; its cwd is absolute, so it is never "claude".
        return start[start.index("claude", 1) :]
    return start


def _record(
    conn: sqlite3.Connection, plan: ResumePlan, subcommand: str | None, argv: list[str]
) -> None:
    try:
        record_launch(
            conn,
            source="vigil",
            subcommand=subcommand,
            flags=_flag_names(argv[2:] if subcommand else argv[1:]),
            at=datetime.now(UTC),
            session_id=plan.session_id,
        )
    except sqlite3.Error:
        # The session is already running; failing to count it must not fail the resume.
        log.warning("could not record a launch", exc_info=True)


def execute(plan: ResumePlan, *, conn: sqlite3.Connection | None = None) -> str:
    """Carry out `plan`; return the attach command as a shell string, for display.

    Runs `start` detached in `start_cwd` and waits (up to 20 s) for it to return, then opens
    `terminal` detached. Raises ResumeRefused when `start` cannot be run or exits non-zero; then
    no terminal is opened. A terminal that fails to open is not an error: the caller shows the
    attach command. With `conn`, each `claude` invocation vigil makes is recorded with
    `record_launch` (flag names only).
    """
    if plan.start is not None:
        command = shlex.join(plan.start)
        try:
            process = _spawn(plan.start, plan.start_cwd)
        except OSError as error:
            raise ResumeRefused(f"could not run `{command}`: {error.strerror or error}") from error
        try:
            code = process.wait(timeout=_START_TIMEOUT)
        except subprocess.TimeoutExpired:
            log.warning("`%s` still running after %s s; carrying on", command, _START_TIMEOUT)
        else:
            if code != 0:
                raise ResumeRefused(f"starting the session failed: `{command}` exited with {code}")
        if conn is not None:
            _record(conn, plan, None, _claude_argv(plan, plan.start))

    if plan.terminal is not None:
        try:
            terminal = _spawn(plan.terminal, plan.start_cwd)
        except OSError:
            log.warning("could not open a terminal with %s", shlex.join(plan.terminal))
        else:
            # Reap the terminal when it closes, so a long-running service leaves no zombies.
            threading.Thread(target=terminal.wait, daemon=True).start()
            if conn is not None and plan.attach[0] == "claude":
                _record(conn, plan, "attach", plan.attach)
    return shlex.join(plan.attach)
