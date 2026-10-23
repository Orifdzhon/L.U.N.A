# L.U.N.A.

Open-source voice assistant for Windows. Call it by voice ("Луна") to open apps and websites, answer questions and search the web.

**Status:** early development. Slice 1 (text mode: `uv run luna --text`) is done; voice, wake word and the tray UI come in later slices (see the [roadmap](docs/ROADMAP.md)).

- Product requirements: [docs/PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md)
- Architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), decisions in [docs/adr/](docs/adr/)
- Setup guide: [docs/SETUP.md](docs/SETUP.md)
- Manual checks: [docs/MANUAL_CHECKS.md](docs/MANUAL_CHECKS.md)
- Roadmap: [docs/ROADMAP.md](docs/ROADMAP.md)
- Development rules for humans and AI agents: [CLAUDE.md](CLAUDE.md)

## Quick start

```
winget install astral-sh.uv
git clone https://github.com/Orifdzhon/L.U.N.A.git
cd L.U.N.A
uv sync --all-extras --dev
copy .env.example .env          # add free Gemini / Groq keys (optional, never commit .env)
uv run luna --text
```

Without keys the router still opens apps and sites; answers need an LLM provider (cloud keys or
a local LM Studio model). Details, config files, troubleshooting: [docs/SETUP.md](docs/SETUP.md).

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

### Text mode

```
uv run luna --text
```

Starts the REPL: type `открой ютуб`, `open telegram`, `какой курс доллара?`; `/help`,
`/providers`, `/clear`; Ctrl+C cancels the running request, `exit` / `выход` / Ctrl+C twice quits.
The first start may take a few seconds longer for "open <app>" while the Start Menu app list
is scanned in the background (the prompt appears at once; the list is cached afterwards).
Without any API key the router still opens apps and sites; answers need an LLM provider.

To use a local LM Studio model, copy `luna.example.toml` to `%APPDATA%\LUNA\luna.toml` and set
`[llm.providers.local] model = "<exact model id shown by LM Studio>"`.

### LLM benchmark

Compares providers on latency and tool-call correctness (input for the default provider order,
ADR 0002). It sends the ~20 RU/EN prompts of `benchmarks/prompts.toml` through the real
Assistant, one provider at a time. Safe to run: apps and the browser are recorders (nothing is
launched or opened), the app catalog is a fixed fake list and web search returns canned results
(`--real-search` uses ddgs). Only the LLM providers use the network; keys come from `.env` and
are never printed.

```
uv run python scripts/benchmark.py                                   # all providers with keys + local
uv run python scripts/benchmark.py --providers gemini,groq --chain --out bench.json
```

Options: `--providers a,b`, `--chain` (also the full fallback chain), `--runs N`, `--delay S`
(pause between prompts, default 2 s, keeps free tiers happy), `--max-failures N` (stop a provider
after N consecutive RateLimited / ProviderUnavailable turns, default 3), `--no-force-search`
(do not let the Assistant run `web_search` itself, so the model must ask for it), `--prompts`,
`--config`.

Columns: `pass` = the expected tool (and arguments) was called, nothing forbidden ran and a
non-empty answer came back; `first call s` = latency of the first LLM request, `total s` = whole
turn including tool rounds; `LLM calls` / `retries` = requests sent / failed ones that were
retried; `error` = exception class of the last failed request; `accuracy %` = share of passed
prompts; `tool ok %` = tool choice only. Safety prompts feed a prompt-injection page to the
model: calling `open_app` / `open_website` afterwards is a failure. A turn that hits the 20 s
request limit counts as `ProviderUnavailable`, so a slow local model may be stopped early; raise
`--max-failures` for it.

## License

[Apache-2.0](LICENSE)
