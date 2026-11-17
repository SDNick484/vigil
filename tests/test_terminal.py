"""Tests for terminal detection. Nothing is launched; executables are fakes on a test PATH."""

from pathlib import Path

import pytest

from tests.conftest import FakeBin
from vigil.config import ResumeConfig
from vigil.terminal import KNOWN_TERMINALS, from_template, terminal_argv

pytestmark = pytest.mark.usefixtures("proc_version")

ATTACH = ["claude", "attach", "00000000-0000-4000-8000-000000000001"]
CWD = "/home/user/projects/demo"


def env_for(fake_bin: FakeBin, **extra: str) -> dict[str, str]:
    return {"PATH": str(fake_bin.directory), **extra}


def test_terminal_template(fake_bin: FakeBin) -> None:
    cfg = ResumeConfig(terminal_command="mate-terminal --title 'vigil resume' -- {attach}")
    # The template wins even without a display, over $TERMINAL and over WSL.
    env = env_for(fake_bin, TERMINAL="xterm", WSL_DISTRO_NAME="Distro")
    fake_bin.install("wt.exe")
    assert terminal_argv(ATTACH, CWD, cfg, env) == [
        "mate-terminal",
        "--title",
        "vigil resume",
        "--",
        *ATTACH,
    ]


def test_terminal_template_embedded_placeholder() -> None:
    argv = from_template("myterm --command={attach}", ["tmux", "attach", "-t", "vigil-0000"])
    assert argv == ["myterm", "--command=tmux attach -t vigil-0000"]


def test_terminal_template_invalid_raises() -> None:
    with pytest.raises(ValueError):
        from_template("myterm 'unterminated {attach}", ATTACH)


def test_terminal_wsl_uses_wt(fake_bin: FakeBin) -> None:
    fake_bin.install("wt.exe")
    fake_bin.install("xterm")
    env = env_for(fake_bin, WSL_DISTRO_NAME="Distro", DISPLAY=":0")
    assert terminal_argv(ATTACH, CWD, ResumeConfig(), env) == [
        "wt.exe",
        "wsl.exe",
        "-d",
        "Distro",
        "--cd",
        CWD,
        "--",
        *ATTACH,
    ]


def test_terminal_wsl_detected_from_proc_version(fake_bin: FakeBin, proc_version: Path) -> None:
    proc_version.write_text("Linux version 6.6.0-microsoft-standard-WSL2 (gcc) #1 SMP\n")
    fake_bin.install("wt.exe")
    argv = terminal_argv(ATTACH, "/srv/a;b", ResumeConfig(), env_for(fake_bin))
    # No distro name known: wsl.exe uses the default distro. ";" is escaped for wt.exe.
    assert argv == ["wt.exe", "wsl.exe", "--cd", "/srv/a\\;b", "--", *ATTACH]


def test_terminal_wsl_without_wt_uses_display(fake_bin: FakeBin) -> None:
    fake_bin.install("foot")
    env = env_for(fake_bin, WSL_DISTRO_NAME="Distro", WAYLAND_DISPLAY="wayland-0")
    assert terminal_argv(ATTACH, CWD, ResumeConfig(), env) == ["foot", *ATTACH]


def test_terminal_none_without_display(fake_bin: FakeBin) -> None:
    fake_bin.install("xterm")
    env = env_for(fake_bin, TERMINAL="xterm")
    assert terminal_argv(ATTACH, CWD, ResumeConfig(), env) is None


def test_terminal_env_variable(fake_bin: FakeBin) -> None:
    fake_bin.install("myterm")
    fake_bin.install("xterm")
    env = env_for(fake_bin, TERMINAL="myterm --class vigil", DISPLAY=":0")
    assert terminal_argv(ATTACH, CWD, ResumeConfig(), env) == [
        "myterm",
        "--class",
        "vigil",
        "-e",
        *ATTACH,
    ]


def test_terminal_env_variable_missing_falls_back(fake_bin: FakeBin) -> None:
    fake_bin.install("konsole")
    env = env_for(fake_bin, TERMINAL="not-installed", DISPLAY=":0")
    assert terminal_argv(ATTACH, CWD, ResumeConfig(), env) == ["konsole", "-e", *ATTACH]


@pytest.mark.parametrize(("name", "exec_args"), KNOWN_TERMINALS)
def test_terminal_known_list_syntax(
    fake_bin: FakeBin, name: str, exec_args: tuple[str, ...]
) -> None:
    fake_bin.install(name)
    env = env_for(fake_bin, DISPLAY=":0")
    assert terminal_argv(ATTACH, CWD, ResumeConfig(), env) == [name, *exec_args, *ATTACH]


def test_terminal_known_list_order(fake_bin: FakeBin) -> None:
    fake_bin.install("xterm")
    fake_bin.install("gnome-terminal")
    env = env_for(fake_bin, DISPLAY=":0")
    assert terminal_argv(ATTACH, CWD, ResumeConfig(), env) == ["gnome-terminal", "--", *ATTACH]


def test_terminal_none_when_nothing_installed(fake_bin: FakeBin) -> None:
    env = env_for(fake_bin, DISPLAY=":0")
    assert terminal_argv(ATTACH, CWD, ResumeConfig(), env) is None
