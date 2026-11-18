import json
import os
import stat
from pathlib import Path

import pytest

from tests.conftest import VigilEnv
from vigil import cli, setup
from vigil.commands import doctor
from vigil.setup import Health

CLAUDE_OUT = "2.1.312 (Claude Code)\n  --bg  Start in the background\n  attach\n  agents\n"
CLAUDE_OLD = "1.0.0 (Claude Code)\n"
UP = Health("vigil", "0.1.0")


class Doctor:
    """A `vigil doctor` run in a healthy world: every check passes until a test breaks one."""

    def __init__(
        self, env: VigilEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: object
    ) -> None:
        self.env = env
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.capsys: pytest.CaptureFixture[str] = capsys  # type: ignore[assignment]
        self.home = tmp_path / "home"
        self.config_home = tmp_path / "xdg_config"
        self.vigil = self.home / ".local" / "bin" / "vigil"
        self.vigil.parent.mkdir(parents=True)
        self.vigil.write_text("#!/bin/sh\n")
        self.vigil.chmod(0o755)
        monkeypatch.setattr(setup, "vigil_executable", lambda: self.vigil)
        monkeypatch.setenv("PATH", f"{env.bin.directory}{os.pathsep}{self.vigil.parent}")
        monkeypatch.setenv("DISPLAY", ":0")
        env.bin.install("claude", stdout=CLAUDE_OUT)
        env.bin.install("xterm")
        self.port(UP)
        self.installed()

    def port(self, health: Health) -> None:
        self.monkeypatch.setattr(setup, "probe_health", lambda port, timeout=1.0: health)

    def installed(self, command: str | None = None) -> Path:
        """An autostart entry that runs `command` (this world's vigil by default)."""
        path = setup.entry_path("autostart", self.config_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"[Desktop Entry]\nExec={command or self.vigil} serve\n")
        return path

    def config(self, text: str) -> None:
        (self.config_home / "vigil" / "config.toml").write_text(text)

    def run(self, *argv: str) -> tuple[int, str]:
        code = cli.main(["doctor", *argv])
        return code, self.capsys.readouterr().out

    def checks(self) -> dict[str, doctor.Check]:
        code, out = self.run("--json")
        data = json.loads(out)
        assert data["ok"] is (code == 0)
        return {c["id"]: doctor.Check(**c) for c in data["checks"]}


@pytest.fixture
def healthy(
    vigil_env: VigilEnv,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> Doctor:
    return Doctor(vigil_env, tmp_path, monkeypatch, capsys)


def test_everything_fine_exits_0(healthy: Doctor) -> None:
    code, out = healthy.run()
    assert code == 0
    assert "PROBLEM" not in out and "warning" not in out
    lines = out.splitlines()
    assert lines[0].startswith("ok") and "Python 3." in lines[0]
    assert any("claude 2.1.312" in line and "--bg: yes, attach: yes" in line for line in lines)
    assert any("Resume opens xterm" in line for line in lines)
    assert any("answered by vigil 0.1.0" in line for line in lines)


def test_json_has_every_check_as_data(healthy: Doctor) -> None:
    code, out = healthy.run("--json")
    data = json.loads(out)
    assert code == 0 and data["ok"] is True
    assert [c["id"] for c in data["checks"]] == [
        "python",
        "vigil",
        "claude",
        "claude_home",
        "index",
        "config",
        "display",
        "terminal",
        "port",
        "autostart",
    ]
    assert all(set(c) == {"id", "status", "message", "fix"} for c in data["checks"])
    assert {c["status"] for c in data["checks"]} == {"ok"}


def test_doctor_changes_nothing(healthy: Doctor) -> None:
    def snapshot() -> dict[str, tuple[int, float]]:
        return {
            str(p): (p.stat().st_size, p.stat().st_mtime)
            for p in healthy.tmp_path.rglob("*")
            if p.is_file() and ".calls" not in p.parts
        }

    before = snapshot()
    assert healthy.run()[0] == 0
    assert snapshot() == before
    assert not (healthy.tmp_path / "xdg_data").exists()  # the index was not even created


# Each check, passing and failing -----------------------------------------------------------


def test_python_too_old(healthy: Doctor) -> None:
    healthy.monkeypatch.setattr(doctor, "_MIN_PYTHON", (3, 99))
    check = healthy.checks()["python"]
    assert check.status == "problem" and check.fix and "3.14" in check.message + check.fix
    assert healthy.run()[0] == 1


def test_vigil_folder_not_on_path_is_a_warning(healthy: Doctor) -> None:
    healthy.monkeypatch.setenv("PATH", str(healthy.env.bin.directory))
    check = healthy.checks()["vigil"]
    assert check.status == "warning"
    assert check.fix is not None and f'export PATH="{healthy.vigil.parent}:$PATH"' in check.fix
    assert healthy.run()[0] == 0  # a warning is not a problem


def test_vigil_not_found_is_a_warning(healthy: Doctor) -> None:
    healthy.monkeypatch.setattr(setup, "vigil_executable", lambda: None)
    assert healthy.checks()["vigil"].status == "warning"


def test_claude_missing_is_a_problem(healthy: Doctor) -> None:
    (healthy.env.bin.directory / "claude").unlink()
    code, out = healthy.run()
    assert code == 1
    assert "claude was not found on PATH" in out and "fix:" in out


def test_claude_that_will_not_run_is_a_problem(healthy: Doctor) -> None:
    healthy.env.bin.install("claude", exit_code=1)
    check = healthy.checks()["claude"]
    assert check.status == "problem" and "--version" in check.message


def test_claude_without_bg_or_attach_is_a_warning(healthy: Doctor) -> None:
    healthy.env.bin.install("claude", stdout=CLAUDE_OLD)
    check = healthy.checks()["claude"]
    assert check.status == "warning"
    assert "--bg: no, attach: no" in check.message
    assert healthy.run()[0] == 0


def test_claude_home_missing_and_unreadable(healthy: Doctor) -> None:
    gone = healthy.tmp_path / "gone"
    healthy.config(f'claude_home = "{gone}"\n')
    check = healthy.checks()["claude_home"]
    assert check.status == "problem" and "does not exist" in check.message
    gone.mkdir()
    gone.chmod(0)
    try:
        if os.access(gone, os.R_OK):  # running as someone who ignores modes
            pytest.skip("permissions are not enforced")
        check = healthy.checks()["claude_home"]
        assert check.status == "problem" and "not readable" in check.message
    finally:
        gone.chmod(stat.S_IRWXU)
    assert healthy.checks()["claude_home"].status == "ok"


def test_index_folder_unwritable_is_a_problem(healthy: Doctor) -> None:
    data = healthy.tmp_path / "xdg_data"
    data.mkdir()
    data.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        if os.access(data, os.W_OK):
            pytest.skip("permissions are not enforced")
        check = healthy.checks()["index"]
        assert check.status == "problem" and check.fix
        assert healthy.run()[0] == 1
    finally:
        data.chmod(stat.S_IRWXU)
    assert healthy.checks()["index"].status == "ok"


def test_existing_index_is_checked_itself(healthy: Doctor) -> None:
    index = healthy.env.index
    index.parent.mkdir(parents=True)
    index.write_text("")
    index.chmod(stat.S_IRUSR)
    try:
        if os.access(index, os.W_OK):
            pytest.skip("permissions are not enforced")
        assert healthy.checks()["index"].status == "problem"
    finally:
        index.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_broken_config_is_a_problem_line_not_the_end_of_the_run(healthy: Doctor) -> None:
    healthy.config("nope = 1\n")
    code, out = healthy.run()
    assert code == 1
    assert "the config does not load: unknown config key: nope" in out
    assert "Python 3." in out and "claude 2.1.312" in out  # the other checks still ran
    assert "not checked: the config did not load" in out
    assert "using the defaults" in out


def test_broken_toml_is_reported_too(healthy: Doctor) -> None:
    healthy.config("this is not toml\n")
    check = healthy.checks()["config"]
    assert check.status == "problem" and "cannot read config" in check.message


def test_other_commands_still_stop_on_a_broken_config(
    healthy: Doctor, capsys: pytest.CaptureFixture[str]
) -> None:
    healthy.config("nope = 1\n")
    assert cli.main(["ls"]) == 2
    assert "unknown config key: nope" in capsys.readouterr().err


def test_commands_load_the_config_unless_they_say_otherwise() -> None:
    others = [c for c in cli.COMMANDS if c.name != "doctor"]
    assert not any(getattr(c, "loads_own_config", False) for c in others)
    assert doctor.COMMAND.loads_own_config is True


def test_no_config_file_is_fine(healthy: Doctor) -> None:
    (healthy.config_home / "vigil" / "config.toml").unlink()
    assert healthy.checks()["config"].status == "ok"


def test_no_display_is_a_warning_and_names_why_no_terminal(healthy: Doctor) -> None:
    healthy.monkeypatch.delenv("DISPLAY")
    checks = healthy.checks()
    assert checks["display"].status == "warning"
    assert checks["terminal"].status == "warning"
    assert "no display" in checks["terminal"].message
    assert healthy.run()[0] == 0


def test_display_without_a_terminal_says_so(healthy: Doctor) -> None:
    (healthy.env.bin.directory / "xterm").unlink()
    check = healthy.checks()["terminal"]
    assert check.status == "warning" and "none of the known ones" in check.message


def test_a_configured_terminal_is_used(healthy: Doctor) -> None:
    healthy.config('[resume]\nterminal_command = "myterm -e {attach}"\n')
    assert "Resume opens myterm" in healthy.checks()["terminal"].message


def test_a_terminal_template_that_will_not_split_is_a_problem(healthy: Doctor) -> None:
    healthy.config('[resume]\nterminal_command = "term \\"{attach}"\n')
    check = healthy.checks()["terminal"]
    assert check.status == "problem" and "terminal_command" in check.message


def test_port_free_and_port_taken(healthy: Doctor) -> None:
    healthy.port(Health("free"))
    check = healthy.checks()["port"]
    assert check.status == "ok" and "is free" in check.message
    healthy.port(Health("other"))
    check = healthy.checks()["port"]
    assert check.status == "problem" and check.fix and "`port`" in check.fix
    assert healthy.run()[0] == 1


def test_the_configured_port_is_the_one_asked(healthy: Doctor) -> None:
    asked: list[int] = []

    def probe(port: int, timeout: float = 1.0) -> Health:
        asked.append(port)
        return UP

    healthy.config("port = 7801\n")
    healthy.monkeypatch.setattr(setup, "probe_health", probe)
    healthy.run()
    assert asked == [7801]


# The autostart entry or service ------------------------------------------------------------


def test_no_startup_entry_is_a_warning(healthy: Doctor) -> None:
    setup.entry_path("autostart", healthy.config_home).unlink()
    check = healthy.checks()["startup"]
    assert check.status == "warning" and check.fix == "Run `vigil setup`."
    assert healthy.run()[0] == 0


def test_entry_whose_path_is_gone_is_a_problem(healthy: Doctor) -> None:
    healthy.installed("/nowhere/bin/vigil")
    check = healthy.checks()["autostart"]
    assert check.status == "problem" and "does not exist" in check.message
    assert check.fix is not None and "vigil setup" in check.fix
    assert healthy.run()[0] == 1


def test_entry_with_a_bare_command_is_a_warning(healthy: Doctor) -> None:
    healthy.installed("vigil")
    check = healthy.checks()["autostart"]
    assert check.status == "warning" and "without a folder" in check.message


def test_quoted_path_with_a_space(healthy: Doctor) -> None:
    spaced = healthy.tmp_path / "a b" / "vigil"
    spaced.parent.mkdir()
    spaced.write_text("#!/bin/sh\n")
    spaced.chmod(0o755)
    healthy.installed(f'"{spaced}"')
    assert healthy.checks()["autostart"].status == "ok"


def test_systemd_unit_written_by_setup_is_checked(healthy: Doctor) -> None:
    unit = setup.entry_path("systemd", healthy.config_home)
    unit.parent.mkdir(parents=True)
    unit.write_text(setup.render("systemd", healthy.vigil, None, healthy.home))
    assert healthy.checks()["systemd"].status == "ok"
    unit.write_text(setup.render("systemd", Path("/nowhere/vigil"), None, healthy.home))
    assert healthy.checks()["systemd"].status == "problem"


def test_systemd_unit_with_percent_h(healthy: Doctor) -> None:
    unit = setup.entry_path("systemd", healthy.config_home)
    unit.parent.mkdir(parents=True)
    unit.write_text(setup.template("systemd"))  # ExecStart=%h/.local/bin/vigil serve
    healthy.monkeypatch.setenv("HOME", str(healthy.home))
    assert healthy.checks()["systemd"].status == "ok"
    assert str(healthy.home) in healthy.checks()["systemd"].message


def test_openrc_script_and_its_conf_override(healthy: Doctor) -> None:
    script = setup.entry_path("openrc", healthy.config_home)
    script.parent.mkdir(parents=True)
    script.write_text(setup.render("openrc", Path("/nowhere/vigil"), None, healthy.home))
    check = healthy.checks()["openrc"]
    assert check.status == "problem" and "/nowhere/vigil" in check.message
    conf = healthy.config_home / "rc" / "conf.d" / "vigil"
    conf.parent.mkdir(parents=True)
    conf.write_text(f'vigil_bin="{healthy.vigil}"\n')
    assert healthy.checks()["openrc"].status == "ok"


def test_openrc_script_from_contrib_uses_home(healthy: Doctor) -> None:
    script = setup.entry_path("openrc", healthy.config_home)
    script.parent.mkdir(parents=True)
    script.write_text(setup.template("openrc"))
    healthy.monkeypatch.setenv("HOME", str(healthy.home))
    check = healthy.checks()["openrc"]
    assert check.status == "ok" and str(healthy.vigil) in check.message


def test_each_installed_entry_is_reported(healthy: Doctor) -> None:
    unit = setup.entry_path("systemd", healthy.config_home)
    unit.parent.mkdir(parents=True)
    unit.write_text(setup.render("systemd", healthy.vigil, None, healthy.home))
    assert {"autostart", "systemd"} <= set(healthy.checks())


def test_every_problem_line_says_how_to_fix_it(healthy: Doctor) -> None:
    (healthy.env.bin.directory / "claude").unlink()
    healthy.config(f'claude_home = "{healthy.tmp_path / "gone"}"\n')
    healthy.port(Health("other"))
    healthy.installed("/nowhere/vigil")
    code, out = healthy.run()
    assert code == 1
    lines = out.splitlines()
    problems = [i for i, line in enumerate(lines) if line.startswith("PROBLEM")]
    assert len(problems) == 4
    assert all(lines[i + 1].strip().startswith("fix:") for i in problems)
