"""Composition root: the only module that knows the concrete classes (ARCHITECTURE section 1).

``build_app(settings)`` wires providers, tools, Router, Assistant, Orchestrator, ActionLog and
EventBus from :class:`~luna.config.settings.Settings`. Degraded start (section 8): an LLM
provider without its key is skipped with a warning; with no provider at all the app still
starts (the Router fast path works) and LLM answers apologise. Keyword-only arguments replace
the real adapters (network, browser, app launching) so tests can wire fakes.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from luna.config.settings import Settings
from luna.core.assistant import Assistant
from luna.core.clock import Clock
from luna.core.errors import ProviderUnavailable
from luna.core.events import EventBus
from luna.core.orchestrator import Orchestrator
from luna.core.router import Router
from luna.llm.base import ChatResponse, LLMProvider, Message, ToolSchema
from luna.llm.chain import LLMChain
from luna.llm.openai_compat import OpenAICompatProvider, Quirks
from luna.logs.action_log import ActionLog
from luna.logs.setup import default_log_dir
from luna.platform.base import AppCatalog, AppLauncher, BrowserOpener
from luna.search.base import SearchProvider
from luna.tools.builtin.datetime import GetDatetimeTool
from luna.tools.builtin.open_app import OpenAppTool
from luna.tools.builtin.open_website import OpenWebsiteTool
from luna.tools.builtin.web_search import WebSearchTool
from luna.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

ACTION_LOG_NAME = "actions.jsonl"
_CATALOG_JOIN_S = 1.0
_ORCHESTRATOR_CLOSE_S = 2.0


class _NoLLM:
    """Stands in for the chain when no provider is configured: every call is "unavailable"."""

    name = "none"

    def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] = (),
        *,
        timeout_s: float,
        request_id: str | None = None,
    ) -> ChatResponse:
        raise ProviderUnavailable("no LLM provider is configured")


@dataclass
class App:
    """Everything the UI needs; ``close()`` releases the threads and HTTP clients."""

    settings: Settings
    orchestrator: Orchestrator
    assistant: Assistant
    bus: EventBus
    registry: ToolRegistry
    action_log: ActionLog
    active_providers: list[str] = field(default_factory=list)
    missing_keys: dict[str, str] = field(default_factory=dict)  # provider -> env variable name
    warnings: list[str] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    _closers: list[LLMProvider] = field(default_factory=list, repr=False)
    _catalog_thread: threading.Thread | None = field(default=None, repr=False)
    _closed: bool = field(default=False, repr=False)

    @property
    def llm_available(self) -> bool:
        return bool(self.active_providers)

    def startup_lines(self) -> list[str]:
        """Human-readable startup summary. Names only, never key values."""
        lines: list[str] = []
        if self.active_providers:
            lines.append("LLM providers: " + " -> ".join(self.active_providers))
        else:
            lines.append(
                "LLM providers: none. Answers are unavailable; "
                "commands like 'open youtube' still work."
            )
        for name, env_name in self.missing_keys.items():
            lines.append(f"Missing key for '{name}' ({env_name}): provider skipped.")
        lines.extend(self.warnings)
        return lines

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.orchestrator.close(_ORCHESTRATOR_CLOSE_S)
        self.bus.close()
        thread = self._catalog_thread
        if thread is not None and thread.is_alive():
            thread.join(_CATALOG_JOIN_S)
        for provider in self._closers:
            closer = getattr(provider, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    logger.debug("provider close failed", exc_info=True)


def _build_providers(settings: Settings) -> tuple[list[OpenAICompatProvider], dict[str, str]]:
    available = set(settings.available_providers())
    providers: list[OpenAICompatProvider] = []
    missing: dict[str, str] = {}
    for name in settings.llm.chain:
        conf = settings.llm.providers[name]
        if name not in available:
            missing[name] = (conf.api_key_env or "").strip()
            logger.warning("LLM provider %r skipped: %s is not set", name, missing[name])
            continue
        try:
            providers.append(
                OpenAICompatProvider(
                    name,
                    conf.base_url,
                    conf.model,
                    settings.api_key(conf.api_key_env),
                    Quirks(conf.supports_tool_choice, conf.reasoning_effort, conf.max_tokens),
                    connect_timeout_s=settings.llm.connect_timeout_s,
                )
            )
        except Exception as exc:
            logger.warning("LLM provider %r skipped: %s", name, type(exc).__name__)
            missing[name] = f"invalid configuration ({type(exc).__name__})"
    return providers, missing


def _warm_catalog(catalog: AppCatalog) -> None:
    try:
        catalog.list_apps()  # reads the on-disk cache or scans Start Menu apps once
    except Exception as exc:
        logger.warning("app catalog scan failed: %s", type(exc).__name__)


def build_app(
    settings: Settings,
    *,
    llm: LLMProvider | None = None,
    search: SearchProvider | None = None,
    browser: BrowserOpener | None = None,
    catalog: AppCatalog | None = None,
    launcher: AppLauncher | None = None,
    log_dir: Path | None = None,
    platform: str | None = None,
    clock: Clock | None = None,
) -> App:
    """Build the whole object graph. Secrets must already be registered for log redaction."""
    clock = clock or time.monotonic
    platform = platform or sys.platform
    warnings: list[str] = []
    bus = EventBus()
    directory = log_dir if log_dir is not None else default_log_dir()
    action_log = ActionLog(directory / ACTION_LOG_NAME)
    registry = ToolRegistry(action_log)

    # LLM
    closers: list[LLMProvider] = []
    missing: dict[str, str] = {}
    active: list[str] = []
    chat: LLMProvider
    if llm is not None:
        chat, active = llm, [llm.name]
    else:
        providers, missing = _build_providers(settings)
        closers = list(providers)
        active = [p.name for p in providers]
        chat = LLMChain(providers, clock, bus) if providers else _NoLLM()

    # Tools
    if browser is None:
        from luna.platform.browser import WebBrowserOpener

        browser = WebBrowserOpener()
    registry.register(OpenWebsiteTool(browser, settings.router.site_aliases))
    registry.register(GetDatetimeTool())

    thread: threading.Thread | None = None
    if catalog is None and platform == "win32":
        from luna.platform.windows.apps import WindowsAppCatalog, WindowsAppLauncher

        catalog = WindowsAppCatalog()
        launcher = launcher or WindowsAppLauncher(catalog)
    if catalog is not None and launcher is not None:
        registry.register(
            OpenAppTool(
                catalog,
                launcher,
                settings.router.app_aliases,
                threshold=settings.router.threshold,
                margin=settings.router.margin,
            )
        )
        thread = threading.Thread(
            target=_warm_catalog, args=(catalog,), name="luna-app-catalog", daemon=True
        )
        thread.start()
    else:
        warnings.append("Launching apps is only supported on Windows: 'open <app>' is disabled.")

    if search is None and settings.search.provider == "ddgs":
        from luna.search.ddgs_search import DdgsSearch

        search = DdgsSearch()
    if search is not None:
        registry.register(
            WebSearchTool(
                search,
                max_results=settings.search.max_results,
                fetch_pages=settings.search.fetch_pages,
            )
        )
    else:
        warnings.append(
            f"Search provider '{settings.search.provider}' is not available yet: "
            "web search is disabled."
        )
    for text in warnings:
        logger.warning(text)

    assistant = Assistant(chat, registry, history_turns=settings.app.history_turns)
    orchestrator = Orchestrator(
        Router(registry), assistant, bus, settings.app.request_timeout_s, timer=clock
    )
    return App(
        settings=settings,
        orchestrator=orchestrator,
        assistant=assistant,
        bus=bus,
        registry=registry,
        action_log=action_log,
        active_providers=active,
        missing_keys=missing,
        warnings=warnings,
        tool_names=sorted(s.name for s in registry.schemas()),
        _closers=closers,
        _catalog_thread=thread,
    )
