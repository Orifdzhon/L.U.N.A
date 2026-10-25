# ADR 0003 — Speech-to-text

- Status: **accepted (with amendment of 2026-10-26)**
- Date: 2026-10-22 (amended 2026-10-26)

> The Amendment at the end (owner decisions D1-D6, Issue #84) **replaces the Decision below**:
> **Groq first, local faster-whisper on CPU (`base`, int8) as the fallback; GPU STT is not used.**
> The original text is kept for history.

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

## Amendment (2026-10-26): Groq first, local CPU fallback, push-to-talk (Issue #84)

Owner decisions D1-D6 (Issue #84 comment, ROADMAP slice 3), recorded after slice 3 was merged
(PRs #90-#98). The reference LLM setup changed since the original Decision: the owner runs a
local LLM (LM Studio, ~9B q4) on the same 6 GB GPU, so "VRAM is free because the LLM is in the
cloud" no longer holds; the owner has a Groq key and does not treat privacy as a priority.

1. **D1 - default order `[stt] chain = ["groq", "local"]`** (`STTChain`, `luna.stt.chain`):
   - `GroqWhisperSTT` (`luna.stt.groq`): `whisper-large-v3-turbo`, `response_format="verbose_json"`
     (language + segment log-probabilities), language auto-detect; a `language` is sent only when
     `[stt] language_hints` has exactly one entry. `[stt] prompt` (<= 500 chars) is passed as
     `prompt` for brand/app names.
   - `FasterWhisperSTT` (`luna.stt.faster_whisper`, provider name `local`): `model="base"`,
     `device="cpu"`, `compute_type="int8"`, `beam_size=1`, `vad_filter=False` (our VAD already cut
     the utterance), `condition_on_previous_text=False`, model in `%LOCALAPPDATA%\LUNA\models`,
     lazy load (first use or background warm-up when listening is enabled), segments that Whisper
     itself flags as silence (`no_speech_prob > 0.6` and `avg_logprob < -1`) are dropped.
   - **Why `base` and not `small`**: measured by the owner on the reference CPU (i3-12100F) -
     `small` int8 ran at RTF ~1.8-2.0 (a 2 s clip takes ~4 s, misses N4), `base` int8 ~1 s for a
     ~2 s clip. `small` stays a config option (`[stt.local] model = "small"`). Accuracy figures:
     pending the STT benchmark (#87).
   - **Why Groq first**: best RU/EN code-switching quality, no model load, no VRAM, no CPU
     contention with the LLM host. Free-plan limits (~20 RPM / 2K requests per day, each request
     billed as >= 10 s; values not re-verified for the free plan) are ample for push-to-talk;
     on `RateLimited` / `ProviderUnavailable` the chain cools Groq down and the same utterance is
     decoded locally. Without `GROQ_API_KEY` Groq is skipped with a startup note.
   - `STTChain` duplicates `LLMChain`'s cooldown state machine on purpose (same constants) so
     `LLMChain` stayed untouched; a shared `FallbackChain` (ARCHITECTURE §8) is a backlog item.
2. **CUDA stance: opt-in, effectively unused.** `[stt.local] device` is `"cpu" | "cuda" | "auto"`.
   `"auto"` adds the `bin` folders of pip-installed `nvidia-cublas-cu12` / `nvidia-cudnn-cu12`
   (if present) with `os.add_dll_directory`, asks CTranslate2 for a CUDA device, and **proves CUDA
   with a 1 s test decode** right after loading; any failure falls back to CPU with a warning
   (`float16` compute types become `int8`). `"cuda"` fails loudly instead. Observed on the
   owner's machine: CTranslate2 sees the RTX 2060 but decoding fails (`cublas64_12.dll` missing),
   which is exactly the case the test decode catches. The NVIDIA wheels are **not** dependencies.
   Result: GPU STT is not used, VRAM stays with the local LLM (ARCHITECTURE §11).
3. **D2 - toggle push-to-talk** (no hold-to-talk): `RegisterHotKey` reports presses only, and a
   low-level keyboard hook is not wanted (antivirus / anti-cheat). The interaction model is
   defined once, in ARCHITECTURE §4.2: press -> barge-in -> record; auto-stop after 700 ms of
   silence or 10 s; second press = stop early and keep the audio; press during recognition or
   handling = cancel and record again; empty audio or empty transcript -> no STT / LLM call.
4. **D3 - hotkey** default `ctrl+alt+space`, configurable in `[voice] hotkey`. It really collided
   on the owner's PC (taken by another program); the owner uses `ctrl+shift+space`. A taken hotkey
   is a degraded start whose message names `[voice] hotkey` (ADR 0007 amendment).
5. **D4 - start beep** optional, **off** by default (`[voice] start_beep`, `winsound.Beep`
   880 Hz / 80 ms, silent where `winsound` does not exist).
6. **D5 - `faster-whisper` is a core dependency** (`faster-whisper>=1.1,<2` in
   `[project] dependencies`), not an extra. Lesson from slice 2: a plain `uv run` re-syncs without
   extras and silently uninstalls them (the `sounddevice` trap). Cost: ctranslate2 / onnxruntime /
   av in every install (hundreds of MB); acceptable for a voice assistant.
7. **D6 - follow-up window** (speak again without pressing the hotkey): deferred to slice 4
   (wake word). Slice 3 is one command per press.
8. **VAD: own energy VAD, no Silero.** `EnergyVAD` (`luna.audio.vad`) - RMS above
   `max(vad_energy_threshold, noise_floor * vad_noise_margin)`, the floor estimated from the first
   240 ms and then adapted on non-speech frames, 60 ms hangover; no dependency, no native build.
   Default threshold lowered **0.01 -> 0.003** after calibrating the owner's Logi C270 webcam mic
   (floor ~0.0001, speech ~0.005-0.016 RMS of full scale). The CLI `/mic` command measures the
   noise floor and the peak for 3 s and suggests a threshold. Silero/ONNX VAD remains a drop-in
   option behind `VoiceActivityDetector` if endpointing proves unreliable.
9. **Audio format** unchanged: 16 kHz mono PCM16, 30 ms frames. The microphone is open only while
   a recording runs (`SoundDeviceMic`; context manager plus a `weakref.finalize` safety net).

### Consequences

- Simple voice commands depend on the network for the best path; offline they still work at
  ~1 s STT on CPU with lower accuracy than Groq, especially for mixed RU/EN brand names.
- One Groq key now serves both STT and the LLM; heavy testing can hit the shared free-plan limits
  and push both to their local fallbacks.
- The CUDA path (pip NVIDIA wheels on Windows) stays unverified; revisit only if the owner moves
  the LLM to the cloud and wants GPU STT, after #87 measures it.
- Echo: barge-in stops speech before the microphone opens, so push-to-talk does not hear the
  assistant; an always-on wake word (slice 4) must solve TTS echo separately.
- Follow-ups: shared `FallbackChain` for LLM/STT (backlog), STT benchmark with accuracy and RAM
  numbers (#87), follow-up window and echo (slice 4).

### Sources (accessed 2026-10-26)

- Merged code: `src/luna/stt/{base,chain,groq,faster_whisper}.py`, `src/luna/audio/{capture,vad,recorder}.py`,
  `src/luna/voice/{frontend,input}.py`, `src/luna/platform/windows/hotkey.py`, `src/luna/config/settings.py`.
- Owner decisions: Issue #84 (comment), ROADMAP "Owner decisions needed for slice 3".
- Owner measurements (chat, 2026-10-26): `small` vs `base` CPU timing on i3-12100F, CUDA decode
  failure (`cublas64_12.dll`), C270 microphone levels via `/mic`, hotkey conflict.
