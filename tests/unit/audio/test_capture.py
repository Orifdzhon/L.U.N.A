import threading
import time
import types

import pytest

from fakes import FakeMic
from luna.audio.capture import (
    MicSource,
    RingBuffer,
    SoundDeviceMic,
    check_audio_input,
    frame_bytes,
    list_input_devices,
)
from luna.core.errors import AudioError

DEVICES = [
    {"name": "Speakers", "max_input_channels": 0},
    {"name": "Microphone (USB Audio)", "max_input_channels": 1},
    {"name": "Headset Mic", "max_input_channels": 2},
]


class FakeStream:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.events: list[str] = []
        self.fail_start: Exception | None = None

    def start(self) -> None:
        if self.fail_start:
            raise self.fail_start
        self.events.append("start")

    def abort(self) -> None:
        self.events.append("abort")

    def close(self) -> None:
        self.events.append("close")


class FakeSD:
    def __init__(self, devices=DEVICES, fail_start: Exception | None = None) -> None:
        self.devices = devices
        self.fail_start = fail_start
        self.streams: list[FakeStream] = []
        self.default = types.SimpleNamespace(device=(2, 0))

    def query_devices(self, kind=None):
        if kind == "input" and not any(d["max_input_channels"] for d in self.devices):
            raise ValueError("No input device")
        return self.devices

    def RawInputStream(self, **kwargs):
        stream = FakeStream(**kwargs)
        stream.fail_start = self.fail_start
        self.streams.append(stream)
        return stream


def make_mic(sd: FakeSD | None = None, **kwargs) -> tuple[SoundDeviceMic, FakeSD]:
    sd = sd or FakeSD()
    return SoundDeviceMic(loader=lambda: sd, **kwargs), sd


def feed(sd: FakeSD, data: bytes) -> None:
    sd.streams[-1].kwargs["callback"](data, 0, None, None)


def test_ring_buffer_keeps_last_ms_oldest_first():
    ring = RingBuffer(duration_ms=100)  # 3200 bytes
    for i in range(10):
        ring.append(bytes([i]) * 960)  # 30 ms each
    snap = ring.snapshot()
    assert len(snap) == ring.capacity == 3200
    assert snap.endswith(bytes([9]) * 960)
    assert snap.startswith(bytes([6]))
    ring.clear()
    assert ring.snapshot() == b"" and len(ring) == 0


def test_ring_buffer_zero_capacity_and_small_input():
    zero = RingBuffer(0)
    zero.append(b"\x01\x00")
    assert zero.snapshot() == b""
    ring = RingBuffer(100)
    ring.append(b"\x01\x00")
    assert ring.snapshot() == b"\x01\x00"


def test_opens_16k_mono_int16_and_returns_exact_frames():
    mic, sd = make_mic(frame_ms=20)
    mic.start()
    kw = sd.streams[0].kwargs
    assert (kw["samplerate"], kw["channels"], kw["dtype"]) == (16000, 1, "int16")
    assert kw["blocksize"] == 320 and kw["device"] == 2  # sd default device
    n = frame_bytes(20)
    assert n == 640
    feed(sd, b"\x01\x00" * 320 + b"\x02\x00" * 100)  # 1 frame + partial
    feed(sd, b"\x02\x00" * 220)
    first, second = mic.read(0.1), mic.read(0.1)
    assert len(first) == len(second) == n
    assert first[:2] == b"\x01\x00" and second[:2] == b"\x02\x00"
    assert mic.read(0.01) is None
    assert mic.preroll.snapshot() == first + second


def test_not_opened_before_start():
    mic, sd = make_mic()
    assert sd.streams == [] and not mic.is_open
    assert mic.read(0.01) is None


def test_callback_never_blocks_drops_oldest_and_counts():
    mic, sd = make_mic(queue_frames=2)
    mic.start()
    n = frame_bytes(30)
    for i in range(5):
        feed(sd, bytes([i]) * n)
    assert mic.dropped_frames == 3
    assert mic.read(0.1)[0] == 3
    assert mic.read(0.1)[0] == 4


def test_stop_closes_stream_idempotent_and_restart_works():
    mic, sd = make_mic()
    mic.stop()  # no-op before start
    mic.start()
    mic.start()  # already open
    assert len(sd.streams) == 1
    feed(sd, b"\x00" * frame_bytes(30))
    mic.stop()
    mic.stop()
    assert sd.streams[0].events == ["start", "abort", "close"]
    assert not mic.is_open
    mic.start()
    assert len(sd.streams) == 2 and mic.is_open
    assert mic.read(0.01) is None  # stale frames from the previous session are gone


def test_stop_unblocks_pending_read_from_another_thread():
    mic, _ = make_mic()
    mic.start()
    result = []
    t = threading.Thread(target=lambda: result.append(mic.read(5.0)))
    t.start()
    time.sleep(0.05)
    started = time.monotonic()
    threading.Thread(target=mic.stop).start()
    t.join(2)
    assert not t.is_alive() and result == [None]
    assert time.monotonic() - started < 1


def test_close_then_start_raises():
    mic, _ = make_mic()
    mic.close()
    with pytest.raises(AudioError, match="closed"):
        mic.start()


def test_missing_backend_error(monkeypatch):
    import builtins

    real = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "sounddevice":
            raise ImportError("no module")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(AudioError, match="not installed"):
        SoundDeviceMic().start()
    assert "not installed" in check_audio_input()


def test_no_input_device_error():
    sd = FakeSD(devices=[DEVICES[0]])
    mic, _ = make_mic(sd)
    with pytest.raises(AudioError, match="no audio input device"):
        mic.start()
    assert "no audio input device" in check_audio_input(lambda: sd)


def test_unknown_device_name_error_lists_available():
    mic, _ = make_mic(device="webcam")
    with pytest.raises(AudioError, match=r"'webcam' not found.*Headset Mic"):
        mic.start()


def test_unknown_device_index_error():
    mic, _ = make_mic(device=0)  # Speakers: no input channels
    with pytest.raises(AudioError, match=r"#0 not found"):
        mic.start()


def test_open_failure_names_device_and_closes():
    mic, sd = make_mic(FakeSD(fail_start=OSError("Device unavailable")), device="usb")
    with pytest.raises(AudioError, match=r"cannot open microphone 'Microphone \(USB Audio\)'"):
        mic.start()
    assert sd.streams[0].events == ["close"] and not mic.is_open


def test_device_by_name_substring_case_insensitive_and_by_index():
    mic, sd = make_mic(device="HEADSET")
    mic.start()
    assert sd.streams[0].kwargs["device"] == 2
    mic2, sd2 = make_mic(device=1)
    mic2.start()
    assert sd2.streams[0].kwargs["device"] == 1
    mic3, sd3 = make_mic(device="1")
    mic3.start()
    assert sd3.streams[0].kwargs["device"] == 1


def test_default_device_falls_back_to_first_input():
    sd = FakeSD()
    sd.default.device = (-1, 0)
    mic, _ = make_mic(sd)
    mic.start()
    assert sd.streams[0].kwargs["device"] == 1


def test_list_input_devices_only_inputs():
    devs = list_input_devices(lambda: FakeSD())
    assert [(d.index, d.name) for d in devs] == [(1, "Microphone (USB Audio)"), (2, "Headset Mic")]


def test_check_audio_input_ok():
    assert check_audio_input(lambda: FakeSD()) is None


def test_fake_mic_conforms_and_scripts_frames():
    mic: MicSource = FakeMic([b"a", b"b"])
    assert mic.read(0.1) is None  # not started
    mic.start()
    assert [mic.read(0.1), mic.read(0.1), mic.read(0.1)] == [b"a", b"b", None]
    mic.stop()
    assert mic.read(0.1) is None
    with pytest.raises(AudioError):
        FakeMic(error=AudioError("boom")).start()
    failing = FakeMic(read_error=AudioError("lost"))
    failing.start()
    with pytest.raises(AudioError):
        failing.read(0.1)
