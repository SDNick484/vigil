"""Set vigil up to run all the time: pick a method, render its file, run its steps.

Used by `vigil setup` (which asks, prints and writes) and `vigil doctor` (which only reads).
Nothing here runs `sudo`, writes outside the user's own config folders, or edits a shell rc file.
The files come from the templates in ``vigil/templates``, which `contrib/` keeps byte-identical
copies of (`tests/test_setup.py` checks), with a few known lines replaced.
"""

import difflib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Literal, get_args

Method = Literal["autostart", "systemd", "openrc"]
METHODS: tuple[Method, ...] = get_args(Method)

MARKER = "# Written by `vigil setup`. `vigil setup --remove` deletes it."
"""A line in every file setup writes. A file without it is not setup's, and setup never removes
it or (without asking) replaces it."""

HEALTH_WAIT = 10.0
"""Seconds to wait for vigil to answer after starting it."""

_STEP_TIMEOUT = 30.0
_TEMPLATES: dict[Method, tuple[str, str]] = {
    "autostart": ("xdg", "vigil.desktop"),
    "systemd": ("systemd", "vigil.service"),
    "openrc": ("openrc", "vigil"),
}
# What may appear in a path that goes into a generated file: no quote, `$`, backtick, backslash,
# `%` (a systemd specifier, a desktop field code) or newline, so no quoting rule can be got wrong.
_SAFE_PATH = re.compile(r"[\w./+@:,=~ -]+")
_OPENRC_VERSION = re.compile(r"(\d+)\.(\d+)")
OPENRC_MIN = (0, 60)


class SetupError(Exception):
    """Something setup cannot do or a step that failed; the text says what and how to go on."""


def effective_uid() -> int:
    """The user id vigil runs as. Tests replace this to check the refusal to run as root."""
    return os.geteuid()


def vigil_executable() -> Path | None:
    """The absolute path of the `vigil` that is running, as the user reaches it (a symlink such
    as ``~/.local/bin/vigil`` is kept, so the file survives a rebuilt virtual environment), or
    None when none can be found."""
    candidates: list[Path] = []
    argv0 = Path(sys.argv[0])
    if argv0.name == "vigil":
        candidates.append(Path(os.path.abspath(argv0)))
    candidates.append(Path(sys.executable).parent / "vigil")
    on_path = shutil.which("vigil")
    if on_path is not None:
        candidates.append(Path(on_path))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def claude_folder(env: Mapping[str, str] | None = None) -> str | None:
    """The folder `claude` is found in on PATH now, or None."""
    found = shutil.which("claude", path=(env if env is not None else os.environ).get("PATH", ""))
    return os.path.dirname(found) if found else None


def path_dirs(env: Mapping[str, str]) -> list[str]:
    return [d for d in env.get("PATH", "").split(os.pathsep) if d]


def on_path(folder: Path, env: Mapping[str, str]) -> bool:
    """Whether `folder` is one of the PATH entries (compared as given and with links resolved)."""
    real = os.path.realpath(folder)
    return any(Path(d) == folder or os.path.realpath(d) == real for d in path_dirs(env))


# Detection ---------------------------------------------------------------------------------


def _probe(argv: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(  # noqa: S603 - argv is a fixed, vigil-built list
            argv,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None


def systemd_user_works() -> bool:
    """Whether a systemd user manager answers `systemctl --user`."""
    result = _probe(["systemctl", "--user", "show-environment"])
    return result is not None and result.returncode == 0


def openrc_user_works() -> bool:
    """Whether `rc-service` is OpenRC 0.60 or newer, the first with user services."""
    result = _probe(["rc-service", "--version"])
    if result is None or result.returncode != 0:
        return False
    match = _OPENRC_VERSION.search(result.stdout)
    return match is not None and (int(match[1]), int(match[2])) >= OPENRC_MIN


def detect_method(env: Mapping[str, str]) -> tuple[Method, str] | None:
    """The method to recommend and why, or None when none of them can be used here.

    XDG autostart when a desktop session is detected (a display and ``XDG_CURRENT_DESKTOP``);
    otherwise a systemd user unit when ``systemctl --user`` works; otherwise an OpenRC user
    service when ``rc-service`` is 0.60 or newer.
    """
    display = env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")
    desktop = env.get("XDG_CURRENT_DESKTOP")
    if display and desktop:
        return "autostart", f"a desktop session is running ({desktop})"
    if systemd_user_works():
        return "systemd", "`systemctl --user` works and there is no desktop session"
    if openrc_user_works():
        return "openrc", "OpenRC 0.60 or newer is installed and there is no desktop session"
    return None


NO_METHOD = (
    "no way to start vigil automatically was found: no desktop session (DISPLAY or "
    "WAYLAND_DISPLAY with XDG_CURRENT_DESKTOP), no working `systemctl --user`, and no OpenRC "
    "0.60 or newer (`rc-service --version`). Pick one with --method autostart|systemd|openrc, "
    "or follow Run in the README."
)


# Files -------------------------------------------------------------------------------------


def entry_path(method: Method, config_home: Path) -> Path:
    """Where setup puts the file for `method`: under the user's own config folder, always."""
    return {
        "autostart": config_home / "autostart" / "vigil.desktop",
        "systemd": config_home / "systemd" / "user" / "vigil.service",
        "openrc": config_home / "rc" / "init.d" / "vigil",
    }[method]


def entry_mode(method: Method) -> int:
    return 0o755 if method == "openrc" else 0o644


def template(method: Method) -> str:
    folder, name = _TEMPLATES[method]
    return resources.files("vigil").joinpath("templates", folder, name).read_text()


def _check_path(value: str, what: str) -> str:
    if not _SAFE_PATH.fullmatch(value):
        raise SetupError(
            f"{what} {value!r} has a character setup will not put in a service file "
            "(a quote, $, backtick, backslash, % or a newline); use the manual steps in the README"
        )
    return value


def _replace_line(text: str, prefix: str, new: str) -> str:
    lines = text.splitlines(keepends=True)
    found = [i for i, line in enumerate(lines) if line.startswith(prefix)]
    if len(found) != 1:  # a template was edited; `tests/test_setup.py` catches this first
        raise SetupError(f"the template has no single line starting {prefix!r}")
    lines[found[0]] = new + "\n"
    return "".join(lines)


def _line(text: str, prefix: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(prefix))


def _with_claude(value: str, claude_dir: str | None, home: Path, variable: str) -> str:
    """The `PATH` value `value` with `claude_dir` in front, unless it is already listed (with
    `variable`, the template's spelling of the home folder, expanded)."""
    if claude_dir is None:
        return value
    listed = [d.replace(variable, str(home)) for d in value.split(":")]
    return value if claude_dir in listed else f"{claude_dir}:{value}"


def render(method: Method, vigil_bin: Path, claude_dir: str | None, home: Path) -> str:
    """The file setup writes: the template with the absolute path of `vigil` in it, for the
    services a PATH that holds `claude_dir`, and the marker as its second line.

    Raises SetupError for a path with a character that cannot be written safely.
    """
    text = template(method)
    binary = _check_path(str(vigil_bin), "the path to vigil")
    quoted = f'"{binary}"' if " " in binary else binary
    if claude_dir is not None:
        _check_path(claude_dir, "the folder of claude")
    if method == "autostart":
        text = _replace_line(text, "Exec=", f"Exec={quoted} serve")
    elif method == "systemd":
        text = _replace_line(text, "ExecStart=", f"ExecStart={quoted} serve")
        value = _line(text, "Environment=PATH=").removeprefix("Environment=PATH=")
        value = _with_claude(value, claude_dir, home, "%h")
        setting = f"PATH={value}"
        text = _replace_line(
            text,
            "Environment=PATH=",
            f'Environment="{setting}"' if " " in value else f"Environment={setting}",
        )
    else:
        text = _replace_line(text, ': "${vigil_bin:=', f': "${{vigil_bin:={binary}}}"')
        head = 'export PATH="'
        value = _line(text, head).removeprefix(head)
        text = _replace_line(text, head, head + _with_claude(value, claude_dir, home, "${HOME}"))
    first, _, rest = text.partition("\n")
    return f"{first}\n{MARKER}\n{rest}"


def is_ours(text: str) -> bool:
    """Whether `text` is a file setup wrote (it has the marker line)."""
    return MARKER in text.splitlines()


def diff(old: str, new: str) -> str:
    """What changes in a file, as a unified diff."""
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile="installed",
            tofile="new",
        )
    )


def check_target(path: Path, config_home: Path) -> None:
    """Refuse a path outside the three folders setup may write in."""
    roots = [config_home / "autostart", config_home / "systemd" / "user", config_home / "rc"]
    resolved = Path(os.path.abspath(path))
    if not any(resolved.is_relative_to(Path(os.path.abspath(root))) for root in roots):
        raise SetupError(f"refusing to touch {path}: it is outside {config_home}")


def ensure_runlevel(method: Method, config_home: Path) -> None:
    """Make the user's OpenRC `default` runlevel folder. `openrc --user` makes it on its first
    run; until then (no pam_openrc, as under WSL) `rc-update --user add` rejects the runlevel."""
    if method == "openrc":
        (config_home / "rc" / "runlevels" / "default").mkdir(parents=True, exist_ok=True)


def write_entry(path: Path, text: str, mode: int, config_home: Path) -> None:
    """Write `text` to `path` (replacing any file there), making the folders it needs."""
    check_target(path, config_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".vigil-setup-")
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(text)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


# Steps -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    """A command setup runs. `required` False means a failure is reported and setup goes on."""

    what: str
    argv: tuple[str, ...]
    required: bool = True
    detached: bool = False
    """Start a program that stays running (no output is kept, and it does not wait)."""


def start_steps(
    method: Method, vigil_bin: Path, *, replaced: bool, running: bool, enable_only: bool
) -> list[Step]:
    """The steps that start vigil and make it start again by itself.

    `replaced`: the file was changed, so a running vigil is restarted. `running`: vigil already
    answers (autostart then starts nothing). `enable_only`: register it without starting it.
    """
    if method == "autostart":
        if enable_only or running:
            return []
        return [Step("start vigil", ("setsid", "-f", str(vigil_bin), "serve"), detached=True)]
    if method == "systemd":
        user = ("systemctl", "--user")
        steps = [Step("reload the user units", (*user, "daemon-reload"))]
        if enable_only:
            return [*steps, Step("enable the service", (*user, "enable", "vigil.service"))]
        steps.append(
            Step("enable and start the service", (*user, "enable", "--now", "vigil.service"))
        )
        if replaced:
            steps.append(Step("restart the service", (*user, "restart", "vigil.service")))
        return steps
    steps = []
    if not enable_only:
        action = "restart" if replaced else "start"
        steps.append(Step(f"{action} the service", ("rc-service", "--user", "vigil", action)))
    steps.append(Step("start it at login", ("rc-update", "--user", "add", "vigil", "default")))
    return steps


def stop_steps(method: Method, *, running: bool) -> list[Step]:
    """The steps that stop vigil and unregister it (before the file is deleted). Each may fail
    harmlessly, for instance when the service was not running."""
    if method == "autostart":
        if not running:
            return []
        # The command line of `vigil serve` is "<python> <folder>/vigil serve".
        return [Step("stop vigil", ("pkill", "-f", "--", "/vigil serve( |$)"), required=False)]
    if method == "systemd":
        return [
            Step(
                "stop and disable the service",
                ("systemctl", "--user", "disable", "--now", "vigil.service"),
                required=False,
            )
        ]
    return [
        Step("stop the service", ("rc-service", "--user", "vigil", "stop"), required=False),
        Step(
            "remove it from the default runlevel",
            ("rc-update", "--user", "del", "vigil", "default"),
            required=False,
        ),
    ]


def after_remove_steps(method: Method) -> list[Step]:
    if method == "systemd":
        return [Step("reload the user units", ("systemctl", "--user", "daemon-reload"), False)]
    return []


def run_step(step: Step) -> str | None:
    """Run `step`. Returns None on success, else why it failed (the program's own words)."""
    if step.argv[0] == "sudo":  # never; a guard against a future edit
        raise SetupError("setup never runs sudo")
    try:
        if step.detached:
            subprocess.run(  # noqa: S603 - argv is a fixed, vigil-built list
                step.argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_STEP_TIMEOUT,
                check=True,
            )
            return None
        result = subprocess.run(  # noqa: S603 - argv is a fixed, vigil-built list
            step.argv,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_STEP_TIMEOUT,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError:
        return f"{step.argv[0]} was not found on PATH"
    except subprocess.CalledProcessError as error:
        return f"exited with status {error.returncode}"
    except (OSError, subprocess.SubprocessError) as error:
        return str(error)
    if result.returncode == 0:
        return None
    detail = (result.stderr or result.stdout).strip()
    return detail or f"exited with status {result.returncode}"


# Health ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Health:
    state: Literal["vigil", "free", "other"]
    """`vigil`: vigil answers. `free`: nothing is listening. `other`: something else is."""
    version: str | None = None


_LOOPBACK_ONLY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
"""Never through a proxy from the environment: the check is to 127.0.0.1 and nowhere else."""


def health_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/api/v1/health"


def probe_health(port: int, timeout: float = 1.0) -> Health:
    """Ask 127.0.0.1:`port` for `/api/v1/health`."""
    try:
        with _LOOPBACK_ONLY.open(health_url(port), timeout=timeout) as response:
            body = json.load(response)
    except urllib.error.HTTPError:
        return Health("other")
    except urllib.error.URLError as error:
        if isinstance(error.reason, ConnectionRefusedError):
            return Health("free")
        return Health("free" if _can_bind(port) else "other")
    except OSError, ValueError:
        return Health("free" if _can_bind(port) else "other")
    if (
        isinstance(body, dict)
        and body.get("status") == "ok"
        and isinstance(body.get("version"), str)
    ):
        return Health("vigil", body["version"])
    return Health("other")


def _can_bind(port: int) -> bool:
    """Whether nothing listens on 127.0.0.1:`port`. A closed port can fail to answer instead of
    refusing (WSL2 mirrored networking drops the connection), so no answer alone proves nothing."""
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def wait_for_health(port: int, timeout: float | None = None) -> Health | None:
    """Poll until vigil answers on `port`; None when it does not within `timeout` seconds
    (`HEALTH_WAIT` by default)."""
    limit = HEALTH_WAIT if timeout is None else timeout
    deadline = time.monotonic() + limit
    while True:
        health = probe_health(port)
        if health.state == "vigil":
            return health
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.25)
