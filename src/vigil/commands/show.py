"""`vigil show ID`: everything vigil knows about one session."""

import argparse
import json
from datetime import datetime

from vigil.claude_cli import capabilities, live_sessions
from vigil.commands import find_session, open_index, resume_command
from vigil.config import Config
from vigil.index.queries import SessionRow, chain
from vigil.resume import short_id
from vigil.serialize import apply_status, display_name, session_to_dict


def _time(at: datetime | None) -> str:
    return at.astimezone().strftime("%Y-%m-%d %H:%M:%S") if at is not None else "-"


def _text(label: str, value: str | None) -> list[str]:
    if not value:
        return []
    lines = value.splitlines() or [""]
    return [f"{label}: {lines[0]}", *(f"    {line}" for line in lines[1:])]


def render(row: SessionRow, links: list[SessionRow]) -> str:
    """A session as labelled lines, ending with the command that resumes it by hand."""
    lines = [
        f"Session:  {row.session_id} ({short_id(row.session_id)})",
        f"Name:     {display_name(row) or '-'}",
        f"Project:  {row.project_label}",
        f"Folder:   {row.cwd or '-'}",
        f"Branch:   {row.git_branch or '-'}",
        f"Status:   {row.status}",
        f"Started:  {_time(row.started_at)}",
        f"Active:   {_time(row.last_activity_at)}",
        f"Messages: {row.message_count}",
        f"Models:   {', '.join(row.models) or '-'}",
        f"Version:  {row.version or '-'}",
        f"Machine:  {row.machine}",
    ]
    flags = [name for name, on in (("pinned", row.pinned), ("archived", row.archived)) if on]
    if flags:
        lines.append(f"Flags:    {', '.join(flags)}")
    lines += _text("Note", row.note)
    if len(links) > 1:
        lines.append("Chain:    " + " -> ".join(short_id(link.session_id) for link in links))
    lines += _text("First prompt", row.first_prompt)
    lines += _text("Last you said", row.last_user)
    lines += _text("Last Claude said", row.last_assistant)
    lines.append(f"Resume:   {resume_command(row)}")
    return "\n".join(lines)


class ShowCommand:
    name = "show"
    help = "Show one session: where you left off, and the command to resume it."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("id", metavar="ID", help="session id, a unique id prefix, or its title")
        parser.add_argument("--json", action="store_true", help="print JSON, as the API serves it")

    def run(self, args: argparse.Namespace, config: Config) -> int:
        conn = open_index(config)
        try:
            row = find_session(conn, args.id)
            if row is None:
                return 1
            links = chain(conn, row.chain_id)
        finally:
            conn.close()
        caps = capabilities()
        live = live_sessions(caps)
        row, *_ = apply_status([row], caps, live)
        links = apply_status(links, caps, live)
        if args.json:
            data = session_to_dict(row)
            data["chain"] = [session_to_dict(link) for link in links]
            data["resume_command"] = resume_command(row)
            print(json.dumps(data, indent=2))
        else:
            print(render(row, links))
        return 0


COMMAND = ShowCommand()
