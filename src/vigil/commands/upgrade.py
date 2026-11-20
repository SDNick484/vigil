"""`vigil upgrade [--ref REF] [--dry-run] [--no-restart]`: upgrade vigil the way it was installed,
then restart it the way `vigil setup` installed it (if it did)."""

import argparse
import os
import subprocess
import sys
import time
from itertools import chain
from pathlib import Path

from vigil import __version__, setup, upgrade
from vigil.config import Config
from vigil.paths import config_home
from vigil.setup import Method, SetupError, Step
from vigil.upgrade import Install, UpgradeRefused

_KINDS = {
    "script": "installed by install.sh",
    "pipx": "installed with pipx",
    "venv": "installed in a virtual environment",
    "system": "installed by a package manager",
}
_STOP_WAIT = 5.0
"""Seconds to wait for a stopped vigil to let go of its port before starting it again."""


def _say(text: str = "") -> None:
    print(text)


def _new_version() -> str | None:
    """What the upgraded vigil calls itself: a new process, because this one still has the old
    code loaded. None when it does not run."""
    try:
        result = subprocess.run(  # noqa: S603 - argv is a fixed, vigil-built list
            [sys.executable, "-m", "vigil", "--version"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=60,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def service_method() -> Method | None:
    """The method whose file `vigil setup` wrote (it has setup's marker line), or None when none
    is installed. With more than one, the one `vigil setup` would recommend here, else the
    first."""
    folder = config_home()
    ours: list[Method] = []
    for method in setup.METHODS:
        try:
            text = setup.entry_path(method, folder).read_text()
        except OSError, ValueError:
            continue
        if setup.is_ours(text):
            ours.append(method)
    if len(ours) < 2:
        return ours[0] if ours else None
    picked = setup.detect_method(os.environ)
    return picked[0] if picked and picked[0] in ours else ours[0]


def restart_steps(
    method: Method, vigil_bin: Path, *, running: bool
) -> tuple[list[Step], list[Step]]:
    """The steps that restart vigil under `method`, as (stop, start). The service managers
    restart (or start) the service in one go, so their stop part is empty; autostart has nothing
    to restart with, so a running vigil is stopped, then started."""
    if method == "autostart":
        return (
            setup.stop_steps(method, running=running),
            setup.start_steps(method, vigil_bin, replaced=True, running=False, enable_only=False),
        )
    steps = setup.start_steps(method, vigil_bin, replaced=True, running=running, enable_only=False)
    return [], steps


def _run_steps(steps: list[Step]) -> bool:
    for step in steps:
        _say(f"  {step.what}: {' '.join(step.argv)}")
        failure = setup.run_step(step)
        if failure is None:
            continue
        if step.required:
            print(f"vigil: could not {step.what}: {failure}", file=sys.stderr)
            return False
        _say(f"    (note: {failure})")
    return True


def _wait_until_stopped(port: int) -> None:
    deadline = time.monotonic() + _STOP_WAIT
    while setup.probe_health(port).state == "vigil" and time.monotonic() < deadline:
        time.sleep(0.25)


class UpgradeCommand:
    name = "upgrade"
    help = "Upgrade vigil the way it was installed, then restart it."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--ref",
            metavar="REF",
            help="install this git branch, tag or commit (default: the ref of the last "
            "install.sh install, else main)",
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="show what would happen and change nothing"
        )
        parser.add_argument(
            "--no-restart", action="store_true", help="upgrade, but do not restart vigil"
        )

    def run(self, args: argparse.Namespace, config: Config) -> int:
        install = upgrade.detect_install()
        try:
            ref = upgrade.resolve_ref(install, args.ref)
            plan = upgrade.plan_upgrade(install, args.ref)
        except UpgradeRefused as error:
            print(f"vigil: {error}", file=sys.stderr)
            return 1
        _say(f"vigil is {_KINDS[install.kind]} ({install.prefix}); upgrading to {ref}.")
        _say(f"Version before: vigil-sessions {__version__}")
        method = None if args.no_restart else service_method()
        if args.dry_run:
            return self._dry_run(plan, install, ref, method, config, args.no_restart)

        for argv in plan:
            _say(f"+ {' '.join(argv)}")
            try:
                code = subprocess.run(argv, stdin=subprocess.DEVNULL, check=False).returncode  # noqa: S603
            except OSError as error:
                print(f"vigil: could not run {argv[0]}: {error}", file=sys.stderr)
                return 1
            if code != 0:
                print(
                    f"vigil: {argv[0]} exited with status {code}; vigil was not changed "
                    "or restarted",
                    file=sys.stderr,
                )
                return 1
        # The marker is vigil's own file in its own environment, not under the Claude home.
        if upgrade.record_install(install, ref):
            _say(f"Recorded {ref} in {install.prefix / upgrade.MARKER_NAME}.")
        new = _new_version()
        if new is None:
            print(
                "vigil: the upgrade ran but the new vigil does not start "
                f"(`{sys.executable} -m vigil --version` failed); not restarting. "
                "Run `vigil doctor` or install again.",
                file=sys.stderr,
            )
            return 1
        _say(f"Version after:  {new}")
        if args.no_restart:
            _say(
                "Not restarting (--no-restart); a running vigil keeps the old version until it is."
            )
            return 0
        return self._restart(method, config)

    def _dry_run(
        self,
        plan: list[list[str]],
        install: Install,
        ref: str,
        method: Method | None,
        config: Config,
        no_restart: bool,
    ) -> int:
        for argv in plan:
            _say(f"would run: {' '.join(argv)}")
        if install.kind == "script":
            _say(f"would record {ref} in {install.prefix / upgrade.MARKER_NAME}")
        if no_restart:
            _say("would not restart (--no-restart)")
        elif method is None:
            _say("would not restart: vigil is not running as a service (see `vigil setup`)")
        else:
            running = setup.probe_health(config.port).state == "vigil"
            vigil_bin = setup.vigil_executable() or Path("vigil")
            for step in chain(*restart_steps(method, vigil_bin, running=running)):
                _say(f"would run: {' '.join(step.argv)}")
        _say()
        _say("Dry run: nothing was run and nothing was changed.")
        return 0

    def _restart(self, method: Method | None, config: Config) -> int:
        if method is None:
            _say(
                "vigil is not running as a service (`vigil setup` installed nothing), so there "
                "is nothing to restart. Start it again yourself if it is running."
            )
            return 0
        vigil_bin = setup.vigil_executable()
        if vigil_bin is None and method == "autostart":
            print("vigil: cannot find the vigil executable to start it again", file=sys.stderr)
            return 1
        running = setup.probe_health(config.port).state == "vigil"
        _say(f"Restarting vigil ({method}).")
        try:
            stop, start = restart_steps(method, vigil_bin or Path("vigil"), running=running)
        except SetupError as error:
            print(f"vigil: {error}", file=sys.stderr)
            return 1
        if stop:
            if not _run_steps(stop):
                return 1
            _wait_until_stopped(config.port)
        if not _run_steps(start):
            return 1
        url = f"http://127.0.0.1:{config.port}/"
        answer = setup.wait_for_health(config.port)
        if answer is None:
            print(
                f"vigil: it did not answer on {url} within {setup.HEALTH_WAIT:g} s; "
                "run `vigil doctor` to see why",
                file=sys.stderr,
            )
            return 1
        _say(f"vigil {answer.version} is running: {url}")
        return 0


COMMAND = UpgradeCommand()
