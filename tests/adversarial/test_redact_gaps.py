"""Adversarial: PII-redaction gaps in `observability/redact.py`.

Findings F-003 (phone redaction misses) and F-004 (mask_name single-letter).
"""

from __future__ import annotations

from prosper.console.events import ConsoleEvent, validate_event
from prosper.observability.redact import mask_name, redact_pii


def test_ten_digit_phone_is_redacted() -> None:
    """Baseline: the common case works — keeps the suite honest."""
    assert "[PHONE]" in redact_pii("phone 202 555 0142")


def test_hex_glued_phone_is_redacted() -> None:
    """A 10-digit number immediately after 'ref' (ends in hex 'f') masks (F-003 fixed)."""
    out = redact_pii("ref2025550142 today")
    assert "[PHONE]" in out, f"phone leaked: {out!r}"


def test_seven_digit_local_number_is_not_redacted_documented_limitation() -> None:
    """Documents the deliberate 10-digit floor (module docstring).

    A US local 7-digit number ("555-0142") is below the floor and is NOT
    masked. This is a known PII gap for area-code-less numbers; pinned here
    so anyone tightening redaction sees the trade-off spelled out.
    """
    assert "[PHONE]" not in redact_pii("call me at 555-0142")


def test_all_single_letter_name_still_masks() -> None:
    """`mask_name` always emits at least one mask character (F-004 fixed)."""
    masked = mask_name("A B")
    assert any(ch == "*" for ch in masked), f"no mask char in {masked!r}"
    assert masked == "A* B*"


def test_single_letter_name_now_passes_event_validation() -> None:
    """End-to-end: a `patient_identified` event built from a two-single-letter
    name now validates (the masked name carries a mask char), so the event is
    no longer silently dropped (F-004 fixed)."""
    event = ConsoleEvent(
        type="patient_identified",
        ts=0.0,
        session_id="s",
        payload={
            "name_masked": mask_name("A B"),  # → "A* B*"
            "dob_year": 1990,
            "phone_masked": "+1***0142",
            "id_internal": "uuid",
        },
    )
    validate_event(event)  # must not raise
