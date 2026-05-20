from evals.types import Scenario, StateExpectation


def test_state_expectation_defaults() -> None:
    s = StateExpectation()
    assert s.patient_count_delta == 0
    assert s.expected_tool_call_codes == []


def test_scenario_constructible() -> None:
    s = Scenario(
        name="x",
        tags=frozenset({"happy"}),
        persona="say hi",
        setup=lambda session: None,
        expected_state=StateExpectation(),
        judge_criteria=["bot greeted the caller"],
    )
    assert s.name == "x"
    assert "happy" in s.tags
