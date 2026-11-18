"""`vigil doctor [--json]`: check that vigil can work here and say how to fix what cannot.

Read-only: it looks at files, runs `claude --version` and `--help`, and asks 127.0.0.1 for
`/api/v1/health`. It changes nothing. Exit status 0 when no check is a problem, else 1.
"""

import argparse
import json
import os
import re
import shlex
import shutil
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from vigil import claude_cli, paths, setup
from vigil.config import Config, ConfigError, load_config
from vigil.paths import config_home
from vigil.terminal import is_wsl, terminal_argv

Status = Literal["ok", "warning", "problem"]
_MARK: dict[Status, str] = {"ok": "ok     ", "warning": "warning", "problem": "PROBLEM"}
_MIN_PYTHON = (3, 14)
_FALLBACK_NOTE = " (using the defaults: the config did not load)"


@dataclass(frozen=True)
class Check:
    id: str
    status: Status
    message: str
    fix: str | None = None
    """How to put it right; every problem has one, a warning may."""


def _python() -> Check:
    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info[:2] >= _MIN_PYTHON:
        return Check("python", "ok", f"Python {version}")
    return Check(
        "python",
        "problem",
        f"Python {version}; vigil needs 3.14 or newer",
        "Install Python 3.14 (on Gentoo: emerge --ask dev-lang/python:3.14) and reinstall vigil "
        "with it (see Install in the README).",
    )


def _location(env: Mapping[str, str]) -> Check:
    vigil_bin = setup.vigil_executable()
    if vigil_bin is None:
        return Check(
            "vigil",
            "warning",
            "cannot tell where vigil is installed",
            "Run `vigil doctor` from the installed `vigil` command.",
        )
    folder = vigil_bin.parent
    if setup.on_path(folder, env):
        return Check("vigil", "ok", f"vigil is {vigil_bin}, in a folder on PATH")
    return Check(
        "vigil",
        "warning",
        f"vigil is {vigil_bin}, but {folder} is not on PATH",
        f'Add this line to ~/.bashrc (or your shell\'s equivalent): export PATH="{folder}:$PATH"',
    )


def _claude(env: Mapping[str, str]) -> list[Check]:
    folder = setup.claude_folder(env)
    if folder is None:
        return [
            Check(
                "claude",
                "problem",
                "claude was not found on PATH, so sessions cannot be resumed",
                "Install Claude Code, or add its folder to PATH (`command -v claude` in a "
                "terminal where it works tells you where it is). Indexing works without it.",
            )
        ]
    found = os.path.join(folder, "claude")
    caps = claude_cli.capabilities(found)
    if caps.version is None:
        return [
            Check(
                "claude",
                "problem",
                f"{found} did not answer `--version`",
                "Run `claude --version` yourself to see what is wrong with it.",
            )
        ]
    bg, attach = ("yes" if flag else "no" for flag in (caps.has_bg, caps.has_attach))
    message = f"claude {caps.version} at {found} (--bg: {bg}, attach: {attach})"
    if caps.has_bg and caps.has_attach:
        return [Check("claude", "ok", message)]
    return [
        Check(
            "claude",
            "warning",
            message,
            "This claude lacks `--bg` or `attach`; Resume falls back to tmux or shows the "
            "command. Upgrade Claude Code for the full Resume.",
        )
    ]


def _not_checked(check_id: str) -> Check:
    return Check(
        check_id,
        "warning",
        "not checked: the config did not load, and it may set this",
        "Fix the config first (see the config line).",
    )


def _claude_home(config: Config) -> Check:
    home = config.claude_home
    if not home.is_dir():
        return Check(
            "claude_home",
            "problem",
            f"{home} does not exist",
            "Run Claude Code once, or set `claude_home` in the vigil config.",
        )
    if not os.access(home, os.R_OK | os.X_OK):
        return Check(
            "claude_home",
            "problem",
            f"{home} is not readable",
            f"Give your user read access: chmod u+rx {shlex.quote(str(home))}",
        )
    return Check("claude_home", "ok", f"Claude home {home} is readable")


def _writable_target(path: Path) -> Path:
    """`path`, or the nearest folder above it that exists (where vigil would create it)."""
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def _index() -> Check:
    folder = paths.data_dir()
    target = _writable_target(folder)
    index = paths.index_path()
    if index.exists():
        target = index
    if os.access(target, os.W_OK):
        return Check("index", "ok", f"index folder {folder} is writable")
    return Check(
        "index",
        "problem",
        f"{target} is not writable, so vigil cannot keep its index",
        f"Make it yours: chown -R $USER {shlex.quote(str(target))} (as the owner), or set "
        "XDG_DATA_HOME to a folder you own.",
    )


def _config() -> tuple[Config | None, Check]:
    path = paths.config_path()
    try:
        config = load_config()
    except ConfigError as error:
        return None, Check(
            "config",
            "problem",
            f"the config does not load: {error}",
            f"Fix or remove {path}; Configuration in the README lists every key.",
        )
    if path.is_file():
        return config, Check("config", "ok", f"config {path} loads")
    return config, Check("config", "ok", f"no config file ({path}); the defaults apply")


def _display_and_terminal(config: Config, env: Mapping[str, str], note: str) -> list[Check]:
    display = env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")
    checks = [
        Check("display", "ok", f"a display is available ({display})")
        if display
        else Check(
            "display",
            "warning",
            "no display is visible to this process",
            "Normal for a service and over SSH. Resume then starts the session in the "
            "background and shows the `claude attach` command to run; use XDG autostart "
            "(`vigil setup --method autostart`) for windows.",
        )
    ]
    try:
        argv = terminal_argv(["claude", "attach", "00000000"], str(Path.home()), config.resume, env)
    except ValueError as error:
        checks.append(
            Check(
                "terminal",
                "problem",
                f"`resume.terminal_command` is not usable: {error}",
                "Fix `resume.terminal_command` in the vigil config (it needs {attach}).",
            )
        )
        return checks
    if argv is not None:
        checks.append(Check("terminal", "ok", f"Resume opens {argv[0]}{note}"))
    elif not display and not is_wsl(env):
        checks.append(
            Check(
                "terminal",
                "warning",
                "Resume opens no terminal: there is no display",
                "Run the command Resume shows, or start vigil from a desktop session.",
            )
        )
    else:
        checks.append(
            Check(
                "terminal",
                "warning",
                "Resume opens no terminal: none of the known ones is on PATH",
                "Install one (xterm, for example) or set `resume.terminal_command` in the "
                "vigil config.",
            )
        )
    return checks


def _port(port: int) -> Check:
    health = setup.probe_health(port)
    if health.state == "vigil":
        return Check("port", "ok", f"port {port} is answered by vigil {health.version}")
    if health.state == "free":
        return Check("port", "ok", f"port {port} is free (vigil is not running)")
    return Check(
        "port",
        "problem",
        f"port {port} is in use by something that is not vigil",
        "Stop that program, or set `port` in the vigil config.",
    )


# What the startup files say vigil is, as a path (first word of the command).
_PREFIXES: dict[setup.Method, str] = {
    "autostart": "Exec=",
    "systemd": "ExecStart=",
    "openrc": ': "${vigil_bin:=',
}
_OPENRC_BIN = re.compile(r'^\s*vigil_bin\s*=\s*"?([^"\n]+)"?\s*$', re.MULTILINE)


def _command_path(method: setup.Method, text: str, conf: str | None) -> str | None:
    """The program a startup file runs, as written there with `~`, `%h` and `${HOME}` expanded."""
    value: str | None = None
    for line in text.splitlines():
        if line.startswith(_PREFIXES[method]):
            value = line[len(_PREFIXES[method]) :]
            break
    if value is None:
        return None
    if method == "openrc":
        value = value.rsplit("}", 1)[0]
        if conf is not None and (override := _OPENRC_BIN.search(conf)):
            value = override[1]
    try:
        words = shlex.split(value)
    except ValueError:
        return None
    if not words:
        return None
    home = str(Path.home())
    return os.path.expanduser(words[0].replace("%h", home).replace("${HOME}", home))


def _startup(config_dir: Path) -> list[Check]:
    checks: list[Check] = []
    for method in setup.METHODS:
        path = setup.entry_path(method, config_dir)
        if not path.is_file():
            continue
        name = f"{method} ({path})"
        try:
            text = path.read_text()
            conf_path = config_dir / "rc" / "conf.d" / "vigil"
            conf = conf_path.read_text() if method == "openrc" and conf_path.is_file() else None
        except OSError as error:
            checks.append(Check(method, "problem", f"cannot read {name}: {error.strerror}"))
            continue
        command = _command_path(method, text, conf)
        if command is None:
            checks.append(
                Check(
                    method,
                    "warning",
                    f"{name} is installed, but its command could not be read",
                    "Run `vigil setup` to write it again.",
                )
            )
        elif not os.path.isabs(command):
            found = shutil.which(command)
            checks.append(
                Check(
                    method,
                    "warning",
                    f"{name} runs `{command}` without a folder, so it depends on the PATH of "
                    "the session that starts it" + ("" if found else " (not on this PATH)"),
                    "Run `vigil setup` to write the full path.",
                )
            )
        elif os.path.isfile(command) and os.access(command, os.X_OK):
            checks.append(Check(method, "ok", f"{name} runs {command}, which exists"))
        else:
            checks.append(
                Check(
                    method,
                    "problem",
                    f"{name} runs {command}, which does not exist",
                    "Run `vigil setup` to write the path of the vigil that is installed now.",
                )
            )
    if not checks:
        checks.append(
            Check(
                "startup",
                "warning",
                "no autostart entry or user service is installed; vigil does not start by itself",
                "Run `vigil setup`.",
            )
        )
    return checks


def run_checks(env: Mapping[str, str]) -> list[Check]:
    """Every check, in the order they are shown."""
    config, config_check = _config()
    note = "" if config is not None else _FALLBACK_NOTE
    settings = config if config is not None else Config()
    checks = [_python(), _location(env), *_claude(env)]
    checks.append(_claude_home(settings) if config is not None else _not_checked("claude_home"))
    checks.append(_index())
    checks.append(config_check)
    checks += _display_and_terminal(settings, env, note)
    checks.append(_port(settings.port) if config is not None else _not_checked("port"))
    checks += _startup(config_home())
    return checks


def render(checks: list[Check]) -> str:
    lines: list[str] = []
    for check in checks:
        lines.append(f"{_MARK[check.status]}  {check.message}")
        if check.fix and check.status != "ok":
            lines.append(f"         fix: {check.fix}")
    return "\n".join(lines)


class DoctorCommand:
    name = "doctor"
    help = "Check that vigil can work on this machine and say how to fix what cannot."
    loads_own_config = True
    """`vigil.cli.main` does not load the config for this command: a config that does not load is
    one of the things it reports."""

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--json", action="store_true", help="print the results as JSON")

    def run(self, args: argparse.Namespace, config: Config) -> int:
        checks = run_checks(os.environ)
        ok = all(check.status != "problem" for check in checks)
        if args.json:
            print(json.dumps({"ok": ok, "checks": [asdict(check) for check in checks]}, indent=2))
        else:
            print(render(checks))
        return 0 if ok else 1


COMMAND = DoctorCommand()
