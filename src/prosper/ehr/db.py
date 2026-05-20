"""Engine + session factory.

Engine URL is taken from ``PROSPER_DB_URL`` (default: file-backed
``data/ehr.db``). ``get_engine(reset=True)`` rebuilds a fresh engine — used
by tests so each fixture gets isolated state.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
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


def _enable_sqlite_wal(dbapi_connection: object, _: object) -> None:
    """Enable WAL + sane busy_timeout on every SQLite connection.

    Perf wave 2 #1: under 20-way write contention, default journal_mode=DELETE
    serialises writes through a global lock (29.7 ms/req). WAL allows readers
    and writers to coexist; busy_timeout=5000 ms makes SQLite retry briefly
    on a transient lock instead of immediately raising. Net measured: 29.7 →
    ~8-10 ms/req under contention. Unlocks safe parallel eval scenarios.
    """
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA synchronous=NORMAL")  # WAL + NORMAL is safe + fast
    cursor.close()


def get_engine(*, reset: bool = False) -> Engine:
    global _engine, _SessionLocal
    if _engine is None or reset:
        _engine = create_engine(
            _resolve_url(),
            future=True,
            connect_args={"check_same_thread": False},
        )
        # Apply WAL pragmas to every new SQLite connection (file-backed only;
        # in-memory engines used by tests are not file-locked and skip these
        # silently when the pragma is harmless).
        if _engine.url.drivername.startswith("sqlite"):
            event.listen(_engine, "connect", _enable_sqlite_wal)
        # Perf wave 2 #4: expire_on_commit=False lets the FastAPI layer keep
        # using ORM objects (e.g. ``appt.slot.provider.name`` for the JSON
        # response) after commit without forcing a re-SELECT. Combined with
        # the dropped ``session.refresh(p)`` calls in repository.py this
        # saves ~1.4 ms per write turn.
        _SessionLocal = sessionmaker(
            bind=_engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            future=True,
        )
    return _engine


def init_db() -> None:
    Base.metadata.create_all(get_engine())


def get_session() -> Session:
    assert _SessionLocal is not None, "call get_engine() first"
    return _SessionLocal()
