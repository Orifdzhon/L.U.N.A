"""STT benchmark script (Issue #87): scoring, phrase file, audio files, recording, report.

No audio hardware, network or model downloads: providers and TTS are fakes.
"""

import io
import json
import wave
from pathlib import Path

import bench_stt
import pytest

from luna.audio.base import AudioClip
from luna.core.errors import ConfigError, ProviderRequestError, ProviderUnavailable, RateLimited
from luna.stt.base import Transcript

PHRASES = Path(__file__).resolve().parents[2] / "benchmarks" / "stt_phrases.toml"
NO_SLEEP = lambda _s: None  # noqa: E731


def phrase(pid="p1", lang="ru", category="app", text="открой ютуб"):
    return bench_stt.Phrase(pid, lang, category, text)


def silent(pid="s"):
    return phrase(pid, category="silence", text="")


def pcm(seconds=1.0, rate=16_000):
    return b"\x01\x00" * int(seconds * rate)


class FakeSTT:
    name = "fake"

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []

    def transcribe(self, pcm16, sample_rate=16_000, *, lang_hint=None, prompt=None, timeout_s):
        self.calls.append(lang_hint)
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class Ticker:
    def __init__(self, step=0.5):
        self.t, self.step = 0.0, step

    def __call__(self):
        self.t += self.step
        return self.t


def clips_for(*phrases):
    return [bench_stt.Clip(p, pcm(2.0)) for p in phrases]


# -- text scoring ------------------------------------------------------------------------------


def test_normalize_text():
    assert bench_stt.normalize_text("  Открой, YouTube!! Ёлка ") == "открой youtube елка"
    assert bench_stt.normalize_text("Set 20-minute timer.") == "set 20 minute timer"
    assert bench_stt.normalize_text("a_b") == "a b"


def test_edit_distance():
    assert bench_stt.edit_distance("kitten", "sitting") == 3
    assert bench_stt.edit_distance([], ["a", "b"]) == 2
    assert bench_stt.edit_distance(["a"], []) == 1
    assert bench_stt.edit_distance(["a", "b"], ["a", "b"]) == 0


def test_score_text_perfect_after_normalisation():
    s = bench_stt.score_text("Открой YouTube", "открой, youtube.")
    assert (s.wer, s.cer) == (0.0, 0.0)


def test_score_text_counts_word_and_char_edits():
    s = bench_stt.score_text("открой телеграм сейчас", "открой телеграмм")
    assert (s.word_edits, s.ref_words) == (2, 3)  # 1 substitution + 1 deletion
    assert s.wer == pytest.approx(2 / 3)
    assert s.char_edits == len(" сейчас")  # 1 substitution + 6 deletions
    assert s.cer == pytest.approx(s.char_edits / len("открой телеграм сейчас"))


def test_score_text_empty_reference_has_no_rate():
    s = bench_stt.score_text("", "ghost text")
    assert s.wer is None and s.cer is None and s.word_edits == 2


# -- phrase file -------------------------------------------------------------------------------


def test_shipped_phrase_file_is_valid_and_covers_the_cases():
    phrases = bench_stt.load_phrases(PHRASES)
    assert {p.lang for p in phrases} == {"ru", "en", "mixed"}
    assert {"app", "site", "search", "number", "silence"} <= {p.category for p in phrases}
    assert any(p.text == "Открой YouTube" for p in phrases)


@pytest.mark.parametrize(
    "body",
    [
        "",
        '[[phrase]]\nid = "a"\nlang = "ru"\ncategory = "app"\n',
        '[[phrase]]\nid = "a b"\nlang = "ru"\ncategory = "app"\ntext = "x"\n',
        '[[phrase]]\nid = "a"\nlang = "de"\ncategory = "app"\ntext = "x"\n',
        '[[phrase]]\nid = "a"\nlang = "ru"\ncategory = "zzz"\ntext = "x"\n',
        '[[phrase]]\nid = "a"\nlang = "ru"\ncategory = "silence"\ntext = "x"\n',
        '[[phrase]]\nid = "a"\nlang = "ru"\ncategory = "app"\ntext = "x"\n'
        '[[phrase]]\nid = "a"\nlang = "ru"\ncategory = "app"\ntext = "y"\n',
        "not toml [",
    ],
)
def test_load_phrases_rejects_bad_files(tmp_path, body):
    path = tmp_path / "p.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError):
        bench_stt.load_phrases(path)


def test_mixed_phrase_expects_russian():
    assert phrase(lang="mixed").expected_lang == "ru"
    assert phrase(lang="en").expected_lang == "en"


# -- audio files -------------------------------------------------------------------------------


def test_wav_roundtrip_and_no_overwrite(tmp_path):
    path = tmp_path / "sub" / "a.wav"
    bench_stt.write_wav(path, pcm(0.5))
    assert bench_stt.read_wav(path) == pcm(0.5)
    with pytest.raises(FileExistsError):
        bench_stt.write_wav(path, pcm(0.1))
    assert bench_stt.read_wav(path) == pcm(0.5)


def test_resample_changes_length_and_keeps_same_rate():
    data = pcm(1.0, 24_000)
    assert len(bench_stt.resample_pcm16(data, 24_000, 16_000)) // 2 == 16_000
    assert bench_stt.resample_pcm16(data, 16_000, 16_000) is data


class FakeTTS:
    name = "faketts"

    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    def synthesize(self, text, lang):
        self.calls.append((text, lang))
        if text in self.fail:
            raise ProviderUnavailable("down")
        return AudioClip(pcm(1.0, 24_000), 24_000)


def test_synthesize_clips_caches_and_regenerates_on_text_change(tmp_path):
    tts = FakeTTS()
    phrases = [phrase("a", text="раз"), silent()]
    first = bench_stt.synthesize_clips(tts, phrases, tmp_path)
    assert [c.phrase.id for c in first] == ["a", "s"]
    assert first[0].duration_s == pytest.approx(1.0 + 2 * bench_stt.SYNTH_PAD_S)
    assert first[1].duration_s == pytest.approx(bench_stt.SILENCE_S)
    bench_stt.synthesize_clips(tts, phrases, tmp_path)
    assert len(tts.calls) == 1  # the second run reads the cache
    bench_stt.synthesize_clips(tts, [phrase("a", text="два")], tmp_path)
    assert tts.calls[-1] == ("два", "ru")


def test_synthesize_clips_skips_failed_phrase(tmp_path):
    logs = []
    clips = bench_stt.synthesize_clips(
        FakeTTS(fail={"bad"}),
        [phrase("a", text="bad"), phrase("b", text="ok")],
        tmp_path,
        log=logs.append,
    )
    assert [c.phrase.id for c in clips] == ["b"]
    assert "a failed" in logs[0]


def test_load_recorded_clips_skips_missing(tmp_path):
    bench_stt.write_wav(tmp_path / "a.wav", pcm())
    logs = []
    clips = bench_stt.load_recorded_clips(
        [phrase("a"), phrase("b"), silent()], tmp_path, log=logs.append
    )
    assert [c.phrase.id for c in clips] == ["a"]
    assert len(logs) == 1 and "b" in logs[0]


# -- recording ---------------------------------------------------------------------------------


class FakeMic:
    def __init__(self, utterances):
        self.utterances = list(utterances)

    def record_pcm(self):
        return self.utterances.pop(0)


def scripted(answers):
    it = iter(answers)
    return lambda _prompt: next(it)


def wav_bytes(data):
    buf = io.BytesIO()
    bench_stt._write_wav_to(buf, data, 16_000)
    return buf.getvalue()


def test_record_saves_16k_mono_and_refuses_overwrite(tmp_path):
    existing = tmp_path / "a.wav"
    bench_stt.write_wav(existing, b"\x07\x00" * 10)
    out = []
    written = bench_stt.record_phrases(
        [phrase("a"), phrase("b"), silent()],
        tmp_path,
        FakeMic([pcm(0.5)]),
        input_fn=scripted(["", ""]),
        out=out.append,
    )
    assert written == 1
    assert existing.read_bytes() == wav_bytes(b"\x07\x00" * 10)  # untouched
    with wave.open(str(tmp_path / "b.wav")) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16_000, 1, 2)
    assert any("not overwriting" in line for line in out)


def test_record_retry_redo_skip_quit(tmp_path):
    mic = FakeMic([b"", pcm(0.4), pcm(0.4)])
    # a: record (nothing heard) -> record -> redo -> record -> keep;  b: skip;  c: quit
    answers = ["", "", "r", "", "", "s", "q"]
    written = bench_stt.record_phrases(
        [phrase("a"), phrase("b"), phrase("c")],
        tmp_path,
        mic,
        input_fn=scripted(answers),
        out=lambda _m: None,
    )
    assert written == 1
    assert (tmp_path / "a.wav").exists()
    assert not (tmp_path / "b.wav").exists() and not (tmp_path / "c.wav").exists()


# -- running a variant -------------------------------------------------------------------------


def test_run_variant_scores_and_times():
    report = bench_stt.VariantReport("fake", "m")
    stt = FakeSTT(
        [Transcript("Открой YouTube", "ru"), Transcript("Open telegram", "ru"), Transcript("")]
    )
    clips = clips_for(
        phrase("a", "mixed", text="Открой YouTube"),
        phrase("b", "en", text="Open Telegram"),
        silent(),
    )
    bench_stt.run_variant(report, stt, clips, clock=Ticker(), sleep=NO_SLEEP)
    a, b, s = report.results
    assert (a.word_edits, a.lang_ok, a.latency_s, a.rtf) == (0, True, 0.5, 0.25)
    assert (b.word_edits, b.lang_ok, b.detected_lang) == (0, False, "ru")
    assert s.empty and s.lang_ok is None
    summary = bench_stt.summarize(report)
    assert summary["wer_pct"] == 0.0
    assert summary["lang_accuracy_pct"] == 50.0
    assert summary["silence_hallucinations"] == 0
    assert summary["cold_s"] == 0.5 and summary["warm_p50_s"] == 0.5
    assert stt.calls == [None, None, None]


def test_run_variant_phrase_hint_hallucinated_silence_and_empty():
    report = bench_stt.VariantReport("fake", "m")
    stt = FakeSTT([Transcript(""), Transcript("спасибо за просмотр", "ru")])
    clips = clips_for(phrase("a", "en", text="hello"), silent())
    bench_stt.run_variant(report, stt, clips, lang_hint="phrase", clock=Ticker(), sleep=NO_SLEEP)
    assert stt.calls == ["en", "ru"]
    summary = bench_stt.summarize(report)
    assert summary["empty_results"] == 1
    assert summary["wer_pct"] == 100.0
    assert summary["silence_hallucinations"] == 1


def test_run_variant_stops_after_consecutive_rate_limits_and_sleeps_between():
    report = bench_stt.VariantReport("groq", "m")
    stt = FakeSTT([RateLimited("x"), RateLimited("x"), Transcript("a", "ru")])
    sleeps = []
    bench_stt.run_variant(
        report,
        stt,
        clips_for(*[phrase(f"p{i}", text="a") for i in range(5)]),
        max_failures=2,
        delay_s=1.5,
        sleep=sleeps.append,
        clock=Ticker(),
    )
    assert len(report.results) == 2
    assert report.stopped_reason == "stopped after 2 x RateLimited"
    assert sleeps == [1.5]
    assert bench_stt.summarize(report)["errors"] == {"RateLimited": 2}


def test_run_variant_success_resets_failure_counter_and_request_error_does_not_stop():
    report = bench_stt.VariantReport("groq", "m")
    stt = FakeSTT(
        [RateLimited("x"), Transcript("a", "ru"), RateLimited("x"), ProviderRequestError("bad")]
    )
    bench_stt.run_variant(
        report,
        stt,
        clips_for(*[phrase(f"p{i}", text="a") for i in range(4)]),
        max_failures=2,
        clock=Ticker(),
        sleep=NO_SLEEP,
    )
    assert len(report.results) == 4 and report.stopped_reason is None


def test_percentile():
    assert bench_stt.percentile([], 50) is None
    assert bench_stt.percentile([3.0], 95) == 3.0
    assert bench_stt.percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5


# -- report ------------------------------------------------------------------------------------


def sample_reports():
    ok = bench_stt.VariantReport("local", "base", "cpu", load_s=1.234)
    bench_stt.run_variant(
        ok,
        FakeSTT([Transcript("открой ютуб | x", "ru"), Transcript("hello", "en")]),
        clips_for(phrase("a", text="открой ютуб"), phrase("b", "en", text="hello")),
        clock=Ticker(),
        sleep=NO_SLEEP,
    )
    skipped = bench_stt.VariantReport(
        "groq", "turbo", "cloud", skipped_reason="no key (GROQ_API_KEY)"
    )
    return [ok, skipped]


def test_markdown_report():
    md = bench_stt.format_markdown(sample_reports(), {"usable": False})
    assert "| provider | model | device | load s |" in md
    assert "| local | base | cpu | 1.23 |" in md
    assert "no key (GROQ_API_KEY)" in md
    assert "## local/base" in md
    assert "открой ютуб \\| x" in md  # pipes escaped
    assert "## CUDA probe" in md and '"usable": false' in md


def test_json_report_is_serialisable():
    data = bench_stt.build_json(sample_reports(), {"source": "synth-edge"}, None)
    loaded = json.loads(json.dumps(data, ensure_ascii=False))
    assert loaded["variants"][0]["summary"]["run"] == 2
    assert loaded["variants"][1]["skipped_reason"].startswith("no key")


# -- probes ------------------------------------------------------------------------------------


def test_probe_cuda_reports_missing_dll_without_crashing():
    def broken():
        raise RuntimeError("no driver")

    def loader(name):
        raise OSError(name)

    report = bench_stt.probe_cuda(
        device_count=broken, dll_loader=loader, dll_setup=list, smi=lambda: None
    )
    assert report["usable"] is False
    assert report["ctranslate2_cuda_devices"].startswith("error")
    assert report["dlls_loadable"] == {"cublas64_12.dll": False, "cudnn64_9.dll": False}


def test_probe_cuda_usable():
    report = bench_stt.probe_cuda(
        device_count=lambda: 1, dll_loader=lambda n: object(), dll_setup=list, smi=lambda: 512.0
    )
    assert report["usable"] is True and report["gpu_memory_used_mb"] == 512.0


def test_gpu_memory_mb():
    assert bench_stt.gpu_memory_mb(which=lambda _n: None) is None
    assert bench_stt.gpu_memory_mb(runner=lambda _a: "1234\n", which=lambda _n: "smi") == 1234.0
    assert bench_stt.gpu_memory_mb(runner=lambda _a: "N/A", which=lambda _n: "smi") is None

    def boom(_a):
        raise OSError

    assert bench_stt.gpu_memory_mb(runner=boom, which=lambda _n: "smi") is None


def test_peak_rss_is_positive_number_or_none():
    value = bench_stt.peak_rss_mb()
    assert value is None or value > 0


def test_help_works(capsys):
    with pytest.raises(SystemExit) as exc:
        bench_stt.main(["--help"])
    assert exc.value.code == 0
    assert "--record" in capsys.readouterr().out


def test_main_rejects_unknown_provider():
    assert bench_stt.main(["--providers", "nope"]) == 2
