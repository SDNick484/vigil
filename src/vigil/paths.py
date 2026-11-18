"""Where vigil keeps its config and data, following the XDG base directory spec."""

import os
from pathlib import Path


def _xdg_dir(env_var: str, default: str) -> Path:
    value = os.environ.get(env_var, "")
    # The spec says relative paths in these variables are invalid and must be ignored.
    if value and os.path.isabs(value):
        return Path(value)
    return Path.home() / default


def config_home() -> Path:
    """The user's config folder: ``$XDG_CONFIG_HOME``, else ``~/.config``."""
    return _xdg_dir("XDG_CONFIG_HOME", ".config")


def config_path() -> Path:
    """The config file: ``$XDG_CONFIG_HOME/vigil/config.toml``."""
    return config_home() / "vigil" / "config.toml"


def data_dir() -> Path:
    """The data directory: ``$XDG_DATA_HOME/vigil``."""
    return _xdg_dir("XDG_DATA_HOME", ".local/share") / "vigil"


def index_path() -> Path:
    """The SQLite index: ``$XDG_DATA_HOME/vigil/index.db``."""
    return data_dir() / "index.db"
