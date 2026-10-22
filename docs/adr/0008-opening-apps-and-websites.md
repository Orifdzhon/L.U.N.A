# ADR 0008 — Opening apps and websites on Windows

- Status: **accepted** (owner, 2026-10-23)
- Date: 2026-10-22

## Context

F3/F4: "открой ютуб", "open GitHub", "открой Telegram", "запусти хром". Names arrive
from STT in Cyrillic or Latin, sometimes transliterated ("телеграм", "хром",
"ютуб"). The owner's Windows UI is Russian, so some app names are localized
("Калькулятор", "Блокнот"). Apps include classic Win32 programs, per-user installs
(Telegram) and UWP/Store apps. Only cataloged apps may be launched (safety §3.3).

## Options considered

### App discovery

| Option | Pros | Cons |
|---|---|---|
| **`Get-StartApps`** (PowerShell `StartLayout` module) → `Name`, `AppID` (AppUserModelID) | one list covering Win32 shortcuts and UWP apps, as the Start menu shows them; localized names | spawns PowerShell (~1 s) → run at startup + on demand, cache to JSON |
| Scan `.lnk` in `%ProgramData%` and `%AppData%\Microsoft\Windows\Start Menu\Programs` | pure Python | misses UWP; `.lnk` target parsing needs COM or a parser |
| Enumerate `shell:AppsFolder` via COM (`pywin32`/`comtypes`) | same data as Get-StartApps, in-process | extra dependency, more code |
| Registry `App Paths` / Uninstall keys | exe paths | incomplete, noisy |

Verified on the owner's machine (2026-10-22): `Get-StartApps` returned **193** entries,
e.g. `Google Chrome → Chrome`, `Microsoft Edge → MSEdge`,
`Telegram → Telegram.TelegramDesktop`, `Блокнот → {1AC14E77-…}\notepad.exe`,
`Калькулятор → Microsoft.WindowsCalculator_8wekyb3d8bbwe!App`.

### Launching

Every `AppID` from `Get-StartApps` is launchable as
`explorer.exe shell:AppsFolder\<AppID>` (equivalently `os.startfile("shell:AppsFolder\\<AppID>")`).
This works for UWP and Win32 entries alike and starts the app as the user would from
Start. (**To verify in slice 1 with Telegram, Chrome, Calculator.**) No command-line
arguments are passed.

### Name matching

- Normalization: lowercase, strip "открой/запусти/open/…", strip punctuation and
  filler ("пожалуйста", "please"), `ё→е`.
- Alias tables (`data/app_aliases.toml`, user overrides in `luna.toml`):
  "хром" → "Google Chrome", "телега"/"телеграм" → "Telegram", "калькулятор" →
  "Калькулятор"/"Calculator", "блокнот" → "Notepad".
- Fuzzy matching with **`rapidfuzz`** (MIT, 3.14.6) `WRatio` over both the original
  and a Cyrillic→Latin transliteration of the query and the app names; accept if
  score ≥ 85 and the best candidate leads the second by ≥ 5; otherwise ask the LLM
  / user to disambiguate ("Нашла Telegram и Telegram Web. Какой открыть?").

### Websites

- `data/site_aliases.toml`: "ютуб"/"youtube" → `https://www.youtube.com`,
  "гитхаб"/"github" → `https://github.com`, etc. (user-extendable).
- Explicit domains in the query ("открой habr.com") → `https://` + domain.
- Unknown site names → LLM tool call `open_website(url)`; the tool validates the
  scheme (`http`/`https` only), rejects `file:`, `javascript:` and local paths.
- "Открой браузер" → default browser home via `webbrowser.open("about:blank")` or the
  browser's AppID (owner preference).
- Opening uses stdlib `webbrowser.open(url)` (default browser) behind `BrowserOpener`.

## Decision

- `WindowsAppCatalog` = `Get-StartApps | ConvertTo-Json` via
  `powershell -NoProfile -NonInteractive`, cached to `%LOCALAPPDATA%\LUNA\apps.json`,
  refreshed at startup in the background and when a lookup fails.
- `WindowsAppLauncher` = `shell:AppsFolder\<AppID>`.
- Alias tables + `rapidfuzz` with transliteration as described; thresholds configurable.
- `BrowserOpener` = `webbrowser`; site aliases + domain detection; LLM fallback with
  URL validation.
- All of it behind `AppCatalog` / `AppLauncher` / `BrowserOpener` (ARCHITECTURE §3);
  Linux/macOS implementations later (`.desktop` files / `open -a`).

## Consequences

- Only apps visible in the Start menu can be launched — acceptable and safer than
  arbitrary executables. Portable apps can be added via aliases pointing to a
  Start-menu name (no raw paths in MVP).
- PowerShell startup cost (~1 s) is paid in the background, not per request.
- Tests use a fixture JSON from `Get-StartApps` (anonymized) and a fake launcher.

## Sources (accessed 2026-10-22)

- Get-StartApps: https://learn.microsoft.com/en-us/powershell/module/startlayout/get-startapps
- rapidfuzz on PyPI: https://pypi.org/pypi/rapidfuzz/json
- Python `webbrowser`: https://docs.python.org/3/library/webbrowser.html (not re-fetched this session)
- Local check on the owner's PC: `Get-StartApps` (count 193, sample entries above), 2026-10-22.
