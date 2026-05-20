"""Throwaway fuzz probe for the EHR API + dispatcher."""
from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8")

from prosper.ehr.repository import normalize_name, normalize_phone


def main() -> None:
    print("--- Topic 1: Unicode in names ---")
    samples_names = [
        "Mary‎Sue",      # LRM (Left-to-Right Mark)
        "Mary‏Sue",      # RLM
        "Mary‮Sue",      # RLO (Right-to-Left Override)
        "\U0001F600 Smith",  # emoji
        "مَريم Smith",  # Arabic
        "ﬁona",          # fi ligature -> fi after NFKD
        "Mr. Dr. Prof. Smith",
        "   ",
        " Smith",
        "AAA\U0010FFFF",
        "A" * 200,
        "‎",             # only RLM/LRM
        "‎‏",       # bidi only
        "Smith‎",
    ]
    for raw in samples_names:
        try:
            result = normalize_name(raw)
            print(f"  ok: {raw!r} -> {result!r}")
        except Exception as e:  # noqa: BLE001
            print(f"  CRASH: {raw!r}: {type(e).__name__}: {e}")

    print()
    print("--- Topic 2: Phone normalisation edge cases ---")
    samples_phone = [
        "",
        "+",
        "abcdef",
        "+abcdef",
        "0000000000",
        "1" * 30,
        "0",
        "+0",
        "1234567",
        "1" * 11,
        "01234567890",
        "+1 (555) 123-4567 ext 99",
        "NaN",
        "‎+15551234567",
        "++1234567",
        "+1­555­1234567",  # soft hyphen
    ]
    for raw in samples_phone:
        try:
            result = normalize_phone(raw)
            print(f"  ok: {raw!r} -> {result!r}")
        except Exception as e:  # noqa: BLE001
            print(f"  CRASH: {raw!r}: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
