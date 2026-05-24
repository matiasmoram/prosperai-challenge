"""Unit tests for tester/personas.py.

Covers the placeholder-validator that guards generated personas against
unfilled template brackets (e.g. ``[Your Name]``, ``[Month, Day, Year]``).
"""

from __future__ import annotations

import pytest

from tester.personas import WORLDS, _persona_from_dict, has_unfilled_placeholders

# ---------------------------------------------------------------------------
# has_unfilled_placeholders
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        # Clear placeholders — must be flagged.
        ("[Your Name]", True),
        ("[Month, Day, Year]", True),
        ("[Phone Number]", True),
        ("[DOB]", True),
        ("[Date of Birth]", True),
        ("[PHONE]", True),
        ("[Your Date of Birth]", True),
        ("[First Name] [Last Name]", True),
        # Safe short brackets that appear in legitimate prompts.
        ("[1]", False),  # numbered list item — starts with digit, not capital letter
        ("[2]", False),  # ditto
        # Normal text with no brackets.
        ("You are Ada Lovelace, DOB December 10 1990, phone 202-555-0100.", False),
        ("", False),
        # Mixed: real text containing a placeholder.
        ("Your name is [Your Name] and your DOB is March 3rd 1985.", True),
        # Concrete replacement — no placeholder.
        ("Your name is Harold Jenkins and your DOB is March 3rd 1948.", False),
    ],
)
def test_has_unfilled_placeholders(text: str, expected: bool) -> None:
    assert has_unfilled_placeholders(text) == expected


# ---------------------------------------------------------------------------
# _persona_from_dict — rejection of placeholder-bearing personas
# ---------------------------------------------------------------------------


def _valid_dict(**overrides: str) -> dict:
    """Minimal valid persona dict."""
    base = {
        "name": "test-persona",
        "world": "new",
        "goal": "Book a morning appointment.",
        "prompt": (
            "You are Harold Jenkins, DOB March 3rd 1948, phone 555-0142. "
            "You want a morning appointment tomorrow."
        ),
    }
    base.update(overrides)
    return base


def test_valid_persona_accepted() -> None:
    p = _persona_from_dict(_valid_dict())
    assert p is not None
    assert p.name == "test-persona"


def test_placeholder_in_prompt_rejected() -> None:
    d = _valid_dict(prompt="You are [Your Name], DOB [Date of Birth], phone 555-0142.")
    assert _persona_from_dict(d) is None


def test_placeholder_in_goal_rejected() -> None:
    d = _valid_dict(goal="Cancel the appointment for [Patient Name].")
    assert _persona_from_dict(d) is None


def test_placeholder_in_both_rejected() -> None:
    d = _valid_dict(
        prompt="You are [Your Name].",
        goal="Book for [Date of Appointment].",
    )
    assert _persona_from_dict(d) is None


def test_concrete_name_accepted() -> None:
    # Bracket that is NOT a placeholder (numbered list) must not be flagged.
    d = _valid_dict(
        prompt="Options: [1] book [2] cancel. You pick [1]. DOB March 3rd 1948, phone 555-0142."
    )
    # [1] and [2] are short — they don't match the Capital-word regex.
    assert _persona_from_dict(d) is not None


def test_missing_world_rejected() -> None:
    d = _valid_dict(world="nonexistent_world")
    assert _persona_from_dict(d) is None


def test_missing_name_rejected() -> None:
    d = _valid_dict(name="")
    assert _persona_from_dict(d) is None


def test_missing_prompt_rejected() -> None:
    d = _valid_dict(prompt="")
    assert _persona_from_dict(d) is None


def test_all_worlds_still_recognised() -> None:
    """Sanity: every world in WORLDS can be used in a valid persona."""
    for world_name in WORLDS:
        d = _valid_dict(world=world_name)
        assert _persona_from_dict(d) is not None, f"world {world_name!r} incorrectly rejected"
