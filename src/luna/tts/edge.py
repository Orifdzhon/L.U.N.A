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
from luna.core.errors import NothingToSpeak, ProviderRequestError, ProviderUnavailable
from luna.tts.base import TTSProvider
from luna.tts.text import is_speakable

log = logging.getLogger(__name__)

MAX_TEXT_CHARS = 20_000
MAX_MP3_BYTES = 32 * 1024 * 1024
DECODE_SAMPLE_RATE = 24_000  # native rate of the service's MP3
VOICE_PATTERN = re.compile(r"^[a-z]{2,3}-[A-Z]{2,4}-[A-Za-z0-9]+Neural$")

FIRST_AUDIO_TIMEOUT_S = 5.0  # no audio byte after this long: the exchange is stalled
SECONDS_PER_CHAR = 0.05  # total timeout = first-audio timeout + this per character
ATTEMPTS = 2  # one immediate retry of the same chunk

# (text, voice, rate "+0%", volume "+0%", first-audio timeout s, total timeout s) -> MP3 bytes.
# Faked in unit tests.
Fetcher = Callable[[str, str, str, str, float, float], bytes]
# MP3 bytes -> AudioClip. Faked in unit tests.
Decoder = Callable[[bytes], AudioClip]


def rate_to_percent(rate: int) -> str:
    """Map the SAPI-style ``rate`` -10..10 to edge-tts: 5 % per step, so -50 %..+50 %."""
    return f"{max(-10, min(10, int(rate))) * 5:+d}%"


def volume_to_percent(volume: int) -> str:
    """Map ``volume`` 0..100 (100 = full) to an edge-tts offset: 100 -> +0 %, 50 -> -50 %."""
    return f"{max(0, min(100, int(volume))) - 100:+d}%"


def total_timeout(text: str, first_audio_s: float, cap_s: float) -> float:
    """Whole-exchange timeout scaled to the text length, never above ``cap_s``."""
    return min(cap_s, first_audio_s + SECONDS_PER_CHAR * len(text))


def _fetch_mp3(
    text: str, voice: str, rate: str, volume: str, first_audio_s: float, total_s: float
) -> bytes:
    import edge_tts  # lazy: keeps `import luna.tts.edge` cheap

    async def run() -> bytes:
        comm = edge_tts.Communicate(
            text,
            voice,
            rate=rate,
            volume=volume,
            connect_timeout=min(5, first_audio_s, total_s),
            receive_timeout=total_s,
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + total_s
        first_deadline = loop.time() + min(first_audio_s, total_s)
        buf = bytearray()
        stream = comm.stream().__aiter__()
        try:
            while True:
                # Until the first audio byte arrives the short first-audio limit applies.
                limit = (deadline if buf else min(deadline, first_deadline)) - loop.time()
                try:
                    chunk = await asyncio.wait_for(stream.__anext__(), max(0.0, limit))
                except StopAsyncIteration:
                    break
                if chunk["type"] == "audio":
                    buf += chunk["data"]
                    if len(buf) > MAX_MP3_BYTES:
                        raise ProviderUnavailable("Edge TTS output is unreasonably large")
        finally:
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                await aclose()
        return bytes(buf)

    return asyncio.run(run())


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
        first_audio_timeout_s: float = FIRST_AUDIO_TIMEOUT_S,
        fetcher: Fetcher | None = None,
        decoder: Decoder | None = None,
    ) -> None:
        for voice in (voice_ru, voice_en):
            if not VOICE_PATTERN.match(voice):
                raise ValueError(f"not an Edge neural voice name: {voice!r}")
        self._voices = {"ru": voice_ru, "en": voice_en}
        self._rate = rate_to_percent(rate)
        self._volume = volume_to_percent(volume)
        self._timeout_s = timeout_s  # upper bound of the length-scaled total timeout
        self._first_audio_s = first_audio_timeout_s
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

    def _fetch_with_retry(self, text: str, voice: str, total_s: float) -> bytes:
        """Fetch the MP3; a stalled or failed exchange is retried at once (the service drops
        about one request in ten with NoAudioReceived or a slow start)."""
        last: ProviderUnavailable | None = None
        for attempt in range(1, ATTEMPTS + 1):
            try:
                mp3 = self._fetch(
                    text, voice, self._rate, self._volume, self._first_audio_s, total_s
                )
            except ProviderUnavailable as e:
                last = e
            except TimeoutError as e:  # asyncio.TimeoutError is TimeoutError on 3.11+
                last = ProviderUnavailable(
                    f"Edge TTS timed out (first audio {self._first_audio_s}s, total {total_s:.0f}s)"
                )
                last.__cause__ = e
            except Exception as e:  # aiohttp / edge_tts / OS errors: "service unusable"
                last = ProviderUnavailable(f"Edge TTS request failed ({type(e).__name__})")
                last.__cause__ = e
            else:
                if mp3:
                    return mp3
                last = ProviderUnavailable("Edge TTS returned no audio")
            if attempt < ATTEMPTS:
                log.debug("Edge TTS attempt %d failed (%s); retrying", attempt, last)
        assert last is not None
        raise last

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
        if not is_speakable(clean):
            # Edge answers NoAudioReceived after ~1 s for "...", "—" or Hangul on a RU voice.
            raise NothingToSpeak("no character the Edge voice can pronounce")
        total_s = total_timeout(clean, self._first_audio_s, self._timeout_s)
        mp3 = self._fetch_with_retry(clean, voice, total_s)
        try:
            clip = self._decode(mp3)
        except Exception as e:
            raise ProviderUnavailable(f"cannot decode Edge TTS audio ({type(e).__name__})") from e
        if not clip.pcm:
            raise ProviderUnavailable("Edge TTS returned empty audio")
        return clip


class FallbackTTS:
    """Use ``primary``; when it fails for a chunk, speak that chunk with ``fallback``.

    The next chunk tries ``primary`` again, so one glitch does not make the rest of the reply
    robotic. After ``failure_threshold`` failures in a row the primary is skipped for
    ``cooldown_s`` (a real outage must not make every sentence wait for a failing call); the
    first success resets the count.

    Each failure is logged (the primary is not called during the backoff, so an outage logs
    once per probe). ``ProviderRequestError`` (input the engine cannot speak, including
    ``NothingToSpeak``) is not a service failure: it propagates and counts for nothing.
    """

    def __init__(
        self,
        primary: TTSProvider,
        fallback: TTSProvider,
        *,
        cooldown_s: float = 15.0,
        failure_threshold: int = 2,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._cooldown_s = cooldown_s
        self._threshold = max(1, failure_threshold)
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
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
                    self._failures += 1
                    backoff = self._failures >= self._threshold
                    if backoff:
                        self._down_until = self._clock() + self._cooldown_s
                log.warning(
                    "%s unavailable (%s); speaking this chunk with %s%s",
                    self._primary.name,
                    e,
                    self._fallback.name,
                    f", retrying in {self._cooldown_s:.0f} s" if backoff else "",
                )
            else:
                with self._lock:
                    recovered = self._failures > 0
                    self._failures = 0
                    self._down_until = None
                if recovered:
                    log.info("%s is back", self._primary.name)
                return clip
        return self._fallback.synthesize(text, lang)
