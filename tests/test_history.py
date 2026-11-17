import json
import os
import shutil
import sqlite3
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from vigil.config import UsageConfig
from vigil.history import (
    Invocation,
    default_history_files,
    import_history,
    parse_claude_invocation,
)
from vigil.index.db import connect

FIXTURES = Path(__file__).parent / "fixtures" / "history"
MTIME = 1_650_000_000  # What a history file without timestamps is dated with.


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "data" / "index.db")
    yield connection
    connection.close()


def history_file(tmp_path: Path, name: str) -> Path:
    """A copy of a fixture history file, dated MTIME."""
    path = tmp_path / "history" / name
    path.parent.mkdir(exist_ok=True)
    shutil.copy(FIXTURES / name, path)
    os.utime(path, (MTIME, MTIME))
    return path


def enabled(*files: Path, aliases: tuple[str, ...] = ()) -> UsageConfig:
    return UsageConfig(shell_history=True, history_files=files, claude_aliases=aliases)


def launches(conn: sqlite3.Connection) -> list[tuple[str, str | None, list[str]]]:
    """(at, subcommand, flags) of every shell launch, oldest first."""
    rows = conn.execute(
        "SELECT at, subcommand, flags FROM launches WHERE source = 'shell' ORDER BY at, id"
    ).fetchall()
    return [(row["at"], row["subcommand"], json.loads(row["flags"])) for row in rows]


def iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="microseconds")


_WATCHED: set[str] = set()
_OPENED: list[str] = []


def _audit(event: str, args: tuple[object, ...]) -> None:
    if event == "open" and _WATCHED and str(args[0]) in _WATCHED:
        _OPENED.append(str(args[0]))


sys.addaudithook(_audit)


def test_disabled_by_default_reads_nothing(tmp_path: Path, conn: sqlite3.Connection) -> None:
    path = history_file(tmp_path, "bash_history")
    assert UsageConfig().shell_history is False
    _WATCHED.add(str(path))
    try:
        # Both when the file is named in the config and when it is a default location.
        assert import_history(conn, UsageConfig(history_files=(path,)), {}) == 0
        env = {"HOME": str(tmp_path), "HISTFILE": str(path)}
        assert import_history(conn, UsageConfig(), env) == 0
        assert _OPENED == []
    finally:
        _WATCHED.clear()
    assert conn.execute("SELECT COUNT(*) FROM launches").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM history_state").fetchone()[0] == 0


def test_bash_plain_and_timestamped(tmp_path: Path, conn: sqlite3.Connection) -> None:
    plain = history_file(tmp_path, "bash_history")
    stamped = history_file(tmp_path, "bash_history_timestamped")
    assert import_history(conn, enabled(plain, stamped), {}) == 5
    assert launches(conn) == [
        (iso(MTIME), None, []),
        (iso(MTIME), None, ["-p", "--model"]),
        (iso(MTIME), "agents", ["--json"]),
        (iso(1700000100), None, ["--resume", "--permission-mode"]),
        (iso(1700000300), "mcp", []),
    ]


def test_zsh_extended(tmp_path: Path, conn: sqlite3.Connection) -> None:
    path = history_file(tmp_path, "zsh_history_extended")
    assert import_history(conn, enabled(path), {}) == 3
    assert launches(conn) == [
        (iso(1700000100), None, ["--model", "-n"]),
        (iso(1700000300), "doctor", []),
        # A command continued over two lines with a backslash is one launch.
        (iso(1700000400), None, ["-p", "--effort"]),
    ]


def test_zsh_plain(tmp_path: Path, conn: sqlite3.Connection) -> None:
    path = history_file(tmp_path, "zsh_history_plain")
    assert import_history(conn, enabled(path), {}) == 2
    assert launches(conn) == [
        (iso(MTIME), None, ["--continue"]),
        (iso(MTIME), "update", []),
    ]


def test_fish(tmp_path: Path, conn: sqlite3.Connection) -> None:
    path = history_file(tmp_path, "fish_history")
    assert import_history(conn, enabled(path), {}) == 3
    assert launches(conn) == [
        (iso(1700000100), None, ["--model", "-p"]),
        (iso(1700000300), "agents", []),
        # The escaped newline (`\n`) in the quoted prompt does not end the command.
        (iso(1700000400), None, ["-p", "--name"]),
    ]


def test_only_claude_lines_kept(tmp_path: Path, conn: sqlite3.Connection) -> None:
    path = tmp_path / "history"
    path.write_text(
        "ls\n"
        'git commit -m "claude"\n'
        "echo claude\n"
        "claude-code-notes --model x\n"
        "command -v claude\n"
        "cat claude\n"
    )
    assert import_history(conn, enabled(path), {}) == 0
    assert conn.execute("SELECT COUNT(*) FROM launches").fetchone()[0] == 0


def test_values_and_prompts_not_stored(tmp_path: Path, conn: sqlite3.Connection) -> None:
    path = tmp_path / "history"
    path.write_text(
        'claude -p "secret prompt" --model opus\n'
        "claude --name=hush --effort high -- secret-tail --not-a-flag\n"
        "claude fix the secret bug --model opus\n"
        'claude --model opus "unterminated secret prompt\n'
    )
    import_history(conn, enabled(path), {})
    assert launches(conn) == [
        (iso(path.stat().st_mtime_ns // 10**9), None, ["-p", "--model"]),
        (iso(path.stat().st_mtime_ns // 10**9), None, ["--name", "--effort"]),
        (iso(path.stat().st_mtime_ns // 10**9), None, ["--model"]),
        (iso(path.stat().st_mtime_ns // 10**9), None, ["--model"]),
    ]
    stored = json.dumps([tuple(row) for row in conn.execute("SELECT * FROM launches")])
    for leaked in ("secret", "opus", "hush", "high", "fix", "bug", "unterminated"):
        assert leaked not in stored


@pytest.mark.parametrize(
    ("line", "aliases", "expected"),
    [
        ("claude", (), Invocation(None, [])),
        ("/usr/local/bin/claude -p x", (), Invocation(None, ["-p"])),
        ("~/bin/claude --continue", (), Invocation(None, ["--continue"])),
        ("cc --model x", ("cc",), Invocation(None, ["--model"])),
        ("cc --model x", (), None),
        ("FOO=1 BAR=two claude doctor", (), Invocation("doctor", [])),
        ("env FOO=1 claude --bg", (), Invocation(None, ["--bg"])),
        ("env -u FOO -i claude agents --json", (), Invocation("agents", ["--json"])),
        ("sudo claude update", (), Invocation("update", [])),
        ("sudo -E -u someone claude --resume abc", (), Invocation(None, ["--resume"])),
        ("command claude --continue", (), Invocation(None, ["--continue"])),
        ("command -v claude", (), None),
        ("sudo env X=1 /opt/tools/claude -n a", (), Invocation(None, ["-n"])),
        ("claude --model opus && ls -la", (), Invocation(None, ["--model"])),
        ("claude -p hi | tee out.txt -a", (), Invocation(None, ["-p"])),
        ("claude --model opus; rm -rf x", (), Invocation(None, ["--model"])),
        ("claude -p hi > out.txt", (), Invocation(None, ["-p"])),
        ("claude mcp list", (), Invocation(None, [])),
        ("claude Review", (), Invocation(None, [])),
        ("claude review", (), Invocation("review", [])),
        ("claude -", (), Invocation(None, [])),
        ("", (), None),
        ("   ", (), None),
        ("echo claude", (), None),
        ("git claude --model x", (), None),
        ("claude-code --model x", (), None),
    ],
)
def test_parse_claude_invocation(
    line: str, aliases: tuple[str, ...], expected: Invocation | None
) -> None:
    assert parse_claude_invocation(line, aliases) == expected


def test_alias_and_path_and_env_prefix(tmp_path: Path, conn: sqlite3.Connection) -> None:
    path = tmp_path / "history"
    path.write_text(
        "cc --model opus\n"
        "/opt/tools/claude --continue\n"
        "env FOO=bar claude agents\n"
        "sudo claude --resume abc\n"
        "cc2 --model opus\n"
    )
    assert import_history(conn, enabled(path, aliases=("cc",)), {}) == 4
    assert [(sub, flags) for _, sub, flags in launches(conn)] == [
        (None, ["--model"]),
        (None, ["--continue"]),
        ("agents", []),
        (None, ["--resume"]),
    ]


def test_invalid_utf8_line_skipped_rest_parsed(tmp_path: Path, conn: sqlite3.Connection) -> None:
    path = tmp_path / "history"
    path.write_bytes(
        b"claude --first\n"
        b"\xff\xfe\x80 not a command \xc3\x28\n"
        b'claude -p "bad \xff\xfe bytes" --model opus\n'
        b"claude --mod\xffel x\n"
        b"claude --last\n"
    )
    assert import_history(conn, enabled(path), {}) == 4
    flags = [flags for _, _, flags in launches(conn)]
    # The line of garbage is dropped; a bad byte in a prompt does not hide the flags around it;
    # a bad byte inside a flag name drops that flag; every line after is still read.
    assert flags == [["--first"], ["-p", "--model"], [], ["--last"]]
    assert "�" not in json.dumps(flags, ensure_ascii=False)


def test_incremental_import(tmp_path: Path, conn: sqlite3.Connection) -> None:
    path = tmp_path / "history"
    path.write_text("claude --one\nls\n")
    config = enabled(path)
    assert import_history(conn, config, {}) == 1
    assert import_history(conn, config, {}) == 0  # Unchanged: nothing read, nothing recorded.

    with path.open("a") as handle:
        handle.write("claude --two\ncla")  # A line still being written.
    assert import_history(conn, config, {}) == 1
    with path.open("a") as handle:
        handle.write("ude --three\n")
    assert import_history(conn, config, {}) == 1
    assert [flags for _, _, flags in launches(conn)] == [["--one"], ["--two"], ["--three"]]

    state = conn.execute("SELECT size, offset FROM history_state").fetchone()
    assert state["offset"] == state["size"] == path.stat().st_size


def test_incremental_import_waits_for_timestamp_and_fish_entry(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    bash = tmp_path / "bash"
    bash.write_text("#1700000000\n")
    fish = tmp_path / "fish"
    fish.write_text("- cmd: claude --fish\n")
    config = enabled(bash, fish)
    assert import_history(conn, config, {}) == 0
    with bash.open("a") as handle:
        handle.write("claude --bash\n")
    with fish.open("a") as handle:
        handle.write("  when: 1700000050\n")
    assert import_history(conn, config, {}) == 2
    assert launches(conn) == [
        (iso(1700000000), None, ["--bash"]),
        (iso(1700000050), None, ["--fish"]),
    ]


def test_rewritten_file_does_not_count_timestamped_launches_twice(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    path = tmp_path / "history"
    path.write_text(": 1700000100:0;claude --one\n: 1700000200:0;claude --two\n")
    config = enabled(path)
    assert import_history(conn, config, {}) == 2
    # The shell compacts its history: the file shrinks and is read again from the start.
    path.write_text(": 1700000200:0;claude --two\n: 1700000300:0;claude -x\n")
    assert import_history(conn, config, {}) == 1
    assert [flags for _, _, flags in launches(conn)] == [["--one"], ["--two"], ["-x"]]


def test_missing_and_unreadable_files_are_skipped(tmp_path: Path, conn: sqlite3.Connection) -> None:
    good = tmp_path / "good"
    good.write_text("claude --ok\n")
    config = enabled(tmp_path / "missing", tmp_path, good)  # A directory cannot be read as a file.
    assert import_history(conn, config, {}) == 1


def test_default_files_used_when_none_configured(tmp_path: Path, conn: sqlite3.Connection) -> None:
    (tmp_path / ".bash_history").write_text("claude --from-default\n")
    env = {"HOME": str(tmp_path)}
    assert import_history(conn, UsageConfig(shell_history=True), env) == 1


def test_default_history_files(tmp_path: Path) -> None:
    home = tmp_path / "home"
    env = {"HOME": str(home)}
    assert default_history_files(env) == [
        home / ".bash_history",
        home / ".zsh_history",
        home / ".local/share/fish/fish_history",
    ]
    custom = {
        "HOME": str(home),
        "HISTFILE": str(home / ".zsh_history"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }
    assert default_history_files(custom) == [
        home / ".zsh_history",
        home / ".bash_history",
        tmp_path / "data/fish/fish_history",
    ]
    relative = {"HOME": str(home), "XDG_DATA_HOME": "relative/data"}
    assert default_history_files(relative)[-1] == home / ".local/share/fish/fish_history"


def test_migration_0003_creates_history_state(conn: sqlite3.Connection) -> None:
    # 0002 belongs to another change: whether or not it is present, 0003 applies.
    versions = [row[0] for row in conn.execute("SELECT version FROM schema_migrations")]
    assert 3 in versions
    columns = [row[1] for row in conn.execute("PRAGMA table_info(history_state)")]
    assert columns == ["path", "size", "mtime_ns", "offset"]
