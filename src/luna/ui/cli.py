"""CLI REPL for ``luna --text`` (ARCHITECTURE section 5: runs on the main thread).

Ctrl+C while a request is running cancels it; Ctrl+C at the prompt asks to press it again,
the second one in a row quits. ``exit`` / ``quit`` / ``выход`` and end of input quit as well.
"""

from __future__ import annotations

import builtins
import contextlib
import queue
import sys
import threading
from collections.abc import Callable
from typing import TextIO

from luna.app import App
from luna.core.events import CLOSED, ProviderSwitched, Subscription
from luna.core.models import Request

PROMPT = "> "
EXIT_WORDS = frozenset({"exit", "quit", "выход"})
LOCAL_PROVIDER = "local"
HELP_TEXT = (
    "Type a request, e.g. 'открой ютуб', 'open telegram', 'какой курс доллара?'.\n"
    "Commands: /help, /providers, /clear (forget the conversation), exit (or Ctrl+C twice)."
)

InputFn = Callable[[str], str]


def configure_console_encoding() -> None:
    """Make stdin/stdout/stderr UTF-8 so Cyrillic works in PowerShell 5.1 and Windows Terminal."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            # A stream that cannot be changed keeps working with its own encoding.
            with contextlib.suppress(OSError, ValueError):
                reconfigure(encoding="utf-8", errors="replace")


class CliConfirmer:
    """``Confirmer`` that asks y/n on the terminal. Anything but y/yes/д/да is a refusal."""

    def __init__(self, input_fn: InputFn | None = None) -> None:
        self._input = input_fn if input_fn is not None else builtins.input

    def confirm(self, prompt: str) -> bool:
        try:
            answer = self._input(f"{prompt} [y/N] ")
        except (EOFError, KeyboardInterrupt):
            return False
        return answer.strip().lower() in {"y", "yes", "д", "да"}


class Repl:
    def __init__(
        self, app: App, input_fn: InputFn | None = None, out: TextIO | None = None
    ) -> None:
        self._app = app
        self._input = input_fn if input_fn is not None else builtins.input
        self._out = out if out is not None else sys.stdout

    def _say(self, text: str) -> None:
        print(text, file=self._out, flush=True)

    def run(self) -> int:
        self._say("L.U.N.A. text mode. Type /help for help, 'exit' to quit.")
        for line in self._app.startup_lines():
            self._say(line)
        with self._app.bus.subscribe() as events:
            interrupted = False
            while True:
                try:
                    line = self._input(PROMPT)
                except EOFError:
                    self._say("")
                    return 0
                except KeyboardInterrupt:
                    if interrupted:
                        self._say("")
                        return 0
                    interrupted = True
                    self._say("\n(press Ctrl+C again or type 'exit' to quit)")
                    continue
                interrupted = False
                text = line.strip()
                if not text:
                    continue
                if text.lower() in EXIT_WORDS:
                    return 0
                if text.startswith("/"):
                    self._command(text)
                    continue
                self._request(text, events)

    def _command(self, text: str) -> None:
        name = text.split(maxsplit=1)[0].lower()
        if name == "/help":
            self._say(HELP_TEXT)
        elif name == "/providers":
            active = self._app.active_providers
            self._say("Active LLM providers: " + (" -> ".join(active) if active else "none"))
            for provider, env_name in self._app.missing_keys.items():
                self._say(f"Skipped: {provider} ({env_name})")
        elif name == "/clear":
            self._app.assistant.clear_history()
            self._say("Conversation cleared.")
        else:
            self._say(f"Unknown command {name}. Try /help.")

    def _request(self, text: str, events: Subscription) -> None:
        cancel = threading.Event()
        try:
            reply = self._app.orchestrator.handle(Request(text, "text"), cancel)
        except KeyboardInterrupt:
            cancel.set()  # the orchestrator already abandoned the turn; its reply is dropped
            self._say("\n(cancelled)")
            self._drain(events)
            return
        self._drain(events)
        self._say(reply.text)

    def _drain(self, events: Subscription) -> None:
        """Print status notes (provider switches) queued during the request."""
        while True:
            try:
                event = events.queue.get_nowait()
            except queue.Empty:
                return
            if event is CLOSED:
                return
            if isinstance(event, ProviderSwitched):
                if event.to_provider == LOCAL_PROVIDER:
                    self._say("[offline mode: using the local model]")
                else:
                    self._say(f"[provider: {event.to_provider}]")


def run_repl(app: App, input_fn: InputFn | None = None, out: TextIO | None = None) -> int:
    return Repl(app, input_fn, out).run()
