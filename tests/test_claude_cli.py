"""Tests for claude_cli. Only fake `claude` executables are ever run (see FakeBin)."""

import json
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.conftest import FakeBin
from vigil import claude_cli
from vigil.claude_cli import Capabilities, LiveSession, capabilities, live_sessions

FIXTURES = Path(__file__).parent / "fixtures" / "claude_help"
NO_CAPS = Capabilities(version=None, has_bg=False, has_attach=False, has_agents_json=False)
ALL_CAPS = Capabilities(version="2.1.312", has_bg=True, has_attach=True, has_agents_json=True)


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    claude_cli.clear_cache()
    yield
    claude_cli.clear_cache()


def install_claude(
    fake_bin: FakeBin, *, version: str, help_text: str, agents_json: str = "[]"
) -> None:
    """A fake `claude` that answers --version, --help and `agents --json` like the real one."""
    script = fake_bin.directory / "claude"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        f"with open({str(fake_bin.directory / '.calls' / 'claude.jsonl')!r}, 'a') as log:\n"
        "    log.write(json.dumps({'argv': args, 'cwd': os.getcwd()}) + '\\n')\n"
        f"if args == ['--version']: sys.stdout.write({version!r})\n"
        f"elif args == ['--help']: sys.stdout.write({help_text!r})\n"
        f"elif args[:1] == ['agents']: sys.stdout.write({agents_json!r})\n"
        "else: sys.exit(2)\n"
    )
    script.chmod(0o755)


def test_caps_new_version(fake_bin: FakeBin) -> None:
    install_claude(
        fake_bin,
        version=(FIXTURES / "claude_version.txt").read_text(),
        help_text=(FIXTURES / "claude_help.txt").read_text(),
    )
    assert capabilities() == ALL_CAPS


def test_caps_old_version(fake_bin: FakeBin) -> None:
    old_help = "\n".join(
        line
        for line in (FIXTURES / "claude_help.txt").read_text().splitlines()
        if "--bg" not in line
    )
    install_claude(fake_bin, version="1.0.0 (Claude Code)\n", help_text=old_help)
    caps = capabilities()
    assert caps.version == "1.0.0"
    assert not caps.has_bg
    assert caps.has_attach  # still advertised by the old help text we built


def test_caps_old_version_without_agents(fake_bin: FakeBin) -> None:
    install_claude(
        fake_bin, version="1.0.0\n", help_text="Usage: claude [options]\n\nCommands:\n  mcp\n"
    )
    assert capabilities() == Capabilities("1.0.0", False, False, False)


def test_caps_missing_binary(fake_bin: FakeBin) -> None:
    assert capabilities() == NO_CAPS
    assert live_sessions(capabilities()) == {}


def test_caps_are_cached_per_version(fake_bin: FakeBin) -> None:
    help_text = (FIXTURES / "claude_help.txt").read_text()
    install_claude(fake_bin, version="2.1.312\n", help_text=help_text)
    assert capabilities().has_bg
    # Same version string, different help: the cached answer wins.
    install_claude(fake_bin, version="2.1.312\n", help_text="Usage: claude\n")
    assert capabilities().has_bg
    # A new version is probed again.
    install_claude(fake_bin, version="2.2.0\n", help_text="Usage: claude\n")
    assert not capabilities().has_bg


def test_live_sessions_parses_json(fake_bin: FakeBin) -> None:
    captured = (FIXTURES / "claude_agents_json_all.json").read_text()
    bg = {"sessionId": "bg-1", "kind": "background", "cwd": "/home/user/work", "name": ""}
    junk = ["not a dict", {"kind": "interactive"}, {"sessionId": 7}]
    install_claude(
        fake_bin,
        version="2.1.312\n",
        help_text=(FIXTURES / "claude_help.txt").read_text(),
        agents_json=json.dumps([*json.loads(captured), bg, *junk]),
    )
    assert live_sessions(ALL_CAPS) == {
        "00000000-0000-4000-8000-000000000001": LiveSession(
            "00000000-0000-4000-8000-000000000001",
            "interactive",
            "demo-session",
            "/home/user/projects/demo",
            busy=True,
            started_at=datetime(2026, 11, 17, 15, 7, 5, 568000, tzinfo=UTC),
        ),
        "bg-1": LiveSession("bg-1", "background", None, "/home/user/work"),
    }
    assert fake_bin.calls("claude")[-1].argv == ["agents", "--json"]


def test_live_sessions_needs_agents_json(fake_bin: FakeBin) -> None:
    install_claude(fake_bin, version="1.0.0\n", help_text="", agents_json='[{"sessionId": "x"}]')
    assert live_sessions(NO_CAPS) == {}


@pytest.mark.parametrize("output", ["", "not json", "{}", '"x"', "[1, null]", "[{"])
def test_live_sessions_handles_garbage(fake_bin: FakeBin, output: str) -> None:
    install_claude(fake_bin, version="2.1.312\n", help_text="", agents_json=output)
    assert live_sessions(ALL_CAPS) == {}


def test_live_sessions_handles_failure(fake_bin: FakeBin) -> None:
    fake_bin.install("claude", stdout='[{"sessionId": "x"}]', exit_code=1)
    assert live_sessions(ALL_CAPS) == {}


def test_live_sessions_handles_timeout(fake_bin: FakeBin, monkeypatch: pytest.MonkeyPatch) -> None:
    script = fake_bin.directory / "claude"
    script.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    script.chmod(0o755)
    monkeypatch.setattr(claude_cli, "_LIVE_TIMEOUT", 0.3)
    assert live_sessions(ALL_CAPS) == {}


def test_live_session_busy_and_started(fake_bin: FakeBin) -> None:
    entries = [
        {
            "pid": 1,
            "cwd": "/home/user/work",
            "kind": "interactive",
            "startedAt": 1794938034841,
            "sessionId": "busy-1",
            "name": "x",
            "status": "busy",
        },
        {"kind": "interactive", "sessionId": "idle-1", "status": "idle", "startedAt": "soon"},
    ]
    install_claude(fake_bin, version="2.1.312\n", help_text="", agents_json=json.dumps(entries))
    live = live_sessions(ALL_CAPS)
    assert live["busy-1"].busy is True
    assert live["busy-1"].started_at == datetime.fromtimestamp(1794938034.841, tz=UTC)
    assert live["busy-1"].started_at == datetime(2026, 11, 17, 17, 53, 54, 841000, tzinfo=UTC)
    assert live["idle-1"].busy is False
    assert live["idle-1"].started_at is None
