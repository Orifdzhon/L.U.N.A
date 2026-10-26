"""ShellController: the toolkit-free brain of the desktop shell (ADR 0009, ARCHITECTURE 4.4).

Every public method runs on the UI thread and never blocks: blocking work (``enable_wake``,
``disable_wake``, autostart toggles) goes to the ``luna-ui-worker`` queue thread, typed requests
to ``luna-ui-req-<n>`` threads, and bus events arrive from ``luna-ui-events`` through
``UiThread.post``. No Qt import: the views are protocols (``luna.ui.shell.views``).
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from collections.abc import Callable
from typing import Literal, Protocol

from luna.app import App
from luna.config.settings import UISettings
from luna.core.errors import ToolError
from luna.core.events import (
    CLOSED,
    ProviderSwitched,
    ReplyReady,
    Status,
    StatusChanged,
    Subscription,
    TranscriptReady,
)
from luna.core.models import Reply, Request
from luna.platform.base import Autostart, LaunchCommand
from luna.ui.replies import is_silent
from luna.ui.shell.state import (
    Conversation,
    ConversationLine,
    IconState,
    TrayMenuState,
    derive_icon_state,
    tooltip_for,
)
from luna.ui.shell.views import TrayView, UiThread, WindowView
from luna.wakeword.listener import WakeState, WakeStateChanged

logger = logging.getLogger(__name__)

ERROR_ICON_S = 5.0
NOTIFY_INTERVAL_S = 300.0
WATCHDOG_S = 10.0
LOCAL_PROVIDER = "local"
WAKE_LABEL = "Listen for «Луна»"
WAKE_STARTING_LABEL = "Starting wake word…"
COMMANDS_HINT = "Commands are in the tray menu"
NOTIFY_TITLE = "L.U.N.A."

LineKind = Literal["user", "assistant", "action", "system"]
Clock = Callable[[], float]
StartThread = Callable[[str, Callable[[], None]], None]
Job = Callable[[], None]


class Cancellable(Protocol):
    def cancel(self) -> None: ...


Schedule = Callable[[float, Callable[[], None]], Cancellable]


def start_daemon_thread(name: str, target: Callable[[], None]) -> None:
    threading.Thread(target=target, name=name, daemon=True).start()


def schedule_timer(delay: float, fn: Callable[[], None]) -> Cancellable:
    timer = threading.Timer(delay, fn)
    timer.name = "luna-ui-timer"
    timer.daemon = True
    timer.start()
    return timer


def arm_watchdog(seconds: float = WATCHDOG_S) -> None:
    """Daemon thread: if shutdown hangs, log and leave the process hard (ADR 0009 decision 12)."""

    def watch() -> None:
        time.sleep(seconds)
        logger.critical("Shutdown took more than %.0f s: forcing exit", seconds)
        os._exit(0)

    threading.Thread(target=watch, name="luna-ui-quit-watchdog", daemon=True).start()


class ShellController:
    def __init__(
        self,
        app: App,
        tray: TrayView,
        window: WindowView,
        ui: UiThread,
        *,
        autostart: Autostart,
        launch: Callable[[], LaunchCommand],
        conf: UISettings,
        clock: Clock = time.monotonic,
        start_thread: StartThread = start_daemon_thread,
        schedule: Schedule = schedule_timer,
        watchdog: Callable[[], None] = arm_watchdog,
        on_quit: Callable[[], None] | None = None,
    ) -> None:
        self._app = app
        self._tray = tray
        self._window = window
        self._ui = ui
        self._autostart = autostart
        self._launch = launch
        self._conf = conf
        self._clock = clock
        self._start_thread = start_thread
        self._schedule = schedule
        self._watchdog = watchdog
        self._on_quit = on_quit

        self._conversation = Conversation(conf.history_lines)
        self._status: Status | None = None
        self._status_detail: str | None = None
        self._wake_state = "OFF"
        self._provider: str | None = None
        self._error_token = 0
        self._error_timer: Cancellable | None = None
        self._icon = IconState.IDLE

        self._events: Subscription | None = None
        self._jobs: queue.Queue[Job | None] = queue.Queue()
        self._started = False
        self._quitting = False
        self._wake_busy = False
        self._autostart_busy = False
        self._autostart_checked = False
        self._request_seq = 0
        self._request_cancel: threading.Event | None = None
        self._last_notice: dict[str, float] = {}
        self._close_notified = False

    # ---- lifecycle -------------------------------------------------------------------

    def start(self, *, wake: bool, minimized: bool) -> None:
        if self._started or self._quitting:
            return
        self._started = True
        app = self._app
        voice = app.voice_input
        if voice is not None:
            voice.set_sinks(self._voice_reply, self._voice_stale_reply)
            self._wake_state = voice.wake_state
        self._autostart_checked = self._read_autostart()
        self._events = app.bus.subscribe()
        self._start_thread("luna-ui-events", lambda: self._pump(self._events))
        self._start_thread("luna-ui-worker", self._worker_loop)
        self._tray.show()
        for text in app.startup_lines():
            self._line("system", text)
        if not minimized:
            self._window.show()
        if self._degraded():
            self._notify("degraded", "L.U.N.A. started with limited features; see its window.")
        self._refresh_icon()
        self._refresh_status()
        if wake:
            self._queue_wake_on(from_start=True)
        else:
            self._refresh_menu()

    @property
    def icon_state(self) -> IconState:
        return self._icon

    @property
    def conversation(self) -> Conversation:
        return self._conversation

    # ---- bus events ------------------------------------------------------------------

    def _pump(self, sub: Subscription | None) -> None:
        """``luna-ui-events``: one subscription, every event is handed to the UI thread."""
        if sub is None:
            return
        while True:
            item = sub.queue.get()
            if item is CLOSED:
                return
            self._ui.post(lambda event=item: self.on_event(event))

    def on_event(self, event: object) -> None:
        if self._quitting:
            return
        if isinstance(event, StatusChanged):
            self._on_status(event)
        elif isinstance(event, WakeStateChanged):
            self._on_wake_state(event)
        elif isinstance(event, TranscriptReady):
            self._line("user", event.text)
        elif isinstance(event, ReplyReady):
            self._on_reply_ready(event)
        elif isinstance(event, ProviderSwitched):
            self._provider = event.to_provider
            self._refresh_status()

    def _on_status(self, event: StatusChanged) -> None:
        self._status = event.status
        self._status_detail = event.detail
        if event.status is Status.ERROR:
            self._error_token += 1
            token = self._error_token
            self._cancel_error_timer()
            self._error_timer = self._schedule(
                ERROR_ICON_S, lambda: self._ui.post(lambda: self._expire_error(token))
            )
        else:
            self._error_token += 1  # a newer status ends the transient error
            self._cancel_error_timer()
        self._refresh_icon()
        self._refresh_status()

    def _expire_error(self, token: int) -> None:
        if token != self._error_token or self._quitting:
            return
        self._error_timer = None
        if self._status is Status.ERROR:
            self._status = Status.IDLE
            self._status_detail = None
        self._error_token += 1
        self._refresh_icon()
        self._refresh_status()

    def _cancel_error_timer(self) -> None:
        timer, self._error_timer = self._error_timer, None
        if timer is not None:
            timer.cancel()

    def _on_wake_state(self, event: WakeStateChanged) -> None:
        self._wake_state = event.state.name
        if event.state is WakeState.FAILED:
            reason = event.detail or "unknown error"
            self._notify("wake-failed", f"Wake word stopped: {reason}")
            self._line("system", f"Wake word stopped: {reason}. Hotkey works; retry from the menu.")
        self._refresh_icon()
        self._refresh_status()
        self._refresh_menu()

    def _on_reply_ready(self, event: ReplyReady) -> None:
        reply = event.reply
        self._line("assistant", reply.text)
        for result in reply.actions:
            self._add(self._conversation.add_action(result))

    # ---- typed requests --------------------------------------------------------------

    def submit(self, text: str) -> None:
        text = text.replace(chr(0xFEFF), "").strip()
        if not text or self._quitting:
            return
        if self._request_cancel is not None:
            self._line("system", "Still working on the previous request (Esc cancels it).")
            return
        if text.startswith("/"):
            self._line("system", COMMANDS_HINT)
            return
        self._line("user", text)
        self._request_seq += 1
        number = self._request_seq
        cancel = threading.Event()
        self._request_cancel = cancel
        self._window.set_input_busy(True)
        self._start_thread(f"luna-ui-req-{number}", lambda: self._run_request(text, cancel))

    def _run_request(self, text: str, cancel: threading.Event) -> None:
        reply: Reply | None = None
        try:
            reply = self._app.orchestrator.handle(Request(text, "text"), cancel)
        except Exception:
            logger.exception("Typed request failed")
        self._ui.post(lambda: self._request_done(cancel, reply))

    def _request_done(self, cancel: threading.Event, reply: Reply | None) -> None:
        if self._request_cancel is cancel:
            self._request_cancel = None
        if self._quitting:
            return
        if reply is None:
            self._line("system", "The request failed. See the log.")
        else:
            self._speak(reply)  # display came from ReplyReady
        self._window.set_input_busy(False)

    def cancel(self) -> None:
        cancel = self._request_cancel
        if cancel is not None:
            cancel.set()

    # ---- speech ----------------------------------------------------------------------

    def _speak(self, reply: Reply) -> None:
        speaker = self._app.speaker
        if speaker is None or speaker.muted or is_silent(reply):
            return
        speaker.speak(reply)

    def _voice_reply(self, reply: Reply) -> None:
        """A finished voice turn (voice thread): spoken here, shown from ``ReplyReady``."""
        self._speak(reply)

    def _voice_stale_reply(self, reply: Reply) -> None:
        """The hotkey was pressed again meanwhile: shown (via the bus), never spoken."""
        return None

    # ---- window ----------------------------------------------------------------------

    def toggle_window(self) -> None:
        if self._quitting:
            return
        if self._window.visible:
            self._window.hide()
        else:
            self._window.show()

    def window_closed(self) -> None:
        self._window.hide()
        if self._quitting or self._close_notified:
            return
        self._close_notified = True
        self._notify_now("L.U.N.A. is still running in the tray. Use Quit in the tray menu.")

    # ---- tray toggles ----------------------------------------------------------------

    def set_wake(self, on: bool) -> None:
        if self._quitting:
            return
        if on:
            self._queue_wake_on(from_start=False)
        else:
            self._queue_wake_off()

    def _queue_wake_on(self, *, from_start: bool) -> None:
        voice = self._app.voice_input
        if voice is None:
            self._line("system", "Voice input is not available.")
            self._refresh_menu()
            return
        if self._wake_busy:
            return
        self._wake_busy = True
        self._refresh_menu()

        def progress(text: str) -> None:
            logger.info("%s", text)
            self._ui.post(lambda: self._window.set_status(text))

        def job() -> None:
            try:
                reason = voice.enable_wake(progress)
            except Exception as exc:
                logger.warning("enable_wake failed", exc_info=True)
                reason = f"unexpected error ({type(exc).__name__})"
            self._ui.post(lambda: self._wake_on_done(reason, from_start))

        self._jobs.put(job)

    def _queue_wake_off(self) -> None:
        voice = self._app.voice_input
        if voice is None or self._wake_busy:
            return
        self._wake_busy = True
        self._refresh_menu()

        def job() -> None:
            try:
                voice.disable_wake()
            except Exception:
                logger.warning("disable_wake failed", exc_info=True)
            self._ui.post(self._wake_off_done)

        self._jobs.put(job)

    def _wake_on_done(self, reason: str | None, from_start: bool) -> None:
        self._wake_busy = False
        if self._quitting:
            return
        voice = self._app.voice_input
        if voice is not None:
            self._wake_state = voice.wake_state
        if reason is not None:
            text = f"Wake word disabled: {reason}. Continuing with the hotkey."
            self._line("system", text)
            if from_start:
                self._notify("wake-start", text)
        elif voice is not None:
            self._line("system", voice.wake_status_line())
        self._refresh_icon()
        self._refresh_status()
        self._refresh_menu()

    def _wake_off_done(self) -> None:
        self._wake_busy = False
        if self._quitting:
            return
        voice = self._app.voice_input
        self._wake_state = voice.wake_state if voice is not None else "OFF"
        self._refresh_icon()
        self._refresh_status()
        self._refresh_menu()

    def set_voice(self, on: bool) -> None:
        speaker = self._app.speaker
        if speaker is not None and not self._quitting:
            speaker.set_muted(not on)  # muting also cuts the current speech
        self._refresh_status()
        self._refresh_menu()

    def set_autostart(self, on: bool) -> None:
        if self._quitting or self._autostart_busy:
            return
        self._autostart_busy = True
        self._refresh_menu()

        def job() -> None:
            error: str | None = None
            try:
                if on:
                    self._autostart.enable(self._launch())
                else:
                    self._autostart.disable()
            except Exception as exc:  # ToolError or an unexpected failure: never kill the worker
                if not isinstance(exc, ToolError):
                    logger.warning("autostart toggle failed", exc_info=True)
                error = str(exc) or type(exc).__name__
            self._ui.post(lambda: self._autostart_done(error))

        self._jobs.put(job)

    def _autostart_done(self, error: str | None) -> None:
        self._autostart_busy = False
        if self._quitting:
            return
        self._autostart_checked = self._read_autostart()  # the file system is the truth
        if error is not None:
            text = f"Start with Windows failed: {error}"
            self._line("system", text)
            self._notify("autostart", text)
        self._refresh_menu()

    def _read_autostart(self) -> bool:
        try:
            return bool(self._autostart.is_enabled())
        except Exception:
            logger.warning("autostart.is_enabled failed", exc_info=True)
            return False

    # ---- worker ----------------------------------------------------------------------

    def _worker_loop(self) -> None:
        """``luna-ui-worker``: blocking shell actions, one at a time (``None`` ends it)."""
        while True:
            job = self._jobs.get()
            if job is None:
                return
            try:
                job()
            except Exception:
                logger.exception("Shell worker job failed")

    # ---- shutdown --------------------------------------------------------------------

    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self._watchdog()
        self._tray.hide()
        self._window.hide()
        self._window.set_input_busy(True)
        self._cancel_error_timer()
        cancel = self._request_cancel
        if cancel is not None:
            cancel.set()
        events = self._events
        if events is not None:
            events.close()
        self._jobs.put(None)  # the worker ends after its running job (an enable_wake may linger)
        voice = self._app.voice_input
        if voice is not None:
            voice.set_sinks(_ignore, _ignore)
        try:
            self._app.close()
        except Exception:
            logger.exception("App.close failed")
        finally:
            if self._on_quit is not None:
                try:
                    self._on_quit()
                except Exception:
                    logger.exception("Quit hook failed")

    # ---- views -----------------------------------------------------------------------

    def _line(self, kind: LineKind, text: str) -> None:
        self._add(self._conversation.add(ConversationLine(kind, text)))

    def _add(self, line: ConversationLine) -> None:
        if not self._quitting:
            self._window.append(line)

    def _refresh_icon(self) -> None:
        state = derive_icon_state(self._status, self._wake_state, self._error_timer is not None)
        self._icon = state
        self._tray.set_state(state, tooltip_for(state))

    def _refresh_status(self) -> None:
        parts = [self._status.value if self._status is not None else Status.IDLE.value]
        if self._status is Status.ERROR and self._status_detail:
            parts[0] = f"error: {self._status_detail}"
        if self._wake_state != "OFF":
            parts.append(f"Wake: {self._wake_state}")
        speaker = self._app.speaker
        if speaker is not None and speaker.muted:
            parts.append("Voice: muted")
        if self._provider is not None:
            parts.append(
                "LLM: offline" if self._provider == LOCAL_PROVIDER else f"LLM: {self._provider}"
            )
        self._window.set_status(" | ".join(parts))

    def _refresh_menu(self) -> None:
        voice = self._app.voice_input
        speaker = self._app.speaker
        wake_on = self._wake_state in ("ARMED", "HELD")
        self._tray.set_menu(
            TrayMenuState(
                wake_checked=wake_on and not self._wake_busy,
                wake_enabled=voice is not None and not self._wake_busy,
                wake_label=WAKE_STARTING_LABEL if self._wake_busy else WAKE_LABEL,
                voice_checked=speaker is not None and not speaker.muted,
                voice_enabled=speaker is not None,
                autostart_checked=self._autostart_checked,
                autostart_enabled=not self._autostart_busy,
            )
        )

    # ---- notifications ---------------------------------------------------------------

    def _degraded(self) -> bool:
        if not self._app.llm_available:
            return True
        prefixes = ("Voice disabled", "Voice input disabled", "Wake word disabled")
        return any(w.startswith(prefixes) for w in self._app.warnings)

    def _notify(self, reason: str, text: str) -> None:
        """Error notification: one per ``reason`` per 5 minutes, only if enabled."""
        if not self._conf.notifications or self._quitting:
            return
        now = self._clock()
        last = self._last_notice.get(reason)
        if last is not None and now - last < NOTIFY_INTERVAL_S:
            return
        self._last_notice[reason] = now
        self._tray.notify(NOTIFY_TITLE, text)

    def _notify_now(self, text: str) -> None:
        if self._conf.notifications:
            self._tray.notify(NOTIFY_TITLE, text)


def _ignore(reply: Reply) -> None:
    return None
