# L.U.N.A.

Open-source voice assistant for Windows. Call it by voice ("Луна") to open apps and websites, answer questions and search the web.

**Status:** early development - project skeleton only.

- Product requirements: [docs/PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md)
- Development rules for humans and AI agents: [CLAUDE.md](CLAUDE.md)

## Development

Requires [uv](https://docs.astral.sh/uv/) (`winget install astral-sh.uv`) and Python 3.13.

```
uv sync --all-extras --dev      # create .venv and install dependencies
uv run luna --version           # run (also: uv run python -m luna)
uv run ruff check .             # lint
uv run ruff format --check .    # format check (drop --check to apply)
uv run pytest                   # tests (live/gpu/audio markers are skipped by default)
```

Copy `.env.example` to `.env` for API keys (gitignored). Config lives in `luna.toml`
(`--config PATH`, then `%APPDATA%\LUNA\luna.toml`, then `./luna.toml`).
