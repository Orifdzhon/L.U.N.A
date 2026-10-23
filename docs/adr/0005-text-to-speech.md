# ADR 0005 — Text-to-speech

- Status: **accepted (with amendment of 2026-10-23)**
- Date: 2026-10-22 (amended 2026-10-23)

> Amendment 2 (2026-10-24, at the end) makes **Edge online voices the default, SAPI the fallback**.
>
> The Amendment below replaces the default-voice part of the original Decision:
> **SAPI is the default, Piper is a later optional provider.** The original text is kept
> for history.

## Context

Replies are spoken in Russian or English (F7), locally or free, with low latency to
first audio (speak sentence by sentence). The code is open source with a possible paid
server-side service later, so licenses of the engine **and of each voice** matter.
Persona/voice gender is an open owner question (spec Q4).

## Options considered

| Option | RU | EN | License | Notes |
|---|---|---|---|---|
| **Piper** (`piper-tts` 1.8.0, OHF-Voice/piper1-gpl) | yes | yes | **GPL-3.0-or-later** (engine; embeds espeak-ng). Old `rhasspy/piper` was MIT but is **archived since 2025-10-06** | fast on CPU, ONNX, `cp39-abi3-win_amd64` wheel; voices downloaded separately |
| Windows SAPI / OneCore voices | yes (installed here: "Microsoft Irina Desktop" ru-RU, OneCore Irina/Pavel) | yes (David, Zira, Mark) | part of Windows; not redistributed by us | zero download, robotic quality, Windows-only |
| edge-tts (7.3.1, LGPL-3.0) | excellent neural voices | excellent | library LGPL; uses Microsoft Edge's **online** service unofficially | may break or violate ToS; network required |
| Silero TTS | very good RU | yes | models **CC BY-NC(-SA)** per mirrors (official LICENSE not reached) | non-commercial — rejected as default |
| Gemini TTS (`gemini-3.8-flash-tts`, free tier) | yes | yes | API ToS | cloud, extra latency and quota; possible later |
| Kokoro | no RU (not verified for latest version) | yes | Apache-2.0 | not suitable for RU |

### Piper voice licenses (model cards, accessed 2026-10-22)

| Voice | Gender | Dataset license |
|---|---|---|
| `ru_RU-dmitri-medium` | male | **CC0** (OHF-Voice voice-datasets) |
| `ru_RU-denis-medium` | male | **CC0** |
| `ru_RU-irina-medium` | female | dataset from RHVoice, **license "Unknown"** |
| `ru_RU-ruslan-medium` | male | RUSLAN corpus, **CC BY-NC-SA 4.0** — not for a paid service |
| `en_US-ljspeech-medium` | female | **public domain** |
| `en_US-amy-medium` | female | "see URL" (mimic3-voices) — check before use |

### The GPL question (not legal advice)

`piper-tts` ≥ 1.3 is GPL-3.0. Importing it in-process makes the distributed
combination subject to GPL terms. Options:
1. License L.U.N.A. itself as GPL-3.0 — compatible; a server-side paid service is
   still possible (GPL, not AGPL, has no network clause), but third parties could not
   ship closed forks.
2. Keep L.U.N.A. permissive (MIT/Apache-2.0) and run Piper as a **separate process**
   (`python -m piper` / Piper HTTP server) communicating via stdin/stdout or HTTP —
   generally treated as "aggregation", not a derivative work.
3. Use the archived MIT Piper binaries — unmaintained, rejected.

## Decision

- **`PiperTTS` runs Piper in a separate worker process** (long-lived, model loaded
  once, text in → raw PCM out over pipes, or the Piper HTTP server on localhost).
  This keeps the license boundary clean whatever project license the owner chooses
  and also isolates crashes. Piper is installed in the venv as a dependency of the
  `voice` group but never imported by `luna` modules.
- Default voices: **RU `ru_RU-dmitri-medium` (CC0)**, **EN `en_US-ljspeech-medium`
  (public domain)**. Voice is chosen per sentence by language (Cyrillic ratio + STT
  language hint); Latin brand names inside Russian text stay in the RU voice.
- **Fallback `SapiTTS`** (Windows built-in voices, e.g. "Microsoft Irina" female RU) —
  also an option for the owner who prefers a female voice right now.
- edge-tts may be added later as an optional "cloud voice" provider, off by default,
  marked unofficial.
- Text is split into sentences; the first sentence is synthesized and played while
  the rest is synthesized (latency to first audio).

## Consequences

- No female Russian Piper voice with a clear license today — owner chooses between
  male CC0 Piper, female SAPI, or `irina` (personal use only until its license is clarified).
- Voices (~60 MB each) are downloaded on first run to `%LOCALAPPDATA%\LUNA\voices`,
  not committed.
- Training a custom CC0 female voice is possible later (Piper training), out of MVP.

## Owner decisions needed

- ~~Project license~~ — **decided: Apache-2.0** (owner, 2026-10-23). Piper, when added,
  stays in a separate process (see "The GPL question", option 2).
- ~~Voice choice~~ — **decided** in the Amendment: SAPI "Irina" (RU) and "Zira" (EN).

## Amendment (2026-10-23)

### Context

The owner wants a female voice now and a slice 2 that works with zero downloads. No
female Russian Piper voice has a clear license (see the table above), while the Windows
voice "Microsoft Irina Desktop" (ru-RU, female) is already installed on the reference
machine.

### Decision

1. **Default provider: `SapiTTS`** (Windows built-in SAPI voices), `[tts] provider = "sapi"`.
   - Russian: "Microsoft Irina Desktop"; English: "Microsoft Zira Desktop" (female, matches
     Irina; "Microsoft David Desktop" is the alternative).
   - Voices are selected by a **case-insensitive substring** of the installed voice name
     (`voice_ru = "Irina"`, `voice_en = "Zira"`). Fallback order: substring match → any
     installed voice whose culture matches the language (`ru` / `en`) → the system default
     voice, with a logged warning that lists the installed voices.
   - Settings (`[tts]`, already on `main`): `enabled`, `provider`, `voice_ru`, `voice_en`,
     `rate` (−10..10, SAPI scale), `volume` (0..100), `max_chars`, `first_chunk_max_chars`.
   - The whole reply is spoken in the voice of `Reply.lang`; per-sentence language
     switching is out of scope for slice 2.
2. **Piper is later and optional** (Issue #63), not part of slice 2. When it is added, the
   original Decision applies unchanged: separate worker process (GPL-3.0 boundary), male CC0
   voices `ru_RU-dmitri-medium` / `ru_RU-denis-medium`, `en_US-ljspeech-medium`, downloaded on
   first use. `provider` gains the value `"piper"` then.
3. **Playback: `sounddevice`** (PortAudio, MIT; bundled in the Windows wheel) as a core
   dependency on Windows (`sys_platform == 'win32'`; an optional extra is uninstalled by a plain
   `uv run`), behind the `AudioPlayer` protocol (`luna.audio`). `winsound` was rejected
   (measured in PR #67): `SND_MEMORY | SND_ASYNC` raises `RuntimeError`, and `SND_PURGE` does
   not stop a synchronous in-memory sound, so in-memory playback cannot be interrupted.
   `sounddevice` is needed for the microphone in slice 3 anyway.
4. **The UI drives speech explicitly.** A `Speaker` worker (own thread) is called by the UI
   with `speak(reply)`; it does **not** subscribe to `ReplyReady`. So the fallback text of a
   cancelled or timed-out turn is not spoken unless the UI decides to speak it. The Speaker
   emits `StatusChanged(SPEAKING)` / `StatusChanged(IDLE)` and stops within ~0.5 s on
   `stop()` (Ctrl+C, `/stop`, a new request).
5. **SAPI integration approach stays open** and is decided, with measurements, in #59.
   Candidates: a PowerShell `System.Speech` subprocess (no new dependency) or in-process COM
   via `comtypes` / `pywin32` (`SAPI.SpVoice`, possibly rendering to a memory stream so the
   result is an `AudioClip`). Criteria, in order:
   - **No injection through the reply text**: the text must never be interpolated into a
     command line or script; pass it as data (stdin, a COM argument), and SSML/XML must not
     be interpreted from LLM output (plain-text speak flags).
   - **Time to first audio** for a typical first sentence (measured; target fits spec N4).
   - **Interruptibility**: stop within ~0.5 s.
   - **Dependencies and licensing**: prefer none; any new package must be permissively
     licensed, and Windows-only imports stay inside the SAPI provider module (`luna.tts`).
   - **Output as PCM** (`AudioClip`) so the shared `AudioPlayer` does playback and stopping.
   If SAPI cannot return PCM, or speaking directly is clearly better, #59 stops and flags
   "needs architect" instead of changing the `TTSProvider` protocol (ARCHITECTURE §3).

### Rationale

- Zero download and zero setup: the voices ship with Windows.
- No GPL boundary and no voice-dataset license questions in the default configuration.
- A female voice, as the owner wants.

### Consequences

- Quality is noticeably more robotic than Piper or neural cloud voices.
- Windows-only: Linux/macOS need another provider (Piper, #63, is the natural candidate).
- SAPI voices are part of Windows and are **not redistributed** by us; a user without the
  Russian voice must install the Windows Russian speech pack (to be documented in #62).
- No ~200 MB RAM for a Piper process in the default budget (ARCHITECTURE §11).

## Sources (accessed 2026-10-22)

- piper-tts on PyPI (1.8.0, GPL-3.0-or-later, wheels): https://pypi.org/pypi/piper-tts/json
- Piper (new home): https://github.com/OHF-Voice/piper1-gpl · CLI docs: https://raw.githubusercontent.com/OHF-Voice/piper1-gpl/main/docs/CLI.md
- Old Piper (MIT, archived): https://github.com/rhasspy/piper
- Voice model cards: https://huggingface.co/rhasspy/piper-voices/tree/main/ru/ru_RU ,
  …/ru_RU/dmitri/medium/MODEL_CARD, …/denis/medium/MODEL_CARD, …/irina/medium/MODEL_CARD,
  …/ruslan/medium/MODEL_CARD, …/en/en_US/ljspeech/medium/MODEL_CARD, …/en_US/amy/medium/MODEL_CARD
- edge-tts on PyPI: https://pypi.org/pypi/edge-tts/json
- Silero license (mirrors): https://gitee.com/nauch/silero-models
- Gemini TTS free tier: https://ai.google.dev/gemini-api/docs/pricing
- Local check: installed Windows voices listed via `System.Speech` (2026-10-22): Irina (ru-RU), David, Zira (en-US); OneCore: Irina, Pavel, David, Mark, Zira.
- PR #67 (winsound vs sounddevice measurement, 2026-10-23): https://github.com/Orifdzhon/L.U.N.A/pull/67
- sounddevice on PyPI (0.5.6, MIT, Python >=3.7; accessed 2026-10-23): https://pypi.org/pypi/sounddevice/json

## Amendment 2 (2026-10-24): Edge online voices by default, SAPI as fallback

Owner decision after hearing Irina/Zira: SAPI sounds too robotic. The owner accepts that reply
text is sent to Microsoft's online service and that the endpoint is unofficial (it may break).

1. **Default provider: `EdgeTTS`** (`luna.tts.edge`, library `edge-tts` 7.x). Voices:
   `ru-RU-SvetlanaNeural` (female) and `en-US-JennyNeural`, settings `edge_voice_ru` /
   `edge_voice_en` (validated against `^[a-z]{2,3}-[A-Z]{2,4}-[A-Za-z0-9]+Neural$`).
   `[tts] provider` is `"edge" | "sapi"`; `voice_ru` / `voice_en` keep meaning "SAPI voice".
   `rate` (-10..10) maps to `-50%..+50%` (5 % per step); `volume` (0..100) maps to the offset
   `volume - 100` percent (`+0%` = full volume).
2. **Fallback: `FallbackTTS(EdgeTTS, SapiTTS)`.** `ProviderUnavailable` from Edge (network, timeout,
   service error, undecodable audio) makes the same chunk be spoken by SAPI; for
   `fallback_cooldown_s` (60 s) Edge is skipped, so no sentence waits for a failing call. One
   warning per outage, an info line when Edge is back. `ProviderRequestError` is not a service
   failure and propagates. Without SAPI (not Windows / no voices) Edge runs alone and the
   startup shows a warning.
3. **Audio format.** `edge-tts` always streams MP3 (24 kHz, 48 kbps, mono); the output format is
   hard-coded in the library, so a PCM/WAV format cannot be requested. Decoder options:
   `miniaudio` (MIT, pyminiaudio wheels for Windows/CPython 3.13, ~0.5 MB, no external binary) -
   **chosen**; PyAV (bundles FFmpeg, ~30 MB+, heavier licensing story); ffmpeg subprocess (not
   installed, would be an external prerequisite). The clip is decoded to int16 mono at 24 kHz.
4. **Threading.** `synthesize` runs on the Speaker's synthesis thread; it creates a short-lived
   event loop with `asyncio.run`, bounded by `edge_timeout_s` (plus 5 s connect / 15 s read
   timeouts in the library).
5. **Security.** Plain text only: `edge_tts.Communicate` XML-escapes the text (`&`, `<`, `>`)
   before building its SSML, so injected `<speak>`/`<voice>`/`<prosody>` tags are read aloud as
   text (covered by tests). Voice names come from config and are pattern-checked; rate and
   volume are built from ints.
6. **Dependencies and licenses.** `edge-tts` is **LGPL-3.0** (pure Python, unmodified, used
   through `import`), which does not make L.U.N.A. LGPL: the library stays replaceable by the
   user (`uv`-managed dependency). Its dependencies: `aiohttp` (Apache-2.0), `certifi` (MPL-2.0),
   `tabulate` (MIT), `typing-extensions` (PSF). `miniaudio` is MIT.
