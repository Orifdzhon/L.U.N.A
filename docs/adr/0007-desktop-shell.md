# ADR 0007 — Desktop shell: tray, UI toolkit, process model, autostart

- Status: **proposed**
- Date: 2026-10-22

## Context

F7/F8: a tray icon, a small window showing the reply text (and, per the owner's
interview, accepting typed input), status indication (listening / thinking /
speaking), start from a terminal, optional autostart with Windows. One developer;
Windows now, other OSes later.

## Options considered

### UI toolkit

| Option | License | Pros | Cons |
|---|---|---|---|
| **PySide6** 6.12.0 | LGPL-3.0 / GPL (dynamic linking via wheels is LGPL-compliant) | tray (`QSystemTrayIcon`), frameless popup, animations, signals for cross-thread updates, dialogs for confirmations — one toolkit; abi3 wheel, Python 3.10–3.15; cross-platform | ~150–200 MB install; Qt learning curve |
| tkinter + pystray | PSF / LGPL-3.0 | tiny, stdlib | two event loops (tk main thread + pystray thread), dated look, awkward animations |
| Web UI (pywebview / local FastAPI page) | various | owner knows web stack | extra process/browser engine; tray still needs another lib |
| Flet / Dear PyGui | Apache / MIT | modern look | tray support weak or missing |

### Process model

| Option | Pros | Cons |
|---|---|---|
| **Single process**, Qt main thread + worker threads (ARCHITECTURE §5); Piper as child process | simple to run/debug; one log | a native crash in an ML library kills the app |
| Core as a background service + UI client over IPC | robust, enables future web/mobile clients | IPC protocol, two processes to manage — premature |

The core is UI-agnostic (adapters), so a split into service + clients is possible later
(e.g. a FastAPI wrapper for a web client) without rewriting it.

### Global hotkey

| Option | Pros | Cons |
|---|---|---|
| **Win32 `RegisterHotKey` via `ctypes`** in a thread with a message loop | no dependency, no low-level keyboard hook, OS-level conflict detection | Windows-only (behind `HotkeyListener`) |
| `pynput` (LGPL-3.0) / `keyboard` (MIT) | cross-platform-ish | global keyboard hooks (look like keyloggers to AV, need care) |

### Autostart

| Option | Pros | Cons |
|---|---|---|
| **`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`** value via stdlib `winreg` | per-user, no admin, no extra deps, visible in Task Manager → Startup | registry write (only when the user enables it) |
| Startup-folder `.lnk` | visible as a file | creating `.lnk` needs COM (`pywin32`) |
| Task Scheduler | delays, conditions | overkill, may need elevation |

## Decision

- **PySide6** for tray + popup window + confirmation dialogs (slice 5). Until then the
  CLI is the UI.
- **Single process**; Qt owns the main thread; `EventBus` → Qt signals bridge.
- Window: small frameless popup near the tray showing the last request/reply, a text
  input line, and a status indicator (simple colored/animated icon states first;
  fancy animation is owner's call, spec Q2). Tray menu: show window, mute wake word,
  offline mode, open config, open logs, autostart on/off, quit.
- Entry points: `luna` (console, CLI/tray with logs in terminal) and `luna-gui`
  (`gui-scripts`, no console window via `pythonw`).
- **Hotkey:** `RegisterHotKey` in `luna.platform.windows.hotkey` (default
  `Ctrl+Alt+Space`, configurable).
- **Autostart:** HKCU `Run` value `LUNA` = `"<venv>\Scripts\luna-gui.exe"`, toggled
  only from the tray/config by the user (never enabled silently). Behind the
  `Autostart` protocol; tests use a fake registry.

## Consequences

- PySide6 is an optional `ui` dependency group, keeping CI for the core light.
- Packaging into a single `.exe` (PyInstaller/Nuitka) is post-MVP; until then the app
  runs from the uv-managed venv.
- Agents must not touch the real registry during development (CLAUDE.md); autostart is
  verified manually by the owner.

## Sources (accessed 2026-10-22)

- PySide6 on PyPI (6.12.0, license, abi3 wheels): https://pypi.org/pypi/PySide6/json
- Win32 `RegisterHotKey`: https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-registerhotkey (not re-fetched this session; long-standing API)
- Run registry keys: https://learn.microsoft.com/windows/win32/setupapi/run-and-runonce-registry-keys (not re-fetched this session)

## Amendment (2026-10-26): hotkey implemented, toggle push-to-talk (Issue #84)

The hotkey part of this ADR is decided by the owner (D2, D3 in ROADMAP slice 3) and implemented in
`luna.platform.windows.hotkey.WindowsHotkeyListener` (PR #90, wired into the app in #98). The
rest of this ADR (PySide6, autostart) stays proposed until slice 5.

- `RegisterHotKey` with `MOD_NOREPEAT`, `hWnd=NULL`, on a dedicated `luna-hotkey` message-loop
  thread; `on_press` runs on a separate `luna-hotkey-cb` worker fed by a bounded queue (8), so a
  slow callback never blocks the message loop. **No low-level keyboard hook.**
- Consequence: only presses are reported, so push-to-talk is a **toggle** (press to start, VAD or
  a second press to stop). The interaction model lives in ARCHITECTURE §4.2; `HotkeyListener`
  stays press-only (`start(hotkey, on_press)` / `stop()`).
- Default `ctrl+alt+space`, configurable in `[voice] hotkey`. It collided with another program on
  the owner's PC (they use `ctrl+shift+space`). `ERROR_HOTKEY_ALREADY_REGISTERED` becomes a
  `ToolError` saying the hotkey is taken and pointing to `[voice] hotkey`; the app continues in
  text mode.
