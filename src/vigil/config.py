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

Mode = Literal["local", "hub"]
"""What a vigil is: `local` indexes this machine's sessions; `hub` indexes nothing and keeps the
sessions machines push to it (`vigil.hub`), and never runs `claude`."""

AccessScope = Literal["read", "push", "full"]
"""What a remote credential may do (`vigil.web.security.authorize`): `read` is GET and HEAD on
`/api/v1/*`; `push` is a hub's ingest for one machine; `full` is everything vigil's pages do."""


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
class TokenConfig:
    """One `[[server.tokens]]` entry: a token file and what its token may do."""

    file: Path
    """The file holding the token; same rules as `server.token_file`."""
    scope: AccessScope
    machine: str | None = None
    """The machine name a `push` token may push for (required for `push`, absent otherwise)."""


@dataclass(frozen=True)
class ServiceTokenConfig:
    """One `[[server.access.service_tokens]]` entry: a Cloudflare Access service token, known by
    its client ID (the `common_name` claim of its assertions), and what it may do."""

    client_id: str
    scope: AccessScope
    machine: str | None = None
    """As for `TokenConfig.machine`: required for `push`, absent otherwise."""


@dataclass(frozen=True)
class AccessConfig:
    """`[server.access]`: verify Cloudflare Access assertions on proxied requests
    (`vigil.web.access`)."""

    team_domain: str
    """`<team>.cloudflareaccess.com`, lowercase: where the signing keys are and who issues."""
    aud: tuple[str, ...]
    """The Access application's audience tags; an assertion must name one of them."""
    emails: frozenset[str] = frozenset()
    """User emails (lowercase) that get `full`."""
    service_tokens: tuple[ServiceTokenConfig, ...] = ()


@dataclass(frozen=True)
class ServerConfig:
    bind: str = "127.0.0.1"
    """The address `vigil serve` listens on: an IP address. Anything but 127.0.0.1 needs remote
    access (checked when the service starts, `vigil.web.security.check_bind_address`)."""
    token_file: Path | None = None
    """A file holding the `full` access token for remote clients; it must be mode 0600 (or
    0400)."""
    allowed_hosts: tuple[str, ...] = ()
    """Host names (lowercase, no port; IPv6 in brackets) remote clients may use in `Host`, on
    top of the loopback names and the bind address."""
    tokens: tuple[TokenConfig, ...] = ()
    """More access tokens, each with a scope (`[[server.tokens]]`)."""
    access: AccessConfig | None = None
    """Cloudflare Access verification (`[server.access]`); None when not configured."""

    @property
    def remote_enabled(self) -> bool:
        """Whether remote access is configured: a `token_file`, any `tokens`, or `access`."""
        return self.token_file is not None or bool(self.tokens) or self.access is not None


@dataclass(frozen=True)
class ReviewConfig:
    """How `vigil review` and the session page's Review run `claude -p` (`vigil.review.runner`)."""

    max_budget_usd: float = 3.0
    """`--max-budget-usd` for a review, 0.05 to 50. Claude Code checks it after each turn, so one
    turn can go over it."""
    model: str | None = "sonnet"
    """`--model` for a review; None (`model = ""` in the file) means Claude Code's default."""


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
class HubModeConfig:
    """`[hub_mode]`: settings of a vigil with `mode = "hub"`."""

    public_url: str | None = None
    """The hub's public base URL, `https://host[:port]` with no path (for `resume_url`)."""
    stale_after: int = 180
    """Seconds, 30 to 3600, after which a machine that has not pushed or sent a heartbeat is
    stale."""


@dataclass(frozen=True)
class HubMachine:
    """One `[[machines]]` entry: a machine allowed to push its sessions to this hub."""

    name: str
    """The name it pushes as (`/ingest/{name}/...`); same rules as `peers[].name`."""
    url: str | None = None
    """Its own vigil's base URL, `http(s)://host[:port]`, for links only; the hub never requests
    it."""


@dataclass(frozen=True)
class HubClientConfig:
    """`[hub]`: the hub this machine pushes its sessions to (`vigil.push`), with one kind of
    credential: a bearer token (`token_file`) or a Cloudflare Access service token
    (`access_client_id` and `access_secret_file`)."""

    url: str
    """The hub's base URL, `http(s)://host[:port]` with no path; credentials go only there."""
    token_file: Path | None = None
    """A file holding the hub's `push` token for this machine (a hub reached directly)."""
    access_client_id: str | None = None
    """An Access service token's client ID (`<id>.access`), sent as `CF-Access-Client-Id`."""
    access_secret_file: Path | None = None
    """A file holding that service token's secret, sent as `CF-Access-Client-Secret`."""
    live_interval: int = 900
    """Seconds, 60 to 3600, between the push loop's own probes of live state."""


@dataclass(frozen=True)
class Config:
    claude_home: Path = field(default_factory=lambda: Path.home() / ".claude")
    port: int = 7787
    resume: ResumeConfig = field(default_factory=ResumeConfig)
    usage: UsageConfig = field(default_factory=UsageConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    machine: str | None = None
    """What the machine column calls this machine; None means its host name."""
    peers: tuple[Peer, ...] = ()
    mode: Mode = "local"
    hub_mode: HubModeConfig = field(default_factory=HubModeConfig)
    """Only set (`[hub_mode]`) with `mode = "hub"`."""
    machines: tuple[HubMachine, ...] = ()
    """Only set (`[[machines]]`) with `mode = "hub"`."""
    hub: HubClientConfig | None = None
    """The hub to push this machine's sessions to (`[hub]`); never set with `mode = "hub"`."""


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


def _parse_tokens(value: Any) -> tuple[TokenConfig, ...]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ConfigError("`server.tokens` must be an array of tables ([[server.tokens]])")
    tokens: list[TokenConfig] = []
    for number, table in enumerate(cast(list[dict[str, Any]], value), start=1):
        where = f"server.tokens[{number}]"
        _check_keys(table, {"file", "scope", "machine"}, where)
        for key in ("file", "scope"):
            if key not in table:
                raise ConfigError(f"`{where}.{key}` is missing")
        scope, machine = _scope_and_machine(table, where)
        tokens.append(
            TokenConfig(
                file=_token_path(table["file"], f"{where}.file"),
                scope=scope,
                machine=machine,
            )
        )
    return tuple(tokens)


def _scope_and_machine(table: dict[str, Any], where: str) -> tuple[AccessScope, str | None]:
    """A credential entry's `scope` (present) and `machine` (required for `push`, forbidden
    otherwise)."""
    scope = table["scope"]
    if scope not in get_args(AccessScope):
        raise ConfigError(f"`{where}.scope` must be one of {', '.join(get_args(AccessScope))}")
    machine: str | None = None
    if scope == "push":
        if "machine" not in table:
            raise ConfigError(f"`{where}.machine` is missing: a push token is for one machine")
        machine = check_machine_name(table["machine"], f"{where}.machine")
    elif "machine" in table:
        raise ConfigError(f"`{where}.machine` is only for scope push")
    return cast(AccessScope, scope), machine


ACCESS_DOMAIN = ".cloudflareaccess.com"


def _team_domain(value: Any) -> str:
    """`server.access.team_domain`, lowercased: a host name `<team>.cloudflareaccess.com`."""
    host = value.strip().lower() if isinstance(value, str) else ""
    if (
        len(host) <= 253
        and _HOST_NAME.fullmatch(host)
        and host.endswith(ACCESS_DOMAIN)
        and len(host) > len(ACCESS_DOMAIN)
    ):
        return host
    raise ConfigError(
        "`server.access.team_domain` must be your team's host name, "
        f"<team>{ACCESS_DOMAIN} (no scheme or path), not {value!r}"
    )


def _audiences(value: Any) -> tuple[str, ...]:
    """`server.access.aud`: one audience tag or a non-empty list of them."""
    tags = [value] if isinstance(value, str) else value
    if (
        not isinstance(tags, list)
        or not tags
        or not all(isinstance(tag, str) and tag.strip() for tag in tags)
    ):
        raise ConfigError(
            "`server.access.aud` must be the application's audience tag, or a list of them"
        )
    return tuple(tag.strip() for tag in cast(list[str], tags))


def _single_word(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value.isprintable()
        and not any(char.isspace() for char in value)
    )


def _access_emails(value: Any) -> frozenset[str]:
    names = _string_list(value, "server.access.emails")
    emails = [name.strip().lower() for name in names]
    for email in emails:
        if not _single_word(email) or len(email) > 254:
            raise ConfigError(f"`server.access.emails`: not an email address: {email!r}")
    return frozenset(emails)


def _parse_service_tokens(value: Any) -> tuple[ServiceTokenConfig, ...]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ConfigError(
            "`server.access.service_tokens` must be an array of tables "
            "([[server.access.service_tokens]])"
        )
    tokens: list[ServiceTokenConfig] = []
    for number, table in enumerate(cast(list[dict[str, Any]], value), start=1):
        where = f"server.access.service_tokens[{number}]"
        _check_keys(table, {"client_id", "scope", "machine"}, where)
        for key in ("client_id", "scope"):
            if key not in table:
                raise ConfigError(f"`{where}.{key}` is missing")
        client_id = table["client_id"]
        if not _single_word(client_id) or len(client_id) > 255:
            raise ConfigError(f"`{where}.client_id` must be the service token's client ID")
        if any(token.client_id == client_id for token in tokens):
            raise ConfigError(f"`{where}.client_id`: {client_id!r} is listed twice")
        scope, machine = _scope_and_machine(table, where)
        tokens.append(ServiceTokenConfig(client_id=client_id, scope=scope, machine=machine))
    return tuple(tokens)


def _parse_access(value: Any) -> AccessConfig:
    if not isinstance(value, dict):
        raise ConfigError("`server.access` must be a table ([server.access])")
    table = cast(dict[str, Any], value)
    _check_keys(table, {"team_domain", "aud", "emails", "service_tokens"}, "server.access")
    for key in ("team_domain", "aud"):
        if key not in table:
            raise ConfigError(f"`server.access.{key}` is missing")
    return AccessConfig(
        team_domain=_team_domain(table["team_domain"]),
        aud=_audiences(table["aud"]),
        emails=_access_emails(table.get("emails", [])),
        service_tokens=_parse_service_tokens(table.get("service_tokens", [])),
    )


def _parse_server(raw: dict[str, Any]) -> ServerConfig:
    table = _table(raw, "server")
    _check_keys(table, {"bind", "token_file", "allowed_hosts", "tokens", "access"}, "server")
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
    tokens = defaults.tokens
    if "tokens" in table:
        tokens = _parse_tokens(table["tokens"])
    allowed_hosts = defaults.allowed_hosts
    if "allowed_hosts" in table:
        names = _string_list(table["allowed_hosts"], "server.allowed_hosts")
        allowed_hosts = tuple(_allowed_host(name) for name in names)
    access = defaults.access
    if "access" in table:
        access = _parse_access(table["access"])
    server = ServerConfig(
        bind=bind, token_file=token_file, allowed_hosts=allowed_hosts, tokens=tokens, access=access
    )
    if allowed_hosts and not server.remote_enabled:
        raise ConfigError(
            "`server.allowed_hosts` needs remote access "
            "(`server.token_file`, `[[server.tokens]]` or `[server.access]`)"
        )
    return server


def _parse_review(raw: dict[str, Any]) -> ReviewConfig:
    table = _table(raw, "review")
    _check_keys(table, {"max_budget_usd", "model"}, "review")
    defaults = ReviewConfig()
    budget = defaults.max_budget_usd
    if "max_budget_usd" in table:
        value = table["max_budget_usd"]
        if isinstance(value, bool) or not isinstance(value, int | float) or not 0.05 <= value <= 50:
            raise ConfigError("`review.max_budget_usd` must be a number from 0.05 to 50")
        budget = float(value)
    model = defaults.model
    if "model" in table:
        value = table["model"]
        if not isinstance(value, str):
            raise ConfigError("`review.model` must be a string")
        model = value.strip() or None
        if model is not None:
            try:
                check(["--model", model])
            except FlagRefused as error:
                raise ConfigError(f"review.model: {error.reason}") from error
    return ReviewConfig(max_budget_usd=budget, model=model)


MACHINE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}")
"""A machine name as peers and push tokens use it: 1 to 63 letters, digits, `-` and `_`,
starting with a letter or digit."""


def check_machine_name(value: Any, where: str) -> str:
    """`value` when it is a machine name (`MACHINE_NAME`); else ConfigError naming `where`."""
    if not isinstance(value, str) or not MACHINE_NAME.fullmatch(value):
        raise ConfigError(
            f"`{where}` must be 1 to 63 letters, digits, - or _ (starting with a letter or digit)"
        )
    return value


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
        name = check_machine_name(table["name"], f"{where}.name")
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


def _parse_mode(raw: dict[str, Any]) -> Mode:
    value = raw.get("mode", "local")
    if value not in get_args(Mode):
        raise ConfigError(f"`mode` must be one of {', '.join(get_args(Mode))}")
    return cast(Mode, value)


def _public_url(value: Any) -> str:
    problem = (
        "`hub_mode.public_url` must be https://host[:port] with no path, user name, query or "
        "fragment"
    )
    try:
        url = _peer_url(value, "hub_mode.public_url")
    except ConfigError:
        raise ConfigError(f"{problem}: {value!r}") from None
    if not url.startswith("https://"):
        raise ConfigError(f"{problem}: {value!r}")
    return url


def _parse_hub_mode(raw: dict[str, Any]) -> HubModeConfig:
    table = _table(raw, "hub_mode")
    _check_keys(table, {"public_url", "stale_after"}, "hub_mode")
    defaults = HubModeConfig()
    public_url = defaults.public_url
    if "public_url" in table:
        public_url = _public_url(table["public_url"])
    stale_after = defaults.stale_after
    if "stale_after" in table:
        value = table["stale_after"]
        if isinstance(value, bool) or not isinstance(value, int) or not 30 <= value <= 3600:
            raise ConfigError("`hub_mode.stale_after` must be an integer from 30 to 3600")
        stale_after = value
    return HubModeConfig(public_url=public_url, stale_after=stale_after)


def _parse_machines(raw: dict[str, Any]) -> tuple[HubMachine, ...]:
    value = raw.get("machines", [])
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ConfigError("`machines` must be an array of tables ([[machines]])")
    machines: list[HubMachine] = []
    for number, table in enumerate(cast(list[dict[str, Any]], value), start=1):
        where = f"machines[{number}]"
        _check_keys(table, {"name", "url"}, where)
        if "name" not in table:
            raise ConfigError(f"`{where}.name` is missing")
        name = check_machine_name(table["name"], f"{where}.name")
        if any(machine.name == name for machine in machines):
            raise ConfigError(f"`{where}.name`: two machines are called {name!r}")
        url = _peer_url(table["url"], where) if "url" in table else None
        machines.append(HubMachine(name=name, url=url))
    return tuple(machines)


def _parse_hub_client(raw: dict[str, Any], machine: str | None) -> HubClientConfig | None:
    """`[hub]`, or None when it is not there. It needs `machine` set to a machine name (the name
    the hub knows this machine by) and exactly one credential."""
    if "hub" not in raw:
        return None
    table = _table(raw, "hub")
    _check_keys(
        table,
        {"url", "token_file", "access_client_id", "access_secret_file", "live_interval"},
        "hub",
    )
    if "url" not in table:
        raise ConfigError("`hub.url` is missing")
    url = _peer_url(table["url"], "hub")
    if machine is None:
        raise ConfigError("`machine` must be set with [hub]: it is the name this machine pushes as")
    check_machine_name(machine, "machine")
    token_file = (
        _token_path(table["token_file"], "hub.token_file") if "token_file" in table else None
    )
    client_id: str | None = None
    if "access_client_id" in table:
        client_id = table["access_client_id"]
        if not _single_word(client_id) or len(cast(str, client_id)) > 255:
            raise ConfigError("`hub.access_client_id` must be the service token's client ID")
    secret_file = (
        _token_path(table["access_secret_file"], "hub.access_secret_file")
        if "access_secret_file" in table
        else None
    )
    if (client_id is None) != (secret_file is None):
        raise ConfigError(
            "`hub.access_client_id` and `hub.access_secret_file` go together: give both or neither"
        )
    if (token_file is None) == (client_id is None):
        raise ConfigError(
            "[hub] needs one credential: `hub.token_file`, or `hub.access_client_id` with "
            "`hub.access_secret_file` (not both)"
        )
    live_interval = HubClientConfig.live_interval
    if "live_interval" in table:
        value = table["live_interval"]
        if isinstance(value, bool) or not isinstance(value, int) or not 60 <= value <= 3600:
            raise ConfigError("`hub.live_interval` must be an integer from 60 to 3600")
        live_interval = value
    return HubClientConfig(
        url=url,
        token_file=token_file,
        access_client_id=client_id,
        access_secret_file=secret_file,
        live_interval=live_interval,
    )


def _check_mode(raw: dict[str, Any], mode: Mode) -> None:
    """Refuse settings that do not go with `mode`: peers and `[hub]` on a hub; `[hub_mode]` and
    `[[machines]]` anywhere else."""
    if mode == "hub":
        if raw.get("peers"):
            raise ConfigError('`peers` cannot be used with `mode = "hub"`')
        if "hub" in raw:
            raise ConfigError(
                '`hub` ([hub]) cannot be used with `mode = "hub"`: a hub pushes nowhere'
            )
        return
    for key, name in (("hub_mode", "[hub_mode]"), ("machines", "[[machines]]")):
        if key in raw:
            raise ConfigError(f'`{key}` ({name}) needs `mode = "hub"`')


def load_config(path: Path | None = None) -> Config:
    """Load the config file; a missing file gives the defaults.

    Raises ConfigError on unreadable or malformed TOML, unknown keys, wrong types, a default
    flag or review model outside the allow-list, a review budget out of range, a `server` value
    that is not an address, path or host name, a `[[server.tokens]]` entry without a valid file,
    scope and (for `push` only) machine, a `[server.access]` without a `<team>.cloudflareaccess.com`
    team domain and audience tags (or with a service token like a bad `[[server.tokens]]` entry,
    or a client ID listed twice), a `peers` entry without a valid name, URL and token file path,
    a `mode` other than `local` or `hub`, `peers` with `mode = "hub"`, `[hub_mode]` or
    `[[machines]]` without it, a `[[machines]]` entry without a valid, unique name, or a `[hub]`
    without a valid URL, exactly one credential, a `live_interval` of 60 to 3600 and `machine`
    set to a machine name (`MACHINE_NAME`), or with `mode = "hub"`.
    """
    path = path if path is not None else config_path()
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError:
        return Config()
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"cannot read config {path}: {error}") from error

    _check_keys(
        raw,
        {
            "claude_home",
            "port",
            "resume",
            "usage",
            "server",
            "review",
            "machine",
            "peers",
            "mode",
            "hub_mode",
            "machines",
            "hub",
        },
        "",
    )
    mode = _parse_mode(raw)
    _check_mode(raw, mode)
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
        review=_parse_review(raw),
        machine=machine,
        peers=_parse_peers(raw, machine),
        mode=mode,
        hub_mode=_parse_hub_mode(raw),
        machines=_parse_machines(raw),
        hub=_parse_hub_client(raw, machine),
    )
