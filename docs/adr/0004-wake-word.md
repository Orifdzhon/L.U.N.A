# ADR 0004 — Wake word "Луна"

- Status: **accepted (with amendment of 2026-10-26)** - owner decisions D7-D14 on Issue #102;
  **amendment 2 (local wake verification, D15, 2026-10-26): accepted** (owner, 2026-10-26)
- Date: 2026-10-22
- Risk: **highest technical risk of the MVP** (spec open question 7)

> The "Amendment (slice 4)" at the end records the owner decisions D7-D14 and the slice-4 design
> (shared microphone stream, half-duplex gating, follow-up window, packaging). Where it differs
> from the Decision below (verification stage, "Silero VAD gate", 2 s command rule), **the
> amendment wins**. "Amendment 2" (local second-stage verification with faster-whisper) replaces
> D12 "no verification stage" and wins over amendment 1 where they differ. Measured recall /
> false accepts are added by Issue #115.

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

- ~~Accept FA/recall targets above.~~ **Decided** (D7, see the amendment).
- ~~Allow a short recording session~~ **Decided** (D14): yes, later, run by the owner (#115).

## Sources (accessed 2026-10-22)

- openWakeWord README (Windows/onnx, licenses, English-only training, targets): https://github.com/dscripka/openWakeWord
- openWakeWord training notebook: https://github.com/dscripka/openWakeWord/raw/refs/heads/main/notebooks/automatic_model_training.ipynb
- openWakeWord on PyPI: https://pypi.org/pypi/openwakeword/json
- livekit-wakeword docs: https://docs.livekit.io/agents/multimodality/audio/wakeword/ · repo: https://github.com/livekit/livekit-wakeword · PyPI: https://pypi.org/pypi/livekit-wakeword/json
- microWakeWord Russian «Ирина»: https://huggingface.co/droman42/microwakeword-irina-ru
- ACAV100M licensing note (community): https://github.com/briankelley/atlas-voice-training
- Vosk models: https://alphacephei.com/vosk/models · PyPI: https://pypi.org/project/vosk/#files
- Picovoice free tier shutdown (forum, quoting Picovoice): https://community.home-assistant.io/t/fyi-picovoice-confirmed-free-tier-accesskeys-will-stop-working-after-june-30-2026/1012744
- vosk 0.3.45 files (wheels `py3-none-win_amd64`, `manylinux` x86_64/aarch64, `armv7l`, all
  uploaded 2022-12-14; no macOS wheel, no sdist; `requires_python >=3`; deps `cffi, requests,
  tqdm, srt, websockets`), accessed 2026-10-26: https://pypi.org/pypi/vosk/0.3.45/json
- vosk-model-small-ru-0.22 (45 MB, Apache-2.0,
  https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip), accessed 2026-10-26:
  https://alphacephei.com/vosk/models

## Amendment (slice 4, 2026-10-26): owner decisions D7-D14 and the slice-4 design (Issue #102)

Owner decisions of 2026-10-26 (comment on Issue #102; questions in ROADMAP "Slice 4"). Facts that
shaped the design: `Recorder.record()` opens the microphone and stops it in `finally`, so an
always-on stream needs one owner; replies are played through speakers next to a webcam microphone
(Logitech C270), so the assistant hears itself; the owner's microphone is quiet (noise floor
~0.0001 RMS, voice 0.005-0.016). The interfaces are in ARCHITECTURE §3, the state/gating tables
in §4.2, threads in §5, settings in §6.

### Owner decisions

| # | Decision | Rationale (one line) |
|---|---|---|
| D7 | Targets stay **FA <= 0.5/h, recall >= 90 %**. If #115 measures FA 0.5-2/h, wake mode still ships **opt-in** (`[wakeword] enabled = false`); the hotkey stays. | The hotkey keeps the MVP usable, so a mediocre detector is an option, not a blocker. |
| D8 | One-breath commands ("Луна, открой ютуб"). Start cue = short beep, **off by default** (`[wakeword] cue`). No spoken "да?". | A spoken cue costs TTS latency and is itself echo into the open microphone. |
| D9 | Follow-up window implemented (#113) but **off by default** (`follow_up_s = 0`; ~6 s when on). | Natural multi-turn talk vs. an open ear for room speech after every reply; the owner decides after living with it. |
| D10 | **Half-duplex**: the detector is muted while TTS speaks plus a ~400 ms tail; speech is interrupted with the hotkey (barge-in as in slice 3). Voice "Луна, стоп" is deferred to slice 6. | Voice barge-in needs echo cancellation or a headset; speakers + C270 would self-trigger. |
| D11 | **Keep the audio after the keyword** in the same recording. | A fresh recording after the keyword loses the first words of a one-breath command. |
| D12 | **No verification stage** (no STT check of the keyword window) in slice 4. **Superseded by D15** (amendment 2): a *local* verification stage is added; the "no cloud check" part stands. | A cloud check would send audio before intent is confirmed and burn Groq quota; local `small` costs 1-2 s CPU. Revisit only if #115 misses FA. |
| D13 | `vosk` is a **core dependency** (not an extra), imported lazily; the ~45 MB model is downloaded on first enable. | A plain `uv run` re-syncs without extras and uninstalls them (the `sounddevice` / slice-3 D5 lesson). |
| D14 | Calibration recording session later, run by the owner (#115). | It is the only way to know FA/hour for this room, microphone and voice. |

### Design decisions (architect, within D7-D14)

1. **One microphone stream, one owner, one reader.** In wake mode the `WakeListener` owns the
   `MicSource`: it calls `start()` when wake mode is turned on and `stop()` when it is turned off
   (or the device fails). Its thread `luna-wake` is the only caller of `mic.read()`. A recording
   never opens or closes the microphone in wake mode: it reads from a **`FrameTap`** (bounded
   queue) that the listener fills with the same frames. This also covers hotkey turns while wake
   mode is on (one stream, not two) and the `/mic` diagnostic.
   *Why:* handing `read()` rights between threads, or closing and reopening per turn, both have
   races and add 100-300 ms of device start-up exactly when the user is already talking.
   With wake mode off, push-to-talk is unchanged: the `Recorder` starts and stops the mic per turn.
2. **Keyword audio contract (D11).** `WakeEvent.end_sample` counts samples passed to `process()`
   since the detector's last `reset()`; the listener maps it to an absolute stream position
   (samples read since `mic.start()`). At a detection the listener cuts its 2 s ring buffer at that
   position: up to 1 s **before** the cut becomes `lead_in` (the keyword plus context, prepended to
   the recording but not used for endpointing); everything **after** the cut is pre-loaded into the
   tap as backlog, followed by live frames. The `Recorder` runs VAD over the backlog like live
   audio, so a command that started (or even ended) before the detector fired is not lost.
   The keyword stays in the audio on purpose: a cut inside a word is worse than a known leading
   word, which the router already strips (`_WAKE_RE`); a transcript that is only the keyword ends
   the turn silently.
3. **Endpointing after a wake.** The `Recorder` waits up to `[wakeword] command_timeout_s` (5 s)
   for the command, so "Луна" ... pause ... "открой ютуб" works; no speech -> the turn ends
   **silently** (no STT, no LLM, nothing leaves the machine). Its VAD is seeded with the
   listener's noise floor (no 240 ms warm-up that would swallow a one-breath command).
4. **Half-duplex gate (D10).** The detector is fed only while no *hold* is active. Holds:
   `speaking` (from `Speaker.speak()` accepting a reply until playback really ended),
   `turn:<request_id>` (from the start of any voice turn until it ended, i.e. after `on_reply`
   started speech, so the two holds overlap), then `speaking_tail_ms` (400 ms) after the last hold
   is released (output-device buffer and room echo), plus `cooldown_s` (2 s) minimum gap between
   two wake events (`mute_while_speaking = false` drops the `speaking` hold, headset use only). On re-arm the detector is reset and the ring buffer cleared, so the end of a
   reply that contains "Луна" can never fire. The stream stays open while held. The `Speaker`
   exposes `active` + `add_activity_listener(callback)` and knows nothing about wake words (wired
   in the composition root).
5. **Energy VAD gate before Vosk (replaces "Silero VAD gate").** The listener runs the existing
   `EnergyVAD` on every frame and feeds Vosk only inside speech segments (300 ms pre-roll from the
   ring, segment closed after 600 ms of non-speech, detector `reset()` at segment end and after 10 s of continuous
   speech). *Why:* silence is most of the day and continuous Kaldi decoding is the main CPU cost;
   Silero would add an ONNX model to manage for a gate whose only job is saving CPU (precision
   comes from the grammar). Silero stays an option behind `VoiceActivityDetector`.
6. **Follow-up window (D9, #113, off by default).** After a turn delivered a reply, the frontend
   asks the listener for a window of `follow_up_s`; the window starts only when the listener
   re-arms (speech finished + tail), so the reply cannot open a turn. Speech onset inside the window
   starts a turn without the keyword; silence closes it quietly. *Amended 2026-10-26 (owner,
   conversation mode):* the window rolls (every reply restarts a full `follow_up_s`, up to 300 s;
   the "max 3 chained follow-ups" cap is dropped), end phrases and noise/filler transcripts are
   handled silently, and the open window is visible (tray icon, `/wake`). Wake mode only: with wake
   off the microphone stays closed after a reply (slice-3 privacy model).
7. **Privacy.** Before a keyword, audio exists only in memory (2 s ring buffer, tap queue) and is
   never written to disk or sent anywhere; the app has no debug-recording option (calibration
   recordings are made by `bench_wake.py`, #112, into a gitignored folder). After a keyword the
   utterance goes to the STT chain (Groq first), so a false accept followed by room speech uploads
   one short utterance - an accepted trade-off (ADR 0003 amendment: privacy is not the owner's
   priority), limited by the silent no-speech exit. `/wake off` stops the stream at once, clears
   the ring buffer and closes taps. Indicator: `Wake: on | paused | off | failed` in the REPL
   (tray in slice 5) via `WakeStateChanged` events; `/wake on` says that the microphone stays open.
8. **Packaging (D13).** `vosk>=0.3.45,<0.4` in `[project] dependencies` with the marker
   `sys_platform == 'win32' or sys_platform == 'linux'` (no macOS wheel and no sdist exist, so an
   unmarked requirement breaks the universal `uv lock`). The wheel is `py3-none` (cffi, no CPython
   ABI), so Python 3.13 should work - #105 verifies the install first and stops with "needs
   architect" if it does not (fallback: sherpa-onnx, option D). New transitive dependencies:
   `cffi`, `requests`, `tqdm`, `srt`, `websockets`. Model: `vosk-model-small-ru-0.22` from the
   official URL, pinned SHA-256 (computed once in #106 and committed as a constant), downloaded to
   a temp file and unpacked with a zip-slip guard under `%LOCALAPPDATA%\LUNA\models` (`WakeModelStore`,
   `luna/voice/wake_model.py`) on the **first enable** of wake mode, never in tests or at install
   time; `[wakeword] model_dir` points to a pre-downloaded copy for offline use.
9. **Out of slice 4:** ~~verification stage (D12)~~ (superseded by amendment 2: a *local*
   verification stage is in slice 4), "Луна, стоп" during speech (D10), acoustic echo
   cancellation, a custom ONNX model (spike #116 only if #115 misses the targets), automatic
   microphone reconnect (a lost microphone turns wake mode `failed`; `/wake on` retries).

### Consequences

- `Recorder.record` gains a `live` argument; with it the Recorder never starts or stops the
  microphone. Push-to-talk without wake mode keeps its slice-3 behaviour and tests.
- The Windows microphone indicator stays on while wake mode is on; this is stated when it is
  turned on.
- CPU target on the i3-12100F (4C/8T): < 1 % total in a quiet room, <= 5 % total averaged over
  room speech/TV, **zero dropped frames** over the 2 h session (measured in #115).
- Lock order (deadlock rule): `VoiceFrontEnd` lock -> `Speaker` lock -> `WakeListener` lock, never
  the reverse; the listener calls `on_wake` and publishes events without holding its lock.
- The `WakeWordDetector` sketch changes (as implemented in PR #119): frames are `bytes` (PCM16,
  like the rest of `luna.audio`); `WakeEvent(keyword, confidence, end_sample)` with
  `end_sample: int | None` relative to the last `reset()`; when it is `None` the listener cuts at
  "samples fed - 250 ms". There is no separate flush: the listener calls `reset()` at the end of
  each VAD segment.

## Amendment 2 (2026-10-26): local second-stage wake verification (D15) - accepted

### Context

The synthetic smoke run of `scripts/bench_wake.py` (PR #125: SAPI Irina, 7 positive / 21 negative
clips, vosk 0.3.45 + small-ru-0.22) showed:

| Vosk mode | recall | negative clips that fired | CPU s per audio s | wake lag p50 |
|---|---|---|---|---|
| `grammar` (`["луна", "[unk]"]`) | 7/7 | 14-18 of 21 ("лунный", "Лена", "она", "Луначарский", ...); the threshold has almost no effect | ~0.015 | 0.75 s |
| `free` (full small-ru LM) | 6/7 (lost an isolated "Луна") | 3-4 of 21 | ~0.11 (~7x) | 1.2-2.2 s |

Synthetic speech proves nothing about FA/hour, but it shows the structural problem: a grammar
that forces every sound to "луна" or "[unk]" has no competing words, so near-homophones win and
the confidence threshold cannot separate them. Free mode separates better but is slower, ~7x more
CPU and loses recall. A detector that is cheap *and* precise is not available today (the ONNX
spike #116 is conditional and has its own data/licensing risk).

**Owner decision D15 (2026-10-26):** keep Vosk `grammar` as the cheap always-on **first stage**; on
each `WakeEvent` run a **local second stage** with the already-installed faster-whisper (`base`,
CPU int8, the model of `luna.stt.faster_whisper`) and continue only if the utterance really
starts with the address "Луна"; otherwise drop the wake silently and re-arm. **No cloud** in the
check: the D12 rationale (no audio leaves the machine before intent is confirmed, no Groq quota
per false wake) stands. Expected cost per the owner: +0.3-0.5 s latency.

### Options considered

| Option | Pros | Cons |
|---|---|---|
| A. Verify **serially** before recording continues (wake -> check -> record) | simplest control flow | the whole decode time (likely 0.5-1 s on the i3, see below) is added to every command |
| **B. Verify in parallel with the recording** (chosen) | the decode overlaps the user still speaking and the 700 ms end-of-speech silence, so for typical commands the added latency is ~0; no audio is lost (the tap keeps buffering) | one more worker thread; a reject must cancel a running recording; status must be deferred until accepted |
| C. Verify on the `luna-wake` thread | no new thread | blocks the only mic reader for ~1 s: frames back up and the listener falls behind real time (violates "zero dropped frames") |
| D. Re-use the command transcript as the verification (STT the utterance, then check the first word) | no extra decode | the default STT is Groq: the audio of every false wake would be uploaded - exactly what D12/D15 forbid |
| E. Vosk `free` mode as the only stage | no Whisper | ~7x CPU, slower, lost recall on the isolated keyword (above) |

Latency fact that drives B over A: Whisper always encodes a fixed 30 s window, so the encoder
cost does not shrink with a 1.4 s clip; the owner measured ~1 s for a ~2 s clip with `base` int8 on
the i3-12100F (ARCHITECTURE section 11). A short window with few output tokens saves decoder time
only. The owner's +0.3-0.5 s is therefore optimistic for a *serial* check; running it in parallel
hides most of it. The real numbers come from the bench extension (item 12) and #115.

### Decision

1. **Where it runs.** Not on `luna-wake`. `VoiceFrontEnd.on_wake` stays non-blocking and accepts
   the turn as before. The turn thread `luna-voice` (a) waits until the tap holds the
   verification window (`FrameTap.peek`, non-consuming; usually already there, because Vosk
   reports ~0.6-0.75 s after the keyword ends), (b) submits the check to a single worker thread
   `luna-verify` owned by the front end, and (c) runs `Recorder.record(live=...)` on the same tap
   at the same time. The tap buffers everything (400 frames = 12 s), so nothing is lost.
2. **What is verified.** `verify_window(lead_in, after)` = the last **700 ms of `lead_in`** (the
   keyword; real Vosk words measured 0.36-0.47 s) + the first **700 ms after the cut** (enough for
   "-ный свет" when Vosk cut inside "лунный", and for the start of a one-breath command), ~1.4 s
   in total, not padded (silence after an isolated "Луна" is real audio). Decoded with the
   **shared** `FasterWhisperSTT` instance: `language="ru"` (no detection pass), `beam_size=1`,
   `without_timestamps=True`, `max_new_tokens=12`, `condition_on_previous_text=False`,
   `vad_filter=False`, **no `initial_prompt` and no `hotwords`**: biasing the verifier towards
   "Луна" would turn "Лена" into "Луна" and defeat the check.
3. **Accept rule** (pure function `keyword_first(text, words) -> bool`): Unicode NFC, lower case,
   `ё` -> `е`; tokens = runs of letters with inner hyphens (`луна-парк` stays one token);
   **accept iff the first token is in `verify_words`** (default `["луна", "luna"]`). Exact match,
   **no fuzzy distance**: the words to reject are at edit distance 1-2 ("лена", "луну", "луны",
   "она"), so any fuzziness that helps recall lets them through. Rejected by construction:
   "лунный ...", "Лена ...", "она ...", "Луначарский", "Луна-парк", "на луну", empty text (noise).
   Known limit: a sentence that *starts* with the noun ("луна на небе ...", or "полная луна" when
   Whisper drops the cut fragment) passes; it then meets the existing silent exits (keyword-only
   transcript, no speech within 5 s). Measured by the bench negatives, not designed away.
4. **No transcript reuse.** The verifier transcript is not used as the command transcript (it
   covers only ~1.4 s; stitching it with a second pass adds edge cases). The command still goes
   through `STTChain` (Groq -> local). Revisit only if #115 shows the extra pass hurts when Groq is
   down.
5. **Interface.** `WakeVerifier` protocol in `luna/wakeword/verify.py`: `verify(pcm16) ->
   VerifyResult(accepted, reason, text, latency_s)`, `warm_up()`, `close()`.
   `FasterWhisperVerifier(stt: FasterWhisperSTT, words)` uses the STT provider's model instance
   (one model in RAM, one decode lock) through a new `FasterWhisperSTT.transcribe_short(...)`.
   `PassThroughVerifier` (`verify = "off"`, or the degraded fallback) accepts everything with
   reason `"off"`. Sketches in ARCHITECTURE section 3.
6. **Settings.** `[wakeword] verify = "local" | "off"` (default **`"local"`**) and `verify_words =
   ["луна", "luna"]`. Window lengths and the timeout are module constants (bench flags override
   them), not settings. The verifier always uses the `[stt.local]` model (`base`); if the owner
   switches that to `small`, verification gets ~2x slower (stated in the example config).
7. **Loading.** The composition root passes the `FasterWhisperSTT` already built for the STT
   chain; if `[stt] chain` has no `"local"`, it builds one from `[stt.local]` (lazy, same class).
   `VoiceInput.enable_wake()` calls `verifier.warm_up()` **synchronously** after the Vosk model is
   loaded (progress line "Loading the wake verification model..."): it waits for the model (the
   `stt-warm-up` thread may already be loading the same instance; the load lock prevents a double
   load) and decodes 0.5 s of silence once to prime CTranslate2, so the first wake pays no load
   latency. The first-ever enable may download `base` (~145 MB) - same progress path as the Vosk
   download.
8. **Reject.** Silent: no beep, no `StatusChanged`, nothing spoken, nothing sent anywhere. The front
   end cancels the running recording (turn `cancel` event -> tap closed), releases the turn hold,
   and the listener re-arms normally after the 2 s cooldown that started at the detection (no
   extra cooldown). INFO line `wake rejected: verify_ms=..., reason=no_keyword` (counters only);
   the rejected text at DEBUG only (calibration aid, same rule as transcripts). **Status and cue
   are deferred** until accepted: on a verified wake turn `StatusChanged(LISTENING)` and the
   optional beep (D8) happen when the verifier accepts; `speaker.stop()` (reachable only with
   `mute_while_speaking = false`) also moves to the accept.
9. **Verifier errors: fail open** (**owner decision 2026-10-26**). A decode exception or no
   result `VERIFY_TIMEOUT_S` (3 s) after submission counts as `accepted=True, reason="error" |
   "timeout"` with a WARNING log (once per distinct reason per session). If the model cannot be
   loaded at `/wake on`, wake mode still starts with `PassThroughVerifier` and the REPL says "Wake
   verification unavailable (<reason>); every detection starts a turn".
   *Why open:* failing closed makes the assistant deaf without feedback (the owner would debug a
   "broken microphone"); failing open degrades to the amendment-1 baseline, which was designed to
   be livable (silent exits, hotkey). The cost of open is an occasional unneeded Groq upload,
   which the ADR 0003 amendment accepts. (`fail_open=False` exists for tests and the bench only.)
10. **What is not verified.** Hotkey turns and follow-up-window turns (`WakeTrigger.event is
    None`: there is no keyword). A hotkey press on a wake turn that is still unverified is the
    usual early stop (toggle table) **and overrides verification** (the user clearly wants to
    talk; the late result is ignored).
11. **Privacy.** Unchanged before the keyword. After a Vosk detection the verification window
    exists only in memory and is decoded locally; **a rejected wake never leaves the machine**.
    Only accepted wakes reach `STTChain` (Groq first). This removes most of the "a false accept
    uploads one utterance" trade-off of amendment 1, item 7.
12. **Measurement.** `bench_wake.py` gets a second stage (`--verify local|off`): for each stage-1
    event it cuts `verify_window` from the clip exactly as production, runs the same
    `FasterWhisperVerifier`, and reports recall / FA count / FA per hour **after** stage 2, the
    stage-2 false rejects on positives, and verify decode time p50/p95 (wall and CPU). The D7
    targets (FA <= 0.5/h, recall >= 90 %) apply to the **two-stage** pipeline; #115 reports both
    stages. Grammar mode with a low threshold becomes the natural stage 1 (high recall; the
    verifier supplies precision); #115 picks the final values.

### Consequences

- RAM: no new model (the `base` instance is shared with local STT), but wake mode now always loads
  it (~0.3-0.5 GB est., pending #87), even when Groq is the active STT provider.
- CPU: one ~1 s burst of multi-core CTranslate2 work per Vosk detection (grammar mode may fire
  several times per hour of room speech). The listener is held during the turn, so the burst does
  not compete with Vosk; the PortAudio callback is not CPU-bound.
- Latency: ~0 extra for commands that take longer to say than the decode; up to the decode time
  (~0.5-1 s, to be measured) for very short commands ("Луна, стоп"); for "Луна" + pause +
  command the wait is hidden in the pause.
- Code: `luna/wakeword/verify.py` (new), `FasterWhisperSTT.transcribe_short`, `FrameTap.peek`,
  `[wakeword] verify` / `verify_words`, front-end wake flow (#110), wiring (#111), bench (new
  Issue). The `WakeWordDetector` and `WakeListener` contracts are unchanged.
- Risk: Whisper `base` on the quiet C270 may mis-hear the owner's "Луна" (stage-2 false rejects).
  The bench reports them separately; if they are high, the options are `small` for verification
  only (a second model, ~1 GB RAM, slower) or the ONNX spike #116.

### Impact on slice-4 Issues (for the planner)

| Issue | Change |
|---|---|
| new: verifier | `luna/wakeword/verify.py` (protocol, `FasterWhisperVerifier`, `PassThroughVerifier`, `keyword_first`, `verify_window`), `FasterWhisperSTT.transcribe_short`, `FrameTap.peek`, settings `verify` / `verify_words`. Before #110. |
| #110 | wake turn flow: peek window, submit to `luna-verify`, record in parallel, deferred status/cue/`speaker.stop()`, silent reject, fail open, hotkey override; `set_live(..., verifier)`. Blocked by the verifier Issue. |
| #111 | build the verifier (share the chain's `FasterWhisperSTT`), synchronous warm-up with a progress line in `enable_wake()`, degrade note + `PassThroughVerifier`, `/wake on` line shows `verify: local` or `verify: off`. |
| new: bench | `bench_wake.py --verify local` / `--verify off` two-stage metrics (#112 is merged, so a follow-up Issue). Before #115. |
| #114 | docs: `[wakeword] verify`, what a rejected wake looks like (nothing), manual check "лунный свет" / "Лена" do not start a turn. |
| #115 | measure both stages; D7 verdict on the two-stage pipeline; record verify latency and stage-2 false rejects; tune stage-1 threshold for recall. |

### Owner decisions (2026-10-26, in chat): amendment accepted

- **Verifier error / timeout: fail open** (accept the wake, one WARNING per distinct reason), as
  in item 9.
- **Accept rule: first token only**, exact forms `луна` / `luna`, no punctuation requirement
  (item 3). The stricter variant "keyword followed by punctuation or end of text" may appear only
  as an extra bench column.

### Sources

- PR #125 synthetic run (numbers above), accessed 2026-10-26: https://github.com/Orifdzhon/L.U.N.A/pull/125
- faster-whisper 1.2.1 (`uv.lock`): `WhisperModel.transcribe` parameters `language`,
  `without_timestamps`, `max_new_tokens`, `initial_prompt`, `hotwords` checked in the installed
  package (`faster_whisper/transcribe.py`) on 2026-10-26.
