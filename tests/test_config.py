from pathlib import Path

import pytest

from vigil.config import Config, ConfigError, load_config
from vigil.paths import config_path, data_dir, index_path


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_defaults_when_file_missing(tmp_path: Path) -> None:
    config = load_config(tmp_path / "absent.toml")
    assert config == Config()
    assert config.port == 7787
    assert config.resume.launcher == "auto"
    assert config.resume.default_flags == ()
    assert config.resume.terminal_command is None
    assert config.usage.shell_history is False
    assert config.claude_home == Path.home() / ".claude"


def test_loads_toml_values(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        """
claude_home = "/srv/claude"
port = 9000

[resume]
default_flags = ["--remote-control", "--model", "opus", "--permission-mode=plan"]
terminal_command = "xterm -e {attach}"
launcher = "tmux"

[usage]
shell_history = true
history_files = ["/srv/history/bash", "~/zsh_history"]
claude_aliases = ["cc"]
""",
    )
    config = load_config(path)
    assert config.claude_home == Path("/srv/claude")
    assert config.port == 9000
    assert config.resume.default_flags == (
        "--remote-control",
        "--model",
        "opus",
        "--permission-mode=plan",
    )
    assert config.resume.terminal_command == "xterm -e {attach}"
    assert config.resume.launcher == "tmux"
    assert config.usage.shell_history is True
    assert config.usage.history_files == (Path("/srv/history/bash"), Path.home() / "zsh_history")
    assert config.usage.claude_aliases == ("cc",)


def test_unknown_key_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="bogus"):
        load_config(write(tmp_path, "bogus = 1\n"))
    with pytest.raises(ConfigError, match="resume.bogus"):
        load_config(write(tmp_path, "[resume]\nbogus = 1\n"))
    with pytest.raises(ConfigError, match="usage.bogus"):
        load_config(write(tmp_path, "[usage]\nbogus = 1\n"))


def test_disallowed_default_flag_raises(tmp_path: Path) -> None:
    path = write(tmp_path, '[resume]\ndefault_flags = ["--dangerously-skip-permissions"]\n')
    with pytest.raises(ConfigError, match="--dangerously-skip-permissions"):
        load_config(path)


def test_disallowed_flag_in_equals_form_or_after_allowed_one_raises(tmp_path: Path) -> None:
    for flags in (
        '["--add-dir=/etc"]',
        '["--model", "opus", "--add-dir", "/etc"]',
        '["opus"]',
    ):
        with pytest.raises(ConfigError):
            load_config(write(tmp_path, f"[resume]\ndefault_flags = {flags}\n"))


@pytest.mark.parametrize(
    "flags",
    [
        '["--permission-mode", "bypassPermissions"]',
        '["--model", "a b"]',
        '["--worktree=x"]',
        '["--effort"]',
    ],
)
def test_default_flags_value_checked(tmp_path: Path, flags: str) -> None:
    with pytest.raises(ConfigError, match="resume.default_flags"):
        load_config(write(tmp_path, f"[resume]\ndefault_flags = {flags}\n"))


def test_default_flags_error_names_the_flag_and_why(tmp_path: Path) -> None:
    path = write(tmp_path, '[resume]\ndefault_flags = ["--permission-mode", "bypassPermissions"]\n')
    with pytest.raises(ConfigError, match="--permission-mode bypassPermissions is not allowed"):
        load_config(path)


@pytest.mark.parametrize(
    "text",
    [
        "port = '7787'",
        "port = true",
        "port = 0",
        "port = 70000",
        "claude_home = 5",
        "resume = 5",
        "[resume]\nlauncher = 'screen'",
        "[resume]\nterminal_command = 5",
        "[resume]\nterminal_command = 'xterm'",
        "[resume]\ndefault_flags = 'x'",
        "[usage]\nshell_history = 'yes'",
        "[usage]\nhistory_files = [1]",
        "[usage]\nclaude_aliases = 'cc'",
        "port = ",
    ],
)
def test_bad_values_raise(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text + "\n"))


def test_xdg_env_respected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert config_path() == tmp_path / "cfg" / "vigil" / "config.toml"
    assert data_dir() == tmp_path / "data" / "vigil"
    assert index_path() == tmp_path / "data" / "vigil" / "index.db"
    (tmp_path / "cfg" / "vigil").mkdir(parents=True)
    config_path().write_text("port = 8123\n")
    assert load_config().port == 8123


def test_xdg_defaults_and_relative_values_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", "relative/path")
    assert config_path() == tmp_path / ".config" / "vigil" / "config.toml"
    assert index_path() == tmp_path / ".local" / "share" / "vigil" / "index.db"


def test_remote_link_timeout_bounds(tmp_path: Path) -> None:
    assert load_config(write(tmp_path, "")).resume.remote_link_timeout == 30.0
    for value, expected in (("1", 1.0), ("12.5", 12.5), ("300", 300.0)):
        config = load_config(write(tmp_path, f"[resume]\nremote_link_timeout = {value}\n"))
        assert config.resume.remote_link_timeout == expected
    for value in ("0", "0.5", "301", "-1", "'30'", "true"):
        with pytest.raises(ConfigError, match="resume.remote_link_timeout"):
            load_config(write(tmp_path, f"[resume]\nremote_link_timeout = {value}\n"))
