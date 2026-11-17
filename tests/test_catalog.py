"""Tests for the feature catalog, parsed from the captured `claude --help` fixtures. Only fake
`claude` executables are ever run (see FakeBin)."""

import re
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.conftest import FakeBin
from vigil import claude_cli
from vigil.catalog import CatalogEntry, current_catalog, parse_help, refresh_catalog, short_to_long
from vigil.index.db import connect

FIXTURES = Path(__file__).parent / "fixtures" / "claude_help"
HELP = (FIXTURES / "claude_help.txt").read_text()
VERSION = (FIXTURES / "claude_version.txt").read_text()
SUB_HELP = {
    "agents": (FIXTURES / "claude_agents_help.txt").read_text(),
    "attach": (FIXTURES / "claude_attach_help.txt").read_text(),
}


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    claude_cli.clear_cache()
    yield
    claude_cli.clear_cache()


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "data" / "index.db")
    yield connection
    connection.close()


def install_claude(
    fake_bin: FakeBin,
    *,
    version: str = VERSION,
    help_text: str = HELP,
    sub_help: dict[str, str] | None = None,
) -> None:
    """A fake `claude` answering --version, --help and `<subcommand> --help`; any other
    invocation (including a subcommand without help text) exits non-zero."""
    sub = SUB_HELP if sub_help is None else sub_help
    script = fake_bin.directory / "claude"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        f"with open({str(fake_bin.directory / '.calls' / 'claude.jsonl')!r}, 'a') as log:\n"
        "    log.write(json.dumps({'argv': args, 'cwd': os.getcwd()}) + '\\n')\n"
        f"sub = {sub!r}\n"
        f"if args == ['--version']: sys.stdout.write({version!r})\n"
        f"elif args == ['--help']: sys.stdout.write({help_text!r})\n"
        "elif len(args) == 2 and args[1] == '--help' and args[0] in sub:\n"
        "    sys.stdout.write(sub[args[0]])\n"
        "else: sys.exit(2)\n"
    )
    script.chmod(0o755)


def by_name(entries: list[CatalogEntry], kind: str) -> dict[str, CatalogEntry]:
    return {e.name: e for e in entries if e.kind == kind}


def test_parses_flags_with_long_names() -> None:
    flags = by_name(parse_help(HELP), "flag")
    for name in ("--resume", "--bg", "--remote-control", "--name"):
        assert name in flags
    assert flags["--name"].short == "-n"
    assert flags["--resume"].short == "-r"
    assert flags["--continue"].short == "-c"
    assert flags["--bg"].short is None
    # Argument placeholders are not part of the name.
    assert flags["--cloud"].name == "--cloud"
    assert flags["--debug"].short == "-d"
    assert all(e.parent is None for e in flags.values())
    assert not any("<" in name or "[" in name or "," in name for name in flags)


def test_parses_alias_flags_and_stacked_layout() -> None:
    """`--a, --b <x>` takes the first long name; a description on the line below still lands."""
    flags = by_name(parse_help(HELP), "flag")
    allowed = flags["--allowedTools"]
    assert allowed.short is None
    assert allowed.description.startswith("Comma or space-separated list of tool names to allow")
    assert "--allowed-tools" not in flags
    assert "--exclude-dynamic-system-prompt-sections" in flags
    assert flags["--exclude-dynamic-system-prompt-sections"].description.startswith(
        "Move per-machine"
    )


def test_parses_subcommands() -> None:
    commands = by_name(parse_help(HELP), "subcommand")
    assert "attach" in commands
    assert "agents" in commands
    assert commands["attach"].description.startswith("Open a background session in this terminal.")
    # Aliases ("plugin|plugins", "stop|kill <id>") collapse to the first name.
    assert "plugin" in commands
    assert "stop" in commands
    assert "plugins" not in commands
    assert all(e.short is None and e.parent is None for e in commands.values())


def test_multiline_descriptions_joined() -> None:
    flags = by_name(parse_help(HELP), "flag")
    assert flags["--add-dir"].description == "Additional directories to allow tool access to"
    bg = flags["--bg"].description
    assert bg.startswith("Start the session in the background and return immediately.")
    assert bg.endswith("or starts a copy and says so when the session is already running")
    assert "\n" not in bg and "  " not in bg
    commands = by_name(parse_help(HELP), "subcommand")
    assert commands["stop"].description.endswith("`claude --resume` works once it is stopped")


def test_nothing_is_dropped_or_invented() -> None:
    """Every `--flag` in the Options section is catalogued, and nothing but those."""
    options = HELP.split("\nOptions:\n")[1].split("\nCommands:\n")[0]
    spec = re.compile(r"^  (?:-\w, )?(--[\w-]+)(?:, (--[\w-]+))?")
    listed = {
        name
        for line in options.splitlines()
        for m in [spec.match(line)]
        if m
        for name in m.groups()
        if name
    }
    names = set(by_name(parse_help(HELP), "flag"))
    assert names <= listed
    # Alias long names are folded into their first name, so only those may be missing.
    assert listed - names == {"--allowed-tools", "--disallowed-tools", "--background"}


def test_parse_help_tolerates_garbage() -> None:
    assert parse_help("") == []
    assert parse_help("Usage: claude attach <id|name>\n\n  Open the session.\n") == []
    assert parse_help(SUB_HELP["attach"]) == []


def test_short_to_long() -> None:
    mapping = short_to_long(parse_help(HELP))
    assert mapping["-r"] == "--resume"
    assert mapping["-n"] == "--name"
    assert mapping["-p"] == "--print"
    assert mapping["-h"] == "--help"
    assert "--bg" not in mapping
    sub_entry = CatalogEntry("subcommand_flag", "--json", "-j", "agents", "x")
    assert short_to_long([sub_entry]) == {}


def test_refresh_stores_flags_subcommands_and_subcommand_flags(
    fake_bin: FakeBin, conn: sqlite3.Connection
) -> None:
    install_claude(fake_bin)
    assert refresh_catalog(conn) == "2.1.312"
    entries = current_catalog(conn)
    assert {"--resume", "--bg"} <= set(by_name(entries, "flag"))
    assert {"attach", "agents"} <= set(by_name(entries, "subcommand"))
    sub_flags = [e for e in entries if e.kind == "subcommand_flag"]
    assert {e.parent for e in sub_flags} == {"agents"}  # attach has no options of its own
    assert {"--json", "--all", "--cwd"} <= {e.name for e in sub_flags}
    # Each subcommand listed was asked for its help, none was invented.
    asked = {call.argv[0] for call in fake_bin.calls("claude") if call.argv[1:] == ["--help"]}
    assert asked == {e.name for e in entries if e.kind == "subcommand"}
    assert (
        conn.execute("SELECT COUNT(*) FROM catalog WHERE version != '2.1.312'").fetchone()[0] == 0
    )


def test_refresh_only_on_new_version(fake_bin: FakeBin, conn: sqlite3.Connection) -> None:
    install_claude(fake_bin)
    assert refresh_catalog(conn) == "2.1.312"
    count = conn.execute("SELECT COUNT(*) FROM catalog").fetchone()[0]
    calls = len(fake_bin.calls("claude"))
    assert refresh_catalog(conn) == "2.1.312"
    # Only `--version` ran again; nothing was re-read or re-stored.
    assert [c.argv for c in fake_bin.calls("claude")[calls:]] == [["--version"]]
    assert conn.execute("SELECT COUNT(*) FROM catalog").fetchone()[0] == count

    # A new version is catalogued next to the old one, and becomes current.
    install_claude(
        fake_bin,
        version="2.1.400 (Claude Code)\n",
        help_text="Options:\n  --brand-new                           A flag added later\n",
        sub_help={},
    )
    assert refresh_catalog(conn) == "2.1.400"
    assert [e.name for e in current_catalog(conn)] == ["--brand-new"]
    versions = {r[0] for r in conn.execute("SELECT DISTINCT version FROM catalog")}
    assert versions == {"2.1.312", "2.1.400"}


def test_current_catalog_orders_versions_as_numbers(conn: sqlite3.Connection) -> None:
    with conn:
        for version in ("2.1.312", "2.1.99"):
            conn.execute(
                "INSERT INTO catalog VALUES (?, 'flag', '--x', NULL, NULL, ?)", (version, version)
            )
    assert [e.description for e in current_catalog(conn)] == ["2.1.312"]


def test_current_catalog_empty(conn: sqlite3.Connection) -> None:
    assert current_catalog(conn) == []


def test_missing_claude_returns_none(fake_bin: FakeBin, conn: sqlite3.Connection) -> None:
    assert refresh_catalog(conn) is None
    assert current_catalog(conn) == []


def test_failing_help_stores_nothing_and_retries(
    fake_bin: FakeBin, conn: sqlite3.Connection
) -> None:
    install_claude(fake_bin, help_text="")
    assert refresh_catalog(conn) is None  # lists nothing
    assert current_catalog(conn) == []
    install_claude(fake_bin)
    assert refresh_catalog(conn) == "2.1.312"  # not remembered as done
    assert current_catalog(conn)


def test_subcommand_without_help_is_skipped(fake_bin: FakeBin, conn: sqlite3.Connection) -> None:
    install_claude(fake_bin, sub_help={})
    assert refresh_catalog(conn) == "2.1.312"
    kinds = {e.kind for e in current_catalog(conn)}
    assert kinds == {"flag", "subcommand"}


def test_duplicate_entries_do_not_break_refresh(
    fake_bin: FakeBin, conn: sqlite3.Connection
) -> None:
    doubled = "Options:\n  --a  one\n  --a  two\nCommands:\n  x  cmd\n  x  cmd\n"
    install_claude(fake_bin, help_text=doubled, sub_help={})
    assert refresh_catalog(conn) == "2.1.312"
    assert len(current_catalog(conn)) == 2
