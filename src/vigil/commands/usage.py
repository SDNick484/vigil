"""`vigil usage`: what you use in Claude Code, how you launch it, and what you have never tried."""

import argparse
import json
import sys
from datetime import datetime

from vigil.commands import open_index
from vigil.config import Config
from vigil.usage import FeatureUse, UsageReport, build_report, refresh_sources, report_to_dict

HISTORY_HINT = (
    "Launch history is off. Set `shell_history = true` under `[usage]` in the vigil config "
    "(usage.shell_history = true) to count the flags you start claude with; only subcommand and "
    "flag names are kept, never values or prompts."
)
_LIMIT = 25


def _time(at: datetime | None) -> str:
    return at.astimezone().strftime("%Y-%m-%d %H:%M") if at is not None else "-"


def _uses(uses: list[FeatureUse], *, limit: int | None) -> list[str]:
    shown = uses if limit is None else uses[:limit]
    rows = [("KIND", "NAME", "COUNT", "LAST USED")] + [
        (u.kind, u.name, str(u.count), _time(u.last_used_at)) for u in shown
    ]
    widths = [max(len(row[i]) for row in rows) for i in range(4)]
    lines = [
        "  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip() for row in rows
    ]
    if len(shown) < len(uses):
        lines.append(f"... and {len(uses) - len(shown)} more (use --json for all)")
    return lines


def render(report: UsageReport) -> str:
    """The report as three plain-text sections."""
    lines = ["Inside sessions (from transcripts)"]
    lines += _uses(report.features, limit=_LIMIT) if report.features else ["  Nothing yet."]
    lines += ["", "How you launch claude"]
    if not report.history_enabled:
        lines.append(HISTORY_HINT)
    if report.launch_flags:
        lines += _uses(report.launch_flags, limit=_LIMIT)
    elif report.history_enabled:
        lines.append("  No launches recorded yet.")
    lines += ["", "Never tried (flags and subcommands of the installed claude)"]
    if not report.catalog_available:
        lines.append("  No catalog: the installed claude was not found, or reports no help.")
    elif not report.never_used:
        lines.append("  You have used everything it lists.")
    else:
        if not report.history_enabled:
            lines.append("  (Launch history is off, so launches vigil did not start are not seen.)")
        for entry in report.never_used:
            name = f"{entry.short}, {entry.name}" if entry.short else entry.name
            description = " ".join(entry.description.split())
            if len(description) > 70:
                description = description[:69] + "…"
            lines.append(f"  {entry.kind:<10} {name:<34} {description}".rstrip())
    return "\n".join(lines)


class UsageCommand:
    name = "usage"
    help = "Show what you use in Claude Code and what you have never tried."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--project", metavar="KEY", help="in-session usage of this project only (its key)"
        )
        parser.add_argument("--json", action="store_true", help="print JSON, as the API serves it")

    def run(self, args: argparse.Namespace, config: Config) -> int:
        conn = open_index(config)
        try:
            refresh_sources(conn, config)
            report = build_report(
                conn, project_key=args.project, history_enabled=config.usage.shell_history
            )
        finally:
            conn.close()
        if args.json:
            print(json.dumps(report_to_dict(report), indent=2))
        else:
            print(render(report))
        if args.project and not report.features:
            print(f"vigil: no usage recorded for project {args.project!r}", file=sys.stderr)
        return 0


COMMAND = UsageCommand()
