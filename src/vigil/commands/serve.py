"""`vigil serve [--port N]`: run the web UI and JSON API on 127.0.0.1."""

import argparse
import sys

from vigil.config import Config


def _port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("must be from 1 to 65535")
    return port


class ServeCommand:
    name = "serve"
    help = "Run the web UI and JSON API on 127.0.0.1."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--port", type=_port, metavar="N", help="port to listen on (default: config `port`)"
        )
        parser.add_argument(
            "--host", metavar="ADDRESS", help="address to listen on; only 127.0.0.1 is accepted"
        )

    def run(self, args: argparse.Namespace, config: Config) -> int:
        # Imported here: the web stack takes a noticeable time to import, and every other command
        # would pay for it otherwise.
        import uvicorn

        from vigil.web.app import create_app
        from vigil.web.security import BIND_ADDRESS, BindRefused, check_bind_address

        host = args.host if args.host is not None else BIND_ADDRESS
        try:
            check_bind_address(host)
        except BindRefused as refused:
            print(f"vigil: {refused}", file=sys.stderr)
            return 2
        port = args.port if args.port is not None else config.port
        uvicorn.run(create_app(config), host=host, port=port, log_level="info")
        return 0


COMMAND = ServeCommand()
