"""CLI commands. Each module defines `COMMAND`, registered in `vigil.cli.COMMANDS`.

Shared by the commands that read the index: they open it, bring it up to date with an incremental
scan, and look sessions up the same way.
"""

import shlex
import sqlite3
import sys

from vigil.config import Config
from vigil.index.db import connect
from vigil.index.queries import SessionRow, get_session
from vigil.index.scanner import ScanStats
from vigil.index.scanner import scan as scan_transcripts  # `scan` is the submodule's name
from vigil.paths import index_path


def open_index(config: Config) -> sqlite3.Connection:
    """Open the index (creating it on first use) and scan the transcripts into it.

    The scan is incremental, so this is cheap when nothing changed.
    """
    return scan_index(config)[0]


def scan_index(config: Config) -> tuple[sqlite3.Connection, ScanStats]:
    """Open the index and scan; return the connection and what the scan did."""
    conn = connect(index_path())
    return conn, scan_transcripts(conn, config.claude_home)


def find_session(conn: sqlite3.Connection, ident: str) -> SessionRow | None:
    """The session `ident` names (id, unique id prefix or exact title); say so on stderr and
    return None when there is none."""
    row = get_session(conn, ident)
    if row is None:
        print(
            f"vigil: no session matches {ident!r} (unknown, or an ambiguous id prefix)",
            file=sys.stderr,
        )
    return row


def resume_command(row: SessionRow) -> str:
    """The shell command that resumes `row` by hand, in its folder."""
    command = f"claude --resume {row.session_id}"
    return f"cd {shlex.quote(row.cwd)} && {command}" if row.cwd else command
