"""VoskKeywordSpotter with an injected fake engine: no vosk import, no model."""

import json
import sys
from pathlib import Path

import pytest

from luna.core.errors import ConfigError, ProviderUnavailable
from luna.wakeword.base import WakeEvent, WakeWordDetector
from luna.wakeword.vosk_spotter import VoskKeywordSpotter, _VoskEngine

FRAME = b"\x00\x00" * 480  # 30 ms
SILENCE = [(False, "{}")] * 15  # 450 ms of nothing recognized


def partial(*words):
    """Partial result with word timings: words are (word, end_seconds) pairs."""
    items = [{"word": w, "start": max(0.0, e - 0.4), "end": e} for w, e in words]
    return json.dumps({"partial": " ".join(w for w, _ in words), "partial_result": items})


def final(*words):
    """Final result: words are (word, end_seconds, conf) triples."""
    items = [{"word": w, "start": e - 0.4, "end": e, "conf": c} for w, e, c in words]
    return json.dumps({"text": " ".join(w for w, _, _ in words), "result": items})


class FakeRecognizer:
    def __init__(self, script):
        self.script = list(script)  # one (accepted, json) per frame
        self._current = (False, "{}")

    def AcceptWaveform(self, data):
        self._current = self.script.pop(0) if self.script else (False, "{}")
        return self._current[0]

    def Result(self):
        return self._current[1]

    def PartialResult(self):
        return self._current[1]


class FakeEngine:
    def __init__(self, script=(), vocabulary=("луна",)):
        self.script = script
        self.vocabulary = set(vocabulary)
        self.created = []  # (grammar, rate)
        self.closed = False

    def has_word(self, word):
        return word in self.vocabulary

    def create(self, grammar, sample_rate):
        self.created.append((grammar, sample_rate))
        # A fresh recognizer replays the script from the start (as after reset()).
        return FakeRecognizer(self.script)

    def close(self):
        self.closed = True


def make(script=(), *, keywords=("луна",), threshold=0.5, **engine_kwargs):
    engine = FakeEngine(script, **engine_kwargs)
    spotter = VoskKeywordSpotter(
        "model-dir", keywords, threshold=threshold, engine_factory=lambda _path: engine
    )
    return spotter, engine


def feed(spotter, count):
    return [spotter.process(FRAME) for _ in range(count)]


def test_satisfies_protocol_and_frame_geometry():
    spotter, _ = make()
    assert isinstance(spotter, WakeWordDetector)
    assert (spotter.name, spotter.sample_rate, spotter.frame_samples) == ("vosk", 16_000, 480)


def test_grammar_is_keywords_plus_unk():
    _, engine = make(keywords=("Луна", " луна ", "селена"), vocabulary=("луна", "селена"))
    assert engine.created == [(json.dumps(["луна", "селена", "[unk]"], ensure_ascii=False), 16_000)]


def test_partial_keyword_is_reported_once_with_end_sample():
    script = [*SILENCE, (False, partial(("луна", 0.45))), (False, partial(("луна", 0.45)))]
    events = feed(make(script)[0], 20)
    assert events[:15] == [None] * 15
    assert events[15] == WakeEvent("луна", None, round(0.45 * 16_000))
    assert events[16:] == [None] * 4


def test_second_keyword_in_same_utterance_is_reported_when_count_grows():
    script = [
        *SILENCE,
        (False, partial(("луна", 0.4))),
        *[(False, partial(("луна", 0.4)))] * 40,
        (False, partial(("луна", 0.4), ("луна", 1.2))),
    ]
    events = [e for e in feed(make(script)[0], 15 + 1 + 40 + 1) if e]
    assert [e.end_sample for e in events] == [round(0.4 * 16_000), round(1.2 * 16_000)]


def test_final_result_reports_keyword_missed_by_partials_with_confidence():
    script = [*SILENCE, (True, final(("луна", 0.4, 0.9)))]
    events = feed(make(script)[0], 16)
    assert events[15] == WakeEvent("луна", 0.9, 6_400)


def test_final_after_partial_does_not_report_twice_then_next_utterance_does():
    script = [
        (False, partial(("луна", 0.4))),
        (True, final(("луна", 0.4, 0.9))),
        (False, partial(("луна", 0.3))),
    ]
    events = feed(make(script)[0], 3)
    assert events[0] is not None and events[1] is None and events[2] is not None


def test_low_confidence_final_is_rejected():
    script = [(True, final(("луна", 0.5, 0.49)))]
    assert feed(make(script, threshold=0.5)[0], 1) == [None]


def test_confidence_at_threshold_is_accepted():
    script = [(True, final(("луна", 0.5, 0.5)))]
    assert feed(make(script, threshold=0.5)[0], 1)[0].confidence == 0.5


def test_unknown_and_other_words_are_ignored():
    script = [(False, partial(("[unk]", 0.4))), (True, final(("[unk]", 0.9, 1.0)))]
    assert feed(make(script)[0], 2) == [None, None]


def test_no_word_timings_falls_back_to_fed_minus_250ms():
    script = [(False, "{}"), (False, "{}"), (False, json.dumps({"partial": "луна"}))]
    event = feed(make(script)[0], 3)[2]
    assert event == WakeEvent("луна", None, 0)  # 3 frames = 1440 samples < 250 ms: clamped to 0


def test_no_word_timings_fallback_on_longer_stream():
    script = [(False, "{}")] * 19 + [(False, json.dumps({"partial": "луна"}))]
    event = feed(make(script)[0], 20)[19]
    assert event.end_sample == 20 * 480 - 4_000


def test_end_sample_is_clamped_to_samples_fed():
    script = [(False, partial(("луна", 99.0)))]
    assert feed(make(script)[0], 1)[0].end_sample == 480


def test_garbage_json_is_ignored():
    script = [(False, "not json"), (True, "[]"), (False, '{"partial_result": 5}')]
    assert feed(make(script)[0], 3) == [None, None, None]


def test_wrong_frame_size_raises_and_does_not_advance():
    spotter, _ = make([(False, partial(("луна", 0.01)))])
    for bad in (b"", b"\x00" * 3, FRAME + b"\x00\x00"):
        with pytest.raises(ValueError, match="expected 960 bytes"):
            spotter.process(bad)
    assert spotter.process(FRAME).end_sample == 160  # clamped to fed; counter untouched by errors


def test_reset_builds_a_fresh_recognizer_and_restarts_counters():
    script = [(False, partial(("луна", 0.01)))]
    spotter, engine = make(script)
    assert feed(spotter, 1)[0] is not None
    spotter.reset()
    assert len(engine.created) == 2
    event = feed(spotter, 1)[0]  # same word is reported again after reset
    assert event is not None and event.end_sample == 160


def test_close_releases_engine_and_blocks_use():
    spotter, engine = make()
    spotter.close()
    spotter.close()  # idempotent
    assert engine.closed
    with pytest.raises(RuntimeError):
        spotter.process(FRAME)
    with pytest.raises(RuntimeError):
        spotter.reset()


def test_keyword_missing_from_vocabulary_fails_at_construction():
    engine = FakeEngine(vocabulary=())
    with pytest.raises(ConfigError, match="vocabulary"):
        VoskKeywordSpotter("m", ["луна"], threshold=0.5, engine_factory=lambda _p: engine)
    assert engine.closed


@pytest.mark.parametrize(
    "kwargs",
    [
        {"keywords": []},
        {"keywords": [" "]},
        {"threshold": 1.5},
        {"sample_rate": 8_000},
    ],
)
def test_invalid_arguments(kwargs):
    args = {"keywords": ["луна"], "threshold": 0.5, **kwargs}
    with pytest.raises(ConfigError):
        VoskKeywordSpotter(
            "m",
            args.pop("keywords"),
            engine_factory=lambda _p: FakeEngine(),
            **args,
        )


def test_missing_vosk_is_provider_unavailable_with_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "vosk", None)  # makes `import vosk` raise ImportError
    with pytest.raises(ProviderUnavailable, match="uv sync"):
        VoskKeywordSpotter("m", ["луна"], threshold=0.5)


def test_native_load_failure_is_provider_unavailable(monkeypatch):
    class Broken:
        @staticmethod
        def SetLogLevel(level):
            pass

        @staticmethod
        def Model(**_kw):
            raise Exception("bad model files")

    monkeypatch.setitem(sys.modules, "vosk", Broken)
    with pytest.raises(ProviderUnavailable, match="cannot load the wake-word model"):
        _VoskEngine(Path("x"))


def test_real_engine_wiring_with_stubbed_vosk(monkeypatch):
    calls = []

    class FakeRec:
        def __init__(self, model, rate, grammar):
            calls.append(("rec", rate, grammar))

        def SetWords(self, flag):
            calls.append(("words", flag))

        def SetPartialWords(self, flag):
            calls.append(("partial_words", flag))

    class FakeModel:
        def __init__(self, model_path):
            calls.append(("model", model_path))

        def vosk_model_find_word(self, word):
            return 7 if word == "луна" else -1

    class Vosk:
        Model = FakeModel
        KaldiRecognizer = FakeRec

        @staticmethod
        def SetLogLevel(level):
            calls.append(("log", level))

    monkeypatch.setitem(sys.modules, "vosk", Vosk)
    spotter = VoskKeywordSpotter("some/dir", ["луна"], threshold=0.5)
    assert calls[0] == ("log", -1)  # silenced before the model is loaded
    assert ("model", str(Path("some/dir"))) in calls
    assert ("words", True) in calls and ("partial_words", True) in calls
    spotter.close()
    with pytest.raises(ConfigError, match="vocabulary"):
        VoskKeywordSpotter("some/dir", ["марс"], threshold=0.5)
