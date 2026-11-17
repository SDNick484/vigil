"""`vigil scan`: bring the index up to date with the transcripts."""

import argparse

from vigil.commands import scan_index
from vigil.config import Config


class ScanCommand:
    name = "scan"
    help = "Scan Claude Code transcripts into the index."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        pass

    def run(self, args: argparse.Namespace, config: Config) -> int:
        conn, stats = scan_index(config)
        conn.close()
        print(
            f"Scanned {stats.files_seen} transcripts: {stats.files_parsed} read, "
            f"{stats.sessions_updated} sessions updated, {stats.errors} errors."
        )
        return 0


COMMAND = ScanCommand()
