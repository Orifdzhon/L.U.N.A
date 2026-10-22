# ADR 0002 — LLM providers and tool calling

- Status: **proposed**
- Date: 2026-10-22

## Context

L.U.N.A. needs an LLM that (a) understands RU/EN mixed requests, (b) reliably emits
tool calls (`open_app`, `open_website`, `web_search`, `get_datetime`), (c) answers
general questions, and (d) is free (N1). Privacy is not a priority; free cloud tiers
are preferred for quality and speed, a local model is the offline / rate-limit fallback
(N5). Simple commands bypass the LLM via the router (ARCHITECTURE §4.1), so the LLM
handles only questions, searches and commands the router could not resolve — a low
request volume (tens per day), well under free limits.

## Options considered

### Providers

| Provider | Free tier (verified 2026-10-22) | Tool calling | Notes |
|---|---|---|---|
| **Google Gemini API** | Free tier exists for Flash / Flash-Lite models (e.g. `gemini-3.8-flash`, `gemini-3.6-flash`, `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`, `gemini-2.5-flash(-lite)`); free-tier data is "used to improve our products". **Exact RPM/RPD per model are no longer published on the docs page — they are shown per project in AI Studio** (unverified numbers). Pro models have no free tier. | yes, native and via OpenAI-compatible endpoint (beta) | best multilingual quality among free options; Gemini 3 models cannot fully disable reasoning via the compat layer (latency) |
| **Groq** | Free plan exists; docs page shows Developer-plan base limits (e.g. `openai/gpt-oss-120b`: 30 RPM, 1K RPD, 8K TPM, 200K TPD); free-plan limits are shown in the console (**unverified**, secondary sources report ~30 RPM / 1K RPD for 70B-class models) | yes for `openai/gpt-oss-20b/120b`, `llama-3.3-70b-versatile`, `llama-3.1-8b-instant`, `qwen/qwen3.8-27b` (preview) | very low latency; OpenAI-compatible API |
| **Ollama (local)** | free, offline | yes (`tools` supported on `/v1/chat/completions`; `tool_choice` **not** supported) | `qwen3:4b` = 2.5 GB, tools + thinking, 256K ctx; `qwen3:8b` = 5.2 GB (too big next to STT on 6 GB) |
| OpenRouter free models, Mistral free tier, Cerebras | possible later | varies | not needed for MVP; the client design allows adding them by config |

### Client library

| Option | Pros | Cons |
|---|---|---|
| **One `openai` SDK client (Apache-2.0) for all three** via `base_url` | one code path, one message/tool format, trivially add providers, easy HTTP mocking (httpx) | Gemini compat layer is "beta"; provider quirks (Ollama ignores `tool_choice`) handled by small per-provider flags |
| Native SDKs (`google-genai`, `groq`, `ollama`) | full feature access (Gemini search grounding, thinking config) | 3 implementations and 3 message formats to maintain and test |
| LiteLLM / LangChain | many providers | heavy dependency, more abstraction than needed, frequent breaking changes |

We do not need Gemini's built-in Google Search grounding (we run our own search
for provider independence; also its free quota is limited), so the compat layer is
sufficient.

### Models (defaults, all configurable)

| Role | Model | Why |
|---|---|---|
| Primary | Gemini `gemini-3.5-flash-lite` | stable, described by Google as the fastest/most cost-effective 3.5 model; free tier; good RU |
| Secondary | Groq `openai/gpt-oss-120b` | production model, tool use, fast; alternative `llama-3.3-70b-versatile` (supports parallel tool calls) |
| Local fallback | Ollama `qwen3:4b` | fits VRAM budget next to STT (ARCHITECTURE §11); tool calling; RU support. Candidate to compare: `qwen3.5:4b` (3.3–4.0 GB) |

## Decision

1. A single `OpenAICompatProvider(base_url, model, api_key, quirks)` implements
   `LLMProvider` using the `openai` SDK. Quirks: `supports_tool_choice`,
   `reasoning_effort` (e.g. `"low"` for Gemini 3 / gpt-oss), `max_tokens`.
2. `LLMChain` with default order **Gemini → Groq → Ollama**, cooldown on 429/5xx/
   timeouts (ARCHITECTURE §8). A provider with no API key is skipped.
3. Tool calling uses the OpenAI `tools` format; tool schemas come from `ToolRegistry`.
   We never depend on `tool_choice` (Ollama ignores it); "must search first" is
   enforced by the system prompt and by the assistant loop (if `force_search` and the
   model answers without calling `web_search`, the assistant runs the search itself
   and asks again with results and no tools).
4. Slice 1 includes a **benchmark script** (≈20 RU/EN prompts: commands, questions,
   searches) measuring latency and tool-call correctness per provider/model. The owner
   picks the final default order from its output.
5. If the Gemini compat layer proves unreliable for tools, add `GeminiNativeProvider`
   (`google-genai`) behind the same protocol — no core changes.

## Consequences

- Free limits are per project/organization and change often; L.U.N.A. must degrade
  gracefully (fallback + "offline mode" indicator) rather than assume numbers.
- Gemini free tier data may be used by Google for training — acceptable per owner.
- Two API keys in `.env` (`GEMINI_API_KEY`, `GROQ_API_KEY`); Ollama needs the owner
  to install Ollama and `ollama pull qwen3:4b` (manual step).
- Small local models hallucinate: factual/time-sensitive questions go through search
  (spec F6) regardless of provider.

## Owner decisions needed

- Confirm default order and models after the slice-1 benchmark.
- Confirm that free-tier data use by Google/Groq is acceptable (assumed yes).

## Sources (accessed 2026-10-22)

- Gemini rate limits (no free table; "see AI Studio"), last updated 2026-10-15: https://ai.google.dev/gemini-api/docs/rate-limits
- Gemini pricing / free tier per model: https://ai.google.dev/gemini-api/docs/pricing
- Gemini models and status: https://ai.google.dev/gemini-api/docs/models
- Gemini OpenAI compatibility (base URL, function calling, beta, reasoning limits): https://ai.google.dev/gemini-api/docs/openai
- Groq rate limits: https://console.groq.com/docs/rate-limits
- Groq models: https://console.groq.com/docs/models
- Groq tool use: https://console.groq.com/docs/tool-use
- Ollama OpenAI compatibility: https://docs.ollama.com/api/openai-compatibility
- Ollama qwen3 tags/sizes: https://ollama.com/library/qwen3 · qwen3.5: https://ollama.com/library/qwen3.5
- Secondary (unverified numbers): https://eesel.ai/blog/groq-pricing , https://localaimaster.com/blog/groq-api-free-guide
- `openai` SDK license/version: https://pypi.org/pypi/openai/json
