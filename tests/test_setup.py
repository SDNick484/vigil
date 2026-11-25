import builtins
import contextlib
import http.server
import json
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.conftest import FakeBin, VigilEnv
from vigil import cli, setup
from vigil.setup import Health, Method

REPO = Path(__file__).resolve().parent.parent
CONTRIB_FILES = {
    "autostart": "xdg/vigil.desktop",
    "systemd": "systemd/vigil.service",
    "openrc": "openrc/vigil",
}
UP = Health("vigil", "0.1.0")
FREE = Health("free")
CLAUDE_OUT = "2.1.312 (Claude Code)\n  --bg  Start in the background\n  attach\n  agents\n"


class World:
    """A `vigil setup` run's world: HOME and XDG dirs under tmp, fakes on PATH, a `vigil` file."""

    def __init__(self, env: VigilEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.env = env
        self.bin: FakeBin = env.bin
        self.monkeypatch = monkeypatch
        self.home = tmp_path / "home"
        self.config_home = tmp_path / "xdg_config"
        self.vigil = self.home / ".local" / "bin" / "vigil"
        self.vigil.parent.mkdir(parents=True)
        self.vigil.write_text("#!/bin/sh\n")
        self.vigil.chmod(0o755)
        monkeypatch.setattr(setup, "vigil_executable", lambda: self.vigil)
        monkeypatch.setattr(setup, "effective_uid", lambda: 1000)
        self.health([FREE, UP])

    def health(self, states: list[Health]) -> None:
        """What each probe of the port answers, in turn (the last one repeats)."""
        remaining = list(states)

        def probe(port: int, timeout: float = 1.0) -> Health:
            return remaining.pop(0) if len(remaining) > 1 else remaining[0]

        self.monkeypatch.setattr(setup, "probe_health", probe)

    def answers(self, *answers: str) -> list[str]:
        """Make `input` give `answers`, and record the questions asked."""
        asked: list[str] = []
        queue = list(answers)

        def fake_input(prompt: str = "") -> str:
            asked.append(prompt)
            if not queue:
                raise EOFError
            return queue.pop(0)

        self.monkeypatch.setattr(builtins, "input", fake_input)
        return asked

    def path_of(self, method: Method) -> Path:
        return setup.entry_path(method, self.config_home)

    def run(self, capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
        code = cli.main(["setup", *argv])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    def written(self) -> list[Path]:
        """Every file under the config folder except the one the fixture made."""
        return sorted(
            p for p in self.config_home.rglob("*") if p.is_file() and p.parent.name != "vigil"
        )

    def calls(self, name: str) -> list[list[str]]:
        return [call.argv for call in self.bin.calls(name)]


@pytest.fixture
def world(
    vigil_env: VigilEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, proc_version: Path
) -> World:
    monkeypatch.delenv("XDG_CURRENT_DESKTOP", raising=False)
    return World(vigil_env, tmp_path, monkeypatch)


# The templates -----------------------------------------------------------------------------


@pytest.mark.parametrize("method", list(CONTRIB_FILES))
def test_contrib_copies_match_the_package_templates(method: Method) -> None:
    """`contrib/` is for installing by hand; the package templates are what setup renders. They
    are one file twice, so they must not drift."""
    contrib = (REPO / "contrib" / CONTRIB_FILES[method]).read_text()
    assert contrib == setup.template(method)


def test_templates_ship_inside_the_package() -> None:
    package = Path(setup.__file__).parent / "templates"
    for relative in CONTRIB_FILES.values():
        assert (package / relative).is_file()


# Which method ------------------------------------------------------------------------------


def test_desktop_session_picks_autostart(world: World) -> None:
    world.bin.install("systemctl")  # present and working, but a desktop wins
    env = {"DISPLAY": ":0", "XDG_CURRENT_DESKTOP": "MATE"}
    picked = setup.detect_method(env)
    assert picked is not None and picked[0] == "autostart"
    assert "MATE" in picked[1]
    wayland = setup.detect_method({"WAYLAND_DISPLAY": "wayland-0", "XDG_CURRENT_DESKTOP": "KDE"})
    assert wayland is not None and wayland[0] == "autostart"


def test_a_display_alone_is_not_a_desktop_session(world: World) -> None:
    world.bin.install("systemctl")
    picked = setup.detect_method({"DISPLAY": ":0"})
    assert picked is not None and picked[0] == "systemd"


def test_no_display_with_working_systemd_picks_systemd(world: World) -> None:
    world.bin.install("systemctl")
    picked = setup.detect_method({})
    assert picked is not None and picked[0] == "systemd"
    assert world.calls("systemctl") == [["--user", "show-environment"]]


def test_failing_systemctl_falls_to_openrc(world: World) -> None:
    world.bin.install("systemctl", exit_code=1)
    world.bin.install("rc-service", stdout="rc-service (OpenRC) 0.62.6\n")
    picked = setup.detect_method({})
    assert picked is not None and picked[0] == "openrc"


@pytest.mark.parametrize("version", ["rc-service (OpenRC) 0.59.3\n", "garbage\n", ""])
def test_old_or_unreadable_openrc_is_not_picked(world: World, version: str) -> None:
    world.bin.install("rc-service", stdout=version)
    assert setup.detect_method({}) is None


def test_nothing_usable_picks_nothing(world: World) -> None:
    assert setup.detect_method({}) is None


def test_setup_without_a_method_says_so(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = world.run(capsys, "--yes")
    assert code == 1
    assert "--method" in err
    assert world.written() == []


# The generated files -----------------------------------------------------------------------


HOME = Path("/home/user")


def test_autostart_file_has_the_absolute_path_and_the_marker() -> None:
    text = setup.render("autostart", Path("/home/user/.local/bin/vigil"), None, HOME)
    lines = text.splitlines()
    assert lines[0] == "[Desktop Entry]"
    assert lines[1] == setup.MARKER
    assert "Exec=/home/user/.local/bin/vigil serve" in lines
    assert "Exec=vigil serve" not in lines
    assert setup.is_ours(text)


def test_systemd_file_has_the_path_and_claudes_folder() -> None:
    text = setup.render("systemd", Path("/opt/venv/bin/vigil"), "/opt/bin2", HOME)
    lines = text.splitlines()
    assert "ExecStart=/opt/venv/bin/vigil serve" in lines
    assert "Environment=PATH=/opt/bin2:%h/.local/bin:/usr/local/bin:/usr/bin:/bin:/opt/bin" in lines
    assert lines[1] == setup.MARKER


def test_openrc_file_has_the_path_and_claudes_folder(tmp_path: Path) -> None:
    text = setup.render("openrc", Path("/opt/venv/bin/vigil"), "/opt/bin2", HOME)
    assert ': "${vigil_bin:=/opt/venv/bin/vigil}"' in text.splitlines()
    assert (
        'export PATH="/opt/bin2:${HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin:/opt/bin:' in text
    )
    assert text.startswith("#!/usr/bin/openrc-run\n" + setup.MARKER + "\n")
    script = tmp_path / "vigil"
    script.write_text(text)
    sh = shutil.which("sh")
    assert sh is not None
    assert subprocess.run([sh, "-n", str(script)], capture_output=True).returncode == 0


@pytest.mark.parametrize("method", ["systemd", "openrc"])
def test_a_folder_that_is_already_listed_is_not_added_again(method: Method) -> None:
    text = setup.render(method, Path("/x/vigil"), "/opt/bin", HOME)
    assert text == setup.render(method, Path("/x/vigil"), None, HOME)
    home_bin = setup.render(method, Path("/x/vigil"), "/home/user/.local/bin", HOME)
    assert "/home/user/.local/bin:" not in home_bin


def test_without_claude_the_path_is_the_template_default() -> None:
    text = setup.render("systemd", Path("/x/vigil"), None, HOME)
    assert (
        "Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin:/opt/bin" in text.splitlines()
    )


def test_a_path_with_a_space_is_quoted() -> None:
    desktop = setup.render("autostart", Path("/home/a b/bin/vigil"), None, HOME)
    assert 'Exec="/home/a b/bin/vigil" serve' in desktop.splitlines()
    unit = setup.render("systemd", Path("/home/a b/bin/vigil"), "/opt/my bin", HOME)
    assert 'ExecStart="/home/a b/bin/vigil" serve' in unit.splitlines()
    assert any(line.startswith('Environment="PATH=/opt/my bin:') for line in unit.splitlines())


@pytest.mark.parametrize("bad", ['/x/"y/vigil', "/x/$y/vigil", "/x/%h/vigil", "/x/`y`/vigil"])
def test_a_path_that_cannot_be_written_safely_is_refused(bad: str) -> None:
    with pytest.raises(setup.SetupError, match="character"):
        setup.render("systemd", Path(bad), None, HOME)
    with pytest.raises(setup.SetupError, match="character"):
        setup.render("openrc", Path("/x/vigil"), bad, HOME)


def test_claudes_folder_goes_into_the_installed_service(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.bin.install("claude", stdout=CLAUDE_OUT)
    world.bin.install("systemctl")
    code, _, _ = world.run(capsys, "--method", "systemd", "--yes")
    assert code == 0
    written = world.path_of("systemd").read_text()
    assert f"Environment=PATH={world.bin.directory}:" in written
    assert f"ExecStart={world.vigil} serve" in written


WINDOWS_APPS = "/mnt/c/Users/user/AppData/Local/Microsoft/WindowsApps"


@pytest.mark.parametrize("method", ["systemd", "openrc"])
def test_windows_terminals_folder_goes_last_on_the_services_path(method: Method) -> None:
    # Last, so a Windows program never shadows a Linux one of the same name.
    text = setup.render(method, Path("/x/vigil"), "/opt/bin2", HOME, terminal_dir=WINDOWS_APPS)
    line = _line_with(text, "PATH=")
    assert line.startswith(("Environment=PATH=/opt/bin2:", 'export PATH="/opt/bin2:'))
    assert line.rstrip('"').endswith(f":{WINDOWS_APPS}")


def test_the_wsl_distro_is_passed_to_the_service() -> None:
    unit = setup.render("systemd", Path("/x/vigil"), None, HOME, distro="Ubuntu-24.04")
    assert "Environment=WSL_DISTRO_NAME=Ubuntu-24.04" in unit.splitlines()
    script = setup.render("openrc", Path("/x/vigil"), None, HOME, distro="Ubuntu-24.04")
    assert 'export WSL_DISTRO_NAME="Ubuntu-24.04"' in script.splitlines()


def test_a_distro_name_that_cannot_be_written_safely_is_left_out() -> None:
    text = setup.render("openrc", Path("/x/vigil"), None, HOME, distro='a"; rm -rf ~; "')
    assert "WSL_DISTRO_NAME" not in text


def test_autostart_ignores_the_wsl_settings() -> None:
    plain = setup.render("autostart", Path("/x/vigil"), None, HOME)
    wsl = setup.render(
        "autostart", Path("/x/vigil"), None, HOME, terminal_dir=WINDOWS_APPS, distro="Gentoo"
    )
    assert wsl == plain


def test_windows_terminal_is_not_looked_for_outside_wsl(world: World) -> None:
    world.bin.install("wt.exe")
    world.bin.install("cmd.exe")
    assert setup.windows_terminal_folder() is None
    assert world.calls("cmd.exe") == []


def test_windows_terminal_on_path_is_used_under_wsl(world: World, proc_version: Path) -> None:
    proc_version.write_text("Linux version 6.6.0-microsoft-standard-WSL2\n")
    world.bin.install("wt.exe")
    world.bin.install("cmd.exe")
    assert setup.windows_terminal_folder() == str(world.bin.directory)
    assert world.calls("cmd.exe") == []


def _windows_apps(world: World, tmp_path: Path, *, with_wt: bool = True) -> Path:
    """A Windows %LOCALAPPDATA% under tmp that `cmd.exe` names and `wslpath` converts."""
    local = tmp_path / "AppData" / "Local"
    apps = local / "Microsoft" / "WindowsApps"
    apps.mkdir(parents=True)
    if with_wt:
        (apps / "wt.exe").write_text("")
    world.bin.install("cmd.exe", stdout="C:\\Users\\user\\AppData\\Local\r\n")
    world.bin.install("wslpath", stdout=f"{local}\n")
    return apps


def test_windows_terminal_off_path_is_found_in_windows_apps(
    world: World, proc_version: Path, tmp_path: Path
) -> None:
    proc_version.write_text("Linux version 6.6.0-microsoft-standard-WSL2\n")
    apps = _windows_apps(world, tmp_path)
    assert setup.windows_terminal_folder() == str(apps)
    assert world.calls("cmd.exe") == [["/d", "/c", "echo %LOCALAPPDATA%"]]
    assert world.calls("wslpath") == [["-u", "C:\\Users\\user\\AppData\\Local"]]


def test_no_windows_terminal_in_windows_apps_is_none(
    world: World, proc_version: Path, tmp_path: Path
) -> None:
    proc_version.write_text("Linux version 6.6.0-microsoft-standard-WSL2\n")
    _windows_apps(world, tmp_path, with_wt=False)
    assert setup.windows_terminal_folder() is None


def test_under_wsl_the_installed_service_can_open_windows_terminal(
    world: World, proc_version: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    proc_version.write_text("Linux version 6.6.0-microsoft-standard-WSL2\n")
    world.monkeypatch.setenv("WSL_DISTRO_NAME", "Gentoo")
    apps = _windows_apps(world, tmp_path)
    world.bin.install("rc-service")
    world.bin.install("rc-update")
    code, out, _ = world.run(capsys, "--method", "openrc", "--yes")
    assert code == 0
    written = world.path_of("openrc").read_text()
    assert _line_with(written, "export PATH=").endswith(f':${{PATH}}:{apps}"')
    assert 'export WSL_DISTRO_NAME="Gentoo"' in written.splitlines()
    assert f"includes {apps}, where Windows Terminal (wt.exe) was found" in out


def _line_with(text: str, needle: str) -> str:
    return next(line for line in text.splitlines() if needle in line)


# Install -----------------------------------------------------------------------------------


def test_dry_run_writes_and_runs_nothing(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    for name in ("systemctl", "rc-service", "rc-update", "setsid"):
        world.bin.install(name)
    code, out, _ = world.run(capsys, "--method", "systemd", "--dry-run")
    assert code == 0
    assert "Dry run" in out
    assert f"ExecStart={world.vigil} serve" in out
    assert world.written() == []
    assert not world.home.joinpath(".config").exists()
    for name in ("systemctl", "rc-service", "rc-update", "setsid"):
        assert world.calls(name) == []


def test_dry_run_shows_the_difference_to_an_existing_file(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    path = world.path_of("autostart")
    path.parent.mkdir(parents=True)
    path.write_text("[Desktop Entry]\nExec=vigil serve\n")
    code, out, _ = world.run(capsys, "--method", "autostart", "--dry-run")
    assert code == 0
    assert "-Exec=vigil serve" in out
    assert path.read_text() == "[Desktop Entry]\nExec=vigil serve\n"


def test_autostart_install_writes_the_entry_and_starts_vigil(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.bin.install("setsid")
    code, out, _ = world.run(capsys, "--method", "autostart", "--yes")
    assert code == 0
    path = world.path_of("autostart")
    assert path.is_file()
    assert f"Exec={world.vigil} serve" in path.read_text().splitlines()
    assert world.calls("setsid") == [["-f", str(world.vigil), "serve"]]
    assert "http://127.0.0.1:7787/" in out
    assert "vigil 0.1.0 is running" in out


def test_autostart_does_not_start_a_second_vigil(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.bin.install("setsid")
    world.health([UP])
    code, _, _ = world.run(capsys, "--method", "autostart", "--yes")
    assert code == 0
    assert world.calls("setsid") == []


def test_systemd_install_steps(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    world.bin.install("systemctl")
    code, out, _ = world.run(capsys, "--method", "systemd", "--yes")
    assert code == 0
    assert world.calls("systemctl") == [
        ["--user", "daemon-reload"],
        ["--user", "enable", "--now", "vigil.service"],
    ]
    assert world.path_of("systemd").stat().st_mode & 0o777 == 0o644
    assert "is running" in out


def test_openrc_install_steps(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    world.bin.install("rc-service")
    world.bin.install("rc-update")
    code, _, _ = world.run(capsys, "--method", "openrc", "--yes")
    assert code == 0
    assert world.calls("rc-service") == [["--user", "vigil", "start"]]
    assert world.calls("rc-update") == [["--user", "add", "vigil", "default"]]
    assert world.path_of("openrc").stat().st_mode & 0o777 == 0o755
    assert world.path_of("openrc").parent == world.config_home / "rc" / "init.d"


def test_openrc_makes_the_default_runlevel_when_no_user_session_has_yet(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    # `openrc --user` makes ~/.config/rc/runlevels on its first run; without it (no pam_openrc,
    # as under WSL) `rc-update --user add vigil default` says the runlevel is not valid.
    world.bin.install("rc-service")
    world.bin.install("rc-update")
    assert world.run(capsys, "--method", "openrc", "--yes")[0] == 0
    assert (world.config_home / "rc" / "runlevels" / "default").is_dir()


def test_the_recommended_method_is_used_when_none_is_given(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.bin.install("setsid")
    world.monkeypatch.setenv("DISPLAY", ":0")
    world.monkeypatch.setenv("XDG_CURRENT_DESKTOP", "MATE")
    code, out, _ = world.run(capsys, "--yes")
    assert code == 0
    assert "XDG autostart entry (a desktop session is running (MATE))" in out
    assert world.path_of("autostart").is_file()


def test_no_start_installs_without_starting(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("systemctl", "rc-service", "rc-update", "setsid"):
        world.bin.install(name)
    assert world.run(capsys, "--method", "systemd", "--yes", "--no-start")[0] == 0
    assert world.calls("systemctl") == [
        ["--user", "daemon-reload"],
        ["--user", "enable", "vigil.service"],
    ]
    assert world.run(capsys, "--method", "openrc", "--yes", "--no-start")[0] == 0
    assert world.calls("rc-service") == []
    assert world.calls("rc-update") == [["--user", "add", "vigil", "default"]]
    code, out, _ = world.run(capsys, "--method", "autostart", "--yes", "--no-start")
    assert code == 0
    assert world.calls("setsid") == []
    assert world.path_of("autostart").is_file()
    assert "not started" in out


def test_without_yes_it_asks_and_no_changes_nothing(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.bin.install("systemctl")
    asked = world.answers("n")
    code, out, _ = world.run(capsys, "--method", "systemd")
    assert code == 1
    assert len(asked) == 1
    assert "Nothing was changed" in out
    assert not world.path_of("systemd").exists()
    assert world.calls("systemctl") == []


def test_no_answer_to_the_question_is_a_no(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.answers()  # end of input
    code, _, _ = world.run(capsys, "--method", "systemd")
    assert code == 1
    assert not world.path_of("systemd").exists()


def test_yes_to_the_question_installs(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    world.bin.install("systemctl")
    world.answers("y")
    assert world.run(capsys, "--method", "systemd")[0] == 0
    assert world.path_of("systemd").is_file()


def test_a_different_file_is_not_replaced_without_a_yes(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.bin.install("systemctl")
    path = world.path_of("systemd")
    path.parent.mkdir(parents=True)
    path.write_text("[Service]\nExecStart=/my/own/vigil serve\n")
    asked = world.answers("n")
    code, out, _ = world.run(capsys, "--method", "systemd")
    assert code == 1
    assert "exists and differs" in out
    assert "-ExecStart=/my/own/vigil serve" in out
    assert "Replace" in asked[0]
    assert path.read_text() == "[Service]\nExecStart=/my/own/vigil serve\n"
    assert world.calls("systemctl") == []


def test_yes_flag_still_asks_before_replacing_a_file_setup_did_not_write(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.bin.install("systemctl")
    path = world.path_of("systemd")
    path.parent.mkdir(parents=True)
    path.write_text("[Service]\nExecStart=/my/own/vigil serve\n")
    asked = world.answers("n")
    code, _, _ = world.run(capsys, "--method", "systemd", "--yes")
    assert code == 1
    assert len(asked) == 1
    assert path.read_text().startswith("[Service]\nExecStart=/my/own")
    world.answers("y")
    assert world.run(capsys, "--method", "systemd", "--yes")[0] == 0
    assert setup.is_ours(path.read_text())


def test_yes_replaces_a_file_setup_wrote_earlier_and_restarts(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.bin.install("systemctl")
    path = world.path_of("systemd")
    path.parent.mkdir(parents=True)
    path.write_text(setup.render("systemd", Path("/old/vigil"), None, world.home))
    world.health([UP])  # it is running, from the old file
    asked = world.answers()
    code, out, _ = world.run(capsys, "--method", "systemd", "--yes")
    assert code == 0
    assert asked == []
    assert "-ExecStart=/old/vigil serve" in out
    assert f"ExecStart={world.vigil} serve" in path.read_text()
    assert world.calls("systemctl")[-1] == ["--user", "restart", "vigil.service"]


def test_an_up_to_date_file_is_left_as_it_is(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.bin.install("systemctl")
    path = world.path_of("systemd")
    path.parent.mkdir(parents=True)
    path.write_text(setup.render("systemd", world.vigil, None, world.home))
    before = path.stat().st_mtime_ns
    code, out, _ = world.run(capsys, "--method", "systemd", "--yes")
    assert code == 0
    assert "already there and up to date" in out
    assert path.stat().st_mtime_ns == before
    assert world.calls("systemctl")[-1] == ["--user", "enable", "--now", "vigil.service"]


def test_a_failing_step_is_reported_and_stops(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.bin.install("setsid", exit_code=1)
    code, _, err = world.run(capsys, "--method", "autostart", "--yes")
    assert code == 1
    assert "could not start vigil" in err
    assert "run `vigil doctor`" not in err


def test_a_missing_tool_is_reported(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = world.run(capsys, "--method", "systemd", "--yes")  # no systemctl on PATH
    assert code == 1
    assert "systemctl was not found" in err


def test_vigil_that_never_answers_points_at_doctor(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.bin.install("setsid")
    world.monkeypatch.setattr(setup, "HEALTH_WAIT", 0.0)
    world.health([FREE])
    code, _, err = world.run(capsys, "--method", "autostart", "--yes")
    assert code == 1
    assert "vigil doctor" in err


def test_a_busy_port_installs_but_does_not_start(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.bin.install("systemctl")
    world.health([Health("other")])
    code, _, err = world.run(capsys, "--method", "systemd", "--yes")
    assert code == 1
    assert "port 7787 is in use" in err
    assert world.path_of("systemd").is_file()
    assert ["--user", "enable", "--now", "vigil.service"] not in world.calls("systemctl")


def test_a_vigil_off_path_gets_the_line_to_add_and_no_shell_file_is_touched(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.bin.install("setsid")
    code, out, _ = world.run(capsys, "--method", "autostart", "--yes")
    assert code == 0
    assert 'export PATH="$HOME/.local/bin:$PATH"' in out
    assert "~/.bashrc" in out
    assert not list(world.home.glob(".*rc")) and not list(world.home.glob(".profile"))


# Safety ------------------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [["--yes"], ["--dry-run"], ["--remove", "--yes"]])
def test_running_as_root_is_refused(
    world: World, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    for name in ("systemctl", "setsid"):
        world.bin.install(name)
    world.monkeypatch.setattr(setup, "effective_uid", lambda: 0)
    code, _, err = world.run(capsys, "--method", "systemd", *argv)
    assert code == 2
    assert "root" in err
    assert world.written() == []
    assert world.calls("systemctl") == [] and world.calls("setsid") == []


def test_nothing_outside_the_config_folders_is_written(tmp_path: Path) -> None:
    config_home = tmp_path / "config"
    for method in setup.METHODS:
        setup.check_target(setup.entry_path(method, config_home), config_home)
    for outside in (tmp_path / "elsewhere" / "vigil", config_home / "vigil" / "config.toml"):
        with pytest.raises(setup.SetupError, match="outside"):
            setup.write_entry(outside, "x", 0o644, config_home)
        assert not outside.exists()
    with pytest.raises(setup.SetupError):
        setup.check_target(config_home / "autostart" / ".." / ".." / "evil", config_home)


def test_sudo_is_never_run() -> None:
    with pytest.raises(setup.SetupError, match="sudo"):
        setup.run_step(setup.Step("x", ("sudo", "true")))


# Remove ------------------------------------------------------------------------------------


def test_remove_removes_only_what_setup_wrote(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("systemctl", "rc-service", "rc-update", "pkill"):
        world.bin.install(name)
    world.health([UP])
    assert world.run(capsys, "--method", "systemd", "--yes")[0] == 0
    systemd = world.path_of("systemd")
    # A hand-made autostart entry (from contrib/) and OpenRC script, which setup did not write.
    autostart, openrc = world.path_of("autostart"), world.path_of("openrc")
    autostart.parent.mkdir(parents=True)
    autostart.write_text(setup.template("autostart"))
    openrc.parent.mkdir(parents=True)
    openrc.write_text(setup.template("openrc"))
    unrelated = world.config_home / "systemd" / "user" / "other.service"
    unrelated.write_text("[Service]\n")
    code, out, _ = world.run(capsys, "--remove", "--yes")
    assert code == 0
    assert not systemd.exists()
    assert autostart.is_file() and openrc.is_file() and unrelated.is_file()
    assert autostart.read_text() == setup.template("autostart")
    assert "Leaving" in out and "did not write" in out
    assert world.calls("systemctl")[-3:] == [
        ["--user", "enable", "--now", "vigil.service"],
        ["--user", "disable", "--now", "vigil.service"],
        ["--user", "daemon-reload"],
    ]
    assert world.calls("rc-service") == [] and world.calls("rc-update") == []
    assert world.calls("pkill") == []


def test_remove_stops_and_removes_openrc_and_autostart(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("rc-service", "rc-update", "pkill", "setsid"):
        world.bin.install(name)
    world.health([UP])
    assert world.run(capsys, "--method", "openrc", "--yes")[0] == 0
    assert world.run(capsys, "--method", "autostart", "--yes")[0] == 0
    code, _, _ = world.run(capsys, "--remove", "--yes")
    assert code == 0
    assert not world.path_of("openrc").exists() and not world.path_of("autostart").exists()
    assert world.calls("rc-service")[-1] == ["--user", "vigil", "stop"]
    assert world.calls("rc-update")[-1] == ["--user", "del", "vigil", "default"]
    assert world.calls("pkill") == [["-f", "--", "/vigil serve( |$)"]]


def test_remove_with_a_method_leaves_the_others(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.bin.install("setsid")
    world.bin.install("systemctl")
    assert world.run(capsys, "--method", "autostart", "--yes")[0] == 0
    assert world.run(capsys, "--method", "systemd", "--yes")[0] == 0
    assert world.run(capsys, "--remove", "--method", "systemd", "--yes")[0] == 0
    assert world.path_of("autostart").is_file()
    assert not world.path_of("systemd").exists()


def test_remove_with_nothing_installed(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = world.run(capsys, "--remove", "--yes")
    assert code == 0
    assert "Nothing that `vigil setup` installed" in out


def test_remove_asks_first_and_dry_run_changes_nothing(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.bin.install("systemctl")
    assert world.run(capsys, "--method", "systemd", "--yes")[0] == 0
    calls = len(world.calls("systemctl"))
    code, out, _ = world.run(capsys, "--remove", "--dry-run")
    assert code == 0 and "Dry run" in out
    world.answers("n")
    assert world.run(capsys, "--remove")[0] == 1
    assert world.path_of("systemd").is_file()
    assert len(world.calls("systemctl")) == calls


def test_remove_goes_on_when_the_service_would_not_stop(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.bin.install("systemctl")
    assert world.run(capsys, "--method", "systemd", "--yes")[0] == 0
    world.bin.install("systemctl", exit_code=1)
    code, out, _ = world.run(capsys, "--remove", "--yes")
    assert code == 0
    assert not world.path_of("systemd").exists()
    assert "Removed" in out


# The health check --------------------------------------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    body = b""
    status = 200

    def do_GET(self) -> None:
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@contextlib.contextmanager
def _serve(status: int, body: bytes) -> Iterator[int]:
    handler = type("Handler", (_Handler,), {"status": status, "body": body})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_probe_recognises_vigil() -> None:
    body = json.dumps({"status": "ok", "version": "9.9.9"}).encode()
    with _serve(200, body) as port:
        assert setup.probe_health(port) == Health("vigil", "9.9.9")


@pytest.mark.parametrize(
    ("status", "body"), [(404, b"{}"), (200, b"not json"), (200, b'{"status": "ok"}')]
)
def test_probe_says_other_for_what_is_not_vigil(status: int, body: bytes) -> None:
    with _serve(status, body) as port:
        assert setup.probe_health(port).state == "other"


def test_probe_says_free_for_a_closed_port() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert setup.probe_health(port).state == "free"


def test_probe_says_free_when_a_closed_port_does_not_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # WSL2 mirrored networking drops a connection to a closed loopback port instead of
    # refusing it, so the check times out; the port can still be bound, so it is free.
    def timeout(url: str, timeout: float) -> None:
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(setup._LOOPBACK_ONLY, "open", timeout)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert setup.probe_health(port).state == "free"


def test_probe_says_other_when_a_bound_port_does_not_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(url: str, timeout: float) -> None:
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(setup._LOOPBACK_ONLY, "open", timeout)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        assert setup.probe_health(sock.getsockname()[1]).state == "other"


def test_probe_ignores_a_proxy_in_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    body = json.dumps({"status": "ok", "version": "1"}).encode()
    with _serve(200, body) as port:
        assert setup.probe_health(port).state == "vigil"


def test_wait_polls_until_vigil_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = [FREE, FREE, UP]
    monkeypatch.setattr(setup, "probe_health", lambda port, timeout=1.0: answers.pop(0))
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    assert setup.wait_for_health(7787, timeout=5) == UP
    assert answers == []


def test_wait_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setup, "probe_health", lambda port, timeout=1.0: FREE)
    assert setup.wait_for_health(7787, timeout=0.0) is None


def test_the_real_vigil_executable_is_found_from_the_running_python() -> None:
    found = setup.vigil_executable()
    assert found is not None and found.is_absolute() and found.name == "vigil"
