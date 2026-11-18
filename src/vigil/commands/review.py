"""`vigil review ID [--json]`: have Claude review a session, wait for it, and print it.

Acting on the suggestions (resume or start with a flag, change a project file) is left to the
web UI, where each action is shown before it is taken.
"""

import argparse
import json
import shlex
import sys

from vigil.catalog import CatalogEntry, current_catalog, refresh_catalog
from vigil.claude_cli import capabilities
from vigil.commands import find_session, open_index
from vigil.config import Config
from vigil.flags import FlagRefused, check, parse_suggested
from vigil.review.runner import ReviewRefused, busy_review, run_review
from vigil.review.schema import Suggestion
from vigil.review.store import Review, get_review, review_to_dict

NOTE = "Suggestions come from Claude reading this session. Check them before acting."


def _flag_line(review: Review, item: Suggestion, catalog: list[CatalogEntry]) -> str:
    """The command line to try a flag suggestion with, if the flag allow-list lets it through
    (against the installed version's flags, when known); otherwise why not."""
    assert item.flag_name is not None
    try:
        flags = check(parse_suggested(item.flag_name, item.flag_value), catalog or None)
    except FlagRefused as refused:
        return f"   vigil will not suggest running this flag: {refused.reason}"
    return f"   Try: {shlex.join(['claude', '--resume', review.session_id, *flags])}"


def render(review: Review, catalog: list[CatalogEntry] | None = None) -> str:
    """A review as text: status and cost, then each suggestion (for a flag, the command line to
    try it with, only when the flag allow-list passes it against `catalog`), then the token
    advice."""
    cost = f", ${review.cost_usd:.2f}" if review.cost_usd is not None else ""
    lines = [f"Review {review.id} of {review.session_id}: {review.status}{cost}"]
    if review.error and review.status == "done":
        lines.append(f"Note: {review.error}")
    if review.status != "done":
        return "\n".join(lines)
    lines += [NOTE, ""]
    for number, item in enumerate(review.suggestions, start=1):
        lines.append(f"{number}. [{item.kind}] {item.title}")
        lines.append(f"   {item.why}")
        if item.evidence:
            lines.append(f"   Evidence: {item.evidence}")
        if item.flag_name:
            lines.append(_flag_line(review, item, catalog or []))
        if item.file_target:
            lines.append(f"   File: {item.file_target}")
            body = (
                json.dumps(item.file_settings, indent=2)
                if item.file_settings is not None
                else item.file_text or ""
            )
            lines += [f"     {line}" for line in body.splitlines()]
    if not review.suggestions:
        lines.append("No suggestions.")
    if review.advice:
        lines += ["", "Token advice:"]
        lines += [f"- {point.title}: {point.why}" for point in review.advice]
    return "\n".join(lines)


class ReviewCommand:
    name = "review"
    help = "Have Claude review a session (a fork that is not saved) and print its suggestions."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("id", metavar="ID", help="session id, a unique id prefix, or its title")
        parser.add_argument("--json", action="store_true", help="print JSON, as the API serves it")

    def run(self, args: argparse.Namespace, config: Config) -> int:
        conn = open_index(config)
        try:
            row = find_session(conn, args.id)
            if row is None:
                return 1
            running = busy_review(conn)
            if running is not None:
                print(f"vigil: a review is already running (review {running.id})", file=sys.stderr)
                return 2
            refresh_catalog(conn)
            catalog = current_catalog(conn)
            try:
                review_id = run_review(
                    row,
                    config.review,
                    catalog,
                    config.resume.default_flags,
                    conn,
                    claude_version=capabilities().version,
                )
            except ReviewRefused as refused:
                print(f"vigil: {refused.reason}", file=sys.stderr)
                return 2
            review = get_review(conn, review_id)
        finally:
            conn.close()
        if review is None:  # removed by a concurrent rescan meanwhile
            print("vigil: the review's session left the index", file=sys.stderr)
            return 1
        print(
            json.dumps(review_to_dict(review), indent=2) if args.json else render(review, catalog)
        )
        if review.status == "failed":
            print(f"vigil: the review failed: {review.error}", file=sys.stderr)
            return 1
        return 0


COMMAND = ReviewCommand()
