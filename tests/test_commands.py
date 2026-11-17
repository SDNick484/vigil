import json
import shlex
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from tests.conftest import VigilEnv, freeze_tree
from vigil import cli
from vigil.commands.ls import format_time
from vigil.index.db import connect, from_db
from vigil.index.queries import get_session
from vigil.serialize import session_to_dict

NAMED = "e47d2def-0bb3-4d50-9f9c-8e38e4cdcfba"  # titled "fixture-named"
NEWEST = "f26f1b2c-d3b2-4389-b0ed-aba7e19e8594"
HANDMADE = "11111111-1111-4111-8111-111111111111"

# What a fake `claude` prints for `--version`, `--help` and `agents --json` alike: a version first,
# then the lines that make the capability probe see `--bg`, `attach` and `agents`.
CLAUDE_BG = "2.1.312 (Claude Code)\n  --bg  Start in the background\n  attach\n  agents\n"


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def set_cwd(env: VigilEnv, session_id: str, cwd: Path | str) -> None:
    """Point an indexed session at another folder (the fixtures' folders do not exist)."""
    conn = sqlite3.connect(env.index)
    with conn:
        conn.execute("UPDATE sessions SET cwd = ? WHERE session_id = ?", (str(cwd), session_id))
    conn.close()


def last_ids(out: str) -> list[str]:
    """The short id (last column) of each table row."""
    return [line.split()[-1] for line in out.splitlines()[1:]]


def test_scan_fills_the_index(vigil_env: VigilEnv, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "scan")
    assert code == 0
    assert "Scanned 7 transcripts" in out
    conn = connect(vigil_env.index)
    assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 7
    conn.close()
    # A second scan changes nothing.
    assert "0 sessions updated" in run(capsys, "scan")[1]


def test_commands_never_write_under_the_claude_home(
    vigil_env: VigilEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    freeze_tree(vigil_env.claude_home)
    for argv in (["scan"], ["ls"], ["show", NAMED], ["resume", NAMED, "--print"]):
        run(capsys, *argv)
    assert vigil_env.index.exists()


def test_ls_table_columns(vigil_env: VigilEnv, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "ls", "--all")
    assert code == 0
    header, *lines = out.splitlines()
    assert header.split() == ["NAME", "PROJECT", "BRANCH", "LAST", "ACTIVITY", "ID"]
    assert len(lines) == 7
    named = next(line for line in lines if line.endswith(NAMED[:8]))
    # Title when there is one, then project, branch, last activity, short id.
    assert named.startswith("fixture-named")
    assert "fixture-repo" in named
    assert "main" in named
    conn = connect(vigil_env.index)
    stored = conn.execute(
        "SELECT last_activity_at FROM sessions WHERE session_id = ?", (NAMED,)
    ).fetchone()[0]
    conn.close()
    assert format_time(from_db(stored)) in named
    # Without a name, the first prompt stands in.
    handmade = next(line for line in lines if line.endswith(HANDMADE[:8]))
    assert handmade.startswith("first prompt with bad bytes")
    assert "feature/x" in handmade
    # Newest activity first.
    assert lines[0].endswith(NEWEST[:8])


def test_ls_query_filters(vigil_env: VigilEnv, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "ls", "--all", "fixture-named")
    assert code == 0
    assert last_ids(out) == [NAMED[:8]]
    # Words are ANDed.
    assert last_ids(run(capsys, "ls", "--all", "fixture", "named")[1]) == [NAMED[:8]]
    code, out, err = run(capsys, "ls", "nothing-matches-this")
    assert (code, out) == (0, "")
    assert "No sessions found" in err
    # --project takes the project key.
    _, out, _ = run(capsys, "ls", "--all", "--project", "/home/user/src/broken-repo")
    assert last_ids(out) == [HANDMADE[:8]]


def test_ls_json_shape(vigil_env: VigilEnv, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "ls", "--all", "--json")
    assert code == 0
    sessions = json.loads(out)
    assert len(sessions) == 7
    named = next(s for s in sessions if s["id"] == NAMED)
    assert set(named) == {
        "id", "short_id", "name", "title", "label", "note", "pinned", "archived", "cwd",
        "git_branch", "version", "first_prompt", "last_user", "last_assistant", "started_at",
        "last_activity_at", "message_count", "models", "previous_id", "chain_id", "machine",
        "project_key", "project_label", "transcript_path", "status",
        "remote_url", "remote_url_at",
    }  # fmt: skip
    assert named["short_id"] == NAMED[:8]
    assert named["name"] == named["title"] == "fixture-named"
    assert named["project_label"] == "fixture-repo"
    assert named["git_branch"] == "main"
    assert named["models"] == ["claude-haiku-5-5"]
    assert named["pinned"] is False and named["archived"] is False
    assert named["status"] == "unknown"  # no claude on PATH
    for key in ("started_at", "last_activity_at"):
        assert isinstance(named[key], str)
        assert datetime.fromisoformat(str(named[key])).utcoffset() is not None
    assert run(capsys, "ls", "--json", "nothing-matches-this")[1].strip() == "[]"


def test_ls_json_status_from_live_sessions(
    vigil_env: VigilEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    vigil_env.bin.install("claude", stdout=CLAUDE_BG)
    # The fake prints the same text for every call, so `agents --json` is not JSON: none live.
    sessions = json.loads(run(capsys, "ls", "--json")[1])
    assert {s["status"] for s in sessions} == {"idle"}


def test_show_prints_resume_command(
    vigil_env: VigilEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run(capsys, "show", NAMED)
    assert code == 0
    assert f"{NAMED} ({NAMED[:8]})" in out
    assert "fixture-named" in out
    assert "Reply with exactly: named fixt" in out
    assert f"Resume:   cd /home/user/src/fixture-repo && claude --resume {NAMED}" in out
    # By unique prefix and by title too.
    assert run(capsys, "show", NAMED[:8])[1] == out
    assert run(capsys, "show", "fixture-named")[1] == out


def test_show_quotes_the_folder(vigil_env: VigilEnv, capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, "scan")
    folder = "/tmp/it's a folder"
    set_cwd(vigil_env, NAMED, folder)
    out = run(capsys, "show", NAMED)[1]
    assert f"Resume:   cd {shlex.quote(folder)} && claude --resume {NAMED}" in out
    command = out.split("Resume:   ")[1]
    assert shlex.split(command)[:2] == ["cd", folder]


def test_show_json(vigil_env: VigilEnv, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "show", NAMED, "--json")
    assert code == 0
    data = json.loads(out)
    assert data["id"] == NAMED
    assert data["resume_command"] == f"cd /home/user/src/fixture-repo && claude --resume {NAMED}"
    assert [link["id"] for link in data["chain"]] == [NAMED]
    conn = connect(vigil_env.index)
    row = get_session(conn, NAMED)
    conn.close()
    assert row is not None
    expected = session_to_dict(row)
    assert {key: data[key] for key in expected} == expected


def test_unknown_id_exit_1(vigil_env: VigilEnv, capsys: pytest.CaptureFixture[str]) -> None:
    for command in ("show", "resume"):
        code, out, err = run(capsys, command, "no-such-session")
        assert (code, out) == (1, "")
        assert "no session matches 'no-such-session'" in err


def test_resume_print_does_not_launch(
    vigil_env: VigilEnv, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    folder = tmp_path / "work"
    folder.mkdir()
    run(capsys, "scan")
    set_cwd(vigil_env, NAMED, folder)
    vigil_env.bin.install("claude", stdout=CLAUDE_BG)
    code, out, _ = run(capsys, "resume", NAMED, "--print")
    assert code == 0
    assert f"cd {folder} && claude --bg --resume {NAMED}" in out
    assert f"claude attach {NAMED}" in out
    # Only capability probes ran: nothing started.
    assert all("--resume" not in call.argv for call in vigil_env.bin.calls("claude"))
    conn = connect(vigil_env.index)
    assert conn.execute("SELECT count(*) FROM launches").fetchone()[0] == 0
    conn.close()


def test_resume_launches_in_the_folder_and_records_it(
    vigil_env: VigilEnv, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    folder = tmp_path / "work"
    folder.mkdir()
    run(capsys, "scan")
    set_cwd(vigil_env, NAMED, folder)
    vigil_env.bin.install("claude", stdout=CLAUDE_BG)
    code, out, _ = run(capsys, "resume", NAMED)
    assert code == 0
    assert f"claude attach {NAMED}" in out
    assert "No terminal available" in out
    started = [c for c in vigil_env.bin.calls("claude") if "--resume" in c.argv]
    assert [(c.argv, c.cwd) for c in started] == [
        (["--bg", "--resume", NAMED, "--remote-control"], str(folder))
    ]
    conn = connect(vigil_env.index)
    rows = conn.execute("SELECT source, session_id FROM launches").fetchall()
    conn.close()
    assert [tuple(row) for row in rows] == [("vigil", NAMED)]


def test_resume_refusal_exit_code_2_and_message(
    vigil_env: VigilEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    vigil_env.bin.install("claude", stdout=CLAUDE_BG)
    code, out, err = run(capsys, "resume", NAMED)
    assert (code, out) == (2, "")
    assert err == "vigil: folder no longer exists: /home/user/src/fixture-repo\n"
    assert all("--resume" not in call.argv for call in vigil_env.bin.calls("claude"))


def test_resume_without_claude_is_refused(
    vigil_env: VigilEnv, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    run(capsys, "scan")
    set_cwd(vigil_env, NAMED, tmp_path)
    code, _, err = run(capsys, "resume", NAMED)
    assert code == 2
    assert "`claude` is not on PATH" in err
