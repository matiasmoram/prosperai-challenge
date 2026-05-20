"""DOB parsing edge cases."""
from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8")

from prosper.tools import _parse_dob

samples = [
    "1900-01-01",
    "2099-12-31",
    "today",
    "tomorrow",
    "year zero",
    "year 0",
    "0000-00-00",
    "0001-01-01",
    "99999-01-01",
    "2026-02-30",
    "2026-13-01",
    "2026-05-19",  # today
    "9999-12-31",
    "April third nineteen ninety-two",
    "",
    "   ",
    None,
    42,
    "1.2.3",
    "garbage that fuzzy=True will dig digits from like 1990",
]
for raw in samples:
    try:
        r = _parse_dob(raw)
        print(f"  {raw!r} -> kind={r.kind} value={getattr(r, 'value', None)} code={getattr(r, 'code', None)}")
    except Exception as e:  # noqa: BLE001
        print(f"  CRASH {raw!r}: {type(e).__name__}: {e}")
