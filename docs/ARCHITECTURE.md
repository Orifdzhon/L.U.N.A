# L.U.N.A. — Architecture

> Status: **proposed** (v0.1, 2026-10-22). Author: architect agent.
> Inputs: [PRODUCT_SPEC.md](PRODUCT_SPEC.md), owner interview 2026-10-22.
> Decisions are recorded as ADRs in [docs/adr/](adr/). Nothing here is final until
> the owner accepts the ADRs.

## 0. Summary

L.U.N.A. is a single Python process (package `luna`, Python 3.13, managed by `uv`)
built around a **text-first core**: a request is a piece of text, the core turns it
into an answer and/or tool calls, and returns text. Voice (wake word, STT, TTS) and
the desktop shell (tray, window, autostart) are **adapters around that core** and are
added in later slices.

| Concern | Choice (default) | Alternatives / fallback | ADR |
|---|---|---|---|
| Runtime | Python 3.13, `uv`, `ruff`, `pytest`, src layout | Python 3.12 if a dependency breaks | [0001](adr/0001-python-version-packaging-tooling.md) |
| LLM | One OpenAI-compatible client; chain Gemini `gemini-3.5-flash-lite` → Groq `openai/gpt-oss-120b` → Ollama `qwen3:4b` | native `google-genai` SDK if the compat layer breaks | [0002](adr/0002-llm-providers-and-tool-calling.md) |
| STT | `faster-whisper` `large-v3-turbo` on GPU (int8_float16) | Groq `whisper-large-v3-turbo`; CPU `small` int8 | [0003](adr/0003-speech-to-text.md) |
| Wake word | Hotkey first; then Vosk `small-ru` grammar spotter ("луна"); then a custom ONNX model | Porcupine rejected (free tier closed) | [0004](adr/0004-wake-word.md) |
| TTS | Piper (`piper-tts`, out-of-process), CC0 voices | Windows SAPI voices (Irina ru-RU) | [0005](adr/0005-text-to-speech.md) |
| Web search | `ddgs` metasearch + `trafilatura` extraction | self-hosted SearXNG; Tavily free key | [0006](adr/0006-web-search.md) |
| Desktop shell | PySide6 (tray + window), Win32 `RegisterHotKey`, HKCU `Run` autostart | tkinter + pystray | [0007](adr/0007-desktop-shell.md) |
| Open apps/sites | `Get-StartApps` index + aliases + `rapidfuzz`; `webbrowser` for sites | `.lnk` scan | [0008](adr/0008-opening-apps-and-websites.md) |
| Concurrency | **Threads + queues**, Qt on the main thread | asyncio (rejected, §5) | this doc |

## 1. Components and responsibilities

```
                 ┌───────────────────────── Input adapters ─────────────────────────┐
                 │ CLI REPL (slice 1) · Qt text box (slice 5) · Voice front-end      │
                 │                                   (mic → wake word/hotkey → VAD → STT) │
                 └──────────────────────────────┬──────────────────────────────────┘
                                                │ Request(text, source, lang?)
                                                ▼
┌──────────────────────────────────── luna.core ────────────────────────────────────┐
│ Orchestrator ── Router (fast path: "открой X"/"open X") ──► ToolRegistry ─► Tools │
│      │                       │ no match                        ▲                   │
│      │                       ▼                                 │ tool calls        │
│      │                 Assistant (LLM loop with tools) ────────┘                   │
│      │                       │ uses LLMChain (fallback between providers)          │
│      ▼                                                                             │
│ Reply(text, lang, actions) ──► ActionLog (JSONL) · EventBus (status events)        │
└───────────────────────────────────────┬───────────────────────────────────────────┘
                                         ▼
                 Output adapters: CLI print · TTS (Piper) · Qt window/tray
```

| Component | Package | Responsibility |
|---|---|---|
| Composition root | `luna.app` | Reads config, builds providers, wires adapters, starts threads. The only place that knows concrete classes. |
| Config | `luna.config` | Loads `luna.toml` + `.env`, validates with pydantic, exposes typed `Settings`. |
| Orchestrator | `luna.core.orchestrator` | Runs one request end to end: router → assistant → reply; emits status events; enforces timeouts and cancellation. |
| Router | `luna.core.router` | Rule-based fast path for simple commands (F3, F4) without an LLM; RU/EN verb patterns + alias/fuzzy lookup. Also flags "needs fresh facts" (F6 heuristics). |
| Assistant | `luna.core.assistant` | LLM conversation loop with tool calling, system prompt, short dialog history, web-content isolation rule (§9). |
| LLM providers | `luna.llm` | `LLMProvider` protocol, `OpenAICompatProvider`, `LLMChain` (fallback + cooldown). |
| Tools | `luna.tools` | `Tool` protocol, `ToolRegistry`, safety levels, built-in tools: `open_app`, `open_website`, `web_search`, `get_datetime`. |
| Search | `luna.search` | `SearchProvider` protocol, `DdgsSearch`, `SearxngSearch`, page fetch + extraction. |
| Platform | `luna.platform` | OS adapters behind protocols: app catalog/launcher, browser, hotkey, autostart. `luna.platform.windows` implements them. |
| Audio | `luna.audio` | Microphone capture (`sounddevice`), ring buffer, VAD, playback. |
| STT | `luna.stt` | `STTProvider` protocol, `FasterWhisperSTT`, `GroqWhisperSTT`. |
| TTS | `luna.tts` | `TTSProvider` protocol, `PiperTTS` (subprocess worker), `SapiTTS`. |
| Wake word | `luna.wakeword` | `WakeWordDetector` protocol, `VoskKeywordSpotter`, `OnnxWakeWord`. |
| Voice front-end | `luna.voice` | State machine IDLE → LISTENING → TRANSCRIBING → (core) → SPEAKING; glues audio, wake word, VAD, STT, TTS. |
| UI | `luna.ui` | `cli` (REPL, slice 1) and `qt` (tray, popup window, status indicator, slice 5). |
| Logging | `luna.logs` | App log (rotating text) and action log (JSONL, F9). |

## 2. Package layout (src layout)

```
pyproject.toml            # uv project, ruff + pytest config, entry point `luna = "luna.__main__:main"`
uv.lock
.python-version           # 3.13
luna.example.toml         # committed template; real luna.toml is gitignored
.env.example              # GEMINI_API_KEY=, GROQ_API_KEY=, TAVILY_API_KEY= (empty)
src/luna/
  __init__.py
  __main__.py             # `python -m luna [--text] [--config PATH]`
  app.py                  # composition root
  config/
    settings.py           # pydantic models: Settings, LLMSettings, VoiceSettings, ...
    loader.py             # tomllib + .env → Settings; config search paths
  core/
    models.py             # Request, Reply, ToolCall, ToolResult, Message (dataclasses)
    events.py             # EventBus + event types (StatusChanged, ReplyReady, ...)
    orchestrator.py
    router.py
    assistant.py
    prompts.py            # system prompt templates (RU/EN)
    errors.py             # LunaError hierarchy
  llm/
    base.py               # LLMProvider protocol, ChatResponse
    openai_compat.py      # Gemini / Groq / Ollama via `openai` SDK
    chain.py              # LLMChain: ordered fallback + cooldown
  tools/
    base.py               # Tool protocol, SafetyLevel, ToolSpec
    registry.py
    builtin/open_app.py, open_website.py, web_search.py, datetime.py
  search/
    base.py               # SearchProvider, SearchResult
    ddgs_search.py
    searxng.py
    fetch.py              # httpx fetch + trafilatura extract, size/time limits
  platform/
    base.py               # AppCatalog, AppLauncher, BrowserOpener, HotkeyListener, Autostart
    windows/apps.py       # Get-StartApps index, launch via shell:AppsFolder
    windows/hotkey.py     # RegisterHotKey via ctypes
    windows/autostart.py  # HKCU\...\Run via winreg
  audio/
    capture.py            # sounddevice InputStream → frames queue
    ringbuffer.py
    vad.py                # Silero VAD (onnx) endpointing
    playback.py
  stt/
    base.py, faster_whisper_stt.py, groq_stt.py
  tts/
    base.py, piper_tts.py (subprocess worker), sapi_tts.py
  wakeword/
    base.py, vosk_spotter.py, onnx_wakeword.py
  voice/
    frontend.py           # voice state machine
  ui/
    cli.py
    qt/app.py, tray.py, window.py
  logs/
    setup.py              # logging config, secret redaction filter
    action_log.py         # JSONL writer
  data/
    site_aliases.toml     # "ютуб" → https://www.youtube.com, ...
    app_aliases.toml      # "хром" → "Google Chrome", "телега" → "Telegram", ...
tests/
  unit/ ...               # mirrors src/luna, no network/mic/GPU
  integration/            # marked `live` / `gpu` / `audio`; skipped by default
  fixtures/               # wav clips, recorded HTTP responses, fake Get-StartApps JSON
  fakes.py                # FakeLLM, FakeSTT, FakeTTS, FakeSearch, FakeAppCatalog, ...
docs/
  manual-checks.md        # owner's manual checklist (mic, GPU, tray) — added in slice 3
```

Rule: only `luna.platform.windows.*` (and `luna.tts.sapi_tts`) may import Windows-only
modules (`winreg`, `ctypes.windll`). Everything else is OS-neutral.

## 3. Key interfaces (sketches)

Short `typing.Protocol` sketches; the developer may adjust names but not the shape
without an ADR/architecture update.

```python
# luna/core/models.py
@dataclass(frozen=True)
class Request:
    text: str
    source: Literal["text", "voice"]
    lang: str | None = None          # "ru" / "en" hint from STT
    request_id: str = field(default_factory=new_id)

@dataclass
class Reply:
    text: str
    lang: str                        # language to speak in
    actions: list["ToolResult"] = field(default_factory=list)
    provider: str | None = None      # which LLM answered, or "router"
```

```python
# luna/llm/base.py
@dataclass(frozen=True)
class ToolSchema:                    # JSON-schema function definition
    name: str
    description: str
    parameters: dict[str, Any]

@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None

@dataclass(frozen=True)
class ChatResponse:
    text: str | None
    tool_calls: tuple[ToolCall, ...]
    provider: str
    model: str

class LLMProvider(Protocol):
    name: str
    def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] = (),   # empty = tools disabled for this call
        *,
        timeout_s: float,
    ) -> ChatResponse: ...
    # Raises RateLimited(retry_after_s) / ProviderUnavailable / ProviderError.
```

```python
# luna/stt/base.py
@dataclass(frozen=True)
class Transcript:
    text: str
    lang: str                 # detected language
    confidence: float | None

class STTProvider(Protocol):
    name: str
    def transcribe(self, pcm16: bytes, sample_rate: int = 16_000,
                   *, lang_hint: str | None = None, prompt: str | None = None) -> Transcript: ...
```

```python
# luna/tts/base.py
class TTSProvider(Protocol):
    name: str
    def synthesize(self, text: str, lang: str) -> AudioClip: ...  # pcm16 + sample_rate
    # Playback is separate (luna.audio.playback) so it can be interrupted.
```

```python
# luna/wakeword/base.py
@dataclass(frozen=True)
class WakeEvent:
    keyword: str
    score: float
    end_sample: int           # position in the capture stream, used to cut the command audio

class WakeWordDetector(Protocol):
    name: str
    frame_samples: int        # expected frame size at 16 kHz mono
    def process(self, frame: np.ndarray) -> WakeEvent | None: ...   # called on audio thread data
    def reset(self) -> None: ...
```

```python
# luna/search/base.py
@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    content: str | None = None   # extracted page text (truncated), filled by fetch step

class SearchProvider(Protocol):
    name: str
    def search(self, query: str, *, lang: str, max_results: int = 5,
               timeout_s: float = 5.0) -> list[SearchResult]: ...
```

```python
# luna/tools/base.py
class SafetyLevel(enum.Enum):
    SAFE = "safe"              # execute immediately
    SENSITIVE = "sensitive"    # needs confirmation (no such tools in MVP)
    DANGEROUS = "dangerous"    # explicit on-screen confirmation; registry refuses in MVP

@dataclass(frozen=True)
class ToolResult:
    tool: str
    ok: bool
    summary: str               # short text for the LLM / user
    data: dict[str, Any] = field(default_factory=dict)
    untrusted: bool = False    # True for web content → disables tools on the next LLM call

class Tool(Protocol):
    name: str
    description: str
    parameters: dict[str, Any]         # JSON schema
    safety: SafetyLevel
    def run(self, args: dict[str, Any], ctx: "ToolContext") -> ToolResult: ...

class Confirmer(Protocol):             # UI provides it; CLI asks y/n, Qt shows a dialog
    def confirm(self, prompt: str) -> bool: ...

class ToolRegistry:
    def register(self, tool: Tool) -> None: ...      # refuses DANGEROUS while allow_dangerous=False
    def schemas(self) -> list[ToolSchema]: ...
    def execute(self, call: ToolCall, ctx: "ToolContext") -> ToolResult: ...
    # execute(): unknown tool → error result; validates args against the schema;
    # SENSITIVE/DANGEROUS → ctx.confirmer.confirm(...) first; always writes the action log.
```

```python
# luna/platform/base.py
@dataclass(frozen=True)
class AppEntry:
    name: str                 # display name, possibly localized ("Калькулятор")
    app_id: str               # OS-specific launch id (AUMID on Windows)
    aliases: tuple[str, ...] = ()

class AppCatalog(Protocol):
    def list_apps(self) -> list[AppEntry]: ...       # cached; refresh() re-scans
    def refresh(self) -> None: ...

class AppLauncher(Protocol):
    def launch(self, app: AppEntry) -> None: ...

class BrowserOpener(Protocol):
    def open_url(self, url: str) -> None: ...        # http/https only

class HotkeyListener(Protocol):
    def start(self, hotkey: str, on_press: Callable[[], None]) -> None: ...
    def stop(self) -> None: ...

class Autostart(Protocol):
    def is_enabled(self) -> bool: ...
    def enable(self, command: str) -> None: ...
    def disable(self) -> None: ...
```

## 4. Pipeline and event flow

### 4.1 Text request (slice 1)

1. Input adapter creates `Request(text, source="text")` and calls
   `orchestrator.handle(request)` (on the worker thread).
2. **Router** tries the fast path:
   - verb patterns: `открой|запусти|включи|open|launch|start` + target;
   - target resolution: site aliases → URL; app aliases + fuzzy match over the app
     catalog (score ≥ threshold, default 85); explicit URLs/domains (`github.com`).
   - On a confident match it calls the tool through `ToolRegistry` and returns a
     templated reply ("Открываю YouTube"). No LLM call. Target latency < 100 ms.
   - It also tags requests that need fresh facts (keywords like курс, цена, погода,
     сегодня, сейчас, price, rate, weather, today, news) → `force_search=True`.
3. Otherwise the **Assistant** runs the LLM loop (max 3 tool rounds):
   - messages = system prompt + last N turns (default 6) + user text;
   - tools = registry schemas; if `force_search`, the system prompt requires a
     `web_search` call first (we do not rely on `tool_choice`, Ollama ignores it);
   - each tool call → `ToolRegistry.execute` → `ToolResult` appended as a `tool` message;
   - **if any result is `untrusted` (web content), the next LLM call is made with
     `tools=()`**, so web text cannot trigger actions (spec §3.3, ADR 0006);
   - final text → `Reply`.
4. Orchestrator writes the action log, emits `ReplyReady`, returns the reply.

### 4.2 Voice request (slices 3–4)

```
mic (sounddevice callback, 16 kHz mono, 30 ms frames)
  └─► frames queue ─► VoiceFrontEnd thread
          IDLE:       feed WakeWordDetector  (or hotkey event)
          on wake ──► LISTENING: collect frames after WakeEvent.end_sample (+300 ms pre-roll),
                      VAD endpointing: stop at 700 ms silence or 10 s max
          ──────────► TRANSCRIBING: STTProvider.transcribe(...)
          ──────────► Request(text, source="voice", lang=transcript.lang) → Orchestrator
          ──────────► SPEAKING: TTS synthesize per sentence → playback (interruptible)
          ──────────► IDLE (optionally a follow-up window, owner decision Q3)
```

"Луна, открой YouTube" said in one breath works because the detector reports the
sample position where the keyword ended and capture continues into the ring buffer.
If STT text still starts with "Луна"/"Luna", the router strips it.

### 4.3 Status events

`EventBus` (thread-safe, `queue.Queue` per subscriber) carries
`StatusChanged(IDLE|LISTENING|THINKING|SPEAKING|ERROR)`, `TranscriptReady`,
`ReplyReady`, `ActionExecuted`, `ProviderSwitched`. The CLI prints them; the Qt UI
receives them via a `QObject` bridge that re-emits them as Qt signals on the GUI thread.

## 5. Concurrency model — threads, not asyncio

**Decision:** plain threads with `queue.Queue` hand-off; one request at a time.

| Thread | Work |
|---|---|
| Main | Qt event loop (slice 5) or the CLI REPL (slice 1–4) |
| PortAudio callback | copies frames to a queue only (never blocks) |
| `voice` | wake word + VAD + STT + TTS playback state machine |
| `worker` | orchestrator: router, LLM calls, tools (single-threaded → no races on dialog state) |
| `hotkey` | Win32 message loop for `RegisterHotKey` |
| Piper subprocess | separate **process** (license boundary, ADR 0005) |

Why not asyncio:
- The heavy work is blocking C/C++ code (CTranslate2, ONNX Runtime, Vosk, PortAudio)
  that releases the GIL; asyncio would wrap all of it in `run_in_executor` anyway.
- Qt has its own event loop; mixing it with asyncio needs `qasync` or a second loop
  thread — extra moving parts for one developer.
- The workload is one user and one request at a time; there is no fan-out that asyncio
  would make cheaper. The only parallel I/O (fetching 3 result pages) uses a small
  `ThreadPoolExecutor`.

Cancellation: every request carries a `threading.Event`; "stop" (hotkey, tray button,
later "Луна, стоп") sets it, playback stops immediately, LLM/HTTP calls are abandoned
after their timeout and their results discarded.

## 6. Configuration

- `luna.toml` (TOML, read with stdlib `tomllib`), validated into pydantic models.
  Search order: `--config PATH` → `%APPDATA%\LUNA\luna.toml` → `./luna.toml`.
  A committed `luna.example.toml` documents every key.
- `.env` (gitignored) for secrets only: `GEMINI_API_KEY`, `GROQ_API_KEY`,
  `TAVILY_API_KEY`. Loaded by `pydantic-settings`; keys are `SecretStr`, never logged.
- Aliases in `data/*.toml` (shipped defaults) merged with user overrides in `luna.toml`.

```toml
[llm]
chain = ["gemini", "groq", "ollama"]
[llm.providers.gemini]
base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
model = "gemini-3.5-flash-lite"
api_key_env = "GEMINI_API_KEY"
[llm.providers.groq]
base_url = "https://api.groq.com/openai/v1"
model = "openai/gpt-oss-120b"
api_key_env = "GROQ_API_KEY"
[llm.providers.ollama]
base_url = "http://localhost:11434/v1"
model = "qwen3:4b"

[stt]
provider = "faster_whisper"     # or "groq"
model = "large-v3-turbo"
device = "cuda"                 # "cpu" → model "small", compute_type "int8"
compute_type = "int8_float16"

[tts]
provider = "piper"              # or "sapi"
voice_ru = "ru_RU-dmitri-medium"
voice_en = "en_US-ljspeech-medium"

[wakeword]
engine = "vosk"                 # "vosk" | "onnx" | "off"
keywords = ["луна"]
hotkey = "ctrl+alt+space"

[search]
provider = "ddgs"               # "ddgs" | "searxng" | "tavily"
max_results = 5
fetch_pages = 3

[ui]
autostart = false
```

## 7. Logging and the action log

- **App log:** stdlib `logging`, `RotatingFileHandler` at `%LOCALAPPDATA%\LUNA\logs\luna.log`
  (5 × 2 MB), console handler in CLI mode. A redaction filter masks anything that
  looks like an API key and the values of known secret settings.
- **Action log (F9):** one JSON object per line in `%LOCALAPPDATA%\LUNA\logs\actions.jsonl`:
  `ts, request_id, source, text (user request), tool, args, safety, confirmed, ok,
  summary, duration_ms, provider`. Written by `ToolRegistry.execute` for every tool
  call, including refused ones. Rotated monthly.
- Timing spans (wake→end of speech→STT→LLM→first audio) are logged at INFO to
  validate latency targets N4.

## 8. Error handling and provider fallback

- Exceptions: `LunaError` → `ProviderError` → `RateLimited(retry_after_s)`,
  `ProviderUnavailable` (network, 5xx, timeout), `ConfigError`, `ToolError`.
- **`LLMChain`** tries providers in configured order. On `RateLimited` /
  `ProviderUnavailable` the provider is put on cooldown (`Retry-After` or 60 s, doubling
  up to 15 min) and the next provider is tried within the same request. A request
  never waits on a cooling provider. `ProviderSwitched` is emitted so the UI can show
  "offline mode". Errors that are our fault (400 bad request, schema errors) are not
  retried on other providers more than once.
- The same `FallbackChain` helper is reused for STT (local ↔ Groq) and search
  (ddgs ↔ SearXNG/Tavily). TTS falls back Piper → SAPI.
- Per-stage timeouts (defaults): STT 5 s, LLM call 8 s, search 5 s, page fetch 4 s
  each, total request 20 s. On timeout the user hears a short apology instead of silence.
- Tool failures are returned to the LLM as `ToolResult(ok=False, summary=...)`; the
  LLM phrases the error. Router fast-path failures fall through to the LLM path.
- Startup is degraded, not fatal: missing API key → provider skipped with a warning;
  no microphone → text mode only; no CUDA → STT on CPU.

## 9. Safety

- Only tools in `ToolRegistry`; no shell, no file-system tool in the MVP.
- `ToolRegistry.register` refuses `DANGEROUS` tools unless `allow_dangerous=True`
  (not exposed in MVP config). `SENSITIVE` tools require `Confirmer.confirm`.
- `open_website` accepts only `http`/`https` URLs; `open_app` only entries from the
  app catalog (never a raw path or command line from the LLM).
- Web content isolation: search/page text is wrapped as quoted data in the tool
  message, marked `untrusted`, and the following LLM call has no tools (§4.1).
- Logs never contain secrets.

## 10. Testing strategy

| Level | What | How |
|---|---|---|
| Unit (CI, every PR) | router patterns RU/EN, alias + fuzzy resolution, ToolRegistry safety rules, LLMChain fallback/cooldown, assistant loop (tool round-trips, untrusted → no tools), config loading, action log format, voice state machine with synthetic frames | `pytest`; fakes in `tests/fakes.py` (`FakeLLM` replays scripted `ChatResponse`s, `FakeSTT`, `FakeTTS`, `FakeSearch`, `FakeAppCatalog`, `FakeLauncher`, `FakeBrowser`, fake clock) |
| Provider contract (CI) | request/response mapping of `OpenAICompatProvider`, ddgs/SearXNG parsing, Groq STT client | recorded HTTP fixtures via `respx` (httpx mock); no network |
| Platform (CI on `windows-latest`) | `Get-StartApps` JSON parsing, autostart registry logic | parsing tested on fixture JSON; registry calls behind an injectable `winreg`-like wrapper, real registry never touched in tests |
| Integration (opt-in) | real Gemini/Groq/Ollama tool calls, real search, faster-whisper on fixture WAVs | markers `live`, `gpu`, `audio`; `pytest -m live` run manually with keys |
| Manual (owner) | microphone, wake word false accepts/hour, latency, tray, autostart, real app launch | checklist `docs/manual-checks.md`, listed in each PR |

CI: GitHub Actions on `windows-latest`, `uv sync --frozen`, `ruff check`,
`ruff format --check`, `pytest -m "not live and not gpu and not audio"`.
Heavy ML packages are optional dependency groups (`voice`, `ui`) so the core test job
stays fast; voice-module unit tests import them lazily and use fakes.

## 11. VRAM and resource budget (RTX 2060, 6 GB — confirmed 6144 MiB by `nvidia-smi`)

| Item | Device | VRAM (est.) | RAM (est.) | Notes |
|---|---|---|---|---|
| Windows desktop + browser | GPU | 0.3–0.6 GB | — | already used before L.U.N.A. starts |
| faster-whisper `large-v3-turbo` int8_float16 | GPU | ~1.0–1.5 GB | ~1 GB | **estimate, measure in slice 3**; loaded once, kept warm |
| Wake word (Vosk small-ru, 45 MB) or ONNX | CPU | 0 | ~150 MB | ~5–10 % of one core (to measure) |
| Silero VAD | CPU | 0 | ~20 MB | |
| Piper medium voices (2) | CPU | 0 | ~200 MB | separate process |
| Qt UI | CPU | ~0 | ~100 MB | |
| **Default total (cloud LLM)** | | **~1.5 GB** | ~1.5 GB | leaves >4 GB for games/other apps |
| Ollama `qwen3:4b` (2.5 GB Q4 + KV cache 8k) | GPU | ~3–3.5 GB | | only when cloud is unavailable or `offline=true` |
| **Offline total** | | **~5 GB** | | fits with little headroom; `qwen3:8b` (5.2 GB) does **not** fit alongside STT |

Policies:
- Ollama is called with `keep_alive` = 5 min so the local model unloads when cloud
  providers are working.
- "Low-VRAM / game mode" (later; owner question Q1): STT on CPU `small` int8 or Groq,
  Ollama disabled. Exposed as one config switch + tray toggle.

## 12. Build slices (incremental)

Each slice ends in something the owner can run. Later slices only add adapters.

| # | Slice | Contents | Done when |
|---|---|---|---|
| 0 | Skeleton | `uv` project, src layout, ruff/pytest, CI on windows-latest, config loader, logging, action log, fakes | `uv run luna --version`; CI green |
| 1 | **Text core** | CLI REPL `uv run luna --text`; router; `open_website`, `open_app` (Get-StartApps), `web_search` (ddgs + trafilatura); Assistant with `LLMChain` (Gemini → Groq → Ollama); small latency/accuracy benchmark script over ~20 RU/EN prompts | typing "открой ютуб", "open telegram", "какой курс доллара?", "что такое черная дыра?" gives correct actions/answers; tests pass |
| 2 | Voice out | `PiperTTS` worker + `SapiTTS` fallback; sentence splitting; language-based voice choice; interruptible playback | CLI replies are spoken in RU/EN |
| 3 | Voice in (push-to-talk) | audio capture, VAD endpointing, `FasterWhisperSTT` + `GroqWhisperSTT`, global hotkey | hotkey → speak → action + spoken reply; latency spans logged |
| 4 | Wake word | always-on capture, `VoskKeywordSpotter` for "луна", one-breath commands; parallel spike: custom ONNX model (ADR 0004) | "Луна, открой YouTube" works; false accepts/hour measured over a 2-hour session |
| 5 | Desktop shell | PySide6 tray, popup reply window with text input, status indicator, autostart toggle, settings file shortcut | MVP definition of done (spec §6) |
| 6 | Hardening | offline mode switch, game mode, provider status in tray, custom wake-word model if it beats Vosk, packaging (PyInstaller) investigation | owner's choice |

## 13. Open questions for the owner

See the decision list in each ADR. Summary:
1. Project license (MIT / Apache-2.0 / GPL-3.0) — affects how Piper (GPL-3.0) is used (ADR 0005).
2. Default LLM order and models after the slice-1 benchmark (ADR 0002).
3. Voice: Russian CC0 Piper voices are male (`dmitri`, `denis`); the female `irina`
   has an unknown dataset license; the female Windows voice "Irina" (SAPI) is free to
   use locally. Which voice? (ADR 0005)
4. Dialog: one command per wake word vs. a short follow-up window; "Луна, стоп".
5. Hotkey combination (default `Ctrl+Alt+Space`).
6. Game mode / unloading models while gaming.
