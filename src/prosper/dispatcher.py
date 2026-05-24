"""Custom FSM dispatcher.

Drives the conversation by:
1. Building per-turn LLM request with [persona, task_message, history].
2. Calling the LLM via an injectable client (real OpenAI in prod; canned
   replies in tests).
3. Filtering tool calls against the current state's whitelist — REJECTING
   any call not allowed, injecting a system note so the LLM retries.
4. Executing allowed tool calls via the tool handlers; recording every
   action in the transcript.
5. Deciding the next state from tool result codes and short keyword scans
   of the LLM reply.

Kept intentionally small so reviewers can trace the whole machine in one
read.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from prosper.console.bus import ConsoleBus
from prosper.console.events import EventType, make_event
from prosper.ehr_client import EHRClient
from prosper.flows import ALLOWED_TOOLS, TRANSITIONS, State
from prosper.observability.redact import mask_name, mask_phone
from prosper.observability.timing import TimingCollector
from prosper.prompts import CLINIC_PERSONA, FALLBACK_LINES, build_task_message
from prosper.result import Err, Ok, Result, is_err, is_ok
from prosper.speculation import build_disambiguation_message, classify_find_result
from prosper.tools import HANDLERS, ROUTE_INTENT_TOOL, TOOL_SCHEMAS


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    # OpenAI requires every assistant message with tool_calls to carry an id
    # the matching tool-response message references via `tool_call_id`. We
    # capture the LLM-provided id in OpenAILLMAdapter; mocks and tests
    # default to a deterministic stub so the wiring still round-trips.
    id: str = "call_stub"


@dataclass
class LLMReply:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: LLMUsage | None = None


class LLMClientProtocol(Protocol):
    async def generate(
        self,
        *,
        state: str,
        history: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LLMReply: ...


@dataclass
class LLMUsage:
    """Optional usage metrics reported by the LLM provider."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_prompt_tokens: int = 0


@dataclass
class SessionMemory:
    """Cross-state data the LLM accumulates within a call."""

    identified_patient: dict[str, Any] | None = None
    last_slots: list[dict[str, Any]] = field(default_factory=list)
    last_upcoming_appointments: list[dict[str, Any]] = field(default_factory=list)
    # Set when caller's wording implies reschedule rather than plain cancel
    # ("I want to reschedule", "move my appointment"). On a successful
    # ``cancel_appointment`` we then auto-transition into BOOK_FLOW instead
    # of ending the call, so the same call can swap out an appointment.
    wants_reschedule: bool = False
    # Latest output of ``suggest_specialty`` for this call. The dispatcher
    # records these for audit / operator-console + lets the LLM reach for
    # them when calling ``list_availability_slots`` / ``create_appointment``
    # without restating the values every turn. ``None`` means the caller
    # never went through triage.
    recommended_specialty: str | None = None
    recommended_duration_minutes: int | None = None
    # When a name+DOB lookup returns more than one candidate (same DOB,
    # similar names — the "John Smith vs Jon Smith" case), we stash the
    # candidates here and stay in IDENTIFY_PATIENT so the LLM can ask the
    # caller which one they are. Resolved (and cleared) once the caller
    # picks. Empty list = no pending disambiguation.
    pending_identity_candidates: list[dict[str, Any]] = field(default_factory=list)


_AFFIRM = re.compile(
    r"\b(yes|yeah|yep|yup|sure|correct|that'?s right|please do|go ahead|sounds good)\b", re.I
)
_DENY = re.compile(r"\b(no|nope|nah|cancel that|stop|wrong)\b", re.I)
# Cancel/reschedule intent — broadened so phrasings like "take my appointment
# off the schedule", "drop my appointment", "remove the booking", "get rid of
# my visit", "delete my appointment" route correctly. Originally only matched
# `cancel|reschedule|move`, which missed real-eval personas (see ERRORS.md /
# `cancel_when_nothing_to_cancel`).
_CANCEL_INTENT = re.compile(
    r"\b(?:cancel\w*|reschedul\w*|move\w*|take[^.]*off|drop\w*|"
    r"remove\w*|delete\w*|get rid of|won'?t make it|can'?t make it|"
    r"not coming|skip\b)",
    re.I,
)
# Subset of cancel intent that also implies booking a replacement. We honour
# this in CANCEL_FLOW so the call ends in BOOK_FLOW after the cancellation,
# not END. Plain "cancel" stays single-action by default.
_RESCHEDULE_INTENT = re.compile(
    r"(?:\breschedul\w*|\bmove\s+(?:my|the)|\bchange\s+(?:my|the)|"
    r"\bswap\b|\bpush back\b|\bpush.*to)",
    re.I,
)
_BOOK_INTENT = re.compile(
    r"\b(book|schedule|appointment|new appointment|new visit|set up|"
    r"see (?:a |the )?(?:doctor|provider|therapist)|sign up|get in|come in)\b",
    re.I,
)
# Strong, unambiguous verbs used to break ties when both cancel- and book-
# intent match (audit F-006). The generic cancel verbs in `_CANCEL_INTENT`
# (skip/move/drop/remove) also appear in booking phrasings ("let's skip the
# chit-chat, I want to BOOK"); an explicit "cancel" or an explicit
# "book/schedule" verb is decisive and is checked before the broad sets.
_STRONG_CANCEL = re.compile(r"\bcancel\w*\b", re.I)
_STRONG_BOOK = re.compile(r"\b(?:book\w*|schedul\w*|sign\s+up|set\s+up)\b", re.I)
# Universal goodbye intent — hanging up is always legal from any state, so a
# match routes us to END regardless of where we are. Split into two tiers
# to avoid the "never mind the insurance, I want to book" false positive
# that aborted a valid booking pre-fix.
#
# Tier 1: unambiguous explicit goodbyes — match ANYWHERE in the utterance.
# "ok goodbye let's stop", "no, goodbye then" — these are always sign-offs.
# NOTE: bare "bye" is deliberately NOT here — see `_GOODBYE_BYE_TRAILING`.
_GOODBYE_HARD = re.compile(
    r"\b(?:goodbye|hang up|end (?:the )?call)\b",
    re.I,
)
# Bare "bye" is a sign-off ONLY at the end of the utterance ("ok, bye",
# "thanks bye"). Mid-sentence it is almost always the STT homophone of "by"
# ("bye the way, can you book me Tuesday"); matching it anywhere falsely hung
# up an in-progress task (audit F-012). "goodbye"/"hang up"/"end the call"
# stay matchable anywhere via `_GOODBYE_HARD`.
_GOODBYE_BYE_TRAILING = re.compile(r"\bbye\b\s*[.!?…]*\s*$", re.I)
# Tier 2: softer sign-offs that can appear mid-utterance with a totally
# different meaning ("never mind the insurance, book me"). Require they sit
# at the END of the utterance so context is implicitly "wrapping up".
_GOODBYE_INTENT_TRAILING = re.compile(
    r"(?:\bnevermind\b|never mind|see you|talk later|"
    r"that'?s all|thanks bye|i'?m done|no thanks|no thank you)"
    r"\s*[.!?…]*\s*$",
    re.I,
)


def _has_goodbye_intent(text: str) -> bool:
    """Return True if the user signalled they want to end the call."""
    return bool(
        _GOODBYE_HARD.search(text)
        or _GOODBYE_BYE_TRAILING.search(text)
        or _GOODBYE_INTENT_TRAILING.search(text)
    )


# Matches the bracketed handles `_redact_for_llm` emits — `[1]`, `1`, `#1`,
# `slot 1`, `appointment 2`. The LLM never receives raw UUIDs (audit A3), so
# this is the only legitimate way it can refer to a slot or appointment.
_HANDLE_RE = re.compile(
    r"^\s*[\[#]?\s*(?:slot|appointment|appt|option|number)?\s*[#:]?\s*(\d+)\s*\]?\s*$",
    re.I,
)


def _maybe_handle_index(candidate: Any) -> int | None:
    """Return the 0-based index encoded by a handle string, or None.

    Recognises ``"1"``, ``"[2]"``, ``"#3"``, ``"slot 1"``, ``"appointment 2"``.
    Returns ``None`` for full UUIDs or anything that doesn't parse — so the
    caller can safely pass the result through to the legacy UUID path.
    """
    if not isinstance(candidate, str):
        return None
    match = _HANDLE_RE.match(candidate)
    if match is None:
        return None
    return int(match.group(1)) - 1


# Ordinal words → 0-based index, for picking from a short disambiguation
# list ("the first one", "second please"). Only the first few — caller is
# choosing between 2-3 candidates, never a long list.
_ORDINAL_WORDS: Final[dict[str, int]] = {
    "first": 0,
    "second": 1,
    "third": 2,
    "fourth": 3,
    "last": -1,
}
_PICK_DIGIT_RE = re.compile(r"\b(?:number|option|the)?\s*#?\s*(\d{1,2})\b", re.I)
# Spoken cardinal as a pick: standalone ("two", "two please") or explicitly
# cued ("number two", "option one"). A bare cardinal embedded in a phrase is
# almost always a PRONOUN ("that one", "one more time", "the fifth one",
# "which one", "neither one") and must NOT select a candidate — matching it
# silently mis-identified the caller as candidate #1 (audit F-013).
_PICK_CARDINAL_RE = re.compile(
    r"^\s*(one|two|three|four)\b(?:\s+please)?[\s.!?]*$"
    r"|\b(?:number|option)\s+(one|two|three|four)\b",
    re.I,
)
_SPOKEN_NUMBERS: Final[dict[str, int]] = {"one": 1, "two": 2, "three": 3, "four": 4}


def _pick_candidate_index(user_text: str, count: int) -> int | None:
    """Map a caller's pick utterance to a 0-based candidate index, or None.

    Handles ordinals ("first"/"second"/"last"), digits ("2", "number 1"),
    and spoken numbers ("two"). Bounded to ``count`` so a stray digit
    (e.g. part of a DOB) can't select an out-of-range candidate. Returns
    None when nothing parses — the caller will be asked again.
    """
    if count <= 0:
        return None
    lowered = user_text.lower()
    for word, idx in _ORDINAL_WORDS.items():
        if re.search(rf"\b{word}\b", lowered):
            resolved = count - 1 if idx == -1 else idx
            return resolved if 0 <= resolved < count else None
    digit_match = _PICK_DIGIT_RE.search(lowered)
    if digit_match is not None:
        n_digit = int(digit_match.group(1))
        if 1 <= n_digit <= count:
            return n_digit - 1
    cardinal_match = _PICK_CARDINAL_RE.search(lowered)
    if cardinal_match is not None:
        raw = cardinal_match.group(1) or cardinal_match.group(2)
        n_card = _SPOKEN_NUMBERS.get(raw)
        if n_card is not None and 1 <= n_card <= count:
            return n_card - 1
    return None


# Sentinel used as `from_state` in the very first state_change event when the
# call begins. Module-level constant so the value lives in one place — the
# frontend reads the spec, not the dispatcher source.
_INIT_STATE_SENTINEL: Final[str] = "(init)"


# HYBRID navigation: maps the LLM-declared ``route_intent`` intent to an FSM
# transition label. The dispatcher still validates the label is a legal edge
# from the current state (``_handle_route_intent``) — the LLM does the NLU,
# the FSM keeps authority over the graph. ``done`` is the explicit hang-up.
_INTENT_TO_LABEL: Final[dict[str, str]] = {
    "book": "wants_book",
    "cancel": "wants_cancel",
    "reschedule": "wants_reschedule",
    "done": "goodbye",
}


def _redact_for_llm(name: str, value: dict[str, Any]) -> str:
    """Compact, UUID-free string the LLM can safely consume.

    The LLM never sees raw slot_ids or appointment_ids (audit A3 — they
    must not be read aloud and must not leak via the model's next-turn
    arguments). Instead each list is enumerated with bracketed handles
    ``[1]``, ``[2]`` … that the model passes back as ``slot_id`` /
    ``appointment_id``; the dispatcher's `_resolve_memory_handles`
    swaps the handle for the real UUID before any HTTP call.
    """
    if name == "list_availability_slots":
        slots = value.get("slots", [])
        next_day = value.get("next_day_with_slots")
        asked_date = value.get("asked_date", "that date")
        if not slots and next_day:
            nd_total = next_day.get("total_returned", len(next_day["slots"]))
            return (
                f"no slots on {asked_date}; next available is "
                f"{next_day['date']} ({nd_total} free) — slots: "
                + "; ".join(
                    f"[{i + 1}] {s['start_at_iso']} with {s['provider_name']}"
                    for i, s in enumerate(next_day["slots"][:6])
                )
            )
        if not slots:
            return f"no slots available for {asked_date} or the next 6 days"
        # Lead with the total so the model applies the adaptive rule: many →
        # invert (ask preference, don't list); few → read 2-3. We still show
        # the first 6 as concrete handles regardless.
        total = value.get("total_returned", len(slots))
        listed = "; ".join(
            f"[{i + 1}] {s['start_at_iso']} with {s['provider_name']}"
            for i, s in enumerate(slots[:6])
        )
        return f"{total} slots available on {asked_date} (showing first {min(total, 6)}): {listed}"
    if name == "get_upcoming_appointments":
        appts = value.get("appointments", [])
        if not appts:
            return "no upcoming appointments"
        return "upcoming: " + "; ".join(
            f"[{i + 1}] {a['start_at']} with {a['provider_name']}" for i, a in enumerate(appts)
        )
    if name in ("find_patient_by_phone", "find_patient_by_name_dob"):
        patients = value.get("patients", [])
        if not patients:
            return "no patient found"
        if len(patients) > 1:
            # Numbered, UUID-free candidate list so the LLM can read them
            # back and the caller can pick by number / name.
            return build_disambiguation_message(patients)
        p = patients[0]
        return f"matched patient: {p['first_name']} {p['last_name']} (DOB {p['dob']})"
    if name == "create_patient":
        return (
            f"patient registered: {value['first_name']} {value['last_name']} "
            f"(phone {value['phone']})"
        )
    if name == "create_appointment":
        return f"booked {value['start_at']} with {value['provider_name']}"
    if name == "cancel_appointment":
        return "appointment cancelled"
    if name == "reschedule_appointment":
        return f"rescheduled to {value['start_at']} with {value['provider_name']}"
    if name == "suggest_specialty":
        # Render the triage recommendation as a single readable line so the
        # main LLM can act on it next turn. ``follow_up`` (if present) is
        # what the main LLM should ask the caller verbatim before
        # calling suggest_specialty again. Confidence is exposed so the
        # main LLM can decide to ask the follow-up vs commit.
        specialty = value.get("specialty", "?")
        duration = value.get("duration_minutes", 30)
        confidence = value.get("confidence", 0.0)
        follow_up = value.get("follow_up")
        base = (
            f"triage: specialty={specialty}, duration_minutes={duration}, "
            f"confidence={confidence:.2f}"
        )
        if follow_up:
            return base + f"; ask the caller: {follow_up!r}"
        return base
    return "ok"


class Dispatcher:
    def __init__(
        self,
        *,
        llm: LLMClientProtocol,
        ehr_client: EHRClient,
        session_id: str | None = None,
        bus: ConsoleBus | None = None,
    ) -> None:
        """Build a dispatcher.

        Args:
            llm: LLM client implementing `LLMClientProtocol`.
            ehr_client: EHR HTTP client.
            session_id: Stable session id; auto-generated UUID if omitted.
                Constrained to ``[A-Za-z0-9_-]+`` by the operator console's
                ``check_session_id`` — UUIDv4's hex+dash shape passes.
            bus: Optional operator-console event bus. When supplied, the
                dispatcher publishes a typed `ConsoleEvent` at each of the
                8 hook points documented in
                ``docs/superpowers/specs/2026-05-20-operator-console-design.md``
                §3.3. When ``None`` (tests, eval runner), every publish
                site is a no-op — the dispatcher behaves exactly as before.
        """
        self._llm = llm
        self._ehr = ehr_client
        self.state: State = State.GREETING
        self.memory = SessionMemory()
        self.history: list[dict[str, Any]] = []
        self.transcript: list[dict[str, Any]] = []
        self.timing = TimingCollector()
        self.cached_prompt_tokens_total: int = 0
        self.prompt_tokens_total: int = 0
        # Observability: one UUID per call, plus auto-increment per user turn.
        # Threaded into every span log + every X-Request-Id header so a
        # reviewer can grep both the bot stderr and the EHR access log for
        # the same id when an error fires.
        self.session_id: str = session_id or str(uuid.uuid4())
        self.turn_id: int = 0
        # Monotonic per-session counter for console tool-call correlation.
        # The LLM-provided ``ToolCall.id`` is the right key for OpenAI's
        # tool_call_id wiring, but mock/canned LLMs reuse a stub id
        # ("call_stub") so it is NOT unique per call. The operator console
        # keys its tool-row DOM nodes on ``call_id``; a repeated id makes
        # the end-event update the wrong row (rows stick on "running").
        # This counter gives every tool execution a console-unique id.
        self._tool_event_seq: int = 0
        # Propagate the session id into the EHR client so its X-Request-Id
        # header includes it on every httpx call.
        self._ehr.set_session_id(self.session_id)
        self._bus: ConsoleBus | None = bus
        # Track which session ids we already warned about for bus.publish
        # failures so that we don't spam the logs on every event. The bus
        # itself dedups overflow warnings; this set is the safety net for
        # any unexpected publish-side exception that bypasses the bus.
        self._bus_publish_warned: set[str] = set()
        # Strong reference to in-flight publish tasks. Without this, an
        # asyncio implementation detail can GC the task before completion
        # and emit "Task was destroyed but it is pending" warnings.
        self._inflight_publishes: set[asyncio.Task[None]] = set()

    def _publish(self, type_: EventType, payload: dict[str, Any]) -> None:
        """Fire-and-forget publish of a `ConsoleEvent` to the operator bus.

        No-op when no bus was injected (tests / eval runner). Catches any
        per-event validation failure rather than crashing the call path —
        operator-console telemetry must never break the voice agent.
        The bus already enforces PII redaction on `_masked` fields, so a
        forgotten `mask_name` call here surfaces as a logged ValueError.
        """
        if self._bus is None:
            return
        try:
            event = make_event(type_, session_id=self.session_id, payload=payload)
        except ValueError:
            if self.session_id not in self._bus_publish_warned:
                self._bus_publish_warned.add(self.session_id)
                # Stash in transcript so an eval reviewer can grep for it
                # without scraping logs; bus drop messages are also logged
                # at WARNING level from `ConsoleBus._enqueue_with_drop`.
                self.transcript.append(
                    {"kind": "console_publish_failed", "type": type_, "state": self.state.value}
                )
            return
        # Fire-and-forget: the bus is fully non-blocking (bounded queues
        # with overflow-drop semantics), so we don't await the publish.
        # We keep a reference to the task on `self._inflight_publishes`
        # so the garbage collector cannot finalise it mid-await, which
        # would otherwise log "Task was destroyed but it is pending".
        # `get_running_loop` is the correct call inside an async frame
        # (Python 3.10+); `get_event_loop` is deprecated for this use
        # and can silently return a different loop under embedded
        # runtimes like Pipecat. If somehow called outside an async
        # frame, fall back to a silent no-op rather than crashing the
        # call path — telemetry must never break the voice agent.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — not callable in production paths, but a
            # defensive fallback for someone wiring a sync test harness.
            return
        task = loop.create_task(self._bus.publish(event))
        self._inflight_publishes.add(task)
        task.add_done_callback(self._inflight_publishes.discard)

    async def start(self) -> str:
        """Run the GREETING state's opening turn (no user input yet)."""
        # Publish the implicit initial state so the operator console can
        # render the GREETING badge from the very first connection.
        self._publish(
            "state_change",
            {
                "from_state": _INIT_STATE_SENTINEL,
                "to_state": self.state.value,
                "trigger": "start",
            },
        )
        return await self._llm_turn()

    async def handle_user_turn(self, user_text: str) -> str:
        """Process one user utterance; return the bot's spoken reply.

        Publishes a `transcript_turn` event before transition + LLM turn
        so the operator console renders the caller's words even on calls
        that crash mid-handling. PII in `user_text` is the operator's
        responsibility — clinic policy may want it redacted in production,
        but during the demo the operator legitimately needs the raw text.
        """
        self.turn_id += 1
        # Push the per-turn request-id prefix into the EHR client so the
        # X-Request-Id header on every tool's HTTP call is greppable
        # alongside the dispatcher's span logs.
        self._ehr.set_turn_id(self.turn_id)
        self.history.append({"role": "user", "content": user_text})
        self.transcript.append({"kind": "user", "text": user_text})
        self._publish(
            "transcript_turn",
            {"role": "user", "text": user_text, "turn_id": self.turn_id},
        )
        self._maybe_transition_from_user_text(user_text)
        return await self._llm_turn()

    def mark_last_assistant_interrupted(self, spoken_text: str) -> None:
        """Truncate the last assistant history entry to the audible portion
        and append ``[INTERRUPTED by user]`` so the next LLM call sees an
        honest timeline.

        Called by ``observers.TTSAudibleObserver`` when a ``StartInterruptionFrame``
        propagates downstream. ``spoken_text`` is what TTS received to
        synthesize before the interrupt — a best-effort estimate of what
        the caller actually heard (see Pipecat issue #4466 for the gap
        between "received" and "rendered to audio").
        """
        if not self.history:
            return
        last = self.history[-1]
        if last.get("role") != "assistant":
            return
        content = str(last.get("content") or "")
        marker = " [INTERRUPTED by user]"
        if marker in content:
            return
        truncated = spoken_text.strip()
        last["content"] = f"{truncated}…{marker}" if truncated else f"[NOT HEARD]{marker}"
        self._publish(
            "turn_interrupted",
            {
                "turn_id": self.turn_id,
                "spoken_text": truncated,
                "state": self.state.value,
            },
        )

    async def _llm_turn(self) -> str:
        tools = [TOOL_SCHEMAS[name] for name in sorted(ALLOWED_TOOLS[self.state])]
        msgs = self._messages_for_llm()
        reply = LLMReply(text="")
        # Per-turn dedupe: a misbehaving model occasionally fires the same tool
        # with identical args 3-5x in one turn (seen with list_availability_slots
        # when results are empty). Allowing one retry is fine — beyond that we
        # short-circuit with a synthetic tool response telling the LLM the call
        # was suppressed, which steers it back to speaking to the user.
        tool_call_signatures: dict[str, int] = {}
        for _ in range(4):
            llm_started = time.perf_counter()
            async with self.timing.measure(
                phase="llm",
                state=self.state.value,
                session_id=self.session_id,
                turn_id=self.turn_id,
            ):
                reply = await self._llm.generate(state=self.state.value, history=msgs, tools=tools)
            llm_duration_ms = (time.perf_counter() - llm_started) * 1000.0
            self._publish("latency_tick", {"phase": "llm", "duration_ms": llm_duration_ms})
            if reply.usage is not None:
                self.cached_prompt_tokens_total += reply.usage.cached_prompt_tokens
                self.prompt_tokens_total += reply.usage.prompt_tokens
            self.transcript.append(
                {
                    "kind": "assistant",
                    "state": self.state.value,
                    "text": reply.text,
                    "cached_prompt_tokens": (
                        reply.usage.cached_prompt_tokens if reply.usage else 0
                    ),
                    "prompt_tokens": (reply.usage.prompt_tokens if reply.usage else 0),
                }
            )
            if reply.text:
                self._publish(
                    "transcript_turn",
                    {"role": "bot", "text": reply.text, "turn_id": self.turn_id},
                )
            # OpenAI tool-call schema is strict: if the assistant emits
            # tool_calls, the message MUST carry the tool_calls field, and
            # every subsequent role:tool response MUST reference the same
            # id via tool_call_id. Build the assistant message accordingly.
            assistant_msg: dict[str, Any] = {"role": "assistant", "content": reply.text or ""}
            if reply.tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in reply.tool_calls
                ]
            self.history.append(assistant_msg)

            if not reply.tool_calls:
                # CHOOSE_INTENT has no tools, so the LLM's free-text reply
                # is the only intent signal we get. Catch acknowledgements
                # ("sure, let's find a time" / "okay, let me pull up your
                # appointments") that the user-text regex missed and route
                # to the right state — otherwise the bot ad-libs a full
                # booking from CHOOSE_INTENT without ever calling tools.
                if self.state is State.CHOOSE_INTENT and reply.text:
                    self._maybe_transition_from_bot_text(reply.text)
                break

            for call in reply.tool_calls:
                if call.name not in ALLOWED_TOOLS[self.state]:
                    self.transcript.append(
                        {"kind": "tool_rejected", "name": call.name, "state": self.state.value}
                    )
                    # Reject still satisfies the tool_call → tool_response
                    # invariant: every tool_call id MUST be answered by a
                    # role:tool message with matching tool_call_id, or OpenAI
                    # rejects the next turn with a 400.
                    self.history.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": (
                                f"ERROR: tool '{call.name}' is not available in "
                                f"state {self.state.value}; ask the user instead."
                            ),
                        }
                    )
                    continue
                signature = json.dumps(
                    {"name": call.name, "args": call.arguments},
                    sort_keys=True,
                    default=str,
                )
                tool_call_signatures[signature] = tool_call_signatures.get(signature, 0) + 1
                if tool_call_signatures[signature] > 2:
                    self.transcript.append({"kind": "tool_repeated_blocked", "name": call.name})
                    self.history.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": (
                                f"BLOCKED: '{call.name}' already returned the same "
                                "result twice this turn. Do NOT call it again — "
                                "respond to the user with what you know (e.g. that "
                                "date is unavailable, ask for a different one)."
                            ),
                        }
                    )
                    continue
                # HYBRID navigation: route_intent is whitelisted but has no EHR
                # handler — the dispatcher applies the transition itself after
                # validating it against the FSM (LLM proposes, dispatcher
                # disposes). Intercept here so it never reaches HANDLERS.
                if call.name == ROUTE_INTENT_TOOL:
                    result = self._handle_route_intent(call)
                else:
                    result = await self._execute_tool(call)
                self._record_tool_result(call.name, result, tool_call_id=call.id)
                self._maybe_transition_from_tool(call.name, result)

            msgs = self._messages_for_llm()
            tools = [TOOL_SCHEMAS[name] for name in sorted(ALLOWED_TOOLS[self.state])]
            if self.state is State.END:
                # The reply that called the just-executed tool was generated
                # BEFORE its result existed — at END its text can wrongly
                # claim a write failed on a success (seen live: "there was an
                # issue with the booking" after a 201). Generate ONE final
                # confirmation turn (END exposes no tools) so the model speaks
                # from the recorded Ok result. Fall back to the prior text if
                # the model returns nothing (e.g. a mock script with no
                # trailing line).
                async with self.timing.measure(
                    phase="llm",
                    state=self.state.value,
                    session_id=self.session_id,
                    turn_id=self.turn_id,
                ):
                    final = await self._llm.generate(state=self.state.value, history=msgs, tools=[])
                if final.text:
                    self.history.append({"role": "assistant", "content": final.text})
                    self.transcript.append(
                        {"kind": "assistant", "state": self.state.value, "text": final.text}
                    )
                    self._publish(
                        "transcript_turn",
                        {"role": "bot", "text": final.text, "turn_id": self.turn_id},
                    )
                    reply = final
                break
        else:
            # Loop exhausted (4 iterations) without ever hitting
            # `not reply.tool_calls` and without reaching END. Typically a
            # misbehaving model stuck in a tool-call loop. If the final reply
            # has no spoken text, the caller would hear silence and the call
            # would stall. Inject a graceful recovery line so the next user
            # turn can drive the conversation forward, and record the event
            # in the transcript so eval reviewers can spot it.
            if not reply.text:
                self.transcript.append({"kind": "llm_loop_exhausted", "state": self.state.value})
                reply = LLMReply(text=FALLBACK_LINES["llm_loop_exhausted"])
        return reply.text

    async def _execute_tool(self, call: ToolCall) -> Result[dict[str, Any]]:
        """Run one tool handler under a timing span; publish start + end events.

        Emits `tool_call_start` before the handler runs and
        `tool_call_end` after, with the wall-clock duration. The redacted
        args we publish contain only structural metadata (the keys, plus
        whitelist of safe values) — the raw `args` may carry a phone
        number or DOB the LLM is feeding us.
        """
        handler = HANDLERS[call.name]
        # Console-unique correlation id for this tool execution. Distinct
        # from ``call.id`` (which feeds OpenAI's tool_call_id and may be a
        # reused stub under mock LLMs). Paired across this call's
        # tool_call_start / tool_call_end so the console matches rows.
        event_call_id = f"t{self.turn_id}-{self._tool_event_seq}"
        self._tool_event_seq += 1
        args = dict(call.arguments)
        # Test convenience: __use_first_slot__ pulls the slot id we just listed.
        if args.pop("__use_first_slot__", False) and self.memory.last_slots:
            args["slot_id"] = self.memory.last_slots[0]["slot_id"]
            args["patient_id"] = (self.memory.identified_patient or {}).get("id") or args.get(
                "patient_id"
            )
        # Resolve bracketed handles the LLM passes (`slot_id="1"`,
        # `appointment_id="[2]"`) back to the real UUID held in
        # SessionMemory. This is the production path — the LLM only ever
        # sees `[N]` handles (audit A3 keeps UUIDs out of the model).
        self._resolve_memory_handles(call.name, args)
        # Defensive: drop any kwargs the handler doesn't accept. An LLM occasionally
        # invents extra fields (`mystery_field=42`); without this filter the
        # `handler(**args)` raises TypeError and crashes the whole turn.
        args = self._filter_handler_kwargs(handler, args)
        # Audit A4: prevent the LLM from booking/cancelling against a hallucinated
        # id that didn't come from a tool result this session. Cheap defence-in-depth
        # on top of the EHR's own validation.
        guard_err = self._validate_against_memory(call.name, args)
        if guard_err is not None:
            self._publish(
                "tool_call_start",
                {
                    "tool": call.name,
                    "args_redacted": _redact_tool_args(call.name, args),
                    "call_id": event_call_id,
                },
            )
            self._publish(
                "tool_call_end",
                {
                    "tool": call.name,
                    "call_id": event_call_id,
                    "outcome": "err",
                    "code": guard_err.code,
                    "duration_ms": 0.0,
                },
            )
            return guard_err
        self._publish(
            "tool_call_start",
            {
                "tool": call.name,
                "args_redacted": _redact_tool_args(call.name, args),
                "call_id": event_call_id,
            },
        )

        started = time.perf_counter()
        async with self.timing.measure(
            phase=f"tool:{call.name}",
            state=self.state.value,
            session_id=self.session_id,
            turn_id=self.turn_id,
        ):
            result = await handler(self._ehr, **args)
        duration_ms = (time.perf_counter() - started) * 1000.0
        if is_ok(result):
            outcome_label = "ok"
            err_code: str | None = None
        else:
            assert is_err(result)  # noqa: S101 — TypeGuard narrowing for mypy
            outcome_label = "err"
            err_code = result.code
        self._publish(
            "tool_call_end",
            {
                "tool": call.name,
                "call_id": event_call_id,
                "outcome": outcome_label,
                "code": err_code,
                "duration_ms": duration_ms,
            },
        )
        self._publish("latency_tick", {"phase": f"tool:{call.name}", "duration_ms": duration_ms})
        return result

    def _handle_route_intent(self, call: ToolCall) -> Result[dict[str, Any]]:
        """Apply an LLM-proposed navigation intent, validated against the FSM.

        The HYBRID navigation path (ADR/council 2026-05-24): the LLM declares
        the caller's intent via ``route_intent`` and the dispatcher maps it to
        a transition label, applying it ONLY if the edge is legal from the
        current state. This moves the NLU (which the regex did badly — audit
        F-006/F-012/F-013) to the LLM while the dispatcher keeps authority over
        the state graph. An unknown intent or an illegal edge returns an ``Err``
        fed back to the LLM so it re-asks instead of the bot stalling silently.
        Never touches the EHR — it is intercepted before ``_execute_tool``.
        """
        intent = str(call.arguments.get("intent") or "").strip().lower()
        label = _INTENT_TO_LABEL.get(intent)
        if label is None:
            return Err(
                code="unknown_intent",
                message=(
                    f"intent {intent!r} is not one of {sorted(_INTENT_TO_LABEL)}; "
                    "ask the caller to clarify"
                ),
                retryable=True,
            )
        if label not in TRANSITIONS.get(self.state, {}):
            return Err(
                code="illegal_transition",
                message=(
                    f"cannot route to {intent!r} from {self.state.value}; "
                    "ask the caller what they need"
                ),
                retryable=True,
            )
        self.transcript.append(
            {
                "kind": "route_intent",
                "intent": intent,
                "label": label,
                "from": self.state.value,
            }
        )
        self._transition(label)
        return Ok(value={"routed_to": intent})

    @staticmethod
    def _filter_handler_kwargs(handler: Any, args: dict[str, Any]) -> dict[str, Any]:
        sig = inspect.signature(handler)
        params = sig.parameters
        # If the handler explicitly accepts **kwargs, no filtering needed.
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return args
        allowed = {
            name
            for name, p in params.items()
            if p.kind in (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        }
        return {k: v for k, v in args.items() if k in allowed}

    def _resolve_memory_handles(self, name: str, args: dict[str, Any]) -> None:
        """Swap LLM-supplied handles (``"1"``, ``"[2]"``) for the real UUIDs.

        The LLM only sees bracketed handles in `_redact_for_llm` output
        (audit A3 — no UUIDs leak to the model). It must pass those same
        handles back in `create_appointment.slot_id` and
        `cancel_appointment.appointment_id`; we map them to the real ids
        held in ``SessionMemory`` before validation / HTTP.
        """
        if name == "create_appointment":
            idx = _maybe_handle_index(args.get("slot_id"))
            if idx is not None and 0 <= idx < len(self.memory.last_slots):
                args["slot_id"] = self.memory.last_slots[idx]["slot_id"]
            # The LLM never has the real patient UUID (audit A3 redacts it),
            # so any patient_id it passes is a name/handle, not a valid id.
            # ALWAYS override with the identified caller's real id — not just
            # when omitted. Filling-only-when-absent let a name-as-patient_id
            # ("Ada Lovelace") slip through to the EHR. This also enforces
            # "book for the caller only" (cross-patient writes are forbidden).
            identified = self.memory.identified_patient or {}
            if identified.get("id"):
                args["patient_id"] = identified["id"]
        elif name == "cancel_appointment":
            idx = _maybe_handle_index(args.get("appointment_id"))
            if idx is not None and 0 <= idx < len(self.memory.last_upcoming_appointments):
                args["appointment_id"] = self.memory.last_upcoming_appointments[idx]["id"]
        elif name == "reschedule_appointment":
            idx_appt = _maybe_handle_index(args.get("appointment_id"))
            if idx_appt is not None and 0 <= idx_appt < len(self.memory.last_upcoming_appointments):
                args["appointment_id"] = self.memory.last_upcoming_appointments[idx_appt]["id"]
            idx_slot = _maybe_handle_index(args.get("slot_id"))
            if idx_slot is not None and 0 <= idx_slot < len(self.memory.last_slots):
                args["slot_id"] = self.memory.last_slots[idx_slot]["slot_id"]
        elif name == "get_upcoming_appointments":
            # Same as create_appointment: the LLM only ever has the patient's
            # name (UUID is redacted), so it passes the name as patient_id and
            # the EHR 404s with patient_not_found — which silently breaks the
            # entire cancel/reschedule flow (the appointment list never loads,
            # so the FSM never reaches CONFIRM_CANCEL / CONFIRM_RESCHEDULE and
            # cancel_appointment is never even mounted). Always override.
            identified = self.memory.identified_patient or {}
            if identified.get("id"):
                args["patient_id"] = identified["id"]

    def _validate_against_memory(self, name: str, args: dict[str, Any]) -> Err | None:
        if name == "create_appointment":
            slot_id = args.get("slot_id")
            # If LLM forgets either id entirely, return a structured Err so it
            # retries with the right shape instead of crashing on TypeError
            # at the handler boundary.
            if not slot_id:
                return Err(
                    code="missing_slot_id",
                    message=(
                        "create_appointment requires slot_id; call list_availability_slots first"
                    ),
                    retryable=True,
                )
            if not args.get("patient_id"):
                return Err(
                    code="missing_patient_id",
                    message="create_appointment requires patient_id of the identified caller",
                    retryable=True,
                )
            known_slots = {s["slot_id"] for s in self.memory.last_slots}
            if slot_id not in known_slots:
                return Err(
                    code="hallucinated_slot_id",
                    message=(
                        f"slot_id {slot_id!r} not in last_slots — likely "
                        f"hallucinated; call list_availability_slots first"
                    ),
                    retryable=True,
                )
            patient_id = args.get("patient_id")
            identified = self.memory.identified_patient or {}
            known_patient = identified.get("id")
            if patient_id is not None and known_patient and patient_id != known_patient:
                return Err(
                    code="patient_id_mismatch",
                    message=(f"patient_id {patient_id!r} != identified patient {known_patient!r}"),
                    retryable=False,
                )
        elif name == "cancel_appointment":
            appt_id = args.get("appointment_id")
            if not appt_id:
                return Err(
                    code="missing_appointment_id",
                    message=(
                        "cancel_appointment requires appointment_id; "
                        "call get_upcoming_appointments first"
                    ),
                    retryable=True,
                )
            known = {a["id"] for a in self.memory.last_upcoming_appointments}
            if appt_id not in known:
                return Err(
                    code="hallucinated_appointment_id",
                    message=(
                        f"appointment_id {appt_id!r} not in "
                        f"last_upcoming_appointments — call "
                        f"get_upcoming_appointments first"
                    ),
                    retryable=True,
                )
        elif name == "reschedule_appointment":
            appt_id = args.get("appointment_id")
            slot_id = args.get("slot_id")
            if not appt_id:
                return Err(
                    code="missing_appointment_id",
                    message=(
                        "reschedule_appointment requires appointment_id; "
                        "call get_upcoming_appointments first"
                    ),
                    retryable=True,
                )
            if not slot_id:
                return Err(
                    code="missing_slot_id",
                    message=(
                        "reschedule_appointment requires slot_id; "
                        "call list_availability_slots first"
                    ),
                    retryable=True,
                )
            known_appts = {a["id"] for a in self.memory.last_upcoming_appointments}
            if appt_id not in known_appts:
                return Err(
                    code="hallucinated_appointment_id",
                    message=(
                        f"appointment_id {appt_id!r} not in "
                        f"last_upcoming_appointments — call "
                        f"get_upcoming_appointments first"
                    ),
                    retryable=True,
                )
            known_slots = {s["slot_id"] for s in self.memory.last_slots}
            if slot_id not in known_slots:
                return Err(
                    code="hallucinated_slot_id",
                    message=(
                        f"slot_id {slot_id!r} not in last_slots — call "
                        f"list_availability_slots first"
                    ),
                    retryable=True,
                )
        return None

    def _record_tool_result(
        self,
        name: str,
        result: Result[dict[str, Any]],
        *,
        tool_call_id: str = "call_stub",
    ) -> None:
        if is_ok(result):
            self.transcript.append({"kind": "tool_ok", "name": name, "value": result.value})
            # Audit A3: redact raw UUIDs from the string the LLM sees. The
            # transcript and memory keep the originals for our own logic,
            # but the LLM only gets a human-readable summary so it can't
            # accidentally read a slot_id or appointment_id aloud.
            # OpenAI schema: role:tool requires tool_call_id pointing at the
            # preceding assistant message's tool_calls[].id (NOT a `name`).
            self.history.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": _redact_for_llm(name, result.value),
                }
            )
            if name == "list_availability_slots":
                # Empty primary date + auto-scan hit → store the fallback day's
                # slots so create_appointment doesn't reject them as
                # hallucinated. The LLM will have read the fallback date aloud
                # and the caller will pick a slot_id from that list.
                fallback = result.value.get("next_day_with_slots")
                primary_slots = result.value.get("slots") or []
                self.memory.last_slots = (
                    primary_slots if primary_slots else (fallback or {}).get("slots", [])
                )
                if self.memory.last_slots:
                    self._publish_slots_offered(self.memory.last_slots)
            elif name == "suggest_specialty":
                # Remember the triage recommendation so list_availability_slots
                # / create_appointment can default to it, and the operator
                # console can show what the caller was routed to.
                self.memory.recommended_specialty = result.value.get("specialty")
                self.memory.recommended_duration_minutes = result.value.get("duration_minutes")
            elif name == "get_upcoming_appointments":
                self.memory.last_upcoming_appointments = result.value["appointments"]
            elif name in ("find_patient_by_phone", "find_patient_by_name_dob"):
                patients = result.value.get("patients", [])
                if len(patients) == 1:
                    self.memory.identified_patient = patients[0]
                    self._publish_patient_identified(patients[0])
            elif name == "create_patient":
                self.memory.identified_patient = {
                    "id": result.value["patient_id"],
                    "first_name": result.value["first_name"],
                    "last_name": result.value["last_name"],
                    "phone": result.value["phone"],
                    # `dob` is in the create_patient response — propagate
                    # so `_publish_patient_identified` reports the real
                    # year, not `0` (which would render as "year 0" in
                    # the operator console).
                    "dob": result.value.get("dob", ""),
                }
                self._publish_patient_identified(self.memory.identified_patient)
        else:
            assert is_err(result)  # noqa: S101 — TypeGuard narrowing for the type checker
            self.transcript.append(
                {
                    "kind": "tool_err",
                    "name": name,
                    "code": result.code,
                    "message": result.message,
                    "retryable": result.retryable,
                }
            )
            self.history.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": f"ERROR code={result.code} message={result.message}",
                }
            )

    def _maybe_transition_from_bot_text(self, bot_text: str) -> None:
        """Fallback transition for CHOOSE_INTENT when the user-text regex missed.

        Scans the bot's own reply for phrases that betray which flow it
        thinks the call is in (e.g. "check availability" → booking,
        "pull up your appointments" → cancellation). Without this the LLM
        will happily fabricate a whole booking conversation while the FSM
        still has zero tools available.
        """
        lower = bot_text.lower()
        # The CHOOSE_INTENT opener almost always offers BOTH options
        # ("book a new visit or cancel an existing one?"). That's a menu,
        # not a commitment — never transition on it. A clear `book ... or
        # ... cancel` (in either order) is the canonical menu shape.
        if re.search(r"\b(book|schedul)\w*\b[^.?!]{0,80}\bor\b[^.?!]{0,80}\bcancel", lower):
            return
        if re.search(r"\bcancel\w*\b[^.?!]{0,80}\bor\b[^.?!]{0,80}\b(book|schedul)", lower):
            return
        book_hints = (
            "availab",
            "what day",
            "what date",
            "what time",
            "let's find a time",
            "let me find a time",
            "find a time",
            "schedule a",
        )
        cancel_hints = (
            "pull up your appointments",
            "find your appointment",
            "which appointment",
            "cancel that",
            "look up your booking",
            "current bookings",
        )
        if any(p in lower for p in cancel_hints):
            self._transition("wants_cancel")
        elif any(p in lower for p in book_hints):
            self._transition("wants_book")

    def _maybe_transition_from_user_text(self, user_text: str) -> None:
        # Universal goodbye intent — hanging up is always legal. Checked
        # FIRST so "bye" at GREETING goes straight to END instead of
        # auto-transitioning into IDENTIFY_PATIENT. Relies on TRANSITIONS
        # exposing a `goodbye` label from every non-terminal state; if a
        # state doesn't, `_transition` is a no-op (safe by construction).
        if _has_goodbye_intent(user_text):
            # F-009: at a CONFIRM_* state an utterance that ALSO affirms
            # ("yes, book it, thanks bye") must let the pending action run
            # first — hanging up here would jump to END (no tools) and the
            # just-confirmed booking/cancel/reschedule would never fire. The
            # affirmation wins; the goodbye is honoured on a later turn.
            at_confirm = self.state in (
                State.CONFIRM_BOOK,
                State.CONFIRM_CANCEL,
                State.CONFIRM_RESCHEDULE,
            )
            if not (at_confirm and _AFFIRM.search(user_text)):
                self._transition("goodbye")
                if self.state is State.END:
                    return
        if self.state is State.GREETING:
            self._transition("go_identify")
            return
        # Disambiguation pending: the caller is choosing between candidates
        # we read back last turn. Resolve their pick before any other intent
        # parsing so "the first one" / "number two" lands on a patient
        # rather than being mistaken for a booking intent.
        if self.state is State.IDENTIFY_PATIENT and self.memory.pending_identity_candidates:
            self._resolve_pending_identity(user_text)
            return
        # Track reschedule intent from ANY state — caller can say "move my
        # appointment" mid-call. The flag triggers the cancel→rebook
        # auto-transition once `cancel_appointment` returns Ok.
        if _RESCHEDULE_INTENT.search(user_text):
            self.memory.wants_reschedule = True
        if self.state is State.CHOOSE_INTENT:
            # Reschedule is a sub-intent of cancel — check it first so
            # "move my appointment" routes to the atomic RESCHEDULE_FLOW
            # rather than falling into CANCEL_FLOW and then chaining
            # cancel-then-book (which can lose the original on failure).
            if _RESCHEDULE_INTENT.search(user_text):
                self.memory.wants_reschedule = False
                self._transition("wants_reschedule")
            # Explicit "cancel" or explicit "book/schedule" verbs are decisive
            # and beat the broad generic verbs (skip/move/remove) that occur in
            # both kinds of phrasing — "let's skip the chit-chat, I want to
            # book" must route to BOOK_FLOW, not CANCEL_FLOW (audit F-006).
            elif _STRONG_CANCEL.search(user_text):
                self._transition("wants_cancel")
            elif _STRONG_BOOK.search(user_text):
                self._transition("wants_book")
            elif _CANCEL_INTENT.search(user_text):
                self._transition("wants_cancel")
            elif _BOOK_INTENT.search(user_text):
                self._transition("wants_book")
        # In a confirm state, a clear "no" / "different time" routes back to
        # the flow state so the LLM can re-offer choices. Without this, the
        # caller hears the bot wait silently because no transition fires and
        # CONFIRM_* has no flow tools left to call.
        elif (
            (self.state is State.CONFIRM_BOOK and _DENY.search(user_text))
            or (self.state is State.CONFIRM_CANCEL and _DENY.search(user_text))
            or (self.state is State.CONFIRM_RESCHEDULE and _DENY.search(user_text))
        ):
            self._transition("abort")

    def _resolve_pending_identity(self, user_text: str) -> None:
        """Resolve a pending name+DOB disambiguation from the caller's pick.

        Maps "the first one" / "number two" / "two" to a stored candidate,
        sets it as the identified patient, clears the pending list, and
        advances to CHOOSE_INTENT. If the pick can't be parsed, leaves the
        candidates in place so the LLM asks again.
        """
        candidates = self.memory.pending_identity_candidates
        idx = _pick_candidate_index(user_text, len(candidates))
        if idx is None:
            return
        chosen = candidates[idx]
        self.memory.identified_patient = chosen
        self.memory.pending_identity_candidates = []
        self._publish_patient_identified(chosen)
        self._transition("patient_found")

    def _maybe_transition_from_tool(self, tool_name: str, result: Result[dict[str, Any]]) -> None:
        # F-011: a triage red flag (suggest_specialty → medical_emergency) is a
        # hard stop. Route to END so the booking tools are physically
        # unmounted; the LLM still sees the Err message instructing the 911
        # redirect, but it can no longer book even if it ignores the guidance.
        if (
            self.state is State.BOOK_FLOW
            and tool_name == "suggest_specialty"
            and is_err(result)
            and result.code == "medical_emergency"
        ):
            self._transition("medical_emergency")
            return
        if self.state is State.IDENTIFY_PATIENT and tool_name in (
            "find_patient_by_phone",
            "find_patient_by_name_dob",
        ):
            if is_ok(result):
                patients = result.value.get("patients", [])
                outcome = classify_find_result(
                    patients, fuzzy=(tool_name == "find_patient_by_name_dob")
                )
                if outcome == "found_fuzzy_multiple":
                    # More than one candidate — do NOT auto-advance under a
                    # guessed identity. Hold the candidates and stay in
                    # IDENTIFY_PATIENT; the LLM reads them back (see the
                    # disambiguation note injected in _record_tool_result)
                    # and the caller's pick resolves it next turn.
                    self.memory.pending_identity_candidates = patients
                elif len(patients) == 1:
                    self._transition("patient_found")
                elif tool_name == "find_patient_by_name_dob" and not patients:
                    self._transition("no_match")
            elif tool_name == "find_patient_by_name_dob":
                # An Err on the name+DOB fallback (e.g. unparseable DOB) means
                # there's no useful identity left to try in this state — route
                # to REGISTER_PATIENT so the LLM can collect the details fresh
                # instead of looping inside IDENTIFY_PATIENT forever.
                self._transition("no_match")
        elif self.state is State.REGISTER_PATIENT and tool_name == "create_patient":
            if is_ok(result):
                self._transition("registered")
        elif self.state is State.BOOK_FLOW and tool_name == "list_availability_slots":
            if is_ok(result):
                # Advance when EITHER the asked date OR the auto-scan
                # fallback returned slots. The dispatcher's last_slots now
                # carries whichever set was populated, so create_appointment
                # in CONFIRM_BOOK can validate slot_id without a
                # hallucinated_slot_id error. Stay in BOOK_FLOW only when
                # both the asked date and the 6-day look-ahead were empty.
                primary = result.value.get("slots") or []
                fallback = (result.value.get("next_day_with_slots") or {}).get("slots") or []
                if primary or fallback:
                    self._transition("slot_chosen")
                else:
                    self.transcript.append({"kind": "empty_slot_result", "state": self.state.value})
        elif self.state is State.RESCHEDULE_FLOW and tool_name == "get_upcoming_appointments":
            # Nothing to move — end gracefully so the caller doesn't get
            # asked to pick from an empty list.
            if is_ok(result) and not result.value["appointments"]:
                self._transition("nothing_to_reschedule")
        elif self.state is State.RESCHEDULE_FLOW and tool_name == "list_availability_slots":
            if is_ok(result):
                primary = result.value.get("slots") or []
                fallback = (result.value.get("next_day_with_slots") or {}).get("slots") or []
                if primary or fallback:
                    # Slot side filled in. Memory still carries the
                    # caller's chosen appointment from the earlier
                    # `get_upcoming_appointments` call, so CONFIRM_RESCHEDULE
                    # can validate both ids.
                    self._transition("slot_chosen")
                else:
                    self.transcript.append({"kind": "empty_slot_result", "state": self.state.value})
        elif self.state is State.CANCEL_FLOW and tool_name == "get_upcoming_appointments":
            if is_ok(result):
                appts = result.value["appointments"]
                if appts:
                    self._transition("appointment_chosen")
                else:
                    self._transition("nothing_to_cancel")
        elif self.state is State.CONFIRM_BOOK and tool_name == "create_appointment":
            if is_ok(result):
                self._transition("booked")
        elif (
            self.state is State.CONFIRM_CANCEL
            and tool_name == "cancel_appointment"
            and is_ok(result)
        ):
            # Reschedule path: caller said "reschedule" / "move" mid-call
            # while we were already in the cancel branch — auto-route into
            # BOOK_FLOW so the same call can produce the new booking. Plain
            # cancel stays single-action. (When the caller said "reschedule"
            # from CHOOSE_INTENT we now take the atomic RESCHEDULE_FLOW
            # path; this chain only fires for mid-call intent flips.)
            if self.memory.wants_reschedule:
                self.memory.wants_reschedule = False
                self._transition("cancelled_then_rebook")
            else:
                self._transition("cancelled")
        elif (
            self.state is State.CONFIRM_RESCHEDULE
            and tool_name == "reschedule_appointment"
            and is_ok(result)
        ):
            self._transition("rescheduled")

    def _transition(self, label: str) -> None:
        """Apply a labelled transition if `label` is valid for the current state.

        Publishes a `state_change` event on every successful transition.
        On reaching ``State.END`` also emits the call's terminal
        `outcome` event so the audit log is self-describing — the
        reviewer never needs to parse the transcript to know how the
        call ended.
        """
        dst = TRANSITIONS[self.state].get(label)
        if dst is None:
            return
        prev_state = self.state.value
        self.transcript.append(
            {"kind": "transition", "from": prev_state, "to": dst.value, "label": label}
        )
        self.state = dst
        self._publish(
            "state_change",
            {"from_state": prev_state, "to_state": dst.value, "trigger": label},
        )
        if dst is State.END:
            self._publish_outcome(label)

    def _publish_outcome(self, trigger_label: str) -> None:
        """Emit the `outcome` event when the call has just reached END.

        Four outcome categories — chosen so a clinic-analytics dashboard
        can answer the four questions that actually matter:

        - ``booked``      — `_transition` fired with label ``booked``.
        - ``cancelled``   — `_transition` fired with label ``cancelled``.
        - ``rescheduled`` — `_transition` fired with label ``rescheduled``
                            (CONFIRM_RESCHEDULE → END). A completed move is
                            a positive outcome, NOT an abandoned call.
        - ``refused``     — the bot reached a confirmation state and the
                            caller declined (the LLM steered us through
                            CONFIRM_BOOK / CONFIRM_CANCEL / CONFIRM_RESCHEDULE
                            without an actual booked/cancelled/rescheduled
                            trigger).
        - ``abandoned``   — everything else: a goodbye that landed before
                            confirmation, even if a registration or
                            lookup tool already ran. Post-registration
                            hangups are NOT refusals — the bot was never
                            rejected, the caller just left.

        The distinction matters: "refused" implies an offer was made and
        declined (a UX or trust signal); "abandoned" is a generic drop.
        Confusing them poisons clinic dashboards.
        """
        if trigger_label == "booked":
            outcome = "booked"
        elif trigger_label == "cancelled":
            outcome = "cancelled"
        elif trigger_label == "rescheduled":
            outcome = "rescheduled"
        else:
            reached_confirm = any(
                t.get("kind") == "transition"
                and t.get("to") in ("CONFIRM_BOOK", "CONFIRM_CANCEL", "CONFIRM_RESCHEDULE")
                for t in self.transcript
            )
            outcome = "refused" if reached_confirm else "abandoned"
        # `trigger` is always an FSM-edge label from `TRANSITIONS`
        # (`booked` / `cancelled` / `goodbye` / etc.) — a finite,
        # hardcoded vocabulary. Never user text. If a future state
        # routes a free-form string into this field, the bus's
        # defence-in-depth check would not catch it (the field name
        # does not end in `_masked`); reviewers should re-audit then.
        details: dict[str, Any] = {
            "trigger": trigger_label,
            "turns": self.turn_id,
        }
        self._publish("outcome", {"outcome": outcome, "details": details})

    # Sliding-window cap on the message history sent to the LLM. Long calls
    # (50+ turns) would otherwise grow the prompt linearly and blow up the
    # token budget + cost + TTFT. Persona + per-state task_message are
    # always sent (they live outside `history`), and SessionMemory carries
    # the structured facts the FSM needs (identified_patient, last_slots,
    # last_upcoming_appointments), so dropping the oldest raw turns is safe:
    # the bot still has the salient state. Cap is generous (40 messages ≈
    # 20 turns) — typical calls finish in <10 turns.
    _HISTORY_WINDOW = 40

    def _messages_for_llm(self) -> list[dict[str, Any]]:
        """Build the per-turn message list the LLM sees (persona + task + window).

        Pruning runs over the full ``self.history`` (not just the window
        slice) so any orphaned ``role:tool`` message — one whose parent
        ``assistant`` carrying its ``tool_call_id`` has been trimmed — is
        removed from dispatcher state permanently. Pruning only the
        window-sliced copy used to leave the orphan in ``self.history``;
        a later turn whose window boundary lifted the orphan to the front
        would then crash with OpenAI's
        ``messages with role tool must be a response to a preceeding
        message with tool_calls`` 400 (ERRORS.md E1).
        """
        self.history = self._prune_orphan_tool_messages(self.history)
        hist = self.history
        if len(hist) > self._HISTORY_WINDOW:
            hist = hist[-self._HISTORY_WINDOW :]
            hist = self._prune_orphan_tool_messages(hist)
        return [
            {"role": "system", "content": CLINIC_PERSONA},
            {"role": "system", "content": build_task_message(self.state.value)},
            *hist,
        ]

    @staticmethod
    def _prune_orphan_tool_messages(
        hist: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Drop any ``role:tool`` message whose ``tool_call_id`` has no preceding
        assistant ``tool_calls`` entry inside the window.

        OpenAI's schema requires every ``role:tool`` message to be a response
        to a preceding ``assistant`` message carrying that ``tool_call_id`` in
        its ``tool_calls`` array. The sliding-window cap in
        ``_messages_for_llm`` can slice off the assistant message while
        leaving its tool reply behind — the reply then becomes the first
        message in the window and OpenAI rejects the turn with::

            messages with role tool must be a response to a preceding
            message with tool_calls

        Pruning here (the call site) — not inside ``_redact_for_llm`` —
        keeps the invariant local to the place that built the broken
        window. We track every ``tool_call_id`` announced by an assistant
        message we've kept; any ``role:tool`` whose id is unknown to that
        set is dropped.
        """
        seen_call_ids: set[str] = set()
        kept: list[dict[str, Any]] = []
        for msg in hist:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    tc_id = tc.get("id")
                    if isinstance(tc_id, str):
                        seen_call_ids.add(tc_id)
                kept.append(msg)
                continue
            if msg.get("role") == "tool":
                if msg.get("tool_call_id") in seen_call_ids:
                    kept.append(msg)
                # else: orphan — drop it silently to keep OpenAI happy.
                continue
            kept.append(msg)
        return kept

    def _publish_patient_identified(self, patient: dict[str, Any]) -> None:
        """Emit `patient_identified` with masked PII fields.

        ``name_masked`` and ``phone_masked`` use the canonical maskers in
        ``observability/redact.py``. ``dob_year`` is the bare year (no
        month/day) — enough for the operator to confirm identity without
        exposing the full DOB. ``id_internal`` is the EHR UUID; the LLM
        never sees it (the dispatcher's `_redact_for_llm` strips ids
        before they reach the model), but the operator legitimately
        needs it to cross-reference with the EHR if something goes
        wrong on the call.
        """
        first = str(patient.get("first_name") or "")
        last = str(patient.get("last_name") or "")
        full_name = f"{first} {last}".strip() or "(unknown)"
        dob = str(patient.get("dob") or "")
        dob_year = 0
        if len(dob) >= 4 and dob[:4].isdigit():
            dob_year = int(dob[:4])
        self._publish(
            "patient_identified",
            {
                "name_masked": mask_name(full_name),
                "dob_year": dob_year,
                "phone_masked": mask_phone(str(patient.get("phone") or "")),
                "id_internal": str(patient.get("id") or "(none)"),
            },
        )

    def _publish_slots_offered(self, slots: list[dict[str, Any]]) -> None:
        """Emit `slots_offered` with provider lastnames + date span.

        We expose only counts and provider names — the slot ids stay
        inside `memory.last_slots`. The clinical pane in the front-end
        renders a list of "date · time · provider"; the dev pane shows
        the count + the first/last date so the operator sees the spread.
        """
        if not slots:
            return
        providers = sorted({str(s.get("provider_name") or "?") for s in slots})
        first_date = str(slots[0].get("start_at_iso") or slots[0].get("start_at") or "")
        last_date = str(slots[-1].get("start_at_iso") or slots[-1].get("start_at") or "")
        payload: dict[str, Any] = {
            "count": len(slots),
            "providers": providers,
            "first_date": first_date,
            "last_date": last_date,
        }
        # Surface the triage recommendation alongside availability so the
        # operator/receptionist sees WHY this caller is routed to a given
        # specialty + visit length (the handoff context a doctor needs).
        if self.memory.recommended_specialty:
            payload["recommended_specialty"] = self.memory.recommended_specialty
        if self.memory.recommended_duration_minutes:
            payload["recommended_duration_minutes"] = self.memory.recommended_duration_minutes
        self._publish("slots_offered", payload)


def _redact_tool_args(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Strip PII from tool args before publishing them to the bus.

    The operator console's audit log is durable; raw args here would
    persist phone numbers and DOBs to disk. We retain structure (keys)
    so a reviewer can see *what* the tool was called with, but obscure
    values that are PII. For non-PII tools, args pass through unchanged.
    """
    if tool_name == "find_patient_by_phone":
        return {"phone": mask_phone(str(args.get("phone") or ""))}
    if tool_name == "find_patient_by_name_dob":
        return {
            "name": mask_name(str(args.get("name") or "")),
            "dob": "[DOB]" if args.get("dob") else "(none)",
        }
    if tool_name == "create_patient":
        return {
            "first_name": mask_name(str(args.get("first_name") or "")),
            "last_name": mask_name(str(args.get("last_name") or "")),
            "phone": mask_phone(str(args.get("phone") or "")),
            "dob": "[DOB]" if args.get("dob") else "(none)",
        }
    if tool_name == "cancel_appointment":
        # `reason` is free-form text supplied by the caller; it may carry
        # PHI ("cancel because of my chemo appointment"). The UUID is
        # itself safe — the LLM never sees it — but we still suppress the
        # reason from the operator-facing event payload. The transcript
        # (clinician-only) keeps the unredacted version for follow-up.
        out: dict[str, Any] = {}
        if "appointment_id" in args:
            out["appointment_id"] = args["appointment_id"]
        if "reason" in args:
            out["reason"] = "[REASON]" if args["reason"] else "(none)"
        return out
    if tool_name == "create_appointment":
        # Similarly, `notes` may carry PHI. UUIDs pass through.
        out_ca: dict[str, Any] = {}
        for safe_key in ("slot_id", "patient_id"):
            if safe_key in args:
                out_ca[safe_key] = args[safe_key]
        if "notes" in args:
            out_ca["notes"] = "[NOTES]" if args["notes"] else "(none)"
        return out_ca
    if tool_name == "reschedule_appointment":
        # Both ids are EHR UUIDs (no PII). Pass through.
        return {k: args[k] for k in ("appointment_id", "slot_id") if k in args}
    if tool_name == "suggest_specialty":
        # `symptoms` is free-form caller speech and is PHI by definition
        # ("I've had chest pain for two days"). The durable operator-console
        # audit log must not persist it; the clinician-only transcript keeps
        # the unredacted version for follow-up.
        return {"symptoms": "[SYMPTOMS]" if args.get("symptoms") else "(none)"}
    # Remaining tools (list_availability_slots, get_upcoming_appointments,
    # etc.) take only safe scalar args (date, provider_id) — pass through.
    return dict(args)
