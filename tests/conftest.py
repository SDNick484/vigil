"""Shared fixtures. Append new fixtures at the end of this file."""

import json
import stat
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

_STUB = """#!{python}
import json, os, sys
with open({log!r}, "a") as handle:
    handle.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd()}}) + "\\n")
sys.stdout.write({stdout!r})
sys.exit({exit_code})
"""


def freeze_tree(root: Path) -> None:
    """Make a tree read-only, so a test can prove vigil never writes under it."""
    for path in [root, *root.rglob("*")]:
        if not path.is_symlink():
            path.chmod(path.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def thaw_tree(root: Path) -> None:
    """Undo freeze_tree so the tree can be changed or cleaned up."""
    for path in [root, *root.rglob("*")]:
        if not path.is_symlink():
            path.chmod(path.stat().st_mode | stat.S_IWUSR)


@pytest.fixture
def claude_home(tmp_path: Path) -> Iterator[Path]:
    """An empty Claude home. Populate it, then call freeze_tree() to make it read-only.

    The tree is made writable again at teardown so pytest can clean it up.
    """
    home = tmp_path / "claude_home"
    home.mkdir()
    yield home
    thaw_tree(home)


@dataclass(frozen=True)
class Call:
    argv: list[str]
    cwd: str


class FakeBin:
    """Recording stand-ins for external programs, installed on PATH."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._logs = directory / ".calls"
        self._logs.mkdir(parents=True, exist_ok=True)

    def install(self, name: str, stdout: str = "", exit_code: int = 0) -> Path:
        """Install an executable `name` that records argv and cwd, prints stdout, exits."""
        log = self._logs / f"{name}.jsonl"
        script = self.directory / name
        script.write_text(
            _STUB.format(python=sys.executable, log=str(log), stdout=stdout, exit_code=exit_code)
        )
        script.chmod(0o755)
        return script

    def calls(self, name: str) -> list[Call]:
        """Every recorded invocation of `name`, in order."""
        log = self._logs / f"{name}.jsonl"
        if not log.exists():
            return []
        lines = log.read_text().splitlines()
        return [Call(argv=entry["argv"], cwd=entry["cwd"]) for entry in map(json.loads, lines)]


@pytest.fixture
def fake_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeBin:
    """Put recording fakes on PATH. PATH is only the fake directory, so no real `claude`,
    `tmux` or terminal can ever be launched; an executable that is not installed is missing."""
    directory = tmp_path / "fakebin"
    directory.mkdir()
    monkeypatch.setenv("PATH", str(directory))
    return FakeBin(directory)


# Imported here rather than at the top so this file stays append-only.
import shutil  # noqa: E402
import sqlite3  # noqa: E402

from vigil.index.db import connect  # noqa: E402
from vigil.index.scanner import scan  # noqa: E402

FIXTURE_TRANSCRIPTS = Path(__file__).parent / "fixtures" / "transcripts"
FIXTURE_PROJECT_DIR = "-home-user-src-fixture-repo"
HANDMADE_PROJECT_DIR = "-home-user-src-broken-repo"


def install_fixture_transcripts(claude_home: Path) -> Path:
    """Copy the fixture transcripts into `claude_home/projects/` in Claude Code's layout (the
    real ones, with their subagent folder, in one project folder; the hand-made one in another).
    Returns the `projects` folder."""
    projects = claude_home / "projects"
    shutil.copytree(FIXTURE_TRANSCRIPTS / "2.1.312", projects / FIXTURE_PROJECT_DIR)
    shutil.copytree(FIXTURE_TRANSCRIPTS / "handmade", projects / HANDMADE_PROJECT_DIR)
    return projects


@pytest.fixture
def index_db(tmp_path: Path, claude_home: Path) -> Iterator[sqlite3.Connection]:
    """An index at `tmp_path/data/index.db` built by scanning the fixture transcripts, which are
    installed in the `claude_home` fixture (left writable; call freeze_tree() to lock it)."""
    install_fixture_transcripts(claude_home)
    conn = connect(tmp_path / "data" / "index.db")
    scan(conn, claude_home)
    yield conn
    conn.close()


@pytest.fixture
def proc_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point WSL detection at a fake `/proc/version` (a plain Linux kernel string), so tests never
    depend on the real machine. Overwrite the file to simulate WSL."""
    import vigil.terminal

    path = tmp_path / "proc_version"
    path.write_text("Linux version 6.0.0 (builder@example) (gcc) #1 SMP\n")
    monkeypatch.setattr(vigil.terminal, "PROC_VERSION", path)
    return path
