# ADR 0010 — Windows installer: frozen app, setup program, first-run keys, release build

- Status: **proposed**
- Date: 2026-10-27
- Deciders: owner (approval, decisions D1-D10 below), architect (proposal)
- Related: [ADR 0001](0001-python-version-packaging-tooling.md) (Python 3.13, uv),
  [ADR 0005](0005-text-to-speech.md) amendments 3-4 (Piper / espeak-ng licence caveat),
  [ADR 0009](0009-desktop-shell-mvp.md) (tray, single instance, Startup-folder autostart)

## Context

The MVP (slices 0-5) runs from a developer checkout: `uv run luna` / `pythonw -m luna --tray`.
The owner wants a downloadable Windows installer **today (2026-10-27)** (post-MVP priority:
"speed + installer"). The design must therefore be the smallest one that works reliably; everything
else is listed under "Later".

Facts checked in the code (`origin/main` 12ece1e):

- Entry `luna.__main__:main`; no flags = tray mode (PySide6-Essentials 6.12), `--text` = CLI REPL,
  `--version` prints `luna 0.1.0` (`luna.__version__`, duplicated in `pyproject.toml`).
- Native parts: PySide6/shiboken6 (Qt DLLs), `ctranslate2` 4.8.4 (bundles `ctranslate2.dll`, Intel
  `libiomp5md.dll`, a 266 KB `cudnn64_9.dll` stub), `onnxruntime` 1.31 (faster-whisper's Silero VAD),
  `sherpa-onnx` + `sherpa-onnx-core` 1.13.8 (own `onnxruntime.dll`, `sherpa-onnx-c-api.dll`; espeak-ng
  GPL-3.0 compiled in), `vosk` 0.3.45 (`libvosk.dll` + MinGW runtime DLLs loaded by `cffi` from the
  package folder), `sounddevice` (PortAudio DLL in `_sounddevice_data`), `av` (FFmpeg DLLs),
  `miniaudio`, `primp` (ddgs), `lxml`. `pywin32` is **not** used (ctypes + PowerShell only).
- Data files needed at run time: `faster_whisper/assets/silero_vad_v6.onnx`,
  `trafilatura/settings.cfg`, data of `justext`, `htmldate`, `courlan`.
- Models are downloaded lazily into `%LOCALAPPDATA%\LUNA\models` (Vosk small-ru 45 MB,
  faster-whisper `base` ~145 MB from Hugging Face, Piper Irina / Kristin ~65 MB each); each store has
  its own `model_dir` override (`[stt.local]`, `[wakeword]`, `[tts.piper]`).
- Config: `--config` → `%APPDATA%\LUNA\luna.toml` → `./luna.toml`. API keys
  (`GROQ_API_KEY`, `GEMINI_API_KEY`, `TAVILY_API_KEY`): environment → `./.env` →
  `%APPDATA%\LUNA\.env` (`luna.config.loader._read_secrets`). **The owner's keys live in the repo's
  `.env` today, so an installed exe (working directory = install folder) will not see them.**
- Autostart: `StartupFolderAutostart` writes `L.U.N.A.lnk` in the user's Startup folder;
  `luna.ui.shell.launch.own_launch_command` builds `<venv>\pythonw.exe -m luna --tray --minimized`
  from `sys.executable` — wrong for a frozen build.
- Single instance: named mutex `Local\LUNA-instance`. Logs: `%LOCALAPPDATA%\LUNA\logs`.
- Default `[tts] provider = "piper"`, `[tts] enabled = false`; `PiperTTS` downloads the voice
  **before** importing `sherpa_onnx` (`PiperTTS._engine` → `store.ensure` → factory).

### Prototype (throw-away, architect's scratch dir, 2026-10-27)

A scratch venv with `uv pip install <repo> pyinstaller keyring` (same versions as `uv.lock` for the
native packages: ctranslate2 4.8.4, PySide6-Essentials 6.12.0, numpy 2.5.4, faster-whisper 1.2.1;
onnxruntime 1.31.1 vs locked 1.31.0) and **PyInstaller 6.22.3** + pyinstaller-hooks-contrib
2026.9, `--onedir`, options `--collect-binaries vosk/ctranslate2/sherpa_onnx`,
`--collect-data faster_whisper/trafilatura/justext/htmldate/courlan/luna`,
`--hidden-import vosk.vosk_cffi`, `--copy-metadata sherpa_onnx`:

| Check | Result |
|---|---|
| Build time (i3-12100F, warm uv cache) | 2 min 50 s clean, 1 min 50 s incremental |
| Bundle size (onedir) | 480 MB, 2 878 files (personal edition); 449 MB public edition (no sherpa, no `opengl32sw.dll`) |
| Compressed (xz preset 6 of the public bundle, as a proxy for Inno's LZMA2) | 131 MB in 2 min 17 s |
| `luna.exe --version` | `luna 0.1.0`, 0.45 s |
| Import of every native package + Silero VAD session + PortAudio device list + trafilatura extract + keyring backend + PySide6 | all OK in 1.9 s; keyring backend `WinVaultKeyring` |
| Real models inside the frozen exe | Vosk small-ru model + grammar recogniser OK (0.6-0.9 s); Piper Irina via sherpa-onnx synthesises OK (0.9 s); faster-whisper `base` OK when loaded alone |
| Single instance in the frozen exe | `luna-cli.exe --text` → "L.U.N.A. is already running", exit 3 (the owner's instance was running) |
| Two executables (windowed `luna.exe` + console `luna-cli.exe`) over one `_internal` | works with one `Analysis`/`PYZ`, two `EXE`, one `COLLECT` |

**Found by the prototype: L.U.N.A.'s own package data is not collected automatically.** The
alias tables `luna/data/site_aliases.toml` and `app_aliases.toml` are read with
`importlib.resources`; without `collect_data_files("luna")` the frozen app raised
`FileNotFoundError` in `default_site_aliases()` ("открой ютуб" would fail). With it: 53 site and 29
app aliases load. Apart from that, no hook was missing beyond the options above; build warnings were
only the harmless `pycparser.lextab/yacctab` hidden imports.

**Side finding (environment, not packaging; same frozen and unfrozen):** the owner's PC was under
heavy memory pressure during the test (about 1.5 GB free RAM and 1.9 GB free commit charge of
32 GB; other sessions running). Loading a Vosk model and then a faster-whisper model in one process
failed with `RuntimeError: mkl_malloc: failed to allocate memory` (3/3), the reverse order with Vosk
"Failed to create a model" (3/3); `CT2_USE_MKL=0` made both orders pass once, and later even a lone
Vosk load failed once and then passed twice (slowly, 2.5-4 s instead of 0.8 s). Each model alone
normally loads. Inconclusive: re-test wake mode + local STT on a normally loaded PC; if it
reproduces, open an Issue (candidate fix `CT2_USE_MKL=0` or loading order). Not a blocker for the
installer.

**Size.** The `av.libs` FFmpeg DLLs (65 MB), `PySide6` (74 MB), `ctranslate2` (61 MB), `vosk`
(52 MB), `onnxruntime` (38 MB), `babel` (31 MB) dominate. The setup is expected around
120-140 MB (Inno `lzma2/max` is usually a little better than xz preset 6; the real number is
recorded in the PR of the installer Issue).

## Options considered

### 1. Freezer (Python → Windows executable)

Versions checked on PyPI 2026-10-27.

| Option | Version / licence | Py 3.13 Windows | Reliability with our native deps | Build time | AV false positives | Size / start-up | Verdict |
|---|---|---|---|---|---|---|---|
| **PyInstaller `--onedir`** | 6.22.3, GPL-2.0 **with bootloader exception** (output may be any licence) | yes (`requires_python <3.16`) | **prototype works today**; hooks-contrib covers PySide6, onnxruntime, sounddevice, av, pydantic, ddgs, lxml; 5 explicit collect options for the rest | ~2-3 min | moderate: the shared bootloader is sometimes flagged; onedir without UPX is the least affected variant | 450-480 MB unpacked; `--version` 0.45 s, plain Python start-up speed | **recommended** |
| Nuitka `--standalone` | 4.3.1 (2026-10-17), AGPL-3.0 + runtime exception for output | yes (3.4-3.14) | good PySide6 plugin; ctranslate2/vosk/sherpa need manual DLL options; not prototyped | 20-60+ min on a 4-core i3 (C compilation of all modules), needs MSVC or a downloaded MinGW/Zig toolchain | lower than PyInstaller in general, but unsigned C binaries are flagged too | somewhat smaller, faster start-up | later, if start-up time or AV becomes the problem |
| cx_Freeze | 8.7.3 (2026-10-23), PSF-2.0 | yes (3.10-3.15) | fewer ready hooks for our stack; same DLL work by hand | minutes | similar to PyInstaller | similar | no advantage |
| Briefcase | 0.4.7 (2026-10-27), BSD-3 | yes (3.11+) | embedded-Python app + MSI via WiX; Toga-oriented, PySide6 template exists; app packages installed as-is (no import tracing) | minutes | low (real python.exe) | larger (whole site-packages) | heavier tool and project layout for one developer |
| Embedded Python + uv "portable" layout (python.org embeddable zip + `uv pip install --target`) | — | yes | most faithful (no freezing at all) | fast | low | ~1 GB unpacked (no pruning); start through `pythonw.exe` | custom launcher, custom update logic: more glue than PyInstaller |

### 2. Setup program

| Option | Version / licence | Per-user install | Effort for us | Verdict |
|---|---|---|---|---|
| **Inno Setup** | 7.1.0 (2026-08-12) and 6.7.3 (2026-05-26); free licence ("any purpose, including commercial"); commercial users **above USD 5 000 revenue are asked** (not required) to buy a licence | yes (`PrivilegesRequired=lowest`, `{autopf}` → `%LOCALAPPDATA%\Programs`) | one `.iss` file, Pascal script for the two custom bits; preinstalled on GitHub `windows-latest` (image 20261021: Inno Setup **6.7.1**) | **recommended** |
| WiX 3/4/5 (MSI) / MSIX | MS-RL / free | MSI per-user is awkward; MSIX needs signing to install at all | XML authoring, component rules, harvesting 2 900 files | no (MSIX impossible unsigned) |
| NSIS | 3.x, zlib licence | yes | script language is lower-level; not on the runner image | no advantage |
| Zip only ("portable") | — | yes | none | fallback if the installer slips; no shortcuts / uninstall |

### 3. Where API keys live for installed users

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **`keyring` 25.7.0 (MIT) → Windows Credential Manager** (`WinVaultKeyring`, verified in the frozen prototype) | standard, encrypted per user by Windows, visible/removable in Control Panel, same API on macOS Keychain / Linux Secret Service later | adds `keyring` + `jaraco.*` (MIT) + `pywin32-ctypes` (BSD-3) | **recommended** |
| DPAPI via ctypes (`CryptProtectData`) into `%APPDATA%\LUNA\secrets.bin` | no dependency | our own crypto glue and file format; Windows-only | fallback |
| `%APPDATA%\LUNA\.env` written by the dialog | already supported by the loader | plain text on disk | **works today with zero code** (manual stop-gap) |

### 4. One or two executables

A windowed exe has `sys.stdout is None`: `argparse --version` writes nowhere, `--text` cannot work.
A console exe flashes a console window when started from the Start menu / autostart.

| Option | Verdict |
|---|---|
| **Two executables over one bundle: `luna.exe` (windowed: tray, Start menu, autostart) + `luna-cli.exe` (console: `--text`, `--version`, `--self-check`, troubleshooting)** | **recommended** (verified in the prototype; costs one extra 5 MB exe stub) |
| Only `luna.exe` windowed | no CLI, no CI smoke test |
| Only a console exe | console window on every start |

## Decision (proposed)

### D1. Freezer: PyInstaller 6.22+ `--onedir`, one spec file

- New dependency group `build = ["pyinstaller>=6.22,<7", "pip-licenses>=5.5,<6"]` (dev-only, not
  shipped; PyInstaller's bootloader exception allows any licence for the output).
- `packaging/luna.spec` (checked in, plain Python): one `Analysis` of `packaging/entry.py`
  (`from luna.__main__ import main; sys.exit(main())`), the collect options from the prototype
  (**including `collect_data_files("luna")`** for the alias tables),
  `excludes=["tkinter"]`, **no UPX**, version resource and icon; two `EXE` (`luna`, `console=False`;
  `luna-cli`, `console=True`), one `COLLECT` → `dist\luna\`.
- `--edition public | personal` (env `LUNA_EDITION` read by the spec, default `public`):
  `public` **excludes `sherpa_onnx`** (no espeak-ng GPL code, no Irina voice downloads);
  `personal` collects it (owner's own machine, ADR 0005 amendment 3 caveat accepted).
- Build environment = the locked project environment: `uv sync --frozen --no-dev --group build`,
  then `uv run --frozen --no-dev --group build pyinstaller packaging/luna.spec` (always the same
  flags: `uv run` re-syncs the environment). No global installs.
- `luna-cli.exe --self-check` (new hidden-in-help CLI option): imports every native package that the
  edition ships (ctranslate2, onnxruntime + Silero VAD session, vosk, sounddevice, PySide6 with the
  `offscreen` platform, sherpa_onnx in personal), the `luna` modules and the shipped alias tables,
  prints one line each, exit 0 / 1. It is the CI smoke test; `--version` alone does not import any native module.

### D2. Setup program: Inno Setup, per-user, no admin

`packaging/luna.iss` (Inno Setup 6.7 syntax, also compiles with 7.x):

- `AppId` fixed GUID; `AppName=L.U.N.A.`; `AppVersion` passed by the build script
  (`/DAppVersion=0.1.0`); `OutputBaseFilename=LUNA-Setup-<version>-x64`.
- `PrivilegesRequired=lowest`, `DefaultDirName={autopf}\L.U.N.A` (= `%LOCALAPPDATA%\Programs\L.U.N.A`;
  **no trailing dot** in folder names), `ArchitecturesAllowed=x64compatible`,
  `ArchitecturesInstallIn64BitMode=x64compatible`, `MinVersion=10.0`, `Compression=lzma2/max`,
  `SolidCompression=yes`, `WizardStyle=modern`, languages English + Russian.
- `[InstallDelete] {app}\_internal` before copying (upgrades never mix DLLs of two versions).
- `AppMutex=LUNA-instance` (the app's `Local\LUNA-instance`, same session namespace) and
  `CloseApplications=yes`: setup/uninstall refuse to continue while L.U.N.A. runs and offer to close it.
- `[Icons]`: Start menu `L.U.N.A.` → `{app}\luna.exe`; optional desktop icon (task, unchecked).
- Task **"Start L.U.N.A. when Windows starts"** (unchecked by default, same as ADR 0009 D2):
  `{userstartup}\L.U.N.A.lnk` → `{app}\luna.exe`, parameters `--tray --minimized`, working dir
  `{app}` — the **same file name** the tray toggle manages, so the tray check mark stays truthful.
  `[UninstallDelete] {userstartup}\L.U.N.A.lnk` also removes a shortcut the tray created.
- `[Files]` also installs `packaging/luna.default.toml` as `{userappdata}\LUNA\luna.toml` with
  `onlyifdoesntexist uninsneveruninstall` (never overwrites the owner's file), plus `LICENSE`,
  `THIRD_PARTY_NOTICES.txt`.
- `[Run]` "Launch L.U.N.A." (checked, `postinstall nowait skipifsilent`).
- Uninstall asks once (`[Code]`, `CurUninstallStepChanged`, default **No**): "Also delete settings,
  logs, downloaded models and saved API keys?" Yes → runs `{app}\luna-cli.exe --forget-keys`
  before files are removed, then deletes `{userappdata}\LUNA` and `{localappdata}\LUNA`. Silent
  uninstall (`/SILENT`) keeps user data.

### D3. First-run keys: a small Qt dialog + Credential Manager

- `luna.config.secrets.CredentialStore` (protocol: `get(name) -> str | None`, `set(name, value)`,
  `delete(name)`) with `KeyringStore(service="L.U.N.A.")` (one generic credential per env-variable
  name) and an in-memory fake for tests. `keyring` is imported lazily inside `KeyringStore`.
- Loader order (developers keep working): **environment → `./.env` → `%APPDATA%\LUNA\.env` →
  Credential Manager.** Only the names the config refers to are looked up (as today).
- `luna.ui.qt.keys_dialog`: fields Groq key (required for the cloud defaults), Gemini key
  (optional), each with "Test" (worker thread, `httpx` GET `…/models` with the key, 10 s timeout:
  "OK" / "Rejected (401/403)" / "No connection"), "Save" (to Credential Manager), "Later".
  Never logs or displays saved keys (only "saved" / "from environment / .env, which overrides the saved key").
- Shown automatically on tray start when **no cloud LLM key** is resolved (every start until a key
  is saved — a nudge, the local LLM path still works); always reachable from the tray menu item
  **"API keys…"** (new 6th item). Saving applies on the next start (restart prompt), no hot reload
  today.
- `luna-cli.exe --forget-keys` deletes the saved credentials (used by the uninstaller).
- Stop-gap if this Issue slips today: copy the repo `.env` to `%APPDATA%\LUNA\.env` (already read).

### D4. Frozen-build adaptations in the app (small, tested)

- `own_launch_command(executable, config, cwd, frozen=getattr(sys, "frozen", False))`: frozen →
  executable = sibling `luna.exe` of `sys.executable` (also when the running exe is `luna-cli.exe`),
  args `--tray --minimized [--config <abs>]` (no `-m luna`), working dir = exe folder. Unfrozen
  unchanged.
- `build_tts` / `make_piper()`: if `importlib.util.find_spec("sherpa_onnx") is None`, skip Piper with
  the existing one-line warning **before** anything is downloaded.
- `packaging/luna.default.toml` (installed only if the user has no config): `[tts] enabled = true`,
  `provider = "edge"` (Edge → Piper if present → SAPI), `[voice] listen = true` (push-to-talk
  hotkey), wake word off (tray toggle). A unit test loads it through `load_settings`.
- Model downloads stay as they are (background warm-up, log lines, notification on failure);
  `model_dir` overrides in `luna.toml` cover a small C: drive.

### D5. Version and icon

- Single source of truth stays `luna.__version__`; the build script fails if it differs from
  `pyproject.toml` or (in CI) from the tag `vX.Y.Z`.
- Windows version resource generated by the build script (PyInstaller `VSVersionInfo` text file in
  `build/`): FileVersion/ProductVersion, ProductName `L.U.N.A.`, CompanyName = owner's name,
  LegalCopyright, OriginalFilename.
- `packaging/luna.ico` (16-256 px), drawn once from the tray idea (moon/circle) by a script
  (`scripts/make_icon.py`, PySide6, output committed). Used by both exes, the setup and the
  Start-menu shortcut.

### D6. Build and release

- `scripts/build_installer.py` (Python, unit-testable helpers; run with
  `uv run --frozen --no-dev --group build python scripts/build_installer.py [--edition public|personal]
  [--skip-setup]`):
  1. check version consistency; 2. generate version resource; 3. `pyinstaller --noconfirm
  --clean packaging/luna.spec`; 4. `dist\luna\luna-cli.exe --self-check`; 5. generate
  `THIRD_PARTY_NOTICES.txt` (D7); 6. find `ISCC.exe` (`%ProgramFiles(x86)%\Inno Setup 6`,
  `%ProgramFiles%\Inno Setup 7`, `%LOCALAPPDATA%\Programs\Inno Setup *`, or `--iscc PATH`) and
  compile → `dist\LUNA-Setup-<ver>-x64.exe`; 7. print size and SHA-256.
- Outputs go to `build/` and `dist/` (already gitignored).
- GitHub Actions: a new workflow `release.yml` (**proposal only — CI config needs the owner's
  approval**), sketch:

```yaml
name: Release
on:
  push:
    tags: ["v*"]
  workflow_dispatch:
permissions:
  contents: write
jobs:
  installer:
    runs-on: windows-latest
    timeout-minutes: 40
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - run: uv sync --frozen --no-dev --group build
      - run: uv run --frozen --no-dev --group build python scripts/build_installer.py --edition public
      # Inno Setup 6.7.1 is preinstalled on windows-latest (image 20261021); the script finds ISCC.
      - uses: actions/upload-artifact@v4
        with:
          name: LUNA-Setup
          path: dist/LUNA-Setup-*.exe
          if-no-files-found: error
      - if: startsWith(github.ref, 'refs/tags/v')
        shell: bash
        env:
          GH_TOKEN: ${{ github.token }}
        run: gh release create "$GITHUB_REF_NAME" dist/LUNA-Setup-*.exe --draft --generate-notes
```

  Draft release: the owner publishes it by hand (no automatic public release).

### D7. Licences and notices

- `THIRD_PARTY_NOTICES.txt` generated at build time from the **build environment**:
  `pip-licenses --from=mixed --with-license-file --with-notice-file --format=plain-vertical
  --ignore-packages pyinstaller pyinstaller-hooks-contrib pip-licenses luna` plus a hand-written
  header (`packaging/NOTICES_HEADER.txt`) for components pip metadata does not describe: Qt 6
  (LGPL-3.0, source at https://download.qt.io/official_releases/qt/ and PySide6 at
  https://code.qt.io/), FFmpeg inside PyAV wheels (licence to be confirmed from the PyAV wheel
  build — `av` metadata says BSD for the binding only), Intel OpenMP/MKL runtime inside ctranslate2,
  MinGW runtime inside vosk (GCC runtime exception), PortAudio, and (personal edition only)
  espeak-ng GPL-3.0 inside sherpa-onnx-core.
- Copyleft found in the bundle (prototype, `pip-licenses`): PySide6-Essentials / shiboken6
  (LGPL-3.0), edge-tts (LGPL-3.0), certifi (MPL-2.0), tqdm (MPL-2.0 AND MIT), tld (MPL-1.1 / GPL /
  LGPL — MPL chosen). All are shipped unmodified as separate files.
- **LGPL compliance for Qt/PySide6** (Qt's published obligations): onedir keeps every Qt and
  PySide6 DLL/`.pyd` as a separate replaceable file (dynamic linking); ship the LGPL text and a
  prominent notice; give the source location of the exact Qt/PySide6 version (written offer or
  link); do not forbid reverse engineering; edge-tts stays a plain replaceable package in
  `_internal`. Installers built with `--onefile` would be avoided for this reason too.
- **GPL espeak-ng / Irina:** public builds (CI, GitHub Releases) are `--edition public`: no
  sherpa-onnx, Edge → SAPI voices. The owner's own build may be `--edition personal`. An optional
  "local voice pack" download is a later improvement (needs a clean-licence voice and phonemizer
  first, ADR 0005 amendment 3).

### D8. Testing

- Unit tests (CI): `own_launch_command` frozen/unfrozen, Piper skip without `sherpa_onnx`, loader
  order with a fake credential store, key dialog logic behind a Qt-free presenter, build-script
  helpers (version check, ISCC lookup, version resource text), default config loads.
- Build smoke (CI release job + local script): `luna-cli.exe --version` and `--self-check`.
- Manual, **Windows Sandbox** (Windows 10 Pro has it as an optional feature: the owner enables
  "Windows Sandbox" once and reboots — a system change only the owner makes). `packaging/sandbox/
  luna-test.wsb` maps `dist\` read-only and opens Explorer there (paths are absolute; the build
  script writes a ready copy into `dist\` with the real host path):

```xml
<Configuration>
  <MappedFolders>
    <MappedFolder>
      <HostFolder>D:\L.U.N.A\dist</HostFolder>
      <SandboxFolder>C:\Users\WDAGUtilityAccount\Desktop\dist</SandboxFolder>
      <ReadOnly>true</ReadOnly>
    </MappedFolder>
  </MappedFolders>
  <AudioInput>Enable</AudioInput>
  <Networking>Enable</Networking>
  <MemoryInMB>6144</MemoryInMB>
  <LogonCommand>
    <Command>explorer.exe C:\Users\WDAGUtilityAccount\Desktop\dist</Command>
  </LogonCommand>
</Configuration>
```

  Checklist (goes to `docs/MANUAL_CHECKS.md`): SmartScreen "More info → Run anyway"; installs
  without a UAC prompt into `%LOCALAPPDATA%\Programs\L.U.N.A`; Start-menu entry starts the tray
  without a console; key dialog appears, "Test" works, key saved (visible in Credential Manager);
  "открой ютуб" typed works; a typed question is answered and spoken (Edge; SAPI voices in the
  sandbox are English-only unless a Russian voice is installed — expected); push-to-talk hotkey
  records; wake toggle downloads Vosk and arms; second start shows "already running"; "Start with
  Windows" task creates `L.U.N.A.lnk` → `luna.exe --tray --minimized`; tray toggle off removes it;
  sign out/in starts it; upgrade by installing the same setup again while running (asks to close);
  uninstall with "keep data" and with "delete data" (folders and credential gone); no files left in
  `%LOCALAPPDATA%\Programs\L.U.N.A`.
- On the owner's real PC once: install, then the MVP definition-of-done phrases by voice.

### D9. Code signing: none now

Unsigned setup and exes show a SmartScreen warning ("Windows protected your PC" → More info → Run
anyway) until the file gains reputation; some antivirus engines may flag the PyInstaller bootloader.
Documented in `docs/SETUP.md` and on the release page, with the SHA-256 of the setup. Later options:
SignPath Foundation (free for OSI-licensed projects; requires automated builds from the repo, a
published signing policy and **all components open source** — so public edition only), Azure
Artifact Signing (formerly Trusted Signing; from USD 9.99/month, individuals only in the USA/Canada
as of 2026-01), a commercial OV certificate; submitting false positives to Microsoft.

### D10. Scope for today vs later

Today (Issues below): D1-D9 as written. **Later:** silent auto-update / update check; signing;
onefile-free size trimming (exclude `opengl32sw.dll`, unused Qt plugins/translations, `babel`
locales, FFmpeg if faster-whisper stops importing `av`); Nuitka trial for start-up time; a models
folder choice in the installer / one `[app] model_dir` for all stores; download progress in the
window; a settings dialog; "activate the running instance" instead of the "already running" box;
the optional local-voice pack; all-users install (`PrivilegesRequiredOverridesAllowed=dialog`);
winget manifest; Linux/macOS packages.

## Implementation plan (Issue drafts, for the planner)

Order: **A, B, C in parallel** (no shared files) → **D** (needs A's `dist\luna` layout) → **E**, **F**.
Must-have today: A, B, D (+ the `.env` stop-gap if C slips).

| # | Issue | Depends on | Acceptance criteria (checkable) |
|---|---|---|---|
| A | Freeze L.U.N.A. with PyInstaller: `packaging/luna.spec`, `packaging/entry.py`, `scripts/build_installer.py` (freeze stage, `--skip-setup`), `build` dependency group, version resource, `packaging/luna.ico`, `--self-check`, `THIRD_PARTY_NOTICES.txt` generation | — | `uv run --frozen --no-dev --group build python scripts/build_installer.py --skip-setup` produces `dist\luna\luna.exe` + `luna-cli.exe`; `dist\luna\luna-cli.exe --version` prints `luna <ver>`; `--self-check` exits 0 for both editions and lists the alias counts; public edition has no `sherpa_onnx` folder; exe Properties → Details show the version; notices file lists PySide6 (LGPL-3.0) and edge-tts; unit tests for version check / ISCC lookup / version-resource text; ruff + pytest green |
| B | Frozen-build adaptations: `own_launch_command` frozen branch, Piper skipped without `sherpa_onnx` (before any download), `packaging/luna.default.toml` + a test that loads it | — | unit tests: frozen → `<dir>\luna.exe --tray --minimized` with cwd `<dir>` (also from `luna-cli.exe`), unfrozen unchanged; with `find_spec` faked to `None` no `PiperModelStore.ensure` call and one warning; default toml loads via `load_settings`; ruff + pytest green |
| C | First-run API key dialog + Credential Manager: `luna.config.secrets` (`CredentialStore`, `KeyringStore`, fake), loader order env → `.env` → `%APPDATA%\LUNA\.env` → Credential Manager, `luna.ui.qt.keys_dialog` with Test/Save/Later, tray item "API keys…", auto-show without cloud keys, `--forget-keys`; new dependency `keyring` | — | unit tests with the fake store: order and blank handling; a saved key is redacted in logs; presenter tests for Test results (200 / 401 / timeout via `respx`); `--forget-keys` deletes both names; manual: dialog saves a key that appears in Credential Manager as `L.U.N.A.` and is used after restart |
| D | Inno Setup installer: `packaging/luna.iss`, setup stage of the build script, uninstall data prompt, `packaging/sandbox/luna-test.wsb`, Sandbox checklist in `docs/MANUAL_CHECKS.md` | A (layout), B (default toml) | `python scripts/build_installer.py` produces `dist\LUNA-Setup-<ver>-x64.exe` and prints size + SHA-256; installs without UAC to `%LOCALAPPDATA%\Programs\L.U.N.A`; Start menu and uninstall entries exist; autostart task creates `L.U.N.A.lnk` → `luna.exe --tray --minimized`; reinstall while running asks to close; uninstall keep/delete data both behave as described; Sandbox checklist run by the owner |
| E | CI release workflow **proposal** (`release.yml` in the PR description and `docs/`, applied only by the owner) | A, D | YAML as in D6; a manual `workflow_dispatch` run on the owner's approval uploads the artifact; on a tag a **draft** release with the setup is created |
| F | Docs: `docs/SETUP.md` "Install from the setup" (SmartScreen, keys, data folders, uninstall), README download section, licence notes | A-D | docs describe the exact commands and paths; links work |

## Consequences

- One spec file + one `.iss` + one build script; reproducible from the lockfile; ~3 min to freeze,
  ~2-4 min to compress on the reference PC.
- The owner must install **Inno Setup** locally once to build the setup (global tool, owner's
  action: `winget install JRSoftware.InnoSetup` (6.x) or `JRSoftware.InnoSetup.7`); the freeze step
  and `--skip-setup` work without it. CI already has it.
- New runtime dependency `keyring` (MIT) and build-only dependencies `pyinstaller`, `pip-licenses`.
- Public builds speak with Edge (online) and SAPI; the local Piper voice exists only in personal
  builds until a clean-licence voice exists.
- Installed and developer runs share `%APPDATA%\LUNA` / `%LOCALAPPDATA%\LUNA` (config, logs, models,
  Startup shortcut) — intended; the last one to toggle "Start with Windows" owns the shortcut.
- Unsigned: SmartScreen warning on first run.

## Decisions for the owner

| # | Question | Recommendation |
|---|---|---|
| D1 | Freezer | PyInstaller 6.22 onedir (prototype works) |
| D2 | Installer and location | Inno Setup, per-user `%LOCALAPPDATA%\Programs\L.U.N.A`, no admin |
| D2b | Uninstall and user data | ask, default keep |
| D2c | Autostart checkbox default | unchecked |
| D3 | Key storage | `keyring` → Credential Manager; env / `.env` still win |
| D4 | Defaults for a fresh install | TTS on with Edge, push-to-talk on, wake off |
| D4b | Two exes `luna.exe` + `luna-cli.exe` | yes |
| D7 | Public build without sherpa-onnx (Edge + SAPI); personal build with Piper Irina | yes |
| D9 | No code signing now | yes, documented warning |
| — | GitHub release flow | draft release on `v*` tags, owner publishes |

## Sources (accessed 2026-10-27)

- PyInstaller 6.22.3, licence, Python support: https://pypi.org/pypi/pyinstaller/json ·
  changelog https://pyinstaller.org/en/stable/CHANGES.html
- Nuitka 4.3.1 (2026-10-17), AGPL + runtime exception: https://pypi.org/project/Nuitka/
- cx_Freeze 8.7.3: https://pypi.org/project/cx-Freeze/ · Briefcase 0.4.7: https://pypi.org/project/briefcase/
- Inno Setup versions: https://jrsoftware.org/isdl.php · licence: https://jrsoftware.org/files/is/license.txt ·
  commercial request: https://jrsoftware.org/isorder.php · 7.x changes: https://jrsoftware.org/files/is7-whatsnew.htm ·
  features: https://jrsoftware.org/isinfo.php
- GitHub runner image (Inno Setup 6.7.1, WiX 3.14, no NSIS):
  https://raw.githubusercontent.com/actions/runner-images/main/images/windows/Windows2025-Readme.md
- keyring 25.7.0 (MIT, Windows Credential Locker backend): https://pypi.org/pypi/keyring/json
- Qt LGPL obligations: https://www.qt.io/licensing/open-source-lgpl-obligations
- Windows Sandbox `.wsb` format: https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-configure-using-wsb-file
- SignPath Foundation terms: https://signpath.org/terms · https://about.signpath.io/product/open-source
- Azure Artifact Signing availability/pricing (2026-01): https://devclass.com/2026/01/14/code-signing-windows-apps-may-be-easier-and-more-secure-with-new-azure-artifact-service/
- Local evidence: prototype build logs and smoke output in the architect's scratch directory
  (not committed); `pip-licenses` 5.5.5 over the prototype venv.
