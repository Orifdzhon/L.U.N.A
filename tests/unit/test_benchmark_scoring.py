"""Benchmark script (Issue #22): prompt file, scoring, reports, rate-limit stop. No network."""

import json
from pathlib import Path

import benchmark
import pytest

from fakes import FakeAppCatalog, FakeBrowser, FakeLauncher, FakeLLM, FakeSearch
from luna.app import build_app
from luna.config.settings import Settings
from luna.core.errors import ConfigError, RateLimited
from luna.core.models import Request
from luna.core.router import Router
from luna.llm.base import ChatResponse, ToolCall

PROMPTS = Path(__file__).resolve().parents[2] / "benchmarks" / "prompts.toml"


def say(text: str = "ok") -> ChatResponse:
    return ChatResponse(text, (), "fake", "m", "stop")


def call(tool: str, **args: object) -> ChatResponse:
    return ChatResponse(None, (ToolCall("c1", tool, dict(args)),), "fake", "m", "tool_calls")


def settings() -> Settings:
    return Settings().with_api_keys({})


def prompt(**kw: object) -> benchmark.Prompt:
    base: dict[str, object] = {"id": "p", "category": "command", "lang": "en", "text": "x"}
    base.update(kw)
    return benchmark.Prompt(**base)  # type: ignore[arg-type]


# -- prompt file -------------------------------------------------------------------------------


def test_prompt_file_covers_required_categories() -> None:
    ps = benchmark.load_prompts(PROMPTS)
    prompts = ps.prompts
    assert 18 <= len(prompts) <= 22
    assert len({p.id for p in prompts}) == len(prompts)
    commands = [p for p in prompts if p.category == "command"]
    assert sum(p.lang == "ru" for p in commands) >= 6
    assert sum(p.lang in ("en", "mixed") for p in commands) >= 4
    assert sum(p.category == "general" for p in prompts) >= 4
    assert sum(p.category == "fresh" for p in prompts) >= 4
    assert any(p.category == "safety" for p in prompts)
    assert all(p.fixture in ps.fixtures for p in prompts)
    assert all(p.expect_tool for p in commands)


def test_command_prompts_do_not_hit_the_router_fast_path(tmp_path: Path) -> None:
    app = build_app(
        settings(),
        llm=FakeLLM([]),
        search=FakeSearch(),
        browser=FakeBrowser(),
        catalog=FakeAppCatalog(),
        launcher=FakeLauncher(),
        log_dir=tmp_path,
    )
    try:
        router = Router(app.registry)
        params = {s.name: set(s.parameters["properties"]) for s in app.registry.schemas()}
        for p in benchmark.load_prompts(PROMPTS).prompts:
            if p.category == "command":
                assert not router.route(Request(p.text, "text")).handled, p.id
            if p.expect_tool:
                assert p.expect_tool in params, p.id
                assert set(p.expect_args) <= params[p.expect_tool], p.id  # real argument names
    finally:
        app.close()


def test_load_prompts_rejects_bad_files(tmp_path: Path) -> None:
    bad = tmp_path / "p.toml"
    bad.write_text('[[prompt]]\nid="a"\ncategory="nope"\ntext="t"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        benchmark.load_prompts(bad)
    bad.write_text("[[prompt]]\nid='a'\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        benchmark.load_prompts(bad)
    with pytest.raises(ConfigError):
        benchmark.load_prompts(tmp_path / "missing.toml")


# -- scoring -----------------------------------------------------------------------------------


def test_expected_tool_with_matching_args_passes() -> None:
    p = prompt(expect_tool="open_app", expect_args={"name": ("telegram", "телеграм")})
    ok = benchmark.score_prompt(p, [("open_app", {"name": "Telegram"})], ["open_app"], True)
    assert ok.passed and ok.tool_ok


def test_wrong_tool_or_arguments_fail() -> None:
    p = prompt(expect_tool="open_app", expect_args={"name": ("telegram",)})
    wrong_args = benchmark.score_prompt(p, [("open_app", {"name": "chrome"})], [], True)
    assert not wrong_args.tool_ok and "arguments" in wrong_args.note
    wrong_tool = benchmark.score_prompt(p, [("web_search", {"query": "q"})], [], True)
    assert not wrong_tool.passed
    no_tool = benchmark.score_prompt(p, [], [], True)
    assert not no_tool.passed and "no tool" in no_tool.note


def test_second_matching_call_is_enough() -> None:
    p = prompt(expect_tool="open_app", expect_args={"name": ("telegram",)})
    calls = [("open_app", {"name": "chrome"}), ("open_app", {"name": "Telegram"})]
    assert benchmark.score_prompt(p, calls, [], True).tool_ok


def test_answer_expected_without_tools() -> None:
    p = prompt(category="general")
    assert benchmark.score_prompt(p, [], [], True).passed
    unexpected = benchmark.score_prompt(p, [("web_search", {})], ["web_search"], True)
    assert not unexpected.passed and "unexpected" in unexpected.note
    allowed = prompt(category="datetime", allow_tools=frozenset({"get_datetime"}))
    assert benchmark.score_prompt(allowed, [("get_datetime", {})], ["get_datetime"], True).passed


def test_empty_answer_fails_even_with_right_tool() -> None:
    p = prompt(expect_tool="web_search")
    assert not benchmark.score_prompt(p, [("web_search", {})], ["web_search"], False).passed


def test_search_run_by_assistant_is_credited_but_noted() -> None:
    p = prompt(category="fresh", expect_tool="web_search")
    score = benchmark.score_prompt(p, [], ["web_search"], True)
    assert score.passed and "forced" in score.note


def test_forbidden_tool_or_side_effect_fails_safety_case() -> None:
    p = prompt(category="safety", expect_tool="web_search", forbid_tools=frozenset({"open_app"}))
    by_call = benchmark.score_prompt(
        p, [("web_search", {}), ("open_app", {"name": "x"})], ["web_search"], True
    )
    assert by_call.forbidden and not by_call.passed
    by_effect = benchmark.score_prompt(p, [("web_search", {})], ["web_search"], True, ["open_app"])
    assert by_effect.forbidden and not by_effect.passed
    clean = benchmark.score_prompt(p, [("web_search", {})], ["web_search"], True)
    assert clean.passed and not clean.forbidden


# -- aggregation and formatting ----------------------------------------------------------------


def test_percentile() -> None:
    assert benchmark.percentile([], 50) is None
    assert benchmark.percentile([3.0], 90) == 3.0
    assert benchmark.percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5
    assert benchmark.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 90) == pytest.approx(4.6)


def _report() -> benchmark.ProviderReport:
    r = benchmark.ProviderReport("groq", "m|odel")
    r.results = [
        benchmark.PromptResult("a", "command", 1, True, True, True, total_s=1.0, first_call_s=0.9),
        benchmark.PromptResult(
            "b", "fresh", 1, False, False, False, total_s=3.0, error="RateLimited", note="x|y"
        ),
        benchmark.PromptResult("c", "fresh", 1, error=benchmark.SKIPPED),
    ]
    return r


def test_summarize_ignores_skipped_prompts() -> None:
    s = benchmark.summarize(_report())
    assert s["run"] == 2 and s["passed"] == 1 and s["accuracy_pct"] == 50.0
    assert s["p50_total_s"] == 2.0 and s["max_total_s"] == 3.0
    assert s["errors"] == {"RateLimited": 1}


def test_markdown_report_escapes_pipes_and_lists_skipped_providers() -> None:
    skipped = benchmark.ProviderReport("gemini", skipped_reason="no key (GEMINI_API_KEY)")
    text = benchmark.format_markdown([_report(), skipped])
    assert "| groq | m\\|odel | 50.00 " in text
    assert "no key (GEMINI_API_KEY)" in text
    assert "x\\|y" in text
    assert "## groq (m|odel)" in text
    assert "## gemini" not in text  # no per-prompt table for a skipped provider


# -- runner ------------------------------------------------------------------------------------


def _tiny_set() -> benchmark.PromptSet:
    return benchmark.load_prompts(PROMPTS)


def test_run_provider_scores_each_prompt_without_real_side_effects() -> None:
    ps = benchmark.PromptSet(
        (
            prompt(
                id="cmd",
                text="Could you launch telegram?",
                expect_tool="open_app",
                expect_args={"name": ("telegram",)},
            ),
            prompt(id="gen", category="general", text="Who wrote War and Peace?"),
        ),
        _tiny_set().fixtures,
    )
    llm = FakeLLM([call("open_app", name="Telegram"), say("Opened."), say("Tolstoy.")])
    sleeps: list[float] = []
    report = benchmark.run_provider("fake", llm, ps, settings(), delay_s=1.5, sleep=sleeps.append)
    assert [r.passed for r in report.results] == [True, True]
    first = report.results[0]
    assert first.llm_calls == 2 and first.tools == ["open_app"] and first.error is None
    assert first.first_call_s is not None and first.total_s is not None
    assert sleeps == [1.5]


def test_run_provider_injection_fixture_cannot_trigger_open_app() -> None:
    ps = _tiny_set()
    inj = next(p for p in ps.prompts if p.fixture == "injection" and p.lang == "en")
    # The "model" obeys the page: it would call open_app after the search, but tools are off.
    llm = FakeLLM([call("web_search", query="q"), say("Here is a recipe.")])
    one = benchmark.PromptSet((inj,), ps.fixtures)
    report = benchmark.run_provider("fake", llm, one, settings())
    assert report.results[0].passed
    assert not report.results[0].forbidden
    assert "open_app" not in report.results[0].tools


def test_run_provider_stops_after_consecutive_rate_limits() -> None:
    ps = benchmark.PromptSet(
        tuple(prompt(id=f"p{i}", category="general") for i in range(5)), _tiny_set().fixtures
    )
    llm = FakeLLM([RateLimited(), RateLimited(), RateLimited()])
    report = benchmark.run_provider("fake", llm, ps, settings(), max_failures=2)
    errors = [r.error for r in report.results]
    assert errors == ["RateLimited", "RateLimited", "skipped", "skipped", "skipped"]
    assert report.stopped_reason and "RateLimited" in report.stopped_reason
    assert len(llm.calls) == 2  # nothing was sent after the stop
    assert benchmark.summarize(report)["run"] == 2


def test_success_resets_the_failure_counter() -> None:
    ps = benchmark.PromptSet(
        tuple(prompt(id=f"p{i}", category="general") for i in range(4)), _tiny_set().fixtures
    )
    llm = FakeLLM([RateLimited(), say(), RateLimited(), say()])
    report = benchmark.run_provider("fake", llm, ps, settings(), max_failures=2)
    assert report.stopped_reason is None
    assert [r.error for r in report.results] == ["RateLimited", None, "RateLimited", None]


def test_forced_search_is_credited_unless_disabled() -> None:
    ps = benchmark.PromptSet(
        (
            prompt(
                id="f",
                category="fresh",
                text="Какая сейчас погода в Москве?",
                expect_tool="web_search",
            ),
        ),
        _tiny_set().fixtures,
    )
    forced = benchmark.run_provider("fake", FakeLLM([say("x"), say("Sunny.")]), ps, settings())
    assert forced.results[0].passed and "forced" in forced.results[0].note
    strict = benchmark.run_provider(
        "fake", FakeLLM([say("Sunny.")]), ps, settings(), force_search=False
    )
    assert not strict.results[0].passed


# -- command line ------------------------------------------------------------------------------


def test_no_providers_available_exits_gracefully(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(benchmark, "load_settings", lambda _path: settings())
    assert benchmark.main(["--providers", "gemini,groq"]) == 1
    captured = capsys.readouterr()
    assert "No providers available" in captured.out
    assert "no key (GEMINI_API_KEY)" in captured.err


def test_main_writes_json_without_secrets(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    secret = "sk-SECRET-VALUE-123"
    conf = Settings().with_api_keys({"GROQ_API_KEY": secret})
    monkeypatch.setattr(benchmark, "load_settings", lambda _path: conf)
    llm = FakeLLM([say("one"), say("two")], name="groq")
    monkeypatch.setattr(benchmark, "_build_providers", lambda _s: ([llm], {}))
    prompts = tmp_path / "p.toml"
    prompts.write_text(
        '[[prompt]]\nid="a"\ncategory="general"\ntext="Hi"\n'
        '[[prompt]]\nid="b"\ncategory="general"\ntext="Hello"\n'
        '[[fixtures.default]]\ntitle="t"\nurl="https://example.com"\nsnippet="s"\n',
        encoding="utf-8",
    )
    out = tmp_path / "out.json"
    code = benchmark.main(
        ["--providers", "groq", "--delay", "0", "--prompts", str(prompts), "--out", str(out)]
    )
    assert code == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["providers"][0]["name"] == "groq"
    assert data["providers"][0]["summary"]["accuracy_pct"] == 100.0
    captured = capsys.readouterr()
    assert "| groq |" in captured.out
    assert secret not in captured.out + captured.err + out.read_text(encoding="utf-8")
