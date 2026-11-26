import re
from pathlib import Path

import pytest

from vigil.config import (
    MACHINE_NAME,
    Config,
    ConfigError,
    ReviewConfig,
    TokenConfig,
    check_machine_name,
    load_config,
)
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


def test_review_config(tmp_path: Path) -> None:
    assert load_config(write(tmp_path, "")).review == ReviewConfig(
        max_budget_usd=3.0, model="sonnet"
    )
    config = load_config(write(tmp_path, '[review]\nmax_budget_usd = 1\nmodel = "opus"\n'))
    assert config.review == ReviewConfig(max_budget_usd=1.0, model="opus")
    assert load_config(write(tmp_path, '[review]\nmodel = ""\n')).review.model is None
    for value in ("0.01", "51", "'1'", "true"):
        with pytest.raises(ConfigError, match="review.max_budget_usd"):
            load_config(write(tmp_path, f"[review]\nmax_budget_usd = {value}\n"))
    with pytest.raises(ConfigError, match="review.model: --model: 'a b' is not a valid model name"):
        load_config(write(tmp_path, '[review]\nmodel = "a b"\n'))
    with pytest.raises(ConfigError, match="review.model"):
        load_config(write(tmp_path, "[review]\nmodel = 5\n"))
    with pytest.raises(ConfigError, match="unknown config key: review.tools"):
        load_config(write(tmp_path, "[review]\ntools = 1\n"))


def test_server_tokens_parsed(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        '[[server.tokens]]\nfile = "/tmp/r"\nscope = "read"\n\n'
        '[[server.tokens]]\nfile = "/tmp/p"\nscope = "push"\nmachine = "laptop"\n',
    )
    server = load_config(path).server
    assert server.tokens == (
        TokenConfig(file=Path("/tmp/r"), scope="read"),
        TokenConfig(file=Path("/tmp/p"), scope="push", machine="laptop"),
    )
    assert server.token_file is None
    assert server.remote_enabled
    assert not load_config(write(tmp_path, "")).server.remote_enabled


@pytest.mark.parametrize(
    ("entry", "where"),
    [
        ('file = "/tmp/t"\nscope = "admin"', "server.tokens[1].scope"),
        ('file = "/tmp/t"\nscope = "push"', "server.tokens[1].machine"),
        ('file = "/tmp/t"\nscope = "read"\nmachine = "laptop"', "server.tokens[1].machine"),
        ('file = "/tmp/t"\nscope = "full"\nmachine = "laptop"', "server.tokens[1].machine"),
        ('file = "/tmp/t"\nscope = "push"\nmachine = "bad name"', "server.tokens[1].machine"),
        ('file = "relative/t"\nscope = "read"', "server.tokens[1].file"),
        ('scope = "read"', "server.tokens[1].file"),
        ('file = "/tmp/t"', "server.tokens[1].scope"),
        ('file = "/tmp/t"\nscope = "read"\nname = "x"', "server.tokens[1].name"),
        (
            'file = "/tmp/t"\nscope = "read"\n\n[[server.tokens]]\nfile = "/tmp/u"\nscope = 3',
            "server.tokens[2].scope",
        ),
    ],
)
def test_server_tokens_refused(tmp_path: Path, entry: str, where: str) -> None:
    with pytest.raises(ConfigError, match=re.escape(where)):
        load_config(write(tmp_path, f"[[server.tokens]]\n{entry}\n"))


def test_server_tokens_must_be_tables(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=re.escape("server.tokens")):
        load_config(write(tmp_path, '[server]\ntokens = ["/tmp/t"]\n'))


def test_allowed_hosts_with_tokens_only(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        '[server]\nallowed_hosts = ["hub.example.com"]\n\n'
        '[[server.tokens]]\nfile = "/tmp/r"\nscope = "read"\n',
    )
    assert load_config(path).server.allowed_hosts == ("hub.example.com",)
    with pytest.raises(ConfigError, match=re.escape("needs remote access")):
        load_config(write(tmp_path, '[server]\nallowed_hosts = ["hub.example.com"]\n'))


def test_machine_name_check() -> None:
    assert check_machine_name("laptop-2_b", "x") == "laptop-2_b"
    assert MACHINE_NAME.fullmatch("desk")
    for value in ("bad name", "-lead", "", "a" * 64, 3, None):
        with pytest.raises(ConfigError, match="`where` must be 1 to 63 letters"):
            check_machine_name(value, "where")


# --- [server.access] ----------------------------------------------------------------------------

# Imported here rather than at the top so this file stays append-only.
from vigil.config import AccessConfig, ServerConfig, ServiceTokenConfig  # noqa: E402

TEAM = "example-team.cloudflareaccess.com"
ACCESS = f'[server.access]\nteam_domain = "{TEAM}"\naud = "aud-placeholder"\n'


def test_server_access_parsed(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        ACCESS + 'emails = ["Author@Example.com"]\n\n'
        "[[server.access.service_tokens]]\n"
        'client_id = "0123abcd.access"\nscope = "push"\nmachine = "laptop"\n',
    )
    access = load_config(path).server.access
    assert access == AccessConfig(
        team_domain="example-team.cloudflareaccess.com",
        aud=("aud-placeholder",),
        emails=frozenset({"author@example.com"}),
        service_tokens=(ServiceTokenConfig("0123abcd.access", "push", "laptop"),),
    )
    assert access.aud == ("aud-placeholder",)
    assert access.emails == frozenset({"author@example.com"})
    listed = write(
        tmp_path,
        '[server.access]\nteam_domain = "Example-Team.cloudflareaccess.com"\n'
        'aud = ["aud-one", "aud-two"]\n',
    )
    access = load_config(listed).server.access
    assert access is not None
    assert access.team_domain == "example-team.cloudflareaccess.com"
    assert access.aud == ("aud-one", "aud-two")
    assert access.emails == frozenset()
    assert access.service_tokens == ()


@pytest.mark.parametrize(
    ("text", "where"),
    [
        (ACCESS.replace(TEAM, "hub.example.com"), "team_domain"),
        (
            ACCESS.replace(TEAM, "cloudflareaccess.com"),
            "team_domain",
        ),
        (
            ACCESS.replace(TEAM, "https://x.cloudflareaccess.com"),
            "team_domain",
        ),
        (ACCESS.replace('"aud-placeholder"', "[]"), "server.access.aud"),
        (ACCESS.replace('"aud-placeholder"', "3"), "server.access.aud"),
        (ACCESS.replace('"aud-placeholder"', '["ok", ""]'), "server.access.aud"),
        (ACCESS.replace('"aud-placeholder"', '"  "'), "server.access.aud"),
        ('[server.access]\naud = "aud-placeholder"\n', "server.access.team_domain"),
        ('[server.access]\nteam_domain = "example-team.cloudflareaccess.com"\n', "aud"),
        (ACCESS + 'emails = "author@example.com"\n', "server.access.emails"),
        (ACCESS + 'emails = [""]\n', "server.access.emails"),
        (ACCESS + 'emails = ["two words@example.com"]\n', "server.access.emails"),
        (
            ACCESS + '[[server.access.service_tokens]]\nclient_id = "a.access"\nscope = "push"\n',
            "server.access.service_tokens[1].machine",
        ),
        (
            ACCESS + '[[server.access.service_tokens]]\nclient_id = "a.access"\nscope = "read"\n\n'
            '[[server.access.service_tokens]]\nclient_id = "a.access"\nscope = "full"\n',
            "server.access.service_tokens[2].client_id",
        ),
        (
            ACCESS + '[[server.access.service_tokens]]\nclient_id = "a b"\nscope = "read"\n',
            "server.access.service_tokens[1].client_id",
        ),
        (
            ACCESS + '[[server.access.service_tokens]]\nclient_id = ""\nscope = "read"\n',
            "server.access.service_tokens[1].client_id",
        ),
        (
            ACCESS + '[[server.access.service_tokens]]\nclient_id = "a.access"\nscope = "admin"\n',
            "server.access.service_tokens[1].scope",
        ),
        (
            ACCESS + '[[server.access.service_tokens]]\nclient_id = "a.access"\nscope = "read"\n'
            'machine = "laptop"\n',
            "server.access.service_tokens[1].machine",
        ),
        (
            ACCESS + '[[server.access.service_tokens]]\nclient_id = "a.access"\nscope = "read"\n'
            'file = "/tmp/t"\n',
            "server.access.service_tokens[1].file",
        ),
        (ACCESS + 'service_tokens = ["a.access"]\n', "server.access.service_tokens"),
        (ACCESS + 'teams = ["x"]\n', "server.access.teams"),
        ('[server]\naccess = "example-team.cloudflareaccess.com"\n', "server.access"),
    ],
)
def test_server_access_refused(tmp_path: Path, text: str, where: str) -> None:
    with pytest.raises(ConfigError, match=re.escape(where)):
        load_config(write(tmp_path, text))


def test_access_turns_remote_on(tmp_path: Path) -> None:
    access = AccessConfig(team_domain="example-team.cloudflareaccess.com", aud=("aud-placeholder",))
    assert ServerConfig(access=access).remote_enabled is True
    assert ServerConfig().remote_enabled is False
    path = write(tmp_path, '[server]\nallowed_hosts = ["hub.example.com"]\n\n' + ACCESS)
    server = load_config(path).server
    assert server.allowed_hosts == ("hub.example.com",)
    assert server.access == access
