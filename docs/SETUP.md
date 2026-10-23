# L.U.N.A. setup guide (slices 1-2: text mode with spoken replies)

L.U.N.A. runs as a text REPL: you type requests, it opens apps and sites, searches the web and
answers, optionally out loud (see [Voice](#voice-text-to-speech)). Voice input and the wake word
come in later slices. For the checks after setup see [MANUAL_CHECKS.md](MANUAL_CHECKS.md).

## 1. Prerequisites

- Windows 10 or 11 (opening apps uses the Start Menu app list, so it is Windows-only; on other
  systems the app starts with "open <app>" disabled).
- `git`.
- [`uv`](https://docs.astral.sh/uv/), which also installs Python 3.13 for you:

```powershell
winget install astral-sh.uv
```

Open a new terminal afterwards so `uv` is on `PATH`.

## 2. Install

```powershell
git clone https://github.com/Orifdzhon/L.U.N.A.git
cd L.U.N.A
uv sync --all-extras --dev
uv run luna --version
```

## 3. API keys (free tiers)

L.U.N.A. tries LLM providers in order Gemini, Groq, local. Any subset works. Get keys here:

- Gemini: Google AI Studio, <https://aistudio.google.com/apikey>
- Groq: <https://console.groq.com/keys>

Then create your `.env` and fill in the values:

```powershell
Copy-Item .env.example .env
notepad .env        # GEMINI_API_KEY=...   GROQ_API_KEY=...
```

Save it as UTF-8 text. `.env` is gitignored: never commit it, never paste keys into issues or
logs. Keys are looked up in this order: process environment variables, `./.env`,
`%APPDATA%\LUNA\.env`. `TAVILY_API_KEY` is only needed for the (not yet available) Tavily search
provider; the default search (`ddgs`) needs no key.

Free tiers mean your prompts may be used by the provider to improve its models; do not type
anything private (see ADR 0002).

## 4. Local fallback: LM Studio (optional)

The `local` provider keeps answers working offline or when the cloud providers are rate limited.

1. Install [LM Studio](https://lmstudio.ai/) and download a model that supports tool calling
   (a ~4B Qwen instruct model is a good start).
2. Start the server: Developer tab, set Status to Running (or run `lms server start`). It listens
   on `http://localhost:1234/v1`.
3. Find the exact model id (the name must match what the server reports):

   ```powershell
   curl.exe http://localhost:1234/v1/models
   ```

   Use the `id` value of the model you want.
4. Put it into your config (section 5):

   ```toml
   [llm.providers.local]
   model = "<exact model id from /v1/models>"
   ```

For Ollama instead, set `base_url = "http://localhost:11434/v1"` and `model` to the Ollama tag.

## 5. Configuration file

Copy the annotated example; every key is optional and your values are merged over the defaults:

```powershell
New-Item -ItemType Directory -Force "$env:APPDATA\LUNA" | Out-Null
Copy-Item luna.example.toml "$env:APPDATA\LUNA\luna.toml"
notepad "$env:APPDATA\LUNA\luna.toml"
```

`luna.toml` is searched in this order, the first existing file wins:

1. `--config PATH` (an explicit path must exist, otherwise L.U.N.A. exits with a config error)
2. `%APPDATA%\LUNA\luna.toml`
3. `./luna.toml` in the current directory

No file at all means defaults. A broken file prints `luna: config error: ...` naming the key and
exits with code 2. API keys never go into the TOML.

### Provider chain

```toml
[llm]
chain = ["gemini", "groq", "local"]   # tried left to right
```

A provider without its key is skipped (shown at startup and by `/providers`); the local provider
needs no key. The chain may only name providers defined under `[llm.providers.*]`. A provider
that is rate limited or down goes on cooldown (1 minute, growing up to 15 minutes, or the
server's `Retry-After`) and the next one is used; the REPL prints `[provider: groq]` or
`[offline mode: using the local model]` when it switches. Models and base URLs are changed in the
same sections (see `luna.example.toml`).

## 6. Where files live

| What | Location |
|---|---|
| App log (rotating, 2 MB x 5) | `%LOCALAPPDATA%\LUNA\logs\luna.log` |
| Action log (one JSON line per tool call, monthly files) | `%LOCALAPPDATA%\LUNA\logs\actions-YYYY-MM.jsonl` |
| Start Menu app cache | `%LOCALAPPDATA%\LUNA\apps.json` (delete it to force a rescan) |
| Config | `%APPDATA%\LUNA\luna.toml` (see section 5) |

Secrets are masked in both logs. Raise `[app] log_level = "DEBUG"` for more detail (it then also
shows up in the console).

## 7. Run

```powershell
uv run luna --text
```

On start it prints the active providers and any skipped ones. REPL commands:

| Input | Effect |
|---|---|
| `/help` | short help |
| `/providers` | active chain, model notes, providers skipped for a missing key |
| `/clear` | forget the conversation context |
| `exit`, `quit`, `выход`, end of input | quit |
| Ctrl+C during a request | cancel that request |
| Ctrl+C at the prompt | asks to repeat; a second one quits |

Example requests: `открой ютуб`, `open telegram`, `какой курс доллара?`, `что такое черная дыра?`.

## Voice (text-to-speech)

Replies can be spoken with the Windows built-in SAPI voices (ADR 0005). Voice is off by default.

**Install the audio backend** (adds `sounddevice`; `--all-extras` from section 2 already includes it):

```powershell
uv sync --extra voice
```

**Turn it on** for one run with `--voice`, or permanently with `[tts] enabled = true`:

```powershell
uv run luna --text --voice
```

`--voice` and `--no-voice` override `tts.enabled` in both directions and cannot be combined.
Without either flag the config decides.

### `[tts]` settings

| Key | Default | Range / meaning |
|---|---|---|
| `enabled` | `false` | speak replies |
| `provider` | `"sapi"` | only `sapi` exists so far |
| `voice_ru` | `"Irina"` | case-insensitive part of an installed voice name, used for Russian replies |
| `voice_en` | `"Zira"` | same, for English replies |
| `rate` | `0` | -10 (slow) .. 10 (fast) |
| `volume` | `100` | 0 .. 100 |
| `max_chars` | `1500` | >= 1; longer replies are cut at a sentence end for speech (the text is still printed in full) |
| `first_chunk_max_chars` | `200` | >= 1; size limit of the first spoken chunk, smaller means sound starts sooner |

The voice follows the language of the whole reply. If no installed voice matches the name, any
voice of that language is used, and failing that the Windows default voice (a warning with the
installed voices goes to `luna.log`).

### Commands while speaking

| Input | Effect |
|---|---|
| `/stop` | stop speaking (`Nothing is being spoken.` if idle) |
| `/mute` | toggle mute; muting also cuts the current speech and replies are only printed |
| `/voice` | show state and settings (`Voice on: ru=Irina, en=Zira, rate=0, volume=100`) |
| Ctrl+C while a reply is spoken | only silences the speech (`(speech stopped)`); the next Ctrl+C acts as usual |
| any new input | stops the old speech first |

Apologies for a cancelled or timed-out request are printed but never spoken. Markdown, URLs and
emoji are removed before speaking.

### Windows voices

SAPI voices come from `System.Speech`, which sees the **Desktop (SAPI5)** voices: typically
Microsoft **Irina** (Russian), **Zira** and **David** (English). The newer "OneCore" voices used by
Narrator (for example Pavel) are **not visible** to it and cannot be used. List what L.U.N.A. can
use:

```powershell
powershell -NoProfile -Command "Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices() | % { $_.VoiceInfo.Name + ' ' + $_.VoiceInfo.Culture.Name }"
```

To add a voice: Windows Settings -> Time & language -> Speech -> Manage voices -> Add voices (or
install the language pack). A voice added there may be OneCore-only; if it does not show up in the
list above it cannot be used. Then set `voice_ru` / `voice_en` to part of its name.

Known limits: the SAPI voices sound robotic, and this is Windows-only. Piper (more natural voices)
is planned as an optional provider later.

## 8. Troubleshooting

- **Gemini: "User location is not supported".** Google blocks the free API in some regions. L.U.N.A.
  treats it as "provider unavailable" and falls through to Groq or the local model. Use a key
  from a supported region, or remove `gemini` from `chain`.
- **Groq 429 / rate limit.** Free-tier limits (per minute and per day). The provider is put on
  cooldown and the next one answers. Wait, or lower the request rate; the benchmark has
  `--delay` for this.
- **LM Studio not reachable.** Check that the server is Running and the port is 1234
  (`curl.exe http://localhost:1234/v1/models`); adjust `base_url` otherwise. With the server
  down, answers fail only if no other provider is available.
- **Local model: wrong model id.** `/providers` prints a note while `model` is still the placeholder
  `qwen3-4b`. Copy the exact id from `/v1/models`; the server rejects unknown names.
- **"LLM providers: none".** No keys and no reachable local server. Commands like `open youtube`
  still work through the router; answers and search questions do not.
- **Cyrillic shows as `?` or garbage.** L.U.N.A. switches stdin/stdout to UTF-8 itself, which
  covers PowerShell 5.1 and Windows Terminal. In a legacy `cmd.exe` use a TrueType font (for
  example Consolas) or run `chcp 65001` first.
- **First "open <app>" is slow or fails right after start.** The Start Menu app list
  (`Get-StartApps`, run through `powershell.exe -NoProfile -Command`, so the script execution
  policy does not apply) is scanned once in the background and then cached; the prompt appears at
  once but the first app lookup may wait a few seconds. If the scan fails (PowerShell blocked by
  group policy, timeout), a warning goes to `luna.log` and `open <app>` finds nothing; sites and
  answers still work.
- **`config error: ... tts.provider: Input should be 'sapi'`.** Your `luna.toml` was copied from an
  older `luna.example.toml` that had `provider = "piper"` and a Piper voice such as
  `voice_ru = "ru_RU-dmitri-medium"`. Piper is not supported yet. Replace the whole `[tts]` block
  in `%APPDATA%\LUNA\luna.toml` (or delete it, the defaults are the same apart from `enabled`):

  ```toml
  [tts]
  enabled = true
  provider = "sapi"
  voice_ru = "Irina"
  voice_en = "Zira"
  ```

- **`Voice disabled: ... Continuing in text mode.`** Printed at start when `--voice` / `enabled`
  is on but voice cannot start; text mode keeps working. By reason:
  - `audio backend is not installed; install with 'uv sync --extra voice'` (or `audio backend
    failed to load (...)`): run `uv sync --extra voice`.
  - `no audio output device found`: connect or enable a speaker/headphones in Windows Sound
    settings, then restart.
  - `SAPI voices are only available on Windows`: other systems are not supported yet.
  - `cannot list SAPI voices (...)` or `no SAPI voices are installed`: PowerShell or
    `System.Speech` is blocked or has no voices; see "Windows voices" above.
- **Voice is on but there is no sound, or it plays on the wrong device.** Audio goes to the
  Windows default output device; change it in Windows Sound settings and restart L.U.N.A. Check
  that `/voice` does not say `muted` and that the volume mixer is not muting Python. Speech errors
  are logged in `luna.log` (`speech failed: ...`).
- **The wrong voice or language is used.** A name in `voice_ru` / `voice_en` that matches no
  installed voice falls back to any voice of that language (see "Windows voices"); check the
  warning in `luna.log`.
- **The first sentence starts late.** Each sentence is synthesized by a short-lived PowerShell
  process, so sound starts about 0.6 s after the reply appears (more on a cold start). A lower
  `first_chunk_max_chars` can help for long first sentences.
- **Search says it is unavailable.** Only `provider = "ddgs"` exists so far; `searxng` and
  `tavily` print a "not available yet" warning at start and disable web search.
