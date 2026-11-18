"""Resume an indexed session: decide how (`plan_resume`), then do it (`execute`).

The command line is built only from the indexed session's ID and working folder, plus the
allow-listed `resume.default_flags` and, when acting on a review's suggestion, the flags it
suggested (allow-listed too). `plan_start_new` starts a new session, only in the folder of an
indexed one. Nothing else user-supplied reaches an argv, and nothing here runs a shell.

`claude attach` and `claude stop` take a session's job id (`short_id`), not its session id.
`claude --bg` may run a session under another id than the one asked for (`execute` finds out
which).
"""

import logging
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Literal

from vigil.claude_cli import Capabilities, LiveSession, started_session
from vigil.config import ResumeConfig
from vigil.flags import FlagRefused, check, merge
from vigil.index.queries import SessionRow, record_launch
from vigil.terminal import terminal_argv
from vigil.transcript import find_remote_url

log = logging.getLogger(__name__)

# Session IDs are UUIDs; anything else (a leading "-" in particular) is refused rather than passed
# to `claude` or `tmux`, where it could read as an option or a tmux target pattern.
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")

_START_TIMEOUT = 20.0
"""How long `execute` waits for `claude --bg` / `tmux new-session -d` to return. Both return as
soon as the session is started; on a timeout the session is left running and the terminal is
opened anyway."""


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def session_lock(session_id: str) -> threading.Lock:
    """The lock for `session_id`: hold it from looking at whether the session is live until what
    was decided is done, so two requests cannot both launch (or stop and relaunch) one session."""
    with _locks_guard:
        return _locks.setdefault(session_id, threading.Lock())


_STOP_TIMEOUT = 20.0
"""How long `stop_session` waits for `claude stop` to return."""


TERMINAL_ALLOWED: ContextVar[bool] = ContextVar("vigil_terminal_allowed", default=True)
"""Whether `plan_resume` may plan a terminal. The web service's guard sets it to False for the
length of a request from another machine (`vigil.web.security`): a terminal opened on this
machine's screen is of no use to that user, so they get the attach command instead."""


class ResumeRefused(Exception):
    """Resume cannot (or will not) go ahead; `reason` is user-facing text."""

    def __init__(self, reason: str, *, restartable: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.restartable = restartable
        """Stopping the session and starting it again with Remote Control would get past this."""


@dataclass(frozen=True)
class ResumePlan:
    start: list[str] | None
    """argv that starts the session, or None when it is already running in the background."""
    start_cwd: str
    attach: list[str]
    """argv that attaches a terminal to the session. For a new background session, whose id
    is only known once it has started, `claude agents` (to find it)."""
    terminal: list[str] | None
    """argv that opens a terminal running `attach`; None when no terminal is available."""
    method: Literal["bg", "tmux"]
    session_id: str | None = None
    """The session being resumed or started; None for a new background session (`claude --bg`
    picks its id)."""
    terminal_for: Callable[[list[str]], list[str] | None] | None = field(
        default=None, compare=False, repr=False
    )
    """Builds `terminal` for another attach command, for a `claude --bg` start that runs under
    an id other than the planned one."""


@dataclass(frozen=True)
class Launched:
    """What `execute` did."""

    attach: str
    """The attach command, as a shell string, for display."""
    terminal: list[str] | None
    """argv of the terminal opened on the session (or tried); None when none was."""
    session_id: str | None
    """The session now running: the one `claude --bg` said it started, else the planned one;
    None when the id of a new background session could not be found."""
    copy_of: str | None = None
    """The session asked for, when `claude --bg --resume` started a copy of it under
    `session_id` instead (a background session keeps its own options)."""


def short_id(session_id: str) -> str:
    """The job id of a session: what `claude attach` and `claude stop` take (they refuse a full
    session id)."""
    return session_id[:8]


def copy_note(session_id: str) -> str:
    """What to tell the user when `claude --bg --resume` started a copy, `session_id`, of the
    session asked for."""
    job = short_id(session_id)
    return f"Started as a copy ({job}): this background session keeps its own options."


def attach_argv(session_id: str) -> list[str]:
    """argv that attaches a terminal to the background session `session_id`."""
    return ["claude", "attach", short_id(session_id)]


def tmux_session_name(session_id: str) -> str:
    return f"vigil-{short_id(session_id)}"


def _which(name: str, env: Mapping[str, str]) -> str | None:
    return shutil.which(name, path=env.get("PATH", ""))


def _checked_flags(flags: tuple[str, ...], extra: Sequence[str] = ()) -> list[str]:
    """The default flags with `extra` merged in after them (`vigil.flags.merge`: a default flag
    `extra` also has takes its value from `extra`), each re-checked against `vigil.flags` (config
    loading checks the defaults too, the caller `extra` with the installed claude's catalog) and
    normalized to one token per flag and per value."""
    try:
        defaults = check(flags)
    except FlagRefused as error:
        raise ResumeRefused(f"resume.default_flags: {error.reason}") from error
    try:
        return merge(defaults, check(extra))
    except FlagRefused as error:
        raise ResumeRefused(error.reason) from error


def joinable_link(session: SessionRow, live: dict[str, LiveSession]) -> str | None:
    """The Remote Control link `session` can be joined by right now, or None when it is not live
    or has none for its current run. The index is looked at first; when it holds none, the
    transcript is read directly, since the watcher may not have caught up with a link that
    was only just written (strictly after the live run started; any link when its start time
    is unknown)."""
    # Imported here: vigil.serialize imports this module.
    from vigil.serialize import open_link

    link = open_link(session, live)
    running = live.get(session.session_id)
    if link is not None or running is None:
        return link
    after = running.started_at or datetime(1970, 1, 1, tzinfo=UTC)
    try:
        return find_remote_url(Path(session.transcript_path), after)
    except OSError:
        return None


def _already_open(running: LiveSession, session: SessionRow, joinable: bool) -> str:
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
        + ("" if joinable else "; or type /remote-control in that terminal to make it joinable")
    )


def _with_remote_control(flags: list[str]) -> list[str]:
    """`flags` with `--remote-control` in front, unless they already have it: every session vigil
    starts can be joined from a phone."""
    if any(token.split("=", 1)[0] == "--remote-control" for token in flags):
        return flags
    return ["--remote-control", *flags]


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
    extra_flags: Sequence[str] = (),
) -> ResumePlan:
    """Decide how to resume `session`. Starts nothing (it only looks at the filesystem and PATH).

    Raises ResumeRefused when the session is open in an ordinary terminal, its folder is gone,
    neither `claude --bg` nor tmux is available, `claude` is not on PATH, or the config is bad.
    A session already running in the background is attached to, never started again. While
    `TERMINAL_ALLOWED` is False a live session with a Remote Control link (`joinable_link`) gets a
    plan that starts nothing (the caller answers with the link), even when it is open in a
    terminal; without a link there is nothing for the requester to do with it, so a background
    session is refused with `restartable` set. A session vigil starts always has
    `--remote-control`. The plan has no terminal while `TERMINAL_ALLOWED` is False.

    `extra_flags` (a review's suggestion) go after the default flags when the session is started
    (a default flag they also have takes their value), and are refused unless `vigil.flags.check`
    allows them; a live session is joined or attached to as it is, without them.
    """
    session_id = session.session_id
    if not SESSION_ID.fullmatch(session_id):
        raise ResumeRefused(f"not a valid session id: {session_id!r}")
    running = live.get(session_id)
    remote = not TERMINAL_ALLOWED.get()
    link = None
    if running is not None and (remote or running.kind == "interactive"):
        link = joinable_link(session, live)
    if running is not None and running.kind == "interactive" and not (remote and link):
        raise ResumeRefused(_already_open(running, session, joinable=link is not None))
    cwd = _check_cwd(session.cwd)

    start: list[str] | None
    if running is not None:
        if remote and link is None:
            raise ResumeRefused(
                "running in the background without Remote Control; attach at a desk with "
                f"`{shlex.join(attach_argv(session_id))}` and type /remote-control, or restart "
                "it with Remote Control (this interrupts it)",
                restartable=True,
            )
        method: Literal["bg", "tmux"] = "bg"
        start = None
        attach = attach_argv(session_id)
    else:
        flags = _with_remote_control(_checked_flags(cfg.default_flags, extra_flags))
        method = _method(caps, cfg, env)
        if method == "bg":
            start = ["claude", "--bg", "--resume", session_id, *flags]
            attach = attach_argv(session_id)
        else:
            name = tmux_session_name(session_id)
            start = ["tmux", "new-session", "-d", "-s", name, "-c", cwd]
            start += ["claude", "--resume", session_id, *flags]
            attach = ["tmux", "attach", "-t", name]

    return ResumePlan(
        start=start,
        start_cwd=cwd,
        attach=attach,
        terminal=_terminal(attach, cwd, cfg, env),
        method=method,
        session_id=session_id,
        terminal_for=_terminal_for(cwd, cfg, env),
    )


def _terminal(
    attach: list[str], cwd: str, cfg: ResumeConfig, env: Mapping[str, str]
) -> list[str] | None:
    if not TERMINAL_ALLOWED.get():
        return None
    try:
        return terminal_argv(attach, cwd, cfg, env)
    except ValueError as error:
        raise ResumeRefused(f"resume.terminal_command is not usable: {error}") from error


def _terminal_for(
    cwd: str, cfg: ResumeConfig, env: Mapping[str, str]
) -> Callable[[list[str]], list[str] | None]:
    env = dict(env)
    return lambda attach: _terminal(attach, cwd, cfg, env)


def plan_start_new(
    cwd: str | None,
    flags: Sequence[str],
    caps: Capabilities,
    cfg: ResumeConfig,
    env: Mapping[str, str],
    new_session_id: str,
) -> ResumePlan:
    """Decide how to start a new session in `cwd` with the default flags and `flags` (merged as
    `plan_resume` merges its `extra_flags`). Starts nothing.

    `cwd` must be the folder of an indexed session (the caller's to ensure) and still exist.
    Launched like a resume: `claude --bg`, else tmux, always with `--remote-control`, with a
    terminal only while `TERMINAL_ALLOWED`. `claude --bg` picks the new session's id itself (it
    ignores `--session-id`); `execute` finds it once it has started, so until then the plan has
    no session id and its attach command lists the sessions. In tmux, `claude` runs under
    `new_session_id`, one vigil chose (`--session-id`). Raises ResumeRefused as `plan_resume`
    does.
    """
    if not SESSION_ID.fullmatch(new_session_id):
        raise ResumeRefused(f"not a valid session id: {new_session_id!r}")
    folder = _check_cwd(cwd)
    checked = _with_remote_control(_checked_flags(cfg.default_flags, flags))
    method = _method(caps, cfg, env)
    session_id: str | None
    if method == "bg":
        start = ["claude", "--bg", *checked]
        attach = ["claude", "agents"]
        session_id = None
    else:
        name = tmux_session_name(new_session_id)
        start = ["tmux", "new-session", "-d", "-s", name, "-c", folder]
        start += ["claude", "--session-id", new_session_id, *checked]
        attach = ["tmux", "attach", "-t", name]
        session_id = new_session_id
    return ResumePlan(
        start=start,
        start_cwd=folder,
        attach=attach,
        terminal=_terminal(attach, folder, cfg, env),
        method=method,
        session_id=session_id,
        terminal_for=_terminal_for(folder, cfg, env),
    )


def _spawn(
    argv: list[str], cwd: str, stdout: int | IO[bytes] = subprocess.DEVNULL
) -> subprocess.Popen[bytes]:
    """Start `argv` detached from vigil: own session, no stdin, output discarded unless `stdout`
    says where it goes."""
    return subprocess.Popen(  # noqa: S603 - argv is built by plan_resume, never a shell
        argv,
        cwd=cwd,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=subprocess.DEVNULL,
    )


_OUTPUT_LIMIT = 64 * 1024
"""How much of what a start printed `execute` reads."""


def _start(plan: ResumePlan, start: list[str]) -> str:
    """Run `start` detached in the plan's folder and wait (up to 20 s) for it to return; return
    what it printed so far. Its output goes to an unnamed temporary file, not a pipe, so a
    background service it leaves running (holding the file open) cannot hold vigil up. Raises
    ResumeRefused when it cannot be run or exits non-zero."""
    command = shlex.join(start)
    with tempfile.TemporaryFile() as output:
        try:
            process = _spawn(start, plan.start_cwd, output)
        except OSError as error:
            raise ResumeRefused(f"could not run `{command}`: {error.strerror or error}") from error
        try:
            code = process.wait(timeout=_START_TIMEOUT)
        except subprocess.TimeoutExpired:
            log.warning("`%s` still running after %s s; carrying on", command, _START_TIMEOUT)
        else:
            if code != 0:
                raise ResumeRefused(f"starting the session failed: `{command}` exited with {code}")
        output.seek(0)
        return output.read(_OUTPUT_LIMIT).decode("utf-8", errors="replace")


def _flag_names(argv: list[str]) -> list[str]:
    """Flag names only (``--model=x`` → ``--model``); values and positionals are dropped."""
    return [token.split("=", 1)[0] for token in argv if token.startswith("-")]


def _claude_argv(plan: ResumePlan, start: list[str]) -> list[str]:
    if plan.method == "tmux":
        # tmux's own arguments come first; its cwd is absolute, so it is never "claude".
        return start[start.index("claude", 1) :]
    return start


def _record(
    conn: sqlite3.Connection, session_id: str | None, subcommand: str | None, argv: list[str]
) -> None:
    try:
        record_launch(
            conn,
            source="vigil",
            subcommand=subcommand,
            flags=_flag_names(argv[2:] if subcommand else argv[1:]),
            at=datetime.now(UTC),
            session_id=session_id,
        )
    except sqlite3.Error:
        # The session is already running; failing to count it must not fail the resume.
        log.warning("could not record a launch", exc_info=True)


def execute(plan: ResumePlan, *, conn: sqlite3.Connection | None = None) -> Launched:
    """Carry out `plan` and say what it did.

    Runs `start` detached in `start_cwd` and waits (up to 20 s) for it to return, then opens a
    terminal detached. Raises ResumeRefused when `start` cannot be run or exits non-zero; then
    no terminal is opened. A terminal that fails to open is not an error: the caller shows the
    attach command.

    After a `claude --bg` start, the session that runs is the one it says it started
    (`vigil.claude_cli.started_session`): a copy of the resumed session, or a new session's
    own id. The attach command and terminal are for that session. When it cannot be told, a
    resume carries on with the planned session; a new session gets no terminal, and the attach
    command lists the sessions.

    With `conn`, each `claude` invocation vigil makes is recorded with `record_launch` (flag
    names only), under the session that runs.
    """
    session_id = plan.session_id
    if plan.start is not None:
        output = _start(plan, plan.start)
        if plan.method == "bg":
            session_id = started_session(output, plan.start_cwd) or plan.session_id
        if conn is not None:
            _record(conn, session_id, None, _claude_argv(plan, plan.start))

    attach, opens = plan.attach, plan.terminal
    if session_id is None:
        opens = None  # a new background session that cannot be found
    elif session_id != plan.session_id:
        attach = attach_argv(session_id)
        if opens is not None and plan.terminal_for is not None:
            opens = plan.terminal_for(attach)

    if opens is not None:
        try:
            terminal = _spawn(opens, plan.start_cwd)
        except OSError:
            log.warning("could not open a terminal with %s", shlex.join(opens))
        else:
            # Reap the terminal when it closes, so a long-running service leaves no zombies.
            threading.Thread(target=terminal.wait, daemon=True).start()
            if conn is not None and attach[0] == "claude":
                _record(conn, session_id, "attach", attach)
    copy_of = plan.session_id if session_id != plan.session_id else None
    return Launched(shlex.join(attach), opens, session_id, copy_of)


def stop_session(
    session_id: str,
    live_probe: Callable[[], dict[str, LiveSession] | None],
    *,
    timeout: float = 10.0,
    poll: float = 0.5,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Stop the background session `session_id` with `claude stop`, then wait (up to `timeout`
    seconds, looking every `poll`) until `live_probe` no longer lists it. A probe that returns None
    (the live sessions could not be listed) counts as the session still being there.

    This is the one place vigil stops anything, and only for Restart with Remote Control, after the
    author confirmed. Raises ResumeRefused when `claude stop` cannot be run, is too slow or exits
    non-zero, or the session is still live after `timeout`; the caller then starts nothing.
    """
    if not SESSION_ID.fullmatch(session_id):
        raise ResumeRefused(f"not a valid session id: {session_id!r}")
    refused = ResumeRefused("could not stop the session; nothing was restarted")
    argv = ["claude", "stop", short_id(session_id)]
    try:
        result = subprocess.run(  # noqa: S603 - argv is built here from a checked id, never a shell
            argv,
            capture_output=True,
            timeout=_STOP_TIMEOUT,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        log.warning("could not run `%s`", shlex.join(argv), exc_info=True)
        raise refused from None
    if result.returncode != 0:
        log.warning("`%s` exited with %s", shlex.join(argv), result.returncode)
        raise refused
    deadline = clock() + timeout
    while (live := live_probe()) is None or session_id in live:
        remaining = deadline - clock()
        if remaining <= 0:
            raise refused
        sleep(min(poll, remaining))
