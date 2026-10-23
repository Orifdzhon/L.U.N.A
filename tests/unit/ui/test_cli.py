import io
import threading

from fakes import FakeAppCatalog, FakeBrowser, FakeLauncher, FakeLLM, FakeSearch
from luna.app import build_app
from luna.config.settings import Settings
from luna.core.events import ProviderSwitched
from luna.llm.base import ChatResponse
from luna.platform.base import AppEntry
from luna.ui.cli import PROMPT, Repl, configure_console_encoding


def say(text: str) -> ChatResponse:
    return ChatResponse(text, (), "fake", "m", "stop")


class Script:
    """input() replacement: items are lines, or exceptions to raise; EOF when exhausted."""

    def __init__(self, *items):
        self.items = list(items)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if not self.items:
            raise EOFError
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def make_app(tmp_path, llm=None):
    catalog = FakeAppCatalog([AppEntry("Telegram", "Telegram.TelegramDesktop")])
    browser = FakeBrowser()
    app = build_app(
        Settings().with_api_keys({}),
        llm=llm or FakeLLM([]),
        browser=browser,
        catalog=catalog,
        launcher=FakeLauncher(catalog),
        search=FakeSearch(),
        log_dir=tmp_path,
    )
    return app, browser


def run(tmp_path, script, llm=None):
    app, browser = make_app(tmp_path, llm)
    out = io.StringIO()
    try:
        code = Repl(app, script, out).run()
    finally:
        app.close()
    return code, out.getvalue(), browser


def test_router_command_and_exit(tmp_path):
    script = Script("открой ютуб", "exit")
    code, out, browser = run(tmp_path, script)
    assert code == 0
    assert any("youtube" in u for u in browser.urls)
    assert script.prompts == [PROMPT, PROMPT]
    assert out.strip()


def test_assistant_answer_cyrillic(tmp_path):
    llm = FakeLLM([say("Привет, чем помочь?")])
    code, out, _ = run(tmp_path, Script("привет", "выход"), llm)
    assert code == 0
    assert "Привет, чем помочь?" in out


def test_eof_quits_cleanly(tmp_path):
    code, _, _ = run(tmp_path, Script("   ", "/help"))
    assert code == 0


def test_commands(tmp_path):
    _, out, _ = run(tmp_path, Script("/help", "/providers", "/clear", "/nope", "exit"))
    assert "/providers" in out
    assert "Active LLM providers: fake" in out
    assert "Conversation cleared." in out
    assert "Unknown command /nope" in out


def test_clear_forgets_history(tmp_path):
    llm = FakeLLM([say("one"), say("two")])
    run(tmp_path, Script("привет", "/clear", "ещё", "exit"), llm)
    second = llm.calls[1].messages
    assert [m.content for m in second if m.role == "user"] == ["ещё"]


def test_ctrl_c_at_prompt_twice_quits(tmp_path):
    code, out, _ = run(tmp_path, Script(KeyboardInterrupt(), KeyboardInterrupt(), "never"))
    assert code == 0
    assert "Ctrl+C again" in out


def test_single_ctrl_c_then_input_continues(tmp_path):
    script = Script(KeyboardInterrupt(), "open telegram", KeyboardInterrupt(), "exit")
    code, _, _ = run(tmp_path, script)
    assert code == 0
    assert len(script.prompts) == 4


def test_ctrl_c_during_request_cancels_and_continues(tmp_path):
    app, _ = make_app(tmp_path, FakeLLM([]))
    out = io.StringIO()
    seen = []
    orig = app.orchestrator.handle

    def interrupted(request, cancel=None):
        seen.append(cancel)
        raise KeyboardInterrupt

    app.orchestrator.handle = interrupted
    script = Script("long question", "exit")
    try:
        code = Repl(app, script, out).run()
    finally:
        app.orchestrator.handle = orig
        app.close()
    assert code == 0
    assert "(cancelled)" in out.getvalue()
    assert seen[0].is_set()
    assert len(script.prompts) == 2  # the REPL went on to the next prompt


def test_real_cancel_stops_slow_turn(tmp_path):
    release = threading.Event()

    class Slow:
        name = "slow"

        def chat(self, messages, tools=(), *, timeout_s, request_id=None):
            release.wait(5)
            return say("late")

    app, _ = make_app(tmp_path, Slow())
    cancel = threading.Event()
    threading.Timer(0.1, cancel.set).start()
    try:
        from luna.core.models import Request

        reply = app.orchestrator.handle(Request("расскажи длинную историю", "text"), cancel)
        assert "late" not in reply.text
    finally:
        release.set()
        app.close()


def test_provider_switch_notice(tmp_path):
    app, _ = make_app(tmp_path, FakeLLM([say("ok")]))
    out = io.StringIO()
    orig = app.orchestrator.handle

    def handle(request, cancel=None):
        app.bus.publish(ProviderSwitched("gemini", "local", "down"))
        return orig(request, cancel)

    app.orchestrator.handle = handle
    try:
        Repl(app, Script("привет", "exit"), out).run()
    finally:
        app.orchestrator.handle = orig
        app.close()
    assert "offline mode" in out.getvalue()


def test_configure_console_encoding_reconfigures(monkeypatch):
    class Stream:
        def __init__(self):
            self.calls = []

        def reconfigure(self, **kw):
            self.calls.append(kw)

    class Broken:
        def reconfigure(self, **kw):
            raise ValueError

    a, b = Stream(), Stream()
    monkeypatch.setattr("sys.stdin", a)
    monkeypatch.setattr("sys.stdout", b)
    monkeypatch.setattr("sys.stderr", Broken())
    configure_console_encoding()
    assert a.calls == [{"encoding": "utf-8", "errors": "replace"}]
    assert b.calls == a.calls


def test_providers_command_shows_model_and_default_note(tmp_path):
    s = Settings().with_api_keys({})
    app = build_app(s, browser=FakeBrowser(), platform="linux", log_dir=tmp_path)
    out = io.StringIO()
    try:
        Repl(app, Script("/providers", "exit"), out).run()
    finally:
        app.close()
    text = out.getvalue()
    assert "local (qwen3-4b)" in text
    assert "default model" in text
    assert "luna.toml" in text


def test_ctrl_c_in_command_has_no_traceback(tmp_path):
    app, _ = make_app(tmp_path)
    out = io.StringIO()

    def boom():
        raise KeyboardInterrupt

    app.assistant.clear_history = boom
    try:
        code = Repl(app, Script("/clear", "exit"), out).run()
    finally:
        app.close()
    assert code == 0
    assert "(interrupted)" in out.getvalue()
