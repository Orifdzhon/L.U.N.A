# PyInstaller spec for L.U.N.A. (ADR 0010 D1). Build it with scripts/build_installer.py.
#
# One Analysis, two EXE (windowed luna.exe + console luna-cli.exe), one COLLECT -> dist/luna/.
# Environment read here:
#   LUNA_EDITION       "public" (default; no sherpa-onnx) or "personal" (with Piper/sherpa-onnx)
#   LUNA_VERSION_FILE  path of the generated Windows version resource (optional)
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, copy_metadata

# SPECPATH is injected by PyInstaller (the folder of this file).
PACKAGING = Path(SPECPATH)  # noqa: F821
ROOT = PACKAGING.parent

edition = os.environ.get("LUNA_EDITION", "public").strip().lower()
if edition not in ("public", "personal"):
    raise SystemExit(f"LUNA_EDITION must be 'public' or 'personal', got {edition!r}")
version_file = os.environ.get("LUNA_VERSION_FILE") or None

binaries = []
datas = []
hiddenimports = ["vosk.vosk_cffi"]
excludes = ["tkinter"]

for package in ("vosk", "ctranslate2"):
    binaries += collect_dynamic_libs(package)
for package in ("faster_whisper", "trafilatura", "justext", "htmldate", "courlan", "luna"):
    datas += collect_data_files(package)  # luna: the alias tables are required

if edition == "personal":
    binaries += collect_dynamic_libs("sherpa_onnx")
    datas += copy_metadata("sherpa_onnx")
else:
    excludes += ["sherpa_onnx"]  # no espeak-ng (GPL) code in the public build

a = Analysis(  # noqa: F821
    [str(PACKAGING / "entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

common = dict(
    icon=str(PACKAGING / "luna.ico"),
    version=version_file,
    upx=False,
    exclude_binaries=True,
)
exe_gui = EXE(pyz, a.scripts, [], name="luna", console=False, **common)  # noqa: F821
exe_cli = EXE(pyz, a.scripts, [], name="luna-cli", console=True, **common)  # noqa: F821

coll = COLLECT(  # noqa: F821
    exe_gui,
    exe_cli,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="luna",
)
