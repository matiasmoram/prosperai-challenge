"""Unit tests for the log-line PII redactor."""

from __future__ import annotations

import pytest

from prosper.observability.redact import mask_name, redact_pii


@pytest.mark.parametrize(
    "raw,expected_substr",
    [
        ("my phone is 555-123-4567", "[PHONE]"),
        ("call me at +1 (555) 123-4567 today", "[PHONE]"),
        ("born 1990-04-03", "[DOB]"),
        ("dob 4/3/1990", "[DOB]"),
        ("email me at jane.doe@example.com please", "[EMAIL]"),
    ],
)
def test_redacts_pii(raw: str, expected_substr: str) -> None:
    out = redact_pii(raw)
    assert expected_substr in out
    # Ensure the original PII is gone
    if "phone" in raw.lower():
        assert "555" not in out
    if "1990" in raw and "email" not in raw:
        assert "1990" not in out
    if "@" in raw:
        assert "@" not in out


def test_redact_idempotent() -> None:
    once = redact_pii("call 555-123-4567")
    twice = redact_pii(once)
    assert once == twice


def test_redact_keeps_uuids_intact() -> None:
    # 36-char UUIDs must not be mistaken for phone numbers (digit count
    # alone would qualify them — the hyphen layout is wrong though).
    uuid = "a1b2c3d4-1111-4222-8333-abcdef012345"
    assert uuid in redact_pii(f"slot {uuid}")


def test_redact_empty() -> None:
    assert redact_pii("") == ""


def test_mask_name() -> None:
    assert mask_name("Maria Lopez") == "M**** L****"
    assert mask_name("X") == "X"
    assert mask_name("") == ""
