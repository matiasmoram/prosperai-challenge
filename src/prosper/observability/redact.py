"""PII redaction for log lines.

Healthcare-adjacent: phone numbers, dates of birth, and email addresses are
PHI when associated with a patient. ``redact_pii`` is a thin best-effort
filter applied to anything that goes through ``logger.info`` / ``.warning``
for user-spoken or bot-spoken text. Not a substitute for a proper de-id
pipeline (NER + clinical context); good enough to keep tail-end log
operators from seeing the raw caller's contact details in `journalctl`.

Patterns covered (US-centric, voice-agent realistic):
- Phone numbers (10+ digits, with optional + - ( ) and spaces)
- Date-of-birth-like strings (YYYY-MM-DD, MM/DD/YYYY, M-D-YY)
- Email addresses

Names are NOT redacted by regex — too many false positives on common words.
Names are partially masked via ``mask_name`` which the caller invokes
explicitly on known name fields.
"""

from __future__ import annotations

import re

# Phone: a contiguous run of digits and the common voice-agent separators
# +, -, space, ., (, ). UUIDs are stashed BEFORE the phone pass runs (see
# ``redact_pii``), so the matcher never sees their digit-rich interior — we
# therefore only need a digit boundary (not a hex one) to avoid splitting a
# longer digit run. The earlier hex lookarounds also skipped any phone glued
# to a word ending in a-f ("ref2025550142"), leaking it (audit F-003); a
# plain digit boundary fixes that. ``_looks_like_phone`` then re-checks the
# total digit count is in the realistic phone range (10-15).
_PHONE_RE = re.compile(
    r"""
    (?<![0-9])
    \+?[\d\s\-\(\)\.]{9,25}
    (?![0-9])
    """,
    re.VERBOSE,
)


def _looks_like_phone(candidate: str) -> bool:
    digits = sum(ch.isdigit() for ch in candidate)
    return 10 <= digits <= 15


# DOB-ish: matches ISO (YYYY-MM-DD), US (M/D/YYYY or MM/DD/YY), and dotted.
_DOB_RE = re.compile(
    r"""
    (?<!\d)
    (?:
        \d{4}[-/.]\d{1,2}[-/.]\d{1,2}     # 1990-04-03
      | \d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}   # 4/3/1990 or 04-03-90
    )
    (?!\d)
    """,
    re.VERBOSE,
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# UUIDs are hex-only by spec; we use this to skip phone matches that fall
# inside a UUID. We restore each UUID verbatim after the phone pass.
_UUID_RE = re.compile(
    r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b"
)


def redact_pii(text: str) -> str:
    """Mask phone numbers, DOBs, and emails in a free-form string.

    Idempotent — running twice produces the same output (the masks contain
    no PII shapes).
    """
    if not text:
        return text
    # Stash UUIDs first so the phone matcher doesn't eat their digit-rich
    # interior. We swap them back at the end. Order is stable because UUIDs
    # don't overlap with each other.
    uuids: list[str] = []

    def _stash_uuid(m: re.Match[str]) -> str:
        uuids.append(m.group(0))
        return f"\x00UUID{len(uuids) - 1}\x00"

    out = _UUID_RE.sub(_stash_uuid, text)
    out = _EMAIL_RE.sub("[EMAIL]", out)
    out = _DOB_RE.sub("[DOB]", out)
    out = _PHONE_RE.sub(
        lambda m: "[PHONE]" if _looks_like_phone(m.group(0)) else m.group(0),
        out,
    )
    for i, original in enumerate(uuids):
        out = out.replace(f"\x00UUID{i}\x00", original)
    return out


def mask_name(name: str) -> str:
    """First-letter + asterisks. ``"Maria Lopez" -> "M*** L****"``.

    Used when we *know* a string is a person's name (e.g. structured field
    from the EHR). Free-form text uses ``redact_pii`` only.
    """
    if not name:
        return name
    parts = name.split()
    # ``max(1, len-1)`` guarantees at least one mask character per part, even
    # for single-letter names ("A B" → "A* B*"). Without it an all-initials
    # name produced no mask char and the operator-console event validator
    # rejected it, silently dropping the patient_identified event (audit F-004).
    return " ".join(p[0] + "*" * max(1, len(p) - 1) for p in parts)


def mask_phone(phone: str) -> str:
    """Mask all but the leading sign and trailing four digits.

    ``"+12025550142" -> "+1***0142"``. Used when emitting structured phone
    fields to the operator console or audit log: the suffix is enough for
    a clinician to match a known caller without exposing the full number.

    The operator console's bus validation rejects any field name ending
    in ``_masked`` that lacks a mask character or carries a 7+ digit run,
    so this helper is the canonical source of masked phones for events.
    """
    if not phone:
        return phone
    digits_only = "".join(ch for ch in phone if ch.isdigit())
    if len(digits_only) < 4:
        # Fewer than 4 digits: nothing useful to expose, mask everything.
        return "*" * max(len(phone), 1)
    leading_plus = "+" if phone.lstrip().startswith("+") else ""
    return f"{leading_plus}**{digits_only[-4:]}"
