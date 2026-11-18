"""Tests for the flag allow-list and its value rules."""

import pytest

from vigil.catalog import CatalogEntry
from vigil.flags import RULES, FlagRefused, check, choices_from_help, parse_suggested


def entry(name: str, description: str = "", parent: str | None = None) -> CatalogEntry:
    kind = "subcommand_flag" if parent else "flag"
    return CatalogEntry(kind, name, None, parent, description)  # type: ignore[arg-type]


def catalog_of(*names: str) -> list[CatalogEntry]:
    return [entry(name) for name in names]


PERMISSION_HELP = (
    'Permission mode to use for the session (choices: "acceptEdits", "auto", '
    '"bypassPermissions", "manual", "dontAsk", "plan")'
)


@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        (["--remote-control"], ["--remote-control"]),
        (["--remote-control", "my session"], ["--remote-control", "my session"]),
        (["--remote-control=phone"], ["--remote-control", "phone"]),
        (["--remote-control", "--model", "opus"], ["--remote-control", "--model", "opus"]),
        (["--name", "fix the build"], ["--name", "fix the build"]),
        (["--model", "claude-opus-4.5[1m]"], ["--model", "claude-opus-4.5[1m]"]),
        (["--model=opus"], ["--model", "opus"]),
        (["--effort", "high"], ["--effort", "high"]),
        (["--permission-mode=plan"], ["--permission-mode", "plan"]),
        (["--agent", "code_reviewer-2"], ["--agent", "code_reviewer-2"]),
        (["--worktree", "--chrome"], ["--worktree", "--chrome"]),
        (
            ["--no-chrome", "--ide", "--verbose", "--brief"],
            ["--no-chrome", "--ide", "--verbose", "--brief"],
        ),
        ([], []),
    ],
)
def test_accepts_good_examples(tokens: list[str], expected: list[str]) -> None:
    assert check(tokens) == expected


@pytest.mark.parametrize(
    ("tokens", "reason"),
    [
        (["--remote-control", "x" * 65], "--remote-control"),
        (["--remote-control="], "--remote-control"),
        (["--name", ""], "--name"),
        (["--name", "x" * 65], "--name"),
        (["--name", "two\nlines"], "--name"),
        (["--name=-flag"], "--name"),
        (["--name"], "--name needs a value"),
        (["--name", "--model", "opus"], "--name needs a value"),
        (["--model", "opus 4"], "--model"),
        (["--model", "a/b"], "--model"),
        (["--model", "m" * 65], "--model"),
        (["--model=-x"], "--model"),
        (["--model"], "--model needs a value"),
        (["--effort", "x1"], "--effort"),
        (["--effort"], "--effort needs a value"),
        (["--agent", "a b"], "--agent"),
        (["--agent", "a.b"], "--agent"),
        (["--worktree=feature"], "--worktree takes no value"),
        (["--worktree", "feature"], "value without a flag: feature"),
        (["opus"], "value without a flag: opus"),
    ],
)
def test_refuses_bad_examples(tokens: list[str], reason: str) -> None:
    with pytest.raises(FlagRefused) as refused:
        check(tokens)
    assert reason in refused.value.reason


@pytest.mark.parametrize(
    "flag",
    [
        "--dangerously-skip-permissions",
        "--allow-dangerously-skip-permissions",
        "--system-prompt",
        "--mcp-config",
        "--settings",
        "--add-dir",
        "--tools",
        "--bare",
    ],
)
def test_refuses_dangerous(flag: str) -> None:
    with pytest.raises(FlagRefused) as refused:
        check([flag])
    assert refused.value.reason == f"flag not allowed: {flag}"
    with pytest.raises(FlagRefused, match="flag not allowed"):
        check([f"{flag}=x"])
    # A listed flag does not make an unlisted one after it acceptable.
    with pytest.raises(FlagRefused, match="flag not allowed"):
        check(["--model", "opus", flag, "x"])


def test_permission_mode_bypass_refused() -> None:
    for tokens in (
        ["--permission-mode", "bypassPermissions"],
        ["--permission-mode=bypassPermissions"],
        ["--permission-mode", "BYPASSPERMISSIONS"],
    ):
        with pytest.raises(FlagRefused, match="bypassPermissions is not allowed"):
            check(tokens)
        with pytest.raises(FlagRefused, match="bypassPermissions is not allowed"):
            check(tokens, [entry("--permission-mode", PERMISSION_HELP)])


def test_permission_mode_choices_from_catalog() -> None:
    catalog = [entry("--permission-mode", PERMISSION_HELP)]
    assert check(["--permission-mode", "dontAsk"], catalog) == ["--permission-mode", "dontAsk"]
    with pytest.raises(FlagRefused, match="is not one of acceptEdits, auto, manual, dontAsk, plan"):
        check(["--permission-mode", "yolo"], catalog)


def test_effort_choices_from_catalog() -> None:
    catalog = [entry("--effort", 'Effort (choices: "low", "medium", "high")')]
    assert check(["--effort", "high"], catalog) == ["--effort", "high"]
    with pytest.raises(FlagRefused) as refused:
        check(["--effort", "extreme"], catalog)
    assert refused.value.reason == '--effort: "extreme" is not one of low, medium, high'


def test_effort_choices_in_the_bare_help_form() -> None:
    catalog = [entry("--effort", "Effort level for the current session (low, medium, high)")]
    assert check(["--effort=low"], catalog) == ["--effort", "low"]
    with pytest.raises(FlagRefused, match="not one of low, medium, high"):
        check(["--effort", "max"], catalog)


def test_choice_without_listed_choices_falls_back_to_letters() -> None:
    catalog = catalog_of("--effort")
    assert check(["--effort", "whatever"], catalog) == ["--effort", "whatever"]
    with pytest.raises(FlagRefused, match="--effort"):
        check(["--effort", "x;y"], catalog)


def test_flag_missing_from_catalog_refused() -> None:
    catalog = catalog_of("--model")
    assert check(["--model", "opus"], catalog) == ["--model", "opus"]
    with pytest.raises(FlagRefused) as refused:
        check(["--model", "opus", "--worktree"], catalog)
    assert refused.value.reason == "--worktree is not offered by the installed claude"


def test_catalog_lookup_ignores_subcommand_flags() -> None:
    catalog = [entry("--worktree", parent="agents")]
    with pytest.raises(FlagRefused, match="not offered"):
        check(["--worktree"], catalog)


def test_every_rule_flag_checked_against_catalog() -> None:
    for flag, rule in RULES.items():
        tokens = [flag] if rule in ("none", "optional_name") else [flag, "plan"]
        with pytest.raises(FlagRefused, match="not offered"):
            check(tokens, [])


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ('(choices: "a", "b")', ("a", "b")),
        ('Mode\n   (choices: "acceptEdits",\n  "auto", "plan")', ("acceptEdits", "auto", "plan")),
        ("Effort level (low, medium, high, xhigh, max)", ("low", "medium", "high", "xhigh", "max")),
        ("(optionally named)", ()),
        ("Model. Provide an alias (e.g. 'opus', or 'sonnet')", ()),
        ("", ()),
    ],
)
def test_choices_from_help(description: str, expected: tuple[str, ...]) -> None:
    assert choices_from_help(description) == expected


def test_parse_suggested() -> None:
    assert parse_suggested("--model", "opus") == ["--model", "opus"]
    assert parse_suggested("--worktree", None) == ["--worktree"]
    assert parse_suggested("--worktree", "") == ["--worktree"]
    assert check(parse_suggested("--effort", "high")) == ["--effort", "high"]
