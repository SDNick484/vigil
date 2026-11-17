"""Command line entry point. Commands register themselves in COMMANDS."""

import argparse
import sys
from importlib import import_module
from typing import Protocol

import vigil
from vigil.config import Config, ConfigError, load_config


class Command(Protocol):
    name: str
    help: str

    def add_arguments(self, parser: argparse.ArgumentParser) -> None: ...

    def run(self, args: argparse.Namespace, config: Config) -> int: ...


# Append point: later tasks register a command by appending one line here.
COMMANDS: list[Command] = []
COMMANDS.append(import_module("vigil.commands.scan").COMMAND)
COMMANDS.append(import_module("vigil.commands.ls").COMMAND)
COMMANDS.append(import_module("vigil.commands.show").COMMAND)
COMMANDS.append(import_module("vigil.commands.resume").COMMAND)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vigil", description="Index, resume and report on Claude Code sessions."
    )
    parser.add_argument(
        "--version", action="version", version=f"vigil-sessions {vigil.__version__}"
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    for command in COMMANDS:
        sub = subparsers.add_parser(command.name, help=command.help, description=command.help)
        command.add_arguments(sub)
        sub.set_defaults(_command=command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command: Command | None = getattr(args, "_command", None)
    if command is None:
        parser.print_help(sys.stderr)
        return 2
    try:
        config = load_config()
    except ConfigError as error:
        print(f"vigil: {error}", file=sys.stderr)
        return 2
    return command.run(args, config)
