"""Build the Windows distribution of L.U.N.A. (ADR 0010).

Freeze stage (this Issue): version check -> version resource -> PyInstaller -> self-check ->
THIRD_PARTY_NOTICES.txt -> bundle size. The Inno Setup stage is a later Issue; until then pass
``--skip-setup``.

Run it in the locked build environment (always with the same flags, ``uv run`` re-syncs):

    uv run --frozen --no-dev --group build python scripts/build_installer.py --skip-setup
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
BUNDLE = DIST / "luna"

PRODUCT_NAME = "L.U.N.A."
COMPANY_NAME = "L.U.N.A. project"
COPYRIGHT = "Copyright (c) L.U.N.A. contributors. Apache-2.0."
EDITIONS = ("public", "personal")
#: Packages that exist in the build environment but are not shipped in the public edition.
PERSONAL_ONLY_PACKAGES = ("sherpa-onnx", "sherpa-onnx-core")
BUILD_TOOL_PACKAGES = ("pyinstaller", "pyinstaller-hooks-contrib", "pip-licenses", "luna")


class BuildError(RuntimeError):
    """A build step failed; the message is shown to the developer."""


def read_package_version(init_file: Path) -> str:
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', init_file.read_text("utf-8"), re.MULTILINE)
    if match is None:
        raise BuildError(f"no __version__ in {init_file}")
    return match.group(1)


def read_pyproject_version(pyproject: Path) -> str:
    with pyproject.open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def check_version(package_version: str, pyproject_version: str, tag: str | None = None) -> str:
    """Return the release version or raise if the sources (or the git tag ``vX.Y.Z``) disagree."""
    if package_version != pyproject_version:
        raise BuildError(
            f"version mismatch: luna.__version__={package_version} "
            f"but pyproject.toml={pyproject_version}"
        )
    if tag:
        tag_version = tag[1:] if tag.startswith("v") else tag
        if tag_version != package_version:
            raise BuildError(f"tag {tag} does not match version {package_version}")
    return package_version


def version_tuple(version: str) -> tuple[int, int, int, int]:
    """``"1.2.3"`` / ``"1.2.3rc1"`` -> ``(1, 2, 3, 0)`` (Windows wants four integers)."""
    numbers = [int(m.group(0)) for part in version.split(".")[:4] if (m := re.match(r"\d+", part))]
    numbers += [0] * (4 - len(numbers))
    return (numbers[0], numbers[1], numbers[2], numbers[3])


def version_resource_text(version: str) -> str:
    """Text of the PyInstaller ``VSVersionInfo`` file (shown in Properties -> Details)."""
    quad = version_tuple(version)
    dotted = ".".join(str(n) for n in quad)
    strings = {
        "CompanyName": COMPANY_NAME,
        "FileDescription": f"{PRODUCT_NAME} voice assistant",
        "FileVersion": dotted,
        "InternalName": "luna",
        "LegalCopyright": COPYRIGHT,
        "OriginalFilename": "luna.exe",
        "ProductName": PRODUCT_NAME,
        "ProductVersion": version,
    }
    entries = ",\n        ".join(f"StringStruct({k!r}, {v!r})" for k, v in strings.items())
    return (
        "VSVersionInfo(\n"
        "  ffi=FixedFileInfo(\n"
        f"    filevers={quad},\n"
        f"    prodvers={quad},\n"
        "    mask=0x3f,\n"
        "    flags=0x0,\n"
        "    OS=0x40004,\n"
        "    fileType=0x1,\n"
        "    subtype=0x0,\n"
        "    date=(0, 0)\n"
        "  ),\n"
        "  kids=[\n"
        "    StringFileInfo([\n"
        "      StringTable('040904B0', [\n"
        f"        {entries}\n"
        "      ])\n"
        "    ]),\n"
        "    VarFileInfo([VarStruct('Translation', [1033, 1200])])\n"
        "  ]\n"
        ")\n"
    )


def find_iscc(
    env: Mapping[str, str] | None = None,
    exists: Callable[[Path], bool] = Path.is_file,
) -> Path | None:
    """Locate Inno Setup's ``ISCC.exe`` (``ISCC`` env override, then the standard folders)."""
    env = os.environ if env is None else env
    candidates: list[Path] = []
    if env.get("ISCC"):
        candidates.append(Path(env["ISCC"]))
    for var in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = env.get(var)
        if not base:
            continue
        root = Path(base)
        if var == "LOCALAPPDATA":
            root = root / "Programs"
        candidates.append(root / "Inno Setup 6" / "ISCC.exe")
    for candidate in candidates:
        if exists(candidate):
            return candidate
    return None


def _run(command: Sequence[str | Path], env: Mapping[str, str] | None = None) -> None:
    printable = " ".join(str(part) for part in command)
    print(f"+ {printable}", flush=True)
    result = subprocess.run([str(part) for part in command], cwd=ROOT, env=env, check=False)
    if result.returncode != 0:
        raise BuildError(f"command failed with exit code {result.returncode}: {printable}")


def directory_size(path: Path) -> tuple[int, int]:
    """Return ``(bytes, file count)`` of a directory tree."""
    total = count = 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
            count += 1
    return total, count


def notices_ignore_packages(edition: str) -> list[str]:
    ignored = list(BUILD_TOOL_PACKAGES)
    if edition == "public":
        ignored += PERSONAL_ONLY_PACKAGES
    return ignored


def generate_notices(edition: str, target: Path) -> None:
    """Write THIRD_PARTY_NOTICES.txt: the hand-written header + pip-licenses of the build env."""
    command = [
        sys.executable,
        "-m",
        "piplicenses",
        "--from=mixed",
        "--with-license-file",
        "--with-notice-file",
        "--format=plain-vertical",
        "--ignore-packages",
        *notices_ignore_packages(edition),
    ]
    print(f"+ {' '.join(command)}", flush=True)
    result = subprocess.run(command, cwd=ROOT, capture_output=True, check=False)
    if result.returncode != 0:
        raise BuildError(f"pip-licenses failed: {result.stderr.decode('utf-8', 'replace')}")
    header = (PACKAGING / "NOTICES_HEADER.txt").read_text("utf-8")
    body = result.stdout.decode("utf-8", "replace")
    target.write_text(header + body, encoding="utf-8", newline="\n")


def freeze(edition: str, version: str) -> None:
    BUILD.mkdir(exist_ok=True)
    version_file = BUILD / "version_info.txt"
    version_file.write_text(version_resource_text(version), encoding="utf-8")
    env = dict(os.environ, LUNA_EDITION=edition, LUNA_VERSION_FILE=str(version_file))
    _run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            DIST,
            "--workpath",
            BUILD / "pyinstaller",
            PACKAGING / "luna.spec",
        ],
        env=env,
    )
    for name in ("luna.exe", "luna-cli.exe"):
        if not (BUNDLE / name).is_file():
            raise BuildError(f"{BUNDLE / name} was not produced")
    if edition == "public" and any(BUNDLE.rglob("sherpa_onnx")):
        raise BuildError("public edition must not contain sherpa_onnx")


def self_check() -> None:
    cli = BUNDLE / "luna-cli.exe"
    _run([cli, "--version"])
    _run([cli, "--self-check"])


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the L.U.N.A. Windows distribution")
    parser.add_argument("--edition", choices=EDITIONS, default="public")
    parser.add_argument(
        "--skip-setup",
        action="store_true",
        help="freeze only, do not build the Inno Setup installer",
    )
    parser.add_argument("--tag", default=os.environ.get("GITHUB_REF_NAME"), help="vX.Y.Z to match")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    tag = args.tag if args.tag and re.fullmatch(r"v\d.*", args.tag) else None
    started = time.monotonic()
    try:
        version = check_version(
            read_package_version(ROOT / "src" / "luna" / "__init__.py"),
            read_pyproject_version(ROOT / "pyproject.toml"),
            tag,
        )
        print(f"L.U.N.A. {version}, edition {args.edition}")
        freeze(args.edition, version)
        self_check()
        generate_notices(args.edition, BUNDLE / "THIRD_PARTY_NOTICES.txt")
        size, count = directory_size(BUNDLE)
        print(f"bundle: {BUNDLE} - {size / 1024 / 1024:.0f} MB, {count} files")
        if not args.skip_setup:
            iscc = find_iscc()
            if iscc is None:
                raise BuildError("Inno Setup (ISCC.exe) not found; install it or use --skip-setup")
            raise BuildError("the Inno Setup stage is not implemented yet; use --skip-setup")
    except BuildError as exc:
        print(f"build_installer: {exc}", file=sys.stderr)
        return 1
    print(f"done in {time.monotonic() - started:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
