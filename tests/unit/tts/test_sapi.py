import base64
import io
import json
import logging
import struct
import subprocess
import wave

import pytest

from fakes import FakeTTS
from luna.audio.base import AudioClip
from luna.core.errors import ProviderRequestError, ProviderUnavailable
from luna.tts import sapi
from luna.tts.base import TTSProvider
from luna.tts.sapi import SapiTTS, VoiceInfo, select_voice

VOICES = [
    {"name": "Microsoft Irina Desktop", "culture": "ru-RU"},
    {"name": "Microsoft Zira Desktop", "culture": "en-US"},
    {"name": "Microsoft David Desktop", "culture": "en-US"},
]


def make_wav(samples=(0, 1000, -1000, 5), rate=22050, channels=1, width=2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", s) for s in samples))
    return buf.getvalue()


class FakeRunner:
    """Stands in for the PowerShell subprocess: answers the voice query and the synthesis."""

    def __init__(self, voices=VOICES, wav=None, returncode=0, stderr=b"", raises=None):
        self.voices = voices
        self.wav = make_wav() if wav is None else wav
        self.returncode = returncode
        self.stderr = stderr
        self.raises = raises
        self.calls: list[tuple[list[str], bytes, float]] = []

    def __call__(self, argv, stdin, timeout_s):
        self.calls.append((list(argv), stdin, timeout_s))
        if self.raises is not None:
            raise self.raises
        script = base64.b64decode(argv[-1]).decode("utf-16-le")
        out = json.dumps(self.voices).encode() if "GetInstalledVoices" in script else self.wav
        return subprocess.CompletedProcess(argv, self.returncode, out, self.stderr)

    def speak_requests(self):
        return [json.loads(stdin) for argv, stdin, _ in self.calls if stdin]


def tts(runner=None, **kw):
    return SapiTTS(runner=runner or FakeRunner(), **kw)


def test_conforms_to_protocol_and_name():
    assert isinstance(tts(), TTSProvider)
    assert isinstance(FakeTTS(), TTSProvider)
    assert tts().name == "sapi"


def test_list_voices_and_caching():
    runner = FakeRunner()
    t = tts(runner)
    voices = t.list_voices()
    assert voices[0] == VoiceInfo("Microsoft Irina Desktop", "ru-RU")
    assert len(voices) == 3
    t.list_voices()
    assert len(runner.calls) == 1


def test_list_voices_single_object_json():
    t = tts(FakeRunner(voices=VOICES[0]))
    assert t.list_voices() == (VoiceInfo("Microsoft Irina Desktop", "ru-RU"),)


def test_list_voices_garbage_is_unavailable():
    class Bad(FakeRunner):
        def __call__(self, argv, stdin, timeout_s):
            return subprocess.CompletedProcess(argv, 0, b"not json", b"")

    with pytest.raises(ProviderUnavailable):
        tts(Bad()).list_voices()


def test_synthesize_returns_clip():
    runner = FakeRunner(wav=make_wav([1, 2, 3, 4], rate=22050))
    clip = tts(runner).synthesize("Привет", "ru")
    assert isinstance(clip, AudioClip)
    assert clip.sample_rate == 22050
    assert clip.pcm == struct.pack("<4h", 1, 2, 3, 4)


def test_voice_selection_ru_and_en():
    runner = FakeRunner()
    t = tts(runner)
    t.synthesize("Привет", "ru")
    t.synthesize("Hello", "en")
    ru, en = runner.speak_requests()
    assert ru["voice"] == "Microsoft Irina Desktop"
    assert en["voice"] == "Microsoft Zira Desktop"


def test_voice_selection_is_case_insensitive_and_follows_settings():
    runner = FakeRunner()
    tts(runner, voice_en="DAVID").synthesize("Hello", "EN")
    assert runner.speak_requests()[0]["voice"] == "Microsoft David Desktop"


def test_select_voice_fallback_order():
    voices = [VoiceInfo("Foo", "de-DE"), VoiceInfo("Bar Voice", "en-GB")]
    assert select_voice(voices, "bar", "en") == voices[1]  # substring
    assert select_voice(voices, "Zira", "en") == voices[1]  # culture of the language
    assert select_voice(voices, "Irina", "ru") is None  # system default
    assert select_voice([], "x", "en") is None


def test_default_voice_fallback_warns_with_installed_list(caplog):
    runner = FakeRunner(voices=[{"name": "Microsoft Zira Desktop", "culture": "en-US"}])
    with caplog.at_level(logging.WARNING, logger="luna.tts.sapi"):
        tts(runner).synthesize("Привет", "ru")
    assert runner.speak_requests()[0]["voice"] is None
    assert "Zira" in caplog.text
    assert "default" in caplog.text


def test_culture_fallback_does_not_warn(caplog):
    runner = FakeRunner(voices=[{"name": "Some Russian", "culture": "ru-RU"}])
    with caplog.at_level(logging.WARNING, logger="luna.tts.sapi"):
        tts(runner).synthesize("Привет", "ru")
    assert runner.speak_requests()[0]["voice"] == "Some Russian"
    assert not caplog.records


def test_rate_and_volume_applied_and_clamped():
    runner = FakeRunner()
    tts(runner, rate=-4, volume=70).synthesize("Hi", "en")
    req = runner.speak_requests()[0]
    assert (req["rate"], req["volume"]) == (-4, 70)
    runner2 = FakeRunner()
    tts(runner2, rate=99, volume=500).synthesize("Hi", "en")
    req2 = runner2.speak_requests()[0]
    assert (req2["rate"], req2["volume"]) == (10, 100)


def test_timeout_is_passed_and_mapped():
    runner = FakeRunner()
    tts(runner, timeout_s=7.5).synthesize("Hi", "en")
    assert all(call[2] == 7.5 for call in runner.calls)
    expired = FakeRunner(raises=subprocess.TimeoutExpired("powershell", 7.5))
    with pytest.raises(ProviderUnavailable, match="timed out"):
        tts(expired).synthesize("Hi", "en")


def test_process_failure_is_mapped_and_stderr_truncated():
    runner = FakeRunner(returncode=1, stderr=b"x" * 5000)
    with pytest.raises(ProviderUnavailable) as exc:
        tts(runner).list_voices()
    assert len(str(exc.value)) < 500


def test_cannot_start_powershell_is_mapped():
    with pytest.raises(ProviderUnavailable):
        tts(FakeRunner(raises=FileNotFoundError("powershell.exe"))).synthesize("Hi", "en")


@pytest.mark.parametrize("wav", [b"", b"garbage", make_wav([]), make_wav(width=1)])
def test_bad_audio_is_mapped(wav):
    # make_wav(width=1) packs int16 samples into 8-bit frames: still a 8-bit WAV header.
    with pytest.raises(ProviderUnavailable):
        tts(FakeRunner(wav=wav)).synthesize("Hi", "en")


def test_stereo_is_reduced_to_mono():
    clip = tts(FakeRunner(wav=make_wav([1, 100, 2, 200], channels=2))).synthesize("Hi", "en")
    assert clip.pcm == struct.pack("<2h", 1, 2)


@pytest.mark.parametrize("text", ["", "   \n\t"])
def test_empty_text_rejected_without_running_anything(text):
    runner = FakeRunner()
    with pytest.raises(ProviderRequestError):
        tts(runner).synthesize(text, "en")
    assert runner.calls == []


def test_unsupported_language_and_too_long_text_rejected():
    runner = FakeRunner()
    with pytest.raises(ProviderRequestError):
        tts(runner).synthesize("Hallo", "de")
    with pytest.raises(ProviderRequestError):
        tts(runner).synthesize("a" * (sapi.MAX_TEXT_CHARS + 1), "en")
    assert runner.calls == []


HOSTILE = [
    "He said \"hi\" and 'bye'",
    "line1\nline2\r\n\ttab",
    "$(Get-Process | Stop-Process); `calc`; $env:USERNAME; ${x}",
    "'; Remove-Item -Recurse C:\\ ; '",
    '"; calc.exe; "',
    "<speak version='1.0'><voice name='x'>hi</voice><audio src='http://evil'/></speak>",
    "<?xml version='1.0'?><!DOCTYPE a [<!ENTITY e SYSTEM 'file:///c:/x'>]>&e;",
    "| calc & calc || calc > out.txt",
    "Привет, мир \u2028 \U0001f600 \x00 end",
    "x" * 15_000,
]


@pytest.mark.parametrize("text", HOSTILE)
def test_hostile_text_travels_only_as_json_data_on_stdin(text):
    runner = FakeRunner()
    tts(runner).synthesize(text, "en")
    speak_argv, stdin, _ = runner.calls[-1]
    assert json.loads(stdin)["text"] == text  # passed unmodified
    stdin.decode("ascii")  # pure ASCII document
    # The command line (script) is a constant: it never contains any part of the text.
    script = base64.b64decode(speak_argv[-1]).decode("utf-16-le")
    assert script == sapi._SPEAK_SCRIPT
    for part in ("Remove-Item", "calc", "<speak", "Привет"):
        assert part not in " ".join(speak_argv) + script
    assert not any(a == text or text in a for a in speak_argv)


def test_script_uses_plain_text_api_only():
    assert "SpeakSsml" not in sapi._SPEAK_SCRIPT
    assert "SpeakAsync" not in sapi._SPEAK_SCRIPT
    assert "Invoke-Expression" not in sapi._SPEAK_SCRIPT
    assert "$s.Speak([string]$req.text)" in sapi._SPEAK_SCRIPT


def test_lone_surrogate_does_not_crash():
    runner = FakeRunner()
    tts(runner).synthesize("a\ud800b", "en")
    assert json.loads(runner.calls[-1][1])["text"] == "a?b"


def test_default_argv_is_fixed_and_uses_full_powershell_path():
    argv = sapi._argv("x")
    assert argv[0].lower().endswith("powershell.exe") and "\\" in argv[0]
    assert argv[1:4] == ["-NoProfile", "-NonInteractive", "-EncodedCommand"]
    assert "-ExecutionPolicy" not in argv


def test_non_windows_default_runner_is_unavailable(monkeypatch):
    monkeypatch.setattr(sapi.sys, "platform", "linux")
    with pytest.raises(ProviderUnavailable, match="Windows"):
        SapiTTS().synthesize("Hi", "en")


def test_fake_tts_records_and_raises():
    f = FakeTTS()
    assert f.synthesize("a", "ru").sample_rate == 16000
    assert f.calls[0].lang == "ru"
    with pytest.raises(ProviderUnavailable):
        FakeTTS(error=ProviderUnavailable("x")).synthesize("a", "ru")


class FailingListRunner(FakeRunner):
    def __call__(self, argv, stdin, timeout_s):
        script = base64.b64decode(argv[-1]).decode("utf-16-le")
        if "GetInstalledVoices" in script:
            self.calls.append((list(argv), stdin, timeout_s))
            raise subprocess.TimeoutExpired("powershell", timeout_s)
        return super().__call__(argv, stdin, timeout_s)


def test_voice_list_failure_uses_default_voice_and_is_cached(caplog):
    now = [0.0]
    runner = FailingListRunner()
    t = tts(runner, clock=lambda: now[0])
    with caplog.at_level(logging.WARNING, logger="luna.tts.sapi"):
        assert t.synthesize("Hi", "en").pcm
        assert t.synthesize("Hi again", "en").pcm
    assert [r["voice"] for r in runner.speak_requests()] == [None, None]
    assert (
        sum(
            "GetInstalledVoices" in base64.b64decode(c[0][-1]).decode("utf-16-le")
            for c in runner.calls
        )
        == 1
    )
    assert len([r for r in caplog.records if "Cannot list" in r.message]) == 1
    now[0] = sapi.VOICE_RETRY_S + 1  # retried after the back-off
    t.synthesize("Hi", "en")
    assert (
        sum(
            "GetInstalledVoices" in base64.b64decode(c[0][-1]).decode("utf-16-le")
            for c in runner.calls
        )
        == 2
    )


def test_oversized_output_is_rejected(monkeypatch):
    monkeypatch.setattr(sapi, "MAX_OUTPUT_BYTES", 10)
    with pytest.raises(ProviderUnavailable, match="large"):
        tts(FakeRunner(wav=make_wav([1] * 50))).synthesize("Hi", "en")


def test_zero_sample_rate_and_odd_length_are_mapped():
    zero_rate = bytearray(make_wav())
    zero_rate[24:28] = b"\x00\x00\x00\x00"
    with pytest.raises(ProviderUnavailable):
        tts(FakeRunner(wav=bytes(zero_rate))).synthesize("Hi", "en")
    odd = bytearray(make_wav([1, 2]))
    odd = bytes(odd[:-1])
    with pytest.raises(ProviderUnavailable):
        tts(FakeRunner(wav=odd)).synthesize("Hi", "en")


def test_default_runner_uses_minimal_environment(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setenv("GROQ_API_KEY", "secret")
    monkeypatch.setattr(sapi.subprocess, "run", fake_run)
    sapi._default_runner(["x"], b"", 1.0)
    assert "GROQ_API_KEY" not in seen["env"]
    assert "PATH" not in seen["env"]
