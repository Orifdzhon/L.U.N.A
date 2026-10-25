"""Records ~1 s from the default microphone (manual: ``uv run pytest -m audio``)."""

import array
import time

import pytest

from luna.audio.capture import SoundDeviceMic, check_audio_input, frame_bytes

pytestmark = pytest.mark.audio


def test_records_one_second_from_default_mic():
    reason = check_audio_input()
    if reason:
        pytest.skip(reason)
    mic = SoundDeviceMic()
    frames: list[bytes] = []
    try:
        mic.start()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            frame = mic.read(0.2)
            if frame:
                frames.append(frame)
    finally:
        mic.close()
    pcm = b"".join(frames)
    assert pcm and len(pcm) % 2 == 0
    assert all(len(f) == frame_bytes(30) for f in frames)
    peak = max(abs(s) for s in array.array("h", pcm))
    print(f"frames={len(frames)} peak={peak} dropped={mic.dropped_frames}")
