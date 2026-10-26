# ADR 0009 — Desktop shell for the MVP (slice 5): tray, window, background process, autostart

- Status: **accepted** (owner, 2026-10-26; decisions D1-D9 below)
- Date: 2026-10-26
- Refines [ADR 0007](0007-desktop-shell.md). Replaces its autostart mechanism (HKCU `Run` →
  Startup-folder shortcut), its frameless popup (→ a plain small window) and its entry points.
  The hotkey part of ADR 0007 (implemented, amendment of 2026-10-26) is unchanged.

## Context

Slice 5 is the last MVP slice. PRODUCT_SPEC F7/F8 and §6: L.U.N.A. must live in the tray, show
replies as text in a small window, run in the background and optionally start with Windows. The
owner wants the MVP finished on 2026-10-27, so this ADR picks the **smallest** design that makes
L.U.N.A. an everyday background assistant. The installer/.exe and speed work are post-MVP.

What exists (slices 0-4): a text-first core (`build_app` → `App` with `Orchestrator`, `EventBus`,
`Speaker`, `VoiceInput`), the CLI REPL on the main thread, and background threads `luna-hotkey`,
`luna-hotkey-cb`, `luna-voice`, `luna-wake`, `luna-verify`, `luna-speaker*`, `luna-req-*`
(ARCHITECTURE §5). No asyncio. PR #134 (merged 2026-10-26 as 233909e) adds `VoiceInput.enable_wake(progress)` (blocking:
first call downloads/loads models), `disable_wake()`, `wake_active`, `wake_state`,
`wake_status_line()`, and `build_app(..., wake_progress=)`, which runs `enable_wake` synchronously.

Constraints: one developer, Claude Pro limits, Windows now / Linux and macOS later, free to run,
nothing new leaves the machine, agents never touch the real Startup folder or registry.

## Options considered

### 1. GUI toolkit and tray library

Versions and wheels checked on PyPI on 2026-10-26.

| Option | Version / license | Py 3.13 Windows wheel | Threading | Notes |
|---|---|---|---|---|
| **A. PySide6-Essentials** (QtWidgets: `QSystemTrayIcon`, `QWidget`) | 6.12.0 (2026-10-08); LGPL-3.0 / GPL-2.0 / GPL-3.0 | `cp310-abi3-win_amd64`, 82 MB (+ `shiboken6` 1.3 MB); Python >=3.10,<3.16 | one loop: Qt owns the main thread; worker threads hand results over with a queued `Signal` (thread-safe by design) | tray, menu, notifications (`showMessage`), window, DPI scaling, icons drawn with `QPainter` (no Pillow, no assets); actively maintained; works on macOS/Linux later. The full `PySide6` meta-package also pulls WebEngine/Addons/Pdf — not needed |
| B. tkinter (stdlib, Tk 8.6 verified in the project venv) + **pystray** + Pillow | pystray 0.19.5 (last release 2023-09-17), LGPL-3.0; Pillow 13.0.0, MIT-CMU | pystray pure Python + `six`; Pillow `cp313-win_amd64` 7.3 MB | **two loops**: Tk mainloop on the main thread, pystray `run_detached`/`run` on its own thread (allowed on Windows only); every menu callback must be marshalled to Tk through a polled queue (`after()`) because Tk is not thread-safe | ~10x smaller; but pystray is unmaintained for 3 years, its `notify()` is unsupported on macOS/Xorg, and on macOS both pystray and Tk require the main thread (no path to macOS without a rewrite); manual DPI awareness; dated look |
| C. pywebview (window) + a tray library | pywebview 6.2.1, BSD-3-Clause; needs `pythonnet` + WebView2 on Windows | pure Python + .NET bridge | webview loop on the main thread + tray thread, JS↔Python bridge | the owner knows web tech, but it is a third runtime (Edge WebView2 + .NET) and still needs pystray for the tray: most moving parts |

**Recommendation: A (PySide6-Essentials).** One event loop, one thread-hand-off mechanism,
notifications and DPI for free, and a future on Linux/macOS. The costs (an 82 MB wheel, est.
60-100 MB RAM) are small next to `ctranslate2`/`onnxruntime` already installed, and irrelevant
on 16 GB. B is the fallback if PySide6 fails to install or start on the reference PC.

### 2. Process model

| Option | Pros | Cons |
|---|---|---|
| **Single process, Qt on the main thread**, `build_app` unchanged, existing threads untouched | no IPC; same object graph as the CLI; one log | a native crash kills the tray too (same as today) |
| Core service + tray client over IPC | robust, enables web/mobile clients later | protocol + two processes: premature (ADR 0007) |

Windowless start: `pythonw.exe -m luna --tray` (no console). Under `pythonw`, `sys.stdout` and
`sys.stderr` are `None`: `print()` is a no-op, but `stream.write` (tqdm in model downloads,
library warnings) raises. So the entry point replaces missing streams before anything else runs.

Single instance:

| Option | Pros | Cons |
|---|---|---|
| **Named mutex** `CreateMutexW(NULL, FALSE, "Local\\LUNA-instance")`, `ERROR_ALREADY_EXISTS` = taken; handle kept for the process lifetime | released by the OS on any exit, including crashes; ~20 lines of `ctypes`; per session | Windows-only (behind a protocol); a squatter in the same session could block start (irrelevant for a personal PC, per the MS docs remark) |
| Lock file in `%LOCALAPPDATA%` | cross-platform idea | stale files after a crash unless an OS lock is used (also OS-specific) |
| `QLocalServer` | cross-platform, can also "show the existing window" | ties the CLI to Qt; stale sockets on Unix |

### 3. Autostart

| Option | Pros | Cons |
|---|---|---|
| **Per-user Startup folder shortcut** `L.U.N.A.lnk` in `FOLDERID_Startup` (`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`) | no registry; visible and deletable as a file; listed in Task Manager → Startup; per user, no admin | a `.lnk` needs COM: created by a short PowerShell `WScript.Shell` script (values passed via environment variables, never interpolated), ~0.5-1 s per toggle |
| HKCU `...\CurrentVersion\Run` value (ADR 0007) | stdlib `winreg`, simplest code | registry write; the owner prefers no registry |
| Task Scheduler | delays, conditions | overkill, may need elevation |
| `.cmd`/`.bat` in Startup | no COM | flashes a console window at login |

### 4. Window shape

| Option | Pros | Cons |
|---|---|---|
| **Plain small window** (title bar, resizable, ~440×540), close hides it to the tray | standard behaviour, no positioning code, works with any taskbar location | less "assistant-like" |
| Frameless popup anchored at the tray (ADR 0007) | looks native | tray geometry differs per taskbar position / multi-monitor; focus-loss hiding is fiddly — post-MVP polish |

## Decision

1. **Toolkit: `PySide6-Essentials` 6.12.x** as a core dependency (not an extra: a plain `uv run`
   would uninstall an extra, the slice-2/4 lesson). Imported lazily: only tray mode imports Qt;
   `--text` and the tests of the core never do.
2. **Single process.** Qt owns the main thread in tray mode; `build_app`, the core and all
   existing threads are unchanged. A toolkit-free **`ShellController`** holds all shell logic
   and talks to thin Qt views through protocols (`TrayView`, `WindowView`, `UiThread`), so the
   logic is unit-tested with fakes and no Qt.
3. **Entry points:** `luna` with no mode flag starts tray mode (owner decision D3); `luna --text`
   stays the CLI; new flags `--tray` (explicit) and `--minimized` (start with the window hidden).
   The windowless launch is `<venv>\Scripts\pythonw.exe -m luna --tray --minimized` (used by
   autostart; the owner can pin the same command). No `gui-scripts` launcher in the MVP.
4. **Windowless safety:** missing `stdout`/`stderr` are replaced by `os.devnull` writers;
   `sys.excepthook` and `threading.excepthook` log unhandled exceptions (PySide6 routes slot
   exceptions through `sys.excepthook`); `faulthandler` writes native crashes to
   `%LOCALAPPDATA%\LUNA\logs\crash.log`. Progress lines (wake model download) go to the log at INFO
   and to the window status line.
5. **Single instance:** `SingleInstance` protocol; Windows: named mutex `Local\LUNA-instance`.
   Taken in **both** tray and `--text` mode (two instances would fight over the hotkey and the
   microphone). A second launch exits with code 3: `--text` prints "L.U.N.A. is already running
   (see the tray icon)"; tray mode shows the same text in a `QMessageBox`. Activating the existing
   window from a second launch is post-MVP.
6. **Autostart:** `Autostart` protocol revised to take a `LaunchCommand(executable, args,
   working_dir)`; Windows: a Startup-folder shortcut `L.U.N.A.lnk` (folder from
   `SHGetKnownFolderPath(FOLDERID_Startup)`, default path as fallback) created by PowerShell
   `WScript.Shell.CreateShortcut` with paths passed in environment variables; `disable()`
   deletes exactly that file; `is_enabled()` = the file exists. **Off by default**, changed only
   by the user clicking "Start with Windows" in the tray menu. The command is built by the app
   from `sys.executable` (→ `pythonw.exe`), fixed flags, the current working directory (so
   `./luna.toml` and `./.env` are found) and `--config <abs path>` only if `--config` was given.
   Never from window or LLM text. The `[ui] autostart` key of the owner's config is legacy:
   accepted, ignored, one warning if it is `true`.
7. **Tray menu (minimal):** *Open L.U.N.A.* (default item, also left click) · *Listen for «Луна»*
   (checkable, wake on/off) · *Voice replies* (checkable, `Speaker.set_muted`; disabled when
   `[tts] enabled = false`) · *Start with Windows* (checkable) · *Quit*. Toggles are
   **session-only**; persistent defaults stay in `luna.toml`.
8. **Icon states**, drawn at runtime: `IDLE` (grey), `WAKE` (blue: listening for «Луна»),
   `RECORDING` (red), `THINKING` (amber), `SPEAKING` (green), `ERROR` (red "!"). Derived by a pure
   function from the last `StatusChanged`, the wake state (`WakeStateChanged` / `wake_state`) and
   a transient error flag (5 s after `StatusChanged(ERROR)`; persistent while the wake listener is
   `FAILED`). Tooltip = the same state in words.
9. **Window (minimal):** conversation (user lines from typed text and `TranscriptReady`, assistant
   lines from `ReplyReady`, one action line per `ToolResult` in `reply.actions`; summaries of
   `untrusted` results are not shown), a text input that calls `Orchestrator.handle` exactly like
   the CLI (Enter sends, Esc cancels the running typed request), and a status line (state, wake,
   voice, last LLM provider). In-memory only, last `[ui] history_lines` lines. Closing hides the
   window to the tray (one notification per session says so); *Quit* is only in the tray menu.
10. **Notifications:** `QSystemTrayIcon.showMessage` for errors only — wake listener `FAILED`
    (e.g. microphone lost), degraded start (voice input / wake word disabled, no LLM provider),
    autostart toggle failure — rate-limited to one per reason per 5 minutes, `[ui] notifications`
    (default `true`). Reply text and transcripts are never put into notifications.
11. **Threads added (tray mode only):** the Qt main thread; `luna-ui-events` (reads one bus
    subscription and posts events to the GUI thread); `luna-ui-worker` (one **daemon** thread
    with a queue for blocking shell actions: `enable_wake`, `disable_wake`, autostart toggles; not
    a `ThreadPoolExecutor`, whose threads are joined at interpreter exit and would block quitting
    during a model download); `luna-ui-req-<n>` (one per typed request, waits for the
    orchestrator like the REPL). Wake mode is started **after** the tray is visible
    (`build_app(..., start_wake=False)`, then `enable_wake` on `luna-ui-worker`), so a first-run
    model download never blocks the GUI.
12. **Quit:** hide the tray icon, stop accepting input, close the bus subscription, then
    `App.close()` on the GUI thread (bounded by the existing joins, a few seconds), then
    `QApplication.quit()`. A daemon watchdog calls `os._exit(0)` (logged) if shutdown takes more
    than 10 s. Ctrl+C in a console-started tray quits the same way.
13. **Security/privacy:** no new network access and no new data leaves the machine; window text
    is untrusted user input with exactly the CLI's trust level (same Router/Assistant/tool
    safety); the conversation is not persisted; notifications carry no user content; the
    autostart command contains no user or LLM text; agents and tests never touch the real Startup
    folder (fakes and `tmp_path`; the real toggle is a manual owner check).

## Owner decisions needed

| # | Question | Recommendation | Alternative / trade-off |
|---|---|---|---|
| D1 | GUI toolkit | `PySide6-Essentials` (one loop, notifications, DPI, macOS/Linux later) | tkinter + pystray: ~10x smaller, but two loops, unmaintained pystray, no macOS path |
| D2 | Autostart mechanism | Startup-folder shortcut `L.U.N.A.lnk` (no registry) | HKCU `Run` value: simpler code (`winreg`), but a registry write |
| D3 | `luna` without a mode flag | starts tray mode (everyday use); `--text` for the CLI | keep printing the usage; tray only with `--tray` |
| D4 | Single instance scope | both tray and `--text` (one owner of mic + hotkey) | tray only: `--text` for debugging while the tray runs, but the hotkey/mic then conflict |
| D5 | Tray toggles (wake, voice, autostart is a file anyway) | session-only; defaults stay in `luna.toml` | persist into `luna.toml`: needs a comment-preserving TOML writer (`tomlkit`), post-MVP |
| D6 | Notifications | errors only, `[ui] notifications = true` | none at all; or also "reply ready" (privacy on screen share) |
| D7 | Window | plain small window, close hides to tray, English labels like the CLI | frameless popup at the tray; Russian labels (i18n later) |
| D8 | Legacy `[ui] autostart` | ignored (warning if `true`); the tray menu is the only switch | `true` creates the shortcut at start: a silent write outside the app, against "only by explicit action" |
| D9 | Flags in the autostart command | `--tray --minimized [--config]` only; voice/listen/wake come from `luna.toml` | copy the current session flags (`--wake` etc.) into the shortcut: surprising when config changes |

### Owner decisions (2026-10-26, relayed by the coordinator from the owner's chat)

- **D1:** PySide6-Essentials.
- **D3:** `luna` without flags starts the tray **and shows the window**; `--text` stays the CLI.
- **D2, D4, D5, D6, D7, D8, D9:** accepted as recommended in the table above.
- ADR 0009 **accepted**.
- PR #134 (wake wiring: `--wake`, `/wake`, `enable_wake` / `disable_wake`) is merged into `main`
  (233909e), so it no longer blocks the slice-5 Issues "ShellController" and "Tray entry point
  and wiring".

## Consequences

- +82 MB download / est. +60-100 MB RAM in tray mode; nothing changes for `--text`.
- CI needs no config change: Qt view smoke tests run with `QT_QPA_PLATFORM=offscreen` set in
  `tests/conftest.py`; the real tray icon, notifications and the Startup shortcut are manual
  checks (`desktop` marker / `docs/MANUAL_CHECKS.md`).
- `Autostart.enable(command: str)` in `luna/platform/base.py` changes shape before anyone
  implements it (no implementation exists yet).
- The shortcut points at the venv it was created from: enable autostart from the main checkout,
  never from an agent worktree. Moving the repo means toggling autostart off and on.
- If the owner disables the entry in Task Manager → Startup, the file still exists and the menu
  shows "on" while Windows skips it (documented limitation).
- Post-MVP: popup anchored at the tray, "show existing window" from a second launch, settings
  dialog / persisted toggles, open config/logs menu items, offline/game-mode toggles, packaging
  (PyInstaller/Nuitka; LGPL Qt is shipped as replaceable DLLs).

## Sources (accessed 2026-10-26)

- PySide6-Essentials 6.12.0 (license, abi3 wheel size, requires-python): https://pypi.org/pypi/PySide6-Essentials/json ; PySide6 meta-package deps: https://pypi.org/pypi/PySide6/json
- pystray 0.19.5 (LGPLv3, release date): https://pypi.org/pypi/pystray/json ; threading rules (`run()` main thread, Windows exception, macOS, `run_detached`, `notify` unsupported on macOS/Xorg): https://pystray.readthedocs.io/en/latest/usage.html
- Pillow 13.0.0: https://pypi.org/pypi/pillow/json ; pywebview 6.2.1: https://pypi.org/pypi/pywebview/json
- `QSystemTrayIcon` (`showMessage`, `activated`, `messageClicked`, context menu ownership): https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QSystemTrayIcon.html
- `CreateMutexW` (`ERROR_ALREADY_EXISTS`, Local/Global namespaces, handle closed at process exit, squatting remark): https://learn.microsoft.com/windows/win32/api/synchapi/nf-synchapi-createmutexw
- `FOLDERID_Startup` (per user, `%APPDATA%\Microsoft\Windows\Start Menu\Programs\StartUp`): https://learn.microsoft.com/windows/win32/shell/knownfolderid
- uv runs `gui_scripts` entry points and `.pyw` through `pythonw` (not used in the MVP): https://docs-next.astral.sh/uv/guides/scripts
- Local check: the project venv (Python 3.13.1) imports `tkinter` (Tk 8.6) and has `.venv\Scripts\pythonw.exe`.
