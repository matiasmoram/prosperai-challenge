"""Identity-resolution helpers for the IDENTIFY_PATIENT state.

Pure functions, no I/O — the dispatcher calls these to classify a
``find_patient_*`` result and to build the caller-facing disambiguation
prompt when more than one patient matches. Kept separate from
``dispatcher.py`` so the matching policy is unit-testable in isolation
and the dispatcher stays a thin state machine.

The asyncio speculative-prefetch design (firing find + availability in
parallel while the caller is still talking) is documented in
``docs/research/speculative_race.md`` and deferred — on a local SQLite
EHR the round-trip is ~30 ms, so the prefetch saves little against the
risk of cancelling in-flight tasks in the dispatcher core. ``next_n_business_days``
lives here ready for that work when the EHR moves remote.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

# A single name+DOB match at or above this similarity is treated as a
# confident hit — no read-back-to-confirm gymnastics. Below it (but above
# the EHR's 0.85 fuzzy floor) the caller's spoken name and the stored name
# diverge enough that we surface the candidate for confirmation.
EXACT_THRESHOLD: float = 0.97


def classify_find_result(patients: list[dict[str, Any]], *, fuzzy: bool) -> str:
    """Classify a patient-lookup result into an identity outcome.

    ``fuzzy`` is True for ``find_patient_by_name_dob`` (similarity-scored)
    and False for ``find_patient_by_phone`` (exact phone match — any hit
    is exact). Returns one of:
    ``"found_exact"``, ``"found_fuzzy_single"``, ``"found_fuzzy_multiple"``,
    ``"no_match"``.
    """
    if not patients:
        return "no_match"
    if len(patients) > 1:
        return "found_fuzzy_multiple"
    if not fuzzy:
        return "found_exact"
    similarity = float(patients[0].get("similarity", 1.0))
    return "found_exact" if similarity >= EXACT_THRESHOLD else "found_fuzzy_single"


def build_disambiguation_message(candidates: list[dict[str, Any]]) -> str:
    """Build a UUID-free, numbered candidate list for the LLM to read back.

    Uses the same ``[1]``/``[2]`` handle convention as ``_redact_for_llm``
    so the caller can pick a number and the dispatcher can map it back.
    Never includes patient ids.
    """
    lines = [
        f"[{i + 1}] {c.get('first_name', '')} {c.get('last_name', '')} "
        f"(DOB {c.get('dob', '?')})".strip()
        for i, c in enumerate(candidates)
    ]
    return "multiple patients match — ask the caller which one before proceeding: " + "; ".join(
        lines
    )


def next_n_business_days(from_date: date, n: int = 4) -> list[date]:
    """Return the next ``n`` Mon-Fri dates after (not including) ``from_date``."""
    out: list[date] = []
    cursor = from_date
    while len(out) < n:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:  # 0=Mon … 4=Fri
            out.append(cursor)
    return out
