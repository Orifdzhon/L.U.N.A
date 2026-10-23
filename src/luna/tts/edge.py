"""``EdgeTTS``: Microsoft Edge online neural voices via the ``edge-tts`` library (ADR 0005,
amendment 2).

The service is an unofficial endpoint (it may change or break) and the reply text is sent to
Microsoft; the owner accepted both. :class:`FallbackTTS` therefore wraps it with SAPI.

Integration: ``edge-tts`` is asyncio-based and always streams MP3 (24 kHz, 48 kbps, mono; the
output format is not configurable). ``synthesize`` runs on the Speaker's synthesis thread, so
it uses its own short-lived event loop (``asyncio.run``) with an overall timeout, then decodes
the MP3 to int16 mono PCM with ``miniaudio`` (small wheels, no ffmpeg needed).

Security: the text is untrusted. ``edge_tts.Communicate`` XML-escapes it (``&``, ``<``, ``>``)
before putting it into its SSML envelope, so ``<speak>``/``<voice>``/``<prosody>`` in a reply
are read as text. The voice name (config, not user input) is additionally validated against a
strict pattern, and rate/volume are built here from ints, never from strings.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from collections.abc import Callable

from luna.audio.base import AudioClip
from luna.core.errors import ProviderRequestError, ProviderUnavailable
from luna.tts.base import TTSProvider

log = logging.getLogger(__name__)

MAX_TEXT_CHARS = 20_000
MAX_MP3_BYTES = 32 * 1024 * 1024
DECODE_SAMPLE_RATE = 24_000  # native rate of the service's MP3
VOICE_PATTERN = re.compile(r"^[a-z]{2,3}-[A-Z]{2,4}-[A-Za-z0-9]+Neural$")

# (text, voice, rate "+0%", volume "+0%", timeout seconds) -> MP3 bytes. Faked in unit tests.
Fetcher = Callable[[str, str, str, str, float], bytes]
# MP3 bytes -> AudioClip. Faked in unit tests.
Decoder = Callable[[bytes], AudioClip]


def rate_to_percent(rate: int) -> str:
    """Map the SAPI-style ``rate`` -10..10 to edge-tts: 5 % per step, so -50 %..+50 %."""
    return f"{max(-10, min(10, int(rate))) * 5:+d}%"


def volume_to_percent(volume: int) -> str:
    """Map ``volume`` 0..100 (100 = full) to an edge-tts offset: 100 -> +0 %, 50 -> -50 %."""
    return f"{max(0, min(100, int(volume))) - 100:+d}%"


def _fetch_mp3(text: str, voice: str, rate: str, volume: str, timeout_s: float) -> bytes:
    import edge_tts  # lazy: keeps `import luna.tts.edge` cheap

    async def run() -> bytes:
        comm = edge_tts.Communicate(
            text, voice, rate=rate, volume=volume, connect_timeout=5, receive_timeout=15
        )
        buf = bytearray()
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                buf += chunk["data"]
                if len(buf) > MAX_MP3_BYTES:
                    raise ProviderUnavailable("Edge TTS output is unreasonably large")
        return bytes(buf)

    async def bounded() -> bytes:
        return await asyncio.wait_for(run(), timeout_s)

    return asyncio.run(bounded())


def _decode_mp3(data: bytes) -> AudioClip:
    import miniaudio

    decoded = miniaudio.decode(
        data,
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=1,
        sample_rate=DECODE_SAMPLE_RATE,
    )
    return AudioClip(pcm=decoded.samples.tobytes(), sample_rate=decoded.sample_rate)


class EdgeTTS:
    name = "edge"

    def __init__(
        self,
        voice_ru: str = "ru-RU-SvetlanaNeural",
        voice_en: str = "en-US-JennyNeural",
        rate: int = 0,
        volume: int = 100,
        *,
        timeout_s: float = 15.0,
        fetcher: Fetcher | None = None,
        decoder: Decoder | None = None,
    ) -> None:
        for voice in (voice_ru, voice_en):
            if not VOICE_PATTERN.match(voice):
                raise ValueError(f"not an Edge neural voice name: {voice!r}")
        self._voices = {"ru": voice_ru, "en": voice_en}
        self._rate = rate_to_percent(rate)
        self._volume = volume_to_percent(volume)
        self._timeout_s = timeout_s
        self._fetch = fetcher or _fetch_mp3
        self._decode = decoder or _decode_mp3
        if fetcher is None and decoder is None:
            self._preload()

    @staticmethod
    def _preload() -> None:
        """Import the heavy libraries now (at startup) instead of during the first reply."""
        try:
            import edge_tts  # noqa: F401
            import miniaudio  # noqa: F401
        except ImportError:  # reported as ProviderUnavailable on the first synthesis
            log.debug("edge-tts or miniaudio is not importable", exc_info=True)

    def synthesize(self, text: str, lang: str) -> AudioClip:
        if not text or not text.strip():
            raise ProviderRequestError("nothing to speak: empty text")
        if len(text) > MAX_TEXT_CHARS:
            raise ProviderRequestError(f"text too long to speak ({len(text)} > {MAX_TEXT_CHARS})")
        voice = self._voices.get(lang.strip().lower())
        if voice is None:
            raise ProviderRequestError(f"unsupported language: {lang!r}")
        # Lone surrogates cannot be sent as UTF-8; the rest is passed on (and escaped) as is.
        clean = text.encode("utf-8", "replace").decode("utf-8")
        try:
            mp3 = self._fetch(clean, voice, self._rate, self._volume, self._timeout_s)
        except ProviderUnavailable:
            raise
        except TimeoutError as e:  # asyncio.TimeoutError is TimeoutError on 3.11+
            raise ProviderUnavailable(f"Edge TTS timed out after {self._timeout_s}s") from e
        except Exception as e:  # aiohttp / edge_tts / OS errors: all mean "service unusable"
            raise ProviderUnavailable(f"Edge TTS request failed ({type(e).__name__})") from e
        if not mp3:
            raise ProviderUnavailable("Edge TTS returned no audio")
        try:
            clip = self._decode(mp3)
        except Exception as e:
            raise ProviderUnavailable(f"cannot decode Edge TTS audio ({type(e).__name__})") from e
        if not clip.pcm:
            raise ProviderUnavailable("Edge TTS returned empty audio")
        return clip


class FallbackTTS:
    """Use ``primary``; when it is unavailable, speak with ``fallback`` and skip the primary for
    ``cooldown_s`` so that every sentence does not wait for a failing network call.

    One warning is logged per outage (not per sentence). ``ProviderRequestError`` (input the
    engine cannot speak) is not a service failure: it propagates and starts no cooldown.
    """

    def __init__(
        self,
        primary: TTSProvider,
        fallback: TTSProvider,
        *,
        cooldown_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._lock = threading.Lock()
        self._down_until: float | None = None
        self.name = f"{primary.name}+{fallback.name}"

    def synthesize(self, text: str, lang: str) -> AudioClip:
        with self._lock:
            skip_primary = self._down_until is not None and self._clock() < self._down_until
        if not skip_primary:
            try:
                clip: AudioClip = self._primary.synthesize(text, lang)
            except ProviderUnavailable as e:
                with self._lock:
                    first_failure = self._down_until is None
                    self._down_until = self._clock() + self._cooldown_s
                if first_failure:
                    log.warning(
                        "%s unavailable (%s); speaking with %s and retrying in %.0f s",
                        self._primary.name,
                        e,
                        self._fallback.name,
                        self._cooldown_s,
                    )
            else:
                with self._lock:
                    recovered = self._down_until is not None
                    self._down_until = None
                if recovered:
                    log.info("%s is back", self._primary.name)
                return clip
        return self._fallback.synthesize(text, lang)
