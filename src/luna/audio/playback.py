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
from luna.core.errors import AudioError

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


def _abort(stream: RawOutput) -> None:
    with contextlib.suppress(Exception):
        stream.abort()


class SoundDevicePlayer:
    def __init__(self, stream_factory: StreamFactory = _sounddevice_stream) -> None:
        self._factory = stream_factory
        self._play_lock = threading.Lock()  # one clip at a time
        self._state_lock = threading.Lock()
        self._stop: threading.Event | None = None  # set only while a play() is in progress
        self._stream: RawOutput | None = None
        self._closed = False

    def play(self, clip: AudioClip) -> None:
        if not clip.pcm:
            return
        with self._play_lock:
            # Registered before the device is opened, so a stop() during open is not lost.
            stop = threading.Event()
            with self._state_lock:
                if self._closed:
                    return
                self._stop = stop
            stream: RawOutput | None = None
            try:
                try:
                    stream = self._factory(clip.sample_rate)
                except ImportError as e:
                    raise AudioError("audio backend missing: run `uv sync` to install it") from e
                except Exception as e:
                    raise AudioError(f"cannot open audio output: {e}") from e
                with self._state_lock:
                    self._stream = stream
                if stop.is_set():  # stop() arrived while the device was opening
                    _abort(stream)
                    return
                try:
                    stream.start()
                    step = max(
                        SAMPLE_WIDTH_BYTES, int(clip.sample_rate * CHUNK_S) * SAMPLE_WIDTH_BYTES
                    )
                    for i in range(0, len(clip.pcm), step):
                        if stop.is_set():
                            break
                        stream.write(clip.pcm[i : i + step])
                    if stop.is_set():
                        _abort(stream)
                    else:
                        stream.stop()
                except Exception as e:
                    if stop.is_set():  # stop() aborted the stream under our feet
                        return
                    raise AudioError(f"audio playback failed: {e}") from e
            finally:
                with self._state_lock:
                    self._stop = self._stream = None
                if stream is not None:
                    with contextlib.suppress(Exception):  # never mask the original error
                        stream.close()

    def stop(self) -> None:
        with self._state_lock:
            stop, stream = self._stop, self._stream
        if stop is None:
            return
        stop.set()
        if stream is not None:
            _abort(stream)  # unblocks a pending write() and drops buffered audio

    def close(self) -> None:
        with self._state_lock:
            self._closed = True
        self.stop()
