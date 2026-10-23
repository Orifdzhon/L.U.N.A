"""Command line entry point: ``luna`` / ``python -m luna``."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from luna import __version__
from luna.config.loader import load_settings
from luna.config.settings import Settings
from luna.core.errors import ConfigError
from luna.logs.setup import setup_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="luna", description="L.U.N.A. voice assistant")
    parser.add_argument("--version", action="version", version=f"luna {__version__}")
    parser.add_argument("--text", action="store_true", help="text (CLI) mode")
    parser.add_argument("--config", type=Path, metavar="PATH", help="path to luna.toml")
    return parser


def _run_text(settings: Settings) -> int:
    from luna.app import build_app
    from luna.ui.cli import configure_console_encoding, run_repl

    configure_console_encoding()
    app = build_app(settings)
    try:
        return run_repl(app)
    finally:
        app.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
    except (OSError, ConfigError) as exc:
        print(f"luna: config error: {exc}", file=sys.stderr)
        return 2
    setup_logging(settings.app.log_level, console=args.text, secrets=settings.secret_values())
    if args.text:
        return _run_text(settings)
    parser.print_usage(sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
