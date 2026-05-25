"""MailStore: unified, durable full-PII outbound-mail records (SQLite)."""

from __future__ import annotations

from prosper.integrations.mail import MailStore, make_message


async def test_session_id_with_separators_is_stored_verbatim(tmp_path) -> None:
    """The unified store keys rows by an autoincrement id, not a filename, so a
    session id with path separators / ".." is just an ordinary column value: it
    is stored verbatim and creates no stray files outside the mail root."""
    store = MailStore(root=tmp_path)
    await store.write(
        make_message(
            session_id="../../etc/evil",
            kind="handoff",
            to_label="reception@prosper.health",
            subject="x",
            body="y",
            patient_name="A B",
            patient_phone="2025550000",
            ts=1000.0,
        )
    )
    # Single unified DB file in the root; no per-session JSONL files anywhere.
    assert (tmp_path / "mail.db").exists()
    assert list(tmp_path.glob("*.jsonl")) == []
    # Canonical id round-trips unchanged.
    got = store.list_messages()
    assert len(got) == 1
    assert got[0].session_id == "../../etc/evil"


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


async def test_list_unifies_across_sessions(tmp_path) -> None:
    """Mail from two different sessions reads as ONE coherent inbox, newest-first
    — the whole point of the unified store (no per-session fragmentation)."""
    store = MailStore(root=tmp_path)
    await store.write(
        make_message(
            session_id="call-alpha",
            kind="handoff",
            to_label="reception@prosper.health",
            subject="alpha",
            body="y",
            patient_name="A B",
            patient_phone="2025550001",
            ts=1000.0,
        )
    )
    await store.write(
        make_message(
            session_id="call-bravo",
            kind="booking_confirmation",
            to_label="C D",
            subject="bravo",
            body="z",
            patient_name="C D",
            patient_phone="2025550002",
            ts=2000.0,
        )
    )
    got = store.list_messages()
    assert [m.session_id for m in got] == ["call-bravo", "call-alpha"]
    assert {m.session_id for m in got} == {"call-alpha", "call-bravo"}


async def test_durable_across_fresh_store_on_same_root(tmp_path) -> None:
    """A write must survive: a brand-new MailStore pointed at the same root (a
    process restart, in effect) sees the previously-written mail."""
    first = MailStore(root=tmp_path)
    await first.write(
        make_message(
            session_id="s1",
            kind="handoff",
            to_label="reception@prosper.health",
            subject="persisted",
            body="y",
            patient_name="A B",
            patient_phone="2025550000",
            ts=1000.0,
        )
    )
    reopened = MailStore(root=tmp_path)
    got = reopened.list_messages()
    assert len(got) == 1
    assert got[0].subject == "persisted"


def test_list_empty_when_no_writes(tmp_path) -> None:
    store = MailStore(root=tmp_path)
    assert store.list_messages() == []
