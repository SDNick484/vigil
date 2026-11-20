import re
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.conftest import FakeBin, VigilEnv
from vigil import __version__, cli, setup, upgrade
from vigil.setup import MARKER, Health, Method
from vigil.upgrade import Install, UpgradeRefused

SOURCE = "git+https://github.com/SDNick484/vigil@"
NEW = "vigil-sessions 9.9.9"
UP = Health("vigil", "9.9.9")
FREE = Health("free")


class Upgrade:
    """A `vigil upgrade` run's world: a prefix under tmp that `sys.prefix` points at, a fake
    interpreter as `sys.executable` (it records `-m pip ...` and prints a version for
    `-m vigil --version`), fakes on PATH, and a health answer for the port."""

    def __init__(
        self, env: VigilEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: Path
    ) -> None:
        self.env = env
        self.bin: FakeBin = env.bin
        self.monkeypatch = monkeypatch
        self.home = home
        self.prefix = tmp_path / "venv"
        self.prefix.mkdir()
        self.real_python = sys.executable
        self.python = self.install("fakepython", stdout=f"{NEW}\n")
        monkeypatch.setattr(sys, "executable", str(self.python))
        monkeypatch.setattr(sys, "prefix", str(self.prefix))
        monkeypatch.setattr(upgrade, "GENTOO_RELEASE", tmp_path / "no-gentoo-release")
        self.vigil = tmp_path / "bin" / "vigil"
        self.vigil.parent.mkdir()
        self.vigil.write_text("#!/bin/sh\n")
        self.vigil.chmod(0o755)
        monkeypatch.setattr(setup, "vigil_executable", lambda: self.vigil)
        self.health([UP])

    def install(self, name: str, stdout: str = "", exit_code: int = 0) -> Path:
        """A fake program on PATH. Its shebang is the real interpreter, not the fake one that
        `sys.executable` is while a test runs."""
        with self.monkeypatch.context() as patch:
            patch.setattr(sys, "executable", self.real_python)
            return self.bin.install(name, stdout=stdout, exit_code=exit_code)

    def health(self, states: list[Health]) -> None:
        remaining = list(states)

        def probe(port: int, timeout: float = 1.0) -> Health:
            return remaining.pop(0) if len(remaining) > 1 else remaining[0]

        self.monkeypatch.setattr(setup, "probe_health", probe)

    def marker(self, text: str = 'ref = "v1"\nsource = "install.sh"\n') -> Path:
        path = self.prefix / upgrade.MARKER_NAME
        path.write_text(text)
        return path

    def service(self, method: Method) -> Path:
        path = setup.entry_path(method, Path(self.env.index.parent.parent.parent / "xdg_config"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"[Unit]\n{MARKER}\n")
        return path

    def run(self, capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
        code = cli.main(["upgrade", *argv])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    def python_calls(self) -> list[list[str]]:
        return [call.argv for call in self.bin.calls("fakepython")]


@pytest.fixture
def world(vigil_env: VigilEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Upgrade:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.delenv("XDG_CURRENT_DESKTOP", raising=False)
    monkeypatch.delenv("PIPX_HOME", raising=False)
    return Upgrade(vigil_env, tmp_path, monkeypatch, home)


# Detection ---------------------------------------------------------------------------------


def test_marker_in_the_prefix_means_script(tmp_path: Path) -> None:
    (tmp_path / "vigil-install.toml").write_text('ref = "v0.2"\nsource = "install.sh"\n')
    found = upgrade.detect_install(prefix=tmp_path, base_prefix=Path("/usr"), home=tmp_path)
    assert found == Install("script", tmp_path, "v0.2")


def test_script_with_an_unreadable_or_bad_marker_has_no_ref(tmp_path: Path) -> None:
    marker = tmp_path / "vigil-install.toml"
    marker.write_text("this is = = not toml")
    assert upgrade.detect_install(prefix=tmp_path, home=tmp_path).ref is None
    marker.write_text('ref = "--upload-pack=x"\n')
    assert upgrade.detect_install(prefix=tmp_path, home=tmp_path) == Install(
        "script", tmp_path, None
    )


@pytest.mark.parametrize("folder", [".local/share/pipx/venvs", ".local/pipx/venvs"])
def test_prefix_under_a_pipx_venvs_folder_means_pipx(tmp_path: Path, folder: str) -> None:
    prefix = tmp_path / folder / "vigil-sessions"
    prefix.mkdir(parents=True)
    found = upgrade.detect_install(prefix=prefix, env={}, home=tmp_path)
    assert found == Install("pipx", prefix, None)


def test_pipx_home_is_honoured(tmp_path: Path) -> None:
    prefix = tmp_path / "elsewhere" / "venvs" / "vigil-sessions"
    prefix.mkdir(parents=True)
    env = {"PIPX_HOME": str(tmp_path / "elsewhere")}
    assert upgrade.detect_install(prefix=prefix, env=env, home=tmp_path).kind == "pipx"
    assert upgrade.detect_install(prefix=prefix, env={}, home=tmp_path).kind == "venv"


@pytest.mark.parametrize("prefix", ["/usr", "/usr/local"])
def test_usr_without_a_venv_means_system(tmp_path: Path, prefix: str) -> None:
    found = upgrade.detect_install(prefix=Path(prefix), base_prefix=Path(prefix), home=tmp_path)
    assert found == Install("system", Path(prefix), None)


def test_a_venv_based_on_usr_is_not_system(tmp_path: Path) -> None:
    prefix = tmp_path / "env"
    prefix.mkdir()
    found = upgrade.detect_install(prefix=prefix, base_prefix=Path("/usr"), home=tmp_path)
    assert found == Install("venv", prefix, None)


def test_usr_as_a_venv_is_not_system(tmp_path: Path) -> None:
    found = upgrade.detect_install(prefix=Path("/usr"), base_prefix=Path("/opt/py"), home=tmp_path)
    assert found.kind == "venv"


# Plans -------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["script", "venv"])
def test_script_and_venv_plan_a_pip_upgrade(world: Upgrade, kind: str) -> None:
    install = Install(kind, world.prefix, None)  # type: ignore[arg-type]
    assert upgrade.plan_upgrade(install, None) == [
        [str(world.python), "-m", "pip", "install", "--upgrade", f"{SOURCE}main"]
    ]


def test_pipx_plans_a_forced_reinstall_with_this_python(world: Upgrade) -> None:
    install = Install("pipx", world.prefix, None)
    assert upgrade.plan_upgrade(install, "v2") == [
        ["pipx", "install", "--force", "--python", str(world.python), f"{SOURCE}v2"]
    ]


def test_ref_precedence_is_flag_then_marker_then_main(world: Upgrade) -> None:
    marked = Install("script", world.prefix, "from-marker")
    assert upgrade.resolve_ref(marked, "from-flag") == "from-flag"
    assert upgrade.resolve_ref(marked, None) == "from-marker"
    assert upgrade.resolve_ref(Install("script", world.prefix, None), None) == "main"
    assert upgrade.resolve_ref(Install("venv", world.prefix, None), None) == "main"


@pytest.mark.parametrize("ref", ["-e", "--upload-pack=x", "a b", "a;b", "a$b", "a@b", "x?y"])
def test_a_ref_that_is_not_a_git_name_is_refused(world: Upgrade, ref: str) -> None:
    with pytest.raises(UpgradeRefused, match="not a git branch"):
        upgrade.plan_upgrade(Install("venv", world.prefix, None), ref)


def test_system_install_is_refused_naming_emerge_on_gentoo(
    world: Upgrade, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = tmp_path / "gentoo-release"
    release.write_text("Gentoo Base System release 2.17\n")
    monkeypatch.setattr(upgrade, "GENTOO_RELEASE", release)
    with pytest.raises(UpgradeRefused, match=re.escape("emerge -u dev-util/vigil")):
        upgrade.plan_upgrade(Install("system", Path("/usr"), None), None)


def test_system_install_elsewhere_names_the_package_manager(world: Upgrade) -> None:
    with pytest.raises(UpgradeRefused, match="your package manager") as raised:
        upgrade.plan_upgrade(Install("system", Path("/usr"), None), None)
    assert "emerge" not in str(raised.value)


# The marker --------------------------------------------------------------------------------


def test_record_install_updates_ref_and_time_and_keeps_the_format(world: Upgrade) -> None:
    path = world.marker(
        "# Written by install.sh; `vigil upgrade` reads it.\n"
        'ref = "v1"\nsource = "install.sh"\ninstalled_at = "2026-01-01T00:00:00Z"\n'
    )
    install = upgrade.detect_install(prefix=world.prefix)
    when = datetime(2026, 11, 19, 17, 28, 0, tzinfo=UTC)
    assert upgrade.record_install(install, "v2", when)
    text = path.read_text()
    assert text.startswith("# Written by install.sh")
    assert 'ref = "v2"' in text
    assert 'installed_at = "2026-11-19T17:28:00Z"' in text
    assert tomllib.loads(text) == {
        "ref": "v2",
        "source": "install.sh",
        "installed_at": "2026-11-19T17:28:00Z",
    }
    assert [p.name for p in world.prefix.iterdir()] == ["vigil-install.toml"]


def test_record_install_does_nothing_without_a_marker_install(world: Upgrade) -> None:
    assert not upgrade.record_install(Install("venv", world.prefix, None), "v2")
    assert list(world.prefix.iterdir()) == []


# The command -------------------------------------------------------------------------------


def test_upgrade_of_a_script_install_runs_pip_records_the_ref_and_prints_versions(
    world: Upgrade, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = world.marker()
    code, out, _ = world.run(capsys, "--no-restart")
    assert code == 0
    assert world.python_calls() == [
        ["-m", "pip", "install", "--upgrade", f"{SOURCE}v1"],  # the marker's ref
        ["-m", "vigil", "--version"],
    ]
    assert f"Version before: vigil-sessions {__version__}" in out
    assert f"Version after:  {NEW}" in out
    values = tomllib.loads(marker.read_text())
    assert values["ref"] == "v1" and values["source"] == "install.sh"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", str(values["installed_at"]))


def test_ref_flag_wins_over_the_marker_and_is_recorded(
    world: Upgrade, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = world.marker()
    code, _, _ = world.run(capsys, "--ref", "feature/x", "--no-restart")
    assert code == 0
    assert world.python_calls()[0][-1] == f"{SOURCE}feature/x"
    assert tomllib.loads(marker.read_text())["ref"] == "feature/x"


def test_a_plain_venv_defaults_to_main_and_gets_no_marker(
    world: Upgrade, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, _ = world.run(capsys, "--no-restart")
    assert code == 0
    assert world.python_calls()[0][-1] == f"{SOURCE}main"
    assert list(world.prefix.iterdir()) == []


def test_pipx_install_runs_pipx(
    world: Upgrade, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    prefix = world.home / ".local" / "share" / "pipx" / "venvs" / "vigil-sessions"
    prefix.mkdir(parents=True)
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setenv("HOME", str(world.home))
    world.install("pipx")
    code, _, _ = world.run(capsys, "--no-restart")
    assert code == 0
    assert [c.argv for c in world.bin.calls("pipx")] == [
        ["install", "--force", "--python", str(world.python), f"{SOURCE}main"]
    ]
    assert world.python_calls() == [["-m", "vigil", "--version"]]  # no pip


def test_system_install_is_refused_and_nothing_runs(
    world: Upgrade,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    release = tmp_path / "gentoo-release"
    release.write_text("Gentoo\n")
    monkeypatch.setattr(upgrade, "GENTOO_RELEASE", release)
    monkeypatch.setattr(upgrade, "detect_install", lambda: Install("system", Path("/usr"), None))
    code, out, err = world.run(capsys)
    assert code == 1
    assert "emerge -u dev-util/vigil" in err
    assert world.python_calls() == []
    assert "Version" not in out


def test_dry_run_runs_nothing_and_writes_nothing(
    world: Upgrade, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = world.marker()
    before = marker.read_text()
    world.install("systemctl")
    world.service("systemd")
    code, out, _ = world.run(capsys, "--dry-run")
    assert code == 0
    assert f"would run: {world.python} -m pip install --upgrade {SOURCE}v1" in out
    assert "would run: systemctl --user restart vigil.service" in out
    assert "Dry run" in out
    assert world.python_calls() == []
    assert world.bin.calls("systemctl") == []
    assert marker.read_text() == before


def test_failed_pip_stops_without_touching_the_marker_or_restarting(
    world: Upgrade, capsys: pytest.CaptureFixture[str]
) -> None:
    world.install("fakepython", exit_code=3)
    marker = world.marker()
    before = marker.read_text()
    world.install("systemctl")
    world.service("systemd")
    code, _, err = world.run(capsys, "--ref", "other")
    assert code == 1
    assert "exited with status 3" in err
    assert marker.read_text() == before
    assert world.bin.calls("systemctl") == []


def test_a_new_vigil_that_does_not_start_is_not_restarted(
    world: Upgrade, capsys: pytest.CaptureFixture[str]
) -> None:
    world.install("systemctl")
    world.service("systemd")

    # pip succeeds, `-m vigil --version` fails
    script = world.python.read_text().replace(
        "sys.stdout.write(",
        "sys.exit(1 if sys.argv[1:3] == ['-m', 'vigil'] else 0)\nsys.stdout.write(",
    )
    world.python.write_text(script)
    code, _, err = world.run(capsys)
    assert code == 1
    assert "does not start" in err
    assert world.bin.calls("systemctl") == []


# Restart -----------------------------------------------------------------------------------


def test_systemd_service_is_restarted_with_setups_steps(
    world: Upgrade, capsys: pytest.CaptureFixture[str]
) -> None:
    world.install("systemctl")
    world.service("systemd")
    code, out, _ = world.run(capsys)
    assert code == 0
    assert [c.argv for c in world.bin.calls("systemctl")] == [
        ["--user", "daemon-reload"],
        ["--user", "enable", "--now", "vigil.service"],
        ["--user", "restart", "vigil.service"],
    ]
    assert "vigil 9.9.9 is running" in out


def test_with_two_service_files_the_recommended_method_is_restarted(
    world: Upgrade, capsys: pytest.CaptureFixture[str]
) -> None:
    world.install("systemctl")  # works, and there is no desktop: systemd is recommended
    world.install("pkill")
    world.install("setsid")
    world.service("autostart")
    world.service("systemd")
    code, _, _ = world.run(capsys)
    assert code == 0
    assert ["--user", "restart", "vigil.service"] in [c.argv for c in world.bin.calls("systemctl")]
    assert world.bin.calls("setsid") == []


def test_openrc_service_is_restarted(world: Upgrade, capsys: pytest.CaptureFixture[str]) -> None:
    world.install("rc-service", stdout="rc-service (OpenRC) 0.62\n")
    world.install("rc-update")
    world.service("openrc")
    code, _, _ = world.run(capsys)
    assert code == 0
    calls = [c.argv for c in world.bin.calls("rc-service")]
    assert ["--user", "vigil", "restart"] in calls
    assert world.bin.calls("rc-update")[0].argv == ["--user", "add", "vigil", "default"]


def test_autostart_running_vigil_is_stopped_then_started(
    world: Upgrade, capsys: pytest.CaptureFixture[str]
) -> None:
    world.install("pkill")
    world.install("setsid")
    world.service("autostart")
    # Answers before the stop, "gone" after it, up after the start.
    world.health([UP, FREE, UP])
    code, out, _ = world.run(capsys)
    assert code == 0
    assert world.bin.calls("pkill")[0].argv == ["-f", "--", "/vigil serve( |$)"]
    assert world.bin.calls("setsid")[0].argv == ["-f", str(world.vigil), "serve"]
    assert "is running" in out


def test_autostart_not_running_is_only_started(
    world: Upgrade, capsys: pytest.CaptureFixture[str]
) -> None:
    world.install("pkill")
    world.install("setsid")
    world.service("autostart")
    world.health([FREE, UP])
    code, _, _ = world.run(capsys)
    assert code == 0
    assert world.bin.calls("pkill") == []
    assert len(world.bin.calls("setsid")) == 1


def test_a_service_file_setup_did_not_write_is_not_touched(
    world: Upgrade, capsys: pytest.CaptureFixture[str]
) -> None:
    world.install("systemctl")
    path = world.service("systemd")
    path.write_text("[Unit]\nDescription=mine\n")
    code, out, _ = world.run(capsys)
    assert code == 0
    assert "not running as a service" in out
    assert world.bin.calls("systemctl") == []


def test_no_service_installed_says_so_and_stops(
    world: Upgrade, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = world.run(capsys)
    assert code == 0
    assert "vigil is not running as a service" in out
    assert world.python_calls()[0][:3] == ["-m", "pip", "install"]


def test_no_restart_skips_the_service(world: Upgrade, capsys: pytest.CaptureFixture[str]) -> None:
    world.install("systemctl")
    world.service("systemd")
    code, out, _ = world.run(capsys, "--no-restart")
    assert code == 0
    assert world.bin.calls("systemctl") == []
    assert "Not restarting" in out


def test_service_that_does_not_come_back_is_an_error(
    world: Upgrade, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(setup, "HEALTH_WAIT", 0.0)
    world.install("systemctl")
    world.service("systemd")
    world.health([FREE])
    code, _, err = world.run(capsys)
    assert code == 1
    assert "did not answer" in err


def test_a_failing_restart_step_is_an_error(
    world: Upgrade, capsys: pytest.CaptureFixture[str]
) -> None:
    world.install("systemctl", exit_code=1)
    world.service("systemd")
    code, _, err = world.run(capsys)
    assert code == 1
    assert "could not" in err
