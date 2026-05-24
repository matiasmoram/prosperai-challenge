"""HYBRID navigation: the ``route_intent`` tool + dispatcher validation.

The LLM declares the caller's intent via ``route_intent`` and the dispatcher
maps it to an FSM transition label, applying it ONLY if the edge is legal from
the current state (LLM does the NLU, the FSM keeps authority over the graph).
These tests pin: legal intents route, illegal edges are refused without moving,
unknown intents are refused, and the end-to-end path works when the user-text
regex did NOT pre-route (the case the fragile regex used to mishandle).

Uses the shared ``ehr_client`` fixture (see ``tests/conftest.py``); ``route_intent``
never touches the EHR, but the ``Dispatcher`` constructor needs a client.
"""

from __future__ import annotations

from collections.abc import Iterator

from prosper.dispatcher import Dispatcher, LLMClientProtocol, LLMReply, ToolCall
from prosper.ehr_client import EHRClient
from prosper.flows import State
from prosper.result import is_err, is_ok

# An utterance that matches NONE of the CHOOSE_INTENT routing regexes
# (_STRONG_CANCEL / _STRONG_BOOK / _CANCEL_INTENT / _BOOK_INTENT / _RESCHEDULE_INTENT)
# nor the goodbye tiers — so the pre-LLM regex stays out and route_intent is the
# only thing that can move the FSM. This is exactly the "natural, ambiguous
# phrasing the regex can't classify" case the HYBRID path exists for.
_AMBIGUOUS = "um, I'm not really sure what I need yet"


class CannedLLM(LLMClientProtocol):
    """Replays a fixed list of LLMReply objects, one per generate() call."""

    def __init__(self, replies: list[LLMReply]) -> None:
        self._iter: Iterator[LLMReply] = iter(replies)

    async def generate(self, *, state: str, history: list[dict], tools: list[dict]) -> LLMReply:
        try:
            return next(self._iter)
        except StopIteration:
            return LLMReply(text="", tool_calls=[])


def _route_call(intent: str) -> ToolCall:
    return ToolCall(name="route_intent", arguments={"intent": intent})


# ---------------------------------------------------------------------------
# End-to-end: a regex-invisible utterance is routed by the LLM via route_intent
# ---------------------------------------------------------------------------


async def test_route_intent_book_advances_to_book_flow(ehr_client: EHRClient) -> None:
    canned = CannedLLM(
        [LLMReply(text="", tool_calls=[_route_call("book")]), LLMReply(text="What day works?")]
    )
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    d.state = State.CHOOSE_INTENT
    await d.handle_user_turn(_AMBIGUOUS)
    assert d.state is State.BOOK_FLOW


async def test_route_intent_cancel_advances_to_cancel_flow(ehr_client: EHRClient) -> None:
    canned = CannedLLM(
        [LLMReply(text="", tool_calls=[_route_call("cancel")]), LLMReply(text="Let me look.")]
    )
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    d.state = State.CHOOSE_INTENT
    await d.handle_user_turn(_AMBIGUOUS)
    assert d.state is State.CANCEL_FLOW


async def test_route_intent_reschedule_takes_atomic_path(ehr_client: EHRClient) -> None:
    """reschedule from CHOOSE_INTENT routes to RESCHEDULE_FLOW and leaves the
    legacy cancel-then-rebook flag OFF (the swap is one atomic tool call)."""
    canned = CannedLLM(
        [LLMReply(text="", tool_calls=[_route_call("reschedule")]), LLMReply(text="Which visit?")]
    )
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    d.state = State.CHOOSE_INTENT
    await d.handle_user_turn(_AMBIGUOUS)
    assert d.state is State.RESCHEDULE_FLOW
    assert d.memory.wants_reschedule is False


async def test_route_intent_done_ends_the_call(ehr_client: EHRClient) -> None:
    canned = CannedLLM(
        [LLMReply(text="", tool_calls=[_route_call("done")]), LLMReply(text="Take care — goodbye.")]
    )
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    d.state = State.CHOOSE_INTENT
    await d.handle_user_turn(_AMBIGUOUS)
    assert d.state is State.END


async def test_route_intent_records_a_tool_ok_for_eval_assertions(ehr_client: EHRClient) -> None:
    """The eval runner matches expected_tool_call_codes against tool_ok/tool_err
    names — route_intent must show up there so scenarios can assert on it."""
    canned = CannedLLM([LLMReply(text="", tool_calls=[_route_call("book")]), LLMReply(text="ok")])
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    d.state = State.CHOOSE_INTENT
    await d.handle_user_turn(_AMBIGUOUS)
    fired = [e["name"] for e in d.transcript if e.get("kind") in ("tool_ok", "tool_err")]
    assert "route_intent" in fired


# ---------------------------------------------------------------------------
# Validation: the dispatcher keeps authority over the graph
# ---------------------------------------------------------------------------


def test_handle_route_intent_refuses_illegal_edge(ehr_client: EHRClient) -> None:
    """An intent whose label is not a legal edge from the current state is
    refused with illegal_transition and the state does NOT move."""
    canned = CannedLLM([])
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    d.state = State.BOOK_FLOW  # wants_cancel is not an edge out of BOOK_FLOW
    result = d._handle_route_intent(_route_call("cancel"))
    assert is_err(result)
    assert result.code == "illegal_transition"
    assert d.state is State.BOOK_FLOW


def test_handle_route_intent_refuses_unknown_intent(ehr_client: EHRClient) -> None:
    canned = CannedLLM([])
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    d.state = State.CHOOSE_INTENT
    result = d._handle_route_intent(_route_call("frobnicate"))
    assert is_err(result)
    assert result.code == "unknown_intent"
    assert d.state is State.CHOOSE_INTENT


def test_handle_route_intent_legal_edge_returns_ok_and_moves(ehr_client: EHRClient) -> None:
    canned = CannedLLM([])
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    d.state = State.CHOOSE_INTENT
    result = d._handle_route_intent(_route_call("book"))
    assert is_ok(result)
    assert d.state is State.BOOK_FLOW
