"""Command line entrypoint of the connector."""

import argparse
import sys
from collections.abc import Sequence

from agents_matrix_connector import __version__

EXIT_BAD_SETTINGS = 78

SUBCOMMANDS: dict[str, str] = {}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agents-matrix-connector")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")
    for name, help_text in SUBCOMMANDS.items():
        sub.add_parser(name, help=help_text)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
