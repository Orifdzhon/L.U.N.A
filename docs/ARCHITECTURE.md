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
| LLM | One OpenAI-compatible client; chain Gemini `gemini-3.5-flash-lite` → Groq `openai/gpt-oss-120b` → local LM Studio (~4B Qwen) | native `google-genai` SDK if the compat layer breaks | [0002](adr/0002-llm-providers-and-tool-calling.md) |
| STT | `STTChain`: Groq `whisper-large-v3-turbo` → local `faster-whisper` `base` on CPU int8; toggle push-to-talk hotkey + energy VAD | `[stt.local] device = "auto"` (CUDA probed; unused on the reference PC); `small` model | [0003](adr/0003-speech-to-text.md) + amendment |
| Wake word | Hotkey first; then Vosk `small-ru` grammar spotter ("луна"); then a custom ONNX model | Porcupine rejected (free tier closed) | [0004](adr/0004-wake-word.md) |
| TTS | Windows SAPI voices: Irina (ru-RU), Zira (en-US); playback via `sounddevice` | Piper (`piper-tts`, out-of-process, CC0 voices) later/optional (#63) | [0005](adr/0005-text-to-speech.md) + amendment |
| Web search | `ddgs` metasearch + `trafilatura` extraction | self-hosted SearXNG; Tavily free key | [0006](adr/0006-web-search.md) |
| Desktop shell | PySide6 (tray + window), Win32 `RegisterHotKey`, HKCU `Run` autostart | tkinter + pystray | [0007](adr/0007-desktop-shell.md) |
| Open apps/sites | `Get-StartApps` index + aliases + `rapidfuzz`; `webbrowser` for sites | `.lnk` scan | [0008](adr/0008-opening-apps-and-websites.md) |
| Concurrency | **Threads + queues**, Qt on the main thread | asyncio (rejected, §5) | this doc |

## 1. Components and responsibilities

```
                 ┌───────────────────────── Input adapters ─────────────────────────┐
                 │ CLI REPL (slice 1) · Qt text box (slice 5) · Voice front-end      │
                 │      (hotkey → mic → VAD → STTChain, slice 3; wake word, slice 4)  │
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
                 Output adapters: CLI print · Speaker (SAPI TTS) · Qt window/tray
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
| Audio | `luna.audio` | `AudioClip`, `AudioPlayer` protocol + interruptible `sounddevice` playback (slice 2); `MicSource` / `SoundDeviceMic` capture (open only while recording), `RingBuffer`, `EnergyVAD`, utterance `Recorder` (slice 3). |
| STT | `luna.stt` | `STTProvider` protocol, `Transcript`, `STTChain` (fallback + cooldown), `GroqWhisperSTT` (default), `FasterWhisperSTT` (local CPU fallback). |
| TTS | `luna.tts` | `TTSProvider` protocol, `SapiTTS` (default), speech text preparation (`text`), `Speaker` worker (explicit `speak(reply)` from the UI); `PiperTTS` (subprocess worker) later/optional. |
| Wake word | `luna.wakeword` | `WakeWordDetector` protocol, `VoskKeywordSpotter`, `OnnxWakeWord`. |
| Voice front-end | `luna.voice` | `VoiceFrontEnd`: push-to-talk state machine IDLE → LISTENING → TRANSCRIBING → HANDLING (§4.2), barge-in, latency spans. `VoiceInput`: lazy assembly (STT chain, mic, recorder, hotkey), degraded start, `/mic` diagnostic. Wake word joins in slice 4. |
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
    capture.py            # MicSource, SoundDeviceMic (RawInputStream → bounded queue), RingBuffer
    vad.py                # VoiceActivityDetector, EnergyVAD (adaptive noise floor)
    recorder.py           # Recorder: pre-roll, endpointing, max length, cancel → Utterance
    base.py               # AudioClip, AudioPlayer protocol
    playback.py           # SoundDevicePlayer: RawOutputStream, chunked writes, abort() on stop
  stt/
    base.py               # STTProvider, Transcript, normalize_lang
    chain.py              # STTChain (same cooldown rules as LLMChain)
    groq.py               # GroqWhisperSTT (default)
    faster_whisper.py     # FasterWhisperSTT (local CPU fallback, optional CUDA probe)
  tts/
    base.py               # TTSProvider
    sapi.py               # SapiTTS (Windows built-in voices; integration chosen in #59)
    text.py               # speech text clean-up + sentence chunks
    speaker.py            # Speaker worker thread
    piper_tts.py          # later/optional (#63): subprocess worker
  wakeword/
    base.py, vosk_spotter.py, onnx_wakeword.py
  voice/
    frontend.py           # VoiceFrontEnd: push-to-talk state machine
    input.py              # VoiceInput: lazy assembly, degraded start, /mic levels
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

Rule: only `luna.platform.windows.*` (and `luna.tts.sapi`) may import Windows-only
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
    # opaque, not in repr/eq: {"provider": <producer name>, "data": {...}}, e.g. Gemini's
    # thought_signature; only the producing provider echoes `data` back (capped at 8 KB)
    provider_extra: Mapping[str, Any] | None = None

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
    finish_reason: str | None = None     # "stop" / "tool_calls" / "length" / ...
    usage: dict[str, int] | None = None  # token counts when the API reports them

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
class Transcript:                     # text == "" means "nothing understood", not an error
    text: str
    lang: str = "unknown"             # "ru" | "en" | "unknown" (normalize_lang)
    confidence: float | None = None   # 0..1 when the backend reports it
    provider: str = ""                # stamped by STTChain
    duration_s: float | None = None   # audio length, stamped by STTChain
    @property
    def is_empty(self) -> bool: ...

class STTProvider(Protocol):
    name: str
    def transcribe(self, pcm16: bytes, sample_rate: int = 16_000, *,
                   lang_hint: str | None = None, prompt: str | None = None,
                   timeout_s: float) -> Transcript: ...
    # Raises RateLimited / ProviderUnavailable / ProviderRequestError (all ProviderError).

# luna/stt/chain.py: itself an STTProvider (name "chain"); publishes ProviderSwitched
class STTChain:
    def __init__(self, providers: Sequence[STTProvider], clock: Clock,
                 bus: EventBus | None = None) -> None: ...
    def transcribe(self, pcm16: bytes, sample_rate: int = 16_000, *, lang_hint=None,
                   prompt=None, timeout_s: float, request_id: str | None = None) -> Transcript: ...

# luna/stt/faster_whisper.py: provider name "local"
class FasterWhisperSTT:
    def __init__(self, model: str = "base", device: str = "cpu",   # "cpu" | "cuda" | "auto"
                 compute_type: str = "int8", beam_size: int = 1,
                 model_dir: Path | str | None = None, *, vad_filter: bool = False) -> None: ...
    def warm_up(self) -> None: ...                    # blocking load
    def start_warm_up(self) -> threading.Thread: ...  # background load, errors logged
    resolved_device: str | None                       # "cpu" / "cuda" after loading
```

```python
# luna/audio/capture.py: 16 kHz mono PCM16, 30 ms frames
class MicSource(Protocol):
    def start(self) -> None: ...                  # opens the device; AudioError if it cannot
    def stop(self) -> None: ...                   # thread-safe, idempotent, unblocks read()
    def read(self, timeout_s: float) -> bytes | None: ...   # one frame; None on timeout/stop
    def close(self) -> None: ...
# SoundDeviceMic(device, frame_ms, preroll_ms, queue_frames): open only between start/stop;
# bounded queue (oldest frame dropped and counted); context manager + weakref.finalize.

# luna/audio/vad.py
class VoiceActivityDetector(Protocol):
    def is_speech(self, frame: bytes) -> bool: ...
    def reset(self) -> None: ...                  # before each recording
# EnergyVAD(threshold=0.003, margin=3.0, frame_ms=30, hangover_ms=60, warmup_ms=240)

# luna/audio/recorder.py
@dataclass(frozen=True)
class Utterance:
    pcm16: bytes                                  # empty for no_speech / cancelled
    sample_rate: int
    duration_s: float
    reason: Literal["silence", "max", "stopped", "cancelled", "no_speech"]

class Recorder:
    def __init__(self, mic: MicSource, vad: VoiceActivityDetector, conf: VoiceSettings, *,
                 clock: Clock = time.monotonic) -> None: ...
    def record(self, cancel: threading.Event, *,
               stop: threading.Event | None = None) -> Utterance: ...  # mic stopped on return
```

```python
# luna/voice/frontend.py
class VoiceState(enum.Enum):
    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    HANDLING = "handling"

class VoiceFrontEnd:
    def __init__(self, hotkey: HotkeyListener, recorder: Recorder, stt: STTProvider,
                 orchestrator: _Orchestrator, speaker: _Speaker | None, bus: EventBus,
                 on_reply: Callable[[Reply], None], *,
                 on_stale_reply: Callable[[Reply], None] | None = None,   # print only
                 voice: VoiceSettings | None = None, stt_conf: STTSettings | None = None,
                 clock: Clock = time.monotonic,
                 beep: Callable[[], None] | None = None) -> None: ...
    def start(self) -> None: ...      # registers the hotkey; ToolError if invalid/taken
    def stop(self) -> None: ...       # unregisters, cancels the active turn
    def close(self, timeout: float = 2.0) -> None: ...

# luna/voice/input.py: built by luna.app; never raises into the UI
class VoiceInput:
    def enable(self) -> str | None: ...   # None = listening; otherwise the degrade reason
    def disable(self) -> None: ...
    def measure(self, seconds: float = 3.0) -> MicLevels: ...   # /mic: floor, peak, suggestion
    def close(self) -> None: ...
```

```python
# luna/tts/base.py
class TTSProvider(Protocol):
    name: str
    def synthesize(self, text: str, lang: str) -> AudioClip: ...  # pcm16 + sample_rate
    # Playback is separate (luna.audio.playback) so it can be interrupted.

# luna/audio/base.py
@dataclass(frozen=True)
class AudioClip:
    pcm: bytes                # int16 mono
    sample_rate: int

class AudioPlayer(Protocol):
    def play(self, clip: AudioClip) -> None: ...   # blocks until done or stopped
    def stop(self) -> None: ...                    # thread-safe, idempotent
    def close(self) -> None: ...

# luna/tts/speaker.py — called explicitly by the UI, NOT subscribed to ReplyReady,
# so the fallback text of a cancelled/timed-out turn is spoken only if the UI asks.
class Speaker:
    def __init__(self, provider: TTSProvider, player: AudioPlayer,
                 bus: EventBus, settings: TTSSettings) -> None: ...
    def speak(self, reply: Reply, request_id: str | None = None) -> None: ...  # non-blocking
    def stop(self) -> None: ...                    # speech ends within ~0.5 s
    def set_muted(self, muted: bool) -> None: ...
    def close(self, timeout: float = 2.0) -> None: ...
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

class HotkeyListener(Protocol):       # press-only (no key-up): hence toggle push-to-talk
    def start(self, hotkey: str, on_press: Callable[[], None]) -> None: ...
    # hotkey: "+"-joined lowercase names ("ctrl+alt+space"); on_press runs on a background
    # thread; raises ToolError if the combination is invalid or already taken.
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

### 4.2 Voice request

#### Push-to-talk (slice 3, implemented) — the interaction model

This is the single definition of the push-to-talk model (owner decisions D2-D4, ADR 0003
amendment). `HotkeyListener` is press-only (`RegisterHotKey`, no keyboard hook), so it is a
**toggle**: one hotkey whose meaning depends on the state of `VoiceFrontEnd`.

| State when the hotkey is pressed | Effect |
|---|---|
| `IDLE` (also while a reply is being spoken) | **barge-in**: `Speaker.stop()`, optional start beep, open the mic, start a new turn → `LISTENING` |
| `LISTENING` | stop recording now and **keep** what was said (early stop) → `TRANSCRIBING` |
| `TRANSCRIBING` / `HANDLING` | **cancel** the turn (its `cancel` event) and start a new recording at once |

```
hotkey press (luna-hotkey-cb thread) ── flips state only, starts a turn thread ──┐
                                                                                 ▼
turn thread "luna-voice":
  LISTENING     Recorder.record(cancel, stop=turn.stop): mic open, 30 ms frames,
                300 ms pre-roll, EnergyVAD; ends on 700 ms silence, 10 s max,
                second press ("stopped"), or no speech within 5 s ("no_speech"); mic closed
                └─ empty audio / no_speech → IDLE "no speech" (no STT, no LLM call)
  TRANSCRIBING  STTChain.transcribe(pcm16, lang_hint, prompt, timeout_s=5)  (Groq → local)
                └─ empty transcript → IDLE "didn't catch that" (no LLM call)
                └─ ProviderError → ERROR "speech recognition failed"
  HANDLING      TranscriptReady → Orchestrator.handle(Request(text, source="voice", lang), cancel)
                └─ turn still current → UI on_reply(reply): print + Speaker.speak(reply)
                └─ turn superseded → on_stale_reply: print only if an action really ran
                   (never spoken; the orchestrator's "cancelled" apology is dropped)
  → IDLE; one INFO line: hotkey_to_listening_ms, capture_ms, stt_ms, handle_ms, provider, end reason
```

Rules:
- At most one STT call in flight (a superseded turn may still be decoding; the next waits or
  is cancelled). A press never blocks: `on_press` only changes state and signals events.
- Speaking is owned by the UI, after the turn has ended, so a press while speaking is an
  `IDLE` press. After `on_reply` the turn is checked again and speech is cut if a press landed
  meanwhile.
- `StatusChanged`: `LISTENING` on start, `THINKING` for transcribing/handling, final
  `IDLE` / `ERROR` with a short detail. A superseded turn publishes nothing (it only re-publishes
  the current status if the orchestrator's late `IDLE` would hide it).
- Privacy: the microphone is open only while recording; the audio is dropped after recognition;
  the transcript is logged at DEBUG only, the latency line has durations and the provider name.
- Hotkey: default `ctrl+alt+space`, `[voice] hotkey`; start beep `[voice] start_beep` (off).
- Echo: barge-in stops speech before the mic opens, so the assistant does not hear itself.
- No follow-up window: one command per press (D6, revisit in slice 4).

#### Wake word (slice 4, planned)

```
always-on mic ─► WakeWordDetector ─► on WakeEvent: same LISTENING → TRANSCRIBING → HANDLING turn,
                 starting at WakeEvent.end_sample (+300 ms pre-roll from the ring buffer)
```

"Луна, открой YouTube" said in one breath works because the detector reports the
sample position where the keyword ended and capture continues into the ring buffer.
If STT text still starts with "Луна"/"Luna", the router strips it. Slice 4 must also solve
TTS echo into an always-open microphone and decide the follow-up window (Q4).

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
| PortAudio callback | copies frames to a bounded queue only (never blocks; drops the oldest frame if full); exists only while recording |
| `luna-hotkey` | Win32 message loop for `RegisterHotKey` (registers, pumps `GetMessageW`, unregisters) |
| `luna-hotkey-cb` | runs `VoiceFrontEnd._on_press` from a bounded queue (8): flips state, signals events, starts a turn thread; never records or waits |
| `luna-voice` (one per turn, daemon) | record (VAD) → STT → `Orchestrator.handle(request, cancel)` and waits for it; a press can cancel the turn and start the next one at once |
| `stt-warm-up` (daemon, once) | loads the local faster-whisper model in the background when listening is enabled |
| `speaker` | `Speaker` worker: synthesize chunk N+1 while chunk N plays (slice 2) |
| `luna-req-<id>` (one per request, daemon) | orchestrator turn: router, LLM calls, tools. Turns are serialized by a turn lock, so typed and voice requests never run concurrently (no races on dialog state); the caller (REPL or `luna-voice`) waits for the reply, the timeout or its cancel event |
| Piper subprocess | later/optional: separate **process** (license boundary, ADR 0005) |

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
  `TAVILY_API_KEY`. Parsed by the loader with `python-dotenv` (no interpolation; environment
  variables win over `.env`); keys are `SecretStr`, never logged.
- Aliases in `data/*.toml` (shipped defaults) merged with user overrides in `luna.toml`.

```toml
[llm]
chain = ["gemini", "groq", "local"]
[llm.providers.gemini]
base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
model = "gemini-3.5-flash-lite"
api_key_env = "GEMINI_API_KEY"
[llm.providers.groq]
base_url = "https://api.groq.com/openai/v1"
model = "openai/gpt-oss-120b"
api_key_env = "GROQ_API_KEY"
[llm.providers.local]              # LM Studio (ADR 0002 amendment); Ollama: http://localhost:11434/v1
base_url = "http://localhost:1234/v1"
model = "qwen3-4b"                 # exact name as shown by LM Studio; chosen by #22 benchmark

[stt]                           # ADR 0003 amendment
chain = ["groq", "local"]       # order of STTChain; Groq skipped without GROQ_API_KEY
language_hints = ["ru", "en"]   # exactly one entry = force that language; more = auto-detect
prompt = ""                     # bias text for brand / app names (<= 500 chars)
timeout_s = 5.0
[stt.groq]
model = "whisper-large-v3-turbo"
api_key_env = "GROQ_API_KEY"
[stt.local]                     # faster-whisper
model = "base"                  # "small" is more accurate but RTF ~2 on the reference i3
device = "cpu"                  # "cpu" | "cuda" | "auto" (auto: test decode, else CPU)
compute_type = "int8"
beam_size = 1

[voice]                         # push-to-talk input (§4.2)
listen = false                  # --listen / /listen; the mic is never opened otherwise
hotkey = "ctrl+alt+space"       # the owner uses "ctrl+shift+space" (conflict)
start_beep = false
# input_device = "C270"         # omitted = default device; or an index / name substring
vad_energy_threshold = 0.003    # tune with /mic
vad_noise_margin = 3.0
silence_ms = 700
max_utterance_s = 10.0

[tts]                           # ADR 0005 amendment
enabled = false                 # speak replies aloud
provider = "sapi"               # Windows built-in voices; "piper" later (#63)
voice_ru = "Irina"              # case-insensitive substring of "Microsoft Irina Desktop"
voice_en = "Zira"               # "Microsoft Zira Desktop" (or "David")
rate = 0                        # -10 .. 10
volume = 100                    # 0 .. 100
max_chars = 1500                # longest spoken reply, cut at a sentence end
first_chunk_max_chars = 200     # short first chunk → quick first audio

[wakeword]                      # slice 4, not implemented yet
engine = "vosk"                 # "vosk" | "onnx" | "off"
keywords = ["луна"]

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
  Summaries of `untrusted` tool results are never written to the action log (only a
  placeholder with the length).
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
- STT uses `STTChain` (Groq → local), which copies `LLMChain`'s cooldown rules and constants;
  extracting one shared `FallbackChain` for LLM/STT (and later search: ddgs ↔ SearXNG/Tavily)
  is a backlog item. TTS has one provider in slice 2 (SAPI); a TTS failure is logged,
  `StatusChanged(ERROR)` is emitted and the reply stays visible as text.
- Per-stage timeouts (defaults): STT 5 s, LLM call 8 s, search 5 s, page fetch 4 s
  each, total request 20 s. On timeout the user hears a short apology instead of silence.
- Tool failures are returned to the LLM as `ToolResult(ok=False, summary=...)`; the
  LLM phrases the error. Router fast path: unknown/uncertain targets fall through to the LLM path; if the tool matched but failed to launch (`open_failed`), the router replies with a short failure message instead (a retry through the LLM could open things twice).
- Startup is degraded, not fatal: missing API key → provider skipped with a warning;
  no microphone, taken hotkey, non-Windows or no STT engine → `VoiceInput.enable()` returns
  a reason, the app prints it and stays in text mode (a taken hotkey points to
  `[voice] hotkey`); no Groq key → local STT only; CUDA failing → STT on CPU (`device="auto"`).

## 9. Safety

- Only tools in `ToolRegistry`; no shell, no file-system tool in the MVP.
- `ToolRegistry.register` refuses `DANGEROUS` tools unless `allow_dangerous=True`
  (not exposed in MVP config). `SENSITIVE` tools require `Confirmer.confirm`.
- `open_website` accepts only `http`/`https` URLs; `open_app` only entries from the
  app catalog (never a raw path or command line from the LLM).
- Web content isolation: search/page text is wrapped as quoted data in the tool
  message, marked `untrusted`, and the following LLM call has no tools (§4.1).
- A turn that used web content is stored in the dialog history with a
  `[answer based on web search]` prefix (truncated), and the system prompt tells the model never
  to act on requests originating from such messages; only the user's current request is acted on.
- Forced search (`force_search`): the assistant runs `web_search` itself and passes the wrapped
  summary as a system message (no fabricated assistant tool call), then calls the LLM with no tools.
- The tools-free follow-up after untrusted content carries no assistant `tool_calls` / `tool`
  messages (providers such as Groq reject or hallucinate tools then): this turn's tool summaries
  are collapsed into one system message. A `ToolUseRejected` (Groq `tool_use_failed`) on that call
  is retried once with "Answer in plain text. Tools are not available."
  That call uses a dedicated answer-only system prompt (no tool descriptions, no search
  instruction). An empty completion is `EmptyResponse` (a request error: no provider cooldown),
  and a reply that looks like a pseudo tool call in text (`<search`, `{"name":`, ...) is retried
  once, then replaced by a graceful "couldn't" reply; it is never shown to the user.
- Page fetching (`search/fetch.py`) validates every URL and redirect hop against local/private
  addresses. Known residual risk: DNS rebinding between check and connect is not pinned yet
  (ADR 0006, Amendment 1).
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

Updated 2026-10-26 (ADR 0003 amendment). **STT uses no VRAM**: Groq is remote and the local
fallback runs on the CPU, so the whole GPU is left to the local LLM. Measured STT RAM numbers are
**pending the STT benchmark (#87)**; RAM values below are estimates unless marked measured.

| Item | Device | VRAM | RAM | CPU / notes |
|---|---|---|---|---|
| Windows desktop + browser | GPU | 0.3–0.6 GB (est.) | — | already used before L.U.N.A. starts |
| Groq Whisper (default STT) | remote | 0 | ~0 (one WAV upload) | network RTT + inference; no model load |
| faster-whisper `base` int8 (local fallback, default) | CPU | 0 | ~0.3–0.5 GB (est., **pending #87**) | **measured** by the owner: ~1 s for a ~2 s clip on i3-12100F (4 cores); loaded once in the background when listening is enabled |
| faster-whisper `small` int8 (option) | CPU | 0 | ~0.8–1 GB (est., **pending #87**) | **measured**: RTF ~1.8–2.0 → misses N4 on this CPU; not the default |
| faster-whisper on CUDA (`device="auto"`/`"cuda"`) | GPU | ~1–1.5 GB for `large-v3-turbo` (est.) | | **not used**: decoding fails on the owner's PC (`cublas64_12.dll` missing); `auto` falls back to CPU |
| EnergyVAD + mic capture | CPU | 0 | < 5 MB | pure Python RMS per 30 ms frame; mic open only while recording |
| Wake word (Vosk small-ru, 45 MB) or ONNX — slice 4 | CPU | 0 | ~150 MB (est.) | ~5–10 % of one core (to measure) |
| Edge TTS (default) / SAPI fallback | remote / CPU | 0 | ~20–50 MB (est.) | MP3 decode via `miniaudio` |
| Piper medium voices (2) — optional, later | CPU | 0 | ~200 MB | separate process, not in the default total |
| Qt UI — slice 5 | CPU | ~0 | ~100 MB (est.) | |
| **Default total (cloud LLM, Groq STT)** | | **~0 GB** by L.U.N.A. | ~0.5–1 GB (est.) | GPU entirely free for games/other apps |
| Local LLM in LM Studio (owner: ~9B q4) | GPU | most of the 6 GB (owner's setup, not measured here) | | used as the last LLM fallback; this is why GPU STT was dropped |
| **Offline total (local LLM + local STT)** | | LLM only | LLM host + ~0.5 GB | STT and the LLM host share the 4 CPU cores; latency to be checked in #87 |

Policies:
- The local LLM host (LM Studio) manages its own model loading/unloading; L.U.N.A. does not
  load anything onto the GPU in the default configuration.
- "Low-VRAM / game mode" (later; owner question Q6): with STT already off the GPU it reduces to
  disabling the local LLM fallback. Exposed as one config switch + tray toggle.

## 12. Build slices (incremental)

Each slice ends in something the owner can run. Later slices only add adapters.

| # | Slice | Contents | Done when |
|---|---|---|---|
| 0 | Skeleton | `uv` project, src layout, ruff/pytest, CI on windows-latest, config loader, logging, action log, fakes | `uv run luna --version`; CI green |
| 1 | **Text core** | CLI REPL `uv run luna --text`; router; `open_website`, `open_app` (Get-StartApps), `web_search` (ddgs + trafilatura); Assistant with `LLMChain` (Gemini → Groq → Ollama); small latency/accuracy benchmark script over ~20 RU/EN prompts | typing "открой ютуб", "open telegram", "какой курс доллара?", "что такое черная дыра?" gives correct actions/answers; tests pass |
| 2 | Voice out | `SapiTTS` (Irina / Zira); speech text clean-up and sentence splitting; voice by `Reply.lang`; `sounddevice` interruptible playback; `Speaker` worker; CLI `--voice`, `/mute`, `/stop`. Piper later/optional (#63) | CLI replies are spoken in RU/EN |
| 3 | Voice in (push-to-talk) — **code merged** (PRs #90–#98, 2026-10-26); STT benchmark #87 and manual checks #88 open | `SoundDeviceMic` capture, `EnergyVAD` + `Recorder`, `STTChain` (`GroqWhisperSTT` → `FasterWhisperSTT` CPU `base`), `WindowsHotkeyListener`, `VoiceFrontEnd` toggle PTT with barge-in, `VoiceInput` + CLI `--listen` / `/listen` / `/mic`, degraded start | hotkey → speak → action + spoken reply; latency spans logged |
| 4 | Wake word | always-on capture, `VoskKeywordSpotter` for "луна", one-breath commands; parallel spike: custom ONNX model (ADR 0004) | "Луна, открой YouTube" works; false accepts/hour measured over a 2-hour session |
| 5 | Desktop shell | PySide6 tray, popup reply window with text input, status indicator, autostart toggle, settings file shortcut | MVP definition of done (spec §6) |
| 6 | Hardening | offline mode switch, game mode, provider status in tray, custom wake-word model if it beats Vosk, packaging (PyInstaller) investigation | owner's choice |

## 13. Open questions for the owner

See the decision list in each ADR. Summary:
1. ~~Project license~~ — **decided: Apache-2.0** (owner, 2026-10-23). Piper (GPL-3.0) stays in a separate process (ADR 0005).
2. Default LLM order and models after the slice-1 benchmark (ADR 0002).
3. ~~Voice~~ — **decided** (owner, 2026-10-23): SAPI "Irina" (RU) and "Zira" (EN) by default;
   Piper with male CC0 voices later/optional (ADR 0005 amendment).
4. Dialog: one command per wake word vs. a short follow-up window; "Луна, стоп".
   **Partly decided** (owner, 2026-10-26, D2/D6): push-to-talk is one command per press (toggle,
   §4.2); the follow-up window and "Луна, стоп" are decided with the wake word in slice 4.
5. ~~Hotkey combination~~ — **decided** (owner, 2026-10-26, D3): default `Ctrl+Alt+Space`,
   configurable in `[voice] hotkey`; the owner uses `Ctrl+Shift+Space` because of a conflict.
6. Game mode / unloading models while gaming.
7. ~~STT provider order~~ — **decided** (owner, 2026-10-26, D1): Groq first, local CPU `base`
   fallback, no GPU STT (ADR 0003 amendment).
