"""`vigil serve [--port N] [--host ADDRESS]`: run the web UI and JSON API, on 127.0.0.1 unless
remote access is configured (`server.bind` with `server.token_file` or `[[server.tokens]]`; see
`vigil.web.security`)."""

import argparse
import dataclasses
import sys

from vigil.config import Config


def _port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("must be from 1 to 65535")
    return port


class ServeCommand:
    name = "serve"
    help = "Run the web UI and JSON API (on 127.0.0.1 unless remote access is set up)."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--port", type=_port, metavar="N", help="port to listen on (default: config `port`)"
        )
        parser.add_argument(
            "--host",
            metavar="ADDRESS",
            help="IP address to listen on (default: config `server.bind`, 127.0.0.1); any "
            "other than 127.0.0.1 needs remote access (`server.token_file` or `[[server.tokens]]`)",
        )

    def run(self, args: argparse.Namespace, config: Config) -> int:
        # Imported here: the web stack takes a noticeable time to import, and every other command
        # would pay for it otherwise.
        import uvicorn

        from vigil.web.app import create_app
        from vigil.web.auth import AuthError
        from vigil.web.security import BindRefused, check_bind_address

        host = args.host if args.host is not None else config.server.bind
        try:
            check_bind_address(host, config.server)
            config = dataclasses.replace(
                config, server=dataclasses.replace(config.server, bind=host)
            )
            app = create_app(config)
        except (BindRefused, AuthError) as refused:
            print(f"vigil: {refused}", file=sys.stderr)
            return 2
        port = args.port if args.port is not None else config.port
        # proxy_headers=False: the guard decides who is local from the address the connection
        # really comes from, so uvicorn must not replace it with one from X-Forwarded-For.
        uvicorn.run(app, host=host, port=port, log_level="info", proxy_headers=False)
        return 0


COMMAND = ServeCommand()
