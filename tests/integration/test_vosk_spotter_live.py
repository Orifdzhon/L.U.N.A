"""Opt-in live check of VoskKeywordSpotter with the real model (``pytest -m live``).

Needs the model (``WakeModelStore().ensure()`` downloads ~45 MB on first run; skipped if that
fails). The speech sample is synthesized with the Windows Russian voice (Microsoft Irina) when
available; without it only the silence check runs.
"""

import base64
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from luna.core.errors import ProviderUnavailable
from luna.voice.wake_model import WakeModelStore
from luna.wakeword.vosk_spotter import VoskKeywordSpotter

pytestmark = pytest.mark.live

FRAME_BYTES = 480 * 2


@pytest.fixture(scope="module")
def model_path() -> Path:
    pytest.importorskip("vosk")
    try:
        return WakeModelStore().ensure()
    except ProviderUnavailable as exc:
        pytest.skip(f"wake-word model unavailable: {exc}")


def _pcm_frames(pcm: bytes):
    pcm += b"\x00" * (-len(pcm) % FRAME_BYTES)
    return [pcm[i : i + FRAME_BYTES] for i in range(0, len(pcm), FRAME_BYTES)]


def _synthesize(text: str, out: Path) -> bytes:
    if sys.platform != "win32":
        pytest.skip("speech synthesis sample needs Windows SAPI")
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "try { $s.SelectVoice('Microsoft Irina Desktop') } catch { exit 3 };"
        "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo("
        "16000,[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,"
        "[System.Speech.AudioFormat.AudioChannel]::Mono);"
        f"$s.SetOutputToWaveFile('{out}', $f); $s.Speak('{text}'); $s.Dispose()"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    done = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True,
        timeout=60,
        check=False,
    )
    if done.returncode != 0:
        pytest.skip("Russian SAPI voice (Microsoft Irina) is not installed")
    with wave.open(str(out), "rb") as wav:
        assert wav.getframerate() == 16_000 and wav.getnchannels() == 1
        return wav.readframes(wav.getnframes())


def _run(spotter: VoskKeywordSpotter, pcm: bytes):
    events = []
    for frame in _pcm_frames(pcm):
        event = spotter.process(frame)
        if event is not None:
            events.append(event)
    return events


def test_silence_does_not_wake(model_path):
    spotter = VoskKeywordSpotter(model_path, ["луна"], threshold=0.5)
    try:
        assert _run(spotter, b"\x00" * 16_000 * 2 * 5) == []
    finally:
        spotter.close()


def test_spoken_keyword_wakes_and_end_sample_is_inside_the_audio(model_path, tmp_path):
    pcm = _synthesize("Луна, открой ютуб", tmp_path / "luna.wav")
    spotter = VoskKeywordSpotter(model_path, ["луна"], threshold=0.5)
    try:
        events = _run(spotter, pcm + b"\x00" * 16_000 * 2)
    finally:
        spotter.close()
    assert events, "keyword 'луна' was not detected"
    assert events[0].keyword == "луна"
    # "Луна" is the first word: its end is early in the clip, long before "открой ютуб" ends.
    assert 0 < events[0].end_sample < 1.5 * 16_000 < len(pcm) // 2


@pytest.mark.xfail(
    reason="known risk (ADR 0004): the restricted grammar false-accepts on some speech with "
    "confidence 1.0; min_word_s=0.30 removed the one synthetic case seen (0.24 s word), but the "
    "real rate and the 'free' mode are benchmarked in #112 / #115",
    strict=False,
)
def test_other_speech_does_not_wake(model_path, tmp_path):
    pcm = _synthesize("Открой браузер и найди погоду", tmp_path / "other.wav")
    spotter = VoskKeywordSpotter(model_path, ["луна"], threshold=0.5)
    try:
        assert _run(spotter, pcm + b"\x00" * 16_000 * 2) == []
    finally:
        spotter.close()
