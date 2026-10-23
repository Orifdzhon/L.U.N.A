"""Text-to-speech provider interface (platform-neutral).

Playback is separate (``luna.audio.playback``) so that speech can be interrupted.
Failures are reported as ``luna.core.errors.ProviderError`` subclasses.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from luna.audio.base import AudioClip


@runtime_checkable
class TTSProvider(Protocol):
    name: str

    def synthesize(self, text: str, lang: str) -> AudioClip:
        """Synthesize ``text`` (plain text, never markup) in ``lang`` (``"ru"`` / ``"en"``).

        Blocks until the whole clip is ready. Raises ``ProviderUnavailable`` when the engine
        fails or times out and ``ProviderRequestError`` for input it cannot speak.
        """
        ...
