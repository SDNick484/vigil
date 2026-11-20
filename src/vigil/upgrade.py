"""Find out how the running vigil was installed and plan the command that upgrades it.

Used by `vigil upgrade`. Nothing here runs a command: `plan_upgrade` returns the argv lists and
`record_install` rewrites the marker that `install.sh` leaves in its virtual environment.
"""

import os
import re
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import tomlkit
from tomlkit.exceptions import TOMLKitError

REPO_URL = "https://github.com/SDNick484/vigil"
DEFAULT_REF = "main"
MARKER_NAME = "vigil-install.toml"
"""The file `install.sh` writes in the root of its virtual environment (`ref`, `source`,
`installed_at`)."""
GENTOO_RELEASE = Path("/etc/gentoo-release")
"""Tests point this at a temporary path."""

_SYSTEM_PREFIXES = (Path("/usr"), Path("/usr/local"))
# A ref goes into a pip URL; allow what a git ref needs and nothing that starts like an option.
# `install.sh` checks the same characters.
_REF = re.compile(r"[A-Za-z0-9._/+-]+")

Kind = Literal["script", "venv", "pipx", "system"]


class UpgradeRefused(Exception):
    """vigil will not upgrade this install itself; the text says what to do instead."""


@dataclass(frozen=True)
class Install:
    kind: Kind
    """`script`: made by install.sh (the marker is there). `pipx`: in pipx's venvs folder.
    `system`: a package manager's, under /usr. `venv`: any other virtual environment."""
    prefix: Path
    ref: str | None
    """The ref in the marker (`script` only), when it has a usable one."""


def valid_ref(ref: str) -> bool:
    return bool(_REF.fullmatch(ref)) and not ref.startswith("-")


def read_marker(prefix: Path) -> dict[str, object] | None:
    """The marker's contents, or None when it is missing or not valid TOML."""
    try:
        with (prefix / MARKER_NAME).open("rb") as handle:
            return tomllib.load(handle)
    except OSError, tomllib.TOMLDecodeError:
        return None


def _pipx_venvs(env: dict[str, str] | os._Environ[str], home: Path) -> list[Path]:
    folders = [home / ".local" / "share" / "pipx" / "venvs", home / ".local" / "pipx" / "venvs"]
    pipx_home = env.get("PIPX_HOME", "")
    if pipx_home and os.path.isabs(pipx_home):
        folders.insert(0, Path(pipx_home) / "venvs")
    return folders


def detect_install(
    *,
    prefix: Path | None = None,
    base_prefix: Path | None = None,
    env: dict[str, str] | os._Environ[str] | None = None,
    home: Path | None = None,
) -> Install:
    """How the running vigil was installed (the arguments are for tests)."""
    prefix = Path(sys.prefix) if prefix is None else prefix
    base_prefix = Path(sys.base_prefix) if base_prefix is None else base_prefix
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    marker = read_marker(prefix)
    if (prefix / MARKER_NAME).is_file():
        ref = marker.get("ref") if marker else None
        return Install("script", prefix, ref if isinstance(ref, str) and valid_ref(ref) else None)
    resolved = prefix.resolve()
    if any(resolved.is_relative_to(folder.resolve()) for folder in _pipx_venvs(env, home)):
        return Install("pipx", prefix, None)
    if prefix in _SYSTEM_PREFIXES and prefix == base_prefix:
        return Install("system", prefix, None)
    return Install("venv", prefix, None)


def resolve_ref(install: Install, ref: str | None) -> str:
    """The ref to install: the one asked for, else the one the marker records, else `main`."""
    chosen = ref or install.ref or DEFAULT_REF
    if not valid_ref(chosen):
        raise UpgradeRefused(
            f"{chosen!r} is not a git branch, tag or commit name vigil will install "
            "(letters, digits and . _ / + - only, and it cannot start with -)"
        )
    return chosen


def plan_upgrade(install: Install, ref: str | None) -> list[list[str]]:
    """The commands that upgrade `install`, in order. Raises UpgradeRefused for a system install
    (vigil does not touch what a package manager owns) and for a ref that cannot be used."""
    if install.kind == "system":
        if GENTOO_RELEASE.exists():
            raise UpgradeRefused(
                "vigil is installed by your package manager; upgrade it with: "
                "emerge -u dev-util/vigil"
            )
        raise UpgradeRefused(
            "vigil is installed by your package manager; upgrade it with your package manager"
        )
    source = f"git+{REPO_URL}@{resolve_ref(install, ref)}"
    if install.kind == "pipx":
        return [["pipx", "install", "--force", "--python", sys.executable, source]]
    return [[sys.executable, "-m", "pip", "install", "--upgrade", source]]


def record_install(install: Install, ref: str, now: datetime | None = None) -> bool:
    """Set the marker's `ref` and `installed_at` (an ISO 8601 UTC string, as install.sh writes
    it), keeping its other keys and comments. Only a `script` install has a marker; returns
    whether it was written."""
    if install.kind != "script":
        return False
    path = install.prefix / MARKER_NAME
    try:
        document = tomlkit.parse(path.read_text())
    except OSError, TOMLKitError:
        document = tomlkit.document()
        document["source"] = "install.sh"
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    document["ref"] = ref
    document["installed_at"] = stamp
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".vigil-install-")
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(tomlkit.dumps(document))
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return True
