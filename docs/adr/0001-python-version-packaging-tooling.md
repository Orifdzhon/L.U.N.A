# ADR 0001 — Python version, packaging and tooling

- Status: **proposed**
- Date: 2026-10-22
- Deciders: owner (approval), architect (proposal)

## Context

The owner has Python 3.13.1 installed on Windows 10 and no `uv` yet. The spec
(open question 6) worries that speech/ML libraries may require 3.12. The project is
developed by one person, must run on Windows now and Linux/macOS later, and needs a
reproducible environment for CI and for developer agents working in worktrees.

## Options considered

### Python version

| Option | Pros | Cons |
|---|---|---|
| **3.13** | already installed; all key dependencies publish 3.13-compatible Windows wheels (table below) | slightly newer, occasional lagging package |
| 3.12 | most battle-tested for ML stacks | needs a second interpreter (trivial with `uv python install`); no concrete blocker found |
| 3.14 | newest | not needed; fewer wheels expected for smaller packages |

Wheel check on PyPI (accessed 2026-10-22), Windows x86-64:

| Package | Version | 3.13 on Windows |
|---|---|---|
| `ctranslate2` (faster-whisper backend) | 4.8.3 (2026-10-13) | `cp313-cp313-win_amd64` wheel ✔ |
| `faster-whisper` | 1.2.1 (2025-10-31) | pure Python, `>=3.9` ✔ |
| `onnxruntime` | 1.31.0 (2026-10-09) | `cp313-cp313-win_amd64` ✔ |
| `av` (PyAV, faster-whisper dep) | 19.0.2 (2026-10-21) | `cp312-abi3-win_amd64` (stable ABI, works on 3.13) ✔ |
| `piper-tts` | 1.8.0 (2026-09-04) | `cp39-abi3-win_amd64` ✔ |
| `PySide6` | 6.12.0 (2026-10-08) | `cp310-abi3-win_amd64`, `requires_python <3.16` ✔ |
| `vosk` | 0.3.45 (2022-12-14) | `py3-none-win_amd64` (ctypes/cffi, version-independent) ✔ — old but stable |
| `openwakeword` | 0.6.0 (2024-02-11) | pure Python + onnxruntime on Windows ✔ (only needed if we use its runtime) |
| `livekit-wakeword` | 0.3.0 (2026-09-30) | `>=3.11`, numpy + onnxruntime ✔ |
| `ddgs` | 9.16.0 (2026-08-26) | `>=3.10` ✔ |
| `trafilatura` | 2.3.1 | `>=3.10` ✔ |
| `openai` | 3.37.0 | `>=3.10` ✔ |
| `rapidfuzz` | 3.14.6 | `>=3.11` ✔ |
| `sounddevice` | 0.5.6 | `>=3.7` ✔ |

Not verified: exact wheel availability of every transitive dependency (`tokenizers`,
`scipy`, `scikit-learn`, `lxml`); they are mainstream and publish 3.13 wheels, but
the first `uv lock` in slice 0 is the real check.

Wake-word **training** pipelines (openWakeWord notebook, livekit-wakeword `train`
extra) pin old TensorFlow/PyTorch and are Linux-only. They are **not** part of the
app environment; training runs in Colab/WSL/Linux separately (ADR 0004).

### Package and project manager

| Option | Pros | Cons |
|---|---|---|
| **uv** (0.13.5, MIT OR Apache-2.0) | one tool for Python install, venv, lockfile, dependency groups; very fast; `uv python install 3.12` if we must downgrade | young-ish, but widely adopted |
| Poetry | mature | slower, separate Python management |
| pip + venv + pip-tools | no new tool | manual, no unified lock for groups |

### Lint/format and tests

`ruff` (lint + format, replaces flake8/black/isort) and `pytest` — the CLAUDE.md
default; no serious competitor worth the extra tooling. `respx` for HTTP mocking,
`pytest-timeout` to catch hangs in threaded code.

Type checking: optional `pyright` (or `mypy`) in basic mode — proposed but not
required for the definition of done until the codebase settles.

## Decision

- **Python 3.13** (`requires-python = ">=3.13,<3.14"`, `.python-version` = `3.13`).
  If a blocker appears, switch to 3.12 with `uv python install 3.12` — a one-line
  change, no code impact.
- **uv** with `pyproject.toml`, `uv.lock` committed, **src layout**, package `luna`,
  build backend `hatchling` (or `uv_build`).
- Dependency groups: core (default), `voice` (faster-whisper, vosk, sounddevice,
  piper-tts, onnxruntime), `ui` (PySide6), `dev` (ruff, pytest, respx, pytest-timeout).
- `ruff` rules: `E,F,I,UP,B,SIM,RUF` + line length 100; `ruff format`.
- `pytest` markers: `live`, `gpu`, `audio` — excluded by default.
- CI: GitHub Actions `windows-latest`, `astral-sh/setup-uv`, `uv sync --frozen`.

## Consequences

- The owner must install `uv` once (global tool — owner does it himself, e.g.
  `winget install astral-sh.uv` or the official installer).
- CUDA libraries for faster-whisper are a separate concern (ADR 0003).

## Sources (accessed 2026-10-22)

- https://pypi.org/project/ctranslate2/#files
- https://pypi.org/pypi/faster-whisper/json
- https://pypi.org/project/onnxruntime/#files
- https://pypi.org/project/av/#files
- https://pypi.org/pypi/piper-tts/json
- https://pypi.org/pypi/PySide6/json
- https://pypi.org/project/vosk/#files
- https://pypi.org/pypi/openwakeword/json
- https://pypi.org/pypi/livekit-wakeword/json
- https://pypi.org/pypi/ddgs/json · https://pypi.org/pypi/trafilatura/json
- https://pypi.org/pypi/openai/json · https://pypi.org/pypi/rapidfuzz/json · https://pypi.org/pypi/sounddevice/json
- https://pypi.org/pypi/uv/json
- Local check: `python --version` → Python 3.13.1 (owner's machine, 2026-10-22)
