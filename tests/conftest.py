"""Shared pytest fixtures for unit tests.

The ``seeded_ehr_client`` fixture (and its aliases) replaces the near-
identical EHRClient-with-tomorrow-slots fixtures that were copy-pasted into
``test_ehr_client.py``, ``test_tools.py``, ``test_dispatcher.py``,
``test_dispatcher_gaps.py``, and ``test_tools_errors.py``. Behaviour is
identical: a fresh SQLite EHR mounted via ASGITransport, seeded with one
provider and two consecutive 30-minute slots starting at 10:00 UTC
tomorrow.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from prosper.ehr.api import create_app
from prosper.ehr.db import get_engine, init_db
from prosper.ehr.models import Provider, Slot
from prosper.ehr_client import EHRClient


@pytest.fixture
def seeded_ehr_client(tmp_path, monkeypatch) -> EHRClient:
    """Hermetic EHR app seeded with one provider + two tomorrow slots.

    ASGI-mounted httpx client, file-backed SQLite under ``tmp_path``.
    """
    monkeypatch.setenv("PROSPER_DB_URL", f"sqlite:///{tmp_path / 'ehr.db'}")
    get_engine(reset=True)
    init_db()
    app = create_app()
    with Session(get_engine()) as session:
        prov = Provider(name="Dr. Patel", timezone="UTC")
        session.add(prov)
        session.commit()
        start = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )
        for i in range(2):
            session.add(
                Slot(
                    provider_id=prov.id,
                    start_at=start + timedelta(minutes=30 * i),
                    end_at=start + timedelta(minutes=30 * (i + 1)),
                )
            )
        session.commit()
    return EHRClient.for_asgi_app(app)


# Aliases preserve the original fixture names used across test modules.
# A trivial wrapper costs nothing and keeps the diff in each test file
# limited to deleting the local fixture; the parameter names stay stable.
@pytest.fixture
def asgi_client(seeded_ehr_client: EHRClient) -> EHRClient:
    return seeded_ehr_client


@pytest.fixture
def client(seeded_ehr_client: EHRClient) -> EHRClient:
    return seeded_ehr_client


@pytest.fixture
def ehr_client(seeded_ehr_client: EHRClient) -> EHRClient:
    return seeded_ehr_client
