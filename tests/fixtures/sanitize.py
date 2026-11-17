"""Sanitize real Claude Code transcripts into public test fixtures.

Usage (see tests/fixtures/README.md for the exact run):

    python tests/fixtures/sanitize.py RAW_DIR OUT_DIR --replace OLD=NEW [--replace OLD=NEW ...]

Every `.jsonl` (and `.json`) file under RAW_DIR is copied to the same relative path under OUT_DIR
with these changes:

- Each `--replace` pair is applied to every string, longest OLD first. For a pair that looks like a
  path, the Claude Code project-directory encoding of it (every non-alphanumeric character turned
  into `-`) is replaced too. Then `$HOME` becomes `/home/user`, the login name becomes `user`, the
  host name becomes `host`, and any e-mail address other than `noreply@anthropic.com` becomes
  `user@example.com`.
- Bulky or account-specific payloads (system prompt snapshots, API request shapes and blobs, skill
  and agent listings, loaded CLAUDE.md files, session context, credential and environment
  attachments) keep their record type and keys, but every string inside them becomes
  `<redacted>`. Conversation records (`user`, `assistant`) and small bookkeeping records are kept.

Lines that are not valid JSON are copied unchanged after the string replacements, so a raw file
that already contains a malformed line keeps it.
"""

import argparse
import getpass
import json
import os
import re
import socket
from pathlib import Path
from typing import Any

REDACTED = "<redacted>"
# Attachment types whose content is kept. Everything else is redacted (strings only).
KEEP_ATTACHMENTS = frozenset(
    {
        "budget_usd",
        "command_permissions",
        "date",
        "deferred_tools_delta",
        "deferred_tools_record",
        "model",
        "plan_mode",
        "total_tokens_reminder",
    }
)
# Fields of non-attachment records whose content is redacted.
REDACT_FIELDS = {"api-request-shape": ("shape",), "api-request-blob": ("message",)}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
KEEP_EMAILS = frozenset({"noreply@anthropic.com", "user@example.com"})


def encode_path(path: str) -> str:
    """Claude Code's project-directory name for a path."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def build_pairs(explicit: list[tuple[str, str]]) -> list[tuple[str, str]]:
    pairs = list(explicit)
    for old, new in explicit:
        if old.startswith("/"):
            pairs.append((encode_path(old), encode_path(new)))
    home = os.path.expanduser("~")
    pairs += [(home, "/home/user"), (encode_path(home), encode_path("/home/user"))]
    pairs.append((getpass.getuser(), "user"))
    host = socket.gethostname()
    if host and host != "localhost":
        pairs.append((host, "host"))
    # Longest first, so a path is replaced before a name inside it.
    return sorted(pairs, key=lambda pair: len(pair[0]), reverse=True)


def scrub_text(text: str, pairs: list[tuple[str, str]]) -> str:
    for old, new in pairs:
        text = text.replace(old, new)
    return EMAIL.sub(
        lambda m: m.group(0) if m.group(0) in KEEP_EMAILS else "user@example.com", text
    )


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return REDACTED
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {key: (item if key == "type" else redact(item)) for key, item in value.items()}
    return value


def scrub(value: Any, pairs: list[tuple[str, str]]) -> Any:
    if isinstance(value, str):
        return scrub_text(value, pairs)
    if isinstance(value, list):
        return [scrub(item, pairs) for item in value]
    if isinstance(value, dict):
        return {scrub_text(key, pairs): scrub(item, pairs) for key, item in value.items()}
    return value


def sanitize_record(record: dict[str, Any]) -> dict[str, Any]:
    kind = record.get("type")
    if kind == "attachment":
        attachment = record.get("attachment")
        if isinstance(attachment, dict) and attachment.get("type") not in KEEP_ATTACHMENTS:
            record["attachment"] = redact(attachment)
            if "rendered" in record:
                record["rendered"] = redact(record["rendered"])
    for field in REDACT_FIELDS.get(str(kind), ()):
        if field in record:
            record[field] = redact(record[field])
    return record


def sanitize_line(line: str, pairs: list[tuple[str, str]]) -> str:
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return scrub_text(line, pairs)
    if isinstance(record, dict):
        record = sanitize_record(record)
    return json.dumps(scrub(record, pairs), ensure_ascii=False, separators=(",", ":"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("raw", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--replace", action="append", default=[], metavar="OLD=NEW")
    args = parser.parse_args()
    pairs = build_pairs([tuple(item.split("=", 1)) for item in args.replace])
    for source in sorted(args.raw.rglob("*")):
        if source.suffix not in {".jsonl", ".json"} or not source.is_file():
            continue
        target = args.out / scrub_text(str(source.relative_to(args.raw)), pairs)
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = source.read_text(encoding="utf-8").splitlines()
        target.write_text("".join(sanitize_line(line, pairs) + "\n" for line in lines))


if __name__ == "__main__":
    main()
