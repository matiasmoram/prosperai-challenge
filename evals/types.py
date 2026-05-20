"""Eval-suite primitives: Scenario and StateExpectation dataclasses."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy.orm import Session


@dataclass
class StateExpectation:
    """Deterministic post-conditions checked against the EHR after a run."""

    patient_count_delta: int = 0
    active_appointment_count_delta: int = 0
    cancelled_appointment_count_delta: int = 0
    expected_terminal_state: str | None = None
    expected_tool_call_codes: list[str] = field(default_factory=list)
    forbidden_tool_calls: list[str] = field(default_factory=list)


@dataclass
class Scenario:
    """Declarative test case — name, persona prompt, DB seed, expectations."""

    name: str
    tags: frozenset[str]
    persona: str
    setup: Callable[[Session], None]
    expected_state: StateExpectation
    judge_criteria: list[str]
    max_turns: int = 12


@dataclass
class ScenarioResult:
    name: str
    state_pass: bool
    state_reasons: list[str]
    judge_pass: bool
    judge_justification: str
    turns: int
    duration_ms: float
    transcript: list[dict]
    timing_summary: dict[str, dict[str, float]] = field(default_factory=dict)
    cached_prompt_tokens: int = 0
    prompt_tokens: int = 0

    @property
    def overall_pass(self) -> bool:
        return self.state_pass and self.judge_pass

    @property
    def cache_hit_ratio(self) -> float:
        """Fraction of prompt tokens served from OpenAI's prompt cache."""
        if self.prompt_tokens == 0:
            return 0.0
        return self.cached_prompt_tokens / self.prompt_tokens
