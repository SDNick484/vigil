"""Tests for the resume launcher. Only fakes on PATH are ever run (see FakeBin): the fake
`claude`, `tmux` and terminals record their argv and cwd and exit; none of them runs anything."""

import json
import sqlite3
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.conftest import Call, FakeBin
from vigil.claude_cli import Capabilities, LiveSession
from vigil.config import ResumeConfig
from vigil.index.db import connect
from vigil.index.queries import SessionRow
from vigil.remote import wait_for_remote_url
from vigil.resume import (
    TERMINAL_ALLOWED,
    ResumePlan,
    ResumeRefused,
    execute,
    joinable_link,
    plan_resume,
    stop_session,
)

pytestmark = pytest.mark.usefixtures("proc_version")

SID = "0123abcd-0000-4000-8000-000000000001"
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
    assert plan.attach == ["claude", "attach", SID]
    assert plan.terminal is None  # headless
    assert plan.session_id == SID


def test_already_background_skips_start(workdir: Path, env: dict[str, str]) -> None:
    live = {SID: LiveSession(session_id=SID, kind="background", name=None, cwd=str(workdir))}
    # Even with tmux forced, a background session is attached to, never started a second time.
    cfg = ResumeConfig(launcher="tmux")
    plan = plan_resume(make_session(str(workdir)), ALL_CAPS, live, cfg, env)
    assert plan.start is None
    assert plan.method == "bg"
    assert plan.attach == ["claude", "attach", SID]


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
    assert plan.start == ["claude", "--bg", "--resume", SID, *flags]


def test_disallowed_flag_refused(workdir: Path, env: dict[str, str]) -> None:
    cfg = ResumeConfig(default_flags=("--dangerously-skip-permissions",))
    with pytest.raises(ResumeRefused, match="flag not allowed"):
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
    assert plan.terminal == ["xterm", "-e", "claude", "attach", SID]


def bg_plan(workdir: Path, terminal: list[str] | None) -> ResumePlan:
    return ResumePlan(
        start=["claude", "--bg", "--resume", SID, "--model", "opus", "--remote-control"],
        start_cwd=str(workdir),
        attach=["claude", "attach", SID],
        terminal=terminal,
        method="bg",
        session_id=SID,
    )


def test_execute_runs_fakes_with_cwd(workdir: Path, fake_bin: FakeBin, env: dict[str, str]) -> None:
    fake_bin.install("xterm")
    plan = plan_resume(make_session(str(workdir)), ALL_CAPS, {}, RC, {**env, "DISPLAY": ":0"})
    assert execute(plan) == f"claude attach {SID}"
    [claude] = fake_bin.calls("claude")
    assert claude.argv == ["--bg", "--resume", SID, "--remote-control"]
    assert claude.cwd == str(workdir)
    [xterm] = wait_for_calls(fake_bin, "xterm")
    assert xterm.argv == ["-e", "claude", "attach", SID]
    assert xterm.cwd == str(workdir)


def test_execute_tmux(workdir: Path, fake_bin: FakeBin, env: dict[str, str]) -> None:
    fake_bin.install("tmux")
    plan = plan_resume(make_session(str(workdir)), OLD_CAPS, {}, RC, env)
    assert execute(plan) == "tmux attach -t vigil-0123abcd"
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
        execute(bg_plan(workdir, ["xterm", "-e", "claude", "attach", SID]))
    assert fake_bin.calls("xterm") == []


def test_execute_start_missing_refused(workdir: Path, fake_bin: FakeBin) -> None:
    with pytest.raises(ResumeRefused, match="could not run"):
        execute(bg_plan(workdir, None))


def test_execute_terminal_missing_still_returns_attach(
    workdir: Path, fake_bin: FakeBin, env: dict[str, str]
) -> None:
    plan = bg_plan(workdir, ["not-installed-terminal", "-e", "claude", "attach", SID])
    assert execute(plan) == f"claude attach {SID}"
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
    execute(bg_plan(workdir, ["xterm", "-e", "claude", "attach", SID]), conn=conn)
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
    assert f"claude attach {SID}" in refused.value.reason
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
    assert [call.argv for call in fake_bin.calls("claude")] == [["stop", SID]]
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
    assert [call.argv for call in fake_bin.calls("claude")] == [["stop", SID]]


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
