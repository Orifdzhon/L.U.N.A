"""Audio data type and the playback interface (platform-neutral)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

SAMPLE_WIDTH_BYTES = 2  # int16


@dataclass(frozen=True)
class AudioClip:
    """Mono PCM audio, signed 16-bit little-endian samples."""

    pcm: bytes
    sample_rate: int

    def __post_init__(self) -> None:
        if len(self.pcm) % SAMPLE_WIDTH_BYTES:
            raise ValueError("pcm must hold whole int16 samples (even byte length)")
        if self.sample_rate <= 0:
            raise ValueError("sample_rate must be positive")

    @property
    def duration_s(self) -> float:
        return len(self.pcm) / SAMPLE_WIDTH_BYTES / self.sample_rate


class AudioPlayer(Protocol):
    def play(self, clip: AudioClip) -> None:
        """Play ``clip``; block until it finished or ``stop()`` was called."""
        ...

    def stop(self) -> None:
        """Cut the current playback. Thread-safe, idempotent, a no-op when idle."""
        ...

    def close(self) -> None:
        """Release resources; the player must not be used afterwards."""
        ...
