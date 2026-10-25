"""Records one utterance from the default mic (manual: ``uv run pytest -m audio -s``).

With nobody speaking it ends with ``no_speech`` after about 5 s.
"""

import threading

import pytest

from luna.audio.capture import SoundDeviceMic, check_audio_input
from luna.audio.recorder import Recorder
from luna.audio.vad import EnergyVAD
from luna.config.settings import VoiceSettings

pytestmark = pytest.mark.audio


def test_records_one_utterance_from_default_mic():
    reason = check_audio_input()
    if reason:
        pytest.skip(reason)
    conf = VoiceSettings()
    with SoundDeviceMic(frame_ms=conf.frame_ms, preroll_ms=conf.preroll_ms) as mic:
        vad = EnergyVAD(conf.vad_energy_threshold, conf.vad_noise_margin, conf.frame_ms)
        u = Recorder(mic, vad, conf).record(threading.Event())
    print(f"reason={u.reason} duration={u.duration_s:.2f}s bytes={len(u.pcm16)}")
    assert u.reason in {"silence", "max", "no_speech"}
