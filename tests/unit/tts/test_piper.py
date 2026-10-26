# ruff: noqa: RUF001
"""Tests for luna.tts.piper with a fake sherpa engine and a fake model store."""

from __future__ import annotations

import logging
import struct
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from fakes import FakeTTS
from luna.audio.base import AudioClip
from luna.core.errors import NothingToSpeak, ProviderRequestError, ProviderUnavailable
from luna.tts.base import TTSProvider
from luna.tts.edge import FallbackTTS
from luna.tts.piper import PiperTTS, rate_to_speed, volume_to_gain

SAMPLES = [0.0, 0.5, -0.5, 1.0, -1.0]


class FakeEngine:
    def __init__(self, sample_rate=22050, samples=None, delay=0.0):
        self.sample_rate = sample_rate
        self.samples = SAMPLES if samples is None else samples
        self.delay = delay
        self.calls: list[tuple[str, int, float]] = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def generate(self, text, sid=0, speed=1.0):
        with self._lock:
            self.calls.append((text, sid, speed))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            return SimpleNamespace(samples=self.samples, sample_rate=self.sample_rate)
        finally:
            with self._lock:
                self.active -= 1


class FakeStore:
    def __init__(self, error=None, delay=0.0):
        self.error, self.delay = error, delay
        self.ensured: list[str] = []

    def ensure(self, voice):
        self.ensured.append(voice)
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        return Path("models") / voice


class Factory:
    def __init__(self, error=None):
        self.error = error
        self.calls: list[tuple[Path, str, int]] = []
        self.engines: dict[str, FakeEngine] = {}

    def __call__(self, path, spec, threads):
        self.calls.append((path, spec.key, threads))
        if self.error:
            raise self.error
        engine = FakeEngine()
        self.engines[spec.lang] = engine
        return engine


def make(store=None, factory=None, **kw):
    store = store or FakeStore()
    factory = factory or Factory()
    tts = PiperTTS(store=store, engine_factory=factory, **kw)
    return tts, store, factory


def pcm_values(clip: AudioClip) -> list[int]:
    return list(struct.unpack(f"<{len(clip.pcm) // 2}h", clip.pcm))


def test_satisfies_protocol_and_name():
    tts, *_ = make()
    assert isinstance(tts, TTSProvider)
    assert tts.name == "piper"


def test_returns_int16_clip_at_the_model_rate():
    tts, *_ = make()
    clip = tts.synthesize("Привет", "ru")
    assert clip.sample_rate == 22050
    assert pcm_values(clip) == [0, 16383, -16383, 32767, -32767]


def test_loads_each_language_lazily_and_once():
    tts, store, factory = make(num_threads=3)
    assert factory.calls == []  # nothing at construction
    tts.synthesize("Привет", "ru")
    tts.synthesize("Ещё раз", "RU ")
    assert [c[1] for c in factory.calls] == ["ru_RU-irina-medium"]
    tts.synthesize("Hello", "en")
    tts.synthesize("Again", "en")
    assert [c[1] for c in factory.calls] == ["ru_RU-irina-medium", "en_US-kristin-medium"]
    assert store.ensured == ["ru_RU-irina-medium", "en_US-kristin-medium"]
    assert {c[2] for c in factory.calls} == {3}
    assert factory.engines["ru"] is not factory.engines["en"]


def test_concurrent_first_use_loads_once_and_serialises_generate():
    store = FakeStore(delay=0.1)
    factory = Factory()
    tts, *_ = make(store, factory)
    results = []

    def work():
        results.append(tts.synthesize("Привет", "ru"))

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(results) == 4
    assert len(factory.calls) == 1
    assert factory.engines["ru"].max_active == 1


def test_rate_maps_to_speed():
    assert [rate_to_speed(r) for r in (-10, 0, 10, 99, -99)] == [0.5, 1.0, 1.5, 1.5, 0.5]
    tts, _, factory = make(rate=4)
    tts.synthesize("Привет", "ru")
    assert factory.engines["ru"].calls[0][2] == pytest.approx(1.2)


def test_volume_is_a_gain_on_the_samples():
    assert [volume_to_gain(v) for v in (100, 50, 0, -5, 500)] == [1.0, 0.5, 0.0, 0.0, 1.0]
    tts, *_ = make(volume=50)
    assert pcm_values(tts.synthesize("Привет", "ru")) == [0, 8191, -8191, 16383, -16383]
    silent, *_ = make(volume=0)
    assert set(pcm_values(silent.synthesize("Привет", "ru"))) == {0}


def test_samples_beyond_full_scale_are_clipped():
    factory = Factory()
    tts, *_ = make(factory=factory)
    tts.synthesize("a", "en")  # create the engine
    factory.engines["en"].samples = [2.0, -2.0]
    assert pcm_values(tts.synthesize("b", "en")) == [32767, -32768]


def test_text_is_normalised_before_synthesis():
    tts, _, factory = make()
    tts.synthesize("Открой YouTube за 79 764 ₽", "ru")
    spoken = factory.engines["ru"].calls[0][0]
    assert spoken == "Открой ютуб за семьдесят девять тысяч семьсот шестьдесят четыре рубля"


def test_input_validation():
    tts, _, factory = make()
    with pytest.raises(ProviderRequestError, match="empty"):
        tts.synthesize("   ", "ru")
    with pytest.raises(ProviderRequestError, match="too long"):
        tts.synthesize("а" * 20_001, "ru")
    with pytest.raises(ProviderRequestError, match="unsupported language"):
        tts.synthesize("Hallo", "de")
    with pytest.raises(NothingToSpeak):
        tts.synthesize("...", "ru")
    with pytest.raises(NothingToSpeak):
        tts.synthesize("— ♪ —", "en")
    assert factory.calls == []  # never loaded for unspeakable input


def test_unknown_or_mismatched_voice_rejected():
    with pytest.raises(ValueError, match="ru voice"):
        PiperTTS(voice_ru="Irina", store=FakeStore(), engine_factory=Factory())
    with pytest.raises(ValueError, match="en voice"):
        PiperTTS(voice_en="ru_RU-irina-medium", store=FakeStore(), engine_factory=Factory())


def test_engine_failure_is_provider_unavailable():
    class Boom(FakeEngine):
        def generate(self, text, sid=0, speed=1.0):
            raise RuntimeError("onnx exploded")

    tts, *_ = make(factory=lambda *_a: Boom())
    with pytest.raises(ProviderUnavailable, match="synthesis failed"):
        tts.synthesize("Привет", "ru")


def test_empty_audio_is_unavailable():
    tts, *_ = make(factory=lambda *_a: FakeEngine(samples=[]))
    with pytest.raises(ProviderUnavailable, match="no audio"):
        tts.synthesize("Привет", "ru")


# -- warm-up ---------------------------------------------------------------------------------


def test_warm_up_loads_russian_and_generates_once():
    tts, store, factory = make()
    tts.warm_up()
    assert store.ensured == ["ru_RU-irina-medium"]
    assert len(factory.engines["ru"].calls) == 1
    tts.warm_up()  # already warm: nothing more
    assert len(factory.engines["ru"].calls) == 1
    tts.synthesize("Привет", "ru")
    assert len(factory.calls) == 1  # the real call reuses the warmed model


def test_warm_up_never_raises_and_is_retried_later():
    store = FakeStore(error=ProviderUnavailable("no network"))
    tts, *_ = make(store)
    tts.warm_up()  # swallowed
    assert tts._warmed is False


def test_speaker_warmup_hook_exists():
    tts, *_ = make()
    assert callable(tts.warm_up)


# -- failures and fallback -------------------------------------------------------------------


def test_download_failure_raises_unavailable_and_is_not_retried_immediately(caplog):
    store = FakeStore(error=ProviderUnavailable("voice download failed: HTTP 503"))
    now = [100.0]
    tts, _, factory = make(store, retry_after_s=60, clock=lambda: now[0])
    with (
        caplog.at_level(logging.WARNING, logger="luna.tts.piper"),
        pytest.raises(ProviderUnavailable, match="HTTP 503"),
    ):
        tts.synthesize("Привет", "ru")
    for _ in range(3):  # cached failure: no new download attempts
        with pytest.raises(ProviderUnavailable, match="HTTP 503"):
            tts.synthesize("Привет", "ru")
    assert store.ensured == ["ru_RU-irina-medium"]
    assert sum("unavailable" in r.message for r in caplog.records) == 1
    now[0] += 61  # retry window over
    store.error = None
    assert tts.synthesize("Привет", "ru").sample_rate == 22050
    assert len(factory.calls) == 1


def test_load_failure_of_one_language_does_not_block_the_other():
    def factory(path, spec, threads):
        if spec.lang == "en":
            raise OSError("bad model")
        return FakeEngine()

    tts, *_ = make(factory=factory)
    with pytest.raises(ProviderUnavailable, match="cannot load"):
        tts.synthesize("Hello", "en")
    assert tts.synthesize("Привет", "ru").pcm


def test_unexpected_load_error_is_wrapped():
    tts, *_ = make(factory=Factory(error=RuntimeError("dll")))
    with pytest.raises(ProviderUnavailable, match="RuntimeError"):
        tts.synthesize("Привет", "ru")


def test_falls_back_to_the_next_provider_on_load_failure(caplog):
    piper, *_ = make(FakeStore(error=ProviderUnavailable("no network")))
    sapi = FakeTTS(name="sapi")
    chain = FallbackTTS(piper, sapi, cooldown_s=60)
    with caplog.at_level(logging.WARNING, logger="luna.tts.edge"):
        clip = chain.synthesize("Привет", "ru")
    assert clip.pcm
    assert [c.text for c in sapi.calls] == ["Привет"]
    assert chain.name == "piper+sapi"
    assert any("piper unavailable" in r.message for r in caplog.records)


def test_not_speakable_text_is_not_a_failure_for_the_chain():
    piper, *_ = make()
    sapi = FakeTTS(name="sapi")
    chain = FallbackTTS(piper, sapi)
    with pytest.raises(NothingToSpeak):
        chain.synthesize("...", "ru")
    assert sapi.calls == []


def test_sherpa_import_failure_is_unavailable(monkeypatch):
    import sys

    from luna.tts import piper

    monkeypatch.setitem(sys.modules, "sherpa_onnx", None)  # import raises ImportError
    spec = SimpleNamespace(model_file="m.onnx")
    with pytest.raises(ProviderUnavailable, match="sherpa-onnx"):
        piper._sherpa_engine(Path("x"), spec, 2)  # type: ignore[arg-type]
