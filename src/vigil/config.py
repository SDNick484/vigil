"""Configuration: a TOML file validated by hand, with every key optional."""

import ipaddress
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast, get_args
from urllib.parse import urlsplit

from vigil.flags import FlagRefused, check
from vigil.paths import config_path

Launcher = Literal["auto", "bg", "tmux"]


class ConfigError(Exception):
    """The config file is unreadable, malformed, or has an invalid value."""


@dataclass(frozen=True)
class ResumeConfig:
    default_flags: tuple[str, ...] = ()
    terminal_command: str | None = None
    launcher: Launcher = "auto"
    remote_link_timeout: float = 30.0
    """Seconds to wait for a Remote Control link after starting a session for a remote request
    (1 to 300)."""


@dataclass(frozen=True)
class UsageConfig:
    shell_history: bool = False
    history_files: tuple[Path, ...] = ()
    claude_aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ServerConfig:
    bind: str = "127.0.0.1"
    """The address `vigil serve` listens on: an IP address. Anything but 127.0.0.1 needs
    `token_file` (checked when the service starts, `vigil.web.security.check_bind_address`)."""
    token_file: Path | None = None
    """A file holding the access token for remote clients; it must be mode 0600 (or 0400)."""
    allowed_hosts: tuple[str, ...] = ()
    """Host names (lowercase, no port; IPv6 in brackets) remote clients may use in `Host`, on
    top of the loopback names and the bind address."""


@dataclass(frozen=True)
class Peer:
    """Another machine's vigil, whose sessions this one lists (v2, `vigil.peers`)."""

    name: str
    """What the machine column shows for its sessions; letters, digits, `-` and `_`."""
    url: str
    """Its base URL, `http(s)://host[:port]` with no path, user, query or fragment."""
    token_file: Path
    """A file holding that peer's access token (its `server.token_file` token); it is only ever
    sent to `url`. Checked like `server.token_file` when used (`vigil.web.auth.load_token`)."""


@dataclass(frozen=True)
class Config:
    claude_home: Path = field(default_factory=lambda: Path.home() / ".claude")
    port: int = 7787
    resume: ResumeConfig = field(default_factory=ResumeConfig)
    usage: UsageConfig = field(default_factory=UsageConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    machine: str | None = None
    """What the machine column calls this machine; None means its host name."""
    peers: tuple[Peer, ...] = ()


def _table(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"`{key}` must be a table")
    return cast(dict[str, Any], value)


def _check_keys(table: dict[str, Any], allowed: set[str], where: str) -> None:
    for key in table:
        if key not in allowed:
            prefix = f"{where}." if where else ""
            raise ConfigError(f"unknown config key: {prefix}{key}")


def _string_list(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"`{name}` must be a list of strings")
    return tuple(value)


def _validate_flags(flags: tuple[str, ...]) -> None:
    """Allow only flags `vigil.flags` lists, each with a value its rule accepts."""
    try:
        check(flags)
    except FlagRefused as error:
        raise ConfigError(f"resume.default_flags: {error.reason}") from error


def _parse_resume(raw: dict[str, Any]) -> ResumeConfig:
    table = _table(raw, "resume")
    _check_keys(
        table, {"default_flags", "terminal_command", "launcher", "remote_link_timeout"}, "resume"
    )
    defaults = ResumeConfig()
    flags = defaults.default_flags
    if "default_flags" in table:
        flags = _string_list(table["default_flags"], "resume.default_flags")
        _validate_flags(flags)
    command = defaults.terminal_command
    if "terminal_command" in table:
        command = table["terminal_command"]
        if not isinstance(command, str):
            raise ConfigError("`resume.terminal_command` must be a string")
        if "{attach}" not in command:
            raise ConfigError("`resume.terminal_command` must contain {attach}")
    launcher = defaults.launcher
    if "launcher" in table:
        value = table["launcher"]
        if value not in get_args(Launcher):
            raise ConfigError(f"`resume.launcher` must be one of {', '.join(get_args(Launcher))}")
        launcher = value
    timeout = defaults.remote_link_timeout
    if "remote_link_timeout" in table:
        value = table["remote_link_timeout"]
        if isinstance(value, bool) or not isinstance(value, int | float) or not 1 <= value <= 300:
            raise ConfigError("`resume.remote_link_timeout` must be a number from 1 to 300")
        timeout = float(value)
    return ResumeConfig(
        default_flags=flags,
        terminal_command=command,
        launcher=launcher,
        remote_link_timeout=timeout,
    )


def _parse_usage(raw: dict[str, Any]) -> UsageConfig:
    table = _table(raw, "usage")
    _check_keys(table, {"shell_history", "history_files", "claude_aliases"}, "usage")
    defaults = UsageConfig()
    shell_history = defaults.shell_history
    if "shell_history" in table:
        shell_history = table["shell_history"]
        if not isinstance(shell_history, bool):
            raise ConfigError("`usage.shell_history` must be true or false")
    history_files = defaults.history_files
    if "history_files" in table:
        names = _string_list(table["history_files"], "usage.history_files")
        history_files = tuple(Path(name).expanduser() for name in names)
    aliases = defaults.claude_aliases
    if "claude_aliases" in table:
        aliases = _string_list(table["claude_aliases"], "usage.claude_aliases")
    return UsageConfig(
        shell_history=shell_history, history_files=history_files, claude_aliases=aliases
    )


_HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST_NAME = re.compile(rf"{_HOST_LABEL}(?:\.{_HOST_LABEL})*")


def _allowed_host(value: str) -> str:
    """A `server.allowed_hosts` entry, lowercased: a DNS name, an IPv4 address or a bracketed
    IPv6 address, without a port, scheme or wildcard."""
    host = value.strip().lower()
    if host.startswith("[") and host.endswith("]"):
        try:
            ipaddress.IPv6Address(host[1:-1])
        except ValueError:
            pass
        else:
            return host
    elif len(host) <= 253 and _HOST_NAME.fullmatch(host):
        return host
    raise ConfigError(
        f"server.allowed_hosts: not a host name: {value!r} "
        "(give a name or address without scheme or port; IPv6 in brackets)"
    )


def _token_path(value: Any, name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"`{name}` must be a path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ConfigError(f"`{name}` must be an absolute path (or start with ~)")
    return path


def _parse_server(raw: dict[str, Any]) -> ServerConfig:
    table = _table(raw, "server")
    _check_keys(table, {"bind", "token_file", "allowed_hosts"}, "server")
    defaults = ServerConfig()
    bind = defaults.bind
    if "bind" in table:
        bind = table["bind"]
        if not isinstance(bind, str):
            raise ConfigError("`server.bind` must be a string")
        try:
            ipaddress.ip_address(bind)
        except ValueError:
            raise ConfigError(f"`server.bind` must be an IP address, not {bind!r}") from None
    token_file = defaults.token_file
    if "token_file" in table:
        token_file = _token_path(table["token_file"], "server.token_file")
    allowed_hosts = defaults.allowed_hosts
    if "allowed_hosts" in table:
        names = _string_list(table["allowed_hosts"], "server.allowed_hosts")
        allowed_hosts = tuple(_allowed_host(name) for name in names)
        if allowed_hosts and token_file is None:
            raise ConfigError("`server.allowed_hosts` needs `server.token_file`")
    return ServerConfig(bind=bind, token_file=token_file, allowed_hosts=allowed_hosts)


_PEER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}")


def _peer_url(value: Any, where: str) -> str:
    """A peer's base URL, normalised to `scheme://host[:port]` with the host lowercased."""
    if not isinstance(value, str):
        raise ConfigError(f"`{where}.url` must be a string")
    problem = "must be http(s)://host[:port] with no path, user name, query or fragment"
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        raise ConfigError(f"`{where}.url` {problem}: {value!r}") from None
    host = parts.hostname
    if (
        parts.scheme not in ("http", "https")
        or not host
        or parts.username is not None
        or parts.password is not None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or "?" in value
        or "#" in value
    ):
        raise ConfigError(f"`{where}.url` {problem}: {value!r}")
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}" + (f":{port}" if port is not None else "")


def _parse_peers(raw: dict[str, Any], machine: str | None) -> tuple[Peer, ...]:
    value = raw.get("peers", [])
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ConfigError("`peers` must be an array of tables ([[peers]])")
    peers: list[Peer] = []
    for number, table in enumerate(cast(list[dict[str, Any]], value), start=1):
        where = f"peers[{number}]"
        _check_keys(table, {"name", "url", "token_file"}, where)
        for key in ("name", "url", "token_file"):
            if key not in table:
                raise ConfigError(f"`{where}.{key}` is missing")
        name = table["name"]
        if not isinstance(name, str) or not _PEER_NAME.fullmatch(name):
            raise ConfigError(
                f"`{where}.name` must be 1 to 63 letters, digits, - or _ "
                "(starting with a letter or digit)"
            )
        if any(peer.name == name for peer in peers):
            raise ConfigError(f"`{where}.name`: two peers are called {name!r}")
        if name == machine:
            raise ConfigError(f"`{where}.name`: {name!r} is this machine's name (`machine`)")
        peers.append(
            Peer(
                name=name,
                url=_peer_url(table["url"], where),
                token_file=_token_path(table["token_file"], f"{where}.token_file"),
            )
        )
    return tuple(peers)


def _parse_machine(raw: dict[str, Any]) -> str | None:
    if "machine" not in raw:
        return None
    value = raw["machine"]
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 63
        or not value.isprintable()
    ):
        raise ConfigError("`machine` must be a name of 1 to 63 printable characters")
    return value.strip()


def load_config(path: Path | None = None) -> Config:
    """Load the config file; a missing file gives the defaults.

    Raises ConfigError on unreadable or malformed TOML, unknown keys, wrong types, a default
    flag outside the allow-list, a `server` value that is not an address, path or host name, or
    a `peers` entry without a valid name, URL and token file path.
    """
    path = path if path is not None else config_path()
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError:
        return Config()
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"cannot read config {path}: {error}") from error

    _check_keys(raw, {"claude_home", "port", "resume", "usage", "server", "machine", "peers"}, "")
    defaults = Config()
    claude_home = defaults.claude_home
    if "claude_home" in raw:
        if not isinstance(raw["claude_home"], str):
            raise ConfigError("`claude_home` must be a string")
        claude_home = Path(raw["claude_home"]).expanduser()
    port = defaults.port
    if "port" in raw:
        port = raw["port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ConfigError("`port` must be an integer from 1 to 65535")
    machine = _parse_machine(raw)
    return Config(
        claude_home=claude_home,
        port=port,
        resume=_parse_resume(raw),
        usage=_parse_usage(raw),
        server=_parse_server(raw),
        machine=machine,
        peers=_parse_peers(raw, machine),
    )
