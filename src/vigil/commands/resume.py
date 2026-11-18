"""`vigil resume ID [--print]`: resume a session in its folder."""

import argparse
import os
import shlex
import sys

from vigil.claude_cli import capabilities, live_sessions
from vigil.commands import find_session, open_index
from vigil.config import Config
from vigil.resume import ResumePlan, ResumeRefused, copy_note, execute, plan_resume


def describe(plan: ResumePlan) -> str:
    """What `plan` would run, as shell commands."""
    if plan.start is None:
        lines = ["Already running in the background; nothing to start."]
    else:
        lines = [f"Start:  cd {shlex.quote(plan.start_cwd)} && {shlex.join(plan.start)}"]
    lines.append(f"Attach: {shlex.join(plan.attach)}")
    if plan.terminal is not None:
        lines.append(f"Open:   {shlex.join(plan.terminal)}")
    return "\n".join(lines)


class ResumeCommand:
    name = "resume"
    help = "Resume a session in its folder (refuses when it is open or its folder is gone)."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("id", metavar="ID", help="session id, a unique id prefix, or its title")
        parser.add_argument(
            "--print",
            action="store_true",
            dest="print_only",
            help="print the commands instead of running them",
        )

    def run(self, args: argparse.Namespace, config: Config) -> int:
        conn = open_index(config)
        try:
            row = find_session(conn, args.id)
            if row is None:
                return 1
            caps = capabilities()
            try:
                plan = plan_resume(row, caps, live_sessions(caps), config.resume, os.environ)
                if args.print_only:
                    print(describe(plan))
                    return 0
                launched = execute(plan, conn=conn)
            except ResumeRefused as refused:
                print(f"vigil: {refused.reason}", file=sys.stderr)
                return 2
        finally:
            conn.close()
        if launched.terminal is None:
            print(f"No terminal available. Attach with: {launched.attach}")
        else:
            print(f"Opened a terminal. To attach yourself: {launched.attach}")
        if launched.copy_of is not None and launched.session_id is not None:
            print(copy_note(launched.session_id))
        return 0


COMMAND = ResumeCommand()
