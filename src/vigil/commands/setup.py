"""`vigil setup`: make vigil start by itself (XDG autostart, a systemd user unit or an OpenRC user
service), and `vigil setup --remove` to undo exactly that. Asks before it changes anything."""

import argparse
import os
import sys
from pathlib import Path

from vigil import setup
from vigil.config import Config
from vigil.paths import config_home
from vigil.setup import Method, SetupError, Step
from vigil.terminal import is_wsl

_NAMES: dict[Method, str] = {
    "autostart": "XDG autostart entry",
    "systemd": "systemd user unit",
    "openrc": "OpenRC user service",
}


def _say(text: str = "") -> None:
    print(text)


def _tilde(path: Path) -> str:
    home = Path.home()
    return f"~/{path.relative_to(home)}" if path.is_relative_to(home) else str(path)


def _ask(question: str) -> bool:
    """Ask a yes/no question (default no). No answer to give, as when input is not a terminal
    that is read from, counts as no."""
    try:
        answer = input(f"{question} [y/N] ")
    except EOFError:
        print()
        return False
    return answer.strip().lower() in ("y", "yes")


def _run_steps(steps: list[Step]) -> bool:
    """Run each step, saying what it does. A failed required step stops the run (False); a
    failed optional one is noted."""
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


def _path_hint(vigil_bin: Path) -> None:
    if setup.on_path(vigil_bin.parent, os.environ):
        return
    folder = _tilde(vigil_bin.parent)
    _say()
    _say(f"{folder} is not on your PATH, so a shell will not find `vigil`. setup does not edit")
    _say("shell files. To fix that, add this line to ~/.bashrc (or your shell's equivalent):")
    _say(f'  export PATH="{folder.replace("~", "$HOME", 1)}:$PATH"')


class SetupCommand:
    name = "setup"
    help = "Start vigil automatically (autostart, systemd or OpenRC user service), or undo it."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--method",
            choices=setup.METHODS,
            help="use this instead of the recommended method",
        )
        parser.add_argument("--yes", action="store_true", help="do not ask before going ahead")
        parser.add_argument(
            "--dry-run", action="store_true", help="show what would happen and change nothing"
        )
        parser.add_argument(
            "--no-start",
            action="store_true",
            help="install (and enable) but do not start vigil now",
        )
        parser.add_argument(
            "--remove",
            action="store_true",
            help="stop vigil and remove what setup installed, and only that",
        )

    def run(self, args: argparse.Namespace, config: Config) -> int:
        if setup.effective_uid() == 0:
            print(
                "vigil: setup will not run as root; run it as the user whose sessions vigil reads",
                file=sys.stderr,
            )
            return 2
        try:
            if args.remove:
                return self._remove(args, config)
            return self._install(args, config)
        except SetupError as error:
            print(f"vigil: {error}", file=sys.stderr)
            return 1

    def _install(self, args: argparse.Namespace, config: Config) -> int:
        vigil_bin = setup.vigil_executable()
        if vigil_bin is None:
            raise SetupError("cannot find the vigil executable; is vigil installed?")
        method: Method
        if args.method is not None:
            method, reason = args.method, "you chose it"
        else:
            picked = setup.detect_method(os.environ)
            if picked is None:
                raise SetupError(setup.NO_METHOD)
            method, reason = picked
        home_config = config_home()
        path = setup.entry_path(method, home_config)
        claude_dir = setup.claude_folder()
        wsl = method != "autostart" and is_wsl(os.environ)
        terminal_dir = setup.windows_terminal_folder() if wsl else None
        text = setup.render(
            method,
            vigil_bin,
            claude_dir,
            Path.home(),
            terminal_dir=terminal_dir,
            distro=os.environ.get("WSL_DISTRO_NAME") if wsl else None,
        )
        existing = path.read_text() if path.is_file() else None
        changed = existing is not None and existing != text

        _say(f"Method: {_NAMES[method]} ({reason}).")
        _say(f"  file:  {_tilde(path)}")
        _say(f"  vigil: {vigil_bin}")
        if method != "autostart":
            if claude_dir is not None:
                _say(f"  PATH:  includes {claude_dir}, where claude was found")
            else:
                _say("  PATH:  claude was not found now, so the service may not find it either")
        if terminal_dir is not None:
            _say(f"  PATH:  includes {terminal_dir}, where Windows Terminal (wt.exe) was found")
        elif wsl:
            _say("  PATH:  Windows Terminal (wt.exe) was not found, so Resume opens no terminal")
        if existing is None:
            _say("  new file")
        elif not changed:
            _say("  the file is already there and up to date")
        if changed:
            assert existing is not None
            _say()
            _say(f"{_tilde(path)} exists and differs:")
            _say(setup.diff(existing, text).rstrip())
        if args.dry_run:
            if existing != text:
                _say()
                _say(text.rstrip())
            _say()
            _say("Dry run: nothing was written and nothing was started.")
            return 0

        # A different file asks even with --yes, unless setup wrote it itself (it has the marker).
        mine = existing is not None and setup.is_ours(existing)
        if changed and not (args.yes and mine):
            if not _ask(f"Replace {_tilde(path)} with the new one?"):
                _say("Nothing was changed.")
                return 1
        elif not args.yes and not _ask("Go ahead?"):
            _say("Nothing was changed.")
            return 1

        if changed or existing is None:
            setup.write_entry(path, text, setup.entry_mode(method), home_config)
            _say(f"Wrote {_tilde(path)}.")
        setup.ensure_runlevel(method, home_config)

        health = setup.probe_health(config.port)
        url = f"http://127.0.0.1:{config.port}/"
        blocked = health.state == "other"
        if blocked:
            print(
                f"vigil: port {config.port} is in use by something that is not vigil, so vigil "
                "was not started; stop it or set `port` in the vigil config",
                file=sys.stderr,
            )
        steps = setup.start_steps(
            method,
            vigil_bin,
            replaced=changed and health.state == "vigil",
            running=health.state == "vigil",
            enable_only=args.no_start or blocked,
        )
        if not _run_steps(steps):
            return 1
        _path_hint(vigil_bin)
        if args.no_start:
            _say(f"Installed. vigil was not started; it will start at the next login ({url}).")
            return 0
        if blocked:
            return 1
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

    def _remove(self, args: argparse.Namespace, config: Config) -> int:
        home_config = config_home()
        methods: tuple[Method, ...] = (args.method,) if args.method else setup.METHODS
        ours: list[Method] = []
        for method in methods:
            path = setup.entry_path(method, home_config)
            if not path.is_file():
                continue
            if setup.is_ours(path.read_text()):
                ours.append(method)
            else:
                _say(f"Leaving {_tilde(path)}: `vigil setup` did not write it.")
        if not ours:
            _say("Nothing that `vigil setup` installed was found.")
            return 0
        running = setup.probe_health(config.port).state == "vigil"
        _say("This will stop vigil and remove:")
        for method in ours:
            _say(f"  {_NAMES[method]}: {_tilde(setup.entry_path(method, home_config))}")
        if args.dry_run:
            _say()
            _say("Dry run: nothing was stopped and nothing was removed.")
            return 0
        if not args.yes and not _ask("Go ahead?"):
            _say("Nothing was changed.")
            return 1
        for method in ours:
            path = setup.entry_path(method, home_config)
            _run_steps(setup.stop_steps(method, running=running))
            setup.check_target(path, home_config)
            path.unlink()
            _say(f"Removed {_tilde(path)}.")
            _run_steps(setup.after_remove_steps(method))
        return 0


COMMAND = SetupCommand()
