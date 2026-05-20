import json

from prosper.observability.timing import TimingCollector


def test_records_and_aggregates_p50_p95() -> None:
    c = TimingCollector()
    for ms in [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]:
        c.record(phase="llm", duration_ms=ms, state="GREETING")
    summary = c.summary()
    assert summary["llm"]["count"] == 10
    assert 50 <= summary["llm"]["p50"] <= 60
    assert summary["llm"]["p95"] >= 90


def test_summary_groups_by_phase_only() -> None:
    c = TimingCollector()
    c.record(phase="llm", duration_ms=100, state="A")
    c.record(phase="tool:find_patient_by_phone", duration_ms=15, state="A")
    s = c.summary()
    assert set(s.keys()) == {"llm", "tool:find_patient_by_phone"}


def test_format_table_renders_human_readable() -> None:
    c = TimingCollector()
    for ms in [10, 20, 30]:
        c.record(phase="llm", duration_ms=ms, state="A")
    text = c.format_table()
    assert "llm" in text and "p50" in text


def test_record_emits_session_and_turn_ids_when_provided(capsys) -> None:
    """Span JSON must carry session_id + turn_id when passed — that's the
    join key against the EHR access log (which sees the same id via
    X-Request-Id). Backwards-compat: keys are omitted when None."""
    c = TimingCollector()
    c.record(
        phase="llm",
        duration_ms=12.3,
        state="GREETING",
        session_id="abc-123",
        turn_id=2,
    )
    line = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload == {
        "evt": "span",
        "phase": "llm",
        "state": "GREETING",
        "duration_ms": 12.3,
        "session_id": "abc-123",
        "turn_id": 2,
    }


def test_record_omits_ids_when_not_provided(capsys) -> None:
    c = TimingCollector()
    c.record(phase="llm", duration_ms=5.0, state="A")
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "session_id" not in payload
    assert "turn_id" not in payload
