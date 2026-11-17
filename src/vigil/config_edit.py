"""Changing vigil's own config file: `add_default_flags`, for "Always use" on a review's flag
suggestion. This is the only write to vigil's config from the web.

The file keeps everything else as written, comments included (it is edited with tomlkit), and is
replaced only once the result loads (`vigil.config.load_config`), atomically.
"""

import os
import tempfile
import threading
from collections.abc import MutableMapping, Sequence
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.exceptions import TOMLKitError

from vigil.config import ConfigError, load_config
from vigil.flags import FlagRefused, check, merge

_NEW_FILE_MODE = 0o644

_lock = threading.Lock()
"""Held from reading the file to replacing it, so two edits at once do not lose one."""


def _resume_table(document: tomlkit.TOMLDocument) -> MutableMapping[str, Any]:
    table = document.get("resume")
    if table is None:
        table = tomlkit.table()
        document["resume"] = table
    if not isinstance(table, MutableMapping):
        raise ConfigError("`resume` must be a table")
    return table


def add_default_flags(path: Path, tokens: Sequence[str]) -> None:
    """Add `tokens` (flags, as `vigil.flags.check` takes them) to `resume.default_flags` in the
    config file `path`: a flag already there gets the new value, the others are added after the
    existing ones (`vigil.flags.merge`). A missing file is created with just that.

    The rest of the file is kept as written. The result is loaded with `load_config` before it
    replaces the file (temp file in the same folder, then `os.replace`; the file's mode is kept).
    Raises ConfigError, leaving the file as it was, when it cannot be read or parsed, when a flag
    is refused, or when the result does not load.
    """
    target = path.resolve()  # through a symlink, not over it
    with _lock:
        try:
            text: str | None = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = None
        except (OSError, UnicodeDecodeError) as error:
            raise ConfigError(f"cannot read config {path}: {error}") from error
        try:
            document = tomlkit.parse(text) if text is not None else tomlkit.document()
        except TOMLKitError as error:
            raise ConfigError(f"cannot read config {path}: {error}") from error
        resume = _resume_table(document)
        old = resume.get("default_flags", [])
        if not isinstance(old, list) or not all(isinstance(item, str) for item in old):
            raise ConfigError("`resume.default_flags` must be a list of strings")
        try:
            flags = merge(check([str(item) for item in old]), check(tokens))
        except FlagRefused as error:
            raise ConfigError(f"resume.default_flags: {error.reason}") from error
        resume["default_flags"] = flags
        mode = target.stat().st_mode & 0o777 if text is not None else _NEW_FILE_MODE
        _replace(target, tomlkit.dumps(document), mode)


def _replace(target: Path, text: str, mode: int) -> None:
    """Write `text` to a temp file next to `target`, check that it loads, then move it over
    `target`. The temp file is removed when anything fails."""
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    temp = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        temp.chmod(mode)
        load_config(temp)
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
