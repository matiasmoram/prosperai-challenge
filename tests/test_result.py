from prosper.result import Err, Ok, Result, is_err, is_ok


def test_ok_holds_value() -> None:
    r: Result[int] = Ok(value=42)
    assert is_ok(r) and not is_err(r)
    assert r.kind == "ok"
    assert r.value == 42


def test_err_holds_code_message_retryable() -> None:
    r: Result[int] = Err(code="patient_not_found", message="no match for +1...", retryable=False)
    assert is_err(r) and not is_ok(r)
    assert r.code == "patient_not_found"
    assert r.retryable is False


def test_pattern_match() -> None:
    def describe(r: Result[int]) -> str:
        match r:
            case Ok(value=v):
                return f"ok:{v}"
            case Err(code=c):
                return f"err:{c}"
        return "unreachable"

    assert describe(Ok(value=1)) == "ok:1"
    assert describe(Err(code="x", message="y", retryable=True)) == "err:x"
