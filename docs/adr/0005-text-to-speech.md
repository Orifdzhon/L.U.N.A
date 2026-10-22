# ADR 0005 — Text-to-speech

- Status: **proposed**
- Date: 2026-10-22

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

- Project license (MIT / Apache-2.0 / GPL-3.0) — affects nothing in code thanks to the
  process boundary, but must be chosen before publishing.
- Voice choice (see Consequences).

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
