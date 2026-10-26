"""Wake-word benchmark script (Issue #112): grid, stream runner, metrics, report, clips, recording.

No model, no audio device, no TTS: detectors are fakes.
"""

import csv
import json
import wave
from array import array
from pathlib import Path

import bench_stt
import bench_wake
import pytest

from fakes import FakeVerifier, FakeWakeWord
from luna.audio.base import AudioClip
from luna.core.errors import ConfigError
from luna.wakeword import verify as production_verify
from luna.wakeword.verify import VerifyResult

FRAME = 480


def pcm(seconds: float) -> bytes:
    return bytes(int(seconds * bench_wake.SAMPLE_RATE) * 2)


def clip(cid="p", label="positive", seconds=2.0, end=None):
    return bench_wake.BenchClip(cid, label, pcm(seconds), end)


def cfg(**kw):
    base = {"mode": "grammar", "threshold": 0.5, "min_word_s": 0.3, "report_partials": True}
    return bench_wake.Config(**{**base, **kw})


def zero_clock():
    return 0.0


def write_wav(path: Path, data: bytes, rate=16_000, channels=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data)


# -- grid --------------------------------------------------------------------------------------


def test_grid_is_the_cartesian_product_in_stable_order():
    grid = bench_wake.build_grid(["grammar", "free"], [0.3, 0.5], [0, 0.3], [True, False])
    assert len(grid) == 16
    assert grid[0] == cfg(threshold=0.3, min_word_s=0, report_partials=True)
    assert len(set(grid)) == 16


@pytest.mark.parametrize(
    "args",
    [
        (["bogus"], [0.5], [0], [True]),
        (["free"], [1.5], [0], [True]),
        (["free"], [0.5], [-1], [True]),
    ],
)
def test_grid_rejects_bad_values(args):
    with pytest.raises(ConfigError):
        bench_wake.build_grid(*args)


def test_parse_helpers():
    assert bench_wake.parse_floats("0.3, 0.5,0.3") == [0.3, 0.5]
    assert bench_wake.parse_partials("both") == [True, False]
    with pytest.raises(ConfigError):
        bench_wake.parse_floats("a,b")
    with pytest.raises(ConfigError):
        bench_wake.parse_partials("maybe")


def test_percentile_nearest_rank():
    values = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    assert bench_wake.percentile(values, 50) == 0.5
    assert bench_wake.percentile(values, 95) == 1.0
    assert bench_wake.percentile([3.0], 95) == 3.0
    assert bench_wake.percentile([], 50) is None


# -- stream runner -----------------------------------------------------------------------------


def runner(det, **kw):
    return bench_wake.StreamRunner(det, clock=zero_clock, **kw)


def test_runner_records_event_time_and_resets_after_accept():
    det = FakeWakeWord([9], marker=False)  # fires on the 10th frame: 0.3 s
    r = runner(det)
    r.feed(pcm(1.0))
    assert [round(d.at_s, 3) for d in r.detections] == [0.3]
    assert r.detections[0].keyword_end_s == pytest.approx(0.3)
    assert det.reset_count == 1


def test_runner_cooldown_drops_repeats_and_end_is_offset_after_reset():
    # The fake fires on its 3rd frame after every reset, i.e. every 0.09 s of audio.
    r = runner(FakeWakeWord([2], marker=False), cooldown_s=0.2)
    r.feed(pcm(1.0))
    assert len(r.detections) == 1  # the event 0.09 s later (index 2 again) is inside the cooldown
    r2 = runner(FakeWakeWord([2], marker=False), cooldown_s=0.05)
    r2.feed(pcm(1.0))
    assert len(r2.detections) > 1
    # end_sample restarts at every reset(); the runner adds the offset back
    assert r2.detections[1].keyword_end_s == pytest.approx(r2.detections[1].at_s)


def test_runner_rechunks_and_flushes_partial_frames():
    det = FakeWakeWord(marker=False)
    r = runner(det)
    r.feed(bytes(FRAME * 2 + 100))
    assert len(det.frames) == 1
    r.flush(tail_s=0.3)  # pad the partial frame (1) + 10 silent frames
    assert len(det.frames) == 1 + 1 + 10
    assert r.audio_s == pytest.approx(12 * 0.03)


def test_runner_cpu_uses_the_clock():
    ticks = iter(float(i) for i in range(1000))
    r = bench_wake.StreamRunner(FakeWakeWord(marker=False), clock=lambda: next(ticks) * 0.5)
    r.feed(pcm(0.3))  # 10 frames, two clock reads each, 0.5 s apart
    assert r.cpu_s == pytest.approx(5.0)


# -- evaluation --------------------------------------------------------------------------------


def factory_with(trigger):
    """Detector firing on the given frame indices of every clip."""
    return lambda _config: FakeWakeWord(trigger, marker=False)


def test_recall_hits_misses_and_latency_from_engine_end():
    clips = [clip("a"), clip("b")]
    res = bench_wake.evaluate_config(cfg(), factory_with([19]), clips, clock=zero_clock)
    assert (res.positives, res.hits, res.recall) == (2, 2, 1.0)
    assert res.latencies_s == [pytest.approx(0.0), pytest.approx(0.0)]
    miss = bench_wake.evaluate_config(cfg(), factory_with([]), clips, clock=zero_clock)
    assert miss.hits == 0 and miss.misses == ["a", "b"] and miss.recall == 0.0


def test_latency_uses_manifest_keyword_end_when_given():
    c = clip("a", end=0.30)
    res = bench_wake.evaluate_config(cfg(), factory_with([19]), [c], clock=zero_clock)
    assert res.latencies_s == [pytest.approx(0.60 - 0.30)]


def test_false_accepts_per_hour_over_clip_negatives():
    clips = [clip("p"), clip("n1", "negative", 18.0), clip("n2", "negative", 18.0)]
    res = bench_wake.evaluate_config(cfg(), factory_with([5]), clips, clock=zero_clock)
    assert res.fa_clips == 2  # one per negative clip
    assert res.negative_hours == pytest.approx(36 / 3600)
    assert res.fa_per_hour == pytest.approx(2 / (36 / 3600))
    assert {fa.source for fa in res.false_accepts} == {"n1", "n2"}


def test_long_files_are_streamed_and_counted_in_hours():
    def chunks(_path):
        return iter([pcm(60.0), pcm(60.0)])

    res = bench_wake.evaluate_config(
        cfg(),
        factory_with([]),
        [clip("p")],
        [Path("long.wav")],
        tail_s=0.0,
        clock=zero_clock,
        long_reader=chunks,
    )
    assert res.long_negative_s == pytest.approx(120.0)
    assert res.fa_long == 0 and res.fa_per_hour == 0.0


def test_long_stage_runs_only_for_configs_passing_recall():
    calls = []

    def reader(path):
        calls.append(path)
        return iter([pcm(1.0)])

    def factory(config):
        return FakeWakeWord([5] if config.threshold < 0.5 else [], marker=False)

    grid = [cfg(threshold=0.3), cfg(threshold=0.7)]
    args = (grid, factory, [clip("p")], [Path("l.wav")])
    results = bench_wake.run_grid(*args, clock=zero_clock, long_reader=reader)
    assert [r.long_skipped for r in results] == [False, True]
    assert len(calls) == 1
    again = bench_wake.run_grid(*args, long_all=True, clock=zero_clock, long_reader=reader)
    assert [r.long_skipped for r in again] == [False, False]


def test_detector_is_closed_after_each_config():
    made = []

    def factory(config):
        det = FakeWakeWord(marker=False)
        made.append(det)
        return det

    bench_wake.run_grid([cfg(), cfg(threshold=0.7)], factory, [clip("p")], clock=zero_clock)
    assert len(made) == 2 and all(d.closed for d in made)


# -- D7 verdict --------------------------------------------------------------------------------


def result(hits=9, positives=10, fa=0, neg_h=2.0, skipped=False):
    return bench_wake.ConfigResult(
        cfg(), positives, hits, fa_long=fa, long_negative_s=neg_h * 3600, long_skipped=skipped
    )


def test_verdict_yes_provisional_no():
    assert result().verdict() == "yes"
    assert result(hits=8).verdict() == "no"  # recall 80 %
    assert result(fa=2).verdict() == "no"  # 1.0 FA/h
    assert result(fa=1).verdict() == "yes"  # exactly 0.5/h still meets the target
    assert result(neg_h=0.2).verdict() == "provisional"
    assert result(skipped=True).verdict() == "no"
    assert result(neg_h=0.0).verdict() == "no"  # no negative audio at all


# -- report ------------------------------------------------------------------------------------


def test_markdown_highlights_d7_rows_first_and_marks_synthetic():
    good = result()
    bad = bench_wake.ConfigResult(cfg(threshold=0.9), 10, 3, fa_long=9, long_negative_s=7200.0)
    text = bench_wake.markdown_report([bad, good], synthetic=True)
    assert "SYNTHETIC" in text
    rows = [ln for ln in text.splitlines() if ln.startswith("| ") and "thr" not in ln]
    assert rows[0].startswith("| **yes**") and rows[1].startswith("| no")
    assert "90.0" in rows[0] and "9/10" in rows[0]
    assert "SYNTHETIC" not in bench_wake.markdown_report([good], synthetic=False)


def test_markdown_shows_skipped_long_stage():
    text = bench_wake.markdown_report([result(skipped=True)], synthetic=False)
    assert "skipped" in text


def test_json_report_is_serialisable_and_caps_fa_list():
    r = result(fa=1)
    r.false_accepts = [bench_wake.FalseAccept("x.wav", float(i)) for i in range(100)]
    data = bench_wake.json_report([r], synthetic=False, options={"k": 1})
    json.dumps(data)
    row = data["results"][0]
    assert row["verdict"] == "yes" and row["config"]["mode"] == "grammar"
    assert len(row["false_accept_events"]) == bench_wake.MAX_FA_LISTED
    assert data["targets"] == {"max_fa_per_hour": 0.5, "min_recall": 0.9}


# -- clip loading ------------------------------------------------------------------------------


def test_load_clips_from_subfolders(tmp_path):
    write_wav(tmp_path / "positive" / "a.wav", pcm(1.0))
    write_wav(tmp_path / "negative" / "b.wav", pcm(1.0))
    clips = bench_wake.load_clip_folder(tmp_path)
    assert {(c.id, c.label) for c in clips} == {("a", "positive"), ("b", "negative")}


def test_load_clips_from_manifest_with_keyword_end_and_resampling(tmp_path):
    write_wav(tmp_path / "x" / "a.wav", bytes(44_100 * 2), rate=44_100)
    with (tmp_path / "manifest.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["file", "label", "keyword_end_s"])
        w.writerow(["x/a.wav", "Positive", "0.4"])
        w.writerow(["x/missing.wav", "negative", ""])
    msgs = []
    clips = bench_wake.load_clip_folder(tmp_path, log=msgs.append)
    assert len(clips) == 1 and clips[0].keyword_end_s == 0.4
    assert clips[0].duration_s == pytest.approx(1.0, abs=0.01)
    assert any("missing.wav" in m for m in msgs)


@pytest.mark.parametrize(
    "rows",
    [
        [["a.wav", "maybe", ""]],
        [["../evil.wav", "positive", ""]],
        [["a.wav", "positive", "soon"]],
    ],
)
def test_manifest_errors(tmp_path, rows):
    with (tmp_path / "manifest.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["file", "label", "keyword_end_s"])
        w.writerows(rows)
    with pytest.raises(ConfigError):
        bench_wake.load_clip_folder(tmp_path)


def test_load_clips_empty_or_missing_folder(tmp_path):
    with pytest.raises(ConfigError):
        bench_wake.load_clip_folder(tmp_path)
    with pytest.raises(ConfigError):
        bench_wake.load_clip_folder(tmp_path / "nope")


def test_iter_wav_chunks_downmixes_and_resamples(tmp_path):
    left, right = (
        (1000).to_bytes(2, "little", signed=True),
        (-1000).to_bytes(2, "little", signed=True),
    )
    path = tmp_path / "s.wav"
    write_wav(path, (left + right) * 8000, rate=8000, channels=2)
    out = b"".join(bench_wake.iter_wav_chunks(path, chunk_s=0.5))
    assert len(out) == pytest.approx(16_000 * 2, abs=8)  # 1 s at 16 kHz
    assert int.from_bytes(out[:2], "little", signed=True) == 1000  # first channel
    assert bench_wake.wav_duration_s(path) == 1.0


def test_find_long_files(tmp_path):
    with pytest.raises(ConfigError):
        bench_wake.find_long_files(tmp_path)
    (tmp_path / "a.mp3").write_bytes(b"x")
    write_wav(tmp_path / "b.wav", pcm(0.1))
    assert [p.name for p in bench_wake.find_long_files(tmp_path)] == ["b.wav"]
    assert bench_wake.find_long_files(tmp_path / "b.wav") == [tmp_path / "b.wav"]


# -- phrases and synthesis ---------------------------------------------------------------------

PHRASES = Path(__file__).resolve().parents[2] / "benchmarks" / "wake_phrases.toml"


def test_shipped_phrase_file_is_valid_and_has_the_required_cases():
    phrases = bench_wake.load_wake_phrases(PHRASES)
    by_id = {p.id: (p, label) for p, label in phrases}
    texts = {p.text.lower(): label for p, label in phrases}
    assert texts["луна"] == "positive"
    assert texts["луна, открой ютуб"] == "positive"
    for negative in ("лунный свет", "луначарский", "она", "лун", "лена"):
        assert texts[negative] == "negative", negative
    assert any("луна-парк" in t and lbl == "negative" for t, lbl in texts.items())
    assert by_id["en-telegram"][0].category == "en"
    assert all(p.id.replace("-", "").isalnum() for p, _ in phrases)  # safe file names
    assert {label for _, label in phrases} == {"positive", "negative"}


class FakeTTS:
    name = "fake"

    def __init__(self):
        self.calls = []

    def synthesize(self, text, lang):
        self.calls.append((text, lang))
        return AudioClip(pcm=bytes(16_000), sample_rate=16_000)


def test_synth_clips_label_skip_categories_and_cache(tmp_path):
    phrases = bench_wake.load_wake_phrases(PHRASES)
    tts = FakeTTS()
    clips = bench_wake.synth_clips(tts, phrases, tmp_path, skip_categories=["en"])
    assert all(c.category != "en" for c in clips)
    assert {c.label for c in clips} == {"positive", "negative"}
    assert not any(lang == "en" for _, lang in tts.calls)
    again = FakeTTS()
    bench_wake.synth_clips(again, phrases, tmp_path, skip_categories=["en"])
    assert again.calls == []  # served from the cache


def test_bad_phrase_files(tmp_path):
    bad = tmp_path / "p.toml"
    bad.write_text('[[phrase]]\nid="a"\nlabel="maybe"\ncategory="x"\nlang="ru"\ntext="t"\n')
    with pytest.raises(ConfigError):
        bench_wake.load_wake_phrases(bad)
    bad.write_text('[[phrase]]\nid="a"\n')
    with pytest.raises(ConfigError):
        bench_wake.load_wake_phrases(bad)
    with pytest.raises(ConfigError):
        bench_wake.load_wake_phrases(tmp_path / "missing.toml")


# -- recording ---------------------------------------------------------------------------------


class FakeRecorder:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def capture(self, seconds):
        self.calls.append(seconds)
        return self.data


def scripted(answers):
    it = iter(answers)
    return lambda _prompt: next(it)


def two_phrases():
    return [
        (bench_stt.Phrase("w", "ru", "wake", "Луна"), "positive"),
        (bench_stt.Phrase("n", "ru", "contains", "лунный свет"), "negative"),
    ]


def test_record_session_files_clips_and_manifest(tmp_path):
    rec = FakeRecorder(b"\x10\x00" * 16_000)
    written = bench_wake.record_session(
        two_phrases(),
        tmp_path,
        rec,
        seconds=2,
        tag="3m",
        input_fn=scripted(["", "", "", ""]),  # record + keep, twice
        out=lambda _m: None,
    )
    assert written == 2 and rec.calls == [2, 2]
    assert (tmp_path / "positive" / "w-3m.wav").is_file()
    assert (tmp_path / "negative" / "n-3m.wav").is_file()
    with (tmp_path / "manifest.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert [(r["file"], r["label"]) for r in rows] == [
        ("positive/w-3m.wav", "positive"),
        ("negative/n-3m.wav", "negative"),
    ]
    assert len(bench_wake.load_clip_folder(tmp_path)) == 2  # directly loadable


def test_record_session_redo_skip_and_no_overwrite(tmp_path):
    rec = FakeRecorder(b"\x10\x00" * 16_000)
    answers = ["", "r", "", "", "s"]  # record, redo, record, keep; then skip the second phrase
    written = bench_wake.record_session(
        two_phrases(), tmp_path, rec, input_fn=scripted(answers), out=lambda _m: None
    )
    assert written == 1 and len(rec.calls) == 2
    rec2 = FakeRecorder(b"\x10\x00" * 16_000)
    notes = []
    written2 = bench_wake.record_session(
        two_phrases(), tmp_path, rec2, input_fn=scripted(["q"]), out=notes.append
    )
    assert written2 == 0 and rec2.calls == [] and any("exists" in n for n in notes)


def test_record_session_warns_on_silence(tmp_path):
    notes = []
    bench_wake.record_session(
        two_phrases()[:1],
        tmp_path,
        FakeRecorder(bytes(32_000)),
        input_fn=scripted(["", ""]),
        out=notes.append,
    )
    assert any("silent" in n for n in notes)


def test_record_folder_must_be_outside_repo(tmp_path):
    with pytest.raises(ConfigError):
        bench_wake.ensure_outside_repo(bench_wake.REPO / "benchmarks" / "clips")
    with pytest.raises(ConfigError):
        bench_wake.ensure_outside_repo(bench_wake.REPO)
    assert bench_wake.ensure_outside_repo(tmp_path) == tmp_path.resolve()


def test_rms_of():
    assert bench_wake.rms_of(b"") == 0.0
    assert bench_wake.rms_of((16384).to_bytes(2, "little", signed=True) * 100) == pytest.approx(0.5)


# -- main --------------------------------------------------------------------------------------


def test_main_runs_end_to_end_with_a_fake_detector(tmp_path, capsys):
    write_wav(tmp_path / "clips" / "positive" / "a.wav", pcm(1.0))
    write_wav(tmp_path / "clips" / "negative" / "b.wav", pcm(1.0))
    out, md = tmp_path / "r.json", tmp_path / "r.md"
    argv = [
        *("--clips", str(tmp_path / "clips")),
        *("--modes", "grammar", "--thresholds", "0.5", "--min-word", "0.3", "--partials", "on"),
        *("--out", str(out), "--md", str(md)),
    ]
    code = bench_wake.main(argv, factory=lambda _c: FakeWakeWord([10], marker=False))
    assert code == 0
    assert "| grammar | 0.5 | 0.3 | on | 100.0 | 1/1 | 1 |" in capsys.readouterr().out
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["synthetic"] is False and data["results"][0]["fa_clips"] == 1
    assert "Wake-word benchmark" in md.read_text(encoding="utf-8")


def test_main_requires_a_source_and_positives(tmp_path, capsys):
    assert bench_wake.main([], factory=lambda _c: FakeWakeWord()) == 2
    write_wav(tmp_path / "negative" / "b.wav", pcm(1.0))
    assert bench_wake.main(["--clips", str(tmp_path)], factory=lambda _c: FakeWakeWord()) == 2
    assert "no positive clips" in capsys.readouterr().err


def test_help_works_without_vosk(capsys):
    with pytest.raises(SystemExit) as exc:
        bench_wake.main(["--help"])
    assert exc.value.code == 0
    assert "--synth" in capsys.readouterr().out


# -- stage 2: verification (#128) --------------------------------------------------------------

KEEP = {"clock": zero_clock, "keep_windows": True}


def ramp(seconds: float) -> bytes:
    n = int(seconds * bench_wake.SAMPLE_RATE)
    return array("h", (i % 30000 for i in range(n))).tobytes()


def ramp_clip(cid="p", label="positive", seconds=3.0):
    return bench_wake.BenchClip(cid, label, ramp(seconds))


def accept(text="Луна"):
    return VerifyResult(True, "keyword", text, 0.4)


def reject(text="Лена"):
    return VerifyResult(False, "no_keyword", text, 0.4)


def test_bench_uses_the_production_verify_window():
    assert bench_wake.verify_window is production_verify.verify_window
    assert bench_wake.keyword_first is production_verify.keyword_first


def test_window_is_cut_at_the_event_cut_like_production():
    audio = ramp(3.0)
    det = FakeWakeWord([49], marker=False)  # the keyword ends at frame 49: 1.5 s
    r = bench_wake.StreamRunner(det, clock=zero_clock, keep_windows=True)
    r.feed(audio)
    r.flush()
    cut = round(1.5 * bench_wake.SAMPLE_RATE) * 2
    expected = production_verify.verify_window(audio[:cut], [audio[cut:]])
    assert r.detections[0].window == expected
    assert len(expected) == 44_800  # 1.4 s of PCM16


def test_window_survives_small_chunks_and_buffer_trimming():
    audio = ramp(30.0)
    whole = bench_wake.StreamRunner(FakeWakeWord([300], marker=False), keep_windows=True)
    whole.feed(audio)
    whole.flush()
    pieces = bench_wake.StreamRunner(FakeWakeWord([300], marker=False), keep_windows=True)
    for i in range(0, len(audio), 1000):
        pieces.feed(audio[i : i + 1000])
    pieces.flush()
    assert pieces.detections[0].window == whole.detections[0].window
    assert len(pieces._buf) <= (10 + 1) * bench_wake.SAMPLE_RATE * 2  # bounded, not the whole 30 s


def test_window_of_a_late_event_is_cut_at_the_keyword_end_not_at_the_event():
    audio = ramp(5.0)
    det = FakeWakeWord([99], marker=False, end_lag_samples=16_000)  # event 3.0 s, keyword end 2.0 s
    r = bench_wake.StreamRunner(det, clock=zero_clock, keep_windows=True)
    r.feed(audio)
    r.flush()
    assert r.detections[0].at_s == pytest.approx(3.0)
    assert r.detections[0].keyword_end_s == pytest.approx(2.0)
    cut = 2 * bench_wake.SAMPLE_RATE * 2
    assert r.detections[0].window == production_verify.verify_window(audio[:cut], [audio[cut:]])
    assert len(r.detections[0].window) == 44_800


def test_window_at_clip_end_is_shorter_not_padded():
    r = bench_wake.StreamRunner(FakeWakeWord([9], marker=False), keep_windows=True)
    r.feed(ramp(0.42))  # event at 0.3 s, only 0.12 s of audio after it
    r.flush()
    assert len(r.detections[0].window) == (4800 + 1920) * 2


def test_no_windows_without_keep_windows():
    r = bench_wake.StreamRunner(FakeWakeWord([9], marker=False))
    r.feed(ramp(1.0))
    r.flush()
    assert r.detections[0].window is None


def stage1(clips, trigger=(9,), long_files=(), **kw):
    return bench_wake.evaluate_config(
        cfg(), lambda _c: FakeWakeWord(trigger, marker=False), clips, long_files, **KEEP, **kw
    )


def test_stage2_counts_recall_false_rejects_and_negative_rejects():
    clips = [
        ramp_clip("a"),
        ramp_clip("b"),
        ramp_clip("n1", "negative"),
        ramp_clip("n2", "negative"),
    ]
    res = stage1(clips)
    assert res.hits == 2 and res.false_accepts_total == 2
    # order of verify calls: positives a, b, then negatives n1, n2
    verifier = FakeVerifier([accept(), reject(), reject(), accept("Луна, привет")])
    res.stage2 = bench_wake.verify_result(res, verifier, ["луна", "luna"])
    s2 = res.stage2
    assert s2.hits == 1 and s2.false_rejects == ["b"]
    assert s2.negative_rejects == 1 and s2.fa_clips == 1 and s2.fa_long == 0
    assert res.recall2 == 0.5
    assert res.fa_per_hour2 == pytest.approx(1 / (6.0 / 3600))
    assert s2.checks == 4 and len(verifier.calls) == 4
    assert s2.wall_s == [0.4] * 4


def test_stage2_long_negatives_count_as_long(tmp_path):
    path = tmp_path / "long.wav"
    write_wav(path, ramp(3.0))
    res = stage1([ramp_clip("p")], long_files=[path])
    assert res.fa_long == 1 and len(res.neg_windows) == 1
    s2 = bench_wake.verify_result(res, FakeVerifier(accept()), ["луна"])
    assert s2.fa_long == 1 and s2.fa_clips == 0
    s2 = bench_wake.verify_result(res, FakeVerifier(reject()), ["луна"])
    assert s2.fa_long == 0 and s2.negative_rejects == 1


def test_stage2_positive_with_several_events_hits_if_any_accepted():
    res = stage1([ramp_clip("a", seconds=4.0)], trigger=(9, 60), cooldown_s=0.0)
    assert len(res.pos_windows["a"]) >= 2
    s2 = bench_wake.verify_result(res, FakeVerifier([reject(), accept()]), ["луна"])
    assert s2.hits == 1 and not s2.false_rejects


def test_stage2_error_counts_as_reject_and_is_reported():
    res = stage1([ramp_clip("a")])
    s2 = bench_wake.verify_result(res, FakeVerifier(VerifyResult(False, "error")), ["луна"])
    assert s2.errors == 1 and s2.false_rejects == ["a"]


def test_stage2_cpu_seconds_per_check_use_the_cpu_clock():
    res = stage1([ramp_clip("a")])
    ticks = iter([0.0, 0.25])
    s2 = bench_wake.verify_result(
        res, FakeVerifier(accept()), ["луна"], cpu_clock=lambda: next(ticks)
    )
    assert s2.cpu_s == [0.25]


@pytest.mark.parametrize(
    ("text", "strict"),
    [
        ("Луна", True),
        ("Луна.", True),
        ("Луна, открой браузер", True),
        ("Луна! Открой", True),
        ("  луна ...", True),
        ("Луна открой браузер", False),
        ("Лена", False),
        ("", False),
        ("Луна-парк", False),
    ],
)
def test_keyword_strict_rule(text, strict):
    assert bench_wake.keyword_strict(text, ["луна", "luna"]) is strict


def test_strict_columns_use_the_stricter_rule():
    res = stage1([ramp_clip("a"), ramp_clip("n", "negative")])
    s2 = bench_wake.verify_result(res, FakeVerifier(accept("Луна открой")), ["луна"])
    assert s2.hits == 1 and s2.strict_hits == 0
    assert s2.fa_total == 1 and s2.strict_fa == 0


def test_select_for_verification_takes_top_n_by_stage1_recall():
    def r(thr, hits, fa):
        return bench_wake.ConfigResult(
            cfg(threshold=thr), 10, hits, fa_clips=fa, clip_negative_s=3600.0
        )

    results = [r(0.1, 5, 0), r(0.2, 10, 3), r(0.3, 10, 1), r(0.4, 9, 0)]
    top = bench_wake.select_for_verification(results, 2)
    assert [x.config.threshold for x in top] == [0.3, 0.2]
    assert len(bench_wake.select_for_verification(results, 99)) == 4


def test_markdown_without_stage2_is_unchanged_and_with_stage2_adds_a_table():
    plain = result()
    before = bench_wake.markdown_report([plain], synthetic=False)
    assert "Stage 2" not in before
    plain.stage2 = bench_wake.Stage2(
        hits=9, false_rejects=["x"], wall_s=[0.3, 0.5], cpu_s=[1.0], checks=1
    )
    after = bench_wake.markdown_report([plain], synthetic=False)
    assert after.startswith(before.rstrip("\n"))
    assert "Stage 2" in after and "false rejects: x" in after
    data = bench_wake.json_report([plain], synthetic=False, options={})
    assert data["results"][0]["stage2"]["false_rejects"] == ["x"]


GRID_ARGS = ["--modes", "grammar", "--min-word", "0.3", "--partials", "on"]


def test_main_verify_off_output_is_identical_to_default(tmp_path, capsys):
    write_wav(tmp_path / "clips" / "positive" / "a.wav", pcm(1.0))
    write_wav(tmp_path / "clips" / "negative" / "b.wav", pcm(1.0))
    argv = [*("--clips", str(tmp_path / "clips")), *GRID_ARGS, *("--thresholds", "0.5")]

    def fac(_c):
        return FakeWakeWord([10], marker=False)

    bench_wake.main(argv, factory=fac)
    default = capsys.readouterr().out
    bench_wake.main([*argv, "--verify", "off"], factory=fac)
    assert capsys.readouterr().out == default
    assert "Stage 2" not in default


def test_main_verify_local_loads_the_model_once_and_limits_top(tmp_path, capsys, monkeypatch):
    write_wav(tmp_path / "clips" / "positive" / "a.wav", ramp(2.0))
    write_wav(tmp_path / "clips" / "negative" / "b.wav", ramp(2.0))
    verifier = FakeVerifier(accept())
    loads = []

    def fake_loader(_config_path):
        loads.append(1)
        return verifier, ["луна"]

    monkeypatch.setattr(bench_wake, "make_local_verifier", fake_loader)
    argv = [
        *("--clips", str(tmp_path / "clips")),
        *GRID_ARGS,
        *("--thresholds", "0.3,0.5,0.7"),
        *("--verify", "local", "--verify-top", "2"),
        *("--out", str(tmp_path / "r.json")),
    ]
    code = bench_wake.main(argv, factory=lambda _c: FakeWakeWord([10], marker=False))
    assert code == 0 and len(loads) == 1
    out = capsys.readouterr().out
    assert "Stage 2" in out and "top 2 of 3" in out
    data = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert sum("stage2" in row for row in data["results"]) == 2
    assert len(verifier.calls) == 2 * 2  # two configs x (one positive + one negative event)
    assert data["options"]["verify"] == "local"


def test_verify_top_default_and_validation(tmp_path):
    assert bench_wake.build_parser().parse_args(["--synth"]).verify_top == 3
    assert bench_wake.build_parser().parse_args(["--synth"]).verify == "off"
    write_wav(tmp_path / "positive" / "a.wav", pcm(1.0))
    argv = ["--clips", str(tmp_path), "--verify", "local", "--verify-top", "0"]
    assert bench_wake.main(argv, factory=lambda _c: FakeWakeWord()) == 2
