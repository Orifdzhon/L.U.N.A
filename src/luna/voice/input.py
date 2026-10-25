"""Voice input assembly: STT chain, microphone, recorder, hotkey and VoiceFrontEnd.

The pieces are built lazily on the first ``enable()`` (the microphone is never touched while
listening is off) and every problem becomes a short reason string, never an exception:
the app then carries on in text mode (ARCHITECTURE section 8). The UI installs its reply
callbacks with ``set_sinks`` after the app is built.
"""

from __future__ import annotations

import importlib.util
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from luna.audio.capture import MicSource, SoundDeviceMic, check_audio_input
from luna.audio.recorder import Recorder
from luna.audio.vad import EnergyVAD, rms_level
from luna.config.settings import Settings
from luna.core.clock import Clock
from luna.core.errors import AudioError, ToolError
from luna.core.events import EventBus
from luna.core.models import Reply
from luna.platform.base import HotkeyListener
from luna.stt.base import STTProvider
from luna.stt.chain import STTChain
from luna.stt.faster_whisper import FasterWhisperSTT
from luna.stt.groq import GroqWhisperSTT
from luna.voice.frontend import VoiceFrontEnd, VoiceState

logger = logging.getLogger(__name__)

MEASURE_S = 3.0
_CLOSE_TIMEOUT_S = 2.0
_WARMUP_JOIN_S = 0.2
BEEP_HZ = 880
BEEP_MS = 80

ReplySink = Callable[[Reply], None]


class _Degraded(Exception):
    """A piece of voice input cannot be built; the message is the reason shown to the user."""


@dataclass(frozen=True)
class MicLevels:
    """Result of the ``/mic`` diagnostic. Levels are RMS as a fraction of full scale."""

    noise_floor: float
    peak: float
    frames: int
    suggested_threshold: float | None  # None: no speech heard


def suggest_threshold(noise_floor: float, peak: float) -> float | None:
    """A ``vad_energy_threshold`` between the noise and the speech peak (None: no speech)."""
    if peak < max(noise_floor * 4, 0.002):
        return None
    return round(max(noise_floor * 4, peak * 0.2, 0.001), 4)


def beep() -> None:
    """Short start cue; silent where ``winsound`` does not exist."""
    try:
        import winsound

        winsound.Beep(BEEP_HZ, BEEP_MS)
    except (ImportError, RuntimeError):
        pass


@dataclass
class _Parts:
    frontend: VoiceFrontEnd
    mic: MicSource
    stt: STTProvider
    local: FasterWhisperSTT | None
    closers: list[Any]


class VoiceInput:
    def __init__(
        self,
        settings: Settings,
        bus: EventBus,
        orchestrator: Any,
        speaker: Any,
        *,
        platform: str,
        clock: Clock,
        stt: STTProvider | None = None,
        mic: MicSource | None = None,
        hotkey: HotkeyListener | None = None,
    ) -> None:
        self._settings = settings
        self._bus = bus
        self._orchestrator = orchestrator
        self._speaker = speaker
        self._platform = platform
        self._clock = clock
        self._stt_override = stt
        self._mic_override = mic
        self._hotkey_override = hotkey
        self._lock = threading.RLock()
        self._parts: _Parts | None = None
        self._mic: MicSource | None = None  # built once, shared by recording and /mic
        self._enabled = False
        self._closed = False
        self._on_reply: ReplySink = _ignore
        self._on_stale: ReplySink = _ignore
        self._warmup: threading.Thread | None = None
        self.notes: list[str] = []  # non-fatal facts, e.g. "groq skipped (no key)"

    # ---- state -----------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def state(self) -> VoiceState:
        parts = self._parts
        return parts.frontend.state if parts is not None else VoiceState.IDLE

    @property
    def hotkey_label(self) -> str:
        return "+".join(part.capitalize() for part in self._settings.voice.hotkey.split("+"))

    def set_sinks(self, on_reply: ReplySink, on_stale_reply: ReplySink) -> None:
        self._on_reply, self._on_stale = on_reply, on_stale_reply

    def status_line(self) -> str:
        return f"Listen: on ({self.hotkey_label})" if self._enabled else "Listen: off"

    # ---- lifecycle -------------------------------------------------------------------

    def enable(self) -> str | None:
        """Start listening. Returns None on success or the reason it cannot be done."""
        with self._lock:
            if self._closed:
                return "the application is shutting down"
            if self._enabled:
                return None
            try:
                parts = self._parts or self._build()
            except _Degraded as exc:
                return str(exc)
            self._parts = parts
            try:
                parts.frontend.start()
            except ToolError as exc:
                self._teardown()
                return str(exc)
            except Exception as exc:  # a listener bug must not crash the app
                logger.warning("hotkey start failed", exc_info=True)
                self._teardown()
                return f"could not register the hotkey ({type(exc).__name__})"
            self._enabled = True
            if parts.local is not None and self._warmup is None:
                self._warmup = parts.local.start_warm_up()
            return None

    def disable(self) -> None:
        """Stop listening: unregister the hotkey and cancel the running turn."""
        with self._lock:
            parts = self._parts
            self._enabled = False
        if parts is not None:
            parts.frontend.stop()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._enabled = False
            self._teardown()
            warmup = self._warmup
        if warmup is not None and warmup.is_alive():
            warmup.join(_WARMUP_JOIN_S)  # a model load cannot be interrupted; it is a daemon

    def _teardown(self) -> None:
        parts, self._parts = self._parts, None
        self._enabled = False
        closers: list[Any] = []
        if parts is not None:
            parts.frontend.close(_CLOSE_TIMEOUT_S)  # hotkey, active turn, turn threads
            closers = list(parts.closers)
        if self._mic is not None and self._mic not in closers:
            closers.append(self._mic)
        self._mic = None
        for closer in closers:
            try:
                closer.close()
            except Exception:
                logger.debug("voice input close failed", exc_info=True)

    # ---- building --------------------------------------------------------------------

    def _build(self) -> _Parts:
        settings = self._settings
        conf = settings.voice
        hotkey = self._hotkey_override
        if hotkey is None:
            if self._platform != "win32":
                raise _Degraded("global hotkeys are only supported on Windows")
            from luna.platform.windows.hotkey import WindowsHotkeyListener

            hotkey = WindowsHotkeyListener()
        mic = self._build_mic()
        closers: list[Any] = [mic]
        local: FasterWhisperSTT | None = None
        try:
            if self._stt_override is not None:
                stt: STTProvider = self._stt_override
            else:
                stt, local, extra = self._build_stt()
                closers.extend(extra)
            vad = EnergyVAD(conf.vad_energy_threshold, conf.vad_noise_margin, conf.frame_ms)
            recorder = Recorder(mic, vad, conf, clock=self._clock)
            frontend = VoiceFrontEnd(
                hotkey,
                recorder,
                stt,
                self._orchestrator,
                self._speaker,
                self._bus,
                lambda reply: self._on_reply(reply),
                on_stale_reply=lambda reply: self._on_stale(reply),
                voice=conf,
                stt_conf=settings.stt,
                clock=self._clock,
                beep=beep if conf.start_beep else None,
            )
        except BaseException:
            self._mic = None
            for closer in closers:
                try:
                    closer.close()
                except Exception:
                    logger.debug("voice input close failed", exc_info=True)
            raise
        return _Parts(frontend, mic, stt, local, closers)

    def _build_mic(self) -> MicSource:
        if self._mic is not None:
            return self._mic
        if self._mic_override is not None:
            self._mic = self._mic_override
            return self._mic
        reason = check_audio_input()
        if reason is not None:
            raise _Degraded(reason)
        conf = self._settings.voice
        self._mic = SoundDeviceMic(conf.input_device, conf.frame_ms, conf.preroll_ms)
        return self._mic

    def _build_stt(self) -> tuple[STTProvider, FasterWhisperSTT | None, list[Any]]:
        settings = self._settings
        engines = settings.available_stt_engines()
        for name in settings.stt.chain:
            if name not in engines:
                env_name = settings.stt.groq.api_key_env
                self._note(f"Speech recognition: {name} skipped ({env_name} is not set).")
        providers: list[STTProvider] = []
        closers: list[Any] = []
        local: FasterWhisperSTT | None = None
        for name in engines:
            if name == "groq":
                key = settings.api_key(settings.stt.groq.api_key_env)
                if key is None:
                    continue
                groq = GroqWhisperSTT(key, settings.stt.groq.model)
                providers.append(groq)
                closers.append(groq)
            elif name == "local":
                if importlib.util.find_spec("faster_whisper") is None:
                    self._note("Speech recognition: local skipped (faster-whisper not installed).")
                    continue
                conf = settings.stt.local
                local = FasterWhisperSTT(
                    conf.model,
                    conf.device,
                    conf.compute_type,
                    conf.beam_size,
                    conf.model_dir,
                )
                providers.append(local)
        if not providers:
            raise _Degraded(
                "no speech recognition engine is available "
                "(set GROQ_API_KEY or add 'local' to [stt] chain)"
            )
        self._note("Speech recognition: " + " -> ".join(p.name for p in providers) + ".")
        return STTChain(providers, self._clock, self._bus), local, closers

    def _note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)
        logger.info(text)

    # ---- /mic diagnostic -------------------------------------------------------------

    def measure(self, seconds: float = MEASURE_S) -> MicLevels:
        """Listen for ``seconds`` and report the noise floor and the peak. Raises ``AudioError``.

        Refused while a voice turn is running (the microphone is in use).
        """
        with self._lock:
            if self._closed:
                raise AudioError("the application is shutting down")
            if self.state is not VoiceState.IDLE:
                raise AudioError("a voice request is running; try again in a moment")
            mic = self._build_diag_mic()
        conf = self._settings.voice
        wanted = max(1, int(seconds * 1000 / conf.frame_ms))
        levels: list[float] = []
        deadline = time.monotonic() + seconds * 2 + 1
        mic.start()
        try:
            while len(levels) < wanted and time.monotonic() < deadline:
                frame = mic.read(0.1)
                if frame is None:
                    time.sleep(0.005)
                    continue
                levels.append(rms_level(frame))
        finally:
            mic.stop()
        if not levels:
            raise AudioError("the microphone delivered no audio")
        ordered = sorted(levels)
        noise = ordered[len(ordered) // 10]  # 10th percentile: quiet frames between words
        peak = ordered[-1]
        return MicLevels(noise, peak, len(levels), suggest_threshold(noise, peak))

    def _build_diag_mic(self) -> MicSource:
        try:
            return self._build_mic()
        except _Degraded as exc:
            raise AudioError(str(exc)) from exc


def _ignore(reply: Reply) -> None:
    return None
