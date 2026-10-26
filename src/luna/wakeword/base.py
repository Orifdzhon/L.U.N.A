"""Wake-word detector interface (ARCHITECTURE section 3, ADR 0004).

Frame-based and engine-neutral: a Vosk keyword spotter and an ONNX (openWakeWord-style)
detector both fit behind it.

Audio contract: mono PCM16 little-endian at ``WAKE_SAMPLE_RATE`` (16 kHz), the same frames
the ``MicSource`` produces. Each ``process()`` call takes one frame of ``frame_samples``
samples (``frame_samples * 2`` bytes). The frame length is the detector's choice, not the
mic's: the caller re-chunks if needed.

Thread-safety: a detector is *not* thread-safe. It is owned by one thread (the audio
consumer) that calls ``process()`` / ``reset()``; ``close()`` must be called from that
thread or after it stopped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

WAKE_SAMPLE_RATE = 16_000
WAKE_SAMPLE_WIDTH_BYTES = 2


@dataclass(frozen=True)
class WakeEvent:
    """The keyword was heard.

    ``end_sample`` is the index (counted from the first sample passed to ``process()`` after
    construction or the last ``reset()``) of the sample right after the keyword. The caller
    keeps the audio from that offset on, so a command spoken in the same breath ("Луна,
    открой браузер") is not lost (D11). ``None`` if the engine cannot tell; the caller then
    uses the end of the current frame.
    """

    keyword: str
    confidence: float | None = None  # 0..1 when the engine reports it
    end_sample: int | None = None

    def __post_init__(self) -> None:
        if not self.keyword:
            raise ValueError("keyword must not be empty")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be within 0..1")
        if self.end_sample is not None and self.end_sample < 0:
            raise ValueError("end_sample must be >= 0")

    @property
    def end_s(self) -> float | None:
        """``end_sample`` in seconds of the 16 kHz stream."""
        return None if self.end_sample is None else self.end_sample / WAKE_SAMPLE_RATE


@runtime_checkable
class WakeWordDetector(Protocol):
    name: str
    sample_rate: int  # always WAKE_SAMPLE_RATE for now
    frame_samples: int  # expected samples per frame (frame bytes = frame_samples * 2)

    def process(self, frame: bytes) -> WakeEvent | None:
        """Feed one PCM16 mono frame; return a ``WakeEvent`` when the keyword completed.

        Must not block on I/O. After an event the detector keeps working (the caller decides
        whether to ``reset()``). Raises ``ValueError`` for a frame of the wrong size.
        """
        ...

    def reset(self) -> None:
        """Drop internal audio state and restart the sample counter; call after each wake."""
        ...

    def close(self) -> None:
        """Release engine resources; the detector must not be used afterwards."""
        ...
