"""Microphone capture on top of PortAudio (``sounddevice``): 16 kHz mono int16.

The microphone is open only between ``start()`` and ``stop()``: nothing touches the device at
import or construction time. The PortAudio callback only copies frames into a bounded queue
and never blocks; when the consumer is too slow the oldest frame is dropped and counted.
"""

from __future__ import annotations

import contextlib
import queue
import threading
import weakref
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from luna.audio.base import SAMPLE_WIDTH_BYTES
from luna.core.errors import AudioError

SAMPLE_RATE = 16000
CHANNELS = 1
DEFAULT_FRAME_MS = 30
DEFAULT_PREROLL_MS = 300
DEFAULT_QUEUE_FRAMES = 200  # ~6 s at 30 ms; bounds memory if the consumer stalls
VOICE_EXTRA_HINT = "run `uv sync` to install the voice extra"

DeviceSpec = int | str | None


def frame_bytes(frame_ms: int, sample_rate: int = SAMPLE_RATE) -> int:
    """Size in bytes of ``frame_ms`` of mono int16 audio."""
    return sample_rate * frame_ms // 1000 * SAMPLE_WIDTH_BYTES


class FrameSource(Protocol):
    """Anything a ``Recorder`` can read frames from (a microphone or a ``FrameTap``)."""

    def read(self, timeout_s: float) -> bytes | None:
        """One frame of ``frame_ms`` (PCM16, mono, 16 kHz); None on timeout or when stopped."""
        ...


class MicSource(FrameSource, Protocol):
    def start(self) -> None:
        """Open the microphone. Raises ``AudioError`` if it cannot be opened."""
        ...

    def stop(self) -> None:
        """Close the microphone. Thread-safe, idempotent, unblocks a pending ``read()``."""
        ...

    def read(self, timeout_s: float) -> bytes | None:
        """One frame of ``frame_ms`` (PCM16, mono, 16 kHz); None on timeout or when stopped."""
        ...

    def close(self) -> None:
        """Release resources; the source must not be used afterwards."""
        ...


class FrameTap:
    """A ``FrameSource`` fed by another thread (the wake listener): backlog, then live frames.

    ``push()`` never blocks: when ``max_frames`` are already queued the oldest one is dropped
    and counted in ``dropped`` (a stalled reader cannot grow memory). ``close()`` discards the
    queued audio and wakes a blocked ``read()``; after it ``read()`` returns None, or raises
    the ``error`` given to ``close()`` once (the microphone was lost). Thread-safe.
    """

    def __init__(self, backlog: Iterable[bytes] = (), max_frames: int = 400) -> None:
        if max_frames <= 0:
            raise ValueError("max_frames must be positive")
        self.max_frames = max_frames
        self._dropped = 0
        self._frames: deque[bytes] = deque()
        self._cond = threading.Condition()
        self._closed = False
        self._error: AudioError | None = None
        for frame in backlog:
            self._append(frame)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def dropped(self) -> int:
        with self._cond:
            return self._dropped

    def _append(self, frame: bytes) -> None:
        if len(self._frames) >= self.max_frames:
            self._frames.popleft()
            self._dropped += 1
        self._frames.append(frame)

    def push(self, frame: bytes) -> None:
        with self._cond:
            if self._closed:
                return
            self._append(frame)
            self._cond.notify()

    def read(self, timeout_s: float) -> bytes | None:
        with self._cond:
            self._cond.wait_for(lambda: self._frames or self._closed, max(0.0, timeout_s))
            if self._frames:
                return self._frames.popleft()
            if self._error is not None:
                error, self._error = self._error, None
                raise error
            return None

    def close(self, error: AudioError | None = None) -> None:
        with self._cond:
            if not self._closed:
                self._closed = True
                self._error = error
                self._frames.clear()
            self._cond.notify_all()


@dataclass(frozen=True)
class LiveSource:
    """Continue a recording from a live stream instead of opening the microphone."""

    frames: FrameSource  # usually a FrameTap; the Recorder never starts/stops a microphone
    lead_in: bytes = b""  # audio before the cut point (keyword + context)
    noise_floor: float | None = None  # the listener's VAD floor; seeds the Recorder's VAD


class RingBuffer:
    """Bounded byte buffer that keeps only the last ``duration_ms`` of audio (pre-roll)."""

    def __init__(self, duration_ms: int, sample_rate: int = SAMPLE_RATE) -> None:
        if duration_ms < 0:
            raise ValueError("duration_ms must not be negative")
        self.capacity = frame_bytes(duration_ms, sample_rate)
        self._chunks: deque[bytes] = deque()
        self._size = 0
        self._lock = threading.Lock()

    def append(self, data: bytes) -> None:
        with self._lock:
            self._chunks.append(data)
            self._size += len(data)
            while self._size > self.capacity and self._chunks:
                excess = self._size - self.capacity
                head = self._chunks[0]
                if len(head) <= excess:
                    self._chunks.popleft()
                    self._size -= len(head)
                else:  # trim on a sample boundary
                    cut = min(len(head), excess + excess % SAMPLE_WIDTH_BYTES)
                    self._chunks[0] = head[cut:]
                    self._size -= cut

    def snapshot(self) -> bytes:
        """The buffered audio, oldest first."""
        with self._lock:
            return b"".join(self._chunks)

    def clear(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._size = 0

    def __len__(self) -> int:
        with self._lock:
            return self._size


@dataclass(frozen=True)
class InputDevice:
    index: int
    name: str
    channels: int


def _load_sounddevice() -> Any:
    try:
        import sounddevice as sd  # lazy: optional `voice` extra, needs PortAudio
    except ImportError as e:
        raise AudioError(f"audio backend is not installed; {VOICE_EXTRA_HINT}") from e
    except Exception as e:  # e.g. OSError: PortAudio library not found
        raise AudioError(
            f"audio backend failed to load ({type(e).__name__}); {VOICE_EXTRA_HINT}"
        ) from e
    return sd


Loader = Callable[[], Any]


def list_input_devices(loader: Loader = _load_sounddevice) -> list[InputDevice]:
    """All devices that can record. Raises ``AudioError`` if the backend is unusable."""
    sd = loader()
    try:
        devices = list(sd.query_devices())
    except Exception as e:
        raise AudioError(f"cannot list audio devices: {e}") from e
    found = []
    for index, dev in enumerate(devices):
        channels = int(dev.get("max_input_channels", 0))
        if channels > 0:
            found.append(InputDevice(index, str(dev.get("name", "")), channels))
    return found


def resolve_input_device(device: DeviceSpec, loader: Loader = _load_sounddevice) -> InputDevice:
    """Pick the device by index, case-insensitive name substring, or the default one."""
    devices = list_input_devices(loader)
    if not devices:
        raise AudioError("no audio input device (microphone) found")
    if device is None or (isinstance(device, str) and not device.strip()):
        with contextlib.suppress(Exception):
            default = loader().default.device  # sd's (input, output) pair, not a tuple
            try:
                default_index = int(default[0])
            except TypeError:
                default_index = int(default)
            for dev in devices:
                if dev.index == default_index:
                    return dev
        return devices[0]
    if isinstance(device, int) or (device.strip().isascii() and device.strip().isdigit()):
        wanted = int(device)
        for dev in devices:
            if dev.index == wanted:
                return dev
        raise AudioError(f"audio input device #{wanted} not found or has no input channels")
    needle = device.strip().lower()
    for dev in devices:
        if needle in dev.name.lower():
            return dev
    names = ", ".join(f"#{d.index} {d.name}" for d in devices)
    raise AudioError(f"audio input device {device!r} not found; available: {names}")


def check_audio_input(loader: Loader = _load_sounddevice) -> str | None:
    """Reason why audio input is unusable, or None. Imports ``sounddevice`` lazily."""
    try:
        sd = loader()
    except AudioError as e:
        return str(e)
    try:
        sd.query_devices(kind="input")
    except Exception:
        return "no audio input device (microphone) found"
    return None


class _Session:
    """State of one start()..stop() period; the PortAudio callbacks are bound to it only.

    It deliberately does not reference the mic, so a mic that is dropped without ``stop()``
    can be garbage-collected (and its stream closed by the finalizer).
    """

    def __init__(self, frame_bytes_: int, queue_frames: int) -> None:
        self.frame_bytes = frame_bytes_
        self.queue: queue.Queue[bytes | None] = queue.Queue(maxsize=queue_frames)
        self.pending = bytearray()  # touched only by the PortAudio callback thread
        self.dropped = 0
        self.ended = False  # set by stop(): the end of the stream is expected
        self.lost = False  # the stream ended on its own (device unplugged / failed)

    def on_audio(self, indata: Any, frames: int, time: Any, status: Any) -> None:
        # PortAudio thread: copy and queue only, never block.
        self.pending += bytes(indata)
        size = self.frame_bytes
        while len(self.pending) >= size:
            frame = bytes(self.pending[:size])
            del self.pending[:size]
            try:
                self.queue.put_nowait(frame)
            except queue.Full:
                with contextlib.suppress(queue.Empty):
                    self.queue.get_nowait()  # drop the oldest frame
                self.dropped += 1
                with contextlib.suppress(queue.Full):
                    self.queue.put_nowait(frame)

    def on_finished(self) -> None:
        if not self.ended:
            self.lost = True
            self.wake()

    def wake(self) -> None:
        # A sentinel makes a blocked read() return right away.
        with contextlib.suppress(queue.Full):
            self.queue.put_nowait(None)

    def clear(self) -> None:
        self.pending = bytearray()
        with contextlib.suppress(queue.Empty):
            while True:
                self.queue.get_nowait()


def _release(holder: dict[str, Any]) -> None:
    """Abort and close the stream in ``holder`` (idempotent; used by stop() and finalizer)."""
    stream = holder.pop("stream", None)
    if stream is None:
        return
    with contextlib.suppress(Exception):
        stream.abort()
    with contextlib.suppress(Exception):
        stream.close()


class SoundDeviceMic:
    """``MicSource`` backed by ``sounddevice.RawInputStream``; open only while started.

    Use it as a context manager (``with SoundDeviceMic() as mic:``) or call ``close()``. As a
    safety net a ``weakref.finalize`` closes a still-open stream when the object is garbage
    collected or the process exits, so the microphone is never left on.
    """

    def __init__(
        self,
        device: DeviceSpec = None,
        frame_ms: int = DEFAULT_FRAME_MS,
        preroll_ms: int = DEFAULT_PREROLL_MS,
        queue_frames: int = DEFAULT_QUEUE_FRAMES,
        loader: Loader = _load_sounddevice,
    ) -> None:
        if frame_ms <= 0 or queue_frames <= 0:
            raise ValueError("frame_ms and queue_frames must be positive")
        self.frame_ms = frame_ms
        self.preroll = RingBuffer(preroll_ms)
        self._device = device
        self._loader = loader
        self._frame_bytes = frame_bytes(frame_ms)
        self._queue_frames = queue_frames
        self._dropped_before = 0  # dropped frames of finished sessions
        self._lock = threading.Lock()
        self._holder: dict[str, Any] = {}
        self._session: _Session | None = None
        self._stream: Any = None
        self._closed = False
        weakref.finalize(self, _release, self._holder)

    @property
    def is_open(self) -> bool:
        return self._stream is not None

    @property
    def dropped_frames(self) -> int:
        session = self._session
        return self._dropped_before + (session.dropped if session else 0)

    def __enter__(self) -> SoundDeviceMic:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise AudioError("microphone is closed")
            if self._stream is not None:
                return
            info = resolve_input_device(self._device, self._loader)
            sd = self._loader()
            if self._session is not None:
                self._dropped_before += self._session.dropped
            session = _Session(self._frame_bytes, self._queue_frames)  # fresh queue per session
            self.preroll.clear()
            stream = None
            try:
                stream = sd.RawInputStream(
                    samplerate=SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="int16",
                    blocksize=self._frame_bytes // SAMPLE_WIDTH_BYTES,
                    device=info.index,
                    callback=session.on_audio,
                    finished_callback=session.on_finished,
                )
                stream.start()
            except BaseException as e:  # incl. KeyboardInterrupt: never leave the device open
                if stream is not None:
                    with contextlib.suppress(Exception):
                        stream.close()
                if isinstance(e, Exception):
                    raise AudioError(f"cannot open microphone {info.name!r}: {e}") from e
                raise
            self._session = session
            self._stream = stream
            self._holder["stream"] = stream

    def stop(self) -> None:
        with self._lock:
            session, self._stream = self._session, None
            if session is not None:
                session.ended = True
        _release(self._holder)
        if session is not None:
            session.wake()

    def read(self, timeout_s: float) -> bytes | None:
        session = self._session
        if session is None:
            return None
        self._raise_if_lost(session)
        if self._stream is None and session.queue.empty():
            return None
        try:
            frame = session.queue.get(timeout=max(0.0, timeout_s))
        except queue.Empty:
            self._raise_if_lost(session)
            return None
        if frame is None:
            self._raise_if_lost(session)
            return None
        self.preroll.append(frame)
        return frame

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self.stop()
        session = self._session
        if session is not None:
            session.clear()  # no raw audio stays in memory
        self.preroll.clear()

    def _raise_if_lost(self, session: _Session) -> None:
        stream = self._stream
        if stream is None:
            return
        if session.lost or not getattr(stream, "active", True):
            raise AudioError("microphone stopped unexpectedly (device unplugged or in use?)")
