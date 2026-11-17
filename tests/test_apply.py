import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

from tests.conftest import freeze_tree, thaw_tree
from vigil.apply import (
    ApplyRefused,
    apply_change,
    diff_text,
    plan_change,
    project_folder,
)
from vigil.review.schema import FileTarget, Suggestion

CHANGED = "the file changed since the preview; show the change again"


def file_card(
    target: FileTarget, text: str | None = None, settings: dict[str, Any] | None = None
) -> Suggestion:
    return Suggestion(
        kind="file",
        title="Change a file",
        why="why",
        evidence="turn 3",
        flag_name=None,
        flag_value=None,
        file_target=target,
        file_text=text,
        file_settings=settings,
    )


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    return path


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "claude-home"
    path.mkdir()
    return path


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_claude_md_appended(folder: Path, home: Path) -> None:
    existing = "# Project\n\nUse uv.\n\n\n"
    (folder / "CLAUDE.md").write_text(existing)
    change = plan_change(file_card("project_claude_md", "  Run the tests first.\n"), folder, home)
    assert change.target == folder.resolve() / "CLAUDE.md"
    assert change.before == existing
    assert change.after == "# Project\n\nUse uv.\n\nRun the tests first.\n"
    assert change.before_hash == sha(existing.encode())
    assert change.skipped == []
    diff = diff_text(change)
    assert "+Run the tests first." in diff
    assert "-Use uv." not in diff
    # Planning wrote nothing.
    assert (folder / "CLAUDE.md").read_text() == existing

    apply_change(change, home)
    assert (folder / "CLAUDE.md").read_text() == change.after
    assert [p.name for p in folder.iterdir()] == ["CLAUDE.md"]  # no temp file left


def test_claude_md_created(folder: Path, home: Path) -> None:
    change = plan_change(file_card("project_claude_md", "\nRun the tests first.\n\n"), folder, home)
    assert change.before is None
    assert change.before_hash == "absent"
    assert change.after == "Run the tests first.\n"
    assert "/dev/null" in diff_text(change)
    assert not (folder / "CLAUDE.md").exists()

    apply_change(change, home)
    assert (folder / "CLAUDE.md").read_text() == "Run the tests first.\n"


def test_settings_merge_union_and_skip(folder: Path, home: Path) -> None:
    (folder / ".claude").mkdir()
    settings = folder / ".claude" / "settings.json"
    existing = {"model": "sonnet", "permissions": {"deny": ["Bash(rm:*)"]}, "other": 1}
    settings.write_text(json.dumps(existing))
    settings.chmod(0o600)
    suggestion = file_card(
        "project_settings",
        settings={
            "model": "opus",
            "permissions": {"deny": ["WebFetch", "Bash(rm:*)"], "ask": ["Bash(git push:*)"]},
            "autoCompactEnabled": False,
        },
    )
    change = plan_change(suggestion, folder, home)
    assert change.skipped == ["model"]
    assert change.current == {"model": "sonnet"}
    merged = json.loads(change.after)
    assert merged == {
        "model": "sonnet",
        "permissions": {"deny": ["Bash(rm:*)", "WebFetch"], "ask": ["Bash(git push:*)"]},
        "other": 1,
        "autoCompactEnabled": False,
    }
    assert change.after == json.dumps(merged, indent=2) + "\n"

    apply_change(change, home)
    assert settings.read_text() == change.after
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600  # the file's mode is kept


@pytest.mark.parametrize(
    "settings",
    [
        {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}]}},
        {"model": "opus", "hooks": {}},
        {"env": {"ANTHROPIC_BASE_URL": "https://example.invalid"}},
        {"statusLine": {"type": "command", "command": "sh"}},
        {"apiKeyHelper": "sh"},
    ],
)
def test_settings_hooks_refused(folder: Path, home: Path, settings: dict[str, Any]) -> None:
    with pytest.raises(ApplyRefused) as refused:
        plan_change(file_card("project_settings", settings=settings), folder, home)
    (key,) = [k for k in settings if k != "model"]
    assert key in refused.value.reason
    assert not (folder / ".claude").exists()


@pytest.mark.parametrize(
    ("settings", "key"),
    [
        ({"permissions": {"allow": ["Bash(*)"], "deny": ["WebFetch"]}}, "permissions.allow"),
        ({"permissions": {"defaultMode": "bypassPermissions"}}, "permissions.defaultMode"),
        # A dotted key is one key, not a path: it is not `permissions.deny`.
        ({"permissions.deny": ["WebFetch"]}, "permissions.deny"),
        ({"permissions": {}}, "permissions"),
    ],
)
def test_settings_permissions_allow_refused(
    folder: Path, home: Path, settings: dict[str, Any], key: str
) -> None:
    with pytest.raises(ApplyRefused) as refused:
        plan_change(file_card("project_settings", settings=settings), folder, home)
    assert key in refused.value.reason


@pytest.mark.parametrize("existing", ["{not json", "[1, 2]", ""])
def test_settings_invalid_json_refused(folder: Path, home: Path, existing: str) -> None:
    (folder / ".claude").mkdir()
    settings = folder / ".claude" / "settings.json"
    settings.write_text(existing)
    with pytest.raises(ApplyRefused):
        plan_change(file_card("project_settings", settings={"model": "opus"}), folder, home)
    assert settings.read_text() == existing


def test_settings_not_an_object_refused(folder: Path, home: Path) -> None:
    suggestion = file_card("project_settings", settings=cast(dict[str, Any], ["model"]))
    with pytest.raises(ApplyRefused):
        plan_change(suggestion, folder, home)


def test_user_targets_refused(folder: Path, home: Path) -> None:
    for suggestion in (
        file_card("user_claude_md", "Be brief."),
        file_card("user_settings", settings={"model": "opus"}),
    ):
        with pytest.raises(ApplyRefused):
            plan_change(suggestion, folder, home)


def test_apply_refuses_symlink_out(folder: Path, home: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "CLAUDE.md").write_text("elsewhere\n")
    (folder / "CLAUDE.md").symlink_to(outside / "CLAUDE.md")
    with pytest.raises(ApplyRefused) as refused:
        plan_change(file_card("project_claude_md", "More."), folder, home)
    assert "outside the project folder" in refused.value.reason

    # A dangling link out is refused too.
    (folder / "CLAUDE.md").unlink()
    (folder / "CLAUDE.md").symlink_to(outside / "missing.md")
    with pytest.raises(ApplyRefused):
        plan_change(file_card("project_claude_md", "More."), folder, home)

    # So is a `.claude` folder that is a link out (the settings file is missing there).
    (folder / ".claude").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ApplyRefused):
        plan_change(file_card("project_settings", settings={"model": "opus"}), folder, home)
    assert (outside / "CLAUDE.md").read_text() == "elsewhere\n"
    assert sorted(p.name for p in outside.iterdir()) == ["CLAUDE.md"]

    # A link that stays inside the project folder is followed: the file it leads to is written.
    (folder / "CLAUDE.md").unlink()
    (folder / "docs").mkdir()
    (folder / "docs" / "AGENTS.md").write_text("Agents.\n")
    (folder / "CLAUDE.md").symlink_to(Path("docs") / "AGENTS.md")
    change = plan_change(file_card("project_claude_md", "More."), folder, home)
    assert change.target == folder.resolve() / "docs" / "AGENTS.md"
    apply_change(change, home)
    assert (folder / "docs" / "AGENTS.md").read_text() == "Agents.\n\nMore.\n"
    assert (folder / "CLAUDE.md").is_symlink()

    # A file that became a link out after the preview is refused, though its content is the same.
    (folder / "CLAUDE.md").unlink()
    (folder / "CLAUDE.md").write_text("Same.\n")
    change = plan_change(file_card("project_claude_md", "More."), folder, home)
    (outside / "same.md").write_text("Same.\n")
    (folder / "CLAUDE.md").unlink()
    (folder / "CLAUDE.md").symlink_to(outside / "same.md")
    with pytest.raises(ApplyRefused):
        apply_change(change, home)
    assert (outside / "same.md").read_text() == "Same.\n"


def test_apply_refuses_under_claude_home(tmp_path: Path) -> None:
    # A project folder that holds the Claude home as its `.claude` (a home folder in git).
    folder = tmp_path / "user"
    home = folder / ".claude"
    home.mkdir(parents=True)
    (home / "settings.json").write_text('{"model": "sonnet"}\n')
    freeze_tree(home)
    try:
        with pytest.raises(ApplyRefused) as refused:
            plan_change(
                file_card("project_settings", settings={"effortLevel": "high"}), folder, home
            )
        assert "Claude home" in refused.value.reason

        # A project folder inside the Claude home.
        inner = home / "projects"
        thaw_tree(home)
        inner.mkdir()
        freeze_tree(home)
        with pytest.raises(ApplyRefused):
            plan_change(file_card("project_claude_md", "More."), inner, home)

        # A `.claude` link to the Claude home, from a project elsewhere.
        project = tmp_path / "project"
        project.mkdir()
        (project / ".claude").symlink_to(home, target_is_directory=True)
        with pytest.raises(ApplyRefused):
            plan_change(file_card("project_settings", settings={"model": "opus"}), project, home)
    finally:
        thaw_tree(home)
    assert (home / "settings.json").read_text() == '{"model": "sonnet"}\n'


def test_apply_refuses_non_regular_file(folder: Path, home: Path) -> None:
    (folder / "CLAUDE.md").mkdir()
    with pytest.raises(ApplyRefused) as refused:
        plan_change(file_card("project_claude_md", "More."), folder, home)
    assert "not a regular file" in refused.value.reason
    (folder / ".claude").write_text("a file, not a folder\n")
    with pytest.raises(ApplyRefused):
        plan_change(file_card("project_settings", settings={"model": "opus"}), folder, home)


def test_apply_creates_missing_dot_claude(folder: Path, home: Path) -> None:
    suggestion = file_card("project_settings", settings={"permissions": {"deny": ["WebFetch"]}})
    change = plan_change(suggestion, folder, home)
    assert change.before is None
    assert change.before_hash == "absent"
    diff_text(change)
    assert not (folder / ".claude").exists()  # never at preview

    apply_change(change, home)
    dot_claude = folder / ".claude"
    umask = os.umask(0)
    os.umask(umask)
    assert stat.S_IMODE(dot_claude.stat().st_mode) == 0o755 & ~umask
    assert json.loads((dot_claude / "settings.json").read_text()) == {
        "permissions": {"deny": ["WebFetch"]}
    }


def test_apply_refuses_changed_file(folder: Path, home: Path) -> None:
    (folder / "CLAUDE.md").write_text("One.\n")
    change = plan_change(file_card("project_claude_md", "Two."), folder, home)
    (folder / "CLAUDE.md").write_text("One, edited.\n")
    with pytest.raises(ApplyRefused) as refused:
        apply_change(change, home)
    assert refused.value.reason == CHANGED
    assert (folder / "CLAUDE.md").read_text() == "One, edited.\n"

    # A file that appeared since a preview of it missing.
    change = plan_change(file_card("project_settings", settings={"model": "opus"}), folder, home)
    (folder / ".claude").mkdir()
    (folder / ".claude" / "settings.json").write_text("{}\n")
    with pytest.raises(ApplyRefused) as refused:
        apply_change(change, home)
    assert refused.value.reason == CHANGED
    assert (folder / ".claude" / "settings.json").read_text() == "{}\n"


def test_project_folder_git_toplevel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    repo = tmp_path / "repo"
    sub = repo / "src" / "pkg"
    sub.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    assert project_folder(str(sub)) == repo.resolve()

    plain = tmp_path / "plain"
    plain.mkdir()
    assert project_folder(str(plain)) == plain

    # No git: the cwd.
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    assert project_folder(str(sub)) == sub
