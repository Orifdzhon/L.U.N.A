"""Energy-based voice activity detection with an adaptive noise floor (no dependencies).

Good enough for push-to-talk in a quiet room. A Silero/ONNX detector can replace it later
behind the same ``VoiceActivityDetector`` protocol.
"""

from __future__ import annotations

import array
import math
from typing import Protocol

from luna.audio.base import SAMPLE_WIDTH_BYTES
from luna.audio.capture import DEFAULT_FRAME_MS

FULL_SCALE = 32768.0
WARMUP_MS = 240  # noise floor is estimated from this much audio at the start
ADAPT_RATE = 0.05  # how fast the floor follows non-speech frames
DEFAULT_HANGOVER_MS = 60  # a quiet frame right after speech still counts as speech


class VoiceActivityDetector(Protocol):
    def is_speech(self, frame: bytes) -> bool:
        """True if the PCM16 mono ``frame`` (one frame of audio) contains speech."""
        ...

    def reset(self, noise_floor: float | None = None) -> None:
        """Prepare for a new recording; call before each one.

        None: forget the noise floor and warm up again (push-to-talk). A value: start from
        that floor with no warm-up (a live source whose floor is already known).
        """
        ...


def rms_level(frame: bytes) -> float:
    """RMS of a PCM16 little-endian frame as a fraction of full scale (0..1)."""
    usable = len(frame) - len(frame) % SAMPLE_WIDTH_BYTES
    if usable <= 0:
        return 0.0
    samples = array.array("h")
    samples.frombytes(frame[:usable])
    total = sum(s * s for s in samples)
    return math.sqrt(total / len(samples)) / FULL_SCALE


class EnergyVAD:
    """Speech = RMS above ``max(threshold, noise_floor * margin)``.

    The floor is the quietest frame of the first ``WARMUP_MS`` (so speech that starts during
    warm-up does not poison it) and then follows non-speech frames slowly. During warm-up no
    frame is reported as speech. A constant sound that is present from the very first frame
    (hum, or an endless tone) is therefore treated as background.
    """

    def __init__(
        self,
        threshold: float = 0.003,
        margin: float = 3.0,
        frame_ms: int = DEFAULT_FRAME_MS,
        hangover_ms: int = DEFAULT_HANGOVER_MS,
        warmup_ms: int = WARMUP_MS,
    ) -> None:
        if threshold <= 0 or margin < 1 or frame_ms <= 0:
            raise ValueError("threshold must be > 0, margin >= 1, frame_ms > 0")
        self.threshold = threshold
        self.margin = margin
        self._warmup_frames = max(1, warmup_ms // frame_ms)
        self._hangover_frames = max(0, hangover_ms // frame_ms)
        self.reset()

    @property
    def noise_floor(self) -> float:
        return self._floor

    def reset(self, noise_floor: float | None = None) -> None:
        if noise_floor is not None and noise_floor < 0:
            raise ValueError("noise_floor must not be negative")
        self._hangover = 0
        if noise_floor is None:
            self._seen = 0
            self._floor = 0.0
        else:
            self._seen = self._warmup_frames  # no warm-up: speech may start at once
            self._floor = noise_floor

    def is_speech(self, frame: bytes) -> bool:
        level = rms_level(frame)
        if self._seen < self._warmup_frames:
            self._floor = level if self._seen == 0 else min(self._floor, level)
            self._seen += 1
            return False
        if level > max(self.threshold, self._floor * self.margin):
            self._hangover = self._hangover_frames
            return True
        self._floor += ADAPT_RATE * (level - self._floor)
        if self._hangover > 0:
            self._hangover -= 1
            return True
        return False
