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
| LLM | One OpenAI-compatible client; chain Groq `openai/gpt-oss-120b` → Gemini `gemini-3.5-flash-lite` → local LM Studio (`[llm] chain = ["groq", "gemini", "local"]`) | native `google-genai` SDK if the compat layer breaks | [0002](adr/0002-llm-providers-and-tool-calling.md) + amendments |
| STT | `STTChain`: Groq `whisper-large-v3-turbo` → local `faster-whisper` `base` on CPU int8; toggle push-to-talk hotkey + energy VAD | `[stt.local] device = "auto"` (CUDA probed; unused on the reference PC); `small` model | [0003](adr/0003-speech-to-text.md) + amendment |
| Wake word | Opt-in wake mode (slice 4): one always-on mic stream owned by `WakeListener`, energy-VAD gate, Vosk `small-ru` grammar spotter ("луна") as stage 1, local faster-whisper `base` check "starts with Луна" as stage 2 (`[wakeword] verify = "local"`), half-duplex while speaking; the hotkey keeps working | `verify = "off"`; custom ONNX model only if #115 misses the targets; Porcupine rejected | [0004](adr/0004-wake-word.md) + slice-4 amendment + amendment 2 |
| TTS | `FallbackTTS`: Edge online neural voices (`ru-RU-SvetlanaNeural`, `en-US-JennyNeural`) → Windows SAPI (Irina / Zira) on failure; playback via `sounddevice` | `[tts] provider = "sapi"` (local only); Piper later/optional (#63) | [0005](adr/0005-text-to-speech.md) + amendments 1-2 |
| Web search | `ddgs` metasearch + `trafilatura` extraction | self-hosted SearXNG; Tavily free key | [0006](adr/0006-web-search.md) |
| Desktop shell | Slice 5 (accepted 2026-10-26): `PySide6-Essentials` tray + small window driven by a toolkit-free `ShellController`; `luna` / `pythonw -m luna --tray`; named-mutex single instance; Win32 `RegisterHotKey`; autostart = per-user Startup-folder shortcut (off by default) | tkinter + pystray | [0007](adr/0007-desktop-shell.md), [0009](adr/0009-desktop-shell-mvp.md) |
| Open apps/sites | `Get-StartApps` index + aliases + `rapidfuzz`; `webbrowser` for sites | `.lnk` scan | [0008](adr/0008-opening-apps-and-websites.md) |
| Concurrency | **Threads + queues**, Qt on the main thread | asyncio (rejected, §5) | this doc |

## 1. Components and responsibilities

```
                 ┌───────────────────────── Input adapters ─────────────────────────┐
                 │ CLI REPL (slice 1) · Qt text box (slice 5) · Voice front-end      │
                 │  (hotkey or "Луна" → mic → VAD → STTChain; slices 3–4)              │
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
                 Output adapters: CLI print · Speaker (Edge → SAPI TTS) · Qt window/tray
```

| Component | Package | Responsibility |
|---|---|---|
| Composition root | `luna.app` | Reads config, builds providers, wires adapters, starts threads. The only place that knows concrete classes. |
| Config | `luna.config` | Loads `luna.toml` + `.env`, validates with pydantic, exposes typed `Settings`. |
| Orchestrator | `luna.core.orchestrator` | Runs one request end to end: router → assistant → reply; emits status events; enforces timeouts and cancellation. |
| Router | `luna.core.router` | Rule-based fast path for simple commands (F3, F4) without an LLM; RU/EN verb patterns + alias/fuzzy lookup. Also flags "needs fresh facts" (F6 heuristics). |
| Assistant | `luna.core.assistant` | LLM conversation loop with tool calling, system prompt, short dialog history, web-content isolation rule (§9). |
| LLM providers | `luna.llm` | `LLMProvider` protocol, `OpenAICompatProvider`, `LLMChain` (fallback + cooldown; default order Groq → Gemini → local LM Studio). |
| Tools | `luna.tools` | `Tool` protocol, `ToolRegistry`, safety levels, built-in tools: `open_app`, `open_website`, `web_search`, `get_datetime`. |
| Search | `luna.search` | `SearchProvider` protocol, `DdgsSearch`, `SearxngSearch`, page fetch + extraction. |
| Platform | `luna.platform` | OS adapters behind protocols: app catalog/launcher, browser, hotkey, autostart (Startup-folder shortcut, slice 5), single instance (named mutex, slice 5). `luna.platform.windows` implements them. |
| Audio | `luna.audio` | `AudioClip`, `AudioPlayer` protocol + interruptible `sounddevice` playback (slice 2); `MicSource` / `SoundDeviceMic` capture, `RingBuffer`, `EnergyVAD`, utterance `Recorder` (slice 3); `FrameSource` / `FrameTap` / `LiveSource` so a recording can continue from the wake listener's live stream (slice 4). |
| STT | `luna.stt` | `STTProvider` protocol, `Transcript`, `STTChain` (fallback + cooldown), `GroqWhisperSTT` (default), `FasterWhisperSTT` (local CPU fallback). |
| TTS | `luna.tts` | `TTSProvider` protocol, `EdgeTTS` (default, online neural voices), `SapiTTS` (local fallback), `FallbackTTS`, speech text preparation (`text`), `Speaker` worker (explicit `speak(reply)` from the UI; exposes `active` + activity listeners for half-duplex); `PiperTTS` later/optional. |
| Wake word | `luna.wakeword` | `WakeWordDetector` protocol, `WakeEvent`, `VoskKeywordSpotter`, `WakeListener` (owns the always-on mic stream, VAD gate, holds/tail/cooldown, taps, follow-up window), `WakeVerifier` second stage (`FasterWhisperVerifier` sharing the local STT model, `PassThroughVerifier`; ADR 0004 amendment 2). `OnnxWakeWord` only after spike #116. |
| Voice front-end | `luna.voice` | `VoiceFrontEnd`: turn state machine IDLE → LISTENING → TRANSCRIBING → HANDLING (§4.2) for hotkey, wake and follow-up turns; barge-in; latency spans. `WakeModelStore` (`voice/wake_model.py`: lazy verified Vosk model download). `VoiceInput`: lazy assembly (STT chain, mic, recorder, hotkey, wake listener), degraded start, `/mic` diagnostic, `/wake on` / `/wake off`. |
| UI | `luna.ui` | `cli` (REPL, slice 1); `shell` (slice 5, no Qt imports: `ShellController`, icon-state derivation, conversation model, view protocols); `qt` (slice 5: thin PySide6 views - tray icon + menu, small window, GUI-thread dispatcher - and `run_tray`). ADR 0009. |
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
    windows/autostart.py  # Startup-folder shortcut L.U.N.A.lnk (ADR 0009; PowerShell WScript.Shell)
    windows/single_instance.py  # named mutex Local\LUNA-instance (ADR 0009)
    fallback.py           # non-Windows: NoAutostart (unsupported), ProcessLocalInstance (always acquires)
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
    edge.py               # EdgeTTS (online neural voices) + FallbackTTS (Edge → SAPI)
    sapi.py               # SapiTTS (Windows built-in voices)
    text.py               # speech text clean-up + sentence chunks
    speaker.py            # Speaker worker thread; activity listeners (half-duplex)
    piper_tts.py          # later/optional (#63): subprocess worker
  wakeword/
    base.py               # WakeWordDetector, WakeEvent (#104)
    vosk_spotter.py       # VoskKeywordSpotter, lazy `import vosk` (#105)
    listener.py           # WakeListener, WakeGate, WakeState (#108, #109)
    verify.py             # WakeVerifier, VerifyResult, FasterWhisperVerifier, PassThroughVerifier,
                          # keyword_first(), verify_window() (ADR 0004 amendment 2)
    onnx_wakeword.py      # only if spike #116 wins
  voice/
    wake_model.py         # WakeModelStore: download, SHA-256, safe unzip (#106, PR #118)
    frontend.py           # VoiceFrontEnd: push-to-talk state machine
    input.py              # VoiceInput: lazy assembly, degraded start, /mic levels
  ui/
    cli.py
    shell/                # slice 5, toolkit-free (never imports Qt)
      views.py            # TrayView, WindowView, UiThread protocols
      state.py            # IconState, derive_icon_state(), ConversationLine, Conversation
      launch.py           # own_launch_command(): the autostart LaunchCommand
      controller.py       # ShellController
    qt/                   # slice 5, the only package that imports PySide6
      dispatch.py         # QtUiThread: post(fn) → GUI thread via a queued Signal
      icons.py            # state icons drawn with QPainter (no image assets)
      tray.py             # QtTrayView: QSystemTrayIcon + QMenu + showMessage
      window.py           # QtWindowView: conversation, input line, status line
      app.py              # run_tray(settings, args) -> int: QApplication, wiring, quit
  logs/
    setup.py              # logging config, secret redaction filter
    action_log.py         # JSONL writer
    windowless.py         # slice 5: stdio replacement, excepthooks, faulthandler (pythonw)
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
    # ADR 0004 amendment 2: short-clip decode for the wake verifier, same model + decode lock
    def transcribe_short(self, pcm16: bytes, *, language: str = "ru",
                         max_new_tokens: int = 12) -> Transcript: ...
    # beam 1, without_timestamps=True, condition_on_previous_text=False, vad_filter=False,
    # NO initial_prompt / hotwords; same silence filter and errors as transcribe()
```

```python
# luna/audio/capture.py: 16 kHz mono PCM16, 30 ms frames
class FrameSource(Protocol):                      # what a Recorder reads from (slice 4)
    def read(self, timeout_s: float) -> bytes | None: ...   # one frame; None on timeout/stop

class MicSource(FrameSource, Protocol):
    def start(self) -> None: ...                  # opens the device; AudioError if it cannot
    def stop(self) -> None: ...                   # thread-safe, idempotent, unblocks read()
    def close(self) -> None: ...
# SoundDeviceMic(device, frame_ms, preroll_ms, queue_frames): open only between start/stop;
# bounded queue (oldest frame dropped and counted); context manager + weakref.finalize.
# Unchanged in slice 4: in wake mode it is simply started once and read by one thread.

class FrameTap:                                   # slice 4 (#107): a FrameSource fed by the listener
    def __init__(self, backlog: Iterable[bytes] = (), max_frames: int = 400) -> None: ...
    def push(self, frame: bytes) -> None: ...     # listener thread; never blocks, drops oldest
    def read(self, timeout_s: float) -> bytes | None: ...   # backlog first, then live frames
    def peek(self, n_frames: int, timeout_s: float) -> list[bytes]: ...   # amendment 2
    # waits until n_frames are queued, the tap is closed, or the timeout; returns the first
    # min(n, queued) frames WITHOUT consuming them (the wake verifier window, taken by the
    # turn thread before its Recorder starts reading the same tap)
    def close(self, error: AudioError | None = None) -> None: ...
    # close() wakes a blocked read(): afterwards read() returns None (closed=True), or raises
    # `error` once (microphone lost). Idempotent, thread-safe.
    closed: bool
    dropped: int

@dataclass(frozen=True)
class LiveSource:                                 # slice 4 (#107): "continue from the live stream"
    frames: FrameSource                           # usually a FrameTap; never started/stopped by the Recorder
    lead_in: bytes = b""                          # audio before the cut point (keyword + context)
    noise_floor: float | None = None              # listener's VAD floor; seeds the Recorder's VAD

# luna/audio/vad.py
class VoiceActivityDetector(Protocol):
    def is_speech(self, frame: bytes) -> bool: ...
    def reset(self, noise_floor: float | None = None) -> None: ...
    # None: forget the floor and warm up again (push-to-talk, as in slice 3);
    # a value: start from that floor with no warm-up (live sources, slice 4)
# EnergyVAD(threshold=0.003, margin=3.0, frame_ms=30, hangover_ms=60, warmup_ms=240);
# `noise_floor` property (read by the wake listener)

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
               stop: threading.Event | None = None,
               live: LiveSource | None = None,       # slice 4
               no_speech_s: float | None = None,     # slice 4: overrides NO_SPEECH_S (wake: 5 s)
               ) -> Utterance: ...
    # live=None (push-to-talk, wake mode off): mic.start() ... mic.stop() in finally (unchanged).
    # live given: never touches the mic; reads live.frames; calls live.frames.close() on return
    # (the Recorder owns the TAP, never the MIC); vad.reset(live.noise_floor).
    # live.lead_in is prepended to the result only if speech starts while it is still
    # contiguous with the kept pre-roll (no pre-roll frame dropped since the cut); otherwise it
    # is discarded. lead_in is never fed to the VAD. A tap closed without error → "cancelled".
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
    # slice 4 (#110, #113):
    def set_live(self, live: LiveAudio | None, wake: WakeWordSettings | None = None,
                 verifier: WakeVerifier | None = None) -> None: ...
    # called by VoiceInput on /wake on|off; with live set, EVERY turn (hotkey, wake, follow-up)
    # records from live.open_tap(...) instead of opening the mic. set_live(None) cancels a turn
    # that is still LISTENING on a tap (later stages finish normally). verifier None = no second
    # stage (same as PassThroughVerifier); the front end owns the single "luna-verify" worker.
    def on_wake(self, trigger: WakeTrigger) -> bool: ...
    # runs on the luna-wake thread, never blocks: False if not IDLE / stopped / closed (the
    # listener then closes the tap); True: a turn of kind "wake" (or "follow_up" when
    # trigger.event is None) starts: hold "turn:<id>", Recorder.record(live=trigger.source,
    # no_speech_s=wake.command_timeout_s). Kind "wake" with a verifier: the verification runs in
    # parallel with the recording; StatusChanged(LISTENING), cue and speaker.stop() wait for the
    # accept; a reject cancels the recording silently (§4.2). Transcript that is only the
    # keyword → IDLE silently.

class LiveAudio(Protocol):            # implemented by WakeListener; what the front end needs
    def open_tap(self, preroll_ms: int, *, skip_ms: int = 0) -> LiveSource: ...
    # hotkey / follow-up turns: lead_in = last preroll_ms of the ring; skip_ms drops the first
    # live frames (barge-in: the reply's echo tail). AudioError if the listener is not running.
    # Only one tap at a time: a new tap closes the previous one.
    def set_hold(self, reason: str, active: bool) -> None: ...   # idempotent, thread-safe
    def open_window(self, seconds: float) -> None: ...           # follow-up (#113), see §4.2

# luna/voice/input.py: built by luna.app; never raises into the UI
class VoiceInput:
    def enable(self) -> str | None: ...   # None = listening; otherwise the degrade reason
    def disable(self) -> None: ...
    def measure(self, seconds: float = 3.0) -> MicLevels: ...   # /mic: floor, peak, suggestion
    # slice 4 (#111): wake mode needs the push-to-talk parts, so enable_wake() calls enable()
    def enable_wake(self, progress: Callable[[str], None] | None = None) -> str | None: ...
    # blocking: first call downloads the model (progress lines), loads Vosk, builds and warms
    # the verifier ([wakeword] verify = "local": FasterWhisperVerifier on the chain's
    # FasterWhisperSTT instance, or one built from [stt.local]; a failed warm-up → note +
    # PassThroughVerifier), starts the listener (opens the mic), frontend.set_live(listener,
    # wake, verifier); returns the degrade reason or None
    def disable_wake(self) -> None: ...   # frontend.set_live(None), then listener.stop() (mic closed)
    def close(self) -> None: ...
    # /mic while wake mode is on reads through a tap (no second stream); refused during a turn
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
    # slice 4 (#109): activity for half-duplex, no knowledge of wake words
    @property
    def active(self) -> bool: ...
    def add_activity_listener(self, callback: Callable[[bool], None]) -> Callable[[], None]: ...
    # returns "remove". active turns True synchronously inside speak() when the job is accepted
    # (not muted/closed), before any synthesis; False when the last job ended (done, failed or
    # stopped) AND the worker has left player.play() AND nothing is pending. Only transitions
    # are reported. Callbacks run on the thread that caused the change, under the Speaker's
    # lock: they must be quick, must not block and must not call the Speaker (set a flag only).
    # Exceptions in callbacks are logged and swallowed.
```

```python
# luna/wakeword/base.py (#104, PR #119)
@dataclass(frozen=True)
class WakeEvent:
    keyword: str                      # as configured ("луна")
    confidence: float | None          # engine-specific 0..1, None if the engine reports none
    end_sample: int | None            # sample index right after the keyword, counted from the
                                      # detector's last reset() (D11); None = unknown
    # + end_s property (end_sample / sample_rate)

class WakeWordDetector(Protocol):     # not thread-safe; owned by the luna-wake thread
    name: str
    sample_rate: int                  # 16 000
    frame_samples: int                # preferred frame size (the listener sends 30 ms frames)
    def process(self, frame: bytes) -> WakeEvent | None: ...   # PCM16 mono
    def reset(self) -> None: ...      # forget all audio; end_sample counting restarts at 0
    def close(self) -> None: ...
# Each spoken keyword is reported once (Vosk: count keyword words in partial results and report
# when the count grows). At the end of a VAD speech segment the listener calls reset(); a
# detector that only reports on final results must flush inside process() (Vosk: AcceptWaveform
# returning True) - there is no separate flush method.
# end_sample None → the listener uses "samples fed since reset - 250 ms" as the cut.

# luna/wakeword/vosk_spotter.py (#105): `import vosk` inside __init__ (lazy, core dependency)
class VoskKeywordSpotter:
    def __init__(self, model_path: Path, keywords: Sequence[str], *, threshold: float,
                 sample_rate: int = 16_000) -> None: ...
    # KaldiRecognizer(model, 16000, json.dumps([*keywords, "[unk]"])), SetWords(True),
    # SetPartialWords(True) if available (#105 verifies; else end_sample=None).
    # confidence < threshold → no event. Keyword not in the model vocabulary → error at construction.

# luna/voice/wake_model.py (#106, PR #118)
class WakeModelStore:
    def __init__(self, model_dir: Path | None = None) -> None: ...   # None = %LOCALAPPDATA%\LUNA\models
    def ensure(self) -> Path: ...
    # vosk-model-small-ru-0.22: pinned URL + SHA-256; download, verify, unzip with a zip-slip
    # guard, atomic rename; returns the model folder; raises with a short reason on failure

# luna/wakeword/listener.py (#108, gating #109, window #113)
class WakeState(enum.Enum):
    OFF = "off"                # mic closed
    ARMED = "armed"            # detector fed (inside VAD speech segments)
    HELD = "held"              # stream open, detector muted (speaking / turn / tail / cooldown)
    FAILED = "failed"          # mic lost or detector error; mic closed; /wake on retries

@dataclass(frozen=True)
class WakeTrigger:
    source: LiveSource         # tap with backlog after the cut + lead_in before it
    event: WakeEvent | None    # None = speech onset in a follow-up window
    detected_at: float         # clock time, for the wake_to_listening_ms span

@dataclass(frozen=True)
class WakeStateChanged:        # published on the EventBus (indicator)
    state: WakeState
    detail: str | None = None  # "speaking", "turn", "follow-up", failure reason

class WakeListener:            # implements LiveAudio
    def __init__(self, mic: MicSource, detector: WakeWordDetector, vad: VoiceActivityDetector,
                 conf: WakeWordSettings, voice: VoiceSettings, bus: EventBus, *,
                 on_wake: Callable[[WakeTrigger], bool], clock: Clock = time.monotonic) -> None: ...
    def start(self) -> None: ...     # mic.start() (AudioError) + thread "luna-wake"; idempotent
    def stop(self) -> None: ...      # closes taps, mic.stop(), clears ring + detector, joins thread
    def close(self) -> None: ...     # stop() + mic.close() + detector.close()
    state: WakeState
    position: int                    # samples read since mic.start() (absolute stream position)
    noise_floor: float | None
    def open_tap(self, preroll_ms: int, *, skip_ms: int = 0) -> LiveSource: ...
    def set_hold(self, reason: str, active: bool) -> None: ...
    def open_window(self, seconds: float) -> None: ...
# WakeGate (same module): pure, clock-driven hold/tail/cooldown/window logic, unit-tested alone.

# luna/wakeword/verify.py: second stage (ADR 0004 amendment 2, D15)
VERIFY_LEAD_MS = 700        # end of lead_in (the keyword) ...
VERIFY_AFTER_MS = 700       # ... + this much audio after the cut  → ~1.4 s window
VERIFY_TIMEOUT_S = 3.0      # no result by then → fail open (reason "timeout")

@dataclass(frozen=True)
class VerifyResult:
    accepted: bool
    reason: Literal["keyword", "no_keyword", "empty", "off", "error", "timeout"]
    text: str = ""              # what the verifier heard; logged at DEBUG only
    latency_s: float = 0.0      # decode wall time

class WakeVerifier(Protocol):
    name: str                   # "local" | "off"
    def warm_up(self) -> None: ...                 # blocking; ProviderUnavailable if unusable
    def verify(self, pcm16: bytes) -> VerifyResult: ...
    # 16 kHz mono PCM16; never raises: a decode error → VerifyResult(True, "error") (fail open)
    def close(self) -> None: ...

class FasterWhisperVerifier:    # name "local"
    def __init__(self, stt: FasterWhisperSTT, words: Sequence[str], *,
                 fail_open: bool = True, clock: Clock = time.perf_counter) -> None: ...
    # verify(): stt.transcribe_short(pcm16, language="ru") → keyword_first(text, words)
    # warm_up(): stt.warm_up() + one decode of 0.5 s of silence (primes CTranslate2)
    # close(): no-op - the model belongs to the STT provider

class PassThroughVerifier:      # name "off": verify() → VerifyResult(True, "off"); also the
    def __init__(self, reason: str | None = None) -> None: ...   # degraded fallback

def keyword_first(text: str, words: Sequence[str]) -> bool: ...
# NFC, lower, ё→е; tokens = letter runs with inner hyphens; True iff tokens[0] in words.
# Exact match, no fuzzy distance ("лена", "луну", "она" are 1-2 edits away from "луна").
def verify_window(lead_in: bytes, after: Sequence[bytes], lead_ms: int = VERIFY_LEAD_MS,
                  after_ms: int = VERIFY_AFTER_MS) -> bytes: ...
# last lead_ms of lead_in + first after_ms of the frames after the cut; shared with bench_wake.py
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

# slice 5 (ADR 0009): `enable(command: str)` is replaced before anyone implements it
@dataclass(frozen=True)
class LaunchCommand:
    executable: Path              # <venv>\Scripts\pythonw.exe (absolute)
    args: tuple[str, ...]         # ("-m", "luna", "--tray", "--minimized"[, "--config", <abs>])
    working_dir: Path             # cwd at enable time: ./luna.toml and ./.env are found there

class Autostart(Protocol):
    def is_enabled(self) -> bool: ...                # never raises: False if unknown
    def enable(self, command: LaunchCommand) -> None: ...   # ToolError on failure
    def disable(self) -> None: ...                   # idempotent; ToolError on failure
# Windows (luna/platform/windows/autostart.py): StartupFolderAutostart(startup_dir=None,
#   writer=None). startup_dir None → SHGetKnownFolderPath(FOLDERID_Startup) via ctypes, fallback
#   %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup. The file is exactly "L.U.N.A.lnk";
#   is_enabled() = it exists; disable() deletes only that file. ShortcutWriter (injectable):
#   powershell -NoProfile -NonInteractive -Command <fixed script> with LUNA_LNK, LUNA_TARGET,
#   LUNA_ARGS, LUNA_CWD in the child environment ($s = (New-Object -ComObject WScript.Shell)
#   .CreateShortcut($env:LUNA_LNK); ...; $s.Save()); timeout 10 s; nothing interpolated into the
#   script. Args are joined with subprocess.list2cmdline. Tests: tmp_path + fake writer only.
# Other OSes (luna/platform/fallback.py): NoAutostart: is_enabled False, enable → ToolError.

class SingleInstance(Protocol):   # slice 5
    def acquire(self) -> bool: ...   # True: we are the only instance; keep the object alive
    def release(self) -> None: ...   # idempotent; the OS also releases on any process exit
# Windows: NamedMutexInstance(name="Local\\LUNA-instance"): CreateMutexW(NULL, FALSE, name);
#   GetLastError() == ERROR_ALREADY_EXISTS (183) → False (close that handle); ctypes behind a
#   small _Kernel32 wrapper that tests replace (pattern of hotkey.py). Other OSes:
#   ProcessLocalInstance (always True) until a lock-file version is needed.
```

```python
# luna/ui/shell/views.py (slice 5, ADR 0009): implemented by luna/ui/qt, faked in tests
class UiThread(Protocol):
    def post(self, fn: Callable[[], None]) -> None: ...   # any thread; fn runs on the UI thread

class TrayView(Protocol):            # all methods: UI thread only
    def set_state(self, state: IconState, tooltip: str) -> None: ...
    def set_menu(self, menu: TrayMenuState) -> None: ...  # checks, enabled flags, labels
    def notify(self, title: str, text: str) -> None: ...  # balloon/toast; no-op if unsupported
    def show(self) -> None: ...
    def hide(self) -> None: ...

class WindowView(Protocol):          # all methods: UI thread only
    def append(self, line: ConversationLine) -> None: ...
    def set_status(self, text: str) -> None: ...
    def set_input_busy(self, busy: bool) -> None: ...     # typed request running: input read-only
    def show(self) -> None: ...                           # show + raise + activate
    def hide(self) -> None: ...
    @property
    def visible(self) -> bool: ...

@dataclass(frozen=True)
class TrayMenuState:
    wake_checked: bool
    wake_enabled: bool             # False while enable/disable runs or wake is unavailable
    wake_label: str                # "Listen for «Луна»" / "Starting wake word…"
    voice_checked: bool            # not muted
    voice_enabled: bool            # False when [tts] enabled = false (no Speaker)
    autostart_checked: bool
    autostart_enabled: bool        # False while toggling or on unsupported platforms

# luna/ui/shell/state.py
class IconState(enum.Enum):
    IDLE = "idle"; WAKE = "wake"; RECORDING = "recording"
    THINKING = "thinking"; SPEAKING = "speaking"; ERROR = "error"

def derive_icon_state(status: Status | None, wake_state: str, error_active: bool) -> IconState: ...
# Pure. error_active (transient 5 s after StatusChanged(ERROR), or wake_state == "FAILED") → ERROR;
# LISTENING → RECORDING; SPEAKING → SPEAKING; THINKING → THINKING; otherwise wake_state in
# {"ARMED", "HELD"} → WAKE, else IDLE.

@dataclass(frozen=True)
class ConversationLine:
    kind: Literal["user", "assistant", "action", "system"]
    text: str                      # action: "open_website: ok" (+ summary unless untrusted, ≤ 120 chars)

class Conversation:               # bounded deque of ConversationLine ([ui] history_lines), memory only

# luna/ui/shell/launch.py
def own_launch_command(executable: Path, config: Path | None, cwd: Path) -> LaunchCommand: ...
# executable = sys.executable with the name pythonw.exe (same folder); args = -m luna --tray
# --minimized [+ --config <config.resolve()>]. Never takes window, transcript or LLM text.

# luna/ui/shell/controller.py
class ShellController:            # every public method runs on the UI thread
    def __init__(self, app: App, tray: TrayView, window: WindowView, ui: UiThread, *,
                 autostart: Autostart, launch: Callable[[], LaunchCommand],
                 conf: UISettings, clock: Clock = time.monotonic,
                 start_thread: Callable[[str, Callable[[], None]], None] = <daemon thread>) -> None: ...
    def start(self, *, wake: bool, minimized: bool) -> None: ...
    # sets VoiceInput sinks, starts luna-ui-events + luna-ui-worker, shows startup_lines() as
    # system lines, shows the window unless minimized, notifies degraded-start warnings once,
    # queues enable_wake(progress) on the worker when wake is True
    def on_event(self, event: object) -> None: ...         # posted by luna-ui-events
    def submit(self, text: str) -> None: ...               # window Enter
    def cancel(self) -> None: ...                          # window Esc
    def toggle_window(self) -> None: ...                   # tray left click / "Open L.U.N.A."
    def window_closed(self) -> None: ...                   # X button: hide + one notification
    def set_wake(self, on: bool) -> None: ...              # worker: enable_wake / disable_wake
    def set_voice(self, on: bool) -> None: ...             # Speaker.set_muted(not on)
    def set_autostart(self, on: bool) -> None: ...         # worker: autostart.enable/disable
    def quit(self) -> None: ...                            # §4.4 shutdown order
    @property
    def icon_state(self) -> IconState: ...
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
- Privacy: with wake mode off the microphone is open only while recording; the audio is dropped after recognition;
  the transcript is logged at DEBUG only, the latency line has durations and the provider name.
- Hotkey: default `ctrl+alt+space`, `[voice] hotkey`; start beep `[voice] start_beep` (off).
- Echo: barge-in stops speech before the mic opens, so the assistant does not hear itself.
- No follow-up window after a hotkey turn: one command per press (D6). The optional follow-up
  window of slice 4 exists only in wake mode (below).

#### Wake word (slice 4) — the wake-mode model

Owner decisions D7-D14 and rationale: [ADR 0004](adr/0004-wake-word.md), slice-4 amendment.
Wake mode is **opt-in** (`--wake`, `/wake on`, `[wakeword] enabled`) and adds to push-to-talk:
the hotkey table above stays valid in wake mode, unchanged.

**Microphone ownership**

| Mode | Who starts / stops the mic | Who calls `mic.read()` | Recorder input |
|---|---|---|---|
| Wake off (push-to-talk) | `Recorder.record()` per turn (`start` … `stop` in `finally`), as in slice 3 | the turn thread | the mic |
| Wake on | `WakeListener.start()` / `.stop()` only (`/wake on` / `/wake off`, failure, app exit) | only `luna-wake` | a `FrameTap` from the listener (`live=`); the Recorder closes the tap, never the mic |

**Listener states and gating** (`WakeListener`, one `luna-wake` thread)

| State | Mic | Detector | Entered when | Left when |
|---|---|---|---|---|
| `OFF` | closed | — | default; `/wake off`; app exit | `/wake on` → `ARMED` (after model load + mic open) |
| `ARMED` | open | fed inside VAD speech segments only | listener started; all holds released + `speaking_tail_ms` (400) elapsed; cooldown over | a hold is set, or a wake event fires |
| `HELD` | open (frames still go to the ring buffer and to an open tap) | muted | `speaking` (Speaker active), `turn:<id>` (any voice turn), tail, cooldown (`cooldown_s`, 2 s after an event) | last hold released + tail → `ARMED`: `detector.reset()`, ring buffer cleared |
| `FAILED` | closed | — | `AudioError` from the mic (unplugged / taken), no frames for 2 s, detector exception | `/wake on` (retry); push-to-talk still works if the mic is back |

**Wake event → turn** (the turn itself is the push-to-talk pipeline: LISTENING → TRANSCRIBING → HANDLING)

```
luna-wake: mic.read() → position += 480 → ring (2 s) → [tap.push if a tap is open]
           ARMED: EnergyVAD(frame) → gate: segment opens on speech (detector gets 300 ms ring
                  pre-roll first), closes after 600 ms non-speech (detector.reset()), forced
                  reset after 10 s → detector.process(frame) → WakeEvent?
           event: cut = segment_start + event.end_sample
                  lead_in = ring[cut-1000 ms : cut]   backlog = ring[cut : now]
                  tap = FrameTap(backlog); trigger = WakeTrigger(LiveSource(tap, lead_in, vad.noise_floor), event)
                  enter HELD (cooldown); release the listener lock; frontend.on_wake(trigger)
                    └─ False (frontend busy) → tap.close(); stays HELD until the cooldown ends
VoiceFrontEnd.on_wake (luna-wake thread, non-blocking): IDLE only → new turn kind="wake";
           set_hold("turn:<id>", True); start thread luna-voice
           (verify = "off" / no verifier: speaker.stop() here, as before)
luna-voice: verify = "local" (ADR 0004 amendment 2; kind "wake" only):
             after = first VERIFY_AFTER_MS / 30 frames the Recorder reads (a tee in front of the tap;
                     tap.peek cannot work: the Recorder consumes the frames it would peek at)
             window = verify_window(lead_in, after)  (~1.4 s: keyword + 700 ms after the cut)
             future = luna-verify.submit(verifier.verify, window)
               accept → speaker.stop(); StatusChanged(LISTENING); cue beep if enabled
               reject → turn.cancel.set() → Recorder returns "cancelled" → IDLE SILENTLY
                        (no status, no beep, no STT; INFO "wake rejected" with verify_ms)
           verify = "off": StatusChanged(LISTENING); [cue beep if [wakeword] cue = "beep"]
           → Recorder.record(cancel, stop=turn.stop, live=trigger.source,
                             no_speech_s=command_timeout_s (5 s))   ← runs WHILE verifying
           └─ backlog frames are VAD'ed like live audio → one-breath command already inside
           └─ no speech within 5 s → IDLE silently (no STT, nothing leaves the machine)
           → recording ended: wait for the verify result (VERIFY_TIMEOUT_S = 3 s from submit;
             timeout or error → fail open); rejected → IDLE silently
           → STTChain → strip leading "Луна"/"Luna" → empty → IDLE silently
           → Orchestrator.handle(Request(..., source="voice")) → on_reply → Speaker.speak (hold "speaking")
           finally: set_hold("turn:<id>", False)  ← after speak(), so holds overlap, no gap
```

Rules:
- **Half-duplex (D10).** While anything is spoken, and for 400 ms after it, the detector is muted;
  a reply containing "Луна" cannot wake L.U.N.A. (`mute_while_speaking = false` drops the
  `speaking` hold, for headset use only; turn holds stay.) Speech is interrupted with the hotkey only
  (barge-in: `speaker.stop()`, then `open_tap(preroll_ms=0, skip_ms=speaking_tail_ms)` so the echo tail is
  not recorded). "Луна, стоп" during speech: slice 6 (needs AEC or a headset).
- **Typed requests** in wake mode: their spoken replies set the `speaking` hold like any reply.
- **Hotkey in wake mode:** same toggle table; the turn records from `open_tap(voice.preroll_ms)`
  and holds `turn:<id>`; a hotkey press during a wake turn's LISTENING is the usual early stop.
- **Holds** are a set of names (`set_hold(name, active)` is idempotent); turn holds carry the
  request id, so a superseded turn releasing its hold cannot unmute the detector under a new turn.
- **Cue (D8):** `[wakeword] cue = "beep"` plays the 80 ms beep on the turn thread after the tap is
  open (with verification: on the `luna-verify` thread when the verifier accepts, so a rejected
  wake never beeps); it is shorter than `min_speech_ms`, so the VAD ignores its echo. Off by default.
- **Second stage (D15, ADR 0004 amendment 2, `[wakeword] verify = "local"` default):**

  | Situation | Behaviour |
  |---|---|
  | Verifier accepts (first token of the ~1.4 s window is `луна`/`luna`) | turn continues; status/cue published now; the recording was never interrupted |
  | Verifier rejects ("лунный", "Лена", "она", noise, ...) | recording cancelled, turn hold released, **no status, no beep, nothing spoken or sent**; the listener re-arms after the detection's 2 s cooldown |
  | Verifier error / no result within 3 s | **fail open**: treated as accepted, WARNING once per reason; at `/wake on` a model that cannot load → `PassThroughVerifier` + a note |
  | Hotkey press while unverified | the wake turn is cancelled silently and a normal hotkey turn starts (late verdict ignored) |
  | Recording ends before the verdict (short command) | the turn waits for the verdict (≤ 3 s from submit), then STT or silent exit |
  | Hotkey turn, follow-up turn (`event is None`) | not verified |

  The verifier transcript is never used as the command transcript (the command goes through
  `STTChain` as before). One `luna-verify` worker: checks never overlap; the decode lock of the
  shared `FasterWhisperSTT` serializes them with a local-STT fallback call.
- **Status:** a wake turn publishes the same `StatusChanged` sequence as a hotkey turn; the
  listener publishes `WakeStateChanged` (`armed`/`held`/`off`/`failed`) for the indicator.
  INFO line per wake turn: `wake_to_listening_ms`, `verify` (accepted/rejected/error/timeout/off),
  `verify_ms`, `capture_ms`, `stt_ms`, `handle_ms`, confidence. No audio, no transcript at INFO
  (the verifier's text at DEBUG only).
- **Privacy:** before a keyword, audio lives only in the 2 s ring buffer and the tap queue in
  memory; nothing is stored or sent. The verification window is decoded locally; a rejected wake
  never leaves the machine, only accepted wakes reach `STTChain` (Groq first). `/wake off`
  closes the stream at once and clears both.
  The REPL shows `Wake: on|paused|off|failed` and warns on `/wake on` that the mic stays open.

**Follow-up window** (D9, #113; `[wakeword] follow_up_s = 0` = off, ~6 when on; wake mode only)

| Step | What happens |
|---|---|
| Turn delivered a reply (`on_reply` ran, turn still current) | frontend calls `live.open_window(follow_up_s)` before releasing its turn hold |
| Speech + tail still running | window is pending; the detector stays muted (half-duplex wins) |
| Listener re-arms | window starts now: VAD speech onset (≥ `min_speech_ms`) fires `on_wake(WakeTrigger(event=None))` with lead_in = 300 ms pre-roll; the keyword detector also runs |
| Window expires without speech | closes silently; `WakeStateChanged(armed)` |
| Follow-up turn | kind `"follow_up"`, `no_speech_s = follow_up_s`; empty transcript → IDLE silently; after its reply a new window opens, **max 3 in a row**, then the keyword is required |
| Hotkey press, `/wake off`, a wake event | window closed (`open_window(0)`) |

### 4.3 Status events

`EventBus` (thread-safe, `queue.Queue` per subscriber) carries
`StatusChanged(IDLE|LISTENING|THINKING|SPEAKING|ERROR)`, `TranscriptReady`,
`ReplyReady`, `ActionExecuted`, `ProviderSwitched`, and (slice 4) `WakeStateChanged`
(forwarded from the wake listener). `ActionExecuted` is declared but not published; the actions
of a turn are read from `Reply.actions`. The CLI prints them; the tray shell (slice 5) reads one
subscription on the `luna-ui-events` thread and posts each event to the GUI thread through
`UiThread.post` (§4.4).

### 4.4 Desktop shell (slice 5, ADR 0009)

**Start (tray mode)** — `luna` with no mode flag, `luna --tray`, or `pythonw -m luna --tray --minimized`:

1. `__main__`: parse args, load settings, apply flags; if `sys.stdout`/`sys.stderr` is `None`
   (pythonw) → `logs.windowless.install()` (devnull streams, excepthooks, faulthandler);
   `setup_logging(console=<stderr is a real stream>)`.
2. `SingleInstance.acquire()`; taken → exit code 3 with "L.U.N.A. is already running (see the tray
   icon)" (stderr in `--text`, `QMessageBox` in tray mode). Same guard in `--text`.
3. `run_tray`: `QApplication`, `setQuitOnLastWindowClosed(False)`; `wake = settings.wakeword.enabled`;
   `app = build_app(settings, start_wake=False)` (fast: no model download on the GUI thread).
4. Views + `ShellController(app, ...)`; `controller.start(wake=wake, minimized=args.minimized or
   [ui] start_minimized)`; tray icon shown; window shown unless minimized; if
   `QSystemTrayIcon.isSystemTrayAvailable()` is false the window is always shown and a system line
   says so.
5. `wake` → `luna-ui-worker` runs `voice_input.enable_wake(progress)`; progress lines →
   `ui.post(window.set_status)` + log INFO; the menu item shows "Starting wake word…" and is
   disabled until it returns; a reason → system line + notification, push-to-talk stays.
6. `QApplication.exec()`.

**Events → views** (UI thread, `ShellController.on_event`):

| Event | Window | Tray |
|---|---|---|
| `StatusChanged(s)` | status line | `derive_icon_state(s, wake_state, error_active)`; `ERROR` → error for 5 s + detail in the status line |
| `WakeStateChanged(state)` | status line "Wake: …" | `WAKE` while `ARMED`/`HELD`; `FAILED` → persistent `ERROR` + notification "Wake word stopped: <reason>"; menu unchecked |
| `TranscriptReady` | user line | — |
| `ReplyReady` | assistant line + one action line per `reply.actions` entry | — |
| `ProviderSwitched` | status line "LLM: <provider>" ("offline" for `local`) | — |

**Speech:** as in the CLI, replies are spoken by the shell, not by a bus subscriber: voice turns via
the `VoiceInput` sinks (`on_reply` → speak, `on_stale_reply` → never spoken), typed requests when
`Orchestrator.handle` returns; apologies of cancelled / timed-out turns are not spoken (the CLI's
`_is_silent` rule moves to a shared helper). Display comes only from `ReplyReady`, so each reply
appears once.

**Typed request:** `submit(text)` (UI thread) → user line, `window.set_input_busy(True)`, thread
`luna-ui-req-<n>` runs `orchestrator.handle(Request(text, "text"), cancel)` → `ui.post(done)`: speak
(unless silent/muted), `set_input_busy(False)`. Esc sets `cancel`. A text starting with `/` is not
a command in the window: "Commands are in the tray menu" (the CLI keeps its slash commands).

**Tray menu actions:** *Listen for «Луна»* → `set_wake(on)` (worker: `enable_wake` / `disable_wake`);
*Voice replies* → `Speaker.set_muted(not on)` (cuts current speech); *Start with Windows* →
worker: `autostart.enable(launch())` / `disable()`, then the check mark is re-read from
`is_enabled()` (failure → notification, mark unchanged); *Open L.U.N.A.* / left click → toggle
window; *Quit* → shutdown. All toggles are session-only.

**Shutdown** (`quit()`, also on Ctrl+C in a console-started tray, or the OS session ending):
1. start a daemon watchdog: after 10 s → log CRITICAL + `os._exit(0)`;
2. tray hidden, window hidden, input disabled, cancel any typed request;
3. close the bus subscription (`luna-ui-events` exits), stop accepting worker jobs (a running
   `enable_wake` finishes on its own: `VoiceInput` discards the result after `close()`);
4. `app.close()` on the UI thread (hotkey, mic, turns, orchestrator, speaker, bus; bounded joins);
5. `instance.release()`, `QApplication.quit()`.

## 5. Concurrency model — threads, not asyncio

**Decision:** plain threads with `queue.Queue` hand-off; one request at a time.

| Thread | Work |
|---|---|
| Main | Qt event loop in tray mode (slice 5) or the CLI REPL (`--text`). Only the main thread touches Qt objects; everything else reaches it through `UiThread.post` (a queued `Signal`) |
| `luna-ui-events` (slice 5, tray mode) | blocks on one `EventBus` subscription and posts each event to the GUI thread; exits on `CLOSED` |
| `luna-ui-worker` (slice 5, tray mode) | one **daemon** thread + `queue.Queue` for blocking shell actions: `enable_wake` (model download/load), `disable_wake` (joins `luna-wake`), autostart toggles (PowerShell, ≤ 10 s). Jobs run one at a time; results are posted to the GUI thread. Not a `ThreadPoolExecutor`: its threads are joined at interpreter exit and would block quitting during a download |
| `luna-ui-req-<n>` (slice 5, one per typed request, daemon) | `orchestrator.handle(...)` for text typed in the window, like the REPL's wait; turns stay serialized by the orchestrator's turn lock |
| `luna-ui-quit-watchdog` (slice 5, daemon, only during shutdown) | `os._exit(0)` if shutdown takes more than 10 s |
| PortAudio callback | copies frames to a bounded queue only (never blocks; drops the oldest frame if full); exists while recording (push-to-talk) or for the whole time wake mode is on |
| `luna-wake` (slice 4, while wake mode is on) | the only reader of the mic in wake mode: ring buffer, tap push, EnergyVAD gate, Vosk decoding (inside speech segments only), hold/tail/cooldown/window logic; calls `VoiceFrontEnd.on_wake` (non-blocking). Started by `WakeListener.start()`, joined by `stop()` |
| `luna-hotkey` | Win32 message loop for `RegisterHotKey` (registers, pumps `GetMessageW`, unregisters) |
| `luna-hotkey-cb` | runs `VoiceFrontEnd._on_press` from a bounded queue (8): flips state, signals events, starts a turn thread; never records or waits |
| `luna-voice` (one per turn, daemon) | record (VAD) → STT → `Orchestrator.handle(request, cancel)` and waits for it; a press can cancel the turn and start the next one at once |
| `stt-warm-up` (daemon, once) | loads the local faster-whisper model in the background when listening is enabled |
| `luna-verify` (slice 4, one worker while wake mode is on with `verify = "local"`) | `ThreadPoolExecutor(max_workers=1)` owned by `VoiceFrontEnd`: runs `WakeVerifier.verify` (~1 s CPU decode) in parallel with the turn's recording; its done-callback only sets the turn's events and publishes status/cue under the front end's rules (it takes the front end lock briefly, never the listener lock first). Shut down on `set_live(None)` / `close()`; a running decode cannot be interrupted, its result is ignored |
| `luna-speaker`, `luna-speaker-cancel`, `luna-speaker-tts` | `Speaker` worker: synthesize chunk N+1 while chunk N plays (slice 2); activity callbacks (slice 4) run on these threads or the caller of `speak()`/`stop()` |
| `luna-req-<id>` (one per request, daemon) | orchestrator turn: router, LLM calls, tools. Turns are serialized by a turn lock, so typed and voice requests never run concurrently (no races on dialog state); the caller (REPL or `luna-voice`) waits for the reply, the timeout or its cancel event |
| Piper subprocess | later/optional: separate **process** (license boundary, ADR 0005) |

Wake mode adds one long-lived thread (`luna-wake`) plus the long-lived PortAudio callback, and,
with verification on, the `luna-verify` worker (idle except for ~1 s after each Vosk detection,
while the listener is held, so it never competes with Vosk decoding). The model download and Vosk model load run synchronously in the caller of
`VoiceInput.enable_wake()` (REPL / startup; one-time, with progress lines; Ctrl+C aborts),
not in an extra thread. CPU budget on the reference i3-12100F (4C/8T), measured in #115:
< 1 % total in a quiet room (VAD only), <= 5 % total averaged over room speech/TV, and zero
dropped mic frames over the 2 h session (the listener must keep up in real time; the 200-frame
mic queue absorbs ~6 s of hiccups).

Lock order (deadlock rule, slice 4): `VoiceFrontEnd._lock` → `Speaker` lock → `WakeListener`
lock, never the reverse. Speaker activity callbacks only call `WakeListener.set_hold`; the
listener calls `on_wake` and publishes events after releasing its own lock; the front end never
subscribes to Speaker activity (the follow-up window is driven by the listener's re-arm).
`WakeListener.stop()` joins `luna-wake`, so it is never called while holding the front end's lock
(`disable_wake`: `set_live(None)` returns first, then `stop()`). The `luna-verify` done-callback
follows the same order: it checks under the front end lock that its turn is still current and
unverified, records the verdict, releases the lock, and only then calls `speaker.stop()`,
publishes status, beeps or sets `turn.cancel`.

GUI-thread rule (slice 5): the GUI thread never blocks on core work. It may call only
non-blocking core methods (`Speaker.set_muted/stop`, `VoiceInput` state properties,
`wake_status_line()`, setting a `cancel` event); anything that loads models, joins threads,
waits for a turn or runs a subprocess goes to `luna-ui-worker` or `luna-ui-req-<n>`. The one
exception is `App.close()` during shutdown (bounded joins, watchdog). Callbacks from core
threads (`VoiceInput` sinks, wake `progress`) only call `ui.post(...)`; they never touch Qt and
never take a shell lock, so the slice-4 lock order is unaffected (the shell has no locks of its
own: its state lives on the GUI thread).

Why not asyncio:
- The heavy work is blocking C/C++ code (CTranslate2, ONNX Runtime, Vosk, PortAudio)
  that releases the GIL; asyncio would wrap all of it in `run_in_executor` anyway.
- Qt has its own event loop; mixing it with asyncio needs `qasync` or a second loop
  thread — extra moving parts for one developer.
- The workload is one user and one request at a time; there is no fan-out that asyncio
  would make cheaper. The only parallel I/O (fetching 3 result pages) uses a small
  `ThreadPoolExecutor`.

Cancellation: every request carries a `threading.Event`; "stop" (hotkey, tray button,
"Луна, стоп" in slice 6) sets it, playback stops immediately, LLM/HTTP calls are abandoned
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
chain = ["groq", "gemini", "local"]  # ADR 0002 amendment
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
language = "ru"                 # sent to the engines; "auto" = detect (language_hints is deprecated)
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

[tts]                           # ADR 0005 amendments 1-2
enabled = false                 # speak replies aloud
provider = "edge"               # "edge" (online neural, SAPI fallback) | "sapi" (local only)
edge_voice_ru = "ru-RU-SvetlanaNeural"
edge_voice_en = "en-US-JennyNeural"
voice_ru = "Irina"              # SAPI: case-insensitive substring of "Microsoft Irina Desktop"
voice_en = "Zira"               # SAPI: "Microsoft Zira Desktop" (or "David")
rate = 0                        # -10 .. 10
volume = 100                    # 0 .. 100
max_chars = 1500                # longest spoken reply, cut at a sentence end
first_chunk_max_chars = 200     # short first chunk → quick first audio

[wakeword]                      # slice 4: WakeWordSettings (#103, PR #120)
enabled = false                 # --wake / /wake on; the mic is never opened for wake otherwise
engine = "vosk"                 # "vosk" | "off" (= enabled false); "onnx" added only if spike #116 wins
keywords = ["луна"]
threshold = 0.5                 # detector confidence 0..1; calibrated in #115
cooldown_s = 2.0                # minimum gap between two wake events
model = "small-ru"              # vosk-model-small-ru-0.22 (WakeModelStore: pinned URL + SHA-256)
# model_dir = "D:/models"       # omitted = %LOCALAPPDATA%\LUNA\models (downloaded on first enable)
mute_while_speaking = true      # half-duplex (D10); false only for headset use
speaking_tail_ms = 400          # detector stays muted this long after speech ends (D10)
command_timeout_s = 5.0         # wait this long for the command after the keyword
cue = "off"                     # "off" | "beep" (D8)
follow_up_s = 0.0               # 0 = off (D9); ~6 when on; max 30
verify = "local"                # "local" | "off": 2nd stage, faster-whisper [stt.local] model checks
                                # that the utterance starts with the address (ADR 0004 amendment 2);
                                # [stt.local] model = "small" makes it ~2x slower
verify_words = ["луна", "luna"] # accepted first words (exact, lower case, ё = е)
# hotkey = "..."                # LEGACY: accepted, ignored with one warning (the hotkey is [voice] hotkey)

[search]
provider = "ddgs"               # "ddgs" | "searxng" | "tavily"
max_results = 5
fetch_pages = 3

[ui]                            # slice 5: UISettings (ADR 0009)
start_minimized = false         # tray mode starts with the window hidden (autostart adds --minimized)
notifications = true            # error balloons/toasts (wake failed, degraded start, autostart failure)
history_lines = 200             # conversation lines kept in the window (memory only), 20..2000
# autostart = false             # LEGACY: accepted and ignored; autostart is the Startup-folder
                                # shortcut, toggled only from the tray menu ("Start with Windows").
                                # `true` logs one warning saying so.
```

`[ui]` compatibility: the owner's file has `[ui] autostart = false`; it must load unchanged.
`autostart` is dropped (warning only if `true`); every other unknown key is an error
(`extra="forbid"`), like the legacy keys of `[stt]` and `[wakeword]`.

Command line (slice 5): `luna` with no mode flag = tray mode with the window shown (owner decision D3, 2026-10-26, ADR 0009;
before slice 5 it printed the usage); `--tray` = tray mode explicitly; `--minimized` = start with
the window hidden; `--text` = CLI (unchanged). `--voice/--no-voice`, `--listen/--no-listen`,
`--wake/--no-wake` and `--config` work in both modes. `--text --tray` is a usage error.

`[wakeword]` compatibility (#103): the owner's existing file has the old example block
`engine = "vosk"`, `keywords = ["луна"]`, `hotkey = "ctrl+alt+space"`. It must load unchanged:
`engine` and `keywords` keep their meaning; `hotkey` is dropped with one warning (like the legacy
`[stt]` keys); `engine = "off"` behaves as `enabled = false` (`--wake` then reports "wake word is
off in [wakeword] engine"). Every other unknown key is still an error (`extra="forbid"`). The
`--wake` flag and `/wake on` override `enabled` for the session; `enabled = true` means "start in
wake mode" and implies listening (`[voice] listen`).

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
- **Windowless mode (slice 5, `pythonw`):** there is no console, so `luna.log` is the only
  diagnostic channel. `logs.windowless.install(log_dir)` runs before anything else when
  `sys.stdout` or `sys.stderr` is `None`: both become UTF-8 `os.devnull` writers (libraries such
  as tqdm call `.write` on them), `sys.excepthook` and `threading.excepthook` log unhandled
  exceptions at CRITICAL (PySide6 reports exceptions in slots through `sys.excepthook`),
  `faulthandler.enable()` writes native crashes to `logs\crash.log` (file kept open). The console
  handler is only added when stderr is a real stream. Startup lines, warnings and wake progress
  go to the log (INFO) and to the window; the window conversation itself is never logged.

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
  is a backlog item. TTS: `FallbackTTS` falls back from Edge to SAPI on `ProviderUnavailable`
  (per chunk; Edge skipped for `fallback_cooldown_s` after two failures in a row); a failure of the last provider is logged,
  `StatusChanged(ERROR)` is emitted and the reply stays visible as text.
- Per-stage timeouts (defaults): STT 5 s, LLM call 8 s, search 5 s, page fetch 4 s
  each, total request 20 s. On timeout the user hears a short apology instead of silence.
- Tool failures are returned to the LLM as `ToolResult(ok=False, summary=...)`; the
  LLM phrases the error. Router fast path: unknown/uncertain targets fall through to the LLM path; if the tool matched but failed to launch (`open_failed`), the router replies with a short failure message instead (a retry through the LLM could open things twice).
- Startup is degraded, not fatal: missing API key → provider skipped with a warning;
  no microphone, taken hotkey, non-Windows or no STT engine → `VoiceInput.enable()` returns
  a reason, the app prints it and stays in text mode (a taken hotkey points to
  `[voice] hotkey`); no Groq key → local STT only; CUDA failing → STT on CPU (`device="auto"`).
- Wake mode degrades to push-to-talk, never to a crash: `vosk` not importable, model download or
  checksum failure (no network on first enable), keyword not in the model vocabulary, mic cannot
  be opened → `enable_wake()` returns the reason, push-to-talk stays on if it could start. A mic
  lost while wake mode is on → listener `FAILED`, `WakeStateChanged(failed, reason)`, the mic is
  closed, `/wake on` retries (no automatic reconnect in slice 4).
- The wake verifier **fails open** (ADR 0004 amendment 2, item 9): if the faster-whisper model
  cannot be loaded at `/wake on`, wake mode starts with `PassThroughVerifier` and a note
  ("Wake verification unavailable (<reason>); every detection starts a turn"); a decode error or
  a verdict later than 3 s accepts the wake with one WARNING per reason. Never deaf, never a crash.
- Desktop shell (slice 5): every degraded-start warning becomes a system line in the window and
  at most one notification; the tray keeps running with whatever works (text input always). No
  system tray available → the window stays visible instead. A failed autostart toggle → ToolError
  text in a notification, the check mark re-read from `is_enabled()`. Notifications are
  rate-limited to one per reason per 5 minutes. A shell exception in a Qt slot is logged
  (`sys.excepthook`) and never ends the event loop.

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
- Desktop shell (slice 5, ADR 0009): no new network access, nothing new leaves the machine.
  Text typed in the window is untrusted user input with exactly the CLI's trust level (same
  Router → Assistant → `ToolRegistry` path, same safety levels); the window never executes
  commands of its own. The conversation is kept in memory only; notifications never contain
  transcripts or reply text. The autostart shortcut is built only from `sys.executable`, fixed
  flags, the working directory and an explicit `--config` path, never from window, transcript or
  LLM text, and is created only by an explicit click on "Start with Windows". Agents and tests
  never touch the real Startup folder (fake writer + `tmp_path`); the real toggle is a manual
  owner check.

## 10. Testing strategy

| Level | What | How |
|---|---|---|
| Unit (CI, every PR) | router patterns RU/EN, alias + fuzzy resolution, ToolRegistry safety rules, LLMChain fallback/cooldown, assistant loop (tool round-trips, untrusted → no tools), config loading, action log format, voice state machine with synthetic frames | `pytest`; fakes in `tests/fakes.py` (`FakeLLM` replays scripted `ChatResponse`s, `FakeSTT`, `FakeTTS`, `FakeSearch`, `FakeAppCatalog`, `FakeLauncher`, `FakeBrowser`, fake clock) |
| Provider contract (CI) | request/response mapping of `OpenAICompatProvider`, ddgs/SearXNG parsing, Groq STT client | recorded HTTP fixtures via `respx` (httpx mock); no network |
| Platform (CI on `windows-latest`) | `Get-StartApps` JSON parsing, autostart shortcut logic, single-instance mutex | parsing tested on fixture JSON; the shortcut writer and `kernel32` calls are injectable; the real Startup folder and registry are never touched in tests |
| Integration (opt-in) | real Gemini/Groq/Ollama tool calls, real search, faster-whisper on fixture WAVs | markers `live`, `gpu`, `audio`; `pytest -m live` run manually with keys |
| Wake word (CI, slice 4) | `WakeGate` holds/tail/cooldown/window with a fake clock; listener with `FakeMic` frames + `FakeWakeWord` (scripted events at given positions): cut/lead_in/backlog math, one-breath and pause cases, reply-containing-keyword never fires while held, `/wake off` closes the mic, mic loss → `FAILED`; `Recorder(live=...)` never calls `mic.start/stop`; Speaker activity transitions; settings legacy keys; `keyword_first` table (accept "Луна, открой", "луна.", "Luna"; reject "лунный", "Лена", "она", "Луначарский", "Луна-парк", "на луну", ""); `verify_window` math; `FrameTap.peek` non-consuming; front end with `FakeVerifier` (scripted verdict/delay/exception): accept, silent reject cancels the recording, fail open on error/timeout, hotkey override, deferred status/cue | `pytest`; no `vosk` or `faster_whisper` import, no model, no microphone (real engines only under the `audio` marker / `bench_wake.py`) |
| Desktop shell (CI, slice 5) | `derive_icon_state` table; `Conversation` bound + action-line formatting (untrusted summary hidden); `ShellController` with `FakeTrayView`, `FakeWindowView`, an immediate `FakeUiThread`, a synchronous `start_thread`, `FakeAutostart` and the existing fakes (`FakeLLM`, `FakeSTT`, fake `VoiceInput`): events → icon/status/lines, typed request → one user + one assistant line and speech, silent apologies not spoken, wake toggle via worker incl. failure reason and `FAILED` → error + one notification, mute, autostart toggle + failure, close → hide + one notification, quit order (subscription closed, `app.close()` once); `own_launch_command` (pythonw path, flags, `--config` only when given); `StartupFolderAutostart` with `tmp_path` + fake writer (create/exists/delete exactly `L.U.N.A.lnk`, env passed, never the real folder); `NamedMutexInstance` with a fake `_Kernel32`; `[ui]` settings incl. legacy `autostart`; `windowless.install` with `None` streams; Qt views smoke-tested with `QT_QPA_PLATFORM=offscreen` (set in `tests/conftest.py`, no CI change) | `pytest`; no real tray, no Startup folder, no registry; the real shortcut writer only under the `desktop` marker writing into `tmp_path` |
| Manual (owner) | microphone, wake word false accepts/hour, latency, tray, autostart, real app launch | checklist `docs/MANUAL_CHECKS.md`, listed in each PR |

CI: GitHub Actions on `windows-latest`, `uv sync --frozen`, `ruff check`,
`ruff format --check`, `pytest -m "not live and not gpu and not audio"`.
Voice packages (`faster-whisper`, `sounddevice`, `vosk`) are core dependencies because a plain
`uv run` removes extras (ADR 0003 D5, ADR 0004 D13); they are imported lazily, and unit tests
use fakes, so the test job never loads a model or opens a device.

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
| EnergyVAD + mic capture | CPU | 0 | < 5 MB | pure Python RMS per 30 ms frame; mic open only while recording, or all the time in wake mode |
| Wake word: Vosk small-ru (45 MB on disk), wake mode only — slice 4 | CPU | 0 | ~150–300 MB (est., **pending #115**) | decodes only inside VAD speech segments; target < 1 % total CPU in silence, <= 5 % total with room speech (§5); model loaded on the first `/wake on`, kept until exit |
| Wake verification (stage 2, `verify = "local"`) | CPU | 0 | 0 extra: **shares** the faster-whisper `base` instance above (but wake mode now always loads it, even when Groq answers STT) | ~1 s decode burst (est. from the ~1 s per ~2 s clip measurement; 30 s encoder window is fixed) per Vosk detection, in parallel with the recording; measured by the bench extension and #115 |
| Edge TTS (default) / SAPI fallback | remote / CPU | 0 | ~20–50 MB (est.) | MP3 decode via `miniaudio` |
| Piper medium voices (2) — optional, later | CPU | 0 | ~200 MB | separate process, not in the default total |
| Qt UI (PySide6-Essentials) — slice 5, tray mode only | CPU | ~0 (est.) | ~60–100 MB (est., to be measured in the slice-5 manual check) | idle when hidden; 82 MB wheel on disk |
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
| 4 | Wake word (Issues #102–#116 + verification Issues, ADR 0004 amendment 2) | opt-in wake mode: `WakeListener` owning one always-on stream, energy-VAD gate, `VoskKeywordSpotter` for "луна" (lazy model download) + local faster-whisper second stage (`[wakeword] verify`), `Recorder` continuing from the live stream (one-breath commands), half-duplex gating with Speaker activity, optional cue and follow-up window (off); conditional ONNX spike #116 | "Луна, открой YouTube" works in one breath and with a pause; a spoken reply never wakes it; `/wake off` closes the mic; recall and FA/hour measured in #115 |
| 5 | Desktop shell ([ADR 0009](adr/0009-desktop-shell-mvp.md)) | `PySide6-Essentials` tray icon with state colours and a 5-item menu (open, wake on/off, voice replies, start with Windows, quit), small window (conversation, text input, status line; close hides), `luna` / `pythonw -m luna --tray --minimized`, single instance, windowless logging, Startup-folder autostart (off by default), `[ui]` settings. Post-MVP: popup at the tray, settings dialog / persisted toggles, open config/logs, activate the running instance, installer | MVP definition of done (spec §6): runs from the tray without a console, starts with Windows when enabled, voice and typed requests show their replies in the window; CI green |
| 6 | Hardening | offline mode switch, game mode, provider status in tray, custom wake-word model if it beats Vosk, packaging (PyInstaller) investigation | owner's choice |

## 13. Open questions for the owner

See the decision list in each ADR. Summary:
1. ~~Project license~~ — **decided: Apache-2.0** (owner, 2026-10-23). Piper (GPL-3.0) stays in a separate process (ADR 0005).
2. Default LLM order and models after the slice-1 benchmark (ADR 0002).
3. ~~Voice~~ — **decided** (owner, 2026-10-23): SAPI "Irina" (RU) and "Zira" (EN) by default;
   Piper with male CC0 voices later/optional (ADR 0005 amendment).
4. Dialog: one command per wake word vs. a short follow-up window; "Луна, стоп".
   **Decided** (owner, 2026-10-26 D2/D6 and 2026-10-26 D9/D10): push-to-talk is one command per
   press (toggle, §4.2); in wake mode an optional follow-up window exists but is off by default;
   "Луна, стоп" during speech is deferred to slice 6 (half-duplex until then).
5. ~~Hotkey combination~~ — **decided** (owner, 2026-10-26, D3): default `Ctrl+Alt+Space`,
   configurable in `[voice] hotkey`; the owner uses `Ctrl+Shift+Space` because of a conflict.
6. Game mode / unloading models while gaming.
7. ~~STT provider order~~ — **decided** (owner, 2026-10-26, D1): Groq first, local CPU `base`
   fallback, no GPU STT (ADR 0003 amendment).
8. ~~Desktop shell (slice 5)~~ — **decided** (owner, 2026-10-26, ADR 0009 accepted): D1-D9 in [ADR 0009](adr/0009-desktop-shell-mvp.md) —
   toolkit (PySide6-Essentials), autostart mechanism (Startup-folder shortcut), `luna` without
   flags = tray mode, single instance also for `--text`, session-only tray toggles, error-only
   notifications, plain window with English labels, legacy `[ui] autostart`, autostart flags.
