# ADR 0004 — Wake word "Луна"

- Status: **proposed**
- Date: 2026-10-22
- Risk: **highest technical risk of the MVP** (spec open question 7)

## Context

Requirements: always-on, local detection of the Russian word "Луна" (N6), low CPU on a
4-core i3, false accepts rare enough to live with (target ≤ 1 per 2 hours of normal
room audio; openWakeWord's own target is < 0.5/hour), false rejects < 10 %, and a
global hotkey as fallback (F1). The command often follows in the same breath
("Луна, открой YouTube"). "Луна" is a two-syllable common Russian noun, which makes
false accepts from conversation/TV likely for any detector.

License constraint: the project will be open source with a possible paid service, so
models we ship must be redistributable (ideally without non-commercial clauses).

## Options considered

### A. Porcupine (Picovoice) — **rejected**
Supports Russian and custom keywords, best-in-class accuracy, but proprietary and
needs an AccessKey at runtime. Per community reports quoting Picovoice, Free Tier
AccessKeys were disabled on 2026-06-30 and "There is no non-commercial tier planned."
An open-source project cannot depend on it. (Source is a forum post quoting Picovoice
email — not an official page; either way the license is incompatible with free
redistribution.)

### B. Vosk small Russian model with a restricted grammar — **chosen for slice 4**
- `vosk` 0.3.45 (Apache-2.0, Windows wheel), `vosk-model-small-ru-0.22` (45 MB,
  Apache-2.0). Small models support runtime vocabulary, so a `KaldiRecognizer` can be
  built with grammar `["луна", "[unk]"]`: every audio is forced to either "луна" or
  "unknown", which works as a keyword spotter with zero training.
- Partial results give low latency; word timestamps (`SetWords(True)`) give the end
  position of the keyword to cut the command audio.
- Pros: works today, fully open, no training, Russian-native acoustic model.
- Cons: not designed as a KWS — false-accept rate and CPU usage (continuous decoding)
  unknown on our machine (**to measure**); library last released 2022 (stable, but
  maintenance risk). Similar-sounding words ("лунный", "Луначарский", "она") may trigger;
  mitigations below.

### C. Custom ONNX wake-word model (openWakeWord-style) — **parallel spike**
- openWakeWord 0.6.0: code Apache-2.0, runs on Windows via onnxruntime (tflite not on
  Windows). Its **pre-trained models are CC BY-NC-SA 4.0** and its training notebook
  generates positives only with an English Piper voice (`en_US-libritts_r-medium`);
  README states English only. Training is Linux-only, pins 2022-era TF/PyTorch, and
  uses ACAV100M negative features whose license is reported to make trained models
  effectively non-commercial (**unverified**, community report).
- `livekit-wakeword` 0.3.0 (Apache-2.0, 2026-09-30): openWakeWord-compatible ONNX
  output, modern training pipeline (generate → augment → train → export → eval),
  Windows inference, **Russian supported for training via VoxCPM2** TTS (accuracy for
  non-English stated as lower than English). Negative-data licensing not stated.
- Community evidence: a Russian wake word «Ирина» was trained with microWakeWord from
  Piper `ruslan` + Silero voices; the author notes wrong stress marks in TTS prompts
  produce a useless model.
- Plan for the spike: generate positives "Луна" / "Луна," with correct stress using
  Russian voices (Piper `denis`, `dmitri`, `irina`, `ruslan`; VoxCPM2), add 50–200 real
  recordings of the owner, adversarial negatives ("луна-парк", "лунный", "она", "Лена",
  "Лина", "Луана", "Luna" in English context), train on Colab/WSL GPU, export ONNX.
- Pros: tiny CPU cost (~100 KB–1 MB models), purpose-built, tunable threshold.
- Cons: training effort and uncertain accuracy for Russian; data licensing to check
  before distributing a model.

### D. sherpa-onnx open-vocabulary keyword spotting
Apache-2.0, keywords defined as text without training. **Not verified** whether a
Russian KWS model exists; listed for a later check.

### E. STT-based spotting (Whisper on every VAD segment)
Accurate but wastes GPU continuously and adds latency. Used only as an optional
**verification stage** (see Decision 4).

### Fallback: global hotkey
Always available (`RegisterHotKey`, ADR 0007). Default `Ctrl+Alt+Space`.

## Decision

1. **Slices 1–3 need no wake word**: text mode, then hotkey push-to-talk.
2. **Slice 4 ships `VoskKeywordSpotter`** (grammar `["луна", "[unk]"]`, configurable
   keyword list) behind `WakeWordDetector`, plus the hotkey.
3. **Spike (time-boxed, 1–2 days) in parallel with slice 4**: train a custom "Луна" ONNX
   model with `livekit-wakeword` (fallback: openWakeWord notebook with Russian Piper
   voices) and run it through `OnnxWakeWord`. Replace Vosk only if it measures better on
   the same recordings.
4. False-accept mitigations, independent of engine:
   - Silero VAD gate: run the detector only on speech frames;
   - require a short pause or the command to follow within 2 s; if STT returns
     nothing meaningful, return to IDLE silently (no spoken "I didn't understand");
   - optional verification: STT the 1.5 s window around the keyword and require text
     to start with "луна"/"luna" (cheap with faster-whisper on GPU);
   - "mute wake word" tray toggle (for calls, movies).
5. Measurement protocol (manual, owner): 2 h of normal room audio (talk, YouTube, game)
   → false accepts/hour; 50 utterances of "Луна, …" at 1 m and 3 m → recall; CPU %.
   Accept if FA ≤ 0.5/h and recall ≥ 90 %.

## Consequences

- Wake word quality is unknown until measured; the hotkey keeps the MVP usable regardless.
- `WakeWordDetector` must expose the keyword end position so one-breath commands work.
- A trained model must not be committed until its training-data licenses are checked
  (owner decision before distribution).
- Training needs a Linux GPU environment (Colab free / WSL2) — not the app venv.

## Owner decisions needed

- Accept FA/recall targets above.
- Allow a short recording session (~100 "Луна" samples + negatives) for training/eval.

## Sources (accessed 2026-10-22)

- openWakeWord README (Windows/onnx, licenses, English-only training, targets): https://github.com/dscripka/openWakeWord
- openWakeWord training notebook: https://github.com/dscripka/openWakeWord/raw/refs/heads/main/notebooks/automatic_model_training.ipynb
- openWakeWord on PyPI: https://pypi.org/pypi/openwakeword/json
- livekit-wakeword docs: https://docs.livekit.io/agents/multimodality/audio/wakeword/ · repo: https://github.com/livekit/livekit-wakeword · PyPI: https://pypi.org/pypi/livekit-wakeword/json
- microWakeWord Russian «Ирина»: https://huggingface.co/droman42/microwakeword-irina-ru
- ACAV100M licensing note (community): https://github.com/briankelley/atlas-voice-training
- Vosk models: https://alphacephei.com/vosk/models · PyPI: https://pypi.org/project/vosk/#files
- Picovoice free tier shutdown (forum, quoting Picovoice): https://community.home-assistant.io/t/fyi-picovoice-confirmed-free-tier-accesskeys-will-stop-working-after-june-30-2026/1012744
