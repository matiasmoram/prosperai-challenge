"""Adversarial: a hostile/buggy LLM must never crash the dispatcher turn.

Exercises the dispatcher's defensive machinery end-to-end against a real
(in-process ASGI) EHR: per-state tool whitelist, per-turn duplicate-call
blocking, unknown-kwarg filtering, and hallucinated-id guards — in
combination, not in isolation. Each test asserts graceful degradation
(structured transcript entries, a spoken reply) rather than an exception.
"""

from __future__ import annotations

from prosper.dispatcher import Dispatcher, LLMReply, ToolCall
from prosper.flows import State


class _ScriptedLLM:
    """Returns canned replies in order, then empty replies to end the turn."""

    def __init__(self, replies: list[LLMReply]) -> None:
        self._replies = replies
        self._i = 0
        self.states_seen: list[str] = []

    async def generate(self, *, state: str, history: list, tools: list) -> LLMReply:
        self.states_seen.append(state)
        if self._i < len(self._replies):
            r = self._replies[self._i]
            self._i += 1
            return r
        return LLMReply(text="", tool_calls=[])


def _kinds(transcript: list[dict]) -> list[str]:
    return [e.get("kind", "") for e in transcript]


async def test_forbidden_tool_is_rejected_not_executed(ehr_client) -> None:
    """An LLM that calls create_appointment in IDENTIFY_PATIENT is rejected;
    the turn still produces a spoken reply."""
    llm = _ScriptedLLM(
        [
            LLMReply(text="", tool_calls=[ToolCall(name="create_appointment", arguments={})]),
            LLMReply(text="Sorry, what is your phone number?"),
        ]
    )
    async with ehr_client:
        d = Dispatcher(llm=llm, ehr_client=ehr_client)
        reply = await d.handle_user_turn("hi there")  # GREETING -> IDENTIFY_PATIENT
    assert "tool_rejected" in _kinds(d.transcript)
    assert reply == "Sorry, what is your phone number?"
    # The forbidden call must not have executed (no booking tool_ok).
    assert not any(
        e.get("kind") == "tool_ok" and e.get("name") == "create_appointment" for e in d.transcript
    )


async def test_repeated_identical_tool_call_is_blocked(ehr_client) -> None:
    """An LLM spamming the same tool with identical args within one turn is
    blocked after two executions — no infinite loop, no crash."""
    spam = LLMReply(
        text="",
        tool_calls=[ToolCall(name="find_patient_by_phone", arguments={"phone": "5551230000"})],
    )
    llm = _ScriptedLLM([spam, spam, spam, spam])
    async with ehr_client:
        d = Dispatcher(llm=llm, ehr_client=ehr_client)
        await d.handle_user_turn("hello")
    assert "tool_repeated_blocked" in _kinds(d.transcript)


async def test_unknown_kwarg_is_filtered_not_crashing(ehr_client) -> None:
    """An LLM that invents an extra argument must not crash the handler call;
    the unknown kwarg is dropped and the tool runs normally."""
    llm = _ScriptedLLM(
        [
            LLMReply(
                text="",
                tool_calls=[
                    ToolCall(
                        name="find_patient_by_phone",
                        arguments={"phone": "5551230000", "mystery_field": 42},
                    )
                ],
            ),
            LLMReply(text="I couldn't find you — can I take your details?"),
        ]
    )
    async with ehr_client:
        d = Dispatcher(llm=llm, ehr_client=ehr_client)
        reply = await d.handle_user_turn("hello")
    # The tool executed (Ok) despite the bogus kwarg, and produced a reply.
    assert any(
        e.get("kind") == "tool_ok" and e.get("name") == "find_patient_by_phone"
        for e in d.transcript
    )
    assert reply  # non-empty spoken reply, no exception


async def test_hallucinated_slot_id_blocked_before_http(ehr_client) -> None:
    """In CONFIRM_BOOK, a create_appointment with a slot_id never offered is
    caught by the memory guard (hallucinated_slot_id) — no booking happens."""
    llm = _ScriptedLLM(
        [
            LLMReply(
                text="",
                tool_calls=[
                    ToolCall(
                        name="create_appointment",
                        arguments={"patient_id": "p1", "slot_id": "deadbeef-not-real"},
                    )
                ],
            ),
            LLMReply(text="Let me re-check the times for you."),
        ]
    )
    async with ehr_client:
        d = Dispatcher(llm=llm, ehr_client=ehr_client)
        # Jump straight to CONFIRM_BOOK with an identified patient but no slots
        # ever offered, so any slot_id is by definition hallucinated.
        d.state = State.CONFIRM_BOOK
        d.memory.identified_patient = {"id": "p1", "first_name": "A", "last_name": "B"}
        await d._llm_turn()
    assert any(
        e.get("kind") == "tool_err" and e.get("code") == "hallucinated_slot_id"
        for e in d.transcript
    )
