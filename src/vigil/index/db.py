"""The SQLite index: connecting, migrations and timestamp encoding.

Migrations are the files `migrations/NNNN_<name>.sql` in this package, applied in number order,
each in its own transaction, and recorded in `schema_migrations`. A migration is never edited once
released; a change is a new file.
"""

import json
import re
import sqlite3
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def connect(path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the index at `path`, bring its schema up to date, and return a
    connection in WAL mode with `sqlite3.Row` rows and foreign keys enforced.

    The connection uses the standard library's default transaction handling: use `with conn:` to
    commit a group of writes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    _migrate(path)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def migrations() -> list[tuple[int, str, str]]:
    """Every bundled migration as `(version, file name, sql)`, in version order."""
    found: list[tuple[int, str, str]] = []
    for entry in resources.files("vigil.index").joinpath("migrations").iterdir():
        match = _MIGRATION_NAME.match(entry.name)
        if match:
            found.append((int(match.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    found.sort()
    versions = [version for version, _, _ in found]
    if len(versions) != len(set(versions)):
        raise RuntimeError(f"duplicate migration numbers in {[name for _, name, _ in found]}")
    return found


def _migrate(path: Path) -> None:
    # A separate autocommit connection, so each migration's BEGIN/COMMIT is ours alone and
    # `executescript` does no implicit transaction handling. BEGIN IMMEDIATE takes the write lock
    # before reading what is applied, so two processes starting together cannot both apply one.
    conn = sqlite3.connect(path, timeout=10, autocommit=True)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        for version, name, sql in migrations():
            conn.execute("BEGIN IMMEDIATE")
            try:
                done = conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
                ).fetchone()
                if done is None:
                    conn.executescript(sql)
                    conn.execute(
                        "INSERT INTO schema_migrations (version, name, applied_at) "
                        "VALUES (?, ?, ?)",
                        (version, name, to_db(datetime.now(UTC))),
                    )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
    finally:
        conn.close()


def to_db(at: datetime | None) -> str | None:
    """Encode a timestamp for the index: UTC, ISO 8601, microseconds, so text order is time order.
    A naive datetime is taken to be UTC."""
    if at is None:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return at.astimezone(UTC).isoformat(timespec="microseconds")


def from_db(value: str | None) -> datetime | None:
    """Decode a timestamp written by `to_db`."""
    return datetime.fromisoformat(value) if value is not None else None


def urls_from_db(value: str | None) -> list[str]:
    """Decode the `remote_urls` column (a JSON array of strings); anything else is empty."""
    try:
        data = json.loads(value) if value else []
    except ValueError:
        return []
    return [item for item in data if isinstance(item, str)] if isinstance(data, list) else []
