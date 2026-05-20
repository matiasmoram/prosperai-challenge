"""Engine + session factory.

Engine URL is taken from ``PROSPER_DB_URL`` (default: file-backed
``data/ehr.db``). ``get_engine(reset=True)`` rebuilds a fresh engine — used
by tests so each fixture gets isolated state.
"""
from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from prosper.ehr.models import Base

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _resolve_url() -> str:
    url = os.environ.get("PROSPER_DB_URL")
    if url:
        return url
    Path("data").mkdir(exist_ok=True)
    return "sqlite:///data/ehr.db"


def get_engine(*, reset: bool = False) -> Engine:
    global _engine, _SessionLocal
    if _engine is None or reset:
        _engine = create_engine(
            _resolve_url(),
            future=True,
            connect_args={"check_same_thread": False},
        )
        _SessionLocal = sessionmaker(
            bind=_engine, autoflush=False, autocommit=False, future=True
        )
    return _engine


def init_db() -> None:
    Base.metadata.create_all(get_engine())


def get_session() -> Session:
    assert _SessionLocal is not None, "call get_engine() first"
    return _SessionLocal()
