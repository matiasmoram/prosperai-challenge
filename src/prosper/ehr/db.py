"""Engine + session factory.

Two construction modes:

- ``make_engine(url)`` / ``make_session_factory(engine)`` — explicit, no
  globals. Each caller owns its own engine. Used by the parallel eval
  runner so concurrent scenarios get fully isolated databases.
- ``get_engine(reset=True)`` / ``get_session()`` / ``init_db()`` — legacy
  module-level singleton. URL comes from ``PROSPER_DB_URL`` (default:
  file-backed ``data/ehr.db``). Still used by ``scripts/seed.py``, unit
  tests, and the FastAPI module-level ``app = create_app()`` fallback.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from prosper.ehr.models import Base

__all__ = [
    "Base",
    "get_engine",
    "get_session",
    "init_db",
    "make_engine",
    "make_session_factory",
]

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


def make_engine(url: str | None = None) -> Engine:
    """Build a fresh ``Engine`` for the given URL (no globals touched).

    Perf wave 2 #3: enables parallel eval scenarios — each scenario owns
    its own SQLite file + engine, so the module-level singleton can't get
    clobbered between concurrent ``asyncio.gather`` branches.
    """
    resolved = url if url is not None else _resolve_url()
    engine = create_engine(
        resolved,
        future=True,
        connect_args={"check_same_thread": False},
    )
    if engine.url.drivername.startswith("sqlite"):
        event.listen(engine, "connect", _enable_sqlite_wal)
    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Build a Session factory for the given engine.

    ``expire_on_commit=False`` lets the FastAPI layer keep using ORM
    objects (e.g. ``appt.slot.provider.name``) after commit without
    forcing a re-SELECT. Combined with dropped ``session.refresh(p)``
    calls in repository.py this saves ~1.4 ms per write turn.
    """
    return sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        future=True,
    )


def get_engine(*, reset: bool = False) -> Engine:
    """Return the process-wide engine, building it on first use.

    Legacy singleton used by seed scripts and unit tests that monkeypatch
    ``PROSPER_DB_URL``. New code (parallel eval runner) should use
    :func:`make_engine` instead.
    """
    global _engine, _SessionLocal
    if _engine is None or reset:
        _engine = make_engine()
        _SessionLocal = make_session_factory(_engine)
    return _engine


def init_db(engine: Engine | None = None) -> None:
    """Create all tables on the given engine (or the singleton if None).

    Also runs an idempotent column-add migration for ``providers.specialty``
    so callers booting against a pre-specialty SQLite file don't have to
    wipe ``data/ehr.db`` by hand. SQLite only supports ``ADD COLUMN`` so
    drops/renames still need a manual reseed; that's documented in
    CLAUDE.md.
    """
    eng = engine if engine is not None else get_engine()
    Base.metadata.create_all(eng)
    _migrate_provider_specialty(eng)


def _migrate_provider_specialty(engine: Engine) -> None:
    """Add ``providers.specialty`` column if a pre-existing DB lacks it.

    No-op when the column already exists (fresh DBs, in-memory test DBs).
    Default value matches the ORM ``server_default`` so pre-existing rows
    get a sensible specialty without surfacing as NULL.
    """
    insp = inspect(engine)
    if "providers" not in insp.get_table_names():
        return
    columns = {c["name"] for c in insp.get_columns("providers")}
    if "specialty" in columns:
        return
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE providers ADD COLUMN specialty VARCHAR(80) "
                "NOT NULL DEFAULT 'General Practice'"
            )
        )


def get_session() -> Session:
    assert _SessionLocal is not None, "call get_engine() first"
    return _SessionLocal()
