"""`vigil ls [QUERY]`: list sessions, newest activity first."""

import argparse
import json
import sys
from datetime import datetime

from vigil.claude_cli import capabilities, live_sessions
from vigil.commands import open_index
from vigil.config import Config
from vigil.index.queries import SessionRow, list_sessions
from vigil.resume import short_id
from vigil.serialize import apply_status, display_name, session_to_dict

_NAME_WIDTH = 48
_HEADERS = ("NAME", "PROJECT", "BRANCH", "LAST ACTIVITY", "ID")


def format_time(at: datetime | None) -> str:
    """A timestamp in the machine's local time, to the minute; `-` when unknown."""
    return at.astimezone().strftime("%Y-%m-%d %H:%M") if at is not None else "-"


def _one_line(text: str, width: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _name(row: SessionRow) -> str:
    return _one_line(display_name(row) or row.first_prompt or "(no name)", _NAME_WIDTH)


def table(rows: list[SessionRow]) -> str:
    """The rows as aligned columns under a header line."""
    cells = [
        (
            _name(row),
            row.project_label,
            row.git_branch or "-",
            format_time(row.last_activity_at),
            short_id(row.session_id),
        )
        for row in rows
    ]
    widths = [max(len(line[i]) for line in [_HEADERS, *cells]) for i in range(len(_HEADERS))]
    lines = [_HEADERS, *cells]
    return "\n".join(
        "  ".join(c.ljust(w) for c, w in zip(line, widths, strict=True)).rstrip() for line in lines
    )


class LsCommand:
    name = "ls"
    help = "List sessions, newest activity first."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "query",
            nargs="*",
            help="words that must all appear in the name, note, first prompt or folder",
        )
        parser.add_argument("--project", metavar="KEY", help="only this project (its key)")
        parser.add_argument(
            "--all",
            action="store_true",
            help="include archived sessions and every session of a resume chain",
        )
        parser.add_argument("--limit", type=int, default=200, metavar="N", help="at most N rows")
        parser.add_argument("--json", action="store_true", help="print JSON, as the API serves it")

    def run(self, args: argparse.Namespace, config: Config) -> int:
        conn = open_index(config)
        try:
            rows = list_sessions(
                conn,
                query=" ".join(args.query) or None,
                project_key=args.project,
                include_archived=args.all,
                collapse_chains=not args.all,
                limit=args.limit,
            )
        finally:
            conn.close()
        if args.json:
            caps = capabilities()
            rows = apply_status(rows, caps, live_sessions(caps))
            print(json.dumps([session_to_dict(row) for row in rows], indent=2))
        elif rows:
            print(table(rows))
        else:
            print("No sessions found.", file=sys.stderr)
        return 0


COMMAND = LsCommand()
