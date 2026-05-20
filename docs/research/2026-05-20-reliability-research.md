# Voice Agent Reliability Patterns — 2026 Research

**Context:** Pipecat 1.0.x voice bot, ElevenLabs STT + ElevenLabs TTS + OpenAI gpt-4o-mini, FastAPI EHR on SQLite. Today the pipeline is single-provider for STT, LLM, and TTS — any 5xx or rate-limit kills the call. This report surveys current (2026) production patterns and recommends MVP scope for a 3-day take-home.

---

## 1. LLM provider fallback

**State of the art (2026):** Three mature options, in increasing order of infra cost.

1. **Hand-rolled try-A-then-B around the existing OpenAI SDK.** Cheapest. One module, one decorator, no extra service. The 2026 CallSphere guide ([CallSphere, 2026](https://callsphere.ai/blog/retry-strategies-llm-api-calls-exponential-backoff-jitter-tenacity)) shows the canonical pattern: `@retry` from `tenacity` with `wait_exponential_jitter(initial=1, max=60, jitter=5)`, `stop_after_attempt(5)`, and an explicit `retry_if_exception_type((RateLimitError, ServerOverloadError, TimeoutError))`. Wrap that in an outer try/except that flips to a second client with a different `base_url`.
2. **LiteLLM SDK (not the proxy).** Minimal: keep your code, swap `openai.chat.completions.create(...)` for `litellm.completion(model=..., fallbacks=["openai/gpt-4o-mini", "anthropic/claude-haiku-4-5"])`. The docs ([LiteLLM model_fallbacks](https://docs.litellm.ai/docs/tutorials/model_fallbacks)) still recommend a manual `for model in fallback_list` loop for SDK use — there is **no single decorator**; you replace the call site.
3. **OpenRouter as gateway.** Zero infra, one API key, automatic provider rerouting. ([MPIV, 2026](https://mpiv.ai/blog/litellm-vs-openrouter-which-wins-for-production-ai-agents-2026)) explicitly says OpenRouter wins for small teams that want reliability without operating it. Downside: 5.5% platform fee and all traffic flows through a third party (compliance concern for healthcare).

**Infra cost:** Hand-rolled = 0. LiteLLM SDK = 0 (only the proxy variant needs Postgres + optional Redis). OpenRouter = third-party account.

**Test story:** Mock the primary client to raise `openai.APIError` / `RateLimitError`; assert the secondary client is called and a transcript-equivalent response returns. `pytest-httpx` or `respx` for HTTP-level mocking is the 2026 default.

**MVP (1 h):** A `src/llm_client.py` module that exposes `complete(messages) -> str`. Internally: `tenacity` retry on the OpenAI call (3 attempts, exponential jitter), then a fallback branch that hits OpenRouter or Anthropic with the same messages. Three unit tests (success, retry-then-success, full fallback). This is the highest-ROI item in the report.

---

## 2. STT/TTS provider fallback

**Honest finding:** This is significantly harder than LLM fallback, and most production deployments **don't do mid-call swaps**.

Pipecat 1.0 (released 2026-04-14, [Pipecat v1.0.0](https://newreleases.io/project/github/pipecat-ai/pipecat/release/v1.0.0)) ships a `ServiceSwitcher` for dynamic STT/LLM swaps and a `ParallelPipeline` that can run a backup branch ([Pipecat ParallelPipeline docs](https://docs.pipecat.ai/server/pipeline/parallel-pipeline)). **But:** open issue [#4139](https://github.com/pipecat-ai/pipecat/issues/4139) shows ServiceSwitcher still produces cascading "no close frame received" WebSocket errors on long calls — not production-stable as of May 2026.

Realistic patterns:

- **Pre-call provider selection.** Health-check ElevenLabs STT/TTS at call setup; if it fails health-check, instantiate the pipeline with Deepgram STT + Cartesia TTS instead. No mid-call switch. This is what most teams ([Hamming AI, 2026](https://hamming.ai/resources/best-voice-agent-stack)) actually ship.
- **TTS-only fallback (easier).** ElevenLabs WebSocket TTS dies mid-utterance → catch the exception, push a `TTSSpeakFrame` from a pre-recorded MP3 saying "one moment please," and emit the rest via OpenAI TTS (REST, no WebSocket). TTS swap is cheap because each utterance is independent; STT swap mid-call is hard because session state and partial transcripts are non-portable.
- **Fail the call cleanly.** If primary STT dies and warm secondary isn't viable, the dominant production behavior is: play a pre-recorded "we're having trouble, calling you back" via the existing TTS frame, capture the caller's number from caller-ID metadata (not STT), end the call.

**Infra cost:** Health-check + pre-call selection = 0. Mid-call swap = significant (Pipecat ServiceSwitcher is the only path, and it's flaky).

**MVP (1 h):** Wrap STT and TTS factories in `src/voice_providers.py` with a `health_check()` that pings each provider once at startup. On startup failure, swap to a secondary set. No mid-call swap. Mention `ServiceSwitcher` in SOLUTION.md "Future work" and explicitly cite #4139 as the reason it's deferred — this signals senior judgment.

---

## 3. Retry / circuit breaker

**Library choice (2026):** `tenacity` has won. `backoff` is still maintained but `tenacity` has better async support, retry-state introspection, and async hooks, and is what every 2026 LLM-resilience blog recommends ([CallSphere, 2026](https://callsphere.ai/blog/retry-strategies-llm-api-calls-exponential-backoff-jitter-tenacity); [Tenacity docs](https://tenacity.readthedocs.io/)). Handcrafted httpx retry is justified only when you need very tight control (e.g., per-attempt timeout budgets), which we don't.

**Retry vs. circuit breaker decision rule:**

- **Retry with jitter:** transient errors only — 429, 500/502/503/504, network timeout. Never retry 400/401/403 or context-window overflow.
- **Circuit breaker:** appropriate when failures are *sustained* (5 consecutive failures in <30 s indicates the provider is genuinely down, not blip). The breaker prevents the bot from stacking expensive retry attempts against a dead provider and burning latency budget. Three states: closed / open / half-open. Cooldown ~30 s.

For a 3-day take-home, a circuit breaker is **gold-plate unless** you're explicitly demoing the gateway pattern. The CallSphere article is explicit: "retries for transient glitches, fallbacks for persistent failures, circuit breakers for systemic degradation" — you only need the third when you have a sustained-failure problem to solve. We don't.

**MVP (30 min):** `tenacity` decorator on every external API call (LLM, STT, TTS, EHR), 3 attempts, exponential jitter 1→8 s, retry only on the right exception classes. Add a paragraph in SOLUTION.md explaining when we'd add a breaker (sustained 5xx beyond N failures over T seconds).

---

## 4. Graceful degradation

**The core insight from 2026 voice-UX research** ([Fuse Lab, 2026](https://fuselabcreative.com/voice-user-interface-design-guide-2026/)): never trap the caller in an error loop. After two failures the third turn **must** be an escape — handoff or callback. Generic "I didn't catch that" is the anti-pattern; contextual reprompts asking only for the missing slot are the pattern.

Concrete fallback playbook for our bot, in priority order:

1. **Pre-recorded TTSSpeakFrame** for the bad-path. Generate a clean WAV at build time ("I'm having trouble. Let me have a nurse call you back at the number you're calling from. Goodbye.") and push it via `TTSSpeakFrame` directly — no LLM, no live TTS. This works even when both ElevenLabs and OpenAI TTS are down because the audio is already on disk.
2. **Capture callback intent without LLM.** When the LLM has failed twice, fall back to a regex-driven mini-flow: "Please say your phone number" → STT result → regex `r"\b\d{3}[\s-]?\d{3}[\s-]?\d{4}\b"`. If STT itself is dead, use caller-ID from the transport metadata. Persist the callback request to SQLite for a human to action.
3. **Human handoff.** If the deployment had a real call-center, transfer via SIP REFER. For the take-home, log "would transfer here" and play the pre-recorded message.

The Stackwell article ([Stackwell, 2026](https://iamstackwell.com/posts/ai-agent-fallback-strategy/)) classifies every failure as transient / capability / policy / logic and pre-assigns each to retry / degrade / handoff / safe-default / stop. Listing this matrix in SOLUTION.md is cheap and signals you've actually thought about it.

**MVP (1 h):** Record a single fallback WAV (or generate it once at build time and check it in). Add a `FallbackHandler` processor at the end of the pipeline that listens for an `LLMFailedFrame` / `STTFailedFrame` and pushes the WAV + writes a `callback_requests` row to SQLite with `(timestamp, caller_id, last_user_utterance)`.

---

## 5. EHR reachability (future-work section)

Our EHR is in-process via uvicorn — never the bottleneck for this submission. For SOLUTION.md "Future work," the canonical 2026 pattern for real-HTTP EHRs is straightforward:

- **Idempotency-Key header on every write.** Per the [HTTP Toolkit summary of the IETF draft](https://httptoolkit.com/blog/idempotency-keys/), the client sends a UUID in `Idempotency-Key:` on POST/PATCH; the server caches the response for hours-to-days and returns it on retry. Stripe and Adyen ([Adyen idempotency docs](https://docs.adyen.com/development-resources/api-idempotency)) are the reference implementations.
- **Write-timeout verify-not-retry.** When a POST times out, the client does *not* blindly retry — it issues a GET against a deterministic resource path (or query by the idempotency key) to find out whether the write actually landed. Only retry if GET confirms no write. This matters for healthcare: a duplicate appointment or duplicate prescription is worse than a failed one.
- **HL7 v2 / FHIR specifics.** Epic retries on NAK or timeout ([CapMinds 2026 guide](https://www.capminds.com/blog/ehr-api-integration-guide-authentication-standards-real-implementation-patterns/)); the receiver MUST dedupe on `MessageControlID`. For FHIR, dedupe on `Bundle.identifier` + `entry.fullUrl`.

**MVP for take-home:** Not applicable — keep SQLite in-process. Mention in SOLUTION.md "Future work" with a 4-line code sketch of an `IdempotentEHRClient` wrapper.

---

## TOP 3 — what a reviewer will actually be impressed by in a 3-day submission

1. **LLM fallback module with tests** (~1 h). `src/llm_client.py` wrapping `openai.chat.completions.create` with `tenacity` retry + a single fallback to Anthropic-or-OpenRouter. Three tests: happy, retry-then-success, primary-down-secondary-wins. This is the highest signal/effort ratio in the entire report — it's the failure mode every reviewer will probe first.
2. **FallbackHandler + pre-recorded WAV + callback-capture-to-SQLite** (~1 h). One Pipecat processor that catches `LLMFailedFrame`/`STTFailedFrame`, plays a checked-in WAV via `TTSSpeakFrame`, regex-extracts a phone number from the last STT chunk (or uses caller-ID), and writes a `callback_requests` row. This is what differentiates a senior from a mid-level submission: the bot has a defined behavior when everything is on fire.
3. **STT/TTS health-check at call setup + documented `ServiceSwitcher` deferral** (~30 min). A `voice_providers.py` that health-checks ElevenLabs at startup and swaps to Deepgram/OpenAI TTS if it fails, plus a SOLUTION.md paragraph explaining mid-call swap is deferred because Pipecat issue #4139 makes `ServiceSwitcher` unreliable on long calls. Citing the open issue by number is the move that proves you read source, not blog posts.

Skip for take-home: LiteLLM proxy, Redis-backed circuit breaker, idempotency keys (no remote EHR yet).

---

## Sources

- [LiteLLM vs OpenRouter: Which Wins for Production AI Agents (2026) — MPIV](https://mpiv.ai/blog/litellm-vs-openrouter-which-wins-for-production-ai-agents-2026)
- [LiteLLM model_fallbacks tutorial](https://docs.litellm.ai/docs/tutorials/model_fallbacks)
- [Top 5 LLM Failover Routing Gateways in 2026 — Maxim](https://www.getmaxim.ai/articles/top-5-llm-failover-routing-gateways-in-2026-2/)
- [Pipecat ParallelPipeline docs](https://docs.pipecat.ai/server/pipeline/parallel-pipeline)
- [Pipecat issue #4139 — ServiceSwitcher WebSocket errors](https://github.com/pipecat-ai/pipecat/issues/4139)
- [Pipecat v1.0.0 release notes (Apr 2026)](https://newreleases.io/project/github/pipecat-ai/pipecat/release/v1.0.0)
- [Best Voice Agent Stack — Hamming AI (2026)](https://hamming.ai/resources/best-voice-agent-stack)
- [Retry Strategies for LLM API Calls — CallSphere (2026)](https://callsphere.ai/blog/retry-strategies-llm-api-calls-exponential-backoff-jitter-tenacity)
- [Tenacity documentation](https://tenacity.readthedocs.io/)
- [Voice UI Design Guide 2026 — Fuse Lab Creative](https://fuselabcreative.com/voice-user-interface-design-guide-2026/)
- [AI Agent Fallback Strategy — Stackwell (2026)](https://iamstackwell.com/posts/ai-agent-fallback-strategy/)
- [Working with the new Idempotency Keys RFC — HTTP Toolkit](https://httptoolkit.com/blog/idempotency-keys/)
- [Adyen API idempotency docs](https://docs.adyen.com/development-resources/api-idempotency)
- [EHR API Integration Guide 2026 — CapMinds](https://www.capminds.com/blog/ehr-api-integration-guide-authentication-standards-real-implementation-patterns/)
