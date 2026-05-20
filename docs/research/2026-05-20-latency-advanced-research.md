# Advanced Latency Reduction for Pipecat Voice Agents — 2026 Research

> Context: Prosper Technologies challenge bot. Current pipeline is `ElevenLabsRealtimeSTT → DispatcherProcessor (FSM, per-state system prompt + tool whitelist) → ElevenLabsTTS`, pinned to Pipecat `0.0.100`. TTFT is already instrumented via `TimingCollector` (per-span JSON). OpenAI prompt cache is hitting (~1400-token preamble). 42 unit tests green. This document evaluates the five "on the bench" wins against the current Pipecat ecosystem and ranks them by visible reviewer impact.

---

## 1. Streaming TTS with ElevenLabs in Pipecat

**State of the API (May 2026).** The class is still `pipecat.services.elevenlabs.tts.ElevenLabsTTSService` (WebSocket transport). The legacy `flush_on_punctuation` and `optimize_streaming_latency` flags are gone from the WS service — `optimize_streaming_latency` only survives on the HTTP variant (`ElevenLabsHttpTTSService`) and is officially marked deprecated by ElevenLabs in favor of latency tiers in the model itself. The current low-latency primitives are:

- `text_aggregation_mode`: `SENTENCE` (default, natural prosody) vs `TOKEN` (stream raw tokens, lowest TTFB).
- `auto_mode` (bool, default `None`): disables server-side chunk scheduling. When unset, Pipecat enables it for `SENTENCE` mode and disables it for `TOKEN` mode (because TOKEN needs server-side buffering for natural-sounding output).
- `settings=ElevenLabsTTSService.Settings(voice=..., model="eleven_turbo_v2_5" | "eleven_flash_v2_5", speed=1.0..1.2)`.

**Current code.** `bot.py:137` instantiates `ElevenLabsTTSService(api_key=..., voice_id=...)` — default `SENTENCE` aggregation, `auto_mode` auto-on. Already streaming, but not tuned. Sentences only flush on `.`, `?`, `!`. For long replies (state preambles, EHR summaries) this adds ~200–400 ms of first-audio delay vs. clause-level flush.

**MVP patch (<15 min, no test breakage):**

```python
# bot.py — replace tts construction
from pipecat.services.tts_service import TextAggregationMode

tts = ElevenLabsTTSService(
    api_key=elevenlabs_key,
    text_aggregation_mode=TextAggregationMode.SENTENCE,
    auto_mode=True,
    settings=ElevenLabsTTSService.Settings(
        voice="SAz9YHcvj6GT2YYXdXww",
        model="eleven_flash_v2_5",   # ~75 ms first-audio vs ~200 ms turbo
        speed=1.05,
    ),
)
```

**Risk to unit tests:** Zero. The 42 tests cover `Dispatcher`, `EHRClient`, `LLM` adapter, and `TimingCollector`. None instantiate `ElevenLabsTTSService` directly. Mocked TTS in `tests/` ignores constructor kwargs.

**Sources:**
- ElevenLabs Pipecat docs — https://docs.pipecat.ai/api-reference/server/services/tts/elevenlabs
- Reference: `pipecat.services.elevenlabs.tts` — https://reference-server.pipecat.ai/en/latest/_modules/pipecat/services/elevenlabs/tts.html
- Pipecat issue #2957 (initial greeting latency, confirms `auto_mode` recommendation) — https://github.com/pipecat-ai/pipecat/issues/2957

---

## 2. Filler-Speech / "Thinking…" Nodes

**Canonical pattern (2026).** Pipecat does not ship a `FillerNode`. The community-canonical pattern, popularised by GetStream's "speculative tool calling" post, is:

1. **Prompt-driven filler.** System prompt instructs the LLM to emit a short acknowledgment *before* the tool call, in a tagged format. Filler streams to TTS while the tool runs in parallel.
2. **Hard-coded `TTSSpeakFrame` injection.** From a custom `FrameProcessor`, push a `TTSSpeakFrame("Un moment, ho miro…", append_to_context=False)` immediately *before* the long-running async call. The `append_to_context=False` flag keeps it out of LLM history so the model does not echo the filler in its real reply.

Pattern (2) is the right fit for our dispatcher because **we, not the LLM, decide when a tool fires** (the FSM owns tool whitelists). We already know which transitions trigger an EHR roundtrip (`buscar_pacient`, `crear_visita`).

**MVP patch (~45 min):**

```python
# dispatcher.py — expose a "slow tool" predicate per state.
SLOW_STATES = {"identificacio", "agendament"}  # any state that hits EHR

# bot.py — in DispatcherProcessor.process_frame, before handle_user_turn:
if self._dispatcher.state.value in SLOW_STATES:
    await self.push_frame(
        TTSSpeakFrame("Un moment si us plau…", append_to_context=False)
    )
reply = await self._dispatcher.handle_user_turn(user_text)
```

A more polished version varies the filler per state ("Estic buscant la teva fitxa…", "Comprovo l'agenda…") and gates on a measured-latency threshold — only fire filler if last turn for this state was >800 ms.

**Risk to unit tests:** Low. `DispatcherProcessor` is exercised in `tests/test_bot_pipeline.py` if it exists; check whether the test asserts the exact sequence of pushed frames. If it does, the filler frame breaks the assertion — gate filler emission on `os.environ.get("PROSPER_FILLER_ENABLED")` to keep tests deterministic, or update the assertion to allow an optional `TTSSpeakFrame` prefix. `append_to_context=False` keeps `Dispatcher` state unchanged, so dispatcher tests are unaffected.

**Sources:**
- Pipecat TTS guide (TTSSpeakFrame semantics, `append_to_context`) — https://docs.pipecat.ai/guides/learn/text-to-speech
- GetStream "Speculative Tool Calling for Voice" — https://getstream.io/blog/speculative-tool-calling-voice/
- AWS Bedrock + Pipecat voice agent guide (filler phrases recommendation) — https://aws.amazon.com/blogs/machine-learning/building-intelligent-ai-voice-agents-with-pipecat-and-amazon-bedrock-part-1/

---

## 3. Proactive Prefetch on Partial STT

**State of the API.** `pipecat.frames.frames.InterimTranscriptionFrame` is alive and still emitted by `ElevenLabsRealtimeSTTService` on every partial chunk. Pipecat PR #3637 explicitly removed *timing-side* InterimTranscriptionFrame handling (it no longer counts toward turn boundaries) — but interim frames still flow through the pipeline and any `FrameProcessor` can consume them.

**Why nobody ships this.** Three reasons surface across Pipecat issues:

1. **Wasted spend.** Partial transcripts mutate up to the moment of final. Prefetching `buscar_pacient("Joan Mart")` and then `…ínez García` doubles EHR load for no value if the user corrects.
2. **Race with FSM.** The dispatcher's tool whitelist depends on current state. Firing a tool on partials means firing without state-validation — a footgun if the state has already advanced.
3. **Cache invalidation.** OpenAI prompt cache (the win we already have) keys on prefix match. Prefetching with a partial transcript creates a different cache key than the final, costing us the cache hit.

The honest production pattern in 2026 is **partial-transcript LLM warmup, not tool prefetch**: as soon as the partial stabilises for ~300 ms, fire the LLM call with the in-flight transcript so cache gets warmed and `cached_prompt_tokens` is non-zero by the time the final lands.

**MVP patch (~45 min):** Not recommended pre-deadline. The cache-warming variant is interesting but only saves ~80–120 ms (cache lookup) and risks doubling LLM spend for negligible reviewer-visible benefit. Skip.

**Risk to unit tests:** High if attempted. `DispatcherProcessor` currently only handles `TranscriptionFrame`. Adding `InterimTranscriptionFrame` handling adds a new code path that needs test coverage; the FSM's "single source of truth" property becomes harder to assert.

**Sources:**
- Pipecat frames reference (`InterimTranscriptionFrame`) — https://reference-server.pipecat.ai/en/stable/api/pipecat.frames.frames.html
- Pipecat CHANGELOG — https://github.com/pipecat-ai/pipecat/blob/main/CHANGELOG.md (search "InterimTranscriptionFrame", PR #3637)
- Nemotron January 2026 example (interim STT in practice) — https://github.com/pipecat-ai/nemotron-january-2026/blob/main/pipecat_bots/nvidia_stt.py

---

## 4. Per-Node Model Selection: `gpt-4o-mini` vs `gpt-5.4-nano` vs `gpt-5.4-mini`

**Current state.** `bot.py:129` uses `gpt-4o-mini` everywhere via `PROSPER_BOT_MODEL` env var. Same model for state-transition classification (1-token decision) and full conversational replies.

**2026 reality (Softcery LLM-for-voice benchmark, Adam Holter pricing review):**

| Model | TTFT (s) | $/M in | $/M out | Tool-calling (tau2-bench) | Notes |
|---|---|---|---|---|---|
| gpt-4o-mini | ~0.9 | $0.15 | $0.60 | ~74% | Baseline. Function-calling stable. |
| gpt-5.4-nano | ~1.14 | $0.20 | $1.25 | (no data published; nano not graded on tau2) | OpenAI markets it for "classification, extraction, sub-agents". Slightly higher TTFT than 4o-mini but smarter on Catalan/Spanish in published Softcery scores. |
| gpt-5.4-mini | ~1.0 | $0.75 | $4.50 | 93.4% (vs 74% for prior gen) | The function-calling king at sub-flagship price. |

**Recommendation.** Two-tier setup:

- `PROSPER_BOT_MODEL=gpt-5.4-mini` for the dispatcher reply LLM (full conversational turn + tool call). Function-calling jumps from 74% → 93%, which directly reduces dispatcher retry loops the reviewer can hear.
- `PROSPER_CLASSIFIER_MODEL=gpt-5.4-nano` for state-transition heuristics (already a small decision, doesn't need 93% tool quality).
- Keep prompt cache: the 1400-token preamble works identically across the 5.4 family.

**Caution: reasoning toggle.** Every 5.4-family model ships with reasoning mode. **Do not enable it.** Softcery measures TTFT climbing to 8–200 s under reasoning. For voice, pass `reasoning={"effort": "minimal"}` (or omit, defaults vary per provider) and never set `medium`/`high`.

**MVP patch (<10 min):**

```python
# bot.py:129
llm = OpenAILLMAdapter(
    client=client,
    model=os.environ.get("PROSPER_BOT_MODEL", "gpt-5.4-mini"),
    # If OpenAILLMAdapter doesn't already pin this, add it:
    # extra_body={"reasoning": {"effort": "minimal"}},
)
```

**Risk to unit tests:** Medium. Tests that mock LLM responses are unaffected. Tests that hit a recorded fixture and assert exact model name in the request body will fail — search `tests/` for `"gpt-4o-mini"` string literals and replace. The `Dispatcher` tool-call schema is unchanged, so JSON-shape assertions are safe.

**Sources:**
- Softcery — Choosing an LLM for Voice Agents 2026 — https://softcery.com/lab/ai-voice-agents-choosing-the-right-llm
- Adam Holter — GPT-5.4 Mini and Nano benchmarks & pricing — https://adam.holter.com/gpt-5-4-mini-and-nano-benchmarks-pricing-and-what-theyre-actually-good-for/
- Microsoft Azure AI Foundry — GPT-5.4 mini/nano launch — https://techcommunity.microsoft.com/blog/azure-ai-foundry-blog/introducing-openai%E2%80%99s-gpt-5-4-mini-and-gpt-5-4-nano-for-low-latency-ai/4500569

---

## 5. Pipecat-Native Metrics Export

**What `enable_metrics=True` + `enable_usage_metrics=True` actually gives you (we already set both in `bot.py:154`):** Pipecat wraps service-level measurements into `MetricsFrame` objects and streams them downstream alongside audio. Each `MetricsFrame` contains a list of typed `MetricsData`:

- `TTFBMetricsData(processor, model, value)` — time-to-first-byte per service (STT, LLM, TTS).
- `ProcessingMetricsData(processor, model, value)` — wall-clock cost of `process_frame`.
- `LLMUsageMetricsData` — fields: `prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `reasoning_tokens`.
- `TTSUsageMetricsData(processor, value)` — TTS character count (cost driver).
- `TurnMetricsData` — `is_complete`, `probability`, `e2e_processing_time_ms`.

Two ways to surface them:

```python
from pipecat.observers.metrics_log_observer import MetricsLogObserver

task = PipelineTask(
    pipeline,
    params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
    observers=[MetricsLogObserver()],   # logs every MetricsFrame to loguru
)
```

For frame-level custom timing, subclass `BaseObserver` and override `on_push_frame(src, dst, frame, direction, timestamp)` — fires every time a frame crosses two processors.

**Is our custom `TimingCollector` still the right call?** Yes, with one tweak. Our collector records *semantic* spans (`tool:buscar_pacient`, `llm:state=identificacio`) that Pipecat's native metrics cannot produce because Pipecat doesn't know about the FSM. Pipecat's TTFB is per-service (LLM, TTS) — it cannot attribute time to "the EHR roundtrip inside dispatcher". Keep `TimingCollector` for semantic spans, **add `MetricsLogObserver` for free service-level TTFB on top of it.** The two are complementary, and `LLMUsageMetricsData.cache_read_input_tokens` is the canonical replacement for the `cached_prompt_tokens` plumbing we have to do manually today.

**MVP patch (<15 min):**

```python
# bot.py
from pipecat.observers.metrics_log_observer import MetricsLogObserver
from pipecat.metrics.metrics import LLMUsageMetricsData, TTFBMetricsData
from pipecat.observers.base_observer import BaseObserver

class TimingBridgeObserver(BaseObserver):
    """Funnel Pipecat MetricsFrame into our TimingCollector."""
    def __init__(self, dispatcher: Dispatcher):
        super().__init__()
        self._d = dispatcher
    async def on_push_frame(self, src, dst, frame, direction, timestamp):
        from pipecat.frames.frames import MetricsFrame
        if not isinstance(frame, MetricsFrame): return
        for m in frame.data:
            if isinstance(m, TTFBMetricsData):
                self._d.timing.record(phase=f"pipecat:{m.processor}:ttfb",
                                      duration_ms=m.value * 1000,
                                      state=self._d.state.value)
            elif isinstance(m, LLMUsageMetricsData):
                self._d.timing.record_cache(
                    cached=m.value.cache_read_input_tokens or 0,
                    total=m.value.prompt_tokens or 0,
                )

task = PipelineTask(
    pipeline,
    params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
    observers=[MetricsLogObserver(), TimingBridgeObserver(dispatcher)],
)
```

**Risk to unit tests:** Zero if `TimingCollector.record_cache` already exists; if it doesn't, adding it is a pure-add. The bridge observer is only wired in the live pipeline (`run_bot`), not in unit tests.

**Sources:**
- Pipecat Metrics guide — https://docs.pipecat.ai/guides/fundamentals/metrics
- Pipecat metrics module reference — https://reference-server.pipecat.ai/en/stable/api/pipecat.metrics.metrics.html
- FullStackML "Where does the time go?" (concrete Pipecat TTFB numbers: STT 195 ms, LLM 268 ms, TTS 23 ms) — https://www.fullstackml.dev/p/15-where-does-the-time-go-measuring

---

## Top-3 wins, ranked by reviewer-audible impact

The reviewer's perception of the bot is dominated by **first-syllable latency** and **dead-air gaps during tool calls**. Ranking accordingly:

1. **Filler speech before EHR tool calls (#2)** — Highest reviewer-audible impact. The current dead air during `buscar_pacient` / `crear_visita` is the one moment a human reviewer notices "the bot is slow". Filler converts 800–1500 ms of silence into perceived responsiveness. ~45 min, low test risk if gated by env var. **Pick this first.**

2. **gpt-5.4-mini for the dispatcher LLM + minimal reasoning effort (#4)** — Tool-calling reliability jumps from ~74% → 93%. Reviewer hears fewer "let me try that again" loops and fewer wrong-tool retries. Latency is roughly flat vs 4o-mini, the win is *correctness under voice constraints*, which the reviewer experiences as "this bot understood me the first time". ~10 min, medium test risk (grep for model name string literals). **Pick second.**

3. **ElevenLabs Flash v2.5 + tuned settings (#1)** — Drops first-audio latency from ~200 ms to ~75 ms per TTS chunk. Compounds across every bot utterance. The reviewer won't consciously notice the 125 ms but will feel the bot is "snappier". <15 min, zero test risk. **Pick third — it's the cheapest insurance.**

Skip #3 (partial-STT prefetch) — too much complexity for ≤120 ms and a real risk of breaking the FSM single-source-of-truth invariant. Add #5 (`MetricsLogObserver` + cache-tokens bridge) as a 15-min side-quest if time permits; it strengthens the eval story without changing runtime behavior.
