"""Interruptible Windows playback on top of PortAudio (``sounddevice``).

``winsound`` cannot do this: ``SND_MEMORY | SND_ASYNC`` raises ``RuntimeError``, and
``SND_PURGE`` / ``PlaySound(None)`` do not interrupt a synchronous in-memory sound (checked
on Windows 10 / Python 3.13). Here the clip is written to a raw output stream in short
chunks, so ``stop()`` is noticed within one chunk and the stream is aborted.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from typing import Any, Protocol

from luna.audio.base import SAMPLE_WIDTH_BYTES, AudioClip

CHUNK_S = 0.05


class RawOutput(Protocol):
    """The subset of ``sounddevice.RawOutputStream`` that is used."""

    def start(self) -> None: ...
    def write(self, data: bytes) -> Any: ...
    def stop(self) -> None: ...  # waits until queued audio has been played
    def abort(self) -> None: ...  # drops queued audio immediately
    def close(self) -> None: ...


StreamFactory = Callable[[int], RawOutput]


def _sounddevice_stream(sample_rate: int) -> RawOutput:
    import sounddevice as sd  # lazy: optional `voice` extra, needs PortAudio

    return sd.RawOutputStream(samplerate=sample_rate, channels=1, dtype="int16")


class SoundDevicePlayer:
    def __init__(self, stream_factory: StreamFactory = _sounddevice_stream) -> None:
        self._factory = stream_factory
        self._play_lock = threading.Lock()  # one clip at a time
        self._state_lock = threading.Lock()
        self._stop: threading.Event | None = None
        self._stream: RawOutput | None = None
        self._closed = False

    def play(self, clip: AudioClip) -> None:
        if not clip.pcm:
            return
        with self._play_lock:
            stop = threading.Event()
            stream = self._factory(clip.sample_rate)
            with self._state_lock:
                if self._closed:
                    stream.close()
                    return
                self._stop, self._stream = stop, stream
            try:
                stream.start()
                step = max(SAMPLE_WIDTH_BYTES, int(clip.sample_rate * CHUNK_S) * SAMPLE_WIDTH_BYTES)
                for i in range(0, len(clip.pcm), step):
                    if stop.is_set():
                        break
                    stream.write(clip.pcm[i : i + step])
                if stop.is_set():
                    stream.abort()
                else:
                    stream.stop()
            finally:
                with self._state_lock:
                    self._stop = self._stream = None
                stream.close()

    def stop(self) -> None:
        with self._state_lock:
            stop, stream = self._stop, self._stream
        if stop is None or stream is None:
            return
        stop.set()
        # stop() must never raise; play() cleans up. abort() unblocks a pending write().
        with contextlib.suppress(Exception):
            stream.abort()

    def close(self) -> None:
        with self._state_lock:
            self._closed = True
        self.stop()
