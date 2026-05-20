"""Typed Result discriminated union for tool handlers and integrations."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Literal, TypeGuard, TypeVar, Union

T = TypeVar("T")


@dataclass(frozen=True)
class Ok(Generic[T]):
    value: T
    kind: Literal["ok"] = "ok"


@dataclass(frozen=True)
class Err:
    code: str
    message: str
    retryable: bool
    kind: Literal["err"] = "err"


Result = Union[Ok[T], Err]


def is_ok(r: Result[T]) -> TypeGuard[Ok[T]]:
    return r.kind == "ok"


def is_err(r: Result[T]) -> TypeGuard[Err]:
    return r.kind == "err"
