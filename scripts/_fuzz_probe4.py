"""Topic 9: parallel booking race + dispatcher checks."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding="utf-8")

import httpx
from sqlalchemy import create_engine

from prosper.ehr.api import create_app
from prosper.ehr.db import make_session_factory
from prosper.ehr.models import Provider, Slot
from prosper.dispatcher import Dispatcher, ToolCall, LLMReply, SessionMemory
from prosper.flows import State
from prosper.ehr_client import EHRClient


async def topic9() -> None:
    print("--- Topic 9: race ---")
    db_path = os.path.join(tempfile.gettempdir(), "fuzz_probe_race.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    eng = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    app = create_app(engine=eng)
    Sess = make_session_factory(eng)
    with Sess() as s:
        prov = Provider(name="Dr. R", timezone="UTC")
        s.add(prov)
        s.commit()
        slot = Slot(
            provider_id=prov.id,
            start_at=datetime.now(timezone.utc) + timedelta(days=1),
            end_at=datetime.now(timezone.utc) + timedelta(days=1, minutes=30),
        )
        s.add(slot)
        s.commit()
        slot_id = slot.id

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ehr"
    ) as c:
        p1 = await c.post(
            "/patients",
            json={"first_name": "A", "last_name": "1", "dob": "1990-01-01", "phone": "+15551110001"},
        )
        p2 = await c.post(
            "/patients",
            json={"first_name": "B", "last_name": "2", "dob": "1990-01-01", "phone": "+15551110002"},
        )
        pid1 = p1.json()["id"]
        pid2 = p2.json()["id"]
        r1, r2 = await asyncio.gather(
            c.post("/appointments", json={"patient_id": pid1, "slot_id": slot_id}),
            c.post("/appointments", json={"patient_id": pid2, "slot_id": slot_id}),
        )
        print(f"resp 1: {r1.status_code} {r1.json()}")
        print(f"resp 2: {r2.status_code} {r2.json()}")
        ok = sum(1 for r in (r1, r2) if r.status_code == 201)
        conflict = sum(1 for r in (r1, r2) if r.status_code == 409)
        print(f"ok={ok} conflict={conflict} (want exactly 1 of each)")


class _StubLLM:
    """Captures the next reply to return; required by Dispatcher protocol."""

    def __init__(self) -> None:
        self.next = LLMReply(text="hi")

    async def generate(self, **kwargs):  # type: ignore[no-untyped-def]
        return self.next


async def topic10() -> None:
    print("--- Topic 10: extra kwarg in ToolCall ---")
    db_path = os.path.join(tempfile.gettempdir(), "fuzz_probe_tc.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    eng = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    app = create_app(engine=eng)
    async with EHRClient.for_asgi_app(app) as ehr:
        d = Dispatcher(llm=_StubLLM(), ehr_client=ehr)
        d.state = State.CONFIRM_BOOK
        # Seed memory so guard passes
        d.memory.last_slots = [{"slot_id": "abc"}]
        d.memory.identified_patient = {"id": "pid-1"}
        call = ToolCall(
            name="create_appointment",
            arguments={"slot_id": "abc", "patient_id": "pid-1", "mystery_field": 42},
        )
        try:
            r = await d._execute_tool(call)
            print(f"result: {r.kind} code={getattr(r, 'code', None)} value={getattr(r, 'value', None)}")
        except Exception as e:
            print(f"CRASH: {type(e).__name__}: {e}")


async def topic11() -> None:
    print("--- Topic 11: state transitions from END ---")
    from prosper.flows import TRANSITIONS
    print(f"TRANSITIONS[END] = {dict(TRANSITIONS[State.END])}")
    # Drive a dispatcher from END through every possible code path
    db_path = os.path.join(tempfile.gettempdir(), "fuzz_probe_end.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    eng = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    app = create_app(engine=eng)
    async with EHRClient.for_asgi_app(app) as ehr:
        d = Dispatcher(llm=_StubLLM(), ehr_client=ehr)
        d.state = State.END
        d._maybe_transition_from_user_text("yes I want to book")
        print(f"after user_text from END: state={d.state}")
        d._maybe_transition_from_user_text("cancel something")
        print(f"after user_text2 from END: state={d.state}")


async def topic12() -> None:
    print("--- Topic 12: start() called twice ---")
    db_path = os.path.join(tempfile.gettempdir(), "fuzz_probe_twice.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    eng = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    app = create_app(engine=eng)
    async with EHRClient.for_asgi_app(app) as ehr:
        d = Dispatcher(llm=_StubLLM(), ehr_client=ehr)
        await d.start()
        await d.start()
        print(f"transcript kinds: {[t['kind'] for t in d.transcript]}")


async def main() -> None:
    await topic9()
    print()
    await topic10()
    print()
    await topic11()
    print()
    await topic12()


if __name__ == "__main__":
    asyncio.run(main())
