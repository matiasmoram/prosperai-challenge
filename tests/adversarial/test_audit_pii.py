"""Adversarial: caller PII in `transcript_turn` is redacted in the durable audit.

Finding F-007 (FIXED). `transcript_turn` events carry the caller's (and bot's)
raw utterance in `payload["text"]`, which is not a `*_masked` field, so the bus
validator never inspects it. `AuditJSONLWriter.write` now passes every event
through `_redact_event_for_audit`, which runs `redact_pii` over
`transcript_turn` text before persisting — so a spoken phone number / DOB is
masked on disk. The live SSE stream still receives the raw text (the operator
legitimately needs it); only the durable JSONL is scrubbed.
"""

from __future__ import annotations

from prosper.console.audit import AuditJSONLWriter
from prosper.console.events import make_event


async def test_phone_in_transcript_is_redacted_in_audit_log(tmp_path) -> None:
    """F-007 fixed: a spoken phone number in a transcript_turn is masked on disk."""
    writer = AuditJSONLWriter(root=tmp_path)
    event = make_event(
        "transcript_turn",
        session_id="sess-pii-1",
        payload={"role": "user", "text": "my number is 202-555-0142", "turn_id": 1},
    )
    await writer.write(event)
    await writer.close()

    content = (tmp_path / "sess-pii-1.jsonl").read_text(encoding="utf-8")
    assert "202-555-0142" not in content
    assert "[PHONE]" in content


async def test_dob_in_transcript_is_redacted_in_audit_log(tmp_path) -> None:
    """F-007 fixed: a spoken DOB in a transcript_turn is masked on disk."""
    writer = AuditJSONLWriter(root=tmp_path)
    event = make_event(
        "transcript_turn",
        session_id="sess-pii-2",
        payload={"role": "user", "text": "I was born 1986-03-14", "turn_id": 2},
    )
    await writer.write(event)
    await writer.close()

    content = (tmp_path / "sess-pii-2.jsonl").read_text(encoding="utf-8")
    assert "1986-03-14" not in content
    assert "[DOB]" in content


def test_transcript_turn_text_is_not_a_masked_field() -> None:
    """Root of F-007: `text` does not end in `_masked`, so the bus safety net
    (which only inspects `*_masked` fields) never inspects it."""
    event = make_event(
        "transcript_turn",
        session_id="s",
        payload={"role": "user", "text": "ssn 123-45-6789", "turn_id": 1},
    )
    # validate_event ran inside make_event without raising, despite the PII —
    # because no payload key ends in "_masked".
    assert not any(k.endswith("_masked") for k in event.payload)
