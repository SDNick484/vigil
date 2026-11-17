"""Applying a review's project file suggestions: appending to a project's `CLAUDE.md`, or merging
into its `.claude/settings.json`. `plan_change` works out the change (nothing is written), the web
shows it as a diff (`diff_text`), and `apply_change` writes it after that preview, only if the
file is still as previewed.

This is the only place vigil writes outside its own data and config. A suggestion comes from
Claude reading a transcript that can hold untrusted text, so it is untrusted too: only the two
project files are ever targets, only the settings keys in `SETTINGS_KEYS` are applied, and a
target whose real path (after symlinks) is under the Claude home, outside the project folder or
not a regular file is refused. Existing text and settings are never changed, only added to.
"""

import difflib
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vigil.projects import _KEPT_GIT_ENV
from vigil.review.schema import FileTarget, Suggestion

SETTINGS_KEYS = frozenset(
    {
        "model",
        "effortLevel",
        "permissions.deny",
        "permissions.ask",
        "includeCoAuthoredBy",
        "cleanupPeriodDays",
    }
)
"""The settings keys (dotted paths) a suggestion may set; so may any top-level key that starts
with `_AUTO_COMPACT`. A suggestion with any other key is copy-only."""

ABSENT = "absent"
"""`Change.before_hash` of a file that does not exist."""
CHANGED = "the file changed since the preview; show the change again"
USER_TARGET = "vigil does not change files under the Claude home; copy the text to add it yourself"

_AUTO_COMPACT = "autoCompact"
_ALLOWED = tuple(tuple(key.split(".")) for key in SETTINGS_KEYS)
_TARGETS: dict[FileTarget, Path] = {
    "project_claude_md": Path("CLAUDE.md"),
    "project_settings": Path(".claude") / "settings.json",
}
_USER_TARGETS: dict[FileTarget, str] = {
    "user_claude_md": "CLAUDE.md",
    "user_settings": "settings.json",
}
_GIT_TIMEOUT = 5
_NEW_FILE_MODE = 0o644
_NEW_DIR_MODE = 0o755

_lock = threading.Lock()
"""Held from the last check of a file to replacing it, so two applies at once do not lose one."""


class ApplyRefused(Exception):
    """A file suggestion cannot (or will not) be applied; `reason` is user-facing text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Change:
    """A planned change to one project file, as `plan_change` works it out."""

    target: Path
    """The file to write: its real path, after symlinks."""
    before: str | None
    """The file's text now; None when it does not exist."""
    after: str
    before_hash: str
    """sha256 (hex) of the file's bytes now, or `ABSENT`."""
    skipped: list[str]
    """Settings keys (dotted) the suggestion would change but are already set: left alone."""
    current: dict[str, Any]
    """The value each key in `skipped` has now."""
    folder: Path
    """The project folder (real path) the target was checked against."""


def user_target_path(target: FileTarget, claude_home: Path) -> Path | None:
    """Where a user target (`user_claude_md`, `user_settings`) lives under `claude_home`; None
    for a project target. vigil only shows this path, it never writes there."""
    name = _USER_TARGETS.get(target)
    return claude_home / name if name is not None else None


def project_folder(cwd: str) -> Path:
    """The project folder of a session that ran in `cwd`: its git top-level, else `cwd` (no git,
    not in a repository, or git took longer than 5 s)."""
    # Ignore GIT_DIR, GIT_WORK_TREE and the like from our own environment: ask about `cwd` only.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") or k in _KEPT_GIT_ENV}
    try:
        result = subprocess.run(  # noqa: S603 - a fixed argv, never a shell
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            errors="replace",
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return Path(cwd)
    top = result.stdout.strip()
    return Path(top) if result.returncode == 0 and os.path.isabs(top) else Path(cwd)


def check_settings(settings: object) -> dict[str, Any]:
    """`settings` (a suggestion's), when every key in it may be applied; raises ApplyRefused
    naming the first key that may not, or when it is not a JSON object."""
    if not isinstance(settings, dict) or not settings:
        raise ApplyRefused("the suggested settings are not a JSON object")
    for path in _leaf_paths(settings, ()):
        if not _appliable(path):
            raise ApplyRefused(
                f"vigil does not apply the settings key {'.'.join(path)}; it applies only "
                f"{', '.join(sorted(SETTINGS_KEYS))} and {_AUTO_COMPACT}*"
            )
    return settings


def _leaf_paths(value: dict[str, Any], prefix: tuple[str, ...]) -> Iterator[tuple[str, ...]]:
    """The paths to every leaf of `value`: a value that is not an object, or an empty object."""
    if not value and prefix:
        yield prefix
    for key, item in value.items():
        path = (*prefix, str(key))
        if isinstance(item, dict):
            yield from _leaf_paths(item, path)
        else:
            yield path


def _appliable(path: tuple[str, ...]) -> bool:
    if path[0].startswith(_AUTO_COMPACT):
        return True
    return any(path[: len(allowed)] == allowed for allowed in _ALLOWED)


def plan_change(s: Suggestion, folder: Path, claude_home: Path) -> Change:
    """The change suggestion `s` makes to its file in the project folder `folder`; nothing is
    written. `CLAUDE.md`: the text is appended after a blank line (the file is created if
    missing). `.claude/settings.json`: the settings are deep-merged (objects merge, lists are
    unioned in order, an existing value is kept and its key listed in `skipped`).

    Raises ApplyRefused for a user target, a settings key not in `SETTINGS_KEYS`, an existing
    settings file that is not a JSON object, a file that is not UTF-8, and a target whose real
    path is under `claude_home`, outside `folder`, or exists and is not a regular file.
    """
    if s.kind != "file" or s.file_target is None:
        raise ApplyRefused("not a file suggestion")
    relative = _TARGETS.get(s.file_target)
    if relative is None:
        raise ApplyRefused(USER_TARGET)
    settings = check_settings(s.file_settings) if s.file_target == "project_settings" else None
    text = (s.file_text or "").strip()
    if settings is None and not text:
        raise ApplyRefused("the suggestion has no text to add")
    real_folder = _real(folder)
    target = _check_target(real_folder / relative, real_folder, _real(claude_home))
    data = _read(target)
    try:
        before = data.decode("utf-8") if data is not None else None
    except UnicodeDecodeError as error:
        raise ApplyRefused(f"{target} is not UTF-8 text: {error}") from error
    skipped: list[str] = []
    current: dict[str, Any] = {}
    if settings is None:
        kept = (before or "").rstrip("\n")
        after = f"{kept}\n\n{text}\n" if kept else f"{text}\n"
    else:
        existing = _settings_object(target, before)
        merged = _merge(existing, settings, (), skipped, current)
        after = json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
    return Change(target, before, after, _hash(data), skipped, current, real_folder)


def _settings_object(target: Path, before: str | None) -> dict[str, Any]:
    if before is None:
        return {}
    try:
        existing = json.loads(before)
    except ValueError as error:
        raise ApplyRefused(
            f"{target} is not valid JSON ({error}); vigil will not change it"
        ) from error
    if not isinstance(existing, dict):
        raise ApplyRefused(f"{target} is not a JSON object; vigil will not change it")
    return existing


def _merge(
    existing: dict[str, Any],
    new: dict[str, Any],
    prefix: tuple[str, ...],
    skipped: list[str],
    current: dict[str, Any],
) -> dict[str, Any]:
    """`existing` with `new` merged in (in `existing`'s key order, new keys after): objects
    merge, lists get the new items not already there, and any other value already set is kept,
    its key added to `skipped` (and its value to `current`) when the suggestion's differs."""
    merged = dict(existing)
    for key, value in new.items():
        if key not in merged:
            merged[key] = value
            continue
        old = merged[key]
        if isinstance(old, dict) and isinstance(value, dict):
            merged[key] = _merge(old, value, (*prefix, key), skipped, current)
        elif isinstance(old, list) and isinstance(value, list):
            union = list(old)
            for item in value:
                if item not in union:
                    union.append(item)
            merged[key] = union
        elif old != value:
            dotted = ".".join((*prefix, key))
            skipped.append(dotted)
            current[dotted] = old
    return merged


def diff_text(c: Change) -> str:
    """`c` as a unified diff of the target file (from `/dev/null` for a new file)."""
    lines = difflib.unified_diff(
        (c.before or "").splitlines(),
        c.after.splitlines(),
        fromfile=str(c.target) if c.before is not None else "/dev/null",
        tofile=str(c.target),
        lineterm="",
    )
    return "\n".join(lines)


def apply_change(c: Change, claude_home: Path) -> None:
    """Write `c`: check the path rules again, re-read the target and refuse (`CHANGED`) when it
    is not as previewed (`c.before_hash`), create `.claude/` (mode 0755) if needed, then replace
    the file atomically (temp file in the same folder, then `os.replace`; an existing file's mode
    is kept). Never writes through a symlink: the target is the real path. Raises ApplyRefused,
    leaving the file as it was, when anything fails."""
    with _lock:
        if _check_target(c.target, c.folder, _real(claude_home)) != c.target:
            raise ApplyRefused(f"{c.target} is now a link; show the change again")
        if _hash(_read(c.target)) != c.before_hash:
            raise ApplyRefused(CHANGED)
        parent = c.target.parent
        try:
            if c.before is None and not parent.is_dir():
                parent.mkdir(mode=_NEW_DIR_MODE)
            if _real(parent) != parent:
                raise ApplyRefused(f"{parent} is now a link; show the change again")
            mode = stat.S_IMODE(c.target.stat().st_mode) if c.before is not None else None
            _replace(c.target, c.after, mode if mode is not None else _NEW_FILE_MODE)
        except OSError as error:
            raise ApplyRefused(f"cannot write {c.target}: {error}") from error


def _replace(target: Path, text: str, mode: int) -> None:
    """Write `text` to a temp file next to `target`, then move it over `target` (which replaces a
    link rather than writing through it). The temp file is removed when anything fails."""
    handle, name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    temp = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        temp.chmod(mode)
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _real(path: Path) -> Path:
    """`path` absolute with every symlink in it resolved (as far as it exists)."""
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ApplyRefused(f"cannot resolve {path}: {error}") from error


def _check_target(target: Path, folder: Path, claude_home: Path) -> Path:
    """The real path of `target`, checked: under `folder` (a real path), not under `claude_home`
    (a real path), and a regular file; for a missing target, its nearest existing parent must be
    a folder under `folder` and not under `claude_home`."""
    real = _real(target)
    _check_inside(real, target, folder, claude_home)
    try:
        mode = os.stat(real).st_mode
    except FileNotFoundError:
        parent = next(p for p in real.parents if os.path.lexists(p))
        real_parent = _real(parent)
        _check_inside(real_parent, target, folder, claude_home)
        if not real_parent.is_dir():
            raise ApplyRefused(f"{parent} is not a folder") from None
        return real
    except OSError as error:
        raise ApplyRefused(f"cannot read {target}: {error}") from error
    if not stat.S_ISREG(mode):
        raise ApplyRefused(f"{target} is not a regular file")
    return real


def _check_inside(real: Path, shown: Path, folder: Path, claude_home: Path) -> None:
    if real.is_relative_to(claude_home):
        raise ApplyRefused(
            f"{shown} is under the Claude home ({claude_home}); vigil never writes there"
        )
    if not real.is_relative_to(folder):
        raise ApplyRefused(f"{shown} leads outside the project folder ({folder})")


def _read(path: Path) -> bytes | None:
    """The bytes of `path` (a real path: a symlink there now is not followed); None when it does
    not exist."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ApplyRefused(f"cannot read {path}: {error}") from error
    try:
        with os.fdopen(fd, "rb") as file:
            return file.read()
    except OSError as error:
        raise ApplyRefused(f"cannot read {path}: {error}") from error


def _hash(data: bytes | None) -> str:
    return hashlib.sha256(data).hexdigest() if data is not None else ABSENT
