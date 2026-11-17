"""vigil: index, resume and report on Claude Code sessions."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("vigil-sessions")
except PackageNotFoundError:  # pragma: no cover - only when run from an uninstalled checkout
    __version__ = "0+unknown"

__all__ = ["__version__"]
