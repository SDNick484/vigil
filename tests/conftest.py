"""Shared fixtures. Append new fixtures at the end of this file."""

import json
import stat
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

NO_JOB = "No job matching '{}'. Run 'claude agents' to list running sessions.\n"
"""What the real `claude attach`, `stop` and `logs` print (to stderr, exiting 1) for an id that
is not a job id; a fake `claude` does the same (`FakeBin.install`, `tests.test_api`)."""
JOB_COMMANDS = ("attach", "stop", "logs")

_STUB = """#!{python}
import json, os, re, sys
with open({log!r}, "a") as handle:
    handle.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd()}}) + "\\n")
# As the real `claude`: attach, stop and logs take the 8-character job id, never a session id.
job = sys.argv[1:3]
JOB_COMMANDS, NO_JOB = {job_commands!r}, {no_job!r}
if os.path.basename(sys.argv[0]) == "claude" and len(job) == 2 and job[0] in JOB_COMMANDS:
    if not re.fullmatch("[0-9a-f]{{8}}", job[1]):
        sys.stderr.write(NO_JOB.format(job[1]))
        sys.exit(1)
sys.stdout.write({stdout!r})
sys.exit({exit_code})
"""


def freeze_tree(root: Path) -> None:
    """Make a tree read-only, so a test can prove vigil never writes under it."""
    for path in [root, *root.rglob("*")]:
        if not path.is_symlink():
            path.chmod(path.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def thaw_tree(root: Path) -> None:
    """Undo freeze_tree so the tree can be changed or cleaned up."""
    for path in [root, *root.rglob("*")]:
        if not path.is_symlink():
            path.chmod(path.stat().st_mode | stat.S_IWUSR)


@pytest.fixture
def claude_home(tmp_path: Path) -> Iterator[Path]:
    """An empty Claude home. Populate it, then call freeze_tree() to make it read-only.

    The tree is made writable again at teardown so pytest can clean it up.
    """
    home = tmp_path / "claude_home"
    home.mkdir()
    yield home
    thaw_tree(home)


@dataclass(frozen=True)
class Call:
    argv: list[str]
    cwd: str


class FakeBin:
    """Recording stand-ins for external programs, installed on PATH."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._logs = directory / ".calls"
        self._logs.mkdir(parents=True, exist_ok=True)

    def install(self, name: str, stdout: str = "", exit_code: int = 0) -> Path:
        """Install an executable `name` that records argv and cwd, prints stdout, exits. As
        `claude`, it refuses `attach`, `stop` and `logs` with anything but an 8-character job id,
        as the real one does (`NO_JOB`)."""
        log = self._logs / f"{name}.jsonl"
        script = self.directory / name
        script.write_text(
            _STUB.format(
                python=sys.executable,
                log=str(log),
                stdout=stdout,
                exit_code=exit_code,
                job_commands=JOB_COMMANDS,
                no_job=NO_JOB,
            )
        )
        script.chmod(0o755)
        return script

    def calls(self, name: str) -> list[Call]:
        """Every recorded invocation of `name`, in order."""
        log = self._logs / f"{name}.jsonl"
        if not log.exists():
            return []
        lines = log.read_text().splitlines()
        return [Call(argv=entry["argv"], cwd=entry["cwd"]) for entry in map(json.loads, lines)]


@pytest.fixture
def fake_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeBin:
    """Put recording fakes on PATH. PATH is only the fake directory, so no real `claude`,
    `tmux` or terminal can ever be launched; an executable that is not installed is missing."""
    directory = tmp_path / "fakebin"
    directory.mkdir()
    monkeypatch.setenv("PATH", str(directory))
    return FakeBin(directory)


# Imported here rather than at the top so this file stays append-only.
import shutil  # noqa: E402
import sqlite3  # noqa: E402

from vigil.index.db import connect  # noqa: E402
from vigil.index.scanner import scan  # noqa: E402

FIXTURE_TRANSCRIPTS = Path(__file__).parent / "fixtures" / "transcripts"
FIXTURE_PROJECT_DIR = "-home-user-src-fixture-repo"
HANDMADE_PROJECT_DIR = "-home-user-src-broken-repo"
SKIP_REMOTE_FIXTURE = shutil.ignore_patterns("remote-control.jsonl", "usage.jsonl")
"""For copying the hand-made transcripts: the Remote Control and usage ones are only for tests that
name them."""


def install_fixture_transcripts(claude_home: Path) -> Path:
    """Copy the fixture transcripts into `claude_home/projects/` in Claude Code's layout (the
    real ones, with their subagent folder, in one project folder; the hand-made one in another;
    `remote-control.jsonl` is left out, tests that need it copy it themselves).
    Returns the `projects` folder."""
    projects = claude_home / "projects"
    shutil.copytree(FIXTURE_TRANSCRIPTS / "2.1.312", projects / FIXTURE_PROJECT_DIR)
    shutil.copytree(
        FIXTURE_TRANSCRIPTS / "handmade",
        projects / HANDMADE_PROJECT_DIR,
        ignore=SKIP_REMOTE_FIXTURE,
    )
    return projects


@pytest.fixture
def index_db(tmp_path: Path, claude_home: Path) -> Iterator[sqlite3.Connection]:
    """An index at `tmp_path/data/index.db` built by scanning the fixture transcripts, which are
    installed in the `claude_home` fixture (left writable; call freeze_tree() to lock it)."""
    install_fixture_transcripts(claude_home)
    conn = connect(tmp_path / "data" / "index.db")
    scan(conn, claude_home)
    yield conn
    conn.close()


@pytest.fixture
def proc_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point WSL detection at a fake `/proc/version` (a plain Linux kernel string), so tests never
    depend on the real machine. Overwrite the file to simulate WSL."""
    import vigil.terminal

    path = tmp_path / "proc_version"
    path.write_text("Linux version 6.0.0 (builder@example) (gcc) #1 SMP\n")
    monkeypatch.setattr(vigil.terminal, "PROC_VERSION", path)
    return path


@dataclass(frozen=True)
class VigilEnv:
    """A CLI run's whole world, all under tmp: HOME, XDG dirs, a config pointing at `claude_home`
    (holding the fixture transcripts) and fakes on PATH."""

    claude_home: Path
    index: Path
    bin: FakeBin


@pytest.fixture
def vigil_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, claude_home: Path, fake_bin: FakeBin
) -> VigilEnv:
    """Isolate a CLI run from the real config, data folder and `~/.claude`. No `claude` is on PATH
    until a test installs a fake; there is no display, so no terminal is found."""
    install_fixture_transcripts(claude_home)
    config_home = tmp_path / "xdg_config"
    data_home = tmp_path / "xdg_data"
    (config_home / "vigil").mkdir(parents=True)
    (config_home / "vigil" / "config.toml").write_text(f'claude_home = "{claude_home}"\n')
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "WSL_DISTRO_NAME", "TERMINAL"):
        monkeypatch.delenv(name, raising=False)
    return VigilEnv(claude_home, data_home / "vigil" / "index.db", fake_bin)


# The web service (Task 7).
from fastapi.testclient import TestClient  # noqa: E402

from vigil.config import load_config  # noqa: E402
from vigil.web.app import create_app  # noqa: E402

LOOPBACK_URL = "http://127.0.0.1:7787"
"""Where tests reach the service. As an `Origin` header it makes a POST same-origin."""


@pytest.fixture
def web_client(vigil_env: VigilEnv) -> Iterator[TestClient]:
    """The web service over the fixture transcripts (scanned before the app starts; no watcher),
    in the `vigil_env` world. Requests go to `LOOPBACK_URL` with `Origin: LOOPBACK_URL`, as from a
    vigil page in the browser; `del client.headers["origin"]` to send none."""
    conn = connect(vigil_env.index)
    scan(conn, vigil_env.claude_home)
    conn.close()
    app = create_app(
        load_config(), conn_factory=lambda: connect(vigil_env.index), start_watcher=False
    )
    with TestClient(app, base_url=LOOPBACK_URL, headers={"Origin": LOOPBACK_URL}) as client:
        yield client


# Cloudflare Access (hub plan, Task 6).
import threading  # noqa: E402
import time  # noqa: E402
from typing import Any  # noqa: E402

import httpx  # noqa: E402
import jwt  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from jwt.algorithms import RSAAlgorithm  # noqa: E402

ACCESS_TEAM = "example-team.cloudflareaccess.com"
ACCESS_ISSUER = f"https://{ACCESS_TEAM}"
CERTS_URL = f"https://{ACCESS_TEAM}/cdn-cgi/access/certs"
ACCESS_AUD = "aud-placeholder"


@dataclass(frozen=True)
class AccessKeys:
    """A signing key as Cloudflare Access holds it, and its public half as the certs endpoint
    serves it (a JWK with `kid`)."""

    private_key: rsa.RSAPrivateKey
    kid: str
    jwks: dict[str, Any]


def generate_access_keys(kid: str = "kid-placeholder-1", key_size: int = 2048) -> AccessKeys:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    jwk = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    return AccessKeys(private_key, kid, {**jwk, "kid": kid, "alg": "RS256", "use": "sig"})


@pytest.fixture
def access_keys() -> AccessKeys:
    """A fresh RSA 2048 Access signing key."""
    return generate_access_keys()


def make_assertion(keys: AccessKeys, **claims: Any) -> str:
    """A `Cf-Access-Jwt-Assertion` signed with `keys` (RS256, `kid` in the header): a user's for
    `author@example.com`, issued now and valid for 5 minutes, unless `claims` say otherwise; a
    claim given as None is left out."""
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": ACCESS_ISSUER,
        "aud": [ACCESS_AUD],
        "iat": now,
        "exp": now + 300,
        "email": "author@example.com",
        **claims,
    }
    payload = {name: value for name, value in payload.items() if value is not None}
    return jwt.encode(payload, keys.private_key, algorithm="RS256", headers={"kid": keys.kid})


class CertsServer(httpx.MockTransport):
    """The team's certs endpoint (`CERTS_URL`) serving `{"keys": [...]}` for `keys`. Counts
    `fetches`; with `fail` it cannot be reached; with `block` each fetch waits (up to 10 s) for
    the event to be set; with `redirect` it answers 302 to that URL. Every request it sees is in
    `requests`."""

    def __init__(self, *keys: AccessKeys) -> None:
        self.jwks = [key.jwks for key in keys]
        self.fetches = 0
        self.fail = False
        self.block: threading.Event | None = None
        self.redirect: str | None = None
        self.requests: list[httpx.Request] = []
        super().__init__(self._answer)

    def _answer(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) != CERTS_URL:
            return httpx.Response(404)
        self.fetches += 1
        if self.block is not None:
            self.block.wait(10)
        if self.fail:
            raise httpx.ConnectError("certs endpoint unreachable", request=request)
        if self.redirect is not None:
            return httpx.Response(302, headers={"Location": self.redirect})
        return httpx.Response(200, json={"keys": self.jwks})


# Hub mode (vigil hub, Task 9).
from fastapi import FastAPI  # noqa: E402

FULL = "hub-full-token-placeholder-" + "0" * 16
"""The hub's `server.token_file` token (scope `full`). Every hub token is a placeholder."""
READ = "hub-read-token-placeholder-" + "1" * 16
PUSH_LAPTOP = "hub-push-laptop-placeholder-" + "2" * 16
PUSH_DESK = "hub-push-desk-placeholder-" + "3" * 16
PUSH_GHOST = "hub-push-ghost-placeholder-" + "4" * 16
"""A `push` token for `ghost`, a machine the hub's `[[machines]]` does not list."""
HUB_HOST = "hub.example.com"


@dataclass(frozen=True)
class HubEnv:
    """A hub's world, all under tmp: its config file, its data folder and fakes on PATH."""

    config_path: Path
    data_dir: Path
    bin: FakeBin


def _hub_token(directory: Path, name: str, token: str) -> Path:
    path = directory / name
    path.write_text(token + "\n")
    path.chmod(0o600)
    return path


@pytest.fixture
def hub_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_bin: FakeBin) -> HubEnv:
    """A vigil in hub mode: machines `laptop` (with a URL) and `desk`, a full `token_file`, a
    read token and push tokens for `laptop`, `desk` and the unlisted `ghost`, `HUB_HOST`
    allowed. Its `claude_home` does not exist, and a fake `claude` is installed so a test can
    assert it was never run."""
    config_home = tmp_path / "xdg_config"
    data_home = tmp_path / "xdg_data"
    tokens = tmp_path / "tokens"
    (config_home / "vigil").mkdir(parents=True)
    tokens.mkdir()
    entries: list[tuple[str, str, str, str | None]] = [
        ("read", READ, "read", None),
        ("laptop", PUSH_LAPTOP, "push", "laptop"),
        ("desk", PUSH_DESK, "push", "desk"),
        ("ghost", PUSH_GHOST, "push", "ghost"),
    ]
    server = [
        "[server]",
        f'allowed_hosts = ["{HUB_HOST}"]',
        f'token_file = "{_hub_token(tokens, "full", FULL)}"',
    ]
    for name, token, scope, machine in entries:
        server += ["", "[[server.tokens]]", f'file = "{_hub_token(tokens, name, token)}"']
        server += [f'scope = "{scope}"'] + ([f'machine = "{machine}"'] if machine else [])
    config = config_home / "vigil" / "config.toml"
    config.write_text(
        "\n".join(
            [
                'mode = "hub"',
                f'claude_home = "{tmp_path / "no-claude-home"}"',
                "",
                "[hub_mode]",
                f'public_url = "https://{HUB_HOST}"',
                "",
                *server,
                "",
                "[[machines]]",
                'name = "laptop"',
                'url = "http://laptop.example.com:7787"',
                "",
                "[[machines]]",
                'name = "desk"',
                "",
            ]
        )
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    fake_bin.install("claude", stdout="2.1.312 (Claude Code)\n")
    return HubEnv(config, data_home / "vigil", fake_bin)


@pytest.fixture
def hub_app(hub_env: HubEnv) -> FastAPI:
    """The service in hub mode (`hub_env`), as `vigil serve` builds it: no index, no watcher."""
    return create_app(load_config(hub_env.config_path))
