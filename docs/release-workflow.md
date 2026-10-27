# Release workflow (proposal)

Status: **proposal.** CI configuration is owner-only (see `CLAUDE.md`), so the workflow below is
not in `.github/workflows/`. To apply it, copy the YAML block to `.github/workflows/release.yml`
in a commit of your own. Design: [ADR 0010](adr/0010-windows-installer.md) D6 (build and
release), D8 (testing), D9 (no signing), D7 (licences).

## What it does

- **`workflow_dispatch`** (Actions tab -> Release -> Run workflow): builds the **public** setup and
  uploads it as a workflow artifact (kept 14 days). No release is created.
- **Push of a tag `v*`** (for example `v0.1.0`): the same build, then a **draft** GitHub release
  with the public setup and its SHA-256. You review and publish the draft by hand; nothing becomes
  public automatically.
- The build script refuses a tag that differs from `luna.__version__` / `pyproject.toml`
  (`--tag` defaults to `GITHUB_REF_NAME`), so tag `v0.1.0` requires version `0.1.0` in both.
- **Only the public edition is built and published.** The personal edition bundles Piper with
  sherpa-onnx, whose espeak-ng is GPL-3.0, and the Irina voice has its own licence; neither may be
  redistributed from this repository. The personal setup is built on the owner's PC only.
- The smoke test runs `dist\luna\luna-cli.exe --version` and `--self-check` after the build (the
  build script also runs them; the separate step makes a failure obvious in the log).
- Permissions: the build job only reads the repository (`contents: read`); only the release job
  has `contents: write`, and it runs only for tags. Actions are pinned to commit SHAs (the tag is
  in the comment). The Inno Setup installer is pinned by version and SHA-256.
- Not covered: code signing (ADR 0010 D9), so the setup shows a SmartScreen warning.

## Before you apply it

1. Check the pinned Inno Setup hash against the official release page
   (<https://github.com/jrsoftware/issrc/releases/tag/is-6_7_3>) or download the file yourself
   and run `Get-FileHash`. The value below was computed from the file at that URL on 2026-10-27.
2. Update the action SHAs from time to time (Dependabot can do it for workflows).
3. The first run is best done with `workflow_dispatch` on a branch you control.
4. Tag only after the version in `src/luna/__init__.py` and `pyproject.toml` is bumped.

## `release.yml`

```yaml
name: Release

on:
  push:
    tags: ["v*"]
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: release-${{ github.ref }}
  cancel-in-progress: false

env:
  INNO_VERSION: "6.7.3"
  INNO_SHA256: "9c73c3bae7ed48d44112a0f48e66742c00090bdb5bef71d9d3c056c66e97b732"

jobs:
  installer:
    runs-on: windows-latest
    timeout-minutes: 40
    outputs:
      setup_name: ${{ steps.meta.outputs.setup_name }}
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4
        with:
          persist-credentials: false

      - uses: astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e # v6
        with:
          python-version: "3.13"

      # The runner image usually has Inno Setup already; install the pinned version if ISCC is
      # missing. The download is verified against INNO_SHA256 before it is run.
      - name: Install Inno Setup
        shell: pwsh
        run: |
          $iscc = @(
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
          ) | Where-Object { Test-Path $_ } | Select-Object -First 1
          if ($iscc) {
            Write-Host "Inno Setup found: $iscc"
            exit 0
          }
          $url = "https://github.com/jrsoftware/issrc/releases/download/is-$($env:INNO_VERSION.Replace('.', '_'))/innosetup-$($env:INNO_VERSION).exe"
          $file = Join-Path $env:RUNNER_TEMP "innosetup.exe"
          Invoke-WebRequest -Uri $url -OutFile $file
          $actual = (Get-FileHash -Algorithm SHA256 $file).Hash
          if ($actual -ne $env:INNO_SHA256.ToUpper()) {
            throw "Inno Setup hash mismatch: $actual"
          }
          Start-Process -FilePath $file -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-" -Wait
          if (-not (Test-Path "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe")) {
            throw "ISCC.exe not found after installing Inno Setup"
          }

      - name: Build the public setup
        run: uv run --frozen --no-dev --group build python scripts/build_installer.py --edition public

      - name: Smoke test the frozen build
        run: |
          dist\luna\luna-cli.exe --version
          dist\luna\luna-cli.exe --self-check

      - name: Checksum
        id: meta
        shell: pwsh
        run: |
          $setup = Get-ChildItem dist -Filter "LUNA-Setup-*-x64.exe" |
            Where-Object { $_.Name -notlike "*-personal-*" } | Select-Object -First 1
          if (-not $setup) { throw "public setup not found in dist" }
          $hash = (Get-FileHash -Algorithm SHA256 $setup.FullName).Hash.ToLower()
          Set-Content -Path "dist\$($setup.Name).sha256" -Value "$hash  $($setup.Name)" -Encoding ascii
          "setup_name=$($setup.Name)" >> $env:GITHUB_OUTPUT
          "SHA-256 $hash"

      - uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4
        with:
          name: LUNA-Setup
          path: |
            dist/LUNA-Setup-*-x64.exe
            dist/LUNA-Setup-*-x64.exe.sha256
          if-no-files-found: error
          retention-days: 14

  draft-release:
    needs: installer
    if: startsWith(github.ref, 'refs/tags/v')
    runs-on: ubuntu-latest
    timeout-minutes: 10
    permissions:
      contents: write
    steps:
      - uses: actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093 # v4
        with:
          name: LUNA-Setup
          path: release

      - name: Create the draft release
        shell: bash
        env:
          GH_TOKEN: ${{ github.token }}
          GH_REPO: ${{ github.repository }}
          TAG: ${{ github.ref_name }}
        run: |
          set -euo pipefail
          sha="$(cut -d' ' -f1 release/*.sha256)"
          {
            echo "Unsigned build: Windows SmartScreen shows \"Windows protected your PC\" -"
            echo "click \"More info\" -> \"Run anyway\". Verify the download first."
            echo
            echo "SHA-256 of the setup: \`$sha\`"
            echo
            echo "Public edition (no Irina voice). See docs/SETUP.md, \"Install from the setup\"."
          } > notes.md
          gh release create "$TAG" release/LUNA-Setup-*-x64.exe release/LUNA-Setup-*-x64.exe.sha256 \
            --draft --title "L.U.N.A. $TAG" --notes-file notes.md
```
