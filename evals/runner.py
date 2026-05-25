"""Headless scenario runner.

For each Scenario:
1. Build a fresh SQLite EHR (file-backed in a temp path), mount via
   httpx.ASGITransport.
2. Run ``scenario.setup`` to seed the DB.
3. Snapshot patient / appointment counts.
4. Drive the dispatcher with a persona-LLM until END, an error, or
   ``max_turns``.
5. Snapshot counts again; check state expectations.
6. Run the LLM judge.
7. Return a ScenarioResult.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json as _json
import os
import re
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from evals.judge import judge_transcript
from evals.mock_llm import (
    MockDispatcherLLM,
    MockPersonaLLM,
    MockTriageClient,
    mock_judge_transcript,
)
from evals.sim import PersonaSimulator
from evals.types import Scenario, ScenarioResult
from prosper.dispatcher import Dispatcher
from prosper.ehr.api import create_app
from prosper.ehr.db import init_db, make_engine
from prosper.ehr.models import Appointment, AppointmentStatus, Patient
from prosper.ehr_client import EHRClient
from prosper.flows import State
from prosper.llm import OpenAILLMAdapter
from prosper.observability.redact import redact_pii


def render_trace(result: ScenarioResult) -> str:
    """Render a ScenarioResult's transcript as a readable, PII-redacted table.

    Used by ``python -m evals --trace`` (FUTURE.md 6.1). Derives everything
    from the already-populated ``Dispatcher.transcript`` — no extra data
    collection. User- and bot-spoken text is piped through ``redact_pii`` so
    no raw phone/DOB/email reaches the terminal. The turn counter increments
    on each caller utterance; the state column tracks the most recent state
    seen on an assistant event or transition.
    """
    header = (
        f"\nTRACE  {result.name}  "
        f"(state={'P' if result.state_pass else 'F'} "
        f"judge={'P' if result.judge_pass else 'F'}, turns={result.turns})"
    )
    rows: list[str] = [header, f"  {'turn':>4}  {'state':<18}  event"]
    turn = 0
    state = "(init)"

    def _clip(text: str, width: int = 64) -> str:
        text = redact_pii(text or "").replace("\n", " ").strip()
        return text if len(text) <= width else text[: width - 1] + "…"

    for ev in result.transcript:
        kind = ev.get("kind")
        if kind == "user":
            turn += 1
            rows.append(f"  {turn:>4}  {state:<18}  USER  {_clip(ev.get('text', ''))}")
        elif kind == "assistant":
            state = ev.get("state", state)
            rows.append(f"  {'':>4}  {state:<18}  BOT   {_clip(ev.get('text', ''))}")
        elif kind == "tool_ok":
            rows.append(f"  {'':>4}  {state:<18}  TOOL  ok   {ev.get('name', '?')}")
        elif kind == "tool_err":
            rows.append(
                f"  {'':>4}  {state:<18}  TOOL  ERR  {ev.get('name', '?')} "
                f"code={ev.get('code', '?')}"
            )
        elif kind == "tool_rejected":
            rows.append(f"  {'':>4}  {state:<18}  TOOL  REJECTED  {ev.get('name', '?')}")
        elif kind == "transition":
            state = ev.get("to", state)
            rows.append(
                f"  {'':>4}  {state:<18}  ->    {ev.get('from', '?')} -> "
                f"{ev.get('to', '?')} ({ev.get('label', '?')})"
            )
        elif kind:
            # Surface other transcript markers (empty_slot_result,
            # llm_loop_exhausted, tool_repeated_blocked, …) so a debugging
            # operator sees the dispatcher's own breadcrumbs too.
            rows.append(f"  {'':>4}  {state:<18}  NOTE  {kind}")
    return "\n".join(rows)


@contextmanager
def _isolated_engine() -> Iterator[Engine]:
    """Build a fresh SQLite engine on a private tempfile, yield it, cleanup.

    No global state is mutated — safe to call concurrently from multiple
    asyncio tasks. The eval runner calls ``create_app(engine)`` with this
    engine so the FastAPI app, repo writes, and snapshot reads all use the
    same isolated database.
    """
    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = make_engine(f"sqlite:///{tmp_path}")
    init_db(engine)
    try:
        yield engine
    finally:
        engine.dispose()
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)


def _count_patients(session: Session) -> int:
    return session.execute(select(func.count(Patient.id))).scalar_one()


def _count_appts(session: Session, status: AppointmentStatus) -> int:
    return session.execute(
        select(func.count(Appointment.id)).where(Appointment.status == status)
    ).scalar_one()


_HALLUCINATED_CLAIM = re.compile(
    r"(?:\bi(?:'ve| have)?(?: just)? (?:cancelled|canceled|booked)\b"
    r"|\byour appointment (?:has been|is) (?:cancelled|canceled|booked)\b"
    r"|\bthat'?s (?:cancelled|canceled|booked|done)\b"
    r"|\bdone\s*[—-]\s*your appointment has been (?:cancelled|canceled|booked)\b"
    r"|\bdone, your appointment\b)",
    re.IGNORECASE,
)


# End-of-call phrases. We match only when the phrase is the final utterance
# (the persona's hang-up) — not mid-sentence in something like "see you next
# Tuesday at 3pm" while booking. Implementation: phrase must be followed by
# nothing-but-punctuation (incl. exclamation, period, comma, dash) and then
# end-of-string. The "thanks" preamble is allowed before each phrase. Case
# insensitive throughout.
_STOP_WORDS_PATTERN = re.compile(
    r"\b("
    r"thanks,?\s*bye"
    r"|goodbye"
    r"|bye"
    r"|see\s+you"
    r"|talk\s+(?:to\s+you\s+)?later"
    r"|never\s*mind"
    r"|that(?:'s|\s+is)\s+all"
    r"|i'?m\s+done"
    r"|have\s+a\s+good\s+(?:day|night|one)"
    r"|cheers"
    r"|take\s+care"
    r")\s*[!.,\-—…]*\s*$",
    re.IGNORECASE,
)


def _is_persona_stop(user_text: str) -> bool:
    """Return True if ``user_text`` is a stand-alone end-of-call utterance.

    Whole-word, case-insensitive match anchored to the END of the trimmed
    string so phrases that legitimately appear mid-sentence (e.g. "see you
    next Tuesday at 3pm") do NOT trigger the stop.
    """
    stripped = user_text.strip()
    if not stripped:
        return False
    return _STOP_WORDS_PATTERN.search(stripped) is not None


def _check_hallucinated_confirmation(transcript: list[dict]) -> list[str]:
    """Flag any assistant turn that claims a write happened without an
    actually-successful matching tool call in the recent (≤4 events) window.

    Adversarial scenarios like ``hallucinated_confirmation_trap`` would pass
    on state-delta alone, but the bot can still *say* "I cancelled it" to
    the caller. This deterministic regex catches the lie.
    """
    reasons: list[str] = []
    for i, ev in enumerate(transcript):
        if ev.get("kind") != "assistant":
            continue
        text = ev.get("text") or ""
        if not _HALLUCINATED_CLAIM.search(text):
            continue
        window = transcript[max(0, i - 4) : i]
        confirmed = any(
            w.get("kind") == "tool_ok"
            and w.get("name") in ("create_appointment", "cancel_appointment")
            for w in window
        )
        if not confirmed:
            reasons.append(f"hallucinated confirmation: {text[:80]!r}")
    return reasons


def _evaluate_state(
    *,
    scenario: Scenario,
    transcript: list[dict],
    terminal_state: State,
    deltas: dict[str, int],
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    e = scenario.expected_state
    if e.patient_count_delta != deltas["patient"]:
        reasons.append(
            f"patient_count_delta {deltas['patient']} != expected {e.patient_count_delta}"
        )
    if e.active_appointment_count_delta != deltas["active"]:
        reasons.append(
            f"active_appt_delta {deltas['active']} != expected {e.active_appointment_count_delta}"
        )
    if e.cancelled_appointment_count_delta != deltas["cancelled"]:
        reasons.append(
            f"cancelled_appt_delta {deltas['cancelled']} != expected "
            f"{e.cancelled_appointment_count_delta}"
        )
    if e.expected_terminal_state and terminal_state.value != e.expected_terminal_state:
        reasons.append(
            f"terminal state {terminal_state.value} != expected {e.expected_terminal_state}"
        )
    fired_codes = [ev["name"] for ev in transcript if ev.get("kind") in ("tool_ok", "tool_err")]
    for code in e.expected_tool_call_codes:
        if code not in fired_codes:
            reasons.append(f"expected tool {code} never fired")
    for code in e.forbidden_tool_calls:
        if code in fired_codes:
            reasons.append(f"forbidden tool {code} fired")
    reasons.extend(_check_hallucinated_confirmation(transcript))
    return (not reasons), reasons


def _dump_history_on_bad_request(*, scenario_name: str, dispatcher: Dispatcher) -> Path:
    """Write the dispatcher history to ``evals/results/debug_<name>_<ts>.json``.

    Called by ``--debug`` when an ``openai.BadRequestError`` propagates out of
    a dispatcher LLM call. Returns the file path so the runner can print it
    to stderr.
    """
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out_dir = Path("evals/results")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"debug_{scenario_name}_{ts}.json"
    payload = {
        "scenario": scenario_name,
        "timestamp": ts,
        "state": dispatcher.state.value,
        "history": dispatcher.history,
    }
    path.write_text(_json.dumps(payload, indent=2, default=str))
    return path


async def run_scenario(
    scenario: Scenario,
    *,
    openai_client: Any = None,
    mock: bool = False,
    debug: bool = False,
) -> ScenarioResult:
    """Run a single scenario end-to-end.

    Args:
        scenario: the Scenario to drive.
        openai_client: an ``AsyncOpenAI`` instance, required when ``mock=False``.
        mock: when True, swap in deterministic canned LLMs (no API key needed).
            Used by ``python -m evals --mock-llm`` and the fast unit-test
            harness in ``evals/test_scripted.py::test_scenario_mock``.
        debug: when True, on ``openai.BadRequestError`` dump ``dispatcher.history``
            to ``evals/results/debug_<scenario>_<timestamp>.json`` and print
            the path to stderr before re-raising. No-op otherwise.
    """
    if not mock and openai_client is None:
        raise ValueError("openai_client is required when mock=False")
    started = time.perf_counter()
    with _isolated_engine() as engine:
        with Session(engine) as setup_session:
            scenario.setup(setup_session)
            setup_session.commit()
        with Session(engine) as snap:
            before = {
                "patient": _count_patients(snap),
                "active": _count_appts(snap, AppointmentStatus.SCHEDULED),
                "cancelled": _count_appts(snap, AppointmentStatus.CANCELLED),
            }
        app = create_app(engine=engine)
        ehr = EHRClient.for_asgi_app(app)
        if mock:
            mock_llm = MockDispatcherLLM(scenario.name)
            dispatcher = Dispatcher(llm=mock_llm, ehr_client=ehr)
            mock_llm.attach(dispatcher)
            # The triage tool (`suggest_specialty`) calls the mini-LLM via
            # ``prosper.llm.classify_symptoms``; under mock-eval there is no
            # API key, so install a deterministic stub. Set on the module so
            # ``_get_triage_client`` returns it for every triage call this run.
            import prosper.llm as _prosper_llm

            _prosper_llm._TRIAGE_CLIENT_OVERRIDE = MockTriageClient()
        else:
            dispatcher = Dispatcher(
                llm=OpenAILLMAdapter(
                    client=openai_client,
                    model=os.environ.get("PROSPER_BOT_MODEL", "gpt-4o-mini"),
                ),
                ehr_client=ehr,
            )
        async with ehr:
            sim: Any
            if mock:
                sim = MockPersonaLLM(scenario.name)
            else:
                sim = PersonaSimulator(client=openai_client, persona=scenario.persona)
            try:
                bot_text = await dispatcher.start()
                turns = 0
                # HANDOFF is a terminal holding state — the bot speaks its
                # callback confirmation then the FSM will reach END on the
                # next goodbye. Stop driving turns once we reach either
                # terminal so mock scenarios don't run off the end of their
                # scripted replies.
                _terminal_states = (State.END, State.HANDOFF)
                while dispatcher.state not in _terminal_states and turns < scenario.max_turns:
                    user_text = await sim.reply_to(bot_text)
                    if _is_persona_stop(user_text):
                        # The persona just hung up. Reflect that in the
                        # FSM terminal state so the expectation check
                        # ("expected_terminal_state == END") matches what
                        # actually happened: the call ended. Without this,
                        # adding new stop-words would regress scenarios
                        # whose bot mock relied on processing the user's
                        # final utterance to reach END internally.
                        dispatcher.state = State.END
                        break
                    bot_text = await dispatcher.handle_user_turn(user_text)
                    turns += 1
            except Exception as exc:
                # Only act on openai.BadRequestError when --debug is on. Lazy
                # import keeps the runner usable in --mock-llm mode without
                # the openai package's full import cost on the error path.
                if debug:
                    try:
                        from openai import BadRequestError as _BadRequestError
                    except ImportError:
                        _BadRequestError = None  # type: ignore[assignment,misc]
                    if _BadRequestError is not None and isinstance(exc, _BadRequestError):
                        path = _dump_history_on_bad_request(
                            scenario_name=scenario.name, dispatcher=dispatcher
                        )
                        print(
                            f"[debug] dispatcher.history dumped to {path}",
                            file=sys.stderr,
                        )
                raise
        with Session(engine) as snap:
            after = {
                "patient": _count_patients(snap),
                "active": _count_appts(snap, AppointmentStatus.SCHEDULED),
                "cancelled": _count_appts(snap, AppointmentStatus.CANCELLED),
            }
        deltas = {k: after[k] - before[k] for k in before}
        state_pass, reasons = _evaluate_state(
            scenario=scenario,
            transcript=dispatcher.transcript,
            terminal_state=dispatcher.state,
            deltas=deltas,
        )
        if mock:
            judge_pass, justification = await mock_judge_transcript(
                transcript=dispatcher.transcript,
                criteria=scenario.judge_criteria,
            )
        else:
            judge_pass, justification = await judge_transcript(
                client=openai_client,
                transcript=dispatcher.transcript,
                criteria=scenario.judge_criteria,
            )
        timing = dispatcher.timing.summary()
        cached_tokens = dispatcher.cached_prompt_tokens_total
        prompt_tokens = dispatcher.prompt_tokens_total
    duration = (time.perf_counter() - started) * 1000
    return ScenarioResult(
        name=scenario.name,
        state_pass=state_pass,
        state_reasons=reasons,
        judge_pass=judge_pass,
        judge_justification=justification,
        turns=turns,
        duration_ms=duration,
        transcript=dispatcher.transcript,
        timing_summary=timing,
        cached_prompt_tokens=cached_tokens,
        prompt_tokens=prompt_tokens,
    )
