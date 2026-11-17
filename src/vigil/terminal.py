"""Find a terminal that can run an attach command.

Nothing here starts a process: `terminal_argv` only builds the argv. Executables are looked up on
the ``PATH`` of the ``env`` it is given, so callers (and tests) control what can be found.
"""

import shlex
import shutil
from collections.abc import Mapping
from pathlib import Path

from vigil.config import ResumeConfig

PROC_VERSION = Path("/proc/version")
"""Read to detect WSL when ``WSL_DISTRO_NAME`` is not set. Tests point it at a fixture file."""

ATTACH = "{attach}"

# Tried in this order when neither a template nor $TERMINAL gives a terminal. Each entry is the
# executable and the arguments that make it run the rest of its argv as a command.
KNOWN_TERMINALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("x-terminal-emulator", ("-e",)),
    ("mate-terminal", ("-x",)),
    ("gnome-terminal", ("--",)),
    ("konsole", ("-e",)),
    ("xfce4-terminal", ("-x",)),
    ("alacritty", ("-e",)),
    ("kitty", ()),
    ("foot", ()),
    ("xterm", ("-e",)),
)


def _which(name: str, env: Mapping[str, str]) -> str | None:
    # An empty PATH finds nothing (shutil.which would otherwise fall back to os.environ).
    return shutil.which(name, path=env.get("PATH", ""))


def is_wsl(env: Mapping[str, str]) -> bool:
    """True under WSL: ``WSL_DISTRO_NAME`` is set, or the kernel version mentions Microsoft."""
    if env.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in PROC_VERSION.read_text(errors="replace").lower()
    except OSError:
        return False


def from_template(template: str, attach: list[str]) -> list[str]:
    """Split a terminal command template and put the attach command in place of ``{attach}``.

    A token that is exactly ``{attach}`` becomes the attach argv, one argument per word; inside a
    longer token (``--command={attach}``) it becomes the attach command as one shell string.
    Raises ValueError when the template is not valid shell syntax or is empty.
    """
    tokens = shlex.split(template)
    if not tokens:
        raise ValueError("resume.terminal_command is empty")
    argv: list[str] = []
    for token in tokens:
        if token == ATTACH:
            argv.extend(attach)
        else:
            argv.append(token.replace(ATTACH, shlex.join(attach)))
    return argv


def _windows_terminal(attach: list[str], cwd: str, env: Mapping[str, str]) -> list[str]:
    distro = env.get("WSL_DISTRO_NAME")
    args = ["wsl.exe", *(["-d", distro] if distro else []), "--cd", cwd, "--", *attach]
    # wt.exe splits its command line on ";" (its own command separator) unless escaped.
    return ["wt.exe", *(arg.replace(";", "\\;") for arg in args)]


def _from_terminal_env(attach: list[str], env: Mapping[str, str]) -> list[str] | None:
    try:
        parts = shlex.split(env.get("TERMINAL", ""))
    except ValueError:
        return None
    if not parts or _which(parts[0], env) is None:
        return None
    return [*parts, "-e", *attach]


def terminal_argv(
    attach: list[str], cwd: str, cfg: ResumeConfig, env: Mapping[str, str]
) -> list[str] | None:
    """The argv that opens a terminal running `attach`, or None when no terminal can be opened.

    In order: the configured ``resume.terminal_command`` template (used as given, display or
    not); under WSL, Windows Terminal (``wt.exe``) when it is on PATH; then, only with a
    ``DISPLAY`` or ``WAYLAND_DISPLAY``, ``$TERMINAL -e``, else the first of `KNOWN_TERMINALS` on
    PATH. Raises ValueError when the configured template is not valid shell syntax.
    """
    if cfg.terminal_command is not None:
        return from_template(cfg.terminal_command, attach)
    if is_wsl(env) and _which("wt.exe", env) is not None:
        return _windows_terminal(attach, cwd, env)
    if not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
        return None
    from_env = _from_terminal_env(attach, env)
    if from_env is not None:
        return from_env
    for name, exec_args in KNOWN_TERMINALS:
        if _which(name, env) is not None:
            return [name, *exec_args, *attach]
    return None
