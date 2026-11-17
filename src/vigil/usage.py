"""The usage report: what you use, how you launch `claude`, and what the installed version offers
that you have never tried.

Three sources, all already in the index:

- `session_features` (from transcripts): tools, slash commands, skills, subagents, MCP servers and
  tools, plan mode and models, with a count and the last activity of a session that used them.
- `launches` (from vigil's resumes and, when enabled, shell history): subcommands and flag names.
  History stores flags as typed, so short flags are normalized here to their long names with the
  catalog (`-n` counts as `--name`).
- the catalog of the installed `claude` (`vigil.catalog`): never-used is the catalog's top-level
  flags and subcommands that no launch used.

Launches carry no project, so `launch_flags` and `never_used` are always global; only `features`
follows `project_key`.
"""

import json
import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from vigil.catalog import CatalogEntry, current_catalog, refresh_catalog, short_to_long
from vigil.config import Config
from vigil.history import import_history
from vigil.index.db import from_db, to_db


@dataclass(frozen=True)
class FeatureUse:
    """One feature and how much it was used. `kind` is a transcript feature kind (`tool`,
    `slash_command`, `skill`, ...) or, for launches, `flag` or `subcommand`."""

    kind: str
    name: str
    count: int
    last_used_at: datetime | None


@dataclass(frozen=True)
class UsageReport:
    features: list[FeatureUse] = field(default_factory=list)
    launch_flags: list[FeatureUse] = field(default_factory=list)
    never_used: list[CatalogEntry] = field(default_factory=list)
    history_enabled: bool = False
    catalog_available: bool = False
    """Whether the index holds a catalog (the installed `claude` was seen); without one,
    `never_used` is empty because nothing was compared."""


def refresh_sources(
    conn: sqlite3.Connection, config: Config, env: Mapping[str, str] | None = None
) -> None:
    """Bring what the report reads up to date: the catalog (runs `claude` only when its version
    is new; nothing happens when it is missing) and, when `usage.shell_history` is on, the
    launches in shell history (incremental; nothing is opened when it is off)."""
    refresh_catalog(conn)
    import_history(conn, config.usage, os.environ if env is None else env)


def _features(
    conn: sqlite3.Connection, project_key: str | None, since: datetime | None
) -> list[FeatureUse]:
    rows = conn.execute(
        "SELECT f.kind, f.name, SUM(f.count) AS total, MAX(s.last_activity_at) AS last "
        "FROM session_features f JOIN sessions s ON s.session_id = f.session_id "
        "WHERE (?1 IS NULL OR s.project_key = ?1) AND (?2 IS NULL OR s.last_activity_at >= ?2) "
        "GROUP BY f.kind, f.name ORDER BY total DESC, f.kind, f.name",
        (project_key, to_db(since)),
    )
    return [FeatureUse(r["kind"], r["name"], r["total"], from_db(r["last"])) for r in rows]


def _launches(
    conn: sqlite3.Connection, catalog: list[CatalogEntry], since: datetime | None
) -> tuple[list[FeatureUse], set[str], set[str]]:
    """The launch counts, plus the top-level flags and the subcommands that were used."""
    top = short_to_long(catalog)
    by_subcommand: dict[str, dict[str, str]] = {}
    for entry in catalog:
        if entry.kind == "subcommand_flag" and entry.short and entry.parent:
            by_subcommand.setdefault(entry.parent, {})[entry.short] = entry.name

    counts: dict[tuple[str, str], int] = {}
    last: dict[tuple[str, str], str] = {}
    used_flags: set[str] = set()
    used_subcommands: set[str] = set()

    def bump(kind: str, name: str, at: str) -> None:
        key = (kind, name)
        counts[key] = counts.get(key, 0) + 1
        last[key] = max(last.get(key, at), at)

    rows = conn.execute(
        "SELECT at, subcommand, flags FROM launches WHERE (?1 IS NULL OR at >= ?1)",
        (to_db(since),),
    )
    for row in rows:
        subcommand: str | None = row["subcommand"]
        aliases = by_subcommand.get(subcommand, {}) if subcommand else top
        flags = {aliases.get(flag, flag) for flag in json.loads(row["flags"])}
        if subcommand:
            used_subcommands.add(subcommand)
            bump("subcommand", subcommand, row["at"])
        for flag in sorted(flags):
            if subcommand:
                # A subcommand's flag is not the top-level flag of the same name.
                bump("flag", f"{subcommand} {flag}", row["at"])
            else:
                used_flags.add(flag)
                bump("flag", flag, row["at"])
    uses = [
        FeatureUse(kind, name, n, from_db(last[kind, name])) for (kind, name), n in counts.items()
    ]
    uses.sort(key=lambda u: (-u.count, u.kind, u.name))
    return uses, used_flags, used_subcommands


def build_report(
    conn: sqlite3.Connection,
    *,
    project_key: str | None = None,
    since: datetime | None = None,
    history_enabled: bool = False,
) -> UsageReport:
    """The usage report from the index. `project_key` limits `features` to one project and
    `since` to sessions (and launches) active at or after it. `history_enabled` is whether shell
    history import is turned on (it is config, not index, state)."""
    catalog = current_catalog(conn)
    launch_flags, used_flags, used_subcommands = _launches(conn, catalog, since)
    never_used = [
        entry
        for entry in catalog
        if (entry.kind == "flag" and entry.name not in used_flags)
        or (entry.kind == "subcommand" and entry.name not in used_subcommands)
    ]
    return UsageReport(
        features=_features(conn, project_key, since),
        launch_flags=launch_flags,
        never_used=never_used,
        history_enabled=history_enabled,
        catalog_available=bool(catalog),
    )


def feature_to_dict(use: FeatureUse) -> dict[str, Any]:
    return {
        "kind": use.kind,
        "name": use.name,
        "count": use.count,
        "last_used_at": to_db(use.last_used_at),
    }


def catalog_entry_to_dict(entry: CatalogEntry) -> dict[str, Any]:
    return {
        "kind": entry.kind,
        "name": entry.name,
        "short": entry.short,
        "parent": entry.parent,
        "description": entry.description,
    }


def report_to_dict(report: UsageReport) -> dict[str, Any]:
    """The report as the API serves it and `vigil usage --json` prints it."""
    return {
        "features": [feature_to_dict(u) for u in report.features],
        "launch_flags": [feature_to_dict(u) for u in report.launch_flags],
        "never_used": [catalog_entry_to_dict(e) for e in report.never_used],
        "history_enabled": report.history_enabled,
        "catalog_available": report.catalog_available,
    }
