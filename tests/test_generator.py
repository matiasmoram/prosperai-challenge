"""Unit tests for the adversarial scenario generator (FUTURE.md 2.1).

The live run of generated scenarios needs an API key; the *generation logic*
is deterministic and fully testable offline. These tests pin that each rule
produces a valid, distinct, correctly-shaped Scenario derived from the base.
"""

from __future__ import annotations

from evals.generator import (
    ALL_RULES,
    MID_FLOW_ABORT,
    NAME_INJECTION,
    ScenarioTemplate,
    generate,
    generate_all,
)
from evals.types import Scenario, StateExpectation


def _base() -> Scenario:
    return Scenario(
        name="demo_book",
        tags=frozenset({"happy"}),
        persona="You are a caller who wants to book.",
        setup=lambda _session: None,
        expected_state=StateExpectation(
            patient_count_delta=1,
            active_appointment_count_delta=1,
            expected_terminal_state="END",
            expected_tool_call_codes=["create_appointment"],
        ),
        judge_criteria=["the booking completed"],
        max_turns=12,
    )


def test_generate_returns_base_plus_one_per_rule() -> None:
    tpl = ScenarioTemplate(base=_base(), rules=ALL_RULES)
    out = generate(tpl)
    assert len(out) == len(ALL_RULES) + 1
    assert out[0].name == "demo_book"  # base first, unchanged name


def test_variant_names_are_unique_and_suffixed() -> None:
    tpl = ScenarioTemplate(base=_base(), rules=ALL_RULES)
    out = generate(tpl)
    names = [s.name for s in out]
    assert len(names) == len(set(names)), "variant names must be unique"
    for rule in ALL_RULES:
        assert f"demo_book{rule.name_suffix}" in names


def test_perturbation_does_not_mutate_base() -> None:
    base = _base()
    original_persona = base.persona
    tpl = ScenarioTemplate(base=base, rules=ALL_RULES)
    generate(tpl)
    assert base.persona == original_persona, "rules must not mutate the base scenario"


def test_every_variant_extends_the_base_persona() -> None:
    tpl = ScenarioTemplate(base=_base(), rules=ALL_RULES)
    for variant in generate(tpl)[1:]:
        assert variant.persona.startswith("You are a caller who wants to book."), (
            "variant persona should extend, not replace, the base persona"
        )
        assert len(variant.persona) > len(_base().persona)


def test_mid_flow_abort_zeroes_writes_and_forbids_write_tools() -> None:
    variant = MID_FLOW_ABORT.apply(_base())
    e = variant.expected_state
    assert e.active_appointment_count_delta == 0
    assert e.cancelled_appointment_count_delta == 0
    assert e.expected_terminal_state == "END"
    assert "create_appointment" in e.forbidden_tool_calls
    assert "cancel_appointment" in e.forbidden_tool_calls


def test_name_injection_adds_literal_and_no_cancel_criteria() -> None:
    variant = NAME_INJECTION.apply(_base())
    joined = " ".join(variant.judge_criteria).lower()
    assert "literal" in joined
    assert "cancel" in joined
    assert "injection" in variant.tags


def test_generate_all_produces_variants_when_base_present() -> None:
    """generate_all reads evals/scenarios.py; new_patient_books is a known base."""
    variants = generate_all()
    # At least the four rules applied to new_patient_books.
    assert len(variants) >= len(ALL_RULES)
    assert all(isinstance(s, Scenario) for s in variants)
    assert all("__" in s.name for s in variants), "generated variants carry a rule suffix"
