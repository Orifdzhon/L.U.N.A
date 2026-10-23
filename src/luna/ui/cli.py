"""CLI REPL for ``luna --text`` (ARCHITECTURE section 5: runs on the main thread).

Ctrl+C while a request is running cancels it; Ctrl+C at the prompt asks to press it again,
the second one in a row quits. Ctrl+C while a reply is being spoken only stops the speech.
``exit`` / ``quit`` / ``выход`` and end of input quit as well.
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
from luna.core.events import CLOSED, ProviderSwitched, Status, StatusChanged, Subscription
from luna.core.models import Reply, Request
from luna.core.prompts import fallback_text

PROMPT = "> "
BOM = chr(0xFEFF)  # PowerShell pipes / editors may prefix a line with a byte order mark
EXIT_WORDS = frozenset({"exit", "quit", "выход"})
LOCAL_PROVIDER = "local"
HELP_TEXT = (
    "Type a request, e.g. 'открой ютуб', 'open telegram', 'какой курс доллара?'.\n"
    "Commands: /help, /providers, /clear (forget the conversation), exit (or Ctrl+C twice).\n"
    "Voice (needs --voice): /mute (toggle), /stop (stop speaking), /voice (settings)."
)
# Apologies for a cancelled / timed-out turn are shown but not spoken.
_SILENT_KINDS = ("cancelled", "timeout")

InputFn = Callable[[str], str]


def configure_console_encoding() -> None:
    """Make stdin/stdout/stderr UTF-8 so Cyrillic works in PowerShell 5.1 and Windows Terminal."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            # A stream that cannot be changed keeps working with its own encoding.
            with contextlib.suppress(OSError, ValueError):
                reconfigure(encoding="utf-8", errors="replace")


class Repl:
    def __init__(
        self, app: App, input_fn: InputFn | None = None, out: TextIO | None = None
    ) -> None:
        self._app = app
        self._input = input_fn if input_fn is not None else builtins.input
        self._out = out if out is not None else sys.stdout
        self._speech_pending = False  # a reply was handed to the speaker, its IDLE not seen yet

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
                    self._drain(events)
                    if self._stop_speech():  # first Ctrl+C only silences the assistant
                        self._say("\n(speech stopped)")
                        interrupted = False
                        continue
                    if interrupted:
                        self._say("")
                        return 0
                    interrupted = True
                    self._say("\n(press Ctrl+C again or type 'exit' to quit)")
                    continue
                interrupted = False
                text = line.replace(BOM, "").strip()
                if not text:
                    continue
                if text.lower() in EXIT_WORDS:
                    self._stop_speech()
                    return 0
                self._stop_speech()
                try:
                    if text.startswith("/"):
                        self._command(text)
                    else:
                        self._request(text, events)
                except KeyboardInterrupt:  # Ctrl+C while printing / between steps: no traceback
                    self._stop_speech()
                    self._say("\n(interrupted)")

    def _command(self, text: str) -> None:
        name = text.split(maxsplit=1)[0].lower()
        if name == "/help":
            self._say(HELP_TEXT)
        elif name == "/providers":
            active = self._app.active_providers
            described = self._app.describe_providers()
            self._say("Active LLM providers: " + (" -> ".join(described) if active else "none"))
            for note in self._app.model_notes():
                self._say(note)
            for provider, env_name in self._app.missing_keys.items():
                self._say(f"Skipped: {provider} ({env_name})")
        elif name == "/clear":
            self._app.assistant.clear_history()
            self._say("Conversation cleared.")
        elif name == "/stop":
            if not self._stop_speech():
                self._say("Nothing is being spoken.")
        elif name == "/mute":
            self._toggle_mute()
        elif name == "/voice":
            self._show_voice()
        else:
            self._say(f"Unknown command {name}. Try /help.")

    def _request(self, text: str, events: Subscription) -> None:
        cancel = threading.Event()
        try:
            reply = self._app.orchestrator.handle(Request(text, "text"), cancel)
        except KeyboardInterrupt:
            cancel.set()  # the orchestrator already abandoned the turn; its reply is dropped
            self._stop_speech()
            self._say("\n(cancelled)")
            self._drain(events)
            return
        self._drain(events)
        self._say(reply.text)
        self._speak(reply)

    def _speak(self, reply: Reply) -> None:
        speaker = self._app.speaker
        if speaker is None or speaker.muted or _is_silent(reply):
            return
        self._speech_pending = True
        speaker.speak(reply)

    def _stop_speech(self) -> bool:
        """Stop the speaker; True if something was (probably) being spoken."""
        speaker = self._app.speaker
        was_speaking = self._speech_pending
        self._speech_pending = False
        if speaker is not None:
            speaker.stop()
        return was_speaking

    def _toggle_mute(self) -> None:
        speaker = self._app.speaker
        if speaker is None:
            self._say("Voice is off. Start with --voice to enable it.")
            return
        muted = not speaker.muted
        speaker.set_muted(muted)  # muting also cuts the current speech
        if muted:
            self._speech_pending = False
        self._say("Voice muted." if muted else "Voice unmuted.")

    def _show_voice(self) -> None:
        if self._app.speaker is None:
            self._say("Voice is off. Start with --voice to enable it.")
            return
        conf = self._app.settings.tts
        state = "muted" if self._app.speaker.muted else "on"
        self._say(
            f"Voice {state}: ru={conf.voice_ru}, en={conf.voice_en}, "
            f"rate={conf.rate}, volume={conf.volume}"
        )

    def _drain(self, events: Subscription) -> None:
        """Print status notes (provider switches) queued during the request."""
        while True:
            try:
                event = events.queue.get_nowait()
            except queue.Empty:
                return
            if event is CLOSED:
                return
            if isinstance(event, StatusChanged):
                if event.status is Status.SPEAKING:
                    self._speech_pending = True
                elif event.status is Status.IDLE:
                    self._speech_pending = False  # the speaker (or a turn) went idle
                continue
            if isinstance(event, ProviderSwitched):
                if event.to_provider == LOCAL_PROVIDER:
                    self._say("[offline mode: using the local model]")
                else:
                    self._say(f"[provider: {event.to_provider}]")


def _is_silent(reply: Reply) -> bool:
    return any(
        reply.text == fallback_text(kind, lang) for kind in _SILENT_KINDS for lang in ("ru", "en")
    )


def run_repl(app: App, input_fn: InputFn | None = None, out: TextIO | None = None) -> int:
    return Repl(app, input_fn, out).run()
