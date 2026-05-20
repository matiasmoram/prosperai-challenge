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

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from prosper.ehr_client import EHRClient
from prosper.flows import ALLOWED_TOOLS, TRANSITIONS, State
from prosper.observability.timing import TimingCollector
from prosper.prompts import CLINIC_PERSONA, TASK_MESSAGES
from prosper.result import Err, Result, is_err, is_ok
from prosper.tools import HANDLERS, TOOL_SCHEMAS


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMReply:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: LLMUsage | None = None


class LLMClientProtocol(Protocol):
    async def generate(self, *, state: str, history: list[dict], tools: list[dict]) -> LLMReply: ...


@dataclass
class LLMUsage:
    """Optional usage metrics reported by the LLM provider."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_prompt_tokens: int = 0


@dataclass
class SessionMemory:
    """Cross-state data the LLM accumulates within a call."""

    identified_patient: dict | None = None
    last_slots: list[dict] = field(default_factory=list)
    last_upcoming_appointments: list[dict] = field(default_factory=list)


_AFFIRM = re.compile(
    r"\b(yes|yeah|yep|yup|sure|correct|that'?s right|please do|go ahead|sounds good)\b", re.I
)
_DENY = re.compile(r"\b(no|nope|nah|cancel that|stop|wrong)\b", re.I)
_CANCEL_INTENT = re.compile(r"\b(cancel|reschedule|move)\b", re.I)
_BOOK_INTENT = re.compile(r"\b(book|schedule|new appointment|new visit|set up)\b", re.I)


def _redact_for_llm(name: str, value: dict[str, Any]) -> str:
    """Compact, UUID-free string the LLM can safely consume.

    The LLM doesn't need to see slot_ids or appointment_ids — it should never
    read them aloud and never include them in next-turn arguments (the
    dispatcher carries them in `memory`). We feed the LLM a human-readable
    summary that mentions date/time/provider/name only.
    """
    if name == "list_availability_slots":
        slots = value.get("slots", [])
        if not slots:
            return "no slots available for that date"
        return "available slots: " + "; ".join(
            f"{s['start_at_iso']} with {s['provider_name']}" for s in slots[:6]
        )
    if name == "get_upcoming_appointments":
        appts = value.get("appointments", [])
        if not appts:
            return "no upcoming appointments"
        return "upcoming: " + "; ".join(
            f"#{i + 1} {a['start_at']} with {a['provider_name']}"
            for i, a in enumerate(appts)
        )
    if name in ("find_patient_by_phone", "find_patient_by_name_dob"):
        patients = value.get("patients", [])
        if not patients:
            return "no patient found"
        return "matched patients: " + "; ".join(
            f"{p['first_name']} {p['last_name']} (DOB {p['dob']})"
            for p in patients[:3]
        )
    if name == "create_patient":
        return (
            f"patient registered: {value['first_name']} {value['last_name']} "
            f"(phone {value['phone']})"
        )
    if name == "create_appointment":
        return (
            f"booked {value['start_at']} with {value['provider_name']}"
        )
    if name == "cancel_appointment":
        return "appointment cancelled"
    return "ok"


class Dispatcher:
    def __init__(self, *, llm: LLMClientProtocol, ehr_client: EHRClient) -> None:
        self._llm = llm
        self._ehr = ehr_client
        self.state: State = State.GREETING
        self.memory = SessionMemory()
        self.history: list[dict] = []
        self.transcript: list[dict] = []
        self.timing = TimingCollector()
        self.cached_prompt_tokens_total: int = 0
        self.prompt_tokens_total: int = 0
        self._user_turn_start_ts: float | None = None  # for TTFT in DispatcherProcessor

    async def start(self) -> str:
        """Run the GREETING state's opening turn (no user input yet)."""
        return await self._llm_turn()

    async def handle_user_turn(self, user_text: str) -> str:
        self.history.append({"role": "user", "content": user_text})
        self.transcript.append({"kind": "user", "text": user_text})
        self._maybe_transition_from_user_text(user_text)
        return await self._llm_turn()

    async def _llm_turn(self) -> str:
        tools = [TOOL_SCHEMAS[name] for name in sorted(ALLOWED_TOOLS[self.state])]
        msgs = self._messages_for_llm()
        reply = LLMReply(text="")
        for _ in range(4):
            async with self.timing.measure(phase="llm", state=self.state.value):
                reply = await self._llm.generate(state=self.state.value, history=msgs, tools=tools)
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
            self.history.append({"role": "assistant", "content": reply.text})

            if not reply.tool_calls:
                break

            for call in reply.tool_calls:
                if call.name not in ALLOWED_TOOLS[self.state]:
                    self.transcript.append(
                        {"kind": "tool_rejected", "name": call.name, "state": self.state.value}
                    )
                    self.history.append(
                        {
                            "role": "system",
                            "content": (
                                f"Tool '{call.name}' is not available in state {self.state.value}."
                            ),
                        }
                    )
                    continue
                result = await self._execute_tool(call)
                self._record_tool_result(call.name, result)
                self._maybe_transition_from_tool(call.name, result)

            msgs = self._messages_for_llm()
            tools = [TOOL_SCHEMAS[name] for name in sorted(ALLOWED_TOOLS[self.state])]
            if self.state is State.END:
                break
        return reply.text

    async def _execute_tool(self, call: ToolCall) -> Result[dict]:
        handler = HANDLERS[call.name]
        args = dict(call.arguments)
        # Test convenience: __use_first_slot__ pulls the slot id we just listed.
        if args.pop("__use_first_slot__", False) and self.memory.last_slots:
            args["slot_id"] = self.memory.last_slots[0]["slot_id"]
            args["patient_id"] = (self.memory.identified_patient or {}).get("id") or args.get(
                "patient_id"
            )
        # Audit A4: prevent the LLM from booking/cancelling against a hallucinated
        # id that didn't come from a tool result this session. Cheap defence-in-depth
        # on top of the EHR's own validation.
        guard_err = self._validate_against_memory(call.name, args)
        if guard_err is not None:
            return guard_err
        async with self.timing.measure(phase=f"tool:{call.name}", state=self.state.value):
            return await handler(self._ehr, **args)

    def _validate_against_memory(self, name: str, args: dict[str, Any]) -> Err | None:
        if name == "create_appointment":
            slot_id = args.get("slot_id")
            known_slots = {s["slot_id"] for s in self.memory.last_slots}
            if slot_id is not None and slot_id not in known_slots:
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
                    message=(
                        f"patient_id {patient_id!r} != identified patient "
                        f"{known_patient!r}"
                    ),
                    retryable=False,
                )
        elif name == "cancel_appointment":
            appt_id = args.get("appointment_id")
            known = {a["id"] for a in self.memory.last_upcoming_appointments}
            if appt_id is not None and appt_id not in known:
                return Err(
                    code="hallucinated_appointment_id",
                    message=(
                        f"appointment_id {appt_id!r} not in "
                        f"last_upcoming_appointments — call "
                        f"get_upcoming_appointments first"
                    ),
                    retryable=True,
                )
        return None

    def _record_tool_result(self, name: str, result: Result[dict]) -> None:
        if is_ok(result):
            self.transcript.append({"kind": "tool_ok", "name": name, "value": result.value})
            # Audit A3: redact raw UUIDs from the string the LLM sees. The
            # transcript and memory keep the originals for our own logic,
            # but the LLM only gets a human-readable summary so it can't
            # accidentally read a slot_id or appointment_id aloud.
            self.history.append(
                {
                    "role": "tool",
                    "name": name,
                    "content": _redact_for_llm(name, result.value),
                }
            )
            if name == "list_availability_slots":
                self.memory.last_slots = result.value["slots"]
            elif name == "get_upcoming_appointments":
                self.memory.last_upcoming_appointments = result.value["appointments"]
            elif name in ("find_patient_by_phone", "find_patient_by_name_dob"):
                patients = result.value.get("patients", [])
                if len(patients) == 1:
                    self.memory.identified_patient = patients[0]
            elif name == "create_patient":
                self.memory.identified_patient = {
                    "id": result.value["patient_id"],
                    "first_name": result.value["first_name"],
                    "last_name": result.value["last_name"],
                    "phone": result.value["phone"],
                }
        else:
            assert is_err(result)
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
                    "name": name,
                    "content": f"ERROR code={result.code} message={result.message}",
                }
            )

    def _maybe_transition_from_user_text(self, user_text: str) -> None:
        if self.state is State.GREETING:
            self._transition("go_identify")
            return
        if self.state is State.CHOOSE_INTENT:
            if _CANCEL_INTENT.search(user_text):
                self._transition("wants_cancel")
            elif _BOOK_INTENT.search(user_text):
                self._transition("wants_book")

    def _maybe_transition_from_tool(self, tool_name: str, result: Result[dict]) -> None:
        if self.state is State.IDENTIFY_PATIENT and tool_name in (
            "find_patient_by_phone",
            "find_patient_by_name_dob",
        ):
            if is_ok(result):
                patients = result.value.get("patients", [])
                if len(patients) == 1:
                    self._transition("patient_found")
                elif tool_name == "find_patient_by_name_dob" and not patients:
                    self._transition("no_match")
        elif self.state is State.REGISTER_PATIENT and tool_name == "create_patient":
            if is_ok(result):
                self._transition("registered")
        elif self.state is State.BOOK_FLOW and tool_name == "list_availability_slots":
            if is_ok(result):
                self._transition("slot_chosen")
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
        elif self.state is State.CONFIRM_CANCEL and tool_name == "cancel_appointment":
            if is_ok(result):
                self._transition("cancelled")

    def _transition(self, label: str) -> None:
        dst = TRANSITIONS[self.state].get(label)
        if dst is None:
            return
        self.transcript.append(
            {"kind": "transition", "from": self.state.value, "to": dst.value, "label": label}
        )
        self.state = dst

    def _messages_for_llm(self) -> list[dict]:
        return [
            {"role": "system", "content": CLINIC_PERSONA},
            {"role": "system", "content": TASK_MESSAGES[self.state.value]},
            *self.history,
        ]
