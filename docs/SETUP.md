# L.U.N.A. setup guide (text mode, voice, wake word, tray)

L.U.N.A. lives in the system tray with a small window (see [Tray mode](#tray-mode-and-autostart)),
or runs as a text REPL (`--text`). You type or say requests ("Луна, ..."), it opens apps and sites,
searches the web and answers, optionally out loud (see [Voice](#voice-text-to-speech)) and by
voice via the hotkey or the wake word (see [Voice input](#voice-input-push-to-talk) and
[Wake word](#wake-word)). For the checks after setup see [MANUAL_CHECKS.md](MANUAL_CHECKS.md).

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

L.U.N.A. tries LLM providers in order Groq, Gemini, local. Any subset works. Get keys here:

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
chain = ["groq", "gemini", "local"]   # tried left to right
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
| Native crash dump (only for a start without a console, e.g. `pythonw`; usually empty) | `%LOCALAPPDATA%\LUNA\logs\crash.log` |
| Action log (one JSON line per tool call, monthly files) | `%LOCALAPPDATA%\LUNA\logs\actions-YYYY-MM.jsonl` |
| Start Menu app cache | `%LOCALAPPDATA%\LUNA\apps.json` (delete it to force a rescan) |
| Config | `%APPDATA%\LUNA\luna.toml` (see section 5) |

Secrets are masked in both logs. Raise `[app] log_level = "DEBUG"` for more detail (it then also
shows up in the console).

## 7. Run

| Command | What it does |
|---|---|
| `uv run luna` | tray icon + window (same as `--tray`) |
| `uv run luna --tray --minimized` | tray mode with the window hidden (also `[ui] start_minimized = true`) |
| `uv run luna --text` | console REPL (below) |
| `<venv>\Scripts\pythonw.exe -m luna --tray --minimized` | windowless: no console at all (what autostart runs) |

Only one instance runs at a time (tray and `--text` share the microphone and the hotkey). A
second launch exits with code 3 and the message `L.U.N.A. is already running (see the tray
icon)`; in tray mode it is shown in a message box. It does not raise the existing window; use the
tray icon.

The rest of this section describes the console REPL (`uv run luna --text`). On start it prints the active providers and any skipped ones. REPL commands:

| Input | Effect |
|---|---|
| `/help` | short help |
| `/providers` | active chain, model notes, providers skipped for a missing key |
| `/clear` | forget the conversation context |
| `exit`, `quit`, `выход`, end of input | quit |
| Ctrl+C during a request | cancel that request |
| Ctrl+C at the prompt | asks to repeat; a second one quits |

Example requests: `открой ютуб`, `open telegram`, `какой курс доллара?`, `что такое черная дыра?`.

## Wake word

Say "Луна, <request>" instead of pressing the hotkey. Off by default: the microphone is opened
only with `--wake`, `[wakeword] enabled = true`, the `/wake` command (text mode) or the tray
toggle "Listen for «Луна»". `--wake` implies `--listen`; `--no-wake` overrides the config.

```powershell
uv run luna --text --voice --wake
```

Type `/wake` to see the state, `/wake on` / `/wake off` to switch it. In tray mode use the
checkable tray item "Listen for «Луна»" (it reads "Starting wake word..." while models load).
Tray toggles last for the session; defaults live in `luna.toml`.

**First run downloads models.** Enabling the wake word downloads the Vosk model `small-ru`
(pinned by URL and SHA-256) and, for the second-stage check (`verify = "local"`), the
faster-whisper model of `[stt.local]` (`base`, ~150 MB) into `%LOCALAPPDATA%\LUNA\models`. In
tray mode this happens in the background after the icon appears; progress goes to `luna.log` and
the window status line. If C: is short on space, move the models:

```toml
[wakeword]
model_dir = "D:/LUNA/models"

[stt.local]
model_dir = "D:/LUNA/models"
```

`[wakeword] model_dir` defaults to the same folder as `[stt.local] model_dir`. Other keys
(`threshold`, `cooldown_s`, `cue`, `follow_up_s`, `verify`, ...) are annotated in
`luna.example.toml`. `[stt] language` (default `"ru"`) applies to wake commands too. Spoken
replies fall back from Edge to SAPI when the network fails (see [Voice](#voice-text-to-speech)).

### Conversation mode (`follow_up_s`)

With `follow_up_s = 90` under `[wakeword]` (default `0` = off, maximum `300`) L.U.N.A. keeps
listening after she answered a wake or hotkey turn: you can give the next command **without
saying «Луна»**. Every new command restarts the window; after `follow_up_s` seconds without
speech it closes quietly and only the wake word works again. The window opens when the reply has
been spoken (plus `speaking_tail_ms`), so she never hears herself. The tray icon shows "listening"
while it is open and `/wake` prints `conversation: 63 s left`.

- End it at once by saying «спасибо», «стоп», «хватит», «всё» or «отбой» (also «Луна, отбой»;
  English: "stop", "thanks", "that's all"). Nothing is answered and nothing is sent to the LLM.
- Empty, one-letter and filler transcripts («угу», «ага», «м-м») are ignored without a reply.
- Pressing the hotkey or saying «Луна» during the window works as usual.
- **Privacy:** while the window is open, any speech in the room (including a TV) is recorded and
  sent to the configured speech recognition (Groq by default), exactly like a hotkey turn. Use a
  short window, or `0`, if that matters.

### Wake-word calibration (#115)

To tune `threshold` and friends for your voice and room, record your own clips and run the
two-stage benchmark (stage 1: Vosk; stage 2: the production faster-whisper verifier). Recordings
stay outside the repository and are never committed:

```powershell
uv run python scripts/bench_wake.py --record D:\luna-clips --tag 1m
uv run python scripts/bench_wake.py --clips D:\luna-clips --verify local --md wake.md
```

`--record DIR` asks you to say the phrases (`--tag` is a file-name suffix such as `1m` or `3m`
for the distance to the microphone; `--record-seconds` sets the length). `--negatives` takes a
long WAV of ordinary speech or TV to count false accepts per hour; `--verify local` adds the
second stage; `--md` / `--out` save the table / JSON. All options:
`uv run python scripts/bench_wake.py --help`. Put the best `threshold` into `[wakeword]` and
restart.

## Tray mode and autostart

`uv run luna` shows a tray icon and a small window (conversation, a text box, a status line).
Closing the window hides it to the tray; **Quit** is only in the tray menu. Typed requests behave
like the console REPL (Enter sends, Esc cancels a running request); history is kept in memory
only (`[ui] history_lines`, 20..2000, default 200).

Tray icon colours: grey idle, blue listening for «Луна», red recording, amber thinking, green
speaking, red "!" error. The tooltip says the same in words.

Tray menu (left click opens the window):

| Item | Effect |
|---|---|
| Listen for «Луна» | wake word on/off (needs a microphone; see [Wake word](#wake-word)) |
| Voice replies | unmute/mute spoken replies; disabled when `[tts] enabled = false` |
| Open window | show the window |
| Start with Windows | create/remove the autostart shortcut |
| Quit | exit (a watchdog forces the exit after 10 s) |

`[ui]` settings: `start_minimized` (default `false`), `notifications` (default `true`: tray
notifications for errors only, such as a lost microphone; never any reply text), `history_lines`.
The old `[ui] autostart` key is ignored (a warning is logged if it is `true`); the tray menu is
the only switch.

### Start with Windows

Off by default. Tick "Start with Windows" in the tray menu: L.U.N.A. writes the shortcut
`L.U.N.A.lnk` into your per-user Startup folder (no registry, no admin). It runs
`<venv>\Scripts\pythonw.exe -m luna --tray --minimized` (plus `--config <path>` if you started
with `--config`) in the folder you started from, so `./luna.toml` and `./.env` are found. Voice,
listen and wake settings come from `luna.toml`, not from the flags of the session that created it.

- **Enable it from your main checkout**, never from a throwaway worktree: the shortcut points at
  the venv it was created from.
- **Moved or re-cloned the repo?** Untick and tick the item again to rewrite the shortcut.
- **Turn it off:** untick the same menu item (deletes the shortcut), or delete the file by hand:
  press Win+R, run `shell:startup`, and delete `L.U.N.A.lnk`. Or in PowerShell:
  `Remove-Item "$([Environment]::GetFolderPath('Startup'))\L.U.N.A.lnk"`.

### Known limits (tray)

- Disabling the entry in Task Manager -> Startup apps is not reflected in the menu: the file still
  exists, so the item stays ticked while Windows skips it.
- No popup anchored at the tray icon; a plain window. A second launch does not raise the window.
- Tray toggles apply to the current session only; edit `luna.toml` for permanent defaults.
- Errors are shown as tray notifications at most once per reason per 5 minutes.
- PySide6-Essentials adds ~82 MB to the install and some RAM in tray mode (`--text` is unaffected).
- Logs: `luna.log` (section 6); `crash.log` collects native crashes of a windowless start.

## Voice input (push-to-talk)

```powershell
uv run luna --text --voice --listen
```

`--listen` / `--no-listen` override `[voice] listen` (default `false`: the microphone is never
opened unless you enable it). At start you should see `Listen: on (Ctrl+Alt+Space)`. `/listen
[on|off]` switches it while running. The hotkey works with or without the wake word (see [Wake word](#wake-word)).

### How it works

The hotkey is a toggle:

1. Press it: L.U.N.A. prints `(listening...)` (and beeps if `[voice] start_beep = true`).
2. Speak. Recording stops by itself after about 0.7 s of silence (`silence_ms`) or after 10 s
   (`max_utterance_s`). Pressing the hotkey again stops it early.
3. The phrase is recognized (`(heard) ...`), handled like a typed request, and the reply is printed
   and, with `--voice`, spoken.
4. Pressing the hotkey while L.U.N.A. is thinking or speaking interrupts that and starts listening
   again (barge-in).

The microphone is open only while recording, so the Windows microphone indicator is off at idle.
Speech is stopped before recording starts, so the speakers do not feed back into the microphone.

### Speech recognition (`[stt]`)

Engines are tried in `[stt] chain` order, default `["groq", "local"]`:

- `groq`: Groq Whisper (`whisper-large-v3-turbo`), fast, needs `GROQ_API_KEY` in `.env` (section 3);
  skipped without the key. Your audio is sent to Groq. A rate limit (429), timeout or no network
  moves on to the next engine for that phrase.
- `local`: faster-whisper on the CPU, model `base` (int8). It works offline. `faster-whisper` is a
  normal dependency, installed by `uv sync`. The model (~150 MB) is downloaded from Hugging Face on
  first use into `%LOCALAPPDATA%\LUNA\models` and loaded in the background right after listening
  is enabled, so the first local recognition can be slow once. To keep it elsewhere (for example on
  drive D) set `[stt.local] model_dir = "D:/LUNA/models"`.

`/listen` prints which engines are active (`Speech recognition: groq -> local.`) and which were
skipped and why. Other `[stt]` keys: `language` (default `"ru"`; `"en"`, another ISO code, or `"auto"` to let the engine detect; auto-detect often mis-hears Russian as Korean or Chinese), `prompt` (bias text
with app or brand names), `timeout_s` (per-engine limit, default 5). `[stt.local] model = "small"`
is more accurate and about twice as slow. `device = "cuda"` needs NVIDIA cuBLAS 12 and cuDNN 9
installed separately (not bundled); on CPU the default is the safe choice.

### Microphone and `[voice]` settings

The microphone is the Windows default input device; to pick another set `[voice] input_device`
to its index or part of its name (a wrong value is reported with the list of available devices).
Windows must also allow it: Settings -> Privacy -> Microphone -> turn on "Let desktop apps access
your microphone".

| Key | Default | Meaning |
|---|---|---|
| `listen` | `false` | allow opening the microphone (same as `--listen`) |
| `hotkey` | `"ctrl+alt+space"` | lowercase names joined by `+`: at least one of `ctrl`, `alt`, `shift`, `win` plus exactly one key |
| `start_beep` | `false` | short beep when listening starts |
| `input_device` | system default | microphone index or part of its name |
| `vad_energy_threshold` | `0.003` | minimum RMS level (0..1) counted as speech |
| `vad_noise_margin` | `3.0` | speech must exceed the noise floor by this factor |
| `silence_ms` | `700` | silence that ends the phrase |
| `min_speech_ms` | `200` | shorter sounds are ignored |
| `preroll_ms` | `300` | audio kept from before speech was detected |
| `max_utterance_s` | `10` | hard stop of one recording |
| `frame_ms` | `30` | audio frame size: 10, 20 or 30 |

**Calibration.** Type `/mic`, stay quiet for a moment, then speak normally. It prints the noise
floor, the speech peak and a suggested `vad_energy_threshold`. Example from a laptop microphone:
floor about `0.0001`, voice `0.005`-`0.016`, so a threshold of `0.003` works. Put the suggested
value in `[voice]` and restart.

### Voice input troubleshooting

- **`Voice input disabled: hotkey '...' is already taken by another program; choose another
  combination in [voice] hotkey`.** Another program owns the combination (Ctrl+Alt+Space was
  taken on one real PC). Pick a free one, for example:

  ```toml
  [voice]
  hotkey = "ctrl+shift+space"
  ```

- **Other `Voice input disabled: ...` reasons** (the app keeps running in text mode):
  - `audio backend is not installed; ...`: run `uv sync`.
  - `no audio input device (microphone) found`: plug in or enable a microphone in Windows Sound
    settings, check the privacy setting above, then type `/listen on`.
  - `no speech recognition engine is available (set GROQ_API_KEY or add 'local' to [stt] chain)`:
    add a key or put `local` in the chain.
  - `global hotkeys are only supported on Windows`.
  - `invalid hotkey ...` or a `config error: voice.hotkey`: fix the combination.
- **Wrong microphone or silence.** Windows chooses the default input device; change it in Sound
  settings or set `input_device`. Run `/mic`: `No speech heard` means the level is too low or the
  wrong device is used.
- **Phrases are cut off or never start.** Lower `vad_energy_threshold` (use the `/mic` value) or
  raise `silence_ms` (for example `1000`) if you pause while speaking.
- **Background noise starts recordings.** Raise `vad_energy_threshold` or `vad_noise_margin`.
- **Recognition is slow.** The local engine runs on the CPU (about 1 s per phrase with `base`).
  Add `GROQ_API_KEY` so Groq answers first; local is then only the fallback.
- **Groq 429 or no network.** The phrase is recognized locally instead; the first local use may
  need to download the model, so it needs the network once.
- **`uv sync` fails with "access denied" on `luna.exe`.** A running `luna` locks its launcher.
  Quit it (`exit`) before `uv sync`; a plain `uv run luna ...` may re-sync the same way.

## Voice (text-to-speech)

Replies can be spoken with natural Microsoft neural voices (online, default) or the Windows
built-in SAPI voices (offline). See ADR 0005. Voice is off by default.

**Privacy and network:** with `provider = "edge"` the text of each spoken reply is sent to
Microsoft's Edge text-to-speech service (through the unofficial `edge-tts` library; the endpoint
may change or break without notice). If the service is unreachable or fails, L.U.N.A. speaks the
same chunk with the Windows SAPI voices instead and logs a warning. The next chunk tries the
online voice again; after two failed chunks in a row it is skipped for `fallback_cooldown_s`.
The first audio must arrive within 5 s, and a failed chunk is retried once immediately.
Text with nothing a voice can pronounce (for example Korean) is skipped, the rest is spoken. Use `provider = "sapi"` to keep everything local.

**Speed.** The first Edge request of a session used to take 4-5 s (DNS, TLS, websocket); L.U.N.A.
now sends one tiny throw-away request in a background thread at startup and again after about
4 minutes without speech (never while muted, never blocking the GUI; failures are only logged at
DEBUG). The first sentence is spoken as soon as it is synthesized, while the next one is prepared.
Every voice turn logs `tts_first_audio_ms` (reply ready -> first sound) and `total_ms` (end of your
speech -> first sound) in its INFO line; typed requests log `speech: first audio N ms after the
reply`. For the Groq `gpt-oss` model the default is `reasoning_effort = "low"` (set it to
`"medium"` in `[llm.providers.groq]` if answers get too shallow).

**Audio backend:** `sounddevice` is a normal dependency on Windows, so the `uv sync` from section 2
installs it. (Do not rely on an optional extra: a plain `uv run` re-syncs without extras and would
uninstall it.)

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
| `provider` | `"edge"` | `"edge"` (online neural voices, SAPI fallback) or `"sapi"` (local only) |
| `edge_voice_ru` | `"ru-RU-SvetlanaNeural"` | Edge voice for Russian replies (`uv run edge-tts --list-voices`) |
| `edge_voice_en` | `"en-US-JennyNeural"` | Edge voice for English replies |
| `voice_ru` | `"Irina"` | SAPI voice (the fallback, or `provider = "sapi"`): case-insensitive part of an installed voice name, used for Russian replies |
| `voice_en` | `"Zira"` | same, for English replies |
| `rate` | `0` | -10 (slow) .. 10 (fast). SAPI: its own scale. Edge: 5 % per step (`-10` = `-50%`, `+10` = `+50%`) |
| `volume` | `100` | 0 .. 100. Edge: offset from full volume (`100` = `+0%`, `50` = `-50%`, `0` = `-100%`) |
| `edge_timeout_s` | `15` | > 0; upper bound of the (length-scaled) time for one online chunk |
| `fallback_cooldown_s` | `15` | >= 0; after 2 failed Edge chunks in a row use SAPI for this long before trying Edge again |
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

Known limits: the SAPI voices sound robotic and are Windows-only; that is why Edge is the default.
Edge needs the network and each sentence takes about 1.5 s to synthesize. Piper (offline natural
voices) is planned as an optional provider later.

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
- **`config error: ... tts.provider: Input should be 'edge' or 'sapi'`.** Your `luna.toml` was copied from an
  older `luna.example.toml` that had `provider = "piper"` and a Piper voice such as
  `voice_ru = "ru_RU-dmitri-medium"`. Piper is not supported yet. Replace the whole `[tts]` block
  in `%APPDATA%\LUNA\luna.toml` (or delete it, the defaults are the same apart from `enabled`):

  ```toml
  [tts]
  enabled = true
  provider = "edge"   # or "sapi" for local-only voices
  ```

- **`Voice disabled: ... Continuing in text mode.`** Printed at start when `--voice` / `enabled`
  is on but voice cannot start; text mode keeps working. By reason:
  - `audio backend is not installed; run `uv sync` to install it` (or `audio backend failed to
    load (...)`): run `uv sync`.
  - `no audio output device found`: connect or enable a speaker/headphones in Windows Sound
    settings, then restart.
  - `SAPI voices are only available on Windows`: other systems are not supported yet.
  - `cannot list SAPI voices (...)` or `no SAPI voices are installed`: PowerShell or
    `System.Speech` is blocked or has no voices; see "Windows voices" above.
- **`No offline fallback voice: ... Speech needs the network.`** Edge is on but SAPI cannot be
  used as a backup (not Windows, no SAPI voices, PowerShell blocked). Without the network nothing
  is spoken; text replies still work.
- **Replies are spoken in a robotic voice.** The online voice was unreachable and SAPI took over
  (see the warnings `edge unavailable (...)` in `luna.log`); Edge is tried again on the next
  chunk, or after `fallback_cooldown_s` following two failures in a row. Check the network or proxy/firewall rules for Microsoft endpoints.
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
