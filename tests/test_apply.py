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
    _read,
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


def absent(path: Path) -> str:
    return sha(b"absent\0" + os.fsencode(path))


def test_claude_md_appended(folder: Path, home: Path) -> None:
    existing = "# Project\n\nUse uv.\n\n\n"
    (folder / "CLAUDE.md").write_text(existing)
    change = plan_change(file_card("project_claude_md", "  Run the tests first.\n"), folder, home)
    assert change.target == folder.resolve() / "CLAUDE.md"
    assert change.before == existing
    assert change.after == "# Project\n\nUse uv.\n\nRun the tests first.\n"
    assert change.before_hash == sha(
        b"file\0" + os.fsencode(change.target) + b"\0" + existing.encode()
    )
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
    assert change.before_hash == absent(folder / "CLAUDE.md")
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


@pytest.mark.parametrize(
    ("settings", "key"),
    [
        ({"permissions": {"deny": "WebFetch"}}, "permissions.deny"),
        ({"permissions": {"deny": [{"hooks": 1}]}}, "permissions.deny"),
        ({"permissions": {"ask": ["Bash(ls)", ""]}}, "permissions.ask"),
        ({"permissions": {"ask": ["Bash(ls)\nrm"]}}, "permissions.ask"),
        ({"permissions": ["WebFetch"]}, "permissions"),
        ({"model": {"x": 1}}, "model"),
        ({"model": ""}, "model"),
        ({"model": "m" * 65}, "model"),
        ({"model": 4}, "model"),
        ({"effortLevel": "extreme"}, "effortLevel"),
        ({"includeCoAuthoredBy": "yes"}, "includeCoAuthoredBy"),
        ({"cleanupPeriodDays": 0}, "cleanupPeriodDays"),
        ({"cleanupPeriodDays": True}, "cleanupPeriodDays"),
        ({"cleanupPeriodDays": 1.5}, "cleanupPeriodDays"),
        ({"autoCompactX": {"hooks": {}}}, "autoCompactX"),
        ({"autoCompactX": [1]}, "autoCompactX"),
    ],
)
def test_settings_value_rules(folder: Path, home: Path, settings: dict[str, Any], key: str) -> None:
    with pytest.raises(ApplyRefused) as refused:
        plan_change(file_card("project_settings", settings=settings), folder, home)
    assert f"the settings key {key} must be" in refused.value.reason


def test_settings_value_rules_allow(folder: Path, home: Path) -> None:
    settings = {
        "model": "claude-opus-4-1[1m]",
        "effortLevel": "xhigh",
        "permissions": {"deny": ["WebFetch"], "ask": ["Bash(git push:*)"]},
        "includeCoAuthoredBy": False,
        "cleanupPeriodDays": 30,
        "autoCompactEnabled": True,
        "autoCompactThreshold": 0.8,
    }
    change = plan_change(file_card("project_settings", settings=settings), folder, home)
    assert json.loads(change.after) == settings


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_settings_non_finite_number_refused(folder: Path, home: Path, value: float) -> None:
    suggestion = file_card("project_settings", settings={"autoCompactThreshold": value})
    with pytest.raises(ApplyRefused) as refused:
        plan_change(suggestion, folder, home)
    assert "the settings key autoCompactThreshold must be" in refused.value.reason
    assert list(folder.iterdir()) == []


def test_settings_existing_non_finite_number_refused(folder: Path, home: Path) -> None:
    # Python's json reads NaN; written back it would not be JSON.
    (folder / ".claude").mkdir()
    settings = folder / ".claude" / "settings.json"
    settings.write_text('{"autoCompactThreshold": NaN}\n')
    with pytest.raises(ApplyRefused) as refused:
        plan_change(file_card("project_settings", settings={"model": "opus"}), folder, home)
    assert "NaN or Infinity" in refused.value.reason
    assert settings.read_text() == '{"autoCompactThreshold": NaN}\n'


def test_settings_existing_value_of_another_kind_refused(folder: Path, home: Path) -> None:
    (folder / ".claude").mkdir()
    existing = '{"permissions": {"deny": "WebFetch"}}\n'
    (folder / ".claude" / "settings.json").write_text(existing)
    suggestion = file_card("project_settings", settings={"permissions": {"deny": ["Bash(rm:*)"]}})
    with pytest.raises(ApplyRefused) as refused:
        plan_change(suggestion, folder, home)
    assert "permissions.deny" in refused.value.reason
    (folder / ".claude" / "settings.json").write_text('{"permissions": true}\n')
    with pytest.raises(ApplyRefused):
        plan_change(suggestion, folder, home)


@pytest.mark.parametrize(
    "suggestion",
    [
        file_card("project_claude_md", "Run the tests\u202e first."),
        file_card("project_claude_md", "Run the tests first.\U000e0041\U000e0042"),
        file_card("project_claude_md", "Private \ue000 use."),
        file_card("project_settings", settings={"permissions": {"deny": ["Web\u202eFetch"]}}),
        file_card("project_settings", settings={"model": "opus\U000e0041"}),
        file_card("project_settings", settings={"autoCompact\U000e0041": True}),
    ],
)
def test_invisible_characters_refused(folder: Path, home: Path, suggestion: Suggestion) -> None:
    with pytest.raises(ApplyRefused) as refused:
        plan_change(suggestion, folder, home)
    assert "contains invisible or unassigned characters" in refused.value.reason
    assert list(folder.iterdir()) == []


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


LINK = "is a symbolic link; vigil does not write through links. Copy the text instead."


def test_apply_refuses_symlinks(folder: Path, home: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "CLAUDE.md").write_text("elsewhere\n")
    (folder / "scripts").mkdir()
    (folder / "scripts" / "build.sh").write_text("#!/bin/sh\n")
    (folder / "scripts" / "build.sh").chmod(0o755)
    (folder / ".git").mkdir()
    (folder / ".git" / "config").write_text("[core]\n")
    (folder / "conf").mkdir()
    (folder / "conf" / "settings.json").write_text("{}\n")
    claude_md = folder / "CLAUDE.md"
    # Out of the folder, dangling, and inside it: a link is never written through.
    for link in (
        outside / "CLAUDE.md",
        outside / "missing.md",
        Path("scripts") / "build.sh",
        Path(".git") / "config",
    ):
        claude_md.symlink_to(link)
        with pytest.raises(ApplyRefused) as refused:
            plan_change(file_card("project_claude_md", "More."), folder, home)
        assert refused.value.reason == f"CLAUDE.md {LINK}", link
        claude_md.unlink()
    # A `.claude` link, out of the folder or inside it; and a settings.json link.
    for link in (outside, folder / "conf"):
        (folder / ".claude").symlink_to(link, target_is_directory=True)
        with pytest.raises(ApplyRefused) as refused:
            plan_change(file_card("project_settings", settings={"model": "opus"}), folder, home)
        assert refused.value.reason == f".claude {LINK}", link
        (folder / ".claude").unlink()
    (folder / ".claude").mkdir()
    (folder / ".claude" / "settings.json").symlink_to(folder / "conf" / "settings.json")
    with pytest.raises(ApplyRefused) as refused:
        plan_change(file_card("project_settings", settings={"model": "opus"}), folder, home)
    assert refused.value.reason == f".claude/settings.json {LINK}"

    assert (outside / "CLAUDE.md").read_text() == "elsewhere\n"
    assert sorted(p.name for p in outside.iterdir()) == ["CLAUDE.md"]
    assert (folder / "scripts" / "build.sh").read_text() == "#!/bin/sh\n"
    assert (folder / ".git" / "config").read_text() == "[core]\n"
    assert (folder / "conf" / "settings.json").read_text() == "{}\n"

    # A file that became a link after the preview is refused, though its content is the same.
    claude_md.write_text("Same.\n")
    change = plan_change(file_card("project_claude_md", "More."), folder, home)
    (outside / "same.md").write_text("Same.\n")
    claude_md.unlink()
    claude_md.symlink_to(outside / "same.md")
    with pytest.raises(ApplyRefused):
        apply_change(change, home)
    assert (outside / "same.md").read_text() == "Same.\n"
    assert claude_md.is_symlink()


def test_apply_refuses_executable(folder: Path, home: Path) -> None:
    claude_md = folder / "CLAUDE.md"
    claude_md.write_text("echo hi\n")
    claude_md.chmod(0o744)
    with pytest.raises(ApplyRefused) as refused:
        plan_change(file_card("project_claude_md", "More."), folder, home)
    assert "executable" in refused.value.reason
    # Made executable after the preview.
    claude_md.chmod(0o644)
    change = plan_change(file_card("project_claude_md", "More."), folder, home)
    claude_md.chmod(0o755)
    with pytest.raises(ApplyRefused):
        apply_change(change, home)
    assert claude_md.read_text() == "echo hi\n"


def test_apply_refuses_inside_git_folder(folder: Path, home: Path) -> None:
    git = folder / ".git"
    git.mkdir()
    with pytest.raises(ApplyRefused) as refused:
        plan_change(file_card("project_claude_md", "More."), git, home)
    assert ".git" in refused.value.reason
    assert list(git.iterdir()) == []


def test_apply_refuses_fifo(folder: Path, home: Path) -> None:
    claude_md = folder / "CLAUDE.md"
    os.mkfifo(claude_md)
    with pytest.raises(ApplyRefused) as refused:
        plan_change(file_card("project_claude_md", "More."), folder, home)
    assert "not a regular file" in refused.value.reason
    # Reading checks the open file too, without waiting for a writer.
    with pytest.raises(ApplyRefused):
        _read(claude_md)
    assert stat.S_ISFIFO(claude_md.lstat().st_mode)


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
        with pytest.raises(ApplyRefused) as refused:
            plan_change(file_card("project_settings", settings={"model": "opus"}), project, home)
        assert refused.value.reason == f".claude {LINK}"
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
    assert change.before_hash == absent(folder / ".claude" / "settings.json")
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
