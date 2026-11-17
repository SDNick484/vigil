"""The feature catalog: the flags and subcommands the installed ``claude`` offers.

Nothing here is a hardcoded list of features. The catalog is parsed from ``claude --help`` and
from ``claude <subcommand> --help`` for each subcommand the top-level help lists, and stored per
Claude Code version. Everything degrades to "no catalog": a missing binary, a slow or failing
help command never raises.
"""

import re
import sqlite3
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal

from vigil.claude_cli import capabilities

_HELP_TIMEOUT = 5.0

Kind = Literal["flag", "subcommand", "subcommand_flag"]

# "Options:" / "Commands:" / "Arguments:" start a section; only the first two are catalogued.
_SECTION = re.compile(r"^([A-Z][A-Za-z ]*):\s*$")
# One entry starts at exactly two spaces of indent; deeper lines continue the description.
_ENTRY = re.compile(r"^  (\S.*)$")
# The spec ("-c, --continue", "--name <name>") is separated from the description by 2+ spaces.
_GAP = re.compile(r"\s{2,}")
# Where a flag's argument placeholder starts: "<name>" or "[name]".
_ARG = re.compile(r"\s[<\[]")
_FLAG_NAME = re.compile(r"-{1,2}[A-Za-z0-9][\w-]*")


@dataclass(frozen=True)
class CatalogEntry:
    kind: Kind
    name: str  # the long flag ("--resume") or the subcommand ("attach")
    short: str | None  # the short alias ("-r"), flags only
    parent: str | None  # the subcommand a subcommand_flag belongs to
    description: str


def _split_entry(head: str) -> tuple[str, str]:
    """Split an entry's first line into its spec and the start of its description."""
    parts = _GAP.split(head.strip(), maxsplit=1)
    return parts[0], parts[1] if len(parts) > 1 else ""


def _flag(spec: str, description: str) -> CatalogEntry | None:
    names = _FLAG_NAME.findall(_ARG.split(spec, maxsplit=1)[0])
    if not names:
        return None
    long_names = [n for n in names if n.startswith("--")]
    short_names = [n for n in names if not n.startswith("--")]
    name = long_names[0] if long_names else short_names[0]
    short = short_names[0] if long_names and short_names else None
    return CatalogEntry("flag", name, short, None, description)


def _subcommand(spec: str, description: str) -> CatalogEntry | None:
    # "plugin|plugins" and "stop|kill <id>" list aliases; the first name is the catalog name.
    first = spec.split(maxsplit=1)[0].split("|")[0]
    if not re.fullmatch(r"[A-Za-z][\w-]*", first):
        return None
    return CatalogEntry("subcommand", first, None, None, description)


def parse_help(text: str) -> list[CatalogEntry]:
    """Flags (``Options:``) and subcommands (``Commands:``) listed in a Commander-style help text.

    Handles descriptions wrapped over several lines (joined with single spaces), flags whose
    description starts on the line below the flag (long flag lists) and the ``-x, --long <arg>``,
    ``--long [arg]`` and ``--long, --alias`` forms. ``name`` is the first long name (or the short
    name when there is no long one); ``short`` is the short alias or None. Subcommand entries have
    no short alias and use the first of ``a|b`` aliases. Text with neither section yields [].
    """
    entries: list[CatalogEntry] = []
    section: str | None = None
    spec = ""
    pieces: list[str] = []

    def flush() -> None:
        nonlocal spec
        if spec:
            description = " ".join(pieces)
            entry = (
                _flag(spec, description) if section == "Options" else _subcommand(spec, description)
            )
            if entry is not None:
                entries.append(entry)
        spec = ""
        pieces.clear()

    for line in text.splitlines():
        header = _SECTION.match(line)
        if header:
            flush()
            section = header.group(1)
            continue
        if section not in ("Options", "Commands"):
            continue
        entry_line = _ENTRY.match(line)
        if entry_line:
            flush()
            spec, first = _split_entry(entry_line.group(1))
            if first:
                pieces.append(first)
        elif line.strip() and spec:
            pieces.append(line.strip())
        elif not line.strip():
            flush()
    flush()
    return entries


def short_to_long(entries: Sequence[CatalogEntry]) -> dict[str, str]:
    """Map each short flag alias to its long name (``-r`` to ``--resume``), top-level flags only
    (a subcommand's ``-x`` may mean something else than the top-level ``-x``)."""
    return {e.short: e.name for e in entries if e.kind == "flag" and e.short is not None}


def _run_help(argv: list[str]) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603 - argv is a fixed, vigil-built list
            argv,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_HELP_TIMEOUT,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None
    return result.stdout if result.returncode == 0 else None


def _stored_versions(conn: sqlite3.Connection) -> list[str]:
    return [row[0] for row in conn.execute("SELECT DISTINCT version FROM catalog")]


def refresh_catalog(conn: sqlite3.Connection, claude: str = "claude") -> str | None:
    """Make sure the catalog holds the installed ``claude``'s version; return that version.

    Nothing runs beyond ``claude --version`` when the version is already stored. For a new
    version it runs ``claude --help`` and ``claude <subcommand> --help`` for each subcommand
    listed (5 s each) and stores every flag and subcommand found, in one transaction. A
    subcommand whose help fails is skipped. Returns None, storing nothing, when ``claude`` is
    missing, reports no version, or its ``--help`` fails or lists nothing.
    """
    version = capabilities(claude).version
    if version is None:
        return None
    if version in _stored_versions(conn):
        return version
    help_text = _run_help([claude, "--help"])
    if help_text is None:
        return None
    entries = parse_help(help_text)
    if not entries:
        return None
    for command in [e for e in entries if e.kind == "subcommand"]:
        sub_help = _run_help([claude, command.name, "--help"])
        if sub_help is None:
            continue
        entries.extend(
            replace(e, kind="subcommand_flag", parent=command.name)
            for e in parse_help(sub_help)
            if e.kind == "flag"
        )
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO catalog (version, kind, name, short, parent, description) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(version, e.kind, e.name, e.short, e.parent, e.description) for e in entries],
        )
    return version


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", version))


def current_catalog(conn: sqlite3.Connection) -> list[CatalogEntry]:
    """The entries of the latest version stored (compared as numbers, so 2.1.312 is later than
    2.1.99), or [] when there are none."""
    versions = _stored_versions(conn)
    if not versions:
        return []
    latest = max(versions, key=_version_key)
    rows = conn.execute(
        "SELECT kind, name, short, parent, description FROM catalog WHERE version = ? "
        "ORDER BY kind, parent, name",
        (latest,),
    )
    return [
        CatalogEntry(r["kind"], r["name"], r["short"], r["parent"], r["description"]) for r in rows
    ]
