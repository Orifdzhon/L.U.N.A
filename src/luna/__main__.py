"""Command line entry point: ``luna`` / ``python -m luna``."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from luna import __version__
from luna.config.loader import load_settings
from luna.logs.setup import setup_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="luna", description="L.U.N.A. voice assistant")
    parser.add_argument("--version", action="version", version=f"luna {__version__}")
    parser.add_argument("--text", action="store_true", help="text (CLI) mode")
    parser.add_argument("--config", type=Path, metavar="PATH", help="path to luna.toml")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
    except (OSError, ValueError, ValidationError) as exc:
        print(f"luna: config error: {exc}", file=sys.stderr)
        return 2
    setup_logging(settings.app.log_level)
    if args.text:
        print("luna: --text mode is not implemented yet", file=sys.stderr)
        return 2
    parser.print_usage(sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
