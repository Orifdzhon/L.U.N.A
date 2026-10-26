"""Records one utterance from a microphone: pre-roll, VAD endpointing, max length, cancel.

Time is measured in audio (frames received), so results do not depend on scheduling; the
injectable clock only guards against a microphone that stops delivering frames.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Literal

from luna.audio.base import SAMPLE_WIDTH_BYTES
from luna.audio.capture import SAMPLE_RATE, FrameSource, LiveSource, MicSource
from luna.audio.vad import VoiceActivityDetector
from luna.config.settings import VoiceSettings
from luna.core.clock import Clock
from luna.core.errors import AudioError

Reason = Literal["silence", "max", "stopped", "cancelled", "no_speech"]

NO_SPEECH_S = 5.0  # give up if nobody speaks for this long
STALL_S = 2.0  # the mic delivered no frame for this long (wall clock) -> error
READ_TIMEOUT_S = 0.05  # keeps cancel/stop responsive


@dataclass(frozen=True)
class Utterance:
    """pcm16 is empty for ``no_speech`` and ``cancelled`` (and ``stopped`` before speech)."""

    pcm16: bytes
    sample_rate: int
    duration_s: float
    reason: Reason


class Recorder:
    def __init__(
        self,
        mic: MicSource,
        vad: VoiceActivityDetector,
        conf: VoiceSettings,
        *,
        clock: Clock = time.monotonic,
        no_speech_s: float = NO_SPEECH_S,
        stall_s: float = STALL_S,
    ) -> None:
        self._mic = mic
        self._vad = vad
        self._conf = conf
        self._clock = clock
        self._no_speech_s = no_speech_s
        self._stall_s = stall_s
        self._stop = threading.Event()

    def stop(self) -> None:
        """Finish the current recording and keep what was heard (thread-safe)."""
        self._stop.set()

    def record(
        self,
        cancel: threading.Event,
        *,
        stop: threading.Event | None = None,
        live: LiveSource | None = None,
        no_speech_s: float | None = None,
    ) -> Utterance:
        """Block until the utterance ends. ``cancel`` (any thread) discards the audio.

        ``stop`` is an optional per-call event with the meaning of ``stop()``: unlike the
        shared flag it is not cleared when the call starts, so a stop requested just before
        ``record()`` begins is never lost.

        ``live`` continues from a running stream (wake mode): the Recorder then never opens or
        closes the microphone, reads ``live.frames`` and closes that tap on return. A tap
        closed without error ends the recording as ``cancelled``; closed with an error it
        raises it. The VAD starts from ``live.noise_floor`` without warm-up. ``live.lead_in``
        is prepended only if speech starts within the pre-roll of the cut (so the audio is
        contiguous); it is never fed to the VAD. ``no_speech_s`` overrides the constructor
        value for this call.

        Raises ``AudioError`` if the microphone cannot be opened, fails or goes silent.
        Without ``live`` the microphone is always stopped on return.
        """
        self._stop.clear()
        conf = self._conf
        if live is None:
            self._vad.reset()
        else:
            self._vad.reset(noise_floor=live.noise_floor)
        source: FrameSource = self._mic if live is None else live.frames
        frame_s = conf.frame_ms / 1000
        preroll_frames = conf.preroll_ms // conf.frame_ms
        min_speech_frames = max(1, -(-conf.min_speech_ms // conf.frame_ms))
        silence_limit_s = conf.silence_ms / 1000
        max_s = conf.max_utterance_s
        no_speech_s = min(self._no_speech_s if no_speech_s is None else no_speech_s, max_s)

        history: deque[bytes] = deque(maxlen=preroll_frames)
        candidate: list[bytes] = []
        recorded: list[bytes] = []
        speaking = False
        elapsed = 0.0
        silence_s = 0.0
        frames_seen = 0

        def result(reason: Reason, keep: bool) -> Utterance:
            pcm = b"".join(recorded) if keep and speaking else b""
            return Utterance(pcm, SAMPLE_RATE, len(pcm) / SAMPLE_WIDTH_BYTES / SAMPLE_RATE, reason)

        try:
            if live is None:
                self._mic.start()
            last_frame_at = self._clock()
            while True:
                if cancel.is_set():
                    return result("cancelled", False)
                if self._stop.is_set() or (stop is not None and stop.is_set()):
                    return result("stopped", True)
                frame = source.read(READ_TIMEOUT_S)
                if frame is None:
                    if live is not None and getattr(source, "closed", False):
                        return result("cancelled", False)
                    if self._clock() - last_frame_at > self._stall_s:
                        raise AudioError("microphone delivered no audio")
                    continue
                last_frame_at = self._clock()
                if cancel.is_set():
                    return result("cancelled", False)
                elapsed += frame_s
                frames_seen += 1
                is_speech = self._vad.is_speech(frame)

                if speaking:
                    recorded.append(frame)
                    silence_s = 0.0 if is_speech else silence_s + frame_s
                    if silence_s >= silence_limit_s:
                        return result("silence", True)
                elif is_speech:
                    candidate.append(frame)
                    if len(candidate) >= min_speech_frames:
                        speaking = True
                        # nothing was dropped from the pre-roll -> the lead-in is contiguous
                        contiguous = frames_seen - len(candidate) <= preroll_frames
                        lead_in = live.lead_in if live is not None and contiguous else b""
                        recorded = (
                            [lead_in, *history, *candidate] if lead_in else [*history, *candidate]
                        )
                        candidate = []
                else:
                    history.extend(candidate)  # too short to be speech: a click
                    history.append(frame)
                    candidate = []

                if elapsed >= max_s:
                    return result("max", True) if speaking else result("no_speech", False)
                if not speaking and elapsed >= no_speech_s:
                    return result("no_speech", False)
        finally:
            if live is None:
                self._mic.stop()
            else:
                close = getattr(source, "close", None)
                if close is not None:
                    close()
