from pathlib import Path

import pytest

from vigil.config import ConfigError, load_config
from vigil.config_edit import add_default_flags

COMMENTED = """\
# vigil's config (a placeholder for the author's own)
port = 7790  # not the default

[resume]
# flags for every launch
launcher = "auto"
"""


def test_always_use_keeps_comments(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(COMMENTED)
    add_default_flags(path, ["--model", "opus"])
    text = path.read_text()
    assert "# vigil's config (a placeholder for the author's own)" in text
    assert "port = 7790  # not the default" in text
    assert "# flags for every launch" in text
    config = load_config(path)
    assert config.port == 7790
    assert config.resume.default_flags == ("--model", "opus")
    assert config.resume.launcher == "auto"


def test_always_use_creates_file(tmp_path: Path) -> None:
    path = tmp_path / "vigil" / "config.toml"
    add_default_flags(path, ["--permission-mode", "plan"])
    assert load_config(path).resume.default_flags == ("--permission-mode", "plan")
    assert [p.name for p in path.parent.iterdir()] == ["config.toml"]  # no temp file left


def test_always_use_replaces_value(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[resume]\ndefault_flags = ["--verbose", "--model", "sonnet"]\n')
    add_default_flags(path, ["--model", "opus"])
    assert load_config(path).resume.default_flags == ("--verbose", "--model", "opus")
    add_default_flags(path, ["--chrome"])
    assert load_config(path).resume.default_flags == ("--verbose", "--model", "opus", "--chrome")


def test_always_use_refuses_invalid(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    # The file is already invalid: the result would be too, so nothing is written.
    original = 'port = 0\n[resume]\ndefault_flags = ["--model", "sonnet"]\n'
    path.write_text(original)
    with pytest.raises(ConfigError, match="port"):
        add_default_flags(path, ["--model", "opus"])
    assert path.read_text() == original
    assert [p.name for p in tmp_path.iterdir()] == ["config.toml"]


def test_always_use_refuses_a_flag_off_the_list(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(COMMENTED)
    with pytest.raises(ConfigError, match="not allowed"):
        add_default_flags(path, ["--dangerously-skip-permissions"])
    assert path.read_text() == COMMENTED


def test_always_use_keeps_the_file_mode(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(COMMENTED)
    path.chmod(0o600)
    add_default_flags(path, ["--verbose"])
    assert path.stat().st_mode & 0o777 == 0o600


def test_always_use_in_a_read_only_folder_is_a_config_error(tmp_path: Path) -> None:
    folder = tmp_path / "vigil"
    folder.mkdir()
    path = folder / "config.toml"
    path.write_text(COMMENTED)
    folder.chmod(0o555)
    try:
        with pytest.raises(ConfigError, match=r"cannot write config .*config\.toml: "):
            add_default_flags(path, ["--verbose"])
        missing = tmp_path / "vigil" / "sub" / "config.toml"  # its folder cannot be made
        with pytest.raises(ConfigError, match="cannot write config"):
            add_default_flags(missing, ["--verbose"])
    finally:
        folder.chmod(0o755)
    assert path.read_text() == COMMENTED
    assert [p.name for p in folder.iterdir()] == ["config.toml"]
