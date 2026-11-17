"""Grouping sessions into projects: by git remote when the folder has one, else by folder."""

import os
import subprocess
from functools import cache
from pathlib import PurePosixPath
from urllib.parse import urlsplit

UNKNOWN_LABEL = "(unknown)"

_KEPT_GIT_ENV = frozenset({"GIT_CEILING_DIRECTORIES", "GIT_CONFIG_NOSYSTEM"})

_DEFAULT_PORTS = {"ssh": 22, "git+ssh": 22, "ssh+git": 22, "https": 443, "http": 80, "git": 9418}


@cache
def project_for(cwd: str | None) -> tuple[str, str]:
    """`(project_key, label)` for a session's working directory.

    If `cwd` is in a git work tree with a `remote.origin.url`, the key is that URL normalized by
    `normalize_remote` and the label is the repository name. Otherwise the key is `cwd` itself and
    the label its last path component. A session with no known `cwd` gets `("", "(unknown)")`.
    Results are cached for the life of the process.
    """
    if not cwd:
        return "", UNKNOWN_LABEL
    remote = _origin_url(cwd)
    if remote:
        key = normalize_remote(remote)
        if key:
            return key, _last_component(key) or key
    return cwd, _last_component(cwd) or cwd


def normalize_remote(url: str) -> str:
    """A remote URL without scheme, credentials, default port, trailing slash or `.git`.

    `https://user:token@github.com/owner/repo.git` and `git@github.com:owner/repo.git` both give
    `github.com/owner/repo`; a local path remote gives the path.
    """
    url = url.strip()
    if "://" in url:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        try:
            port = parts.port
        except ValueError:
            port = None
        if port is not None and port != _DEFAULT_PORTS.get(parts.scheme.lower()):
            host = f"{host}:{port}"
        path = parts.path
        key = f"{host}/{path.lstrip('/')}" if host else path
    elif _is_scp_like(url):
        userhost, _, path = url.partition(":")
        host = userhost.rpartition("@")[2].lower()
        key = f"{host}/{path.lstrip('/')}"
    else:
        key = url
    key = key.rstrip("/")
    if key.endswith(".git"):
        key = key[: -len(".git")]
    return key.rstrip("/")


def _is_scp_like(url: str) -> bool:
    # git's rule: `host:path` or `user@host:path`, with no slash before the first colon.
    colon = url.find(":")
    slash = url.find("/")
    return colon > 0 and (slash == -1 or colon < slash)


def _last_component(path: str) -> str:
    return PurePosixPath(path.rstrip("/")).name


def _origin_url(cwd: str) -> str | None:
    # Ignore GIT_DIR, GIT_WORK_TREE and the like from our own environment: ask about `cwd` only.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") or k in _KEPT_GIT_ENV}
    try:
        inside = _git(cwd, env, "rev-parse", "--is-inside-work-tree")
        if inside != "true":
            return None
        return _git(cwd, env, "config", "--get", "remote.origin.url") or None
    except OSError, subprocess.SubprocessError:
        return None


def _git(cwd: str, env: dict[str, str], *args: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", cwd, *args],
        capture_output=True,
        text=True,
        errors="replace",
        env=env,
        timeout=5,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None
