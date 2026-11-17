"""Applying a review's project file suggestions: appending to a project's `CLAUDE.md`, or merging
into its `.claude/settings.json`. `plan_change` works out the change (nothing is written), the web
shows it as a diff (`diff_text`), and `apply_change` writes it after that preview, only if the
file is still as previewed.

This is the only place vigil writes outside its own data and config. A suggestion comes from
Claude reading a transcript that can hold untrusted text, so it is untrusted too
(`check_suggestion`): only the two project files are ever targets, only the settings keys in
`SETTINGS_KEYS` are applied and each only with a value of its kind, and text with invisible or
unassigned characters is refused. The target is refused when it, or `.claude`, is a symbolic link,
when it is not a regular file without execute bits, and when its real path is under the Claude
home, outside the project folder or inside a `.git` folder. Existing text and settings are never
changed, only added to.
"""

import difflib
import hashlib
import json
import math
import os
import stat
import subprocess
import tempfile
import threading
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, TypeGuard

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
with `_AUTO_COMPACT`. A suggestion with any other key is copy-only. `check_settings` has the
value rule of each."""

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
CHANGED = "the file changed since the preview; show the change again"
USER_TARGET = "vigil does not change files under the Claude home; copy the text to add it yourself"
INVISIBLE = "the suggestion contains invisible or unassigned characters; vigil will not apply it"

_AUTO_COMPACT = "autoCompact"
_PERMISSION_LISTS = ("deny", "ask")
_MAX_MODEL = 64
_TARGETS: dict[FileTarget, tuple[str, ...]] = {
    "project_claude_md": ("CLAUDE.md",),
    "project_settings": (".claude", "settings.json"),
}
_USER_TARGETS: dict[FileTarget, str] = {
    "user_claude_md": "CLAUDE.md",
    "user_settings": "settings.json",
}
_INVISIBLE_CATEGORIES = frozenset({"Cf", "Co", "Cn"})
_TAGS = range(0xE0000, 0xE0080)
_GIT_TIMEOUT = 5
_NEW_FILE_MODE = 0o644
_NEW_DIR_MODE = 0o755
_EXECUTE = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH

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
    """The file to write: `folder` / `CLAUDE.md` or `.claude/settings.json`, never a link."""
    before: str | None
    """The file's text now; None when it does not exist."""
    after: str
    before_hash: str
    """`file_hash` of the file now: it covers the path and the bytes (or that it is missing)."""
    skipped: list[str]
    """Settings keys (dotted) the suggestion would change but are already set: left alone."""
    current: dict[str, Any]
    """The value each key in `skipped` has now."""
    folder: Path
    """The project folder (real path) the target is in."""


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


def _invisible(text: str) -> bool:
    """`text` has a format (Cf), private-use (Co) or unassigned (Cn) character, or a tag."""
    return any(
        unicodedata.category(char) in _INVISIBLE_CATEGORIES or ord(char) in _TAGS for char in text
    )


def _strings(value: Any) -> Iterator[str]:
    """Every string in `value` (parsed JSON), object keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _printable(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and bool(value) and value.isprintable()


def check_settings(settings: object) -> dict[str, Any]:
    """`settings` (a suggestion's), when vigil may apply all of it; raises ApplyRefused naming
    the first key that may not be applied or has a value of the wrong kind, or when it is not a
    JSON object or has invisible characters.

    The value rules: `model` printable text of 1 to 64 characters; `effortLevel` one of
    `EFFORT_LEVELS`; `permissions.deny` and `permissions.ask` lists of non-empty printable text;
    `includeCoAuthoredBy` true or false; `cleanupPeriodDays` a whole number of at least 1; a
    top-level `autoCompact*` key text, a finite number or true/false (never an object or a list)."""
    if not isinstance(settings, dict) or not settings:
        raise ApplyRefused("the suggested settings are not a JSON object")
    if any(_invisible(text) for text in _strings(settings)):
        raise ApplyRefused(INVISIBLE)
    for key, value in settings.items():
        if key == "permissions":
            if not isinstance(value, dict) or not value:
                _wrong(key, "an object holding `deny` or `ask`")
            for name, rules in value.items():
                if name not in _PERMISSION_LISTS:
                    _not_applied(f"permissions.{name}")
                if not isinstance(rules, list) or not all(map(_printable, rules)):
                    _wrong(f"permissions.{name}", "a list of non-empty printable text")
        elif key == "model":
            if not _printable(value) or len(value) > _MAX_MODEL:
                _wrong(key, f"printable text of 1 to {_MAX_MODEL} characters")
        elif key == "effortLevel":
            if value not in EFFORT_LEVELS:
                _wrong(key, f"one of {', '.join(EFFORT_LEVELS)}")
        elif key == "includeCoAuthoredBy":
            if not isinstance(value, bool):
                _wrong(key, "true or false")
        elif key == "cleanupPeriodDays":
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                _wrong(key, "a whole number of at least 1")
        elif key.startswith(_AUTO_COMPACT):
            if not isinstance(value, str | int | float | bool) or (
                isinstance(value, float) and not math.isfinite(value)
            ):
                _wrong(key, "text, a finite number or true/false")
        else:
            _not_applied(key)
    return settings


def _not_applied(key: str) -> NoReturn:
    raise ApplyRefused(
        f"vigil does not apply the settings key {key}; it applies only "
        f"{', '.join(sorted(SETTINGS_KEYS))} and {_AUTO_COMPACT}*"
    )


def _wrong(key: str, rule: str) -> NoReturn:
    raise ApplyRefused(f"the settings key {key} must be {rule}; vigil will not apply it")


def check_suggestion(s: Suggestion) -> None:
    """Raises ApplyRefused when vigil will not apply file suggestion `s` whatever the files are
    like: a user target, CLAUDE.md text that is empty or has invisible characters, or settings
    `check_settings` refuses."""
    if s.kind != "file" or s.file_target is None:
        raise ApplyRefused("not a file suggestion")
    if s.file_target not in _TARGETS:
        raise ApplyRefused(USER_TARGET)
    if s.file_target == "project_settings":
        check_settings(s.file_settings)
        return
    text = (s.file_text or "").strip()
    if not text:
        raise ApplyRefused("the suggestion has no text to add")
    if _invisible(text):
        raise ApplyRefused(INVISIBLE)


def plan_change(s: Suggestion, folder: Path, claude_home: Path) -> Change:
    """The change suggestion `s` makes to its file in the project folder `folder`; nothing is
    written. `CLAUDE.md`: the text is appended after a blank line (the file is created if
    missing). `.claude/settings.json`: the settings are deep-merged (objects merge, lists are
    unioned in order, an existing value is kept and its key listed in `skipped`).

    Raises ApplyRefused when `check_suggestion` does, when the target is refused
    (`_check_target`), when the file is not UTF-8, and when an existing settings file is not a
    JSON object or holds a value of another kind (such as a list) where the suggestion merges in,
    or when the merged settings would hold NaN or Infinity (which JSON cannot).
    """
    check_suggestion(s)
    assert s.file_target is not None  # checked above
    real_folder = _real(folder)
    target = _check_target(real_folder, _TARGETS[s.file_target], _real(claude_home))
    data = _read(target)
    try:
        before = data.decode("utf-8") if data is not None else None
    except UnicodeDecodeError as error:
        raise ApplyRefused(f"{target} is not UTF-8 text: {error}") from error
    skipped: list[str] = []
    current: dict[str, Any] = {}
    if s.file_settings is None:
        text = (s.file_text or "").strip()
        kept = (before or "").rstrip("\n")
        after = f"{kept}\n\n{text}\n" if kept else f"{text}\n"
    else:
        existing = _settings_object(target, before)
        merged = _merge(target, existing, s.file_settings, (), skipped, current)
        try:
            after = json.dumps(merged, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        except ValueError as error:
            raise ApplyRefused(
                f"{target} would hold a number JSON cannot (NaN or Infinity); vigil will not "
                "write it"
            ) from error
    return Change(target, before, after, file_hash(target, data), skipped, current, real_folder)


def file_hash(target: Path, data: bytes | None) -> str:
    """sha256 (hex) of the path `target` and its bytes `data` (None: the file is missing)."""
    path = os.fsencode(target)
    if data is None:
        return hashlib.sha256(b"absent\0" + path).hexdigest()
    return hashlib.sha256(b"file\0" + path + b"\0" + data).hexdigest()


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
    target: Path,
    existing: dict[str, Any],
    new: dict[str, Any],
    prefix: tuple[str, ...],
    skipped: list[str],
    current: dict[str, Any],
) -> dict[str, Any]:
    """`existing` with `new` merged in (in `existing`'s key order, new keys after): objects
    merge, lists get the new items not already there, and any other value already set is kept,
    its key added to `skipped` (and its value to `current`) when the suggestion's differs. An
    object or list merged into a value of another kind is refused."""
    merged = dict(existing)
    for key, value in new.items():
        if key not in merged:
            merged[key] = value
            continue
        old = merged[key]
        dotted = ".".join((*prefix, key))
        if isinstance(value, dict):
            if not isinstance(old, dict):
                raise ApplyRefused(f"{dotted} in {target} is not an object; vigil will not merge")
            merged[key] = _merge(target, old, value, (*prefix, key), skipped, current)
        elif isinstance(value, list):
            if not isinstance(old, list):
                raise ApplyRefused(f"{dotted} in {target} is not a list; vigil will not merge")
            union = list(old)
            for item in value:
                if item not in union:
                    union.append(item)
            merged[key] = union
        elif old != value:
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
    """Write `c`: check the target again as `plan_change` did, re-read it and refuse (`CHANGED`)
    when it is not as previewed (`c.before_hash`), create `.claude/` (mode 0755) if needed, then
    replace the file at its path atomically (temp file in the same folder, then `os.replace`; an
    existing file's mode is kept). Raises ApplyRefused, leaving the file as it was, when anything
    fails."""
    parts = c.target.relative_to(c.folder).parts
    with _lock:
        if _check_target(c.folder, parts, _real(claude_home)) != c.target:
            raise ApplyRefused(CHANGED)
        data = _read(c.target)
        if file_hash(c.target, data) != c.before_hash:
            raise ApplyRefused(CHANGED)
        parent = c.target.parent
        try:
            if data is None and not os.path.lexists(parent):
                parent.mkdir(mode=_NEW_DIR_MODE)
            if _check_target(c.folder, parts, _real(claude_home)) != c.target:
                raise ApplyRefused(CHANGED)
            mode = stat.S_IMODE(c.target.lstat().st_mode) if data is not None else _NEW_FILE_MODE
            _replace(c.target, c.after, mode)
        except OSError as error:
            raise ApplyRefused(f"cannot write {c.target}: {error}") from error


def _replace(target: Path, text: str, mode: int) -> None:
    """Write `text` to a temp file next to `target`, then move it over `target`. The temp file is
    removed when anything fails."""
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


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ApplyRefused(f"cannot read {path}: {error}") from error


def _check_target(folder: Path, parts: tuple[str, ...], claude_home: Path) -> Path:
    """`folder` / `parts` (`folder` a real path), checked with lstat: no part of it is a symbolic
    link (dangling or not), the folders in it are folders, and the file is missing or a regular
    file without execute bits. Its real path must not be under `claude_home` (a real path),
    outside `folder`, or inside a `.git` folder."""
    target = folder.joinpath(*parts)
    for depth in range(1, len(parts) + 1):
        path = folder.joinpath(*parts[:depth])
        name = "/".join(parts[:depth])
        info = _lstat(path)
        if info is None:
            break
        if stat.S_ISLNK(info.st_mode):
            raise ApplyRefused(
                f"{name} is a symbolic link; vigil does not write through links. "
                "Copy the text instead."
            )
        if path != target and not stat.S_ISDIR(info.st_mode):
            raise ApplyRefused(f"{path} is not a folder")
        if path == target and not stat.S_ISREG(info.st_mode):
            raise ApplyRefused(f"{target} is not a regular file")
        if path == target and info.st_mode & _EXECUTE:
            raise ApplyRefused(f"{target} is executable; vigil will not change it")
    real = _real(target)
    if real.is_relative_to(claude_home):
        raise ApplyRefused(
            f"{target} is under the Claude home ({claude_home}); vigil never writes there"
        )
    if real != target or not real.is_relative_to(folder):
        raise ApplyRefused(f"{target} leads outside the project folder ({folder})")
    if ".git" in real.parts:
        raise ApplyRefused(f"{target} is inside a .git folder; vigil will not change it")
    return target


def _read(path: Path) -> bytes | None:
    """The bytes of the regular file `path` (a symlink there is not followed, and a FIFO or
    device is refused without waiting on it); None when it does not exist."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ApplyRefused(f"cannot read {path}: {error}") from error
    try:
        with os.fdopen(fd, "rb") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ApplyRefused(f"{path} is not a regular file")
            if info.st_mode & _EXECUTE:
                raise ApplyRefused(f"{path} is executable; vigil will not change it")
            return file.read()
    except OSError as error:
        raise ApplyRefused(f"cannot read {path}: {error}") from error
