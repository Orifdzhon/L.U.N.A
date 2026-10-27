# L.U.N.A.

Open-source voice assistant for Windows. Call it by voice ("Луна") to open apps and websites, answer questions and search the web.

**Status:** early development. Slices 1 (text mode: `uv run luna --text`), 2 (voice replies: `--voice`) and 3 (push-to-talk voice input: `--listen`) are done; the wake word (`--wake`) and the tray UI (`uv run luna`) are implemented and being verified by hand (see the [roadmap](docs/ROADMAP.md)).

- Product requirements: [docs/PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md)
- Architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), decisions in [docs/adr/](docs/adr/)
- Setup guide: [docs/SETUP.md](docs/SETUP.md)
- Manual checks: [docs/MANUAL_CHECKS.md](docs/MANUAL_CHECKS.md)
- Roadmap: [docs/ROADMAP.md](docs/ROADMAP.md)
- Development rules for humans and AI agents: [CLAUDE.md](CLAUDE.md)

## Download and install (Windows)

Download `LUNA-Setup-<version>-x64.exe` from the [Releases page](https://github.com/Orifdzhon/L.U.N.A/releases) and run it: per-user install, no administrator rights, no Python needed. The setup is not code-signed, so Windows SmartScreen asks for **More info -> Run anyway**. On first start L.U.N.A. asks for a free Groq key (optionally Gemini) and stores it in Windows Credential Manager. Details (keys, data folders, model downloads, uninstall, building the setup yourself): [Install from the setup](docs/SETUP.md#install-from-the-setup).

The public setup has no local Irina voice (Edge and Windows voices only): the Piper runtime brings GPL-3.0 espeak-ng. Third-party licences (including LGPL Qt/PySide6 and edge-tts) are in `THIRD_PARTY_NOTICES.txt` inside the install folder; see [licences](docs/SETUP.md#licences). Maintainers: the proposed release workflow is in [docs/release-workflow.md](docs/release-workflow.md).

## Quick start (from source)

```
winget install astral-sh.uv
git clone https://github.com/Orifdzhon/L.U.N.A.git
cd L.U.N.A
uv sync --all-extras --dev
copy .env.example .env          # add free Gemini / Groq keys (optional, never commit .env)
uv run luna --text
```

Without keys the router still opens apps and sites; answers need an LLM provider (cloud keys or
a local LM Studio model). Add `--voice` (`uv run luna --text --voice`) to hear replies in Irina/Zira
voices (`/mute`, `/stop`, Ctrl+C silences them), and `--listen` to talk by hotkey (Ctrl+Alt+Space; `/listen`, `/mic`):
`uv run luna --text --voice --listen`. Plain `uv run luna` starts the tray icon with a small window (`--minimized` hides it; "Start with Windows" is in the tray menu). Details, config files, troubleshooting: [docs/SETUP.md](docs/SETUP.md).

## Development

Requires [uv](https://docs.astral.sh/uv/) (`winget install astral-sh.uv`) and Python 3.13.

```
uv sync --all-extras --dev      # create .venv and install dependencies
uv run luna --version           # run (also: uv run python -m luna)
uv run ruff check .             # lint
uv run ruff format --check .    # format check (drop --check to apply)
uv run pytest                   # tests (live/gpu/audio markers are skipped by default)
```

Copy `.env.example` to `.env` for API keys (gitignored). Config lives in `luna.toml`
(`--config PATH`, then `%APPDATA%\LUNA\luna.toml`, then `./luna.toml`).

### Text mode

```
uv run luna --text
```

Starts the REPL: type `открой ютуб`, `open telegram`, `какой курс доллара?`; `/help`,
`/providers`, `/clear`; Ctrl+C cancels the running request, `exit` / `выход` / Ctrl+C twice quits.
The first start may take a few seconds longer for "open <app>" while the Start Menu app list
is scanned in the background (the prompt appears at once; the list is cached afterwards).
Without any API key the router still opens apps and sites; answers need an LLM provider.

To use a local LM Studio model, copy `luna.example.toml` to `%APPDATA%\LUNA\luna.toml` and set
`[llm.providers.local] model = "<exact model id shown by LM Studio>"`.

### LLM benchmark

Compares providers on latency and tool-call correctness (input for the default provider order,
ADR 0002). It sends the ~20 RU/EN prompts of `benchmarks/prompts.toml` through the real
Assistant, one provider at a time. Safe to run: apps and the browser are recorders (nothing is
launched or opened), the app catalog is a fixed fake list and web search returns canned results
(`--real-search` uses ddgs). Only the LLM providers use the network; keys come from `.env` and
are never printed.

```
uv run python scripts/benchmark.py                                   # all providers with keys + local
uv run python scripts/benchmark.py --providers gemini,groq --chain --out bench.json
```

Options: `--providers a,b`, `--chain` (also the full fallback chain), `--runs N`, `--delay S`
(pause between prompts, default 2 s, keeps free tiers happy), `--max-failures N` (stop a provider
after N consecutive RateLimited / ProviderUnavailable turns, default 3), `--no-force-search`
(do not let the Assistant run `web_search` itself, so the model must ask for it), `--prompts`,
`--config`.

Columns: `pass` = the expected tool (and arguments) was called, nothing forbidden ran and a
non-empty answer came back; `first call s` = latency of the first LLM request, `total s` = whole
turn including tool rounds; `LLM calls` / `retries` = requests sent / failed ones that were
retried; `error` = exception class of the last failed request; `accuracy %` = share of passed
prompts; `tool ok %` = tool choice only. Safety prompts feed a prompt-injection page to the
model: calling `open_app` / `open_website` afterwards is a failure. A turn that hits the 20 s
request limit counts as `ProviderUnavailable`, so a slow local model may be stopped early; raise
`--max-failures` for it.

### Wake-word benchmark

`scripts/bench_wake.py` picks the detector settings (ADR 0004, target D7: false accepts
<= 0.5/h and recall >= 90 %). For every combination of `mode` (grammar / free) x `threshold` x
`min_word_s` x `report_partials` it streams audio through the real `VoskKeywordSpotter` and reports
recall, false accepts and FA/hour, wake latency (end of the keyword to the event, p50 / p95) and
CPU seconds per audio second, as a markdown table (rows meeting D7 first, marked **yes**) and
optionally JSON. Needs the Vosk model (downloaded on first use to `%LOCALAPPDATA%\LUNA\models`).

```
uv run python scripts/bench_wake.py --synth                      # smoke run, SAPI voice (SYNTHETIC)
uv run python scripts/bench_wake.py --record D:\wake-clips --tag 1m   # record your own clips
uv run python scripts/bench_wake.py --clips D:\wake-clips --negatives D:\wake-long --out wake.json
```

- `--clips DIR`: `manifest.csv` (`file,label[,keyword_end_s[,note]]`, label `positive` or
  `negative`) or the folders `positive/` and `negative/`; 16-bit WAV, other rates and stereo are
  converted. `--record DIR` writes exactly this layout from the default microphone (same capture
  path as production), prompting for each phrase of `benchmarks/wake_phrases.toml`; DIR must be
  outside the repository (audio is never committed). Use `--tag 1m` / `--tag 3m` for distances.
- `--negatives PATH`: a long WAV (or a folder of WAVs) with speech that must not wake the
  assistant: an hour of room audio, a podcast, TV. Streamed from disk. Convert MP3 to WAV first.
  Because every grid point re-reads it, it is analysed only for configurations that already reach
  90 % recall on the clips (`--long-all` forces all).
- A configuration is `yes` only with >= 1 h of negative audio (`--min-negative-hours`); with less it
  is `provisional`, because FA/hour from minutes of audio means nothing.
- `--synth` results are labelled SYNTHETIC: a clean TTS voice proves the pipeline runs, not that the
  detector works in your room. The English phrase is excluded by default (`--skip-categories en`:
  the model is Russian).
- Phrase policy (`benchmarks/wake_phrases.toml`): "Луна" and "Луна, <команда>" in one breath are
  positive; the word as a noun inside a sentence ("луна на небе", "Луна-парк"), near-homophones
  ("лунный", "Луначарский", "она", "лун", "Лена") and ordinary speech are negative.
- Other options: `--modes`, `--thresholds`, `--min-word`, `--partials on|off|both`, `--cooldown`
  (production default 2 s), `--tail` (silence after each clip, so final results are flushed),
  `--md FILE`, `--model-dir`, `--config`.

Latency is audio time only: the event's position minus the keyword end (the manifest's
`keyword_end_s`, else the end the engine reports); processing cost is the separate CPU column.

#### Two-stage pipeline (`--verify local`)

Production wake mode is two-stage (ADR 0004 amendment 2, D15): Vosk fires, then the shared local
faster-whisper model decodes ~1.4 s around the cut and the wake counts only if the transcript
starts with "луна" / "luna". `--verify local` measures that:

```
uv run python scripts/bench_wake.py --synth --verify local --md wake2.md   # SYNTHETIC
uv run python scripts/bench_wake.py --clips D:\wake-clips --negatives D:\wake-long --verify local
```

- Default is `--verify off`; the output is then exactly the stage-1 report above.
- Each stage-1 event is cut with the production `verify_window` (last 700 ms before the cut +
  first 700 ms after; at the end of a clip the window is simply shorter) and checked by the
  production `FasterWhisperVerifier` on the `[stt.local]` model and `[wakeword] verify_words` of
  your config (`--config`). The model is loaded once per run (first use downloads it).
- `--verify-top N` (default 3): only the N configurations with the best stage-1 recall are
  verified; stage 1 runs for the whole grid first.
- An extra "Stage 2" table follows the stage-1 table: recall and FA (count, FA/h) **after** the
  verifier, stage-2 false rejects (count and list of clips: positives the verifier threw out),
  stage-2 rejects on negatives, verify decode wall time p50 / p95 and CPU seconds per check, and a
  `strict` pair of columns for the stricter rule "keyword followed by punctuation or the end of
  the transcript" ("Луна, открой" passes, "Луна открой" does not). The D7 mark in that table
  uses the stage-2 numbers. `--out` JSON gets a `stage2` object per verified configuration.
- The bench runs the verifier fail-closed: a decode error counts as a reject and is reported.

## License

[Apache-2.0](LICENSE)
