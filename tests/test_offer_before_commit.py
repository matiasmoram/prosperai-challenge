"""Offer-then-confirm invariant: a write tool may not commit in the same caller
turn that its options were read.

Live bug (2026-05-25 session 15da2ce0): the model called
``list_availability_slots`` and then ``create_appointment`` in ONE turn, booking
a morning slot the caller never chose after the caller asked for "afternoon any
day". The slot-id guard accepted it (the slot WAS in the offered list — just
never selected), and a real appointment was written. This pins the architectural
fix in ``dispatcher._READ_BEFORE_WRITE``: the booking is blocked until the caller
picks on a later turn.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from prosper.dispatcher import Dispatcher, LLMClientProtocol, LLMReply, ToolCall
from prosper.ehr_client import EHRClient
from prosper.flows import State


def _tomorrow() -> Any:
    return (datetime.now(timezone.utc) + timedelta(days=1)).date()


def _tomorrow_iso() -> str:
    return _tomorrow().isoformat()


class _ListThenBookSameTurnLLM(LLMClientProtocol):
    """Turn 1: emit BOTH list_availability_slots AND create_appointment (the bug).
    Any later call: a plain text reply so the inner loop terminates."""

    def __init__(self) -> None:
        self.calls = 0

    async def generate(
        self, *, state: str, history: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMReply:
        self.calls += 1
        if self.calls == 1:
            return LLMReply(
                text="",
                tool_calls=[
                    ToolCall(
                        name="list_availability_slots",
                        arguments={"date": _tomorrow_iso(), "specialty": "General Practice"},
                        id="call_list",
                    ),
                    ToolCall(
                        name="create_appointment",
                        arguments={"slot_id": "1"},
                        id="call_book",
                    ),
                ],
            )
        return LLMReply(text="Here are some times — which works for you?")


async def test_create_blocked_when_listed_same_turn(seeded_ehr_client: EHRClient) -> None:
    """list + book in one turn → the booking is blocked, no appointment written."""
    async with seeded_ehr_client:
        d = Dispatcher(llm=_ListThenBookSameTurnLLM(), ehr_client=seeded_ehr_client)
        # Jump straight into the booking flow with an identified caller.
        d.state = State.BOOK_FLOW
        d.memory.identified_patient = {
            "id": "11111111-1111-1111-1111-111111111111",
            "first_name": "Ada",
            "last_name": "Lovelace",
            "phone": "2025550100",
        }
        reply = await d.handle_user_turn("book me a GP visit tomorrow")

        # The list ran (slots cached) but the same-turn book was blocked.
        kinds = [e.get("kind") for e in d.transcript]
        assert "write_before_offer_blocked" in kinds
        blocked = [e for e in d.transcript if e.get("kind") == "write_before_offer_blocked"]
        assert blocked[0]["name"] == "create_appointment"
        # The read DID run; the write did NOT. (`outcome` is a bus event, not a
        # transcript kind — assert on the tool receipts, which ARE in the
        # transcript.) No create_appointment tool_ok means no booking executed.
        assert any(
            e.get("kind") == "tool_ok" and e.get("name") == "list_availability_slots"
            for e in d.transcript
        )
        assert not any(
            e.get("kind") in ("tool_ok", "tool_err") and e.get("name") == "create_appointment"
            for e in d.transcript
        ), "create_appointment must NOT have executed this turn"
        # The bot still says something (offers the slots) — never silent.
        assert reply


async def test_create_allowed_on_a_later_turn(seeded_ehr_client: EHRClient) -> None:
    """The same booking IS allowed once it's a fresh turn (caller has picked)."""

    class _BookOnlyLLM(LLMClientProtocol):
        async def generate(
            self, *, state: str, history: list[dict[str, Any]], tools: list[dict[str, Any]]
        ) -> LLMReply:
            return LLMReply(
                text="",
                tool_calls=[
                    ToolCall(name="create_appointment", arguments={"slot_id": "1"}, id="c1")
                ],
            )

    async with seeded_ehr_client:
        d = Dispatcher(llm=_BookOnlyLLM(), ehr_client=seeded_ehr_client)
        d.state = State.CONFIRM_BOOK
        d.memory.identified_patient = {
            "id": "11111111-1111-1111-1111-111111111111",
            "first_name": "Ada",
            "last_name": "Lovelace",
            "phone": "2025550100",
        }
        # Slots were offered on a PRIOR turn (no list call this turn). Shape them
        # like the tool output the dispatcher caches (handle resolver wants
        # `slot_id`; the raw EHR row keys it `id`).
        raw = await seeded_ehr_client.list_availability(date_=_tomorrow())
        d.memory.last_slots = [{**s, "slot_id": s["id"]} for s in raw]
        await d.handle_user_turn("the first one please")

        # No offer-then-confirm block fired (the read was a prior turn) — the
        # write was free to proceed.
        kinds = [e.get("kind") for e in d.transcript]
        assert "write_before_offer_blocked" not in kinds


async def test_confirm_book_can_relist_instead_of_being_trapped(
    seeded_ehr_client: EHRClient,
) -> None:
    """In CONFIRM_BOOK a caller who asks 'which are free?' (not a yes/no) must be
    able to re-list — the state is no longer a write-only trap. list_availability_
    slots is NOT rejected, and nothing is booked on the question."""

    class _RelistLLM(LLMClientProtocol):
        calls = 0

        async def generate(
            self, *, state: str, history: list[dict[str, Any]], tools: list[dict[str, Any]]
        ) -> LLMReply:
            type(self).calls += 1
            if type(self).calls == 1:
                return LLMReply(
                    text="",
                    tool_calls=[
                        ToolCall(
                            name="list_availability_slots",
                            arguments={"date": _tomorrow_iso(), "specialty": "General Practice"},
                            id="relist",
                        )
                    ],
                )
            return LLMReply(text="Here are the open times — which works for you?")

    async with seeded_ehr_client:
        d = Dispatcher(llm=_RelistLLM(), ehr_client=seeded_ehr_client)
        d.state = State.CONFIRM_BOOK
        d.memory.identified_patient = {
            "id": "11111111-1111-1111-1111-111111111111",
            "first_name": "Ada",
            "last_name": "Lovelace",
            "phone": "2025550100",
        }
        await d.handle_user_turn("wait, which ones are actually free?")

        # list_availability_slots was allowed (NOT rejected) in CONFIRM_BOOK...
        assert not any(
            e.get("kind") == "tool_rejected" and e.get("name") == "list_availability_slots"
            for e in d.transcript
        )
        assert any(
            e.get("kind") == "tool_ok" and e.get("name") == "list_availability_slots"
            for e in d.transcript
        )
        # ...and nothing was booked off the question.
        assert not any(
            e.get("kind") in ("tool_ok", "tool_err") and e.get("name") == "create_appointment"
            for e in d.transcript
        )


async def test_create_blocked_when_caller_turn_is_a_question(
    seeded_ehr_client: EHRClient,
) -> None:
    """Consent gate: in CONFIRM_BOOK a model that fires create_appointment while
    the caller's last turn was a QUESTION ("tell me which ones are not taken")
    is blocked — no booking, the bot re-offers. Live bug 7c9d55a5: the model
    booked an un-offered slot on exactly this turn and ended the call.

    The slots were offered on a PRIOR turn (no list this turn), so the
    same-turn offer-then-confirm guard does NOT fire — only the consent gate
    can catch this."""

    class _BookOnQuestionLLM(LLMClientProtocol):
        calls = 0

        async def generate(
            self, *, state: str, history: list[dict[str, Any]], tools: list[dict[str, Any]]
        ) -> LLMReply:
            type(self).calls += 1
            if type(self).calls == 1:
                return LLMReply(
                    text="",
                    tool_calls=[
                        ToolCall(name="create_appointment", arguments={"slot_id": "1"}, id="c1")
                    ],
                )
            return LLMReply(text="Here are the open afternoon times — which would you like?")

    async with seeded_ehr_client:
        d = Dispatcher(llm=_BookOnQuestionLLM(), ehr_client=seeded_ehr_client)
        d.state = State.CONFIRM_BOOK
        d.memory.identified_patient = {
            "id": "11111111-1111-1111-1111-111111111111",
            "first_name": "Ada",
            "last_name": "Lovelace",
            "phone": "2025550100",
        }
        raw = await seeded_ehr_client.list_availability(date_=_tomorrow())
        d.memory.last_slots = [{**s, "slot_id": s["id"]} for s in raw]
        reply = await d.handle_user_turn("well, tell me which ones are not taken")

        # The consent gate fired and the booking did NOT execute.
        kinds = [e.get("kind") for e in d.transcript]
        assert "write_without_consent_blocked" in kinds
        assert not any(
            e.get("kind") in ("tool_ok", "tool_err") and e.get("name") == "create_appointment"
            for e in d.transcript
        ), "create_appointment must NOT execute on a question turn"
        # The bot still speaks (re-offers) — never silent, never a hallucinated
        # confirmation.
        assert reply


async def test_create_allowed_on_a_natural_pick_not_a_yes(
    seeded_ehr_client: EHRClient,
) -> None:
    """The consent gate is asymmetric: a natural selection that is NOT the word
    'yes' ("the first one please") is neither info-seeking nor a denial, so the
    write proceeds. Guards against the gate over-blocking real confirmations."""

    class _BookOnlyLLM(LLMClientProtocol):
        async def generate(
            self, *, state: str, history: list[dict[str, Any]], tools: list[dict[str, Any]]
        ) -> LLMReply:
            return LLMReply(
                text="",
                tool_calls=[
                    ToolCall(name="create_appointment", arguments={"slot_id": "1"}, id="c1")
                ],
            )

    async with seeded_ehr_client:
        d = Dispatcher(llm=_BookOnlyLLM(), ehr_client=seeded_ehr_client)
        d.state = State.CONFIRM_BOOK
        d.memory.identified_patient = {
            "id": "11111111-1111-1111-1111-111111111111",
            "first_name": "Ada",
            "last_name": "Lovelace",
            "phone": "2025550100",
        }
        raw = await seeded_ehr_client.list_availability(date_=_tomorrow())
        d.memory.last_slots = [{**s, "slot_id": s["id"]} for s in raw]
        await d.handle_user_turn("the first one please")

        # Neither guard fired — the natural pick reached execution (the booking's
        # own success/failure depends on seed data; what matters here is that the
        # consent gate did NOT intercept a legitimate selection).
        kinds = [e.get("kind") for e in d.transcript]
        assert "write_without_consent_blocked" not in kinds
        assert "write_before_offer_blocked" not in kinds
        assert any(
            e.get("kind") in ("tool_ok", "tool_err") and e.get("name") == "create_appointment"
            for e in d.transcript
        ), "create_appointment must reach execution on a natural pick"


class _SilentLLM(LLMClientProtocol):
    async def generate(
        self, *, state: str, history: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMReply:
        return LLMReply(text="")


async def test_create_rejects_unshown_middle_slot(seeded_ehr_client: EHRClient) -> None:
    """Many-slot day: only a day-spanning sample is shown to the LLM. Booking a
    middle slot the caller never heard is rejected — even though it IS in
    last_slots — so the model can't commit a time it never offered."""
    from prosper.dispatcher import _sample_slot_indices

    async with seeded_ehr_client:
        d = Dispatcher(llm=_SilentLLM(), ehr_client=seeded_ehr_client)
        d.memory.identified_patient = {"id": "p1"}
        d.memory.last_slots = [{"slot_id": f"slot-{i}"} for i in range(8)]
        shown = _sample_slot_indices(8)  # [0,1,2,5,6,7] — middle 3,4 hidden
        d.memory.last_shown_slot_ids = {d.memory.last_slots[i]["slot_id"] for i in shown}

        ok = d._validate_against_memory(
            "create_appointment", {"slot_id": "slot-0", "patient_id": "p1"}
        )
        assert ok is None, "a SHOWN slot must pass validation"

        err = d._validate_against_memory(
            "create_appointment", {"slot_id": "slot-4", "patient_id": "p1"}
        )
        assert err is not None and err.code == "hallucinated_slot_id", (
            "an UNSHOWN middle slot must be rejected"
        )


async def test_create_blocked_when_write_precedes_read_in_same_batch(
    seeded_ehr_client: EHRClient,
) -> None:
    """Adversarial ordering: the model emits create_appointment BEFORE
    list_availability_slots in one tool_calls array. The pre-scan still blocks
    the write (offer-then-confirm), so order within the batch can't slip it."""

    class _ReverseOrderLLM(LLMClientProtocol):
        calls = 0

        async def generate(
            self, *, state: str, history: list[dict[str, Any]], tools: list[dict[str, Any]]
        ) -> LLMReply:
            type(self).calls += 1
            if type(self).calls == 1:
                return LLMReply(
                    text="",
                    tool_calls=[
                        ToolCall(name="create_appointment", arguments={"slot_id": "1"}, id="b"),
                        ToolCall(
                            name="list_availability_slots",
                            arguments={"date": _tomorrow_iso(), "specialty": "General Practice"},
                            id="l",
                        ),
                    ],
                )
            return LLMReply(text="Here's what's open — which works?")

    async with seeded_ehr_client:
        d = Dispatcher(llm=_ReverseOrderLLM(), ehr_client=seeded_ehr_client)
        d.state = State.CONFIRM_BOOK
        d.memory.identified_patient = {
            "id": "11111111-1111-1111-1111-111111111111",
            "first_name": "Ada",
            "last_name": "Lovelace",
            "phone": "2025550100",
        }
        await d.handle_user_turn("just book me something tomorrow")

        kinds = [e.get("kind") for e in d.transcript]
        assert "write_before_offer_blocked" in kinds
        assert not any(
            e.get("kind") in ("tool_ok", "tool_err") and e.get("name") == "create_appointment"
            for e in d.transcript
        ), "create must NOT execute even when emitted before the read in the batch"
