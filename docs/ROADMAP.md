# L.U.N.A. — Roadmap

> Maintained by the `planner` agent. Source of stages: [ARCHITECTURE.md §12](ARCHITECTURE.md#12-build-slices-incremental).
> Requirements: [PRODUCT_SPEC.md](PRODUCT_SPEC.md). Decisions: [docs/adr/](adr/).
> Status date: 2026-10-26.

**Slices 0 (skeleton), 1 (text core) and 2 (voice out) are done** (Issues #4-#23, #43, #48 and #55-#62).
Later slices are planned when their predecessor is nearly done. Open follow-ups from slice 1:
#33 (on-screen confirmation for DANGEROUS tools), #35 (redact sensitive tool arguments in the
action log), #39 (pin the validated IP when fetching web pages).

## Owner prerequisites (not Issues)

Do these before the matching Issues can be verified manually:

| What | Needed for | Command / action |
|---|---|---|
| Install `uv` | slice 0 (#4) | `winget install astral-sh.uv` |
| LM Studio with a local model and its server running | slice 1 manual checks (#16, #21, #22) | already installed; download a ~4B official Qwen model, then Developer → Start Server (`http://localhost:1234/v1`) |
| Free Gemini API key | slice 1 manual checks | create in Google AI Studio, put into `.env` as `GEMINI_API_KEY` |
| Free Groq API key | slice 1 manual checks | create in the Groq console, put into `.env` as `GROQ_API_KEY` |

All automated tests need none of these: they run without microphone, GPU, network or real keys.

## Stage overview

| Slice | Name | Status | Issues |
|---|---|---|---|
| 0 | Skeleton | **done** | #4-#8 |
| 1 | Text core | **done** | #9-#23, #43, #48 (open follow-ups: #33, #35, #39) |
| 2 | Voice out | **done** | #55-#62 (#63 Piper: later/optional) |
| 3 | Voice in (push-to-talk) | **code done**, #84 and #87 open | #77-#88 |
| 4 | Wake word | **planned, Issues written** | #102-#116 |
| 5 | Desktop shell | planned | — |
| 6 | Hardening | planned | — |

## Slice 0 — Skeleton (done)

**Goal:** a runnable, tested, CI-checked project that every later Issue can extend.

**Exit criteria** (ARCHITECTURE §12): `uv run luna --version` works; CI green on `windows-latest`;
config loader, logging, action log, protocols and fakes exist.

**Owner decisions:** ADR 0001 (Python 3.13 + uv + ruff + pytest, proposed) must be accepted
before merging #4. The project license (Apache-2.0) has been decided since.

| Issue | Title | Depends on | Parallel with |
|---|---|---|---|
| #4 | Project skeleton: uv project, src layout, ruff/pytest, config loader, logging, CI | — (first) | — |
| #5 | Core models, errors and EventBus | #4 | #6, #7, #8 |
| #6 | Full settings schema, `luna.example.toml` and secret handling | #4 | #5, #7, #8 |
| #7 | Logging: rotating app log, secret redaction, JSONL action log | #4 | #5, #6, #8 |
| #8 | Interfaces (protocols) and test fakes | #4 (soft: #5 for error classes) | #5, #6, #7 |

Note: CI file `.github/workflows/ci.yml` already uses `uv`; per CLAUDE.md any change to it needs
the owner's approval, so #4 only proposes changes if needed.

## Slice 1 — Text core (done)

**Goal:** `uv run luna --text` understands typed RU/EN requests, opens sites and apps, searches
the web and answers, with provider fallback Gemini -> Groq -> Ollama.

**Exit criteria** (ARCHITECTURE §12): typing "открой ютуб", "open telegram", "какой курс
доллара?", "что такое черная дыра?" gives correct actions/answers; all tests pass; benchmark
script exists and has been run by the owner.

**Owner decisions needed in this slice:**
- ADR 0002: confirm default LLM order/models **after** the benchmark (#22); confirm that
  free-tier data use by Google/Groq is acceptable.
- ADR 0006 (search: `ddgs` + `trafilatura`) and ADR 0008 (apps/sites) must be accepted before
  #13/#14 and #10-#12 are merged.
- ADR 0002 acceptance before #16/#17.

| Issue | Title | Depends on | Parallel with |
|---|---|---|---|
| #9 | ToolRegistry: schema validation, safety levels, action logging | #7, #8 (#5) | #10-#13, #15-#17 |
| #10 | `open_website` tool, BrowserOpener and site aliases | #8 | #9, #11-#13, #15-#17 |
| #11 | Windows app catalog (`Get-StartApps`) and launcher | #8 | #9, #10, #12, #13, #15-#17 |
| #12 | `open_app` tool: aliases and fuzzy name matching (adds `rapidfuzz`) | #8 | #9-#11, #13, #15-#17 |
| #13 | `DdgsSearch` provider and page fetch/extract (adds `ddgs`, `httpx`, `trafilatura`) | #8 | #9-#12, #15-#17 |
| #14 | `web_search` tool with untrusted-content wrapping | #8, #13 | #9-#12, #15-#17 |
| #15 | `get_datetime` tool | #8 | #9-#14, #16, #17 |
| #16 | `OpenAICompatProvider` (Gemini / Groq / Ollama; adds `openai`) | #8, #5 | #9-#15, #17 |
| #17 | `LLMChain`: ordered fallback with cooldown | #8, #5 | #9-#16 |
| #18 | Router: RU/EN fast path for open-app/open-site commands | #9 (#8, #5) | #19 |
| #19 | Assistant: LLM tool-calling loop with untrusted-content isolation | #9 (#8, #5) | #18 |
| #20 | Orchestrator: router -> assistant -> reply with events and timeouts | #5, #18, #19 | — |
| #21 | Composition root and CLI REPL: `uv run luna --text` | #6, #10, #11, #12, #14, #15, #16, #17, #20 | — |
| #22 | Benchmark script: ~20 RU/EN prompts, latency and tool-call accuracy | #21 | #23 |
| #23 | Docs: setup guide and slice-1 manual checks | #21 | #22 |

Suggested order: `#4` -> (`#5`, `#6`, `#7`, `#8`) -> (`#9`, `#10`-`#13`, `#15`-`#17`, then `#14`)
-> (`#18`, `#19`) -> `#20` -> `#21` -> (`#22`, `#23`).
Parallel Issues touch separate files; `#12` and `#13` (and `#16`) all add dependencies, so expect a
trivial `uv.lock` rebase.

Planning notes (not decisions):
- The Router calls the tools `open_website` then `open_app` and falls through to the LLM when
  both report `data["reason"] == "no_match"`. This contract is written into #10, #12 and #18.
- Because slice 0 already contains `tests/fakes.py` (#8), slice-1 Issues need no mic, GPU,
  network or API keys; real-provider checks are manual steps for the owner.

## Slice 2 — Voice out (done)

**Goal:** CLI replies are spoken in Russian and English.
**Contents:** `SapiTTS` (Windows built-in voices), speech text clean-up and sentence splitting,
voice by reply language, a non-blocking interruptible `Speaker`, CLI wiring
([ADR 0005](adr/0005-text-to-speech.md)).
**Exit criteria:** replies in `uv run luna --text --voice` are spoken in RU (Irina) and EN;
markdown, URLs and emoji are not read aloud; Ctrl+C, `/stop` or a new request stops speech within
~0.5 s; `/mute` works; all automated tests pass with fakes (no audio device); manual checks done by the owner.

**Owner decision (2026-10-23):** default TTS is the Windows SAPI voice **"Irina"** (ru-RU, female);
English uses a SAPI English voice (David/Zira installed). **Piper** (GPL-3.0, separate process,
male CC0 voices) comes later as an optional provider (#63), outside the slice 2 exit criteria.
Recorded as an Amendment in ADR 0005 (#55).

| Issue | Title | Depends on | Parallel with |
|---|---|---|---|
| #55 | Docs: ADR 0005 amendment (SAPI Irina default, Piper optional), accept ADR | — (architect) | all |
| #56 | Validated `[tts]` settings section and `luna.example.toml` keys | — | #55, #57, #58 |
| #57 | Speech text preparation: markdown/URL/emoji clean-up, U+202F, sentence splitting | — | #55, #56, #58 |
| #58 | `AudioClip` and interruptible `AudioPlayer` (Windows backend + fake) | — | #55, #56, #57 |
| #59 | `SapiTTS` provider (options compared in the PR, time to first audio measured) | #58, #56 (soft: #55) | #57 |
| #60 | `Speaker` worker: non-blocking, interruptible, SPEAKING -> IDLE events | #56, #57, #58, #59 | — |
| #61 | Wire TTS into the CLI: `--voice`, `/mute`, `/stop`, cancel on new request | #56, #58, #59, #60 | — |
| #62 | Docs: voice setup and slice-2 manual checks | #61 | — |
| #63 | **Later/optional:** `PiperTTS` in a separate process | slice 2 done | not scheduled |

Suggested order: (`#55`, `#56`, `#57`, `#58` in parallel) -> `#59` -> `#60` -> `#61` -> `#62`.
`#58` and `#59` both touch `tests/fakes.py` (trivial merge). `#61` is the only Issue in this slice
touching `app.py`, `cli.py` and `__main__.py`.

Planning notes (not decisions):
- Decided: the Speaker is driven by an explicit `speak(reply)` call from the UI, not by a bus
  subscription, so the fallback text of a cancelled turn is not spoken.
- Whether SAPI output can be captured as PCM (`AudioClip`, ARCHITECTURE §3) or must `Speak` directly
  is decided by the developer's comparison in #59; if the sketch does not fit, flag "needs architect".
- Default English voice: planner proposes Zira (female, matches Irina); owner may choose David.
- Voice follows `Reply.lang` for the whole reply; per-sentence language switching is out of scope.

## Slice 3 — Voice in, push-to-talk (code done; #84 and #87 open)

**Goal:** hotkey -> speak -> action + spoken reply.
**Contents:** audio capture, VAD endpointing, `FasterWhisperSTT` + `GroqWhisperSTT`, global hotkey
([ADR 0003](adr/0003-speech-to-text.md), [ADR 0007](adr/0007-desktop-shell.md)); manual mic/GPU
checklist in `docs/MANUAL_CHECKS.md`; STT benchmark and VRAM measurement.
**Exit criteria:** with `uv run luna --text --listen`, pressing the hotkey and saying "открой ютуб" /
"open telegram" / "какой курс доллара" opens/answers and speaks the reply on the reference machine;
pressing the hotkey during speech stops it and listens (barge-in); missing microphone, taken hotkey or
missing STT key degrade to text mode with a clear message; one latency line per voice turn is logged
(`capture_ms`, `stt_ms`, `llm_ms`, `tts_first_audio_ms`), simple commands <= 2 s after end of speech
(N4); all automated tests pass with fakes (no microphone, GPU, network or keys); the benchmark table
(#87) and manual checks (#88) are done by the owner.

| Issue | Title | Depends on | Parallel with |
|---|---|---|---|
| #77 | Validated `[stt]` and `[voice]` settings | — | #78, #79, #80, #84 |
| #78 | Microphone capture: `MicSource`, `SoundDeviceMic`, ring buffer, `FakeMic` | — (soft #77) | #77, #79, #80, #81, #82 |
| #79 | Windows global hotkey listener (`RegisterHotKey`, own thread) | — | all except `tests/fakes.py` merge |
| #80 | STT foundation: `Transcript`, `STTProvider`, `STTChain`, `FakeSTT` | — | #77, #78, #79 |
| #81 | `GroqWhisperSTT` | #80 (soft #77) | #82, #83, #78, #79 |
| #82 | `FasterWhisperSTT` (CPU int8 `small` default, CUDA optional, lazy download) | #80 (soft #77) | #81, #83, #78, #79 |
| #83 | Energy-based VAD and utterance `Recorder` | #78 (soft #77) | #79, #80, #81, #82 |
| #84 | Docs (architect): accept ADR 0003 with the owner's STT order, record the PTT model, VRAM table | owner answers; §11 numbers after #87 | code Issues up to #83 |
| #85 | `VoiceFrontEnd`: hotkey -> record -> STT -> `Request(source="voice")`, events, latency spans | #80, #83 (soft #79) | #81, #82, #87 |
| #86 | Wire it up: `build_app`, CLI `--listen`, barge-in, degraded start | #77-#83, #85 | — |
| #87 | STT benchmark script and owner's run: latency, accuracy, VRAM, CUDA-on-Windows | #78, #81, #82 | #83, #85 |
| #88 | Docs: voice input setup and slice-3 manual checks | #86 (soft #87) | #87 |

Status: #77-#83, #85, #86 and #88 are done (merged or in review); #84 (ADR amendment) and #87
(STT benchmark, owner's run) are still open.

Suggested order: group A in parallel (`#77`, `#78`, `#79`, `#80`) -> group B in parallel (`#81`, `#82`,
`#83`) -> `#85` -> `#86` -> (`#87` owner run, `#88`) -> `#84` final amendment with measured numbers.
Owner can answer the decisions below while group A is being built. `#84` part 1 (decisions) should be
done before `#86`; it only changes docs, not code.
Shared files: `tests/fakes.py` (#78, #79, #80: trivial merges), `pyproject.toml`/`uv.lock` (#82
dependency), `app.py`/`cli.py`/`__main__.py` only in #86.

### Owner decisions needed for slice 3 (needs-decision)

| # | Question | Planner recommendation | Trade-offs |
|---|---|---|---|
| D1 | **Default STT order** (setting `[stt].chain`; ADR 0003 currently says local GPU first) | **Groq first, local CPU `small` int8 as fallback** (`["groq","local"]`). Revisit local-GPU only if #87 shows CUDA works and Groq latency or limits hurt. | *Latency:* Groq ~0.3-0.8 s inference + network RTT, no model load; local CPU `small` int8 on an i3-12100F expected ~1-2 s (estimate, #87 measures). *VRAM:* the 9B q4 local LLM already fills most of the 6 GB, so GPU Whisper (~1-1.5 GB) would force LLM or Whisper swapping/OOM when both are used; CPU Whisper has no contention but loads ~1 GB RAM and uses cores the LLM host also needs. *CUDA on Windows:* CTranslate2 needs CUDA 12 + cuDNN 9; the pip route (~1.3 GB of wheels) is unverified on Windows, so it is risky as a default and is measured in #87. *Free tier:* about 20 RPM / 2K requests per day, each billed as >= 10 s (figures unverified for the free plan); fine for personal push-to-talk but the same key is used for the Groq LLM, so heavy testing can hit limits and fall back to local. *Accuracy RU/EN:* `whisper-large-v3-turbo` (Groq) is clearly better on code-switching ("открой YouTube", brand names) than `small`; `small` may need `initial_prompt` help. *Privacy:* audio goes to Groq; the owner said this is acceptable. |
| D2 | **Push-to-talk interaction** | **Toggle:** press = start listening, auto-stop on 700 ms silence (or 10 s), press again = stop early / cancel the running turn; press while speaking = barge-in. | `RegisterHotKey` gives presses only; true hold-to-talk needs a low-level keyboard hook (more code, antivirus/anti-cheat sensitivity) and is not in the current `HotkeyListener` protocol. Toggle + VAD needs a good endpointing threshold (#83, tuned in #88). Hold-to-talk can be added later behind the same protocol if toggle feels wrong. |
| D3 | **Hotkey** (Q5) | Keep `Ctrl+Alt+Space`; configurable via `[voice].hotkey`. | May collide with IME/other tools; #79 reports a clear error and the owner can change it. |
| D4 | **Audible cue** when listening starts (short beep) | Yes, optional and off by default is the safest; decide after the manual check. Not planned in an Issue; #85 leaves a hook. | Without a tray indicator (slice 5) the CLI line `[listening...]` may be missed when the window is not focused. |
| D5 | **`faster-whisper` as core dependency or optional extra** | Optional extra `stt-local`, with the exact `uv sync --extra stt-local` command documented (decide in #82/#84). | Core: a plain `uv run` always works (lesson from `sounddevice` in slice 2) but every install pulls ctranslate2/onnxruntime/av (hundreds of MB). Extra: lighter, but `uv run` without the extra removes it. |
| D6 | Follow-up window without pressing the hotkey again (Q4) | Not in slice 3; decide with wake word (slice 4). | Toggle PTT is one command per press. |

Planning notes (not decisions):
- VAD: planner proposal is an energy-based VAD with an adaptive noise floor (no new dependency, no
  native build); a Silero/ONNX VAD stays an option behind the same protocol if manual checks show bad
  endpoints. faster-whisper's own `vad_filter` still trims silence inside STT. Architect may override in #84.
- CLI naming: `--voice` already means spoken replies, so voice input is `--listen` / `--no-listen`
  (`[voice].listen`), consistent with `--voice` / `--no-voice`.
- Echo: the pipeline stops speech before recording, so the assistant does not hear itself; this does
  not hold for the always-on wake word (slice 4 must solve it).
- Safety: a transcript has the same trust as typed text; the microphone is open only while listening;
  no new tools or permissions in this slice. Transcript text is not written to the latency log line.
- The old name `docs/manual-checks.md` in earlier plans is `docs/MANUAL_CHECKS.md` in the repo.

## Slice 4 — Wake word (planned, Issues #102-#116)

**Goal:** "Луна, открой YouTube" works in one breath, hands-free, on the reference machine.
**Contents:** always-on capture, `VoskKeywordSpotter` (grammar `["луна","[unk]"]`), half-duplex gating
against TTS echo, wake turns through the existing push-to-talk pipeline, calibration benchmark, and a
conditional spike on a custom ONNX model ([ADR 0004](adr/0004-wake-word.md)). Highest technical risk
of the project; the hotkey stays as fallback and works at the same time.
**Exit criteria:** with `uv run luna --text --wake`, "Луна, открой ютуб" (one breath) and "Луна" ...
pause ... "открой ютуб" both open the site and speak the reply; a spoken reply containing "Луна" does
not wake the assistant; `/wake off` closes the microphone and `Wake: off` is shown; a missing `vosk`
extra, model or microphone degrades to push-to-talk with a clear message; legacy `[wakeword]` config
loads; benchmark table (#115) shows the measured recall and false accepts/hour against ADR 0004
(FA <= 0.5/h, recall >= 90 %) with a verdict; all automated tests pass with fakes (no microphone,
model, network); manual checks (#114) done by the owner.

| Issue | Title | Depends on | Parallel with | Shared files |
|---|---|---|---|---|
| #102 | Docs (architect): accept ADR 0004, record slice-4 design (shared mic, gating, follow-up) | owner answers D7-D13 | #103, #104, #105, #106, #112 (draft) | docs only |
| #103 | Validated `[wakeword]` settings with legacy-key compatibility | — | #102, #104, #105, #106 | `settings.py`, `luna.example.toml` |
| #104 | `WakeWordDetector` protocol, `WakeEvent`, `FakeWakeWord` | — | #102, #103, #106 | **`tests/fakes.py`** |
| #105 | `VoskKeywordSpotter` (core dependency `vosk`, D13) | #104 | #102, #103, #106, #107 | **`pyproject.toml`/`uv.lock`** |
| #106 | `WakeModelStore`: lazy verified download of the Vosk model | — (soft #104) | all | none |
| #107 | Recorder hand-off: continue from wake-time audio on a shared mic stream | #102 (soft #104) | #105, #106, #112 | `recorder.py`, `capture.py`, **`tests/fakes.py`** |
| #108 | `WakeListener`: always-on loop, VAD gate, ring buffer, cooldown, pause/resume | #104, #107, #102 | #105, #106, #109 | none |
| #109 | Half-duplex: pause detection while TTS speaks (`Speaker` activity signal) | #102, #104 | #105, #106, #108, #110 | `speaker.py`, **`tests/fakes.py`** |
| #110 | `VoiceFrontEnd.on_wake`: one-breath commands, optional cue | #107, #108, #102 | #105, #106, #109, #112 | `frontend.py` |
| #111 | Wire it up: `--wake`, `/wake`, status line, degrade paths | #103, #105, #106, #108, #109, #110 | — | `input.py`, `cli.py`, `app.py`, `__main__.py` |
| #112 | Benchmark/calibration script `bench_wake.py` | #104, #105 (soft #106) | #107-#111 | `scripts/`, `benchmarks/` |
| #113 | Follow-up window without the wake word (optional, D9) | #110, #109 | #112, #114 | `frontend.py` |
| #114 | Docs: wake word setup and slice-4 manual checks | #111 (soft #112, #113) | #115 | docs only |
| #115 | Owner run: measure recall / FA per hour; architect finalizes the ADR 0004 amendment | #111, #112 | #114 | docs only |
| #116 | **Conditional spike:** custom ONNX wake word vs Vosk (time-boxed 2 days) | #115 says Vosk misses the targets | #114 | `scripts/bench_wake.py`, no model committed |

Suggested order:
1. Group A in parallel: `#102` (architect, after owner answers), `#103`, `#104`, `#106`. Merge `#104` first
   (it is the base package and the first `tests/fakes.py` edit).
2. Group B after `#104`: `#105` (the only `pyproject.toml` edit), `#112`, plus `#107` once `#102` is
   written. Merge `#107` before `#109` (both edit `tests/fakes.py`, a trivial append/merge).
3. Group C: `#108` (needs `#107`) in parallel with `#109`; then `#110`.
4. `#111` (alone, owns `app.py`/`cli.py`/`__main__.py`) -> `#114` docs and `#115` owner run in parallel
   -> `#113` follow-up window if D9 is yes -> `#116` only if `#115` fails the targets.
Shared files: `tests/fakes.py` in `#104`, `#107`, `#109` (merge in that order); `pyproject.toml` in `#105`
only; `frontend.py` in `#110` then `#113` (sequential). Developers run one at a time per CLAUDE.md, so
this is the safe order, not a requirement to parallelize.

Hard prerequisite for `#107`, `#108`, `#109`, `#110`: the architect's `#102`. Today `Recorder.record()`
closes the microphone in `finally`, so the always-on stream needs an ownership decision before code.

### Owner decisions needed for slice 4 (needs-decision)

| # | Question | Planner recommendation | Trade-offs |
|---|---|---|---|
| D7 | **Targets**: false accepts <= 0.5/h and recall >= 90 % (ADR 0004) | Accept as the bar for "Vosk is good enough"; if the result is FA 0.5-2/h, still ship as opt-in (`enabled=false` by default) because the hotkey stays. | "Луна" is a common noun and the owner's mic is quiet, so both numbers are unknown until #115. A stricter bar triggers the spike #116 (1-2 days + data licensing). |
| D8 | **Confirmation cue** after the wake word | Short beep (`[wakeword] cue = "beep"`), **off by default**, turn it on during calibration. A spoken "да?" is not planned. | Without a cue the owner cannot tell a wake from a miss when not looking at the REPL (no tray until slice 5). A spoken cue costs TTS latency and is itself echo into the open mic. |
| D9 | **Follow-up window** (D6 deferred from slice 3) | Optional Issue #113, **off by default** (`follow_up_s = 0`); when enabled use ~6 s. Decide after the first live week of wake mode. | Natural multi-turn talk vs. an open mic for room speech right after every reply (more false commands, privacy perception). Not needed for the exit criteria. |
| D10 | **"Луна, стоп" while L.U.N.A. speaks** (Q4) | **Not in slice 4.** Half-duplex: detection is paused while speaking plus a 400 ms tail; the hotkey stops speech (barge-in, already works). | Voice barge-in needs echo cancellation or headphones; the C270 plus speakers would self-trigger on the reply. Revisit in slice 6 (AEC or headset mode). |
| D11 | **Audio after the keyword** | Keep it: the command spoken in the same breath is part of the recording (one utterance, no second prompt); the router already strips a leading "Луна". | Slightly more complex Recorder hand-off (#107); the alternative (fresh recording after the keyword) loses the first words. |
| D12 | **Verification stage** (STT of the 1.5 s window must start with "луна") | Do not build in slice 4; add as a follow-up only if #115 misses FA. | A cloud STT call per wake would send audio before the owner confirmed intent (privacy) and burn Groq quota; local `small` costs ~1-2 s CPU. Hotkey fallback exists. |
| D13 | **`vosk` as a core dependency** (not an extra; cf. slice 3 D5) | Core dependency in `[project] dependencies` with a win32/linux marker, imported lazily; the ~45 MB model is downloaded on first enable. | A plain `uv run` re-syncs without extras and uninstalls them (same lesson as `sounddevice`); an extra would break wake mode after any `uv run`. |
| D14 | **Recording session** for calibration (~50 "Луна, ..." samples at 1 m/3 m, 2 h of room audio incl. TTS replies) | Yes, one evening; it is also the only way to know FA/hour. Recordings stay local and gitignored. | Takes the owner's time (mostly passive); required for #115. |

Planning notes (not decisions):
- Defaults of #103 are safe-off: `enabled = false`, `engine = "vosk"`, `keywords = ["луна"]`, `cue = "off"`,
  `follow_up_s = 0`. The legacy `hotkey` key inside `[wakeword]` is accepted and ignored with one warning
  (the hotkey lives in `[voice]`).
- Privacy: the microphone is open only while wake mode is on; audio is processed locally in a short
  in-memory ring buffer; nothing is sent to STT/LLM until the keyword fired; the REPL shows `Wake: on/off`
  and `/wake off` closes the stream at once. A second app using the microphone (calls) is a known
  limitation to document; `/wake off` is the answer.
- The `vosk` wheel for Windows/Python 3.13 and the CPU cost of continuous decoding (~5-10 % of one core,
  ARCHITECTURE §11) are unverified; #105 verifies both first and stops with "needs architect" if the wheel
  does not install (fallback: sherpa-onnx check, ADR 0004 option D).
- Echo is solved by half-duplex gating (#109), not by AEC. The listener also clears its ring buffer on
  resume so the tail of a reply cannot trigger.
- The model download is pinned by URL and SHA-256 and unpacked with a zip-slip guard (#106); it happens
  only when wake mode is enabled, never in tests.
- The conditional spike #116 stores no model or recordings in the repo; training-data licensing needs an
  owner decision before any model is redistributed.

## Slice 5 — Desktop shell (planned)

**Goal:** the MVP definition of done (PRODUCT_SPEC §6).
**Contents:** PySide6 tray, popup reply window with text input, status indicator, autostart toggle
([ADR 0007](adr/0007-desktop-shell.md)).
**Exit criteria:** spec §6 satisfied on the reference machine; CI green.
**Owner decisions:** UI scope (indicator, history, text input - spec open question 2).
**Issues:** to be written.

## Slice 6 — Hardening (planned)

**Goal:** owner's choice from: offline mode switch, game mode (unload models while gaming, Q6),
provider status in tray, custom wake-word model if it beats Vosk, PyInstaller packaging investigation.
**Issues:** to be written when the owner picks the scope.

## Open questions (needs-decision)

1. ADRs 0001-0008 are still "proposed": the owner must accept them; slice 0/1 Issues assume the
   defaults in ARCHITECTURE §0 (Python 3.13, `uv`, `openai` SDK, `ddgs`, `rapidfuzz`).
2. Where target resolution (site alias / app fuzzy match) lives is only implied by ADR 0008. The
   Issues place it inside the tools (`open_website`, `open_app`) so the Router stays thin. Architect
   to confirm.
3. Project license: decided, Apache-2.0 (see `LICENSE`).
