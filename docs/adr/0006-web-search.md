# ADR 0006 — Web search and content extraction

- Status: **accepted** (owner, 2026-10-23)
- Date: 2026-10-22

## Context

F6: factual and time-sensitive questions ("какой сейчас курс доллара?") must be
answered from a fresh web search, ≤ 10 s end to end (N4), for free. Web content is
untrusted and must never trigger tool calls (spec §3.3).

## Options considered

| Option | Cost | Quality | Risk |
|---|---|---|---|
| **`ddgs`** 9.16.0 (MIT, formerly `duckduckgo_search`) — metasearch over bing, brave, duckduckgo, google, mojeek, startpage, yandex, yahoo, wikipedia | free, no key | good; Yandex backend helps RU queries | scrapes public endpoints; "for educational purposes only" disclaimer; can be rate-limited or break when sites change |
| **SearXNG** self-hosted (Docker, AGPL-3.0, runs as a separate service) | free | good, configurable engines | owner must run a container; AGPL is fine as a separate service we don't modify/distribute |
| Tavily API | free plan ~1,000 credits/month (renewal terms **unverified**) | built for LLMs, returns extracted content | key needed; quota |
| Brave Search API | no longer a real free tier for new users (~$5 monthly credit, reports vary) | good | billing account needed — rejected for default |
| Gemini Google Search grounding | 5,000 free requests/month shared across Gemini 3+ models (pricing page; per-model rows inconsistent) | very good | ties search to one LLM vendor; unavailable when falling back to Groq/Ollama |

Content extraction: **`trafilatura` 2.3.1 (Apache-2.0)** for main-text extraction from
HTML; `httpx` for fetching with timeouts. Alternative: rely on snippets only (fast but
often insufficient for numbers like exchange rates).

## Decision

1. `SearchProvider` protocol (ARCHITECTURE §3). Default **`DdgsSearch`** (backend
   `auto`, region by language: `ru-ru` / `us-en`), fallback **`SearxngSearch`** if the
   owner runs it (URL in config) and optional **`TavilySearch`** if a key is present.
2. `web_search` tool flow: search (top 5) → fetch top 3 pages in parallel (thread pool,
   4 s timeout each, 2 MB cap, `text/html` only, no JS) → `trafilatura` extract →
   truncate each to ~2,000 chars → return results with title, URL, snippet, content.
3. **Prompt-injection handling** (structural, not prompt-only):
   - tool result is marked `untrusted=True`; the **next LLM call is made with no tools**,
     so nothing in web text can cause an action;
   - web text is wrapped in clearly delimited blocks
     (`<search_result index=1 url=...> … </search_result>`) and the system prompt says
     it is data to cite, not instructions;
   - strip HTML, scripts, invisible characters; cap total size;
   - the final answer cites the source domain ("по данным cbr.ru …") so the user sees
     where a number came from;
   - URLs from results are never opened automatically; opening a result requires a new
     user request.
4. Answers to "fresh facts" questions include the date/time context
   (`get_datetime` value injected into the system prompt).
5. Specialised skills (e.g. exchange rates from an official API) are out of MVP but fit
   as additional tools later.

## Consequences

- `ddgs` may break without notice → fallback chain + clear "search unavailable" reply;
  pin the version and bump deliberately.
- Fetching pages adds 1–4 s; total budget still within 10 s (measured in slice 1).
- No tools after web content means a request like "find X and open the first result"
  needs two turns — an acceptable safety trade-off.

## Sources (accessed 2026-10-22)

- ddgs on PyPI: https://pypi.org/pypi/ddgs/json
- trafilatura on PyPI: https://pypi.org/pypi/trafilatura/json
- SearXNG: https://docs.searxng.org/ (license AGPL-3.0; not re-verified this session)
- Tavily / Brave free tiers (secondary, unverified numbers): https://parallel.ai/articles/best-free-web-search-api , https://www.implicator.ai/brave-drops-free-search-api-tier-puts-all-developers-on-metered-billing/
- Gemini grounding pricing: https://ai.google.dev/gemini-api/docs/pricing

## Amendment 1 - fetch hardening and residual risks (2026-10-23, PR #38)

Implemented in `src/luna/search/fetch.py`; decisions taken after review of the first
implementation:

- **Page fetching**: http(s) only; every URL (first request and each of at most 3 redirect
  hops) is validated, the host must be public both as a literal and after DNS resolution
  (loopback, private, link-local, multicast, reserved, site-local, IPv4-compatible and
  IPv6-embedded IPv4 forms such as mapped / 6to4 / Teredo / NAT64 are refused). The 2 MB cap
  applies to the *decoded* body (gzip/deflate are inflated incrementally with a bounded
  output and a compression-ratio cap); `timeout_s` (4 s) is one deadline for the whole
  call, not per request; DNS runs with a timeout; extraction is skipped when time is up and
  uses a `trafilatura` tree-size limit.
- **No proxies**: `httpx` runs with `trust_env=False`. A proxy would resolve names on its own
  and make the address checks meaningless. Consequence: users behind a corporate proxy or
  a TLS-inspecting CA get snippet-only answers (pages are skipped). `ddgs` itself may still
  honour proxy settings.
- **Residual risk - DNS rebinding (TOCTOU)**: we resolve the name to check it, then `httpx`
  resolves it again when connecting. A hostile domain that appears in search results can
  answer with a public address for the check and a local one (`127.0.0.1`, LAN, cloud
  metadata) for the connect. Impact: one GET to a local service, with the text entering the
  LLM context; no data can be sent back to the attacker. **Accepted for now, not mitigated.**
  Follow-up (separate Issue): pin the connection to the validated address (custom `httpcore`
  network backend or connect-by-IP with `Host`/SNI preserved).
