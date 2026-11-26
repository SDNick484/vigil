"""deploy/alpine/: the hub's Alpine install script, OpenRC script and example config.

Nothing here installs anything: the dry run runs with PATH holding recording fakes only.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

from vigil.config import load_config

DEPLOY = Path(__file__).resolve().parent.parent / "deploy" / "alpine"
SCRIPT = DEPLOY / "install.sh"
SH = shutil.which("sh") or "/bin/sh"
FAKES = ("apk", "uv", "adduser", "addgroup", "install", "id")


def test_shellcheck_alpine_install() -> None:
    """Fails, never skips, when shellcheck is missing: CI installs it, and so does `.[dev]`."""
    found = shutil.which("shellcheck") or str(Path(sys.executable).parent / "shellcheck")
    assert Path(found).exists(), "shellcheck is not installed; install it to run this gate"
    done = subprocess.run([found, "-s", "sh", str(SCRIPT)], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr


def test_initd_syntax_and_service() -> None:
    initd = DEPLOY / "vigil.initd"
    assert subprocess.run([SH, "-n", str(initd)]).returncode == 0
    text = initd.read_text()
    assert text.startswith("#!/sbin/openrc-run\n")
    assert "command=/opt/vigil/venv/bin/vigil" in text
    assert 'command_args="serve"' in text
    assert "supervisor=supervise-daemon" in text
    assert "need net" in text


def test_initd_environment() -> None:
    text = (DEPLOY / "vigil.initd").read_text()
    assert "XDG_CONFIG_HOME=/etc" in text
    assert "XDG_DATA_HOME=/var/lib" in text
    assert 'command_user="vigil:vigil"' in text
    # supervise-daemon opens the log after dropping to "vigil", so the file must be its own.
    assert "checkpath -f -m 0640 -o vigil:vigil /var/log/vigil.log" in text


def test_example_config_loads() -> None:
    config = load_config(DEPLOY / "config.example.toml")
    assert config.mode == "hub"
    assert config.hub_mode.public_url == "https://hub.example.com"
    assert [machine.name for machine in config.machines] == ["laptop"]
    assert config.server.bind == "127.0.0.1"
    assert config.server.allowed_hosts == ("hub.example.com",)
    access = config.server.access
    assert access is not None
    assert access.team_domain == "example-team.cloudflareaccess.com"
    assert [(token.scope, token.machine) for token in access.service_tokens] == [("push", "laptop")]
    assert [(token.file, token.scope) for token in config.server.tokens] == [
        (Path("/etc/vigil/read.token"), "read")
    ]


def test_dry_run_changes_nothing(tmp_path: Path) -> None:
    fakes = tmp_path / "fakebin"
    fakes.mkdir()
    log = tmp_path / "calls.log"
    log.write_text("")
    for name in FAKES:
        fake = fakes / name
        fake.write_text(f'#!/bin/sh\nprintf \'%s\\n\' "{name} $*" >> "{log}"\n')
        fake.chmod(0o755)
    root = tmp_path / "root"
    root.mkdir()

    done = subprocess.run(
        [SH, str(SCRIPT), "--dry-run"],
        env={"PATH": str(fakes), "ROOT": str(root)},
        capture_output=True,
        text=True,
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert log.read_text() == ""
    assert list(root.iterdir()) == []
    out = done.stdout
    assert "would run: addgroup -S vigil" in out
    assert "would run: adduser -S -D -H -G vigil vigil" in out
    assert "would run: apk add --no-cache uv" in out
    assert "uv python install 3.14" in out
    assert f"{root}/opt/vigil/venv" in out
    assert f"would run: install -m 0755 {DEPLOY}/vigil.initd {root}/etc/init.d/vigil" in out
    assert f"install -d -m 0750 -o root -g vigil {root}/etc/vigil" in out
    assert f"install -d -m 0700 -o vigil -g vigil {root}/var/lib/vigil" in out
    assert f"{DEPLOY}/config.example.toml {root}/etc/vigil/config.toml" in out
    assert "rc-update add vigil default" in out
    assert "rc-service vigil start" in out


def test_no_real_hostnames() -> None:
    """Every URL under deploy/alpine/ points at a placeholder host."""
    allowed = ("example.com", "example-tailnet.ts.net", "cloudflareaccess.com", "github.com")
    seen = 0
    for path in DEPLOY.iterdir():
        for match in re.finditer(r"https?://([^/\s:\"'`]+)", path.read_text()):
            seen += 1
            host = match.group(1).lower()
            assert any(host == name or host.endswith("." + name) for name in allowed), (
                f"{path.name}: {host}"
            )
    assert seen > 0
