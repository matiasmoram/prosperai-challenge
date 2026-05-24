"""MailStore: append-only full-PII outbound-mail records."""

from __future__ import annotations

from prosper.integrations.mail import MailStore, make_message


async def test_write_then_list_roundtrip(tmp_path) -> None:
    store = MailStore(root=tmp_path)
    await store.write(
        make_message(
            session_id="s1",
            kind="handoff",
            to_label="reception@prosper.health",
            subject="Callback — Jane Doe",
            body="Wants a refill on metformin.",
            patient_name="Jane Doe",
            patient_phone="(202) 555-0142",
            category="prescription",
            ts=1000.0,
        )
    )
    got = store.list_messages()
    assert len(got) == 1
    assert got[0].kind == "handoff"
    assert got[0].category == "prescription"
    # Full PII — NOT masked (staff trust tier, distinct from the console bus)
    assert got[0].patient_phone == "(202) 555-0142"


async def test_list_is_newest_first(tmp_path) -> None:
    store = MailStore(root=tmp_path)
    for i, ts in enumerate([1000.0, 3000.0, 2000.0]):
        await store.write(
            make_message(
                session_id=f"s{i}",
                kind="booking_confirmation",
                to_label="A B",
                subject="x",
                body="y",
                patient_name="A B",
                patient_phone="2025550000",
                ts=ts,
            )
        )
    assert [m.ts for m in store.list_messages()] == [3000.0, 2000.0, 1000.0]


def test_list_empty_when_no_writes(tmp_path) -> None:
    store = MailStore(root=tmp_path)
    assert store.list_messages() == []
