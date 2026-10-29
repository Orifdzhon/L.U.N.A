# L.U.N.A. — Product Specification

> Status: **draft v0.1**, agreed in the discovery interview on 2026-10-22.
> Owner: Orif. Changes to scope require the owner's approval.
> Amended 2026-10-29 (owner, ADR 0011 D15): post-MVP features close_app, read-only file search,
> any OpenAI-compatible LLM provider (§3.3, §4, §4.1).

## 1. Vision

L.U.N.A. is a personal voice assistant for the desktop. It runs in the background,
wakes up on the word **"Луна"**, understands Russian and English (including mixed
phrases like "открой YouTube") and performs actions on the computer: opens apps and
websites, answers questions and searches the web.

Unlike browser chatbots, L.U.N.A. is always one spoken phrase away and can act on
the user's own computer.

## 2. Users and distribution

- **Primary user (now):** the author, on their own Windows PC.
- **Later:** open-source project on GitHub, free to use. Advanced features may be
  offered as a paid subscription. Paid features should be server-side services
  (e.g. hosted models, sync, mobile client), because locally run open-source code
  can't enforce payment. Monetization is **out of MVP scope**, but the core must
  stay modular (pluggable skills and providers) so that it can be added later.

## 3. MVP scope

### 3.1 Functional requirements

| ID | Requirement | Example |
|---|---|---|
| F1 | Wake by voice word "Луна"; a global hotkey as a fallback | "Луна, …" |
| F2 | Speech recognition for Russian, English and mixed phrases | "открой YouTube" |
| F3 | Open the default browser, or a specific website by name or alias | "открой ютуб", "open GitHub" |
| F4 | Open a specific installed application by name or alias | "открой Telegram", "запусти хром" |
| F5 | Answer general questions | "что такое черная дыра?" |
| F6 | Search the web and answer from the results (facts, prices, rates) | "какой сейчас курс доллара?" |
| F7 | Reply by voice and show the reply as text in a small window | — |
| F8 | Run in the background: system tray, start from a terminal, optional autostart with Windows | — |
| F9 | Log every executed action locally | — |

Notes:
- Factual and time-sensitive questions (F6) must go through web search, not the
  model's memory, because small models hallucinate.
- Simple commands (F3, F4) should be recognised without a large model where
  possible, for speed.

### 3.2 Non-functional requirements

| ID | Requirement |
|---|---|
| N1 | **Free to run.** Default setup uses local models and/or free API tiers; no paid API is required. |
| N2 | **Platform:** Windows 10/11 only in MVP. Platform-specific code is isolated behind interfaces so Linux/macOS can be added later. |
| N3 | **Reference hardware:** RTX 2060 (6 GB VRAM assumed), 16 GB RAM, Intel Core i3-12100F (4 cores). Everything must fit this machine. |
| N4 | **Latency targets** (from end of speech): simple command ≤ 2 s; answer with web search ≤ 10 s. To be validated by measurement. |
| N5 | **Pluggable LLM providers:** local (Ollama) plus free cloud tiers (Google Gemini, Groq). The owner doesn't prioritise privacy, so cloud providers are acceptable. A local fallback works offline or when free limits run out. |
| N6 | **Always-on listening** for the wake word is acceptable. Wake-word detection runs locally. |
| N7 | API keys and secrets live in local config / environment, never in the repository. |

### 3.3 Safety model

Actions are grouped by risk:

| Level | Examples | Behaviour |
|---|---|---|
| Safe | open app, open website, web search, answer; close an app gracefully (like its X button); find files/folders by name and open or reveal a found document, media file or folder (ADR 0011) | execute immediately |
| Sensitive | camera, microphone recording, sending messages, running scripts | confirmation required (not in MVP) |
| Dangerous | deleting files, arbitrary shell commands, anything touching Windows system files or settings | explicit on-screen confirmation; **not available in MVP** |

Rules:
- L.U.N.A. can only call predefined tools. There is no arbitrary shell or
  file-system tool in the MVP. File search (ADR 0011) is read-only: it reads names only, never
  file contents, never sends files anywhere, never modifies, moves or deletes anything; found
  executables, scripts and shortcuts are only shown in Explorer, never run.
- Closing apps (ADR 0011) is graceful (the app may ask about unsaved data). Force-closing a hung
  app happens only after L.U.N.A. asks and the user explicitly agrees. System, shell and security
  processes and L.U.N.A. itself are never closed.
- Any operation on Windows system files always requires the owner's confirmation.
- Web content is untrusted data: text from web pages must never trigger tool calls.
  File and folder names found on the disk are treated the same way.

## 4. Out of MVP scope (later)

- Long-term memory about the user.
- Music and media control (Spotify, volume), camera, user-defined scenarios
  (e.g. "game mode"), file operations (creating, moving, deleting files; read-only file search
  is no longer here, see §4.1).
- Linux and macOS.
- Web client (possibly Django/FastAPI on top of the same core), mobile client.
- Subscription, accounts, billing, cloud sync.

### 4.1 Added after the MVP (owner, 2026-10-29, ADR 0011)

| ID | Feature | Example |
|---|---|---|
| P1 | Close a running app by name, gracefully; force-close a hung app only after confirmation | "Луна, закрой телеграм" |
| P2 | Find files/folders by name (RU/EN, fuzzy) in user folders and on fixed drives; open the found item or show it in Explorer | "найди резюме" → "открой первый" |
| P3 | New dark window: animated moon indicator for each state, chat bubbles, push-to-talk button; Russian UI with English fallback | — |
| P4 | Use any OpenAI-compatible LLM API (presets for free and paid providers, custom URL), chosen and ordered in a "Models / providers" dialog; keys in Windows Credential Manager; Groq stays the speech-recognition default | OpenRouter `:free`, Mistral, Qwen |

## 5. Open questions

To be resolved before or during architecture work:

1. Exact VRAM (6 or 12 GB)? Should L.U.N.A. unload models while the owner plays games?
2. UI: tray icon + small popup only, or an animated "listening / thinking / speaking" indicator and dialog history? Is text input needed?
3. Dialog flow: one command per wake word, or a short follow-up window? Interrupt by voice ("Луна, стоп")?
4. Persona and voice: female voice? Short and to the point, or chatty? Reply in the language of the question?
5. Which free providers and limits are actually available at implementation time (Gemini, Groq — verify on official pages).
6. Python version: 3.13 is installed; speech/ML libraries may require 3.12.
7. Custom Russian wake word "Луна": feasibility with openWakeWord / Vosk (highest technical risk).

## 6. MVP definition of done

The MVP is done when, on the reference machine:
- saying "Луна, открой YouTube" opens YouTube in the default browser;
- saying "Луна, открой Telegram" launches the installed app;
- saying "Луна, какой курс доллара?" returns a spoken answer based on a fresh web search;
- a general question gets a spoken and written answer;
- it starts with Windows (when enabled) and lives in the tray;
- all automated tests and lint checks pass in CI.
