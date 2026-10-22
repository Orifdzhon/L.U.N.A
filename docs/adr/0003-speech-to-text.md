# ADR 0003 — Speech-to-text

- Status: **proposed**
- Date: 2026-10-22

## Context

Utterances are short (1–6 s), Russian, English or mixed ("открой YouTube",
"запусти Steam"). Latency target for simple commands is ≤ 2 s from end of speech (N4).
Reference GPU: RTX 2060, 6 GB (confirmed 6144 MiB, driver 572.83 via `nvidia-smi`).

## Options considered

| Option | Quality RU/EN | Latency (short clip) | Cost/limits | Notes |
|---|---|---|---|---|
| **faster-whisper (local, GPU)** `large-v3-turbo`, `int8_float16` | high (Whisper large-v3 class) | expected ~0.3–0.8 s on RTX 2060 (**estimate, measure**) | free, no limits | MIT; needs CUDA 12 cuBLAS + cuDNN 9 DLLs on Windows; VRAM ~1–1.5 GB (estimate; README shows large-v2 int8 ≈ 2.9 GB with beam 5 on long audio) |
| faster-whisper (local, CPU) `small` int8 | medium | ~1–2 s on 4-core i3 (estimate) | free | no CUDA needed; fallback / game mode |
| **Groq Whisper API** `whisper-large-v3-turbo` | high | network RTT + fast inference | free plan; docs show 20 RPM, 2K RPD, 7.2K audio-seconds/hour, 28.8K/day (Developer-plan figures; free-plan values in console — **unverified**); each request billed as ≥ 10 s | OpenAI-compatible `/audio/transcriptions`; 25 MB limit on free tier |
| Vosk large RU (1.5–1.8 GB) | good RU, no EN | fast on CPU | free, Apache-2.0 | no code-switching — rejected as main STT |
| Gemini audio input / Live API | good | higher | free tier | ties STT to one LLM vendor — not now |

### RU/EN code-switching

Whisper decodes one language per segment. Practical approach (to validate with test
clips in slice 3):
- Decode with `language=None` (auto) by default; optionally restrict detection to
  `{"ru","en"}` by comparing the two language probabilities and forcing the larger.
- Pass an `initial_prompt` with typical mixed vocabulary ("Луна, открой YouTube,
  Telegram, Chrome, GitHub") to bias spelling of brand names (Groq supports `prompt`
  ≤ 224 tokens too).
- The router does not depend on script: aliases include both spellings
  ("ютуб"/"youtube", "телеграм"/"telegram", "хром"/"chrome"), and fuzzy matching runs on
  a transliterated form as well.
- Built-in Silero VAD in faster-whisper (`vad_filter=True`) trims silence; our own
  endpointing VAD (ARCHITECTURE §4.2) decides when the utterance ends.

### CUDA on Windows

faster-whisper's README: latest CTranslate2 supports CUDA 12 + cuDNN 9 only; on Windows
use the libraries bundled by Purfview's `whisper-standalone-win` or install NVIDIA's
libraries. The pip packages `nvidia-cublas-cu12` (12.9.2.10, 553 MB) and
`nvidia-cudnn-cu12` (9.27.0.42, 743 MB) **do publish `win_amd64` wheels**; the README
documents the pip route only for Linux. Plan: install them in the `voice` group and
call `os.add_dll_directory()` on their `bin` folders before importing ctranslate2
(**unverified on Windows — first task of slice 3**). If that fails: Purfview archive
in a local `vendor/` folder (gitignored) or the CUDA Toolkit installed by the owner.

## Decision

- Default: **`FasterWhisperSTT`**, model `large-v3-turbo`, `device="cuda"`,
  `compute_type="int8_float16"`, `beam_size=1`, model kept loaded.
- Fallback chain: local GPU → **`GroqWhisperSTT`** (`whisper-large-v3-turbo`) →
  local CPU `small` int8. Same `FallbackChain` helper as LLMs.
- Config switch to make Groq primary (fastest path when GPU is busy / game mode).
- Audio format everywhere: 16 kHz mono PCM16.

Why local first even though cloud is acceptable: no daily cap, no network hop for the
most frequent path (simple commands, ≤ 2 s target), and VRAM is available because the
LLM is in the cloud by default.

## Consequences

- ~1.3 GB of NVIDIA wheels in the venv; first model download (~1.6 GB) on first run.
- STT VRAM must be measured in slice 3 and the budget in ARCHITECTURE §11 updated.
- Test clips (owner's voice, RU/EN/mixed, ~20 files) become fixtures for `gpu`-marked
  tests and for comparing local vs Groq accuracy.

## Sources (accessed 2026-10-22)

- faster-whisper README (GPU requirements, Windows notes, benchmarks): https://github.com/SYSTRAN/faster-whisper
- faster-whisper on PyPI (1.2.1, MIT): https://pypi.org/pypi/faster-whisper/json
- CTranslate2 wheels: https://pypi.org/project/ctranslate2/#files
- NVIDIA wheels: https://pypi.org/project/nvidia-cublas-cu12/#files , https://pypi.org/project/nvidia-cudnn-cu12/#files
- Groq speech-to-text: https://console.groq.com/docs/speech-to-text
- Groq rate limits: https://console.groq.com/docs/rate-limits
- Vosk models: https://alphacephei.com/vosk/models
