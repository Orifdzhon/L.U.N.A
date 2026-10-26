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
    voice = parser.add_mutually_exclusive_group()
    voice.add_argument("--voice", action="store_true", help="speak replies (overrides tts.enabled)")
    voice.add_argument(
        "--no-voice", action="store_true", help="do not speak replies (overrides tts.enabled)"
    )
    listen = parser.add_mutually_exclusive_group()
    listen.add_argument(
        "--listen", action="store_true", help="listen to the hotkey (overrides voice.listen)"
    )
    listen.add_argument(
        "--no-listen",
        action="store_true",
        help="do not use the microphone (overrides voice.listen)",
    )
    wake = parser.add_mutually_exclusive_group()
    wake.add_argument(
        "--wake",
        action="store_true",
        help='listen for the wake word "Luna" (implies --listen; overrides wakeword.enabled)',
    )
    wake.add_argument(
        "--no-wake", action="store_true", help="no wake word (overrides wakeword.enabled)"
    )
    parser.add_argument("--config", type=Path, metavar="PATH", help="path to luna.toml")
    return parser


def _run_text(settings: Settings) -> int:
    from luna.app import build_app
    from luna.ui.cli import configure_console_encoding, run_repl

    configure_console_encoding()
    app = build_app(settings, wake_progress=lambda line: print(line, flush=True))
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
    if args.voice:
        settings.tts.enabled = True
    elif args.no_voice:
        settings.tts.enabled = False
    if args.listen:
        settings.voice.listen = True
    elif args.no_listen:
        settings.voice.listen = False
        if not args.wake:
            settings.wakeword.enabled = False  # no microphone at all
    if args.wake:
        settings.wakeword.enabled = True
        settings.voice.listen = True
    elif args.no_wake:
        settings.wakeword.enabled = False
    setup_logging(settings.app.log_level, console=args.text, secrets=settings.secret_values())
    if args.text:
        return _run_text(settings)
    parser.print_usage(sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
