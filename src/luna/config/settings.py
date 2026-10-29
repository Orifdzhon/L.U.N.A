"""Application settings: the full slice-0/1 schema (ARCHITECTURE section 6).

API keys are never part of the TOML schema. They are resolved from the environment / ``.env``
by the loader and kept in a private attribute as :class:`~pydantic.SecretStr`, so they never
show up in ``repr()``, ``model_dump()`` or ``model_dump_json()``.
"""

import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SecretStr,
    field_validator,
    model_validator,
)

_log = logging.getLogger(__name__)


def _known_key_envs() -> set[str]:
    from luna.config.secrets import KNOWN_KEY_NAMES
    from luna.llm.presets import load_presets

    return {*KNOWN_KEY_NAMES, *(p.key_env for p in load_presets() if p.key_env)}


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


# The Assistant must give up this many seconds before the Orchestrator's overall timeout.
ASSISTANT_MARGIN_S = 3.0


class AppSettings(_Section):
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    history_turns: int = Field(default=6, ge=0)
    request_timeout_s: float = Field(default=20.0, gt=ASSISTANT_MARGIN_S)


class ProviderSettings(_Section):
    """One OpenAI-compatible LLM endpoint."""

    base_url: str
    model: str
    api_key_env: str | None = None  # name of the env variable; None = no key needed (local)
    # Optional per-provider quirks (ADR 0002).
    supports_tool_choice: bool | None = None
    reasoning_effort: str | None = None
    max_tokens: int | None = Field(default=None, gt=0)
    # Result of the dialog's tool-calling probe; False = the chain sends this provider no tools
    # (it only answers). None = unknown, tools are sent as usual (ADR 0011 D16).
    supports_tools: bool | None = None

    @field_validator("api_key_env")
    @classmethod
    def _key_env_is_safe(cls, value: str | None) -> str | None:
        """Only a pattern-conforming name that is a known preset key or ``LUNA_*``.

        Stops a config / overlay from pointing a provider at another secret (``GITHUB_TOKEN``)
        and sending it to an arbitrary URL.
        """
        if value is None or not value.strip():
            return value
        name = value.strip()
        if not re.fullmatch(r"[A-Z0-9_]+", name):
            raise ValueError("api_key_env must match [A-Z0-9_]+")
        if not name.startswith("LUNA_") and name not in _known_key_envs():
            raise ValueError("api_key_env must be a known provider key name or start with LUNA_")
        return value


def _default_providers() -> dict[str, ProviderSettings]:
    return {
        "gemini": ProviderSettings(
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            model="gemini-3.5-flash-lite",
            api_key_env="GEMINI_API_KEY",
        ),
        "groq": ProviderSettings(
            base_url="https://api.groq.com/openai/v1",
            model="openai/gpt-oss-120b",
            reasoning_effort="low",  # gpt-oss thinks by default; voice replies need speed
            api_key_env="GROQ_API_KEY",
        ),
        # LM Studio (ADR 0002 amendment); Ollama: http://localhost:11434/v1
        "local": ProviderSettings(base_url="http://localhost:1234/v1", model="qwen3-4b"),
    }


class LLMSettings(_Section):
    chain: list[str] = Field(default_factory=lambda: ["groq", "gemini", "local"])
    providers: dict[str, ProviderSettings] = Field(default_factory=_default_providers)
    timeout_s: float = Field(default=20.0, gt=0)
    connect_timeout_s: float = Field(default=5.0, gt=0)

    @model_validator(mode="after")
    def _chain_names_known(self) -> Self:
        unknown = [name for name in self.chain if name not in self.providers]
        if unknown:
            raise ValueError(f"chain references undefined providers: {', '.join(unknown)}")
        return self


class SearchSettings(_Section):
    provider: Literal["ddgs", "searxng", "tavily"] = "ddgs"
    max_results: int = Field(default=5, ge=1)
    fetch_pages: int = Field(default=3, ge=0)
    searxng_url: str | None = None
    api_key_env: str = "TAVILY_API_KEY"  # used by the tavily provider only

    @model_validator(mode="after")
    def _searxng_needs_url(self) -> Self:
        if self.provider == "searxng" and not self.searxng_url:
            raise ValueError("searxng_url is required when provider = 'searxng'")
        return self


class RouterSettings(_Section):
    threshold: int = Field(default=85, ge=0, le=100)
    margin: int = Field(default=5, ge=0, le=100)
    app_aliases: dict[str, str | list[str]] = Field(default_factory=dict)
    site_aliases: dict[str, str] = Field(default_factory=dict)


class FilesSettings(_Section):
    """File search by name (``find_file`` / ``open_file``, ADR 0011)."""

    roots: list[Literal["user", "drives"]] = Field(default_factory=lambda: ["user", "drives"])
    extra_roots: list[Path] = Field(default_factory=list)  # more folders to search
    exclude: list[str] = Field(default_factory=list)  # more folder names to skip
    aliases: dict[str, str | list[str]] = Field(default_factory=dict)  # "резюме" = ["cv"]
    time_budget_s: float = Field(default=4.0, gt=0, le=30)
    max_entries: int = Field(default=300_000, ge=1000)
    max_results: int = Field(default=5, ge=1, le=10)
    send_names_to_llm: bool = True  # False: the LLM gets only counts, names stay on screen


class AppsSettings(_Section):
    """``close_app`` (ADR 0011)."""

    close_wait_s: float = Field(default=3.0, gt=0, le=30)  # wait for a window to close
    force_window_s: float = Field(default=60.0, gt=0, le=600)  # how long a force offer stays valid
    protected_extra: list[str] = Field(default_factory=list)  # more exe names never closed


EDGE_VOICE_PATTERN = r"^[a-z]{2,3}-[A-Z]{2,4}-[A-Za-z0-9]+Neural$"


class PiperSettings(_Section):
    """Local neural voices (provider "piper"); separate from the SAPI ``voice_*`` names."""

    voice_ru: Literal["ru_RU-irina-medium"] = "ru_RU-irina-medium"
    voice_en: Literal["en_US-kristin-medium"] = "en_US-kristin-medium"
    # None = %LOCALAPPDATA%\LUNA\models (shared with the speech and wake-word models).
    model_dir: Path | None = None
    num_threads: int = Field(default=2, ge=1, le=16)


class EdgeSettings(_Section):
    """Online voice tuning (provider "edge")."""

    # No audio byte after this long: Edge is stalled and the chunk (and the rest of the reply)
    # is spoken by the local voice. Upper bound of one attempt; there is no retry in the chain.
    first_audio_s: float = Field(default=2.5, gt=0, le=30)


class TTSSettings(_Section):
    enabled: bool = False
    # "piper": local neural voice, falling back to SAPI; "edge": Microsoft neural voices online,
    # falling back to the local Piper voice, then SAPI; "sapi": Windows voices only.
    provider: Literal["piper", "edge", "sapi"] = "piper"
    piper: PiperSettings = Field(default_factory=PiperSettings)
    edge: EdgeSettings = Field(default_factory=EdgeSettings)
    edge_voice_ru: str = Field(default="ru-RU-SvetlanaNeural", pattern=EDGE_VOICE_PATTERN)
    edge_voice_en: str = Field(default="en-US-JennyNeural", pattern=EDGE_VOICE_PATTERN)
    # Case-insensitive substrings matched against installed SAPI voice names (also the fallback).
    voice_ru: str = Field(default="Irina", min_length=1)
    voice_en: str = Field(default="Zira", min_length=1)
    rate: int = Field(default=0, ge=-10, le=10)  # edge: 5 % per step
    volume: int = Field(default=100, ge=0, le=100)
    max_chars: int = Field(default=1500, ge=1)  # spoken text per reply; cut at a sentence end
    first_chunk_max_chars: int = Field(default=200, ge=1)  # size of the first spoken chunk
    # Upper bound for the whole online synthesis of one chunk (the real limit scales with the
    # text length; the first audio must arrive within 5 s; one immediate retry).
    edge_timeout_s: float = Field(default=15.0, gt=0)
    # Skip Edge this long after 2 failed chunks in a row (one failure moves only that chunk).
    fallback_cooldown_s: float = Field(default=15.0, ge=0)


# Keys of the old pre-slice-3 example `[stt]` block (faster_whisper / cuda). They are ignored with
# a warning so an existing luna.toml keeps starting; see the `chain` / `local` keys instead.
_LEGACY_STT_KEYS = ("provider", "model", "device", "compute_type")


class STTGroqSettings(_Section):
    model: str = Field(default="whisper-large-v3-turbo", min_length=1)
    api_key_env: str = Field(default="GROQ_API_KEY", min_length=1)


class STTLocalSettings(_Section):
    model: str = Field(default="base", min_length=1)
    device: Literal["cpu", "cuda", "auto"] = "cpu"
    compute_type: str = Field(default="int8", min_length=1)
    beam_size: int = Field(default=1, ge=1, le=10)
    # None = resolved later to %LOCALAPPDATA%\LUNA\models.
    model_dir: Path | None = None


class STTSettings(_Section):
    # Order = fallback order. Provisional default (owner decision D1): groq first, local fallback.
    chain: list[Literal["groq", "local"]] = Field(default_factory=lambda: ["groq", "local"])
    # Language sent to the engines: "ru" (default), "en", any ISO code, or "auto" = detect.
    language: str = "ru"
    # Deprecated and ignored: superseded by ``language`` (kept so an old luna.toml still loads).
    language_hints: list[str] = Field(default_factory=lambda: ["ru", "en"])
    prompt: str = Field(default="", max_length=500)  # bias text for brand / app names
    timeout_s: float = Field(default=5.0, gt=0)
    groq: STTGroqSettings = Field(default_factory=STTGroqSettings)
    local: STTLocalSettings = Field(default_factory=STTLocalSettings)

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            legacy = [key for key in _LEGACY_STT_KEYS if key in data]
            if legacy:
                _log.warning(
                    "Ignoring deprecated [stt] keys %s; use chain / [stt.groq] / [stt.local]",
                    ", ".join(legacy),
                )
                data = {k: v for k, v in data.items() if k not in legacy}
        return data

    @field_validator("language")
    @classmethod
    def _language_valid(cls, value: str) -> str:
        value = value.strip().lower() if isinstance(value, str) else value
        if value != "auto" and not re.fullmatch(r"[a-z]{2,3}", str(value)):
            raise ValueError("language must be 'auto' or an ISO code like 'ru' or 'en'")
        return value

    @field_validator("chain")
    @classmethod
    def _chain_valid(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("chain must not be empty")
        if len(set(value)) != len(value):
            raise ValueError("chain must not contain duplicates")
        return value

    @field_validator("language_hints")
    @classmethod
    def _hints_valid(cls, value: list[str]) -> list[str]:
        if not value or any(not re.fullmatch(r"[a-z]{2,3}", hint) for hint in value):
            raise ValueError("language_hints must be a non-empty list of ISO codes like 'ru'")
        return value


_HOTKEY_MODIFIERS = frozenset({"ctrl", "alt", "shift", "win"})
_HOTKEY_KEY = re.compile(r"[a-z0-9_]+")


def _check_hotkey(value: str) -> str:
    parts = value.split("+")
    if any(not _HOTKEY_KEY.fullmatch(part) for part in parts):
        raise ValueError("hotkey must be lowercase names joined by '+', e.g. 'ctrl+alt+space'")
    modifiers = [part for part in parts if part in _HOTKEY_MODIFIERS]
    keys = [part for part in parts if part not in _HOTKEY_MODIFIERS]
    if len(set(modifiers)) != len(modifiers):
        raise ValueError("hotkey repeats a modifier")
    if not modifiers:
        raise ValueError("hotkey needs at least one modifier (ctrl, alt, shift, win)")
    if len(keys) != 1:
        raise ValueError("hotkey needs exactly one non-modifier key")
    return value


class VoiceSettings(_Section):
    listen: bool = False  # the microphone is never opened unless enabled
    hotkey: str = "ctrl+alt+space"
    start_beep: bool = False
    input_device: int | str | None = None  # None = default; int = index; str = name substring
    frame_ms: Literal[10, 20, 30] = 30
    vad_energy_threshold: float = Field(default=0.003, gt=0, le=1)  # RMS floor, 0..1 of full scale
    vad_noise_margin: float = Field(default=3.0, ge=1)  # speech = this many times the noise floor
    silence_ms: int = Field(default=700, gt=0, le=10_000)  # trailing silence that ends a phrase
    min_speech_ms: int = Field(default=200, gt=0)  # shorter than this is treated as noise
    preroll_ms: int = Field(default=300, ge=0, le=2000)  # audio kept from before speech starts
    max_utterance_s: float = Field(default=10.0, gt=0, le=60)

    @field_validator("hotkey")
    @classmethod
    def _hotkey_valid(cls, value: str) -> str:
        return _check_hotkey(value)

    @field_validator("input_device")
    @classmethod
    def _device_valid(cls, value: int | str | None) -> int | str | None:
        if isinstance(value, int) and value < 0:
            raise ValueError("input_device index must be >= 0")
        if isinstance(value, str) and not value.strip():
            raise ValueError("input_device name must not be blank")
        return value

    @model_validator(mode="after")
    def _speech_fits(self) -> Self:
        if self.min_speech_ms >= self.max_utterance_s * 1000:
            raise ValueError("min_speech_ms must be shorter than max_utterance_s")
        return self


class WakeWordSettings(_Section):
    enabled: bool = False  # opt-in (--wake / /wake); the microphone is never opened otherwise
    engine: Literal["vosk", "off"] = "vosk"
    keywords: list[str] = Field(default_factory=lambda: ["луна"])
    threshold: float = Field(default=0.5, ge=0, le=1)  # detector confidence, 0..1
    # "free": no grammar, the keyword must be a whole recognised word (rejects homophones such as
    # "Лена"); "grammar": recognizer restricted to the keywords (more false triggers).
    mode: Literal["grammar", "free"] = "free"
    cooldown_s: float = Field(default=2.0, ge=0)  # ignore repeated triggers for this long
    model: str = Field(default="small-ru", min_length=1)
    # None = resolved later to %LOCALAPPDATA%\LUNA\models (like [stt.local]).
    model_dir: Path | None = None
    mute_while_speaking: bool = True  # do not listen while L.U.N.A. speaks (no self-trigger)
    speaking_tail_ms: int = Field(default=400, ge=0, le=5000)  # extra mute after speech ends
    command_timeout_s: float = Field(default=5.0, gt=0, le=60)  # wait for a command after the word
    cue: Literal["off", "beep"] = "off"  # sound played when the keyword is heard
    follow_up_s: float = Field(default=0.0, ge=0, le=300)  # 0 = off; conversation window
    # Second stage (ADR 0004 amendment 2): local faster-whisper checks each detection.
    # Off by default (#170): whisper base mishears a quiet "Луна" (3/7 false rejects).
    verify: Literal["local", "off"] = "off"
    # Inflected forms Whisper often writes for the address word ("лунный", "лена" stay rejected).
    verify_words: list[str] = Field(
        default_factory=lambda: ["луна", "луну", "луне", "луны", "luna"]
    )
    # Opt-in: log the stage-2 transcript of REJECTED wakes at INFO, local log only (never sent
    # anywhere). Off by default: a rejected wake may hold speech that was not meant for L.U.N.A.
    log_rejects: bool = False

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_keys(cls, data: Any) -> Any:
        if isinstance(data, dict) and "hotkey" in data:
            _log.warning("[wakeword] hotkey is ignored, use [voice] hotkey")
            data = {k: v for k, v in data.items() if k != "hotkey"}
        return data

    @field_validator("engine", mode="before")
    @classmethod
    def _engine_known(cls, value: Any) -> Any:
        if value == "onnx":
            raise ValueError("engine 'onnx' is not supported yet; use 'vosk' or 'off'")
        return value

    @field_validator("keywords")
    @classmethod
    def _keywords_valid(cls, value: list[str]) -> list[str]:
        if not value or any(not word.strip() for word in value):
            raise ValueError("keywords must be a non-empty list of non-blank words")
        return [word.strip().lower() for word in value]

    @field_validator("verify_words")
    @classmethod
    def _verify_words_valid(cls, value: list[str]) -> list[str]:
        if not value or any(not word.strip() for word in value):
            raise ValueError("verify_words must be a non-empty list of non-blank words")
        return [word.strip().lower() for word in value]


class UISettings(_Section):
    start_minimized: bool = False  # start hidden in the tray
    notifications: bool = True  # tray balloon notifications
    history_lines: int = Field(default=200, ge=20, le=2000)  # lines kept in the window history
    language: Literal["auto", "ru", "en"] = "auto"  # UI language; auto = Windows UI language

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_keys(cls, data: Any) -> Any:
        if isinstance(data, dict) and "autostart" in data:
            if data["autostart"] is True:
                _log.warning('[ui] autostart is ignored, use the tray menu "Start with Windows"')
            data = {k: v for k, v in data.items() if k != "autostart"}
        return data


class Settings(_Section):
    app: AppSettings = Field(default_factory=AppSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    search: SearchSettings = Field(default_factory=SearchSettings)
    router: RouterSettings = Field(default_factory=RouterSettings)
    apps: AppsSettings = Field(default_factory=AppsSettings)
    tts: TTSSettings = Field(default_factory=TTSSettings)
    stt: STTSettings = Field(default_factory=STTSettings)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    wakeword: WakeWordSettings = Field(default_factory=WakeWordSettings)
    ui: UISettings = Field(default_factory=UISettings)
    files: FilesSettings = Field(default_factory=FilesSettings)

    # env-variable name -> secret value; never serialized, never in repr().
    _api_keys: dict[str, SecretStr] = PrivateAttr(default_factory=dict)

    def api_key_names(self) -> set[str]:
        """Env-variable names of every key this config refers to."""
        names = {(p.api_key_env or "").strip() for p in self.llm.providers.values()}
        names.add(self.search.api_key_env.strip())
        names.add(self.stt.groq.api_key_env.strip())
        names.discard("")
        return names

    def with_api_keys(self, env: Mapping[str, str]) -> Self:
        """Pick up the keys this config refers to from ``env``; blank values count as missing."""
        names = self.api_key_names()
        self._api_keys = {n: SecretStr(env[n].strip()) for n in names if env.get(n, "").strip()}
        return self

    def api_key(self, env_name: str | None) -> SecretStr | None:
        """Secret stored under ``env_name`` (``None`` if absent or ``env_name`` is blank)."""
        env_name = (env_name or "").strip()
        return self._api_keys.get(env_name) if env_name else None

    def secret_values(self) -> list[str]:
        """Plain secret values, only for registering them with the log redaction filter."""
        return [secret.get_secret_value() for secret in self._api_keys.values()]

    def available_providers(self) -> list[str]:
        """LLM providers of the chain that can be used, in chain order.

        A provider with ``api_key_env`` but no key is unavailable (degraded start, not an error);
        providers without ``api_key_env`` (local) are always available.
        """
        return [
            name
            for name in self.llm.chain
            if not (env_name := (self.llm.providers[name].api_key_env or "").strip())
            or env_name in self._api_keys
        ]

    def available_stt_engines(self) -> list[str]:
        """STT engines of the chain that can be used, in chain order.

        ``groq`` without its key is skipped (degraded start, not an error); ``local`` needs none.
        """
        return [
            name
            for name in self.stt.chain
            if name != "groq" or self.stt.groq.api_key_env.strip() in self._api_keys
        ]
