"""Composition root: the only module that knows the concrete classes (ARCHITECTURE section 1).

``build_app(settings)`` wires providers, tools, Router, Assistant, Orchestrator, ActionLog and
EventBus from :class:`~luna.config.settings.Settings`. Degraded start (section 8): an LLM
provider without its key is skipped with a warning; with no provider at all the app still
starts (the Router fast path works) and LLM answers apologise. Keyword-only arguments replace
the real adapters (network, browser, app launching) so tests can wire fakes.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from luna.audio.base import AudioPlayer
from luna.audio.capture import MicSource
from luna.config.settings import ASSISTANT_MARGIN_S, Settings, TTSSettings
from luna.core.assistant import Assistant
from luna.core.clock import Clock
from luna.core.errors import ProviderUnavailable
from luna.core.events import EventBus
from luna.core.orchestrator import Orchestrator
from luna.core.router import Router
from luna.llm.base import ChatResponse, LLMProvider, Message, ToolSchema
from luna.llm.chain import LLMChain
from luna.llm.openai_compat import OpenAICompatProvider, Quirks
from luna.logs.action_log import ActionLog
from luna.logs.setup import default_log_dir
from luna.platform.base import AppCatalog, AppLauncher, BrowserOpener, HotkeyListener
from luna.search.base import SearchProvider
from luna.stt.base import STTProvider
from luna.tools.builtin.datetime import GetDatetimeTool
from luna.tools.builtin.open_app import OpenAppTool
from luna.tools.builtin.open_website import OpenWebsiteTool
from luna.tools.builtin.web_search import WebSearchTool
from luna.tools.registry import ToolRegistry
from luna.tts.base import TTSProvider
from luna.voice.input import VoiceInput

if TYPE_CHECKING:
    from luna.tts.speaker import Speaker

logger = logging.getLogger(__name__)

VOICE_EXTRA_HINT = "run `uv sync` to install it"
WAKE_MIC_NOTICE = (
    "The microphone stays open while wake mode is on; nothing is stored or sent "
    "before the wake word. /wake off closes it."
)

ACTION_LOG_NAME = "actions.jsonl"
_CATALOG_JOIN_S = 1.0
_ORCHESTRATOR_CLOSE_S = 2.0


class _NoLLM:
    """Stands in for the chain when no provider is configured: every call is "unavailable"."""

    name = "none"

    def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] = (),
        *,
        timeout_s: float,
        request_id: str | None = None,
    ) -> ChatResponse:
        raise ProviderUnavailable("no LLM provider is configured")


@dataclass
class App:
    """Everything the UI needs; ``close()`` releases the threads and HTTP clients."""

    settings: Settings
    orchestrator: Orchestrator
    assistant: Assistant
    bus: EventBus
    registry: ToolRegistry
    action_log: ActionLog
    active_providers: list[str] = field(default_factory=list)
    missing_keys: dict[str, str] = field(default_factory=dict)  # provider -> env variable name
    warnings: list[str] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    speaker: Speaker | None = None  # None: voice is off (text mode only)
    voice_input: VoiceInput | None = None  # push-to-talk; built lazily, see ``enable()``
    _closers: list[LLMProvider] = field(default_factory=list, repr=False)
    _catalog_thread: threading.Thread | None = field(default=None, repr=False)
    _closed: bool = field(default=False, repr=False)

    @property
    def llm_available(self) -> bool:
        return bool(self.active_providers)

    def describe_providers(self) -> list[str]:
        """``name (model)`` for every active provider (just ``name`` for an injected LLM)."""
        models = {n: p.model for n, p in self.settings.llm.providers.items()}
        return [f"{n} ({models[n]})" if n in models else n for n in self.active_providers]

    def model_notes(self) -> list[str]:
        """Hint when a provider still uses the built-in default model name."""
        defaults = Settings().llm.providers
        notes = []
        for name in self.active_providers:
            conf = self.settings.llm.providers.get(name)
            # Local servers (no key): the model name must match what is loaded.
            if (
                conf is not None
                and not conf.api_key_env
                and name in defaults
                and conf.model == defaults[name].model
            ):
                notes.append(
                    f"Note: '{name}' uses the default model '{conf.model}'. "
                    "Set the exact model name in luna.toml ([llm.providers.local] model)."
                )
        return notes

    def startup_lines(self) -> list[str]:
        """Human-readable startup summary. Names only, never key values."""
        lines: list[str] = []
        if self.active_providers:
            lines.append("LLM providers: " + " -> ".join(self.describe_providers()))
            lines.extend(self.model_notes())
        else:
            lines.append(
                "LLM providers: none. Answers are unavailable; "
                "commands like 'open youtube' still work."
            )
        for name, env_name in self.missing_keys.items():
            lines.append(f"Missing key for '{name}' ({env_name}): provider skipped.")
        lines.extend(self.warnings)
        if self.speaker is not None:
            lines.append("Voice: on (/mute, /stop, /voice).")
        elif not any(w.startswith("Voice disabled") for w in self.warnings):
            lines.append("Voice: off (start with --voice to speak replies).")
        lines.extend(self._listen_lines())
        return lines

    def _listen_lines(self) -> list[str]:
        voice_input = self.voice_input
        if voice_input is None:
            return []
        if voice_input.enabled:
            lines = [voice_input.status_line() + " - /listen, /mic, /wake.", *voice_input.notes]
            if voice_input.wake_active:
                lines.append(voice_input.wake_status_line())
                lines.append(WAKE_MIC_NOTICE)
            return lines
        if any(w.startswith("Voice input disabled") for w in self.warnings):
            return []
        return ["Listen: off (start with --listen or type /listen to talk with the hotkey)."]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.voice_input is not None:
            self.voice_input.close()  # hotkey, mic and voice turns first: they use the rest
        self.orchestrator.close(_ORCHESTRATOR_CLOSE_S)
        if self.speaker is not None:
            self.speaker.close()  # before the bus: it publishes its final IDLE there
        self.bus.close()
        thread = self._catalog_thread
        if thread is not None and thread.is_alive():
            thread.join(_CATALOG_JOIN_S)
        _close_providers(self._closers)


def _build_providers(settings: Settings) -> tuple[list[OpenAICompatProvider], dict[str, str]]:
    available = set(settings.available_providers())
    providers: list[OpenAICompatProvider] = []
    missing: dict[str, str] = {}
    for name in settings.llm.chain:
        conf = settings.llm.providers[name]
        if name not in available:
            missing[name] = (conf.api_key_env or "").strip()
            logger.warning("LLM provider %r skipped: %s is not set", name, missing[name])
            continue
        try:
            providers.append(
                OpenAICompatProvider(
                    name,
                    conf.base_url,
                    conf.model,
                    settings.api_key(conf.api_key_env),
                    Quirks(conf.supports_tool_choice, conf.reasoning_effort, conf.max_tokens),
                    connect_timeout_s=settings.llm.connect_timeout_s,
                )
            )
        except Exception as exc:
            logger.warning("LLM provider %r skipped: %s", name, type(exc).__name__)
            missing[name] = f"invalid configuration ({type(exc).__name__})"
    return providers, missing


def assistant_timeout(settings: Settings) -> float:
    """Assistant deadline: the LLM timeout, but ASSISTANT_MARGIN_S under the request timeout."""
    return max(
        1.0, min(settings.llm.timeout_s, settings.app.request_timeout_s - ASSISTANT_MARGIN_S)
    )


def _warm_catalog(catalog: AppCatalog) -> None:
    try:
        catalog.list_apps()  # reads the on-disk cache or scans Start Menu apps once
    except Exception as exc:
        logger.warning("app catalog scan failed: %s", type(exc).__name__)


def build_app(settings: Settings, **overrides: Any) -> App:
    """Build the whole object graph; providers created before a failure are closed again."""
    created: list[LLMProvider] = []
    try:
        return _assemble(settings, created, **overrides)
    except BaseException:
        _close_providers(created)
        raise


def _close_providers(providers: Sequence[LLMProvider]) -> None:
    for provider in providers:
        closer = getattr(provider, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                logger.debug("provider close failed", exc_info=True)


def _check_audio_output() -> str | None:
    """Reason why audio output is unusable, or None. Imports ``sounddevice`` lazily."""
    try:
        import sounddevice as sd
    except ImportError:
        return f"audio backend is not installed; {VOICE_EXTRA_HINT}"
    except Exception as exc:  # e.g. OSError: PortAudio library not found
        return f"audio backend failed to load ({type(exc).__name__}); {VOICE_EXTRA_HINT}"
    try:
        sd.query_devices(kind="output")
    except Exception:
        return "no audio output device found"
    return None


def _build_tts(
    conf: TTSSettings,
    platform: str,
    warnings: list[str],
    disabled: Callable[[str], None],
) -> TTSProvider | None:
    """The configured voice chain (edge -> piper -> sapi, piper -> sapi, or sapi), or None."""
    sapi = None
    if platform == "win32":
        from luna.tts.sapi import SapiTTS

        sapi = SapiTTS(conf.voice_ru, conf.voice_en, conf.rate, conf.volume)
        try:
            voices = sapi.list_voices()
        except Exception as exc:
            problem = f"cannot list SAPI voices ({type(exc).__name__})"
        else:
            problem = "" if voices else "no SAPI voices are installed"
        if problem:
            sapi = None
    else:
        problem = "SAPI voices are only available on Windows"
    if conf.provider == "sapi":
        if sapi is None:
            disabled(problem)
        return sapi
    from luna.tts.edge import EdgeTTS, FallbackTTS
    from luna.tts.piper import PiperTTS

    def make_piper() -> TTSProvider | None:
        if importlib.util.find_spec("sherpa_onnx") is None:  # public build: before any download
            warnings.append("Local voice (Piper) is unavailable (sherpa-onnx is not installed).")
            return None
        try:
            return PiperTTS(
                conf.piper.voice_ru,
                conf.piper.voice_en,
                conf.rate,
                conf.volume,
                model_dir=conf.piper.model_dir,
                num_threads=conf.piper.num_threads,
            )
        except Exception as exc:  # one warning; the chain simply skips the local neural voice
            warnings.append(f"Local voice (Piper) is unavailable ({type(exc).__name__}).")
            return None

    chain: list[TTSProvider] = []
    if conf.provider == "piper":
        piper = make_piper()
        if piper is not None:
            chain.append(piper)
    else:
        chain.append(
            EdgeTTS(
                conf.edge_voice_ru,
                conf.edge_voice_en,
                conf.rate,
                conf.volume,
                timeout_s=conf.edge_timeout_s,
                first_audio_timeout_s=conf.edge.first_audio_s,
                attempts=1,  # a stalled chunk goes to the local voice, not to a slow retry
            )
        )
        piper = make_piper()
        if piper is not None:
            chain.append(piper)
    if sapi is not None:
        chain.append(sapi)
    if not chain:
        disabled(problem)
        return None
    if len(chain) == 1:
        if conf.provider == "edge":
            warnings.append(f"No offline fallback voice: {problem}. Speech needs the network.")
        return chain[0]
    return FallbackTTS(*chain, cooldown_s=conf.fallback_cooldown_s)


def _build_speaker(
    settings: Settings,
    bus: EventBus,
    tts: TTSProvider | None,
    player: AudioPlayer | None,
    platform: str,
    warnings: list[str],
) -> Speaker | None:
    """Speaker for the enabled voice, or None plus a "Voice disabled" warning (section 8)."""

    def disabled(reason: str) -> None:
        warnings.append(f"Voice disabled: {reason}. Continuing in text mode.")

    conf = settings.tts
    built_tts = tts is None  # only a provider built here is warmed (never an injected fake)
    if tts is None:
        tts = _build_tts(conf, platform, warnings, disabled)
        if tts is None:
            return None
    if player is None:
        reason = _check_audio_output()
        if reason is not None:
            disabled(reason)
            return None
        from luna.audio.playback import SoundDevicePlayer

        player = SoundDevicePlayer()
    from luna.tts.speaker import Speaker

    speaker = Speaker(tts, player, bus, conf)
    if built_tts:
        speaker.start_warmup()  # background thread; never blocks startup
    return speaker


def _assemble(
    settings: Settings,
    closers: list[LLMProvider],
    *,
    llm: LLMProvider | None = None,
    search: SearchProvider | None = None,
    browser: BrowserOpener | None = None,
    catalog: AppCatalog | None = None,
    launcher: AppLauncher | None = None,
    log_dir: Path | None = None,
    platform: str | None = None,
    clock: Clock | None = None,
    tts: TTSProvider | None = None,
    player: AudioPlayer | None = None,
    stt: STTProvider | None = None,
    mic: MicSource | None = None,
    hotkey: HotkeyListener | None = None,
    wake_store: Any | None = None,
    spotter_factory: Callable[[Path], Any] | None = None,
    wake_progress: Callable[[str], None] | None = None,
    start_wake: bool = True,
) -> App:
    """Secrets must already be registered for log redaction."""
    clock = clock or time.monotonic
    platform = platform or sys.platform
    warnings: list[str] = []
    bus = EventBus()
    directory = log_dir if log_dir is not None else default_log_dir()
    action_log = ActionLog(directory / ACTION_LOG_NAME)
    registry = ToolRegistry(action_log)

    # LLM
    missing: dict[str, str] = {}
    active: list[str] = []
    chat: LLMProvider
    if llm is not None:
        chat, active = llm, [llm.name]
    else:
        providers, missing = _build_providers(settings)
        closers.extend(providers)
        active = [p.name for p in providers]
        chat = LLMChain(providers, clock, bus) if providers else _NoLLM()

    # Tools
    if browser is None:
        from luna.platform.browser import WebBrowserOpener

        browser = WebBrowserOpener()
    registry.register(OpenWebsiteTool(browser, settings.router.site_aliases))
    registry.register(GetDatetimeTool())

    thread: threading.Thread | None = None
    if catalog is None and platform == "win32":
        from luna.platform.windows.apps import WindowsAppCatalog, WindowsAppLauncher

        catalog = WindowsAppCatalog()
        launcher = launcher or WindowsAppLauncher(catalog)
    if catalog is not None and launcher is not None:
        registry.register(
            OpenAppTool(
                catalog,
                launcher,
                settings.router.app_aliases,
                threshold=settings.router.threshold,
                margin=settings.router.margin,
            )
        )
        thread = threading.Thread(
            target=_warm_catalog, args=(catalog,), name="luna-app-catalog", daemon=True
        )
        thread.start()
    else:
        warnings.append("Launching apps is only supported on Windows: 'open <app>' is disabled.")

    if search is None and settings.search.provider == "ddgs":
        from luna.search.ddgs_search import DdgsSearch

        search = DdgsSearch()
    if search is not None:
        registry.register(
            WebSearchTool(
                search,
                max_results=settings.search.max_results,
                fetch_pages=settings.search.fetch_pages,
            )
        )
    else:
        warnings.append(
            f"Search provider '{settings.search.provider}' is not available yet: "
            "web search is disabled."
        )

    assistant = Assistant(
        chat,
        registry,
        history_turns=settings.app.history_turns,
        timeout_s=assistant_timeout(settings),
    )
    orchestrator = Orchestrator(
        Router(registry), assistant, bus, settings.app.request_timeout_s, timer=clock
    )
    speaker = None
    if settings.tts.enabled:
        speaker = _build_speaker(settings, bus, tts, player, platform, warnings)
    voice_input = VoiceInput(
        settings,
        bus,
        orchestrator,
        speaker,
        platform=platform,
        clock=clock,
        stt=stt,
        mic=mic,
        hotkey=hotkey,
        wake_store=wake_store,
        spotter_factory=spotter_factory,
    )
    wake = settings.wakeword.enabled  # wake mode implies voice input
    if settings.voice.listen or wake:
        reason = voice_input.enable(hotkey_required=not wake)
        if reason is not None:
            warnings.append(f"Voice input disabled: {reason}. Continuing in text mode.")
            wake = False
    if wake and start_wake:  # tray mode: the shell enables wake on its worker thread
        reason = voice_input.enable_wake(wake_progress)
        if reason is not None:
            warnings.append(f"Wake word disabled: {reason}. Continuing with the hotkey.")
    for text in warnings:
        logger.warning(text)
    return App(
        settings=settings,
        orchestrator=orchestrator,
        assistant=assistant,
        bus=bus,
        registry=registry,
        action_log=action_log,
        active_providers=active,
        missing_keys=missing,
        warnings=warnings,
        tool_names=sorted(s.name for s in registry.schemas()),
        speaker=speaker,
        voice_input=voice_input,
        _closers=closers,
        _catalog_thread=thread,
    )
