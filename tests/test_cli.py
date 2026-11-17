import argparse
from pathlib import Path

import pytest

from vigil import cli
from vigil.config import Config


def test_help_lists_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])
    assert exit_info.value.code == 0
    assert "vigil" in capsys.readouterr().out


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("vigil-sessions ")
    assert out != "vigil-sessions"


def test_no_command_prints_help_and_fails(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 2
    assert "usage: vigil" in capsys.readouterr().err


class _Hello:
    name = "hello"
    help = "Say hello."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("who")

    def run(self, args: argparse.Namespace, config: Config) -> int:
        print(f"hello {args.who} on {config.port}")
        return 0


def test_registered_command_runs_with_config(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "COMMANDS", [_Hello()])
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert cli.main(["hello", "there"]) == 0
    assert capsys.readouterr().out.strip() == "hello there on 7787"


def test_bad_config_is_a_clean_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "COMMANDS", [_Hello()])
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "vigil").mkdir()
    (tmp_path / "vigil" / "config.toml").write_text("nope = 1\n")
    assert cli.main(["hello", "x"]) == 2
    assert "unknown config key: nope" in capsys.readouterr().err
