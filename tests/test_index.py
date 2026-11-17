import json
import sqlite3
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.conftest import FIXTURE_PROJECT_DIR
from vigil.index import db
from vigil.index.db import connect
from vigil.index.queries import (
    ProjectRow,
    SessionRow,
    chain,
    get_session,
    list_projects,
    list_sessions,
    record_launch,
    set_meta,
)
from vigil.index.scanner import scan
from vigil.projects import normalize_remote, project_for

PLAIN = "7fb815ab-28b5-4123-85d0-64e6fd3b4141"
FORKED = "68e04fa1-b95c-4610-8221-261395d0ca5d"
NAMED = "e47d2def-0bb3-4d50-9f9c-8e38e4cdcfba"
COMMAND = "c8232e85-1b73-42a7-b6af-192de6b33a26"
PLAN = "f26f1b2c-d3b2-4389-b0ed-aba7e19e8594"
HANDMADE = "11111111-1111-4111-8111-111111111111"
FIXTURE_CWD = "/home/user/src/fixture-repo"
BROKEN_CWD = "/home/user/src/broken-repo"


def ids(rows: Sequence[SessionRow | None]) -> list[str]:
    return [row.session_id if row is not None else "<none>" for row in rows]


def test_connect_applies_migrations_once(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "index.db"
    first = connect(path)
    first.close()
    conn = connect(path)
    try:
        applied = conn.execute("SELECT version, name FROM schema_migrations").fetchall()
        assert [tuple(r) for r in applied] == [(v, name) for v, name, _ in db.migrations()]
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"sessions", "session_models", "session_features", "file_state"} <= tables
        assert {"session_meta", "launches"} <= tables
    finally:
        conn.close()


def test_migrations_are_numbered_in_order() -> None:
    found = db.migrations()
    assert [version for version, _, _ in found] == sorted({v for v, _, _ in found})
    assert found[0][:2] == (1, "0001_core.sql")


def test_failed_migration_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    good = db.migrations()
    bad = [*good, (9999, "9999_broken.sql", "CREATE TABLE half (x);\nNOT SQL;")]
    monkeypatch.setattr(db, "migrations", lambda: bad)
    with pytest.raises(sqlite3.OperationalError):
        connect(tmp_path / "index.db")
    monkeypatch.setattr(db, "migrations", lambda: good)
    conn = connect(tmp_path / "index.db")
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "half" not in tables
        applied = [r[0] for r in conn.execute("SELECT version FROM schema_migrations")]
        assert applied == [version for version, _, _ in good]
    finally:
        conn.close()


def test_each_session_is_its_own_chain_without_links(index_db: sqlite3.Connection) -> None:
    rows = list_sessions(index_db, collapse_chains=False)
    assert len(rows) == 7
    assert all(row.chain_id == row.session_id for row in rows)
    assert all(row.previous_session_id is None for row in rows)
    assert ids(list_sessions(index_db)) == ids(rows)


def test_chain_collapsed_in_list(index_db: sqlite3.Connection, claude_home: Path) -> None:
    # 2.1.312 transcripts carry no previous-session link (see tests/fixtures/README.md), so the
    # link the parser would report is set directly; the scanner then derives the chain.
    with index_db:
        index_db.execute(
            "UPDATE sessions SET previous_session_id = ? WHERE session_id = ?", (PLAIN, FORKED)
        )
    scan(index_db, claude_home)

    plain, forked = get_session(index_db, PLAIN), get_session(index_db, FORKED)
    assert plain is not None and forked is not None
    assert plain.chain_id == forked.chain_id == PLAIN
    assert ids(chain(index_db, PLAIN)) == [PLAIN, FORKED]  # oldest first

    collapsed = ids(list_sessions(index_db))
    assert FORKED in collapsed  # the newest of the chain is the one shown
    assert PLAIN not in collapsed
    assert len(collapsed) == 6
    assert {PLAIN, FORKED} <= set(ids(list_sessions(index_db, collapse_chains=False)))
    # A search hitting only an older member still shows the chain, by its newest session.
    set_meta(index_db, PLAIN, note="original run")
    assert ids(list_sessions(index_db, query="original run")) == [FORKED]
    assert ids(list_sessions(index_db, query="original run", collapse_chains=False)) == [PLAIN]


def test_list_is_newest_activity_first(index_db: sqlite3.Connection) -> None:
    rows = list_sessions(index_db, collapse_chains=False)
    times = [row.last_activity_at for row in rows]
    assert all(t is not None for t in times)
    assert times == sorted(times, reverse=True)  # type: ignore[type-var]
    assert rows[0].session_id == PLAN
    assert rows[-1].session_id == HANDMADE
    assert len(list_sessions(index_db, limit=2)) == 2


def test_search_matches_title_note_prompt_cwd(index_db: sqlite3.Connection) -> None:
    assert ids(list_sessions(index_db, query="fixture-named")) == [NAMED]  # title
    assert ids(list_sessions(index_db, query="/greet")) == [COMMAND]  # first prompt
    assert ids(list_sessions(index_db, query="broken-repo")) == [HANDMADE]  # cwd
    assert len(list_sessions(index_db, query="FIXTURE-REPO")) == 6  # cwd, any case
    set_meta(index_db, PLAN, note="next: write the LICENSE")
    set_meta(index_db, COMMAND, label="greeter")
    assert ids(list_sessions(index_db, query="next: write")) == [PLAN]  # note
    assert ids(list_sessions(index_db, query="Greeter")) == [COMMAND]  # label
    assert ids(list_sessions(index_db, query="hello nothing-like-this")) == []  # every word
    assert list_sessions(index_db, query="%") == []  # LIKE wildcards are literal


def test_filters_project_since_and_archived(index_db: sqlite3.Connection) -> None:
    assert ids(list_sessions(index_db, project_key=BROKEN_CWD)) == [HANDMADE]
    since = datetime(2026, 11, 17, 15, 8, 0, tzinfo=UTC)
    assert set(ids(list_sessions(index_db, since=since))) == {
        COMMAND,
        PLAN,
        "7bf4d32d-3526-48a2-9532-46c8ec375e87",
    }
    set_meta(index_db, PLAN, archived=True)
    assert PLAN not in ids(list_sessions(index_db))
    assert PLAN in ids(list_sessions(index_db, include_archived=True))
    archived = get_session(index_db, PLAN)
    assert archived is not None and archived.archived


def test_meta_survives_rescan(index_db: sqlite3.Connection, claude_home: Path) -> None:
    set_meta(index_db, PLAIN, label="first", pinned=True, note="next: tidy up")
    set_meta(index_db, PLAIN, note=None)  # only the given field changes
    path = claude_home / "projects" / FIXTURE_PROJECT_DIR / f"{PLAIN}.jsonl"
    record = {"type": "user", "sessionId": PLAIN, "message": {"role": "user", "content": "more"}}
    with path.open("a") as handle:
        handle.write(json.dumps(record) + "\n")
    assert scan(index_db, claude_home).sessions_updated == 1

    row = get_session(index_db, PLAIN)
    assert row is not None
    assert (row.label, row.pinned, row.archived, row.note) == ("first", True, False, None)
    assert row.last_user == "more"
    meta = index_db.execute("SELECT * FROM session_meta WHERE session_id = ?", (PLAIN,)).fetchone()
    assert meta["updated_at"] is not None


def test_session_row_fields(index_db: sqlite3.Connection) -> None:
    row = get_session(index_db, PLAIN)
    assert row is not None
    assert row.cwd == FIXTURE_CWD
    assert row.project_key == FIXTURE_CWD  # the folder does not exist: no git remote
    assert row.project_label == "fixture-repo"
    assert row.models == ("claude-haiku-5-5", "claude-sonnet-5-5")
    assert row.started_at == datetime(2026, 11, 17, 15, 7, 10, 176000, tzinfo=UTC)
    assert row.status == "unknown"
    assert row.transcript_path.endswith(f"{FIXTURE_PROJECT_DIR}/{PLAIN}.jsonl")
    assert row.machine


def test_get_session_by_prefix_and_title(index_db: sqlite3.Connection) -> None:
    assert ids([get_session(index_db, NAMED)]) == [NAMED]
    assert ids([get_session(index_db, "e47d")]) == [NAMED]
    assert ids([get_session(index_db, "fixture-named")]) == [NAMED]
    assert get_session(index_db, "7") is None  # 7bf4… and 7fb8…: ambiguous
    assert get_session(index_db, "nope") is None
    assert get_session(index_db, "") is None
    assert get_session(index_db, "%") is None


def test_list_projects(index_db: sqlite3.Connection) -> None:
    assert list_projects(index_db) == [
        ProjectRow(
            project_key=FIXTURE_CWD,
            label="fixture-repo",
            session_count=6,
            last_activity_at=datetime(2026, 11, 17, 15, 8, 21, 286000, tzinfo=UTC),
        ),
        ProjectRow(
            project_key=BROKEN_CWD,
            label="broken-repo",
            session_count=1,
            last_activity_at=datetime(2026, 11, 17, 10, 0, 3, tzinfo=UTC),
        ),
    ]


def test_record_launch(index_db: sqlite3.Connection) -> None:
    at = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
    record_launch(
        index_db,
        source="vigil",
        subcommand=None,
        flags=["--resume", "--bg"],
        at=at,
        session_id=PLAIN,
    )
    record_launch(index_db, source="shell", subcommand="agents", flags=[], at=at, session_id=None)
    rows = index_db.execute("SELECT * FROM launches ORDER BY id").fetchall()
    assert [
        (r["source"], r["subcommand"], json.loads(r["flags"]), r["session_id"]) for r in rows
    ] == [
        ("vigil", None, ["--resume", "--bg"], PLAIN),
        ("shell", "agents", [], None),
    ]
    assert rows[0]["at"] == "2026-11-17T12:00:00.000000+00:00"
    with pytest.raises(sqlite3.IntegrityError):
        record_launch(index_db, source="other", subcommand=None, flags=[], at=at, session_id=None)  # type: ignore[arg-type]


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def test_project_key_from_git_remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    repo = tmp_path / "checkout"
    (repo / "sub" / "dir").mkdir(parents=True)
    git(repo, "init", "-q")
    git(
        repo, "remote", "add", "origin", "https://someone:secret@Example.com/owner/fixture-repo.git"
    )
    assert project_for(str(repo)) == ("example.com/owner/fixture-repo", "fixture-repo")
    # Any folder inside the work tree belongs to the same project.
    assert project_for(str(repo / "sub" / "dir")) == (
        "example.com/owner/fixture-repo",
        "fixture-repo",
    )


def test_project_key_falls_back_to_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    plain = tmp_path / "plain-folder"
    plain.mkdir()
    assert project_for(str(plain)) == (str(plain), "plain-folder")
    no_remote = tmp_path / "no-remote"
    no_remote.mkdir()
    git(no_remote, "init", "-q")
    assert project_for(str(no_remote)) == (str(no_remote), "no-remote")
    missing = tmp_path / "gone" / "away"
    assert project_for(str(missing)) == (str(missing), "away")
    assert project_for(None) == ("", "(unknown)")


def test_project_key_without_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    folder = tmp_path / "no-git-on-path"
    folder.mkdir()
    assert project_for(str(folder)) == (str(folder), "no-git-on-path")


@pytest.mark.parametrize(
    ("url", "key"),
    [
        ("https://github.com/owner/repo.git", "github.com/owner/repo"),
        ("https://user:token@github.com/owner/repo", "github.com/owner/repo"),
        ("git@github.com:owner/repo.git", "github.com/owner/repo"),
        ("ssh://git@github.com:22/owner/repo.git/", "github.com/owner/repo"),
        ("ssh://git@git.example.com:2222/owner/repo.git", "git.example.com:2222/owner/repo"),
        ("/srv/git/repo.git", "/srv/git/repo"),
        ("file:///srv/git/repo.git", "/srv/git/repo"),
    ],
)
def test_normalize_remote(url: str, key: str) -> None:
    assert normalize_remote(url) == key
