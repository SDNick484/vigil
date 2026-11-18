"""Tests for the resume launcher. Only fakes on PATH are ever run (see FakeBin): the fake
`claude`, `tmux` and terminals record their argv and cwd and exit; none of them runs anything."""

import json
import shlex
import sqlite3
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import pytest

from tests.conftest import Call, FakeBin
from vigil.claude_cli import Capabilities, LiveSession
from vigil.config import ResumeConfig
from vigil.index.db import connect
from vigil.index.queries import SessionRow
from vigil.remote import find_transcript, wait_for_remote_url
from vigil.resume import (
    TERMINAL_ALLOWED,
    Launched,
    ResumePlan,
    ResumeRefused,
    execute,
    joinable_link,
    plan_resume,
    plan_start_new,
    stop_session,
)

pytestmark = pytest.mark.usefixtures("proc_version")

SID = "0123abcd-0000-4000-8000-000000000001"
JOB = "0123abcd"  # its job id: what `claude attach` and `claude stop` take
ALL_CAPS = Capabilities(version="2.1.312", has_bg=True, has_attach=True, has_agents_json=True)
OLD_CAPS = Capabilities(version="1.0.0", has_bg=False, has_attach=False, has_agents_json=False)
RC = ResumeConfig(default_flags=("--remote-control",))


def make_session(cwd: str | None, session_id: str = SID) -> SessionRow:
    return SessionRow(
        session_id=session_id,
        cwd=cwd,
        git_branch="main",
        version="2.1.312",
        title="demo",
        first_prompt="hello",
        last_user="hello",
        last_assistant="hi",
        started_at=None,
        last_activity_at=None,
        message_count=2,
        previous_session_id=None,
        machine="host",
        project_key="demo",
        project_label="demo",
        chain_id=session_id,
        transcript_path="/nonexistent/transcript.jsonl",
    )


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    return path


@pytest.fixture
def env(fake_bin: FakeBin) -> dict[str, str]:
    """A headless environment whose PATH is only the fake directory, with a fake `claude`."""
    fake_bin.install("claude")
    return {"PATH": str(fake_bin.directory)}


def test_bg_plan(workdir: Path, env: dict[str, str]) -> None:
    plan = plan_resume(make_session(str(workdir)), ALL_CAPS, {}, RC, env)
    assert plan.method == "bg"
    assert plan.start == ["claude", "--bg", "--resume", SID, "--remote-control"]
    assert plan.start_cwd == str(workdir)
    assert plan.attach == ["claude", "attach", JOB]
    assert plan.terminal is None  # headless
    assert plan.session_id == SID


def test_already_background_skips_start(workdir: Path, env: dict[str, str]) -> None:
    live = {SID: LiveSession(session_id=SID, kind="background", name=None, cwd=str(workdir))}
    # Even with tmux forced, a background session is attached to, never started a second time.
    cfg = ResumeConfig(launcher="tmux")
    plan = plan_resume(make_session(str(workdir)), ALL_CAPS, live, cfg, env)
    assert plan.start is None
    assert plan.method == "bg"
    assert plan.attach == ["claude", "attach", JOB]


def test_open_interactive_refused(workdir: Path, env: dict[str, str]) -> None:
    live = {SID: LiveSession(session_id=SID, kind="interactive", name="demo", cwd=str(workdir))}
    with pytest.raises(ResumeRefused) as refused:
        plan_resume(make_session(str(workdir)), ALL_CAPS, live, RC, env)
    assert "already open" in refused.value.reason
    assert str(workdir) in refused.value.reason


def test_missing_cwd_refused(tmp_path: Path, fake_bin: FakeBin, env: dict[str, str]) -> None:
    gone = tmp_path / "deleted-project"
    with pytest.raises(ResumeRefused) as refused:
        plan_resume(make_session(str(gone)), ALL_CAPS, {}, RC, env)
    assert refused.value.reason == f"folder no longer exists: {gone}"
    assert fake_bin.calls("claude") == []


def test_cwd_unknown_refused(env: dict[str, str]) -> None:
    with pytest.raises(ResumeRefused, match="no working folder"):
        plan_resume(make_session(None), ALL_CAPS, {}, RC, env)


def test_cwd_is_a_file_refused(tmp_path: Path, env: dict[str, str]) -> None:
    afile = tmp_path / "file"
    afile.write_text("")
    with pytest.raises(ResumeRefused) as refused:
        plan_resume(make_session(str(afile)), ALL_CAPS, {}, RC, env)
    assert refused.value.reason == f"folder no longer exists: {afile}"


def test_tmux_fallback_when_no_bg(workdir: Path, fake_bin: FakeBin, env: dict[str, str]) -> None:
    fake_bin.install("tmux")
    plan = plan_resume(make_session(str(workdir)), OLD_CAPS, {}, RC, env)
    assert plan.method == "tmux"
    assert plan.start == [
        "tmux",
        "new-session",
        "-d",
        "-s",
        "vigil-0123abcd",
        "-c",
        str(workdir),
        "claude",
        "--resume",
        SID,
        "--remote-control",
    ]
    assert plan.attach == ["tmux", "attach", "-t", "vigil-0123abcd"]


def test_tmux_fallback_when_bg_without_attach(
    workdir: Path, fake_bin: FakeBin, env: dict[str, str]
) -> None:
    fake_bin.install("tmux")
    caps = Capabilities(version="2.0.0", has_bg=True, has_attach=False, has_agents_json=False)
    assert plan_resume(make_session(str(workdir)), caps, {}, RC, env).method == "tmux"


def test_no_bg_no_tmux_refused(workdir: Path, env: dict[str, str]) -> None:
    with pytest.raises(ResumeRefused) as refused:
        plan_resume(make_session(str(workdir)), OLD_CAPS, {}, RC, env)
    assert "claude --bg" in refused.value.reason
    assert "tmux" in refused.value.reason


def test_claude_missing_refused(workdir: Path, fake_bin: FakeBin) -> None:
    fake_bin.install("tmux")
    env = {"PATH": str(fake_bin.directory)}
    with pytest.raises(ResumeRefused) as refused:
        plan_resume(make_session(str(workdir)), OLD_CAPS, {}, RC, env)
    assert "`claude` is not on PATH" in refused.value.reason


def test_launcher_forced_tmux(workdir: Path, fake_bin: FakeBin, env: dict[str, str]) -> None:
    fake_bin.install("tmux")
    cfg = ResumeConfig(launcher="tmux")
    plan = plan_resume(make_session(str(workdir)), ALL_CAPS, {}, cfg, env)
    assert plan.method == "tmux"
    assert plan.start is not None and plan.start[0] == "tmux"


def test_launcher_forced_tmux_without_tmux_refused(workdir: Path, env: dict[str, str]) -> None:
    with pytest.raises(ResumeRefused, match="`tmux` is not on PATH"):
        plan_resume(make_session(str(workdir)), ALL_CAPS, {}, ResumeConfig(launcher="tmux"), env)


def test_launcher_forced_bg_without_bg_refused(
    workdir: Path, fake_bin: FakeBin, env: dict[str, str]
) -> None:
    fake_bin.install("tmux")
    with pytest.raises(ResumeRefused, match="claude --bg"):
        plan_resume(make_session(str(workdir)), OLD_CAPS, {}, ResumeConfig(launcher="bg"), env)


def test_default_flags_appended_in_order(workdir: Path, env: dict[str, str]) -> None:
    flags = ("--model", "opus", "--remote-control", "--permission-mode=plan", "--name", "x y")
    plan = plan_resume(make_session(str(workdir)), ALL_CAPS, {}, ResumeConfig(flags), env)
    # `--flag=value` is normalized to separate tokens; the rest keep their order.
    expected = ["--model", "opus", "--remote-control", "--permission-mode", "plan", "--name", "x y"]
    assert plan.start == ["claude", "--bg", "--resume", SID, *expected]


def test_disallowed_flag_refused(workdir: Path, env: dict[str, str]) -> None:
    cfg = ResumeConfig(default_flags=("--dangerously-skip-permissions",))
    with pytest.raises(ResumeRefused, match="flag not allowed: --dangerously-skip-permissions"):
        plan_resume(make_session(str(workdir)), ALL_CAPS, {}, cfg, env)


def test_flag_value_refused(workdir: Path, env: dict[str, str]) -> None:
    cfg = ResumeConfig(default_flags=("--permission-mode", "bypassPermissions"))
    with pytest.raises(ResumeRefused, match="bypassPermissions is not allowed"):
        plan_resume(make_session(str(workdir)), ALL_CAPS, {}, cfg, env)


@pytest.mark.parametrize("bad_id", ["--help", "a b", "x;y", "vigil:0", ""])
def test_invalid_session_id_refused(workdir: Path, env: dict[str, str], bad_id: str) -> None:
    with pytest.raises(ResumeRefused, match="not a valid session id"):
        plan_resume(make_session(str(workdir), bad_id), ALL_CAPS, {}, RC, env)


def test_bad_terminal_template_refused(workdir: Path, env: dict[str, str]) -> None:
    cfg = ResumeConfig(terminal_command="term 'unclosed {attach}")
    with pytest.raises(ResumeRefused, match="terminal_command"):
        plan_resume(make_session(str(workdir)), ALL_CAPS, {}, cfg, env)


def test_plan_includes_terminal(workdir: Path, fake_bin: FakeBin, env: dict[str, str]) -> None:
    fake_bin.install("xterm")
    plan = plan_resume(make_session(str(workdir)), ALL_CAPS, {}, RC, {**env, "DISPLAY": ":0"})
    assert plan.terminal == ["xterm", "-e", "claude", "attach", JOB]


def bg_plan(workdir: Path, terminal: list[str] | None) -> ResumePlan:
    return ResumePlan(
        start=["claude", "--bg", "--resume", SID, "--model", "opus", "--remote-control"],
        start_cwd=str(workdir),
        attach=["claude", "attach", JOB],
        terminal=terminal,
        method="bg",
        session_id=SID,
    )


def test_execute_runs_fakes_with_cwd(workdir: Path, fake_bin: FakeBin, env: dict[str, str]) -> None:
    fake_bin.install("xterm")
    plan = plan_resume(make_session(str(workdir)), ALL_CAPS, {}, RC, {**env, "DISPLAY": ":0"})
    assert execute(plan).attach == f"claude attach {JOB}"
    [claude] = fake_bin.calls("claude")
    assert claude.argv == ["--bg", "--resume", SID, "--remote-control"]
    assert claude.cwd == str(workdir)
    [xterm] = wait_for_calls(fake_bin, "xterm")
    assert xterm.argv == ["-e", "claude", "attach", JOB]
    assert xterm.cwd == str(workdir)


def test_execute_tmux(workdir: Path, fake_bin: FakeBin, env: dict[str, str]) -> None:
    fake_bin.install("tmux")
    plan = plan_resume(make_session(str(workdir)), OLD_CAPS, {}, RC, env)
    assert execute(plan).attach == "tmux attach -t vigil-0123abcd"
    assert plan.start is not None
    [tmux] = fake_bin.calls("tmux")
    assert tmux.argv == plan.start[1:]
    assert fake_bin.calls("claude") == []  # only tmux runs claude, and the fake tmux does not


def test_execute_already_background_only_opens_terminal(
    workdir: Path, fake_bin: FakeBin, env: dict[str, str]
) -> None:
    fake_bin.install("xterm")
    live = {SID: LiveSession(session_id=SID, kind="background", name=None, cwd=None)}
    plan = plan_resume(make_session(str(workdir)), ALL_CAPS, live, RC, {**env, "DISPLAY": ":0"})
    execute(plan)
    assert fake_bin.calls("claude") == []
    assert len(wait_for_calls(fake_bin, "xterm")) == 1


def test_execute_start_failure_refused(workdir: Path, fake_bin: FakeBin) -> None:
    fake_bin.install("claude", exit_code=1)
    fake_bin.install("xterm")
    with pytest.raises(ResumeRefused, match="exited with 1"):
        execute(bg_plan(workdir, ["xterm", "-e", "claude", "attach", JOB]))
    assert fake_bin.calls("xterm") == []


def test_execute_start_missing_refused(workdir: Path, fake_bin: FakeBin) -> None:
    with pytest.raises(ResumeRefused, match="could not run"):
        execute(bg_plan(workdir, None))


def test_execute_terminal_missing_still_returns_attach(
    workdir: Path, fake_bin: FakeBin, env: dict[str, str]
) -> None:
    plan = bg_plan(workdir, ["not-installed-terminal", "-e", "claude", "attach", JOB])
    assert execute(plan).attach == f"claude attach {JOB}"
    assert len(fake_bin.calls("claude")) == 1


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "data" / "index.db")
    yield connection
    connection.close()


def launches(conn: sqlite3.Connection) -> list[tuple[str, str | None, list[str], str | None]]:
    rows = conn.execute("SELECT source, subcommand, flags, session_id FROM launches ORDER BY id")
    return [(r["source"], r["subcommand"], json.loads(r["flags"]), r["session_id"]) for r in rows]


def test_execute_records_launch(
    workdir: Path, fake_bin: FakeBin, env: dict[str, str], conn: sqlite3.Connection
) -> None:
    fake_bin.install("xterm")
    execute(bg_plan(workdir, ["xterm", "-e", "claude", "attach", JOB]), conn=conn)
    # Flag names only: the "opus" value is never stored.
    assert launches(conn) == [
        ("vigil", None, ["--bg", "--resume", "--model", "--remote-control"], SID),
        ("vigil", "attach", [], SID),
    ]
    assert "opus" not in json.dumps(launches(conn))


def test_execute_records_tmux_launch_without_tmux_args(
    workdir: Path, fake_bin: FakeBin, env: dict[str, str], conn: sqlite3.Connection
) -> None:
    fake_bin.install("tmux")
    plan = plan_resume(make_session(str(workdir)), OLD_CAPS, {}, RC, env)
    execute(plan, conn=conn)
    assert launches(conn) == [("vigil", None, ["--resume", "--remote-control"], SID)]


def test_execute_failure_records_nothing(
    workdir: Path, fake_bin: FakeBin, conn: sqlite3.Connection
) -> None:
    fake_bin.install("claude", exit_code=3)
    with pytest.raises(ResumeRefused):
        execute(bg_plan(workdir, None), conn=conn)
    assert launches(conn) == []


def wait_for_calls(fake_bin: FakeBin, name: str, count: int = 1) -> list[Call]:
    """The terminal is started detached and not waited for; give the fake a moment to log."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        calls = fake_bin.calls(name)
        if len(calls) >= count:
            return calls
        time.sleep(0.02)
    return fake_bin.calls(name)


LINK = "https://claude.ai/code/session_PLACEHOLDER01"
LAUNCH = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


def test_start_always_has_remote_control(workdir: Path, env: dict[str, str]) -> None:
    plan = plan_resume(make_session(str(workdir)), ALL_CAPS, {}, ResumeConfig(), env)
    assert plan.start == ["claude", "--bg", "--resume", SID, "--remote-control"]
    flags = ("--remote-control", "--model", "opus")
    plan = plan_resume(make_session(str(workdir)), ALL_CAPS, {}, ResumeConfig(flags), env)
    assert plan.start == ["claude", "--bg", "--resume", SID, *flags]
    # Other default flags follow it.
    plan = plan_resume(
        make_session(str(workdir)), ALL_CAPS, {}, ResumeConfig(("--model", "opus")), env
    )
    assert plan.start == ["claude", "--bg", "--resume", SID, "--remote-control", "--model", "opus"]


def test_tmux_start_has_remote_control(
    workdir: Path, fake_bin: FakeBin, env: dict[str, str]
) -> None:
    fake_bin.install("tmux")
    plan = plan_resume(make_session(str(workdir)), OLD_CAPS, {}, ResumeConfig(), env)
    assert plan.start is not None
    assert plan.start[plan.start.index("claude") :] == [
        "claude",
        "--resume",
        SID,
        "--remote-control",
    ]


def background(workdir: Path, started_at: datetime | None = None) -> dict[str, LiveSession]:
    live = LiveSession(SID, "background", None, str(workdir), started_at=started_at)
    return {SID: live}


def test_remote_bg_without_link_is_restartable(workdir: Path, env: dict[str, str]) -> None:
    token = TERMINAL_ALLOWED.set(False)
    try:
        with pytest.raises(ResumeRefused) as refused:
            plan_resume(make_session(str(workdir)), ALL_CAPS, background(workdir), RC, env)
    finally:
        TERMINAL_ALLOWED.reset(token)
    assert refused.value.restartable is True
    assert "without Remote Control" in refused.value.reason
    assert f"claude attach {JOB}" in refused.value.reason
    assert "/remote-control" in refused.value.reason
    # At the desk the same session is simply attached to.
    plan = plan_resume(make_session(str(workdir)), ALL_CAPS, background(workdir), RC, env)
    assert plan.start is None


def test_remote_bg_with_link_is_attached(workdir: Path, env: dict[str, str]) -> None:
    session = replace(make_session(str(workdir)), remote_url=LINK, remote_url_at=LAUNCH)
    token = TERMINAL_ALLOWED.set(False)
    try:
        plan = plan_resume(session, ALL_CAPS, background(workdir, LAUNCH), RC, env)
    finally:
        TERMINAL_ALLOWED.reset(token)
    assert plan.start is None and plan.terminal is None


def test_interactive_refusal_mentions_remote_control(workdir: Path, env: dict[str, str]) -> None:
    live = {SID: LiveSession(session_id=SID, kind="interactive", name="demo", cwd=str(workdir))}
    with pytest.raises(ResumeRefused) as refused:
        plan_resume(make_session(str(workdir)), ALL_CAPS, live, RC, env)
    assert refused.value.reason.endswith(
        "; or type /remote-control in that terminal to make it joinable"
    )
    assert refused.value.restartable is False


def test_remote_interactive_with_a_link_starts_nothing(workdir: Path, env: dict[str, str]) -> None:
    live = {SID: LiveSession(SID, "interactive", "demo", str(workdir), started_at=LAUNCH)}
    session = replace(make_session(str(workdir)), remote_url=LINK, remote_url_at=LAUNCH)
    token = TERMINAL_ALLOWED.set(False)
    try:
        plan = plan_resume(session, ALL_CAPS, live, RC, env)
    finally:
        TERMINAL_ALLOWED.reset(token)
    assert plan.start is None and plan.terminal is None
    # At the desk it is still an open terminal, but it needs no hint: it is joinable already.
    with pytest.raises(ResumeRefused) as refused:
        plan_resume(session, ALL_CAPS, live, RC, env)
    assert "already open" in refused.value.reason
    assert "/remote-control" not in refused.value.reason


def test_joinable_link_reads_the_transcript_after_the_live_start(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        bridge_line(LINK, LAUNCH - timedelta(minutes=1))
        + bridge_line("https://claude.ai/code/session_PLACEHOLDER02", LAUNCH + timedelta(minutes=1))
    )
    session = replace(make_session(None), transcript_path=str(transcript))
    started = LiveSession(SID, "background", None, None, started_at=LAUNCH)
    assert joinable_link(session, {SID: started}) == "https://claude.ai/code/session_PLACEHOLDER02"
    assert joinable_link(session, {}) is None  # not live
    unknown_start = LiveSession(SID, "background", None, None)
    assert joinable_link(session, {SID: unknown_start}) is not None
    assert joinable_link(make_session(None), {SID: started}) is None  # unreadable transcript


def bridge_line(url: str, at: datetime) -> str:
    record = {"type": "system", "subtype": "bridge_status", "url": url, "timestamp": at.isoformat()}
    return json.dumps(record) + "\n"


class FakeTime:
    """A clock that only moves when `sleep` is called; `on_sleep` runs after each sleep."""

    def __init__(self, on_sleep: Callable[[int], None] | None = None) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self._on_sleep = on_sleep

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        if self._on_sleep is not None:
            self._on_sleep(len(self.sleeps))


def test_wait_for_remote_url_times_out(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    # Only a link from before the launch, and then a missing file: neither counts.
    transcript.write_text(bridge_line(LINK, LAUNCH - timedelta(minutes=1)))
    time_ = FakeTime()
    assert (
        wait_for_remote_url(transcript, LAUNCH, 2.0, clock=time_.clock, sleep=time_.sleep) is None
    )
    assert time_.now == 2.0 and time_.sleeps == [0.5] * 4
    missing = tmp_path / "missing.jsonl"
    time_ = FakeTime()
    assert wait_for_remote_url(missing, LAUNCH, 1.0, clock=time_.clock, sleep=time_.sleep) is None


def test_wait_for_remote_url_sees_appended_line(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(bridge_line(LINK, LAUNCH - timedelta(minutes=1)))
    new = "https://claude.ai/code/session_PLACEHOLDER02"

    def append(sleeps: int) -> None:
        if sleeps == 2:
            with transcript.open("a") as handle:
                handle.write(bridge_line(new, LAUNCH + timedelta(seconds=3)))

    time_ = FakeTime(append)
    found = wait_for_remote_url(transcript, LAUNCH, 30.0, clock=time_.clock, sleep=time_.sleep)
    assert found == new
    assert time_.sleeps == [0.5, 0.5]  # returned as soon as it appeared


class Clock:
    """A fake clock whose sleep advances it, so polling needs no real waiting."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_stop_session_waits_until_gone(fake_bin: FakeBin, env: dict[str, str]) -> None:
    clock = Clock()
    probes: list[float] = []

    def probe() -> dict[str, LiveSession]:
        probes.append(clock.now)
        gone = len(probes) > 3
        return {} if gone else {SID: LiveSession(SID, "background", None, "/x")}

    stop_session(SID, probe, clock=clock, sleep=clock.sleep)
    assert [call.argv for call in fake_bin.calls("claude")] == [["stop", JOB]]
    assert probes == [0.0, 0.5, 1.0, 1.5]


def test_stop_session_refuses_when_still_live(fake_bin: FakeBin, env: dict[str, str]) -> None:
    clock = Clock()

    def probe() -> dict[str, LiveSession]:
        return {SID: LiveSession(SID, "background", None, "/x")}

    with pytest.raises(ResumeRefused) as refused:
        stop_session(SID, probe, timeout=2.0, clock=clock, sleep=clock.sleep)
    assert refused.value.reason == "could not stop the session; nothing was restarted"
    assert refused.value.restartable is False
    assert clock.now >= 2.0
    # Only `claude stop` ran: nothing was started.
    assert [call.argv for call in fake_bin.calls("claude")] == [["stop", JOB]]


def test_stop_session_refuses_on_exit_code(fake_bin: FakeBin) -> None:
    fake_bin.install("claude", exit_code=1)
    probed: list[None] = []

    def probe() -> dict[str, LiveSession]:
        probed.append(None)
        return {}

    with pytest.raises(ResumeRefused, match="could not stop the session; nothing was restarted"):
        stop_session(SID, probe)
    assert probed == []  # no point looking when the stop failed


def test_stop_session_refuses_a_bad_id_and_a_missing_claude(fake_bin: FakeBin) -> None:
    with pytest.raises(ResumeRefused, match="not a valid session id"):
        stop_session("--all", dict)
    with pytest.raises(ResumeRefused, match="could not stop the session"):
        stop_session(SID, dict)  # no `claude` on PATH
    assert fake_bin.calls("claude") == []


def test_stop_session_treats_an_unknown_live_state_as_still_live(fake_bin: FakeBin) -> None:
    fake_bin.install("claude")
    clock = Clock()
    answers: list[dict[str, LiveSession] | None] = [None, {}, None]

    def probe() -> dict[str, LiveSession] | None:
        return answers.pop(0) if answers else None

    # A failed look is not "gone": the stop is only believed once a look succeeds and lacks it.
    stop_session(SID, probe, clock=clock, sleep=clock.sleep)
    assert clock.now == 0.5 and answers == [None]
    with pytest.raises(ResumeRefused, match="could not stop the session"):
        stop_session(SID, probe, timeout=1.0, clock=clock, sleep=clock.sleep)


def test_extra_flags_appended_and_checked(workdir: Path, env: dict[str, str]) -> None:
    cfg = ResumeConfig(default_flags=("--model", "sonnet", "--verbose"))
    plan = plan_resume(
        make_session(str(workdir)), ALL_CAPS, {}, cfg, env, extra_flags=["--model=opus"]
    )
    # The suggested value replaces the default one; the rest of the defaults stay.
    assert plan.start == [
        *("claude", "--bg", "--resume", SID, "--remote-control"),
        *("--model", "opus", "--verbose"),
    ]
    # A flag the defaults lack is added after them.
    extra = ["--permission-mode", "plan"]
    plan = plan_resume(make_session(str(workdir)), ALL_CAPS, {}, cfg, env, extra_flags=extra)
    assert plan.start is not None
    assert plan.start[5:] == ["--model", "sonnet", "--verbose", "--permission-mode", "plan"]
    for bad in (["--dangerously-skip-permissions"], ["--permission-mode", "bypassPermissions"]):
        with pytest.raises(ResumeRefused, match="not allowed"):
            plan_resume(make_session(str(workdir)), ALL_CAPS, {}, cfg, env, extra_flags=bad)


NEW_ID = "0123abcd-0000-4000-8000-0000000000ff"


def test_start_new_bg_argv(workdir: Path, env: dict[str, str]) -> None:
    cfg = ResumeConfig(default_flags=("--verbose",))
    plan = plan_start_new(str(workdir), ["--model", "opus"], ALL_CAPS, cfg, env, NEW_ID)
    assert plan.method == "bg"
    # `claude --bg` picks the new session's id itself (it ignores `--session-id`).
    assert plan.start == ["claude", "--bg", "--remote-control", "--verbose", "--model", "opus"]
    assert plan.start_cwd == str(workdir)
    assert plan.terminal is None  # headless
    assert plan.session_id is None  # known once it has started (`execute`)
    assert plan.attach == ["claude", "agents"]  # until then, list the sessions to find it


def test_start_new_tmux_argv(workdir: Path, fake_bin: FakeBin, env: dict[str, str]) -> None:
    fake_bin.install("tmux")
    plan = plan_start_new(str(workdir), ["--model", "opus"], OLD_CAPS, RC, env, NEW_ID)
    assert plan.method == "tmux"
    assert plan.start == [
        "tmux",
        "new-session",
        "-d",
        "-s",
        "vigil-0123abcd",
        "-c",
        str(workdir),
        "claude",
        "--session-id",
        NEW_ID,
        "--remote-control",
        "--model",
        "opus",
    ]
    assert plan.attach == ["tmux", "attach", "-t", "vigil-0123abcd"]


def test_start_new_refuses_missing_cwd(
    tmp_path: Path, fake_bin: FakeBin, env: dict[str, str]
) -> None:
    gone = tmp_path / "deleted-project"
    with pytest.raises(ResumeRefused) as refused:
        plan_start_new(str(gone), [], ALL_CAPS, RC, env, NEW_ID)
    assert refused.value.reason == f"folder no longer exists: {gone}"
    with pytest.raises(ResumeRefused, match="no working folder"):
        plan_start_new(None, [], ALL_CAPS, RC, env, NEW_ID)
    with pytest.raises(ResumeRefused, match="not a valid session id"):
        plan_start_new(str(tmp_path), [], ALL_CAPS, RC, env, "--resume")
    with pytest.raises(ResumeRefused, match="not allowed"):
        plan_start_new(str(tmp_path), ["--settings", "x"], ALL_CAPS, RC, env, NEW_ID)
    assert fake_bin.calls("claude") == []


def test_wait_for_remote_url_finds_a_new_sessions_transcript(tmp_path: Path) -> None:
    home = tmp_path / "claude_home"
    project = home / "projects" / "-placeholder-work"
    project.mkdir(parents=True)
    assert find_transcript(home, NEW_ID) is None

    def create(sleeps: int) -> None:
        if sleeps == 2:  # Claude Code makes the transcript a moment after starting
            (project / f"{NEW_ID}.jsonl").write_text(
                bridge_line(LINK, LAUNCH + timedelta(seconds=1))
            )

    time_ = FakeTime(create)
    look = partial(find_transcript, home, NEW_ID)
    assert wait_for_remote_url(look, LAUNCH, 30.0, clock=time_.clock, sleep=time_.sleep) == LINK
    assert time_.sleeps == [0.5, 0.5]
    assert find_transcript(home, NEW_ID) == project / f"{NEW_ID}.jsonl"


COPY = "5e6f7a8b-0000-4000-8000-0000000000cc"

_BG_CLAUDE = """#!{python}
import json, os, sys
args = sys.argv[1:]
with open({log!r}, "a") as handle:
    handle.write(json.dumps({{"argv": args, "cwd": os.getcwd()}}) + "\\n")
if args[:1] == ["agents"]:
    sys.stdout.write(json.dumps({live!r}))
elif "--bg" in args:
    sys.stdout.write({output!r})
"""


def install_bg_claude(
    fake_bin: FakeBin, output: str, live: list[dict[str, object]] | None = None
) -> None:
    """A fake `claude` whose `--bg` prints `output` and whose `agents --json` lists `live`."""
    script = fake_bin.directory / "claude"
    script.write_text(
        _BG_CLAUDE.format(
            python=sys.executable,
            log=str(fake_bin.directory / ".calls" / "claude.jsonl"),
            output=output,
            live=live or [],
        )
    )
    script.chmod(0o755)


def backgrounded(session_id: str, note: str = "") -> str:
    """What the real `claude --bg` prints once it has started `session_id`."""
    job = session_id[:8]
    return (
        note
        + "Starting background service…\n"
        + f"backgrounded · {job} (idle — send a prompt to start)\n"
        + "  claude agents             list sessions\n"
        + f"  claude attach {job}    open in this terminal\n"
    )


def entry(session_id: str, cwd: Path | str) -> dict[str, object]:
    return {"sessionId": session_id, "kind": "background", "cwd": str(cwd), "status": "idle"}


def test_execute_attaches_to_the_copy_claude_started(
    workdir: Path, fake_bin: FakeBin, env: dict[str, str], conn: sqlite3.Connection
) -> None:
    # A session that was itself started in the background keeps its own options: resumed with
    # flags, `claude --bg` starts a copy under a new id, and says so.
    note = (
        f"note: background session {JOB} keeps its own saved options, so the flags you passed "
        f"started a copy as {COPY[:8]}. Without flags, the same command continues {JOB} itself.\n"
    )
    install_bg_claude(
        fake_bin, backgrounded(COPY, note), [entry(SID, "/elsewhere"), entry(COPY, workdir)]
    )
    fake_bin.install("xterm")
    plan = plan_resume(make_session(str(workdir)), ALL_CAPS, {}, RC, {**env, "DISPLAY": ":0"})
    launched = execute(plan, conn=conn)
    assert launched == Launched(
        attach=f"claude attach {COPY[:8]}",
        terminal=["xterm", "-e", "claude", "attach", COPY[:8]],
        session_id=COPY,
        copy_of=SID,
    )
    [xterm] = wait_for_calls(fake_bin, "xterm")
    assert xterm.argv == ["-e", "claude", "attach", COPY[:8]]
    assert [session for *_, session in launches(conn)] == [COPY, COPY]


def test_execute_resumed_in_place(workdir: Path, fake_bin: FakeBin, env: dict[str, str]) -> None:
    install_bg_claude(fake_bin, backgrounded(SID), [entry(SID, workdir)])
    plan = plan_resume(make_session(str(workdir)), ALL_CAPS, {}, RC, env)
    assert execute(plan) == Launched(f"claude attach {JOB}", None, SID)


@pytest.mark.parametrize(
    ("output", "live"),
    [
        ("Starting background service\u2026\n", "listed"),  # no job id printed
        (backgrounded(SID), "missing"),  # its own job id, not listed as running
        (backgrounded(SID), "unlisted"),  # its own job id; the sessions cannot be listed
    ],
)
def test_execute_falls_back_to_the_requested_session(
    workdir: Path, fake_bin: FakeBin, env: dict[str, str], output: str, live: str
) -> None:
    sessions = {"listed": [entry(COPY, workdir)], "missing": [], "unlisted": "not json"}[live]
    install_bg_claude(fake_bin, output, sessions)  # type: ignore[arg-type]
    plan = plan_resume(make_session(str(workdir)), ALL_CAPS, {}, RC, env)
    assert execute(plan) == Launched(f"claude attach {JOB}", None, SID)


@pytest.mark.parametrize(
    "live",
    [
        "missing",  # not listed as running (yet)
        "elsewhere",  # listed, but in another folder (a worktree, say)
        "ambiguous",  # two running sessions share the job id
        "unlisted",  # the running sessions cannot be listed
    ],
)
def test_execute_attaches_to_a_copy_whose_full_id_is_unknown(
    workdir: Path,
    fake_bin: FakeBin,
    env: dict[str, str],
    conn: sqlite3.Connection,
    live: str,
) -> None:
    # `claude --bg` said it started another job than the one asked for: that is what runs, and
    # `claude attach` takes the job id, so attach to it even without its full id.
    other = COPY[:8] + "-0000-4000-8000-0000000000dd"
    sessions = {
        "missing": [entry(SID, workdir)],
        "elsewhere": [entry(COPY, workdir / "worktree")],
        "ambiguous": [entry(COPY, workdir), entry(other, workdir)],
        "unlisted": "not json",
    }[live]
    install_bg_claude(fake_bin, backgrounded(COPY), sessions)  # type: ignore[arg-type]
    fake_bin.install("xterm")
    plan = plan_resume(make_session(str(workdir)), ALL_CAPS, {}, RC, {**env, "DISPLAY": ":0"})
    attach = ["claude", "attach", COPY[:8]]
    assert execute(plan, conn=conn) == Launched(
        shlex.join(attach), ["xterm", "-e", *attach], None, copy_of=SID
    )
    [xterm] = wait_for_calls(fake_bin, "xterm")
    assert xterm.argv == ["-e", *attach]
    assert [session for *_, session in launches(conn)] == [None, None]


def test_execute_start_new_finds_the_new_session(
    workdir: Path, fake_bin: FakeBin, env: dict[str, str], conn: sqlite3.Connection
) -> None:
    install_bg_claude(fake_bin, backgrounded(COPY), [entry(COPY, workdir)])
    fake_bin.install("xterm")
    plan = plan_start_new(str(workdir), [], ALL_CAPS, RC, {**env, "DISPLAY": ":0"}, NEW_ID)
    assert execute(plan, conn=conn) == Launched(
        f"claude attach {COPY[:8]}", ["xterm", "-e", "claude", "attach", COPY[:8]], COPY
    )
    [xterm] = wait_for_calls(fake_bin, "xterm")
    assert xterm.argv == ["-e", "claude", "attach", COPY[:8]]
    assert [session for *_, session in launches(conn)] == [COPY, COPY]


def test_execute_start_new_with_only_its_job_id(
    workdir: Path, fake_bin: FakeBin, env: dict[str, str], conn: sqlite3.Connection
) -> None:
    install_bg_claude(fake_bin, backgrounded(COPY), [])  # printed its job id; not listed yet
    fake_bin.install("xterm")
    plan = plan_start_new(str(workdir), [], ALL_CAPS, RC, {**env, "DISPLAY": ":0"}, NEW_ID)
    attach = ["claude", "attach", COPY[:8]]
    assert execute(plan, conn=conn) == Launched(shlex.join(attach), ["xterm", "-e", *attach], None)
    [xterm] = wait_for_calls(fake_bin, "xterm")
    assert xterm.argv == ["-e", *attach]
    assert [session for *_, session in launches(conn)] == [None, None]


def test_execute_start_new_without_its_id_opens_no_terminal(
    workdir: Path, fake_bin: FakeBin, env: dict[str, str], conn: sqlite3.Connection
) -> None:
    install_bg_claude(fake_bin, "Starting background service…\n", [])
    fake_bin.install("xterm")
    plan = plan_start_new(str(workdir), [], ALL_CAPS, RC, {**env, "DISPLAY": ":0"}, NEW_ID)
    # Started, but which session it is cannot be told: list them to find it.
    assert execute(plan, conn=conn) == Launched("claude agents", None, None)
    assert fake_bin.calls("xterm") == []
    assert launches(conn) == [("vigil", None, ["--bg", "--remote-control"], None)]
