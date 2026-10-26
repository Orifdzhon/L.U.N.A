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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from luna.audio.capture import MicSource, SoundDeviceMic, check_audio_input
from luna.audio.recorder import Recorder
from luna.audio.vad import EnergyVAD, rms_level
from luna.config.settings import Settings
from luna.core.clock import Clock
from luna.core.errors import AudioError, ToolError
from luna.core.events import Event, EventBus
from luna.core.models import Reply
from luna.platform.base import HotkeyListener
from luna.stt.base import STTProvider
from luna.stt.chain import STTChain
from luna.stt.faster_whisper import FasterWhisperSTT
from luna.stt.groq import GroqWhisperSTT
from luna.voice.frontend import VoiceFrontEnd, VoiceState

if TYPE_CHECKING:
    from luna.wakeword.base import WakeWordDetector
    from luna.wakeword.listener import WakeListener
    from luna.wakeword.verify import WakeVerifier

logger = logging.getLogger(__name__)

MEASURE_S = 3.0
_CLOSE_TIMEOUT_S = 2.0
_WARMUP_JOIN_S = 0.2
BEEP_HZ = 880
BEEP_MS = 80
DIAG_HOLD = "diag"  # wake listener hold while /mic measures

ReplySink = Callable[[Reply], None]
ProgressSink = Callable[[str], None]
SpotterFactory = Callable[[Path], "WakeWordDetector"]

MODEL_DOWNLOAD_NOTICE = "Downloading the wake word model, 45 MB (one time)..."


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


def _noop() -> None:
    return None


def _ignore_text(text: str) -> None:
    return None


@dataclass
class _Wake:
    """Everything wake mode owns; kept after ``/wake off`` so the models load once."""

    listener: WakeListener
    spotter: WakeWordDetector
    verifier: WakeVerifier
    model_path: Path
    unbind: Callable[[], None] = field(default=_noop)
    active: bool = False


class _WakeBus:
    """Bus given to the listener: forwards everything, and reports ``FAILED`` to VoiceInput."""

    def __init__(self, bus: EventBus, on_failed: Callable[[str], None]) -> None:
        self._bus = bus
        self._on_failed = on_failed

    def publish(self, event: Event) -> None:
        self._bus.publish(event)
        if getattr(getattr(event, "state", None), "value", None) == "failed":
            self._on_failed(str(getattr(event, "detail", None) or "unknown error"))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bus, name)


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
        wake_store: Any | None = None,
        spotter_factory: SpotterFactory | None = None,
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
        self._wake_store = wake_store  # tests; default: WakeModelStore([wakeword] model_dir)
        self._spotter_factory = spotter_factory
        self._wake: _Wake | None = None
        self._wake_local: FasterWhisperSTT | None = None  # built for the verifier when needed
        self._wake_failure: str | None = None
        self._hotkey_failure: str | None = None  # set when only wake mode could start
        self._wake_building = False
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
        if not self._enabled:
            return "Listen: off"
        if self._hotkey_failure:
            return "Listen: on (no hotkey, wake word only)"
        return f"Listen: on ({self.hotkey_label})"

    @property
    def wake_active(self) -> bool:
        wake = self._wake
        return wake is not None and wake.active

    @property
    def wake_state(self) -> str:
        """``ARMED`` / ``HELD`` / ``FAILED`` / ``OFF`` (what the listener reports)."""
        wake = self._wake
        if wake is None or not wake.active:
            return "OFF"
        return str(wake.listener.state.name)

    def wake_status_line(self) -> str:
        """One line for ``/wake`` and the startup summary (never any audio or transcript)."""
        conf = self._settings.wakeword
        state = self.wake_state
        keyword = ", ".join(f"«{word}»" for word in conf.keywords)
        wake = self._wake
        if wake is None or state == "OFF":
            return f"Wake: OFF (keyword {keyword}; /wake on to start)"
        line = (
            f"Wake: {state}, keyword {keyword}, verify: {wake.verifier.name}, "
            f"model: {wake.model_path}"
        )
        if wake.verifier.name == "off":
            line += " - verification off — false wakes may be sent to cloud STT"
        if state == "FAILED" and self._wake_failure:
            line += f" ({self._wake_failure}; /wake on retries)"
        parts = self._parts
        if parts is not None and (stats := parts.frontend.wake_stats)["detections"]:
            line += (
                f" | session: {stats['detections']} detected, "
                f"{stats['accepted']} accepted, {stats['rejected']} rejected"
            )
        return line

    # ---- lifecycle -------------------------------------------------------------------

    def enable(self, *, hotkey_required: bool = True) -> str | None:
        """Start listening. Returns None on success or the reason it cannot be done.

        ``hotkey_required=False`` (wake mode) tolerates a hotkey that cannot be registered
        (taken by another program): voice turns still work, only push-to-talk is missing.
        """
        with self._lock:
            if self._closed:
                return "the application is shutting down"
            if self._enabled:
                return self._hotkey_failure if hotkey_required else None
            try:
                parts = self._parts or self._build()
            except _Degraded as exc:
                return str(exc)
            self._parts = parts
            try:
                registered = parts.frontend.start(hotkey_required=hotkey_required)
            except ToolError as exc:
                self._teardown()
                return str(exc)
            except Exception as exc:  # a listener bug must not crash the app
                logger.warning("hotkey start failed", exc_info=True)
                self._teardown()
                return f"could not register the hotkey ({type(exc).__name__})"
            self._hotkey_failure = (
                None if registered else f"the hotkey {self.hotkey_label} is not available"
            )
            if not registered:
                self.notes.append(f"{self._hotkey_failure}; wake mode works without it.")
            self._enabled = True
            if parts.local is not None and self._warmup is None:
                self._warmup = parts.local.start_warm_up()
            return None

    def disable(self) -> None:
        """Stop listening: wake mode off, hotkey unregistered, the running turn cancelled."""
        with self._lock:
            self.disable_wake()
            parts = self._parts
            self._enabled = False
            self._hotkey_failure = None
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
            local, self._wake_local = self._wake_local, None
        closer = getattr(local, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                logger.debug("verifier STT close failed", exc_info=True)
        if warmup is not None and warmup.is_alive():
            warmup.join(_WARMUP_JOIN_S)  # a model load cannot be interrupted; it is a daemon

    # ---- wake mode (slice 4) ---------------------------------------------------------

    def enable_wake(self, progress: ProgressSink | None = None) -> str | None:
        """Start wake mode. Returns None on success or the reason it cannot be done.

        Blocking: the first call may download the Vosk model and loads the models, so the UI
        shows the ``progress`` lines. Push-to-talk stays usable whatever happens here.
        """
        say = progress or _ignore_text
        with self._lock:
            if self._closed:
                return "the application is shutting down"
            wake = self._wake
            if wake is not None and wake.active and wake.listener.state.name != "FAILED":
                return None
            if self._settings.wakeword.engine == "off":
                return "[wakeword] engine is off"
            reason = self.enable(hotkey_required=False)
            if reason is not None:
                return reason
            parts = self._parts
            assert parts is not None
            if wake is not None:  # models are already built: just start again
                return self._start_wake_safely(wake, parts)
            if self._wake_building:
                return "wake mode is already starting"
            self._wake_building = True
        # The download and the model loads run WITHOUT the lock, so close() stays prompt.
        try:
            built = self._build_wake(parts, say)
        except _Degraded as exc:
            return str(exc)
        except Exception as exc:  # a wake bug must not crash the app
            logger.warning("wake mode failed to build", exc_info=True)
            return f"unexpected error ({type(exc).__name__})"
        finally:
            with self._lock:
                self._wake_building = False
        with self._lock:
            if self._closed or self._parts is not parts:
                built.spotter.close()  # the app shut down meanwhile: discard what was built
                return "the application is shutting down"
            self._wake = built
            return self._start_wake_safely(built, parts)

    def _start_wake_safely(self, wake: _Wake, parts: _Parts) -> str | None:
        try:
            return self._start_wake(wake, parts)
        except Exception as exc:
            logger.warning("wake mode failed to start", exc_info=True)
            self.disable_wake()
            return f"unexpected error ({type(exc).__name__})"

    def disable_wake(self) -> None:
        """Back to push-to-talk: the listener stops (microphone released), the models stay.

        Takes the VoiceInput lock; ``listener.stop()`` joins ``luna-wake``, whose callbacks
        (``on_wake``, the FAILED handler) never take that lock, so there is no deadlock.
        """
        with self._lock:
            wake, parts = self._wake, self._parts
            if wake is None or not wake.active:
                return
            wake.active = False
            if parts is not None:
                parts.frontend.set_live(None)  # cancels a turn on a tap before the tap closes
            wake.unbind()
            wake.unbind = _noop
            wake.listener.stop()  # joins luna-wake; never called under the front end lock

    def _start_wake(self, wake: _Wake, parts: _Parts) -> str | None:
        conf = self._settings.wakeword
        from luna.wakeword.duplex import bind_speaker

        self._wake_failure = None
        wake.unbind()  # a retry after FAILED must not bind the speaker twice
        parts.frontend.set_live(wake.listener, conf, wake.verifier)
        wake.active = True
        if self._speaker is not None:
            wake.unbind = bind_speaker(self._speaker, wake.listener, conf)
        try:
            wake.listener.start()
        except AudioError as exc:
            self.disable_wake()
            return f"cannot open the microphone ({exc})"
        return None

    def _build_wake(self, parts: _Parts, say: ProgressSink) -> _Wake:
        from luna.wakeword.listener import WakeListener

        conf = self._settings.wakeword
        voice = self._settings.voice
        model_path = self._ensure_wake_model(say)
        verifier = self._build_verifier(parts, say)
        spotter = self._build_spotter(model_path)

        def vad_factory() -> EnergyVAD:  # the Recorder keeps its own instance
            return EnergyVAD(voice.vad_energy_threshold, voice.vad_noise_margin, voice.frame_ms)

        try:
            listener = WakeListener(
                parts.mic,
                spotter,
                vad_factory,
                conf,
                voice,
                _WakeBus(self._bus, self._on_wake_failed),
                on_wake=parts.frontend.on_wake,
                clock=self._clock,
            )
        except BaseException:
            spotter.close()
            raise
        return _Wake(listener, spotter, verifier, model_path)

    def _ensure_wake_model(self, say: ProgressSink) -> Path:
        store = self._wake_store
        if store is None:
            from luna.voice.wake_model import WakeModelStore

            store = WakeModelStore(self._settings.wakeword.model_dir)
        try:
            if not store.is_present():
                say(MODEL_DOWNLOAD_NOTICE)
            path = store.ensure()
        except Exception as exc:
            raise _Degraded(f"cannot get the wake word model: {exc}") from exc
        return Path(path)

    def _build_spotter(self, model_path: Path) -> WakeWordDetector:
        conf = self._settings.wakeword
        factory = self._spotter_factory
        try:
            if factory is not None:
                return factory(model_path)
            from luna.wakeword.vosk_spotter import VoskKeywordSpotter

            return VoskKeywordSpotter(model_path, conf.keywords, threshold=conf.threshold)
        except Exception as exc:
            raise _Degraded(f"cannot start the wake word engine: {exc}") from exc

    def _build_verifier(self, parts: _Parts, say: ProgressSink) -> WakeVerifier:
        from luna.wakeword.verify import FasterWhisperVerifier, PassThroughVerifier

        conf = self._settings.wakeword
        if conf.verify == "off":
            return PassThroughVerifier()
        local = parts.local or self._wake_local
        if local is None:
            stt_conf = self._settings.stt.local
            local = FasterWhisperSTT(
                stt_conf.model,
                stt_conf.device,
                stt_conf.compute_type,
                stt_conf.beam_size,
                stt_conf.model_dir,
            )
            self._wake_local = local
        verifier = FasterWhisperVerifier(local, conf.verify_words)
        say("Loading the wake verification model (a few seconds)...")
        try:
            verifier.warm_up()  # shares the load lock with the stt-warm-up thread: loaded once
        except Exception as exc:
            text = f"Wake verification unavailable ({exc}); every detection starts a turn."
            self._note(text)
            say(text)
            return PassThroughVerifier(str(exc))
        return verifier

    def _on_wake_failed(self, reason: str) -> None:
        """Listener failed (luna-wake thread): hotkey turns go back to the microphone itself.

        Takes no VoiceInput lock: ``disable_wake`` holds it while joining this thread. A late
        report from a listener that is no longer FAILED (a quick retry) is ignored.
        """
        wake, parts = self._wake, self._parts
        if wake is None or not wake.active or wake.listener.state.name != "FAILED":
            return
        self._wake_failure = reason
        if parts is not None:
            parts.frontend.set_live(None)

    def _teardown(self) -> None:
        wake, self._wake = self._wake, None
        if wake is not None:
            wake.active = False
            wake.unbind()
            if self._parts is not None:
                self._parts.frontend.set_live(None)
            try:
                wake.listener.close()  # joins luna-wake, closes the microphone and the detector
            except Exception:
                logger.warning("closing the wake listener failed", exc_info=True)
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
            wake = self._wake
            tap = None
            held = None
            if wake is not None and wake.active:
                try:
                    wake.listener.set_hold(DIAG_HOLD, True)  # no wake turn steals the tap
                    held = wake.listener
                    tap = wake.listener.open_tap(0).frames  # no second stream on the microphone
                except AudioError:
                    wake.listener.set_hold(DIAG_HOLD, False)
                    raise AudioError("wake mode has no microphone stream; try /wake on") from None
                mic: Any = tap
            else:
                mic = self._build_diag_mic()
        conf = self._settings.voice
        wanted = max(1, int(seconds * 1000 / conf.frame_ms))
        levels: list[float] = []
        deadline = time.monotonic() + seconds * 2 + 1
        if tap is None:
            mic.start()
        try:
            while len(levels) < wanted and time.monotonic() < deadline:
                frame = mic.read(0.1)
                if frame is None:
                    time.sleep(0.005)
                    continue
                levels.append(rms_level(frame))
        finally:
            if tap is None:
                mic.stop()
            else:
                tap.close()
            if held is not None:
                held.set_hold(DIAG_HOLD, False)
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
