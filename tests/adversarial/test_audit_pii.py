"""Adversarial: raw caller PII in `transcript_turn` reaches the durable audit.

Finding F-007 (policy/PII-at-rest). The dispatcher carefully scrubs tool
arguments via `_redact_tool_args` before they hit the bus (phones masked, DOBs
and free-form `notes`/`reason`/`symptoms` replaced with placeholders). But
`transcript_turn` events carry the caller's raw utterance in `payload["text"]`,
which is *not* a `*_masked` field, so `events.validate_event` lets it through
untouched — and `AuditJSONLWriter.write` persists every event verbatim to
`data/audit/<session>.jsonl`.

Net effect: a caller who speaks "my number is 202-555-0142" or their DOB has
that PII written to a durable on-disk log, even though the equivalent value
passed as a tool arg would have been masked. This may be an intentional
demo-time choice (the dispatcher docstring says the operator "legitimately
needs the raw text"), but it is an undocumented inconsistency with the
tool-arg redaction policy and a PII-at-rest concern. Flagged for a product
decision, not silently changed.

These are plain (passing) tests that pin the *current* behaviour so the
contrast is unambiguous; if a redaction policy is later applied to transcript
text, they must be updated in lockstep.
"""

from __future__ import annotations

from prosper.console.audit import AuditJSONLWriter
from prosper.console.events import make_event


async def test_raw_phone_in_transcript_persists_to_audit_log(tmp_path) -> None:
    """A spoken phone number in a transcript_turn lands raw in the JSONL."""
    writer = AuditJSONLWriter(root=tmp_path)
    event = make_event(
        "transcript_turn",
        session_id="sess-pii-1",
        payload={"role": "user", "text": "my number is 202-555-0142", "turn_id": 1},
    )
    await writer.write(event)
    await writer.close()

    content = (tmp_path / "sess-pii-1.jsonl").read_text(encoding="utf-8")
    # The raw, unmasked phone digits are durably on disk.
    assert "202-555-0142" in content


async def test_raw_dob_in_transcript_persists_to_audit_log(tmp_path) -> None:
    """A spoken date of birth in a transcript_turn lands raw in the JSONL."""
    writer = AuditJSONLWriter(root=tmp_path)
    event = make_event(
        "transcript_turn",
        session_id="sess-pii-2",
        payload={"role": "user", "text": "I was born 1986-03-14", "turn_id": 2},
    )
    await writer.write(event)
    await writer.close()

    content = (tmp_path / "sess-pii-2.jsonl").read_text(encoding="utf-8")
    assert "1986-03-14" in content


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
