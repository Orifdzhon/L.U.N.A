"""Wake verification: keyword_first, verify_window, FasterWhisperVerifier (#127)."""

import struct
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from fakes import FakeVerifier
from luna.core.errors import ProviderUnavailable
from luna.stt.faster_whisper import FasterWhisperSTT
from luna.wakeword.verify import (
    VERIFY_AFTER_MS,
    VERIFY_LEAD_MS,
    FasterWhisperVerifier,
    PassThroughVerifier,
    VerifyResult,
    WakeVerifier,
    keyword_first,
    verify_window,
)

WORDS = ["луна", "luna"]
PCM = struct.pack("<2h", 1000, -1000) * 8000  # 1 s
MODEL_DIR = Path(tempfile.gettempdir()) / "luna-test-models"


@pytest.mark.parametrize("text", ["Луна, открой ютуб", "луна.", "Luna", "ЛУНА!", " ...Луна"])
def test_keyword_first_accepts(text):
    assert keyword_first(text, WORDS)


@pytest.mark.parametrize(
    "text", ["лунный свет", "Лена", "она", "Луначарский", "Луна-парк", "на луну", "", "   ", "..."]
)
def test_keyword_first_rejects(text):
    assert not keyword_first(text, WORDS)


INFLECTED = ["луна", "луну", "луне", "луны", "luna"]


LATIN_A = chr(0x61)  # Latin a, looks like Cyrillic a
LATIN_Y = chr(0x79)  # Latin y, looks like Cyrillic u


@pytest.mark.parametrize(
    "text",
    [
        "Луну, открой",
        "луне",
        "Лун" + LATIN_A + ", привет",
        "л" + LATIN_Y + "на открой",
        "Лун" + LATIN_A,
        "Luna",
    ],
)
def test_keyword_first_accepts_inflections_and_homoglyphs(text):
    assert keyword_first(text, INFLECTED)


@pytest.mark.parametrize(
    "text", ["лунный", "Лена", "она", "Луной", "lyna", "лун" + LATIN_A + "-парк"]
)
def test_keyword_first_still_rejects_look_alikes(text):
    assert not keyword_first(text, INFLECTED)


def test_verify_lead_is_one_second():
    assert VERIFY_LEAD_MS == 1000


def test_keyword_first_yo_and_custom_words():
    assert keyword_first("Ёлка, привет", ["елка"])
    assert not keyword_first("Луна", ["сириус"])


def test_verify_window_exact_lengths_no_padding():
    lead = bytes(range(256)) * 200  # 51200 bytes, more than 700 ms
    after = [b"\x01\x02" * 160] * 100  # 1 s in 10 ms frames
    window = verify_window(lead, after)
    per_ms = 32  # bytes per millisecond at 16 kHz int16
    assert len(window) // 2 == (VERIFY_LEAD_MS + VERIFY_AFTER_MS) * 16
    assert window[: VERIFY_LEAD_MS * per_ms] == lead[-VERIFY_LEAD_MS * per_ms :]


def test_verify_window_short_inputs_are_not_padded():
    assert len(verify_window(b"\x00\x01" * 100, [b"\x02\x03" * 50])) == 300
    assert verify_window(b"", []) == b""


def test_verify_window_custom_lengths():
    window = verify_window(bytes(32000), [bytes(32000)], lead_ms=100, after_ms=50)
    assert len(window) == (100 + 50) * 32


class Model:
    def __init__(self, text=" Луна, открой", error=None):
        self.text = text
        self.error = error
        self.calls = []

    def transcribe(self, audio, **kwargs):
        self.calls.append((audio, kwargs))
        if self.error:
            raise self.error
        segs = (
            [SimpleNamespace(text=self.text, avg_logprob=-0.2, no_speech_prob=0.01)]
            if self.text
            else []
        )
        return iter(segs), SimpleNamespace(language="ru", language_probability=0.9)


def make(model, **kw):
    loads = []

    def factory(*a, **k):
        loads.append(a)
        return model

    stt = FasterWhisperSTT(model_dir=MODEL_DIR, model_factory=factory)
    return FasterWhisperVerifier(stt, WORDS, **kw), stt, loads


def test_verify_accepts_and_passes_decode_params():
    model = Model()
    verifier, _, _ = make(model)
    result = verifier.verify(PCM)
    assert (result.accepted, result.reason) == (True, "keyword")
    assert result.text == "Луна, открой"
    _, kwargs = model.calls[0]
    assert kwargs["language"] == "ru"
    assert kwargs["max_new_tokens"] == 12
    assert kwargs["beam_size"] == 1
    assert kwargs["without_timestamps"] is True
    assert kwargs["vad_filter"] is False
    assert kwargs["condition_on_previous_text"] is False
    assert "initial_prompt" not in kwargs and "hotwords" not in kwargs


def test_verify_rejects_other_word_and_empty():
    verifier, _, _ = make(Model(" Лена, привет"))
    result = verifier.verify(PCM)
    assert (result.accepted, result.reason) == (False, "no_keyword")
    empty, _, _ = make(Model(""))
    result = empty.verify(PCM)
    assert (result.accepted, result.reason) == (False, "empty")


def test_verify_reports_latency_from_clock():
    ticks = iter([10.0, 10.4])
    verifier, _, _ = make(Model(), clock=lambda: next(ticks))
    assert verifier.verify(PCM).latency_s == pytest.approx(0.4)


def test_decode_error_fails_open_by_default():
    verifier, _, _ = make(Model(error=RuntimeError("boom")))
    result = verifier.verify(PCM)
    assert (result.accepted, result.reason) == (True, "error")


def test_decode_error_fail_closed_when_requested():
    verifier, _, _ = make(Model(error=RuntimeError("boom")), fail_open=False)
    result = verifier.verify(PCM)
    assert (result.accepted, result.reason) == (False, "error")


def test_shares_stt_model_without_second_load():
    verifier, stt, loads = make(Model())
    verifier.warm_up()
    stt.transcribe(PCM, lang_hint="ru", timeout_s=5)
    verifier.verify(PCM)
    assert len(loads) == 1


def test_warm_up_loads_once_and_decodes_silence_once():
    model = Model(text="")
    verifier, stt, loads = make(model)
    verifier.warm_up()
    assert len(loads) == 1 and stt.is_loaded
    assert len(model.calls) == 1
    audio, _ = model.calls[0]
    assert len(audio) == 8000 and not audio.any()


def test_warm_up_raises_when_model_cannot_load():
    def factory(*a, **k):
        raise OSError("no")

    stt = FasterWhisperSTT(model_dir=MODEL_DIR, model_factory=factory)
    with pytest.raises(ProviderUnavailable):
        FasterWhisperVerifier(stt, WORDS).warm_up()


def test_protocol_satisfied_and_close_is_noop():
    verifier, _, _ = make(Model())
    verifier.close()
    for impl in (verifier, PassThroughVerifier(), FakeVerifier()):
        assert isinstance(impl, WakeVerifier)


def test_pass_through():
    verifier = PassThroughVerifier("no model")
    verifier.warm_up()
    result = verifier.verify(b"")
    assert (result.accepted, result.reason) == (True, "off")
    assert verifier.name == "off"


def test_fake_verifier_script():
    fake = FakeVerifier([VerifyResult(False, "no_keyword"), VerifyResult(True, "keyword")])
    assert fake.verify(b"a").accepted is False
    assert fake.verify(b"b").accepted is True
    assert fake.verify(b"c").accepted is True
    assert fake.calls == [b"a", b"b", b"c"]
    with pytest.raises(RuntimeError):
        FakeVerifier(error=RuntimeError("x")).verify(b"")
