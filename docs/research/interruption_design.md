# Interruption / Barge-in Design

Research synthesis for adding context-aware barge-in handling to the Prosper voice agent. Read-only research; no code edits in this pass.

## 1. Current state

The bot today has **no explicit interrupt handling at the dispatcher layer**. Barge-in works only to the extent that Pipecat's Silero VAD cancels in-flight TTS, but that cancellation never propagates back to `Dispatcher.history`. A grep for `interrupt|barge|StartInterruption|partial_bot` across `src/prosper/` returns only two comments (`bot.py:208`, `bot.py:448`), neither of which records anything.

Concretely:

- `DispatcherProcessor.process_frame` (`src/prosper/bot.py:186-219`) listens for `StartFrame`, `TranscriptionFrame`, `LLMMessagesAppendFrame`, `EndFrame`. It does **not** subscribe to `StartInterruptionFrame`, `BotStartedSpeakingFrame`, or `BotStoppedSpeakingFrame`. Any of those frames flow through untouched.
- `_route_user_text` (`bot.py:221-265`) pushes the bot reply as a single `TTSSpeakFrame(reply)` (line 264). Pipecat may chop that into chunks downstream, but the dispatcher's view is atomic: it appends `{"role": "assistant", "content": reply.text}` to `self.history` in `dispatcher.py:411-424` immediately after the LLM call, **before** TTS has spoken a single syllable. If the user barges in two words into a 40-word reply, `history` still claims the bot delivered all 40.
- Silero VAD is configured in `bot.py:439-460` with `start_secs=0.15, stop_secs=1.2, min_volume=0.3, confidence=0.5` (note: docstring at line 444-449 lags behind the actual values — comment says `min_volume=0.4, confidence=0.6`, code says `0.3 / 0.5`). When VAD fires `UserStartedSpeakingFrame`, Pipecat's `BaseTransportOutput` (per upstream docs) emits a downstream `InterruptionFrame` that flushes the TTS clock queue. The transport stops audio playback; **the dispatcher is never told this happened**.
- `Dispatcher.history` is built by `_messages_for_llm` (`dispatcher.py:1059-1081`) from `self.history` (sliced to the last 40 messages) plus the persona + per-state task system messages. It has no concept of "this assistant turn was truncated."

Consequence: on the next user turn, the LLM sees the user's barge-in utterance against a history that lies — the bot's full pre-interruption reply appears as if fully delivered. If the bot started saying "Booking 2 PM with Dr. Smith on…" and the user cut in with "wait, no", the model's input shows the booking was confirmed in speech. Tool-call validation prevents *most* hallucinated mutations (`_validate_against_memory`, `dispatcher.py:637-740`), but conversational coherence falls off a cliff: the LLM may say "Great, see you at 2 PM" because as far as it knows the user already heard the confirmation.

Pipecat does emit `StartInterruptionFrame` (per [reference docs](https://reference-server.pipecat.ai/en/stable/api/pipecat.frames.frames.html)). Its built-in `AssistantTranscriptProcessor` would emit a TranscriptionMessage at that point summarising whatever `TTSTextFrame`s the TTS service announced before the interrupt — but **we are not using that processor**. We bypass the standard `LLMContextAggregatorPair` because our FSM dispatcher owns history.

## 2. Other candidates' handling

Read of `other solutions/{AlexLopezGomez,PauMinguet,MarioW333,NoelDNathan}_prosper-challenge` for explicit barge-in handling.

| Candidate | Approach | Verdict |
|---|---|---|
| **AlexLopezGomez** | Uses `pipecat-flows`. Sets `cancel_on_interruption=False` on all node-transition tool registrations (`flows/nodes.py:137,233,299,418,494,574,643`). Tunes `MinWordsUserTurnStartStrategy(min_words=3, use_interim=False)` (`bot.py:355-362`) to filter mic-echo false interrupts during long TTS. Acknowledges the issue ("Barge-in / VAD tuning per population" — `SOLUTION.md:306`, deferred). Caught a real bug: "Flows greet→collect_identity transition dropped under interruption" (`SOLUTION.md:339`) — fix was inlining the greet into the next node. | Tackles transport-layer false-cancel but **does not address history coherence after a real interrupt**. The flows context aggregator records whatever pipecat's built-in does — partial assistant content lands in context, but no explicit annotation. |
| **MarioW333** | Disables interruptions entirely: `allow_interruptions=False` on the transport (`bot.py:299`) and `cancel_on_interruption=False` on every tool (`bot.py:243-246`). Documents the choice in `SOLUTION_2.md:126` as a deliberate trade-off to protect Healthie writes. | Sidesteps the problem — the bot literally cannot be interrupted. Bad UX for a clinical voice agent (caller cannot correct the bot mid-sentence) but technically simplest. |
| **NoelDNathan** | Uses default Pipecat interrupt behaviour; sets `cancel_on_interruption=False` on tool handlers (`bot.py:92-93`) so tool execution survives user speech. Relies entirely on `LLMContextAggregatorPair` to record what the bot spoke. | Closest to what we want but **trusts Pipecat's context aggregator implicitly**. No timeline annotation, no explicit "interrupted" marker; the LLM has to infer interruption from the truncated assistant content alone. |
| **PauMinguet** | Has the best **research** doc (`docs/research/pipecat-tool-calling.md:108-110, 182`) explaining the `cancel_on_interruption` trade-off for mutation tools, but their implementation only inherits the defaults. Their `plan.md:14` reads "leave default `cancel_on_interruption=True`. Idempotency keeps us safe." — relies on backend idempotency rather than instrumentation. | Right conceptual hooks identified, but the design treats interruption as a pipeline concern, not a conversation-history concern. |

What they all miss: **none of the four reconstruct an explicit "interrupted at word N" annotation in the message history visible to the LLM**. They lean on either (a) pipecat's aggregator, which records best-effort partial text but does not flag interruption to the model, or (b) backend idempotency. Neither solves the timeline-coherence problem the user described.

Side note: Pipecat issue [#4466](https://github.com/pipecat-ai/pipecat/issues/4466) ("Interruption drops already-spoken TTSTextFrames from the output transport's clock queue") confirms upstream that even the partial text already pronounced can be lost on interrupt — so we can't rely on the framework to record it for us.

## 3. Best-practice consensus from voice-agent research

From the OpenAI Realtime API guide ([latent.space](https://www.latent.space/p/realtime-api), [OpenAI docs](https://platform.openai.com/docs/guides/realtime-conversations)):

- LLMs generating audio (or TTS playing back text) run **faster than playback**. The server-side history will always contain more than the user actually heard unless explicitly truncated. The Realtime API exposes `conversation.item.truncate` for exactly this reason — the developer is expected to compute "audio actually played" and tell the server.
- The widely-recommended pattern is: on interrupt, **rewrite the assistant turn in history to reflect only the audible portion**, plus an explicit marker (e.g. `[interrupted]`) so the next LLM call understands the truncation was unintentional.
- Without this, the model produces "did you hear that?" style confusion: it acknowledges things the caller never received.

From Pipecat docs ([transcript_processor](https://reference-server.pipecat.ai/en/latest/_modules/pipecat/processors/transcript_processor.html), [speech-input](https://docs.pipecat.ai/pipecat/learn/speech-input)):

- `AssistantTranscriptProcessor` aggregates `TTSTextFrame`s and completes the current utterance on either `BotStoppedSpeakingFrame` (clean end) or `StartInterruptionFrame` (interrupt). It does emit a transcription message in both cases — but it does **not** add an interruption marker; downstream consumers cannot tell the two cases apart from the text alone.
- `TTSTextFrame` is the text the TTS service has *received and started synthesizing*. `BotStartedSpeakingFrame` / `BotStoppedSpeakingFrame` from `BaseTransportOutput` are the closest signal to "audio actually left the transport." For the most accurate "what the user heard" estimate, observe `TTSTextFrame`s pushed downstream between `BotStartedSpeakingFrame` and the `StartInterruptionFrame`.

Dissent / caveats:

- Some voice-agent vendors (LiveKit, Vapi) opt to drop the partial bot text entirely and replay only the user's intervention. Simpler but loses context: a user saying "no no" makes no sense without seeing what they were saying no to.
- A minority position is "interruption is rare enough that getting it slightly wrong is fine." This is plausible for casual chat; **for a healthcare booking agent dealing with PHI it is not** — confusing the user about whether their appointment was confirmed is a clinical-safety issue.

Consensus: annotate the assistant turn with a best-effort approximation of what was audible, plus an explicit `[interrupted]` marker, and feed the next LLM turn the full bilateral timeline so it can decide whether the user's reply targets the interrupted utterance or an older turn.

## 4. Three candidate designs

All three share the same goal: when the user barges in, the dispatcher's history must reflect a truncated, marked assistant turn so the next LLM call sees an honest timeline. They differ in (a) how partial bot text is captured, (b) what the history payload looks like, and (c) how invasive the changes to `bot.py` and `dispatcher.py` are.

### Design A — Word-clock approximation in the dispatcher

**Data model for partial bot turn.** Each assistant turn carries `spoken_chars: int` (estimated by elapsed playback time × characters-per-second) and `interrupted: bool`. `Dispatcher.history` continues to hold dicts; the assistant message gains optional metadata fields that survive the `_messages_for_llm` pipeline by being collapsed into the message body just before send-off.

**History serialization shape sent to LLM** (one example turn):
```
{"role": "assistant", "content": "Booking 2 PM with Dr. Smith on Tuesday — [INTERRUPTED ~14 words in by user]"}
```
The `[INTERRUPTED ~14 words in by user]` suffix is appended deterministically; the LLM is told via a one-line addition to `CLINIC_PERSONA` that this annotation means the caller may not have heard the rest.

**Frame-level changes.** `DispatcherProcessor` subscribes to two new frames: `BotStartedSpeakingFrame` (start a wall-clock timer for the in-flight assistant turn) and `StartInterruptionFrame` (stop the timer, compute estimated chars spoken, call `dispatcher.mark_last_assistant_interrupted(spoken_chars)`). The dispatcher trims `history[-1]['content']` to the first `spoken_chars` characters (rounded to the nearest word boundary) and appends the marker.

**Failure modes.**
- Chars-per-second is a synthesis-rate estimate, not a measurement. ElevenLabs Flash v2.5 varies ±15% by utterance. We round to nearest word so the LLM sees plausible text but the count is approximate. Acceptable for "user reacting to recent line" inference; not acceptable for legal-record purposes.
- Race: `StartInterruptionFrame` can arrive after the dispatcher has already finished the next LLM call cycle. Need a sequencing assertion or the truncation overwrites the wrong turn.
- If the user barges in *before* TTS started speaking (e.g. between assistant token-receipt and audio output), `spoken_chars=0` and we mark the whole turn as `[NOT HEARD]`. Better than claiming it was spoken; equivalent UX to dropping it.

**Test plan.**
- Unit: simulate the frame sequence `[StartFrame, ..., BotStartedSpeakingFrame, sleep(0.5s), StartInterruptionFrame, TranscriptionFrame('no don't book')]`. Assert `dispatcher.history[-1]` is the truncated assistant turn with the marker and the next user turn follows.
- Mock-eval: add 2 scenarios to `evals/scenarios.py` — `barge_in_during_confirm` (user cuts in during confirmation read-back) and `barge_in_correcting_slot` (user cuts in mid slot-list to correct day).
- Live integration: run the bot, observe `make eval` doesn't regress.

### Design B — TTS-text-frame observer (audible substring)

**Data model.** Same `interrupted: bool` flag on the assistant message, but the audible substring is **measured**, not estimated: a `TTSAudibleObserver` processor sits downstream of TTS and upstream of transport.output, accumulating every `TTSTextFrame` payload as it passes. On `BotStoppedSpeakingFrame` (clean end) it clears the buffer; on `StartInterruptionFrame` it sends `dispatcher.mark_last_assistant_interrupted(spoken_text=buffer)`.

**History serialization shape:**
```
{"role": "assistant", "content": "Booking 2 PM with Dr. Smi— [INTERRUPTED, caller cut in]"}
```
The text reflects whatever TTS got to before the interrupt frame propagated. Tail dash + marker hint at the truncation.

**Frame-level changes.** New `TTSAudibleObserver(FrameProcessor)` in `bot.py`, inserted between `tts` and `transport.output()` in the `Pipeline([...])` list (`bot.py:379-388`). It needs a back-channel to the dispatcher — easiest via a method handle passed in the constructor. `DispatcherProcessor` no longer needs to subscribe to the speech frames; the observer is the single source of truth. Adds a state shared between two processors, which is mildly awkward.

**Failure modes.**
- `TTSTextFrame` represents text *sent to* the TTS engine, not text *heard by* the user. ElevenLabs streams audio, and a chunk pushed to TTS might be 200 ms into the synth pipeline when interruption fires — so the buffer overcounts. Less wrong than Design A's wall-clock estimate (it's the actual text TTS received), but still not "what the caller heard."
- Pipecat issue [#4466](https://github.com/pipecat-ai/pipecat/issues/4466) notes that already-spoken TTSTextFrames can be dropped by the output transport's clock queue on interrupt. If we observe between TTS and transport, we still see them — but the buffer might include text that was queued but never played.
- Cross-processor state is a regression risk: a refactor of pipeline order could silently break the link.

**Test plan.** Same as Design A, plus a pure unit test for `TTSAudibleObserver` that injects a sequence of `TTSTextFrame`s followed by `StartInterruptionFrame` and asserts the buffered substring is what we expect.

### Design C — Audio-clock truncation (most accurate, most invasive)

**Data model.** Assistant message carries `spoken_ms: float`. The dispatcher uses a known character-rate-per-voice constant to convert that to a char count, but the source-of-truth is wall-clock playback time, not text-frame counting.

**History serialization shape:**
```
{"role": "assistant", "content": "Booking 2 PM with Dr. Smith", "_interrupted": true, "_spoken_ms": 1840}
```
Then `_messages_for_llm` renders to:
```
{"role": "assistant", "content": "Booking 2 PM with Dr. Smith [INTERRUPTED after 1.8s]"}
```

**Frame-level changes.** Requires hooking into the transport's audio clock — the `BaseTransportOutput` exposes a clock-queue position. This is internal to Pipecat 0.0.100; we'd need to either monkey-patch or fork. The data we want (audio samples actually emitted to the WebRTC track) is the *only* honest answer to "what did the user hear", but extracting it from this Pipecat version is non-trivial. v1.0.0 reportedly cleans this up but the codebase explicitly pins 0.0.100 (`bot.py:138-148`).

**Failure modes.**
- Implementation depth and pipecat-version coupling. Likely the wrong cost/benefit for this submission.
- Even with the accurate timestamp, the audio→character mapping is still an approximation; the user-visible benefit over Design B is marginal.

**Test plan.** Same as Design B plus integration test that exercises real transport — currently we only have unit-level pipeline tests, so this means new infrastructure.

## 5. Recommendation

**Design B (TTS-text-frame observer) with the marker shape proposed in §4-B.**

Rationale:

- Captures the actual text TTS attempted to synthesize, not an elapsed-time guess. The LLM-visible annotation matches a recognisable string from the original utterance, which strengthens the model's ability to infer "user is reacting to this specific line".
- One new processor, one new method on `Dispatcher`, one annotation rule for `_messages_for_llm`. Roughly 80-120 lines net. No fork of Pipecat.
- The asymmetry with Design A: Design A puts wall-clock logic *inside* the dispatcher, which means the dispatcher gains a real-time-clock dependency it didn't have before. Design B keeps the dispatcher pure (text in, text out) and isolates the timing-sensitive piece in a processor — same separation we already have for STT/TTS.
- Pipecat's own `AssistantTranscriptProcessor` already does ~80% of this work upstream of where we'd need it. Design B is essentially "build the equivalent, but route the output into our FSM dispatcher's history instead of Pipecat's context aggregator we aren't using."

The annotation format the LLM sees should be:

```
{"role": "assistant", "content": "<partial text>… [INTERRUPTED by user]"}
{"role": "user", "content": "<the barge-in utterance>"}
```

The persona preamble (`prompts.py::CLINIC_PERSONA`) gets one paragraph added (≤80 tokens) explaining: an assistant turn ending in `[INTERRUPTED by user]` means the caller may not have heard the rest; the next user turn may target the interrupted line OR an older turn; reason from the timeline. This is the only `prompts.py` change required and stays well under the 1KB-per-entry budget (CLINIC_PERSONA is exempt — it's intentionally long for cache hit).

Tool-cancellation policy is **orthogonal** to this design: we should also pass `cancel_on_interruption=False` on the EHR-mutation tools (`create_appointment`, `cancel_appointment`, `reschedule_appointment`, `create_patient`) when they are migrated to Pipecat's tool-registration system, so partial writes don't get aborted by a cough. Today our dispatcher runs tools imperatively (not via `llm.register_function`), so this is a non-issue right now — but it should be called out in CONTRIBUTING.md when we add it.

## 6. Open questions for the human

1. **What's the canonical marker text?** `[INTERRUPTED by user]` is one option; alternatives are `[truncated]`, `<interrupted/>`, structured `{"interrupted": true}`. Trade-off: natural-language marker is easier for the LLM to handle reliably; structured marker is easier for evals to assert on. The LLM-judge eval (`evals/`) operates on bot speech, not history, so either works for evals — only the persona-preamble wording changes.

2. **Do we want a `TURN_INTERRUPTED` console-bus event?** The operator console currently surfaces transcript turns and state changes. Adding an `interruption` event (with `partial_text` redacted by `mask_phone` if needed) gives the live operator a "caller talked over the bot here" signal — useful for QA but adds an event type to maintain.

3. **Should the dispatcher's `_maybe_transition_from_user_text` be wired to detect "no, wait, don't do that" intents specifically when the previous turn is `[INTERRUPTED]`?** The current goodbye / book-intent / cancel-intent regexes (`dispatcher.py:91-138`) don't distinguish "user reacting to interrupted line" from "user starting new thread". Adding an `_INTERRUPTED_DENY` heuristic risks branching where the user explicitly said "no hard branching, context-aware." Recommendation: skip — let the LLM decide. But the question is worth a one-line yes/no from you.

4. **What about user-side interruptions of *their own* utterance?** Not all barge-in is bot-interrupted — sometimes the user trails off and self-corrects ("Tuesday at — no, Wednesday"). Pipecat's STT aggregator typically handles this transparently (single `TranscriptionFrame` with the corrected text) and is out of scope for this design — but it deserves a sentence in the doc you eventually ship.

5. **Eval-side timing.** Our eval harness mounts the EHR in-process via `httpx.ASGITransport` and the LLM is mocked. There's **no audio path**, so by construction no interrupt can fire in evals. Should we add a synthetic `simulate_barge_in` helper to `evals/mock_llm.py` that injects an `[INTERRUPTED]` annotation between turns? This is the only way the FSM scenarios will ever exercise the new history shape; without it the codepath is only tested in live integration.

6. **VAD doc-string drift.** While reading I noticed `bot.py:444-449` says `min_volume=0.4, confidence=0.6` in a comment block but the actual code (`bot.py:452-455`) is `confidence=0.5, min_volume=0.3`. Unrelated to this design but worth a one-line fix in whatever PR ships first.

## Sources

- [Pipecat `frames.html` reference](https://reference-server.pipecat.ai/en/stable/api/pipecat.frames.frames.html)
- [Pipecat `transcript_processor.html` source](https://reference-server.pipecat.ai/en/latest/_modules/pipecat/processors/transcript_processor.html)
- [Pipecat `speech-input` learn page](https://docs.pipecat.ai/pipecat/learn/speech-input)
- [Pipecat issue #4466 — interruption drops already-spoken TTSTextFrames](https://github.com/pipecat-ai/pipecat/issues/4466)
- [Pipecat issue #2785 — using InterimTranscription to trigger interrupts earlier](https://github.com/pipecat-ai/pipecat/issues/2785)
- [Pipecat issue #2791 — context not updated on user interruptions](https://github.com/pipecat-ai/pipecat/issues/2791)
- [OpenAI Realtime API: The Missing Manual (latent.space)](https://www.latent.space/p/realtime-api)
- [OpenAI Realtime conversations guide](https://platform.openai.com/docs/guides/realtime-conversations)
- Local: `other solutions/AlexLopezGomez_prosper-challenge/{bot.py,SOLUTION.md,flows/nodes.py}`
- Local: `other solutions/PauMinguet_prosper-challenge/docs/research/pipecat-tool-calling.md`
- Local: `other solutions/MarioW333_prosper-challenge/bot.py`
- Local: `other solutions/NoelDNathan_prosper-challenge/bot.py`
