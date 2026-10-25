import logging
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from luna.core.errors import ProviderRequestError, ProviderUnavailable
from luna.stt.base import STTProvider
from luna.stt.faster_whisper import (
    FasterWhisperSTT,
    add_nvidia_dll_directories,
    default_model_dir,
)

MODEL_DIR = Path(tempfile.gettempdir()) / "luna-test-models"
PCM = struct.pack("<4h", 0, 16384, -16384, -32768) * 400  # 1600 samples = 0.1 s


def seg(text, avg_logprob=-0.2, no_speech_prob=0.01):
    return SimpleNamespace(text=text, avg_logprob=avg_logprob, no_speech_prob=no_speech_prob)


class FakeModel:
    def __init__(self, segments=None, language="ru", probability=0.9, delay=0.0):
        self.segments = [seg(" открой ютуб")] if segments is None else segments
        self.info = SimpleNamespace(language=language, language_probability=probability)
        self.calls = []
        self.delay = delay

    def transcribe(self, audio, **kwargs):
        self.calls.append((audio, kwargs))
        time.sleep(self.delay)
        return iter(self.segments), self.info


class Factory:
    def __init__(self, model=None, fail_times=0, delay=0.0):
        self.model = model or FakeModel()
        self.calls = []
        self.fail_times = fail_times
        self.delay = delay

    def __call__(self, name, **kwargs):
        self.calls.append((name, kwargs))
        time.sleep(self.delay)
        if len(self.calls) <= self.fail_times:
            raise OSError("offline")
        return self.model


def make(factory=None, **kwargs):
    factory = factory or Factory()
    kwargs.setdefault("model_dir", MODEL_DIR)
    stt = FasterWhisperSTT(model_factory=factory, **kwargs)
    return stt, factory


def run(stt, **kwargs):
    return stt.transcribe(PCM, 16_000, timeout_s=5, **kwargs)


def test_import_is_lazy():
    code = (
        "import sys, luna.stt.faster_whisper;"
        "bad=[m for m in ('faster_whisper','ctranslate2','numpy') if m in sys.modules];"
        "print(bad)"
    )
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert out.stdout.strip() == "[]", out.stderr


def test_satisfies_protocol():
    assert isinstance(make()[0], STTProvider)


def test_constructor_does_not_load_model():
    stt, factory = make()
    assert factory.calls == [] and not stt.is_loaded


def test_factory_called_once_for_many_calls():
    stt, factory = make()
    for _ in range(3):
        run(stt)
    assert len(factory.calls) == 1
    name, kwargs = factory.calls[0]
    assert name == "base"
    assert kwargs["device"] == "cpu" and kwargs["compute_type"] == "int8"
    assert kwargs["download_root"] == str(MODEL_DIR)


def test_factory_called_once_from_two_threads():
    stt, factory = make(Factory(delay=0.2))
    results = []
    threads = [threading.Thread(target=lambda: results.append(run(stt))) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(factory.calls) == 1
    assert [r.text for r in results] == ["открой ютуб"] * 2


def test_warm_up_loads_and_background_variant_swallows_errors(caplog):
    stt, factory = make()
    stt.warm_up()
    assert stt.is_loaded and len(factory.calls) == 1
    bad, _ = make(Factory(fail_times=5))
    with caplog.at_level(logging.WARNING):
        bad.start_warm_up().join(5)
    assert "warm-up failed" in caplog.text and not bad.is_loaded


def test_pcm_converted_to_float32_range():
    stt, factory = make()
    run(stt)
    audio = factory.model.calls[0][0]
    assert str(audio.dtype) == "float32" and len(audio) == 1600
    assert list(audio[:4]) == [0.0, 0.5, -0.5, -1.0]


def test_language_hint_and_auto():
    stt, factory = make()
    run(stt)
    run(stt, lang_hint="ru", prompt="Chrome, Telegram")
    run(stt, lang_hint="xx")
    first, second, third = (c[1] for c in factory.model.calls)
    assert first["language"] is None and first["initial_prompt"] is None
    assert second["language"] == "ru" and second["initial_prompt"] == "Chrome, Telegram"
    assert third["language"] is None


def test_decode_options_and_vad_off_by_default():
    stt, factory = make(beam_size=3)
    run(stt)
    kwargs = factory.model.calls[0][1]
    assert kwargs["vad_filter"] is False and kwargs["beam_size"] == 3
    assert kwargs["condition_on_previous_text"] is False
    stt2, factory2 = make(vad_filter=True)
    run(stt2)
    assert factory2.model.calls[0][1]["vad_filter"] is True


def test_transcript_mapping():
    model = FakeModel([seg(" open"), seg(" youtube ")], language="en", probability=0.99)
    result = run(make(Factory(model))[0])
    assert result.text == "open youtube" and result.lang == "en"
    assert result.confidence == pytest.approx(0.8187, abs=1e-3)  # exp(-0.2)


def test_confidence_falls_back_to_language_probability_and_unknown_lang():
    model = FakeModel([SimpleNamespace(text="hi")], language="de", probability=0.7)
    result = run(make(Factory(model))[0])
    assert result.lang == "unknown" and result.confidence == 0.7


def test_silence_yields_empty_transcript():
    assert run(make(Factory(FakeModel([])))[0]).is_empty
    hallucination = FakeModel([seg("Thanks for watching", -1.5, 0.9)])
    assert run(make(Factory(hallucination))[0]).is_empty


@pytest.mark.parametrize(
    ("pcm", "rate"),
    [(b"", 16_000), (b"\x00", 16_000), (PCM, 8_000), (b"\x00\x00" * 16_000 * 121, 16_000)],
    ids=["empty", "odd", "rate", "too-long"],
)
def test_bad_input(pcm, rate):
    stt, factory = make()
    with pytest.raises(ProviderRequestError):
        stt.transcribe(pcm, rate, timeout_s=5)
    assert factory.calls == []


def test_failed_load_is_unavailable_and_retried_cleanly():
    stt, factory = make(Factory(fail_times=1))
    with pytest.raises(ProviderUnavailable, match="internet"):
        run(stt)
    assert not stt.is_loaded
    assert run(stt).text == "открой ютуб"
    assert len(factory.calls) == 2


def test_decode_error_is_unavailable():
    model = FakeModel()
    model.transcribe = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    with pytest.raises(ProviderUnavailable):
        run(make(Factory(model))[0])


def test_missing_package_gives_install_hint(monkeypatch):
    monkeypatch.setattr("luna.stt.faster_whisper.importlib.util.find_spec", lambda name: None)
    stt = FasterWhisperSTT(model_dir=MODEL_DIR)
    with pytest.raises(ProviderUnavailable, match="uv sync"):
        run(stt)


def test_auto_without_cuda_uses_cpu_and_warns(caplog):
    stt, factory = make(device="auto", cuda_probe=lambda: 0)
    with caplog.at_level(logging.WARNING):
        run(stt)
    assert factory.calls[0][1]["device"] == "cpu" and stt.resolved_device == "cpu"
    assert "CUDA is not available" in caplog.text


def test_cuda_without_cuda_raises():
    stt, factory = make(device="cuda", cuda_probe=lambda: 0)
    with pytest.raises(ProviderUnavailable, match="CUDA is not available"):
        run(stt)
    assert factory.calls == []


def test_auto_cuda_load_failure_falls_back_to_cpu_once(caplog):
    class CudaBroken(Factory):
        def __call__(self, name, **kwargs):
            self.calls.append((name, kwargs))
            if kwargs["device"] == "cuda":
                raise RuntimeError("cublas64_12.dll not found")
            return self.model

    stt, factory = make(CudaBroken(), device="auto", cuda_probe=lambda: 1, compute_type="float16")
    with caplog.at_level(logging.WARNING):
        run(stt)
    assert [c[1]["device"] for c in factory.calls] == ["cuda", "cpu"]
    assert factory.calls[1][1]["compute_type"] == "int8"
    assert caplog.text.count("falling back to CPU") == 1


def test_cuda_probe_error_counts_as_no_cuda():
    def boom():
        raise ImportError("ctranslate2")

    stt, _ = make(device="cuda", cuda_probe=boom)
    with pytest.raises(ProviderUnavailable):
        run(stt)


def test_invalid_device_rejected():
    with pytest.raises(ValueError):
        FasterWhisperSTT(device="tpu")


def test_default_model_dir(monkeypatch):
    assert default_model_dir({"LOCALAPPDATA": "C:/x"}) == Path("C:/x/LUNA/models")
    monkeypatch.setattr(Path, "home", lambda: Path("H"))
    assert default_model_dir({}) == Path("H/AppData/Local/LUNA/models")
    assert FasterWhisperSTT(model_factory=Factory())._model_dir == default_model_dir()


def test_download_logged_only_when_not_cached(tmp_path, caplog):
    stt, _ = make(model_dir=tmp_path)
    with caplog.at_level(logging.INFO):
        run(stt)
    assert "downloading STT model 'base', about 145 MB" in caplog.text
    caplog.clear()
    (tmp_path / "models--Systran--faster-whisper-base").mkdir()
    stt2, _ = make(model_dir=tmp_path)
    with caplog.at_level(logging.INFO):
        run(stt2)
    assert "downloading" not in caplog.text


def test_add_nvidia_dll_directories(tmp_path):
    for pkg in ("cublas", "cudnn"):
        (tmp_path / pkg / "bin").mkdir(parents=True)
    paths = {
        "nvidia.cublas": SimpleNamespace(submodule_search_locations=[str(tmp_path / "cublas")]),
        "nvidia.cudnn": SimpleNamespace(submodule_search_locations=[str(tmp_path / "cudnn")]),
    }
    added = []
    result = add_nvidia_dll_directories(paths.get, added.append)
    assert result == added and len(added) == 2
    assert add_nvidia_dll_directories(lambda name: None, added.append) == []


class CudaLoadsButCannotDecode(Factory):
    """Mirrors the real failure: the model loads on CUDA, decoding needs a missing cuBLAS DLL."""

    def __call__(self, name, **kwargs):
        self.calls.append((name, kwargs))
        if kwargs["device"] == "cuda":
            model = FakeModel()

            def broken(*a, **k):
                raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")

            model.transcribe = broken
            return model
        return self.model


def test_auto_cuda_that_cannot_decode_falls_back_to_cpu(caplog):
    stt, factory = make(CudaLoadsButCannotDecode(), device="auto", cuda_probe=lambda: 1)
    with caplog.at_level(logging.WARNING):
        assert run(stt).text == "открой ютуб"
    assert stt.resolved_device == "cpu"
    assert [c[1]["device"] for c in factory.calls] == ["cuda", "cpu"]
    assert caplog.text.count("falling back to CPU") == 1


def test_explicit_cuda_that_cannot_decode_raises_with_hint():
    stt, _ = make(CudaLoadsButCannotDecode(), device="cuda", cuda_probe=lambda: 1)
    with pytest.raises(ProviderUnavailable, match="cuBLAS"):
        run(stt)
    assert not stt.is_loaded


def test_working_cuda_is_used():
    stt, factory = make(device="auto", cuda_probe=lambda: 1)
    run(stt)
    assert stt.resolved_device == "cuda" and len(factory.calls) == 1
