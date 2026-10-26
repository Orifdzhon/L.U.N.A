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

from luna.app import WAKE_MIC_NOTICE, App
from luna.core.errors import AudioError
from luna.core.events import (
    CLOSED,
    ProviderSwitched,
    Status,
    StatusChanged,
    Subscription,
    TranscriptReady,
)
from luna.core.models import Reply, Request
from luna.ui.replies import is_silent
from luna.voice.frontend import (
    DETAIL_AUDIO_FAILED,
    DETAIL_NO_SPEECH,
    DETAIL_NOT_CAUGHT,
    DETAIL_STT_FAILED,
)
from luna.wakeword.listener import WakeState

PROMPT = "> "
BOM = chr(0xFEFF)  # PowerShell pipes / editors may prefix a line with a byte order mark
EXIT_WORDS = frozenset({"exit", "quit", "выход"})
LOCAL_PROVIDER = "local"
HELP_TEXT = (
    "Type a request, e.g. 'открой ютуб', 'open telegram', 'какой курс доллара?'.\n"
    "Commands: /help, /providers, /clear (forget the conversation), exit (or Ctrl+C twice).\n"
    "Voice (needs --voice): /mute (toggle), /stop (stop speaking), /voice (settings).\n"
    "Listening: /listen [on|off] (hotkey push-to-talk), /mic (3 s microphone level check).\n"
    "Wake word: /wake [on|off] (say «Луна, ...»; /wake shows the state)."
)
# Status details of a voice turn worth telling the user about.
_VOICE_NOTES = {
    DETAIL_NO_SPEECH: "(no speech heard)",
    DETAIL_NOT_CAUGHT: "(did not catch that)",
    DETAIL_STT_FAILED: "(speech recognition failed)",
    DETAIL_AUDIO_FAILED: "(microphone error)",
}
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
        self._print_lock = threading.Lock()  # the voice thread prints too
        self._events: Subscription | None = None
        self._at_prompt = False  # main thread waits in input(): redraw the prompt after output

    def _say(self, text: str) -> None:
        with self._print_lock:
            print(text, file=self._out, flush=True)

    def _say_async(self, text: str) -> None:
        """Print from a background thread: start on a fresh line and show the prompt again."""
        with self._print_lock:
            print(f"\n{text}", file=self._out, flush=True)
            if self._at_prompt:
                print(PROMPT, end="", file=self._out, flush=True)

    def run(self) -> int:
        self._say("L.U.N.A. text mode. Type /help for help, 'exit' to quit.")
        for line in self._app.startup_lines():
            self._say(line)
        voice = self._app.voice_input
        stop_status = threading.Event()
        status_thread: threading.Thread | None = None
        if voice is not None:
            voice.set_sinks(self._voice_reply, self._voice_stale_reply)
            status_thread = threading.Thread(
                target=self._watch_voice, args=(stop_status,), name="luna-ui-status", daemon=True
            )
            status_thread.start()
        try:
            return self._loop()
        finally:
            stop_status.set()
            if voice is not None:
                voice.set_sinks(_ignore, _ignore)
            if status_thread is not None:
                status_thread.join(1.0)

    def _loop(self) -> int:
        with self._app.bus.subscribe() as events:
            self._events = events
            interrupted = False
            while True:
                try:
                    self._at_prompt = True
                    try:
                        line = self._input(PROMPT)
                    finally:
                        self._at_prompt = False
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
        elif name == "/listen":
            self._listen(text.split()[1:])
        elif name == "/wake":
            self._wake(text.split()[1:])
        elif name == "/mic":
            self._mic_check()
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

    # ---- voice input -----------------------------------------------------------------

    def _voice_reply(self, reply: Reply) -> None:
        """A finished voice turn (voice thread): print it like a typed reply, then speak it."""
        self._print_voice_reply(reply)
        self._speak(reply)

    def _voice_stale_reply(self, reply: Reply) -> None:
        """The user pressed the hotkey again but the work finished: show it, never speak."""
        self._print_voice_reply(reply)

    def _print_voice_reply(self, reply: Reply) -> None:
        events = self._events
        with self._print_lock:
            if events is not None:
                self._drain_locked(events)
            print(f"\n{reply.text}", file=self._out, flush=True)
            if self._at_prompt:
                print(PROMPT, end="", file=self._out, flush=True)

    def _watch_voice(self, stop: threading.Event) -> None:
        """Print short status lines of voice turns (own subscription, own thread)."""
        with self._app.bus.subscribe() as events:
            while not stop.is_set():
                try:
                    event = events.queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if event is CLOSED:
                    return
                if getattr(event, "state", None) is WakeState.FAILED:
                    self._say_async(f"wake failed: {event.detail}. Hotkey works; /wake on retries.")
                elif isinstance(event, TranscriptReady):
                    self._say_async(f"(heard) {event.text}")
                elif isinstance(event, StatusChanged):
                    if event.status is Status.LISTENING:
                        self._say_async("(listening...)")
                    elif event.detail in _VOICE_NOTES:
                        self._say_async(_VOICE_NOTES[event.detail])

    def _listen(self, args: list[str]) -> None:
        voice = self._app.voice_input
        if voice is None:
            self._say("Voice input is not available.")
            return
        want = {"on": True, "off": False}.get(args[0].lower()) if args else not voice.enabled
        if want is None:
            self._say("Usage: /listen [on|off]")
            return
        if want:
            reason = voice.enable()
            if reason is not None:
                self._say(f"Voice input disabled: {reason}. Continuing in text mode.")
                return
        else:
            voice.disable()
        self._say(voice.status_line())
        if want:
            for note in voice.notes:
                self._say(note)

    def _wake(self, args: list[str]) -> None:
        voice = self._app.voice_input
        if voice is None:
            self._say("Voice input is not available.")
            return
        word = args[0].lower() if args else ""
        if len(args) > 1 or word not in ("", "on", "off"):
            self._say("Usage: /wake [on|off]")
            return
        if word == "on":
            reason = voice.enable_wake(self._say)
            if reason is not None:
                self._say(f"Wake word disabled: {reason}. Continuing with the hotkey.")
                return
            self._say("🌙 listening for «Луна»")
            self._say(voice.wake_status_line())
            self._say(WAKE_MIC_NOTICE)
        elif word == "off":
            voice.disable_wake()
            self._say("Wake word off: the microphone is closed (hotkey push-to-talk stays).")
        else:
            self._say(voice.wake_status_line())

    def _mic_check(self) -> None:
        voice = self._app.voice_input
        if voice is None:
            self._say("Voice input is not available.")
            return
        self._say("Measuring the microphone for 3 s: stay quiet a moment, then speak normally...")
        try:
            levels = voice.measure()
        except AudioError as exc:
            self._say(f"Microphone check failed: {exc}")
            return
        self._say(f"Noise floor: {levels.noise_floor:.4f}, peak: {levels.peak:.4f} (RMS, 0..1)")
        if levels.suggested_threshold is None:
            self._say("No speech heard: speak louder or closer to the microphone and retry.")
            return
        current = self._app.settings.voice.vad_energy_threshold
        self._say(
            f"Suggested [voice] vad_energy_threshold = {levels.suggested_threshold} "
            f"(now {current})."
        )

    def _speak(self, reply: Reply) -> None:
        speaker = self._app.speaker
        if speaker is None or speaker.muted or is_silent(reply):
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
        if conf.provider == "edge":
            voices = (
                f"edge ru={conf.edge_voice_ru}, en={conf.edge_voice_en} "
                f"(fallback: sapi {conf.voice_ru}/{conf.voice_en})"
            )
        else:
            voices = f"sapi ru={conf.voice_ru}, en={conf.voice_en}"
        self._say(f"Voice {state}: {voices}, rate={conf.rate}, volume={conf.volume}")

    def _drain(self, events: Subscription) -> None:
        """Print status notes (provider switches) queued during the request."""
        with self._print_lock:
            self._drain_locked(events)

    def _drain_locked(self, events: Subscription) -> None:
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
                    note = "[offline mode: using the local model]"
                else:
                    note = f"[provider: {event.to_provider}]"
                print(note, file=self._out, flush=True)


def _ignore(reply: Reply) -> None:
    return None


def run_repl(app: App, input_fn: InputFn | None = None, out: TextIO | None = None) -> int:
    return Repl(app, input_fn, out).run()
