"""Microphone capture on top of PortAudio (``sounddevice``): 16 kHz mono int16.

The microphone is open only between ``start()`` and ``stop()``: nothing touches the device at
import or construction time. The PortAudio callback only copies frames into a bounded queue
and never blocks; when the consumer is too slow the oldest frame is dropped and counted.
"""

from __future__ import annotations

import contextlib
import queue
import threading
from collections import deque
from collections.abc import Callable
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


class MicSource(Protocol):
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
            default = loader().default.device
            default_index = int(default[0] if isinstance(default, (list, tuple)) else default)
            for dev in devices:
                if dev.index == default_index:
                    return dev
        return devices[0]
    if isinstance(device, int) or device.strip().isdigit():
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


class SoundDeviceMic:
    """``MicSource`` backed by ``sounddevice.RawInputStream``; open only while started."""

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
        self.dropped_frames = 0
        self.preroll = RingBuffer(preroll_ms)
        self._device = device
        self._loader = loader
        self._frame_bytes = frame_bytes(frame_ms)
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=queue_frames)
        self._pending = bytearray()  # touched only by the PortAudio callback thread
        self._lock = threading.Lock()
        self._stream: Any = None
        self._closed = False

    @property
    def is_open(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise AudioError("microphone is closed")
            if self._stream is not None:
                return
            info = resolve_input_device(self._device, self._loader)
            sd = self._loader()
            self._drain()
            self._pending = bytearray()
            self.preroll.clear()
            stream = None
            try:
                stream = sd.RawInputStream(
                    samplerate=SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="int16",
                    blocksize=self._frame_bytes // SAMPLE_WIDTH_BYTES,
                    device=info.index,
                    callback=self._on_audio,
                )
                stream.start()
            except Exception as e:
                if stream is not None:
                    with contextlib.suppress(Exception):
                        stream.close()
                raise AudioError(f"cannot open microphone {info.name!r}: {e}") from e
            self._stream = stream

    def stop(self) -> None:
        with self._lock:
            stream, self._stream = self._stream, None
        if stream is None:
            return
        with contextlib.suppress(Exception):
            stream.abort()
        with contextlib.suppress(Exception):
            stream.close()
        # A sentinel makes a blocked read() return None right away.
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)

    def read(self, timeout_s: float) -> bytes | None:
        if self._stream is None and self._queue.empty():
            return None
        try:
            frame = self._queue.get(timeout=max(0.0, timeout_s))
        except queue.Empty:
            return None
        if frame is not None:
            self.preroll.append(frame)
        return frame

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self.stop()

    def _on_audio(self, indata: Any, frames: int, time: Any, status: Any) -> None:
        # PortAudio thread: copy and queue only, never block.
        self._pending += bytes(indata)
        size = self._frame_bytes
        while len(self._pending) >= size:
            frame = bytes(self._pending[:size])
            del self._pending[:size]
            try:
                self._queue.put_nowait(frame)
            except queue.Full:
                with contextlib.suppress(queue.Empty):
                    self._queue.get_nowait()  # drop the oldest frame
                self.dropped_frames += 1
                with contextlib.suppress(queue.Full):
                    self._queue.put_nowait(frame)

    def _drain(self) -> None:
        with contextlib.suppress(queue.Empty):
            while True:
                self._queue.get_nowait()
