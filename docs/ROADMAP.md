# L.U.N.A. — Roadmap

> Maintained by the `planner` agent. Source of stages: [ARCHITECTURE.md §12](ARCHITECTURE.md#12-build-slices-incremental).
> Requirements: [PRODUCT_SPEC.md](PRODUCT_SPEC.md). Decisions: [docs/adr/](adr/).
> Status date: 2026-10-23.

**Slices 0 (skeleton) and 1 (text core) are done** (Issues #4-#23, plus #43 and #48).
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
| 2 | Voice out | planned (Issues written) | #55-#62 (+ #63 later/optional) |
| 3 | Voice in (push-to-talk) | planned | — |
| 4 | Wake word | planned | — |
| 5 | Desktop shell | planned | — |
| 6 | Hardening | planned | — |

## Slice 0 — Skeleton (done)

**Goal:** a runnable, tested, CI-checked project that every later Issue can extend.

**Exit criteria** (ARCHITECTURE §12): `uv run luna --version` works; CI green on `windows-latest`;
config loader, logging, action log, protocols and fakes exist.

**Owner decisions:** ADR 0001 (Python 3.13 + uv + ruff + pytest, proposed) must be accepted
before merging #4. Project license (ARCHITECTURE §13 Q1) is not blocking slice 0.

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

## Slice 2 — Voice out (planned)

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
- The Speaker is driven by an explicit `speak(reply)` call from the UI, not by a bus subscription,
  so the fallback text of a cancelled turn is not spoken. Architect to object if a subscription is preferred.
- Whether SAPI output can be captured as PCM (`AudioClip`, ARCHITECTURE §3) or must `Speak` directly
  is decided by the developer's comparison in #59; if the sketch does not fit, flag "needs architect".
- Default English voice: planner proposes Zira (female, matches Irina); owner may choose David.
- Voice follows `Reply.lang` for the whole reply; per-sentence language switching is out of scope.

## Slice 3 — Voice in, push-to-talk (planned)

**Goal:** hotkey -> speak -> action + spoken reply.
**Contents:** audio capture, VAD endpointing, `FasterWhisperSTT` + `GroqWhisperSTT`, global hotkey
([ADR 0003](adr/0003-speech-to-text.md), [ADR 0007](adr/0007-desktop-shell.md)); `docs/manual-checks.md` mic/GPU checklist; VRAM measurement.
**Exit criteria:** hotkey flow works on the reference machine; latency spans are logged.
**Owner decisions:** hotkey combination (default `Ctrl+Alt+Space`, Q5); dialog flow (Q4).
**Issues:** to be written.

## Slice 4 — Wake word (planned)

**Goal:** "Луна, открой YouTube" works in one breath.
**Contents:** always-on capture, `VoskKeywordSpotter`, parallel spike on a custom ONNX model
([ADR 0004](adr/0004-wake-word.md)). Highest technical risk of the project.
**Exit criteria:** works; false accepts/hour measured over a 2-hour session.
**Owner decisions:** dialog flow and "Луна, стоп" (Q4); acceptable false-accept rate.
**Issues:** to be written.

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
3. Project license (ARCHITECTURE §13 Q1) is not blocking slice 0/1 but should be decided before the
   repository is made public and before slice 2.
