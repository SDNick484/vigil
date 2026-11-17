import subprocess
from pathlib import Path

import pytest

from tests.conftest import FakeBin, freeze_tree, thaw_tree


def test_fake_bin_records_argv_and_cwd(fake_bin: FakeBin, tmp_path: Path) -> None:
    fake_bin.install("claude", stdout="hello\n", exit_code=3)
    result = subprocess.run(["claude", "--bg", "x"], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 3
    assert result.stdout == "hello\n"
    [call] = fake_bin.calls("claude")
    assert call.argv == ["--bg", "x"]
    assert Path(call.cwd).resolve() == tmp_path.resolve()


def test_fake_bin_missing_executable_is_missing(fake_bin: FakeBin) -> None:
    with pytest.raises(FileNotFoundError):
        subprocess.run(["claude", "--version"], capture_output=True)
    assert fake_bin.calls("claude") == []


def test_claude_home_can_be_frozen(claude_home: Path) -> None:
    (claude_home / "projects").mkdir()
    freeze_tree(claude_home)
    with pytest.raises(PermissionError):
        (claude_home / "projects" / "x.jsonl").write_text("x")
    thaw_tree(claude_home)
