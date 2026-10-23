"""LLM provider benchmark: latency and tool-call accuracy (Issue #22, ADR 0002).

Usage::

    uv run python scripts/benchmark.py [--providers gemini,groq,local] [--chain] [--runs 1]
                                       [--out results.json] [--delay 2] [--real-search]

Each provider runs the prompts of ``benchmarks/prompts.toml`` through the real ``Assistant``
(tool-calling loop) built by ``luna.app.build_app``. The platform side is fake: apps are
"launched" and URLs "opened" by recorders, the app catalog is a small fixed list and web search
returns canned results (``--real-search`` uses ddgs). Only the LLM providers touch the network.
No app or browser is ever started and keys are never printed.

The Router fast path is not part of the measurement: the prompts are meant for the LLM path.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from luna.app import _build_providers, build_app
from luna.config.loader import load_settings
from luna.config.settings import Settings
from luna.core.errors import ConfigError, ProviderUnavailable, RateLimited
from luna.core.models import Request
from luna.core.router import needs_fresh_facts
from luna.llm.base import ChatResponse, LLMProvider, Message, ToolSchema
from luna.llm.chain import LLMChain
from luna.platform.base import AppEntry
from luna.search.base import SearchResult

DEFAULT_PROMPTS = Path(__file__).resolve().parent.parent / "benchmarks" / "prompts.toml"
CHAIN_NAME = "chain"
FAKE_APPS = (
    AppEntry("Telegram", "fake.telegram"),
    AppEntry("Google Chrome", "fake.chrome", ("chrome", "хром")),
    AppEntry("Calculator", "fake.calculator", ("калькулятор",)),
    AppEntry("Notepad", "fake.notepad", ("блокнот",)),
    AppEntry("Spotify", "fake.spotify"),
    AppEntry("Visual Studio Code", "fake.code", ("vscode", "code")),
)
CATEGORIES = ("command", "general", "fresh", "datetime", "safety")
SKIPPED = "skipped"


# -- prompt set --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Prompt:
    id: str
    category: str
    lang: str
    text: str
    expect_tool: str | None = None
    expect_args: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    allow_tools: frozenset[str] = frozenset()
    forbid_tools: frozenset[str] = frozenset()
    fixture: str = "default"


@dataclass(frozen=True)
class PromptSet:
    prompts: tuple[Prompt, ...]
    fixtures: Mapping[str, tuple[SearchResult, ...]]


def load_prompts(path: Path) -> PromptSet:
    """Read the prompt file; raises ``ConfigError`` on a malformed or inconsistent file."""
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read prompts from {path}: {exc}") from exc
    fixtures = {
        name: tuple(SearchResult(item["title"], item["url"], item["snippet"]) for item in items)
        for name, items in data.get("fixtures", {}).items()
    }
    prompts: list[Prompt] = []
    seen: set[str] = set()
    try:
        for raw in data.get("prompt", []):
            prompt = Prompt(
                id=raw["id"],
                category=raw["category"],
                lang=raw.get("lang", ""),
                text=raw["text"],
                expect_tool=raw.get("expect_tool"),
                expect_args={k: tuple(v) for k, v in raw.get("expect_args", {}).items()},
                allow_tools=frozenset(raw.get("allow_tools", ())),
                forbid_tools=frozenset(raw.get("forbid_tools", ())),
                fixture=raw.get("fixture", "default"),
            )
            if prompt.id in seen:
                raise ConfigError(f"duplicate prompt id {prompt.id!r}")
            if prompt.category not in CATEGORIES:
                raise ConfigError(f"prompt {prompt.id!r}: unknown category {prompt.category!r}")
            if prompt.fixture not in fixtures:
                raise ConfigError(f"prompt {prompt.id!r}: unknown fixture {prompt.fixture!r}")
            seen.add(prompt.id)
            prompts.append(prompt)
    except KeyError as exc:
        raise ConfigError(f"prompt entry without field {exc} in {path}") from exc
    if not prompts:
        raise ConfigError(f"no prompts in {path}")
    return PromptSet(tuple(prompts), fixtures)


# -- fakes: nothing real is ever launched or opened -------------------------------------------


class StaticCatalog:
    def __init__(self, apps: Sequence[AppEntry] = FAKE_APPS) -> None:
        self._apps = tuple(apps)

    def list_apps(self) -> tuple[AppEntry, ...]:
        return self._apps

    def refresh(self) -> None:
        return None


class RecordingLauncher:
    def __init__(self) -> None:
        self.launched: list[str] = []

    def launch(self, app: AppEntry) -> None:
        self.launched.append(app.name)


class RecordingBrowser:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def open_url(self, url: str) -> None:
        if url.split(":", 1)[0].lower() not in ("http", "https"):
            raise ValueError(f"only http/https URLs are allowed: {url!r}")
        self.urls.append(url)


class CannedSearch:
    """Returns the fixture selected by the runner; never touches the network."""

    name = "canned"

    def __init__(self, fixtures: Mapping[str, Sequence[SearchResult]]) -> None:
        self._fixtures = fixtures
        self.fixture = "default"

    def search(
        self, query: str, *, lang: str, max_results: int = 5, timeout_s: float = 5.0
    ) -> list[SearchResult]:
        return list(self._fixtures[self.fixture])[:max_results]


# -- measuring probe around an LLM -------------------------------------------------------------


@dataclass
class CallRecord:
    duration_s: float
    error: str | None = None  # exception class name only, never the message
    tool_calls: tuple[tuple[str, dict[str, Any]], ...] = ()
    answered_by: str | None = None


class ProbeLLM:
    """Wraps a provider (or the chain) and records every ``chat`` call."""

    def __init__(self, inner: LLMProvider, clock: Callable[[], float] = time.perf_counter) -> None:
        self.name = inner.name
        self.model = str(getattr(inner, "model", ""))
        self._inner = inner
        self._clock = clock
        self.records: list[CallRecord] = []

    def reset(self) -> None:
        self.records = []

    def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] = (),
        *,
        timeout_s: float,
        request_id: str | None = None,
    ) -> ChatResponse:
        start = self._clock()
        try:
            response = self._inner.chat(messages, tools, timeout_s=timeout_s)
        except Exception as exc:
            self.records.append(CallRecord(self._clock() - start, type(exc).__name__))
            raise
        calls = tuple((c.name, dict(c.arguments)) for c in response.tool_calls)
        self.records.append(CallRecord(self._clock() - start, None, calls, response.provider))
        return response


# -- scoring -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Score:
    tool_ok: bool
    forbidden: bool
    answered: bool
    passed: bool
    note: str = ""


def _args_match(expected: Mapping[str, Sequence[str]], args: Mapping[str, Any]) -> bool:
    for key, options in expected.items():
        value = str(args.get(key, "")).casefold()
        if not any(option.casefold() in value for option in options):
            return False
    return True


def score_prompt(
    prompt: Prompt,
    requested: Sequence[tuple[str, Mapping[str, Any]]],
    executed: Sequence[str],
    answered: bool,
    side_effects: Sequence[str] = (),
) -> Score:
    """Judge one turn.

    ``requested`` are the tool calls the model asked for, ``executed`` the tools that really ran
    (the Assistant may run ``web_search`` itself when the model skipped it), ``side_effects``
    names the recorders that fired ("open_app" / "open_website").
    """
    called = {name for name, _ in requested} | set(executed)
    forbidden = bool((called | set(side_effects)) & prompt.forbid_tools)
    note = ""
    if prompt.expect_tool is None:
        extra = called - prompt.allow_tools
        tool_ok = not extra
        if extra:
            note = "unexpected tool: " + ", ".join(sorted(extra))
    elif prompt.expect_tool not in called:
        tool_ok = False
        note = f"expected {prompt.expect_tool}, got " + (", ".join(sorted(called)) or "no tool")
    else:
        matching = [args for name, args in requested if name == prompt.expect_tool]
        if prompt.expect_args and matching:
            tool_ok = any(_args_match(prompt.expect_args, args) for args in matching)
            if not tool_ok:
                note = f"wrong arguments for {prompt.expect_tool}"
        else:
            tool_ok = True
            if not matching:
                note = "run by the assistant (forced), not requested by the model"
    if forbidden:
        note = ("forbidden tool ran; " + note).strip()
    return Score(tool_ok, forbidden, answered, tool_ok and not forbidden and answered, note)


# -- results and aggregation -------------------------------------------------------------------


@dataclass
class PromptResult:
    prompt_id: str
    category: str
    run: int
    passed: bool = False
    tool_ok: bool = False
    answered: bool = False
    forbidden: bool = False
    tools: list[str] = field(default_factory=list)
    first_call_s: float | None = None
    total_s: float | None = None
    llm_calls: int = 0
    retries: int = 0
    error: str | None = None
    answered_by: str | None = None
    note: str = ""


@dataclass
class ProviderReport:
    name: str
    model: str = ""
    skipped_reason: str | None = None
    stopped_reason: str | None = None
    results: list[PromptResult] = field(default_factory=list)


def percentile(values: Sequence[float], pct: float) -> float | None:
    """Linear-interpolation percentile; ``None`` for an empty sample."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * pct / 100
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def summarize(report: ProviderReport) -> dict[str, Any]:
    done = [r for r in report.results if r.error != SKIPPED]
    total = [r.total_s for r in done if r.total_s is not None]
    first = [r.first_call_s for r in done if r.first_call_s is not None]
    errors: dict[str, int] = {}
    for r in done:
        if r.error:
            errors[r.error] = errors.get(r.error, 0) + 1
    return {
        "prompts": len(report.results),
        "run": len(done),
        "passed": sum(r.passed for r in done),
        "accuracy_pct": round(100 * sum(r.passed for r in done) / len(done), 1) if done else None,
        "tool_accuracy_pct": (
            round(100 * sum(r.tool_ok for r in done) / len(done), 1) if done else None
        ),
        "p50_total_s": _round(percentile(total, 50)),
        "p90_total_s": _round(percentile(total, 90)),
        "max_total_s": _round(max(total) if total else None),
        "p50_first_call_s": _round(percentile(first, 50)),
        "retries": sum(r.retries for r in done),
        "errors": errors,
        "safety_violations": sum(r.forbidden for r in done),
    }


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def _verdict(r: PromptResult) -> str:
    return "skipped" if r.error == SKIPPED else ("yes" if r.passed else "NO")


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def format_markdown(reports: Sequence[ProviderReport]) -> str:
    """Summary table over all providers, then one table per provider with the prompts."""
    lines = [
        "## Summary",
        "",
        "| provider | model | accuracy % | tool ok % | p50 s | p90 s | max s | first call p50 s "
        "| retries | errors | status |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for report in reports:
        s = summarize(report)
        status = report.skipped_reason or report.stopped_reason or "ok"
        errs = ", ".join(f"{k} x{v}" for k, v in s["errors"].items()) or "-"
        lines.append(
            f"| {_cell(report.name)} | {_cell(report.model)} | {_fmt(s['accuracy_pct'])} "
            f"| {_fmt(s['tool_accuracy_pct'])} | {_fmt(s['p50_total_s'])} "
            f"| {_fmt(s['p90_total_s'])} | {_fmt(s['max_total_s'])} "
            f"| {_fmt(s['p50_first_call_s'])} | {s['retries']} | {_cell(errs)} "
            f"| {_cell(status)} |"
        )
    for report in reports:
        if not report.results:
            continue
        lines += [
            "",
            f"## {report.name} ({report.model})",
            "",
            "| prompt | category | pass | tools | first call s | total s | LLM calls | retries "
            "| error | note |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for r in report.results:
            lines.append(
                f"| {_cell(r.prompt_id)} | {r.category} | {_verdict(r)} "
                f"| {_cell(', '.join(r.tools) or '-')} | {_fmt(r.first_call_s)} "
                f"| {_fmt(r.total_s)} | {r.llm_calls} | {r.retries} | {_cell(r.error or '-')} "
                f"| {_cell(r.note or '-')} |"
            )
    return "\n".join(lines) + "\n"


def build_json(
    reports: Sequence[ProviderReport], options: Mapping[str, Any], prompts_path: Path
) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "prompts_file": prompts_path.name,
        "options": dict(options),
        "providers": [
            {
                "name": r.name,
                "model": r.model,
                "skipped_reason": r.skipped_reason,
                "stopped_reason": r.stopped_reason,
                "summary": summarize(r),
                "results": [asdict(x) for x in r.results],
            }
            for r in reports
        ],
    }


# -- running -----------------------------------------------------------------------------------


def run_provider(
    name: str,
    llm: LLMProvider,
    prompt_set: PromptSet,
    settings: Settings,
    *,
    runs: int = 1,
    delay_s: float = 0.0,
    max_failures: int = 3,
    real_search: bool = False,
    force_search: bool = True,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.perf_counter,
) -> ProviderReport:
    """Run every prompt against ``llm``. ``max_failures`` consecutive rate-limit / unavailable
    turns stop the provider (the remaining prompts are recorded as skipped)."""
    probe = ProbeLLM(llm, clock)
    report = ProviderReport(name, probe.model or name)
    canned = CannedSearch(prompt_set.fixtures)
    launcher, browser = RecordingLauncher(), RecordingBrowser()
    search_conf = settings.search.model_copy(
        update={} if real_search else {"fetch_pages": 0}  # no page downloads for canned results
    )
    search: Any = None if real_search else canned
    todo = [(run, p) for run in range(1, runs + 1) for p in prompt_set.prompts]
    failures = 0
    with tempfile.TemporaryDirectory(prefix="luna-bench-") as log_dir:
        app = build_app(
            settings.model_copy(update={"search": search_conf}),
            llm=probe,
            search=search,
            browser=browser,
            catalog=StaticCatalog(),
            launcher=launcher,
            log_dir=Path(log_dir),
        )
        try:
            for index, (run, prompt) in enumerate(todo):
                if report.stopped_reason:
                    report.results.append(
                        PromptResult(prompt.id, prompt.category, run, error=SKIPPED)
                    )
                    continue
                if index and delay_s > 0:
                    sleep(delay_s)
                canned.fixture = prompt.fixture
                app.assistant.clear_history()
                launcher.launched.clear()
                browser.urls.clear()
                probe.reset()
                started = clock()
                reply = app.assistant.answer(
                    Request(prompt.text, "text"),
                    force_search=force_search and needs_fresh_facts(prompt.text),
                )
                total = clock() - started
                result = _result_for(prompt, run, probe, reply, total, launcher, browser)
                report.results.append(result)
                if result.error in (RateLimited.__name__, ProviderUnavailable.__name__):
                    failures += 1
                    if failures >= max_failures:
                        report.stopped_reason = f"stopped after {failures} x {result.error}"
                else:
                    failures = 0
        finally:
            app.close()
    return report


def _result_for(
    prompt: Prompt,
    run: int,
    probe: ProbeLLM,
    reply: Any,
    total_s: float,
    launcher: RecordingLauncher,
    browser: RecordingBrowser,
) -> PromptResult:
    records = probe.records
    requested = [call for rec in records for call in rec.tool_calls]
    executed = [a.tool for a in reply.actions]
    errors = [rec.error for rec in records if rec.error]
    last_failed = bool(records) and records[-1].error is not None
    answered = bool(reply.text.strip()) and reply.provider is not None and not last_failed
    effects = (["open_app"] if launcher.launched else []) + (
        ["open_website"] if browser.urls else []
    )
    score = score_prompt(prompt, requested, executed, answered, effects)
    return PromptResult(
        prompt_id=prompt.id,
        category=prompt.category,
        run=run,
        passed=score.passed,
        tool_ok=score.tool_ok,
        answered=score.answered,
        forbidden=score.forbidden,
        tools=sorted({n for n, _ in requested} | set(executed)),
        first_call_s=records[0].duration_s if records else None,
        total_s=total_s,
        llm_calls=len(records),
        retries=sum(1 for i, rec in enumerate(records) if rec.error and i < len(records) - 1),
        error=errors[-1] if errors else None,
        answered_by=next((rec.answered_by for rec in reversed(records) if rec.answered_by), None),
        note=score.note,
    )


# -- command line ------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="benchmark", description="L.U.N.A. LLM provider benchmark")
    p.add_argument("--providers", help="comma-separated names (default: all of [llm].chain)")
    p.add_argument("--chain", action="store_true", help="also benchmark the full fallback chain")
    p.add_argument("--runs", type=int, default=1, help="repetitions of the prompt set")
    p.add_argument("--out", type=Path, help="write the results as JSON to this file")
    p.add_argument("--delay", type=float, default=2.0, help="seconds between prompts")
    p.add_argument("--max-failures", type=int, default=3, help="consecutive rate limits to stop")
    p.add_argument("--prompts", type=Path, default=DEFAULT_PROMPTS, help="prompt file (TOML)")
    p.add_argument("--config", type=Path, help="path to luna.toml")
    p.add_argument("--real-search", action="store_true", help="use real ddgs instead of canned")
    p.add_argument(
        "--no-force-search",
        action="store_true",
        help="do not let the Assistant run web_search itself for fresh-fact prompts "
        "(stricter: measures whether the model asks for the search)",
    )
    return p


def _select(settings: Settings, wanted: str | None) -> tuple[list[str], list[ProviderReport]]:
    """Provider names to run, plus skipped reports (unknown / no key)."""
    if wanted:
        names = [n.strip() for n in wanted.split(",") if n.strip()]
    else:
        names = list(settings.llm.chain)
    available = set(settings.available_providers())
    run: list[str] = []
    skipped: list[ProviderReport] = []
    for name in dict.fromkeys(names):
        conf = settings.llm.providers.get(name)
        if conf is None:
            skipped.append(ProviderReport(name, skipped_reason="unknown provider"))
        elif name not in available:
            env = (conf.api_key_env or "").strip()
            skipped.append(ProviderReport(name, conf.model, skipped_reason=f"no key ({env})"))
        else:
            run.append(name)
    return run, skipped


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.runs < 1 or args.max_failures < 1 or args.delay < 0:
        print("benchmark: --runs and --max-failures must be >= 1, --delay >= 0", file=sys.stderr)
        return 2
    try:
        from luna.ui.cli import configure_console_encoding

        configure_console_encoding()
        settings = load_settings(args.config)
        prompt_set = load_prompts(args.prompts)
    except (OSError, ConfigError) as exc:
        print(f"benchmark: {exc}", file=sys.stderr)
        return 2
    logging.getLogger("luna").setLevel(logging.ERROR)

    names, reports = _select(settings, args.providers)
    for r in reports:
        print(f"Skipping {r.name}: {r.skipped_reason}", file=sys.stderr)
    if not names:
        print("No providers available: set GEMINI_API_KEY / GROQ_API_KEY or run a local server.")
        return 1

    built, _missing = _build_providers(settings)
    by_name = {p.name: p for p in built}
    targets: list[tuple[str, LLMProvider]] = [(n, by_name[n]) for n in names if n in by_name]
    for n in names:
        if n not in by_name:
            reports.append(ProviderReport(n, skipped_reason="invalid configuration"))
    if args.chain and len(targets) > 1:
        targets.append((CHAIN_NAME, LLMChain([p for _, p in targets], time.monotonic)))
    try:
        for name, llm in targets:
            print(f"Running {name} ...", file=sys.stderr, flush=True)
            reports.append(
                run_provider(
                    name,
                    llm,
                    prompt_set,
                    settings,
                    runs=args.runs,
                    delay_s=args.delay,
                    max_failures=args.max_failures,
                    real_search=args.real_search,
                    force_search=not args.no_force_search,
                )
            )
    finally:
        for provider in built:
            closer = getattr(provider, "close", None)
            if callable(closer):
                closer()
    print(format_markdown(reports))
    if args.out:
        options = {
            "runs": args.runs,
            "delay_s": args.delay,
            "real_search": args.real_search,
            "force_search": not args.no_force_search,
            "chain": args.chain,
        }
        args.out.write_text(
            json.dumps(build_json(reports, options, args.prompts), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
