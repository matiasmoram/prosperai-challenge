"""Durable, full-PII outbound-mail records for the front-desk surface.

A single unified SQLite store at ``<root>/mail.db`` (one ``mail`` table) — NOT
one file per session. It reads as one coherent inbox across every call and
survives process restarts, "like part of the DB". Unlike the operator console,
records carry the patient's real name + phone — a callback or a confirmation is
useless masked. Deliberately a different trust tier: the ``/frontdesk`` surface
that reads it is staff-only (behind auth in prod, loopback in the demo). Two
channels share one record via ``kind``. See spec §7.

This store is intentionally SEPARATE from the EHR database (its own file, its
own connection, no shared models/engine): mail is a distinct full-PII trust
tier and must not be coupled to clinical data.

Because rows are keyed by an autoincrement id (not a filename derived from the
session id), the old per-session-file path-traversal concern is gone entirely —
``session_id`` is just an ordinary text column and can hold any value safely.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

_DEFAULT_ROOT_NAME: Final[str] = "data/mail"
_DB_FILENAME: Final[str] = "mail.db"

_CREATE_TABLE: Final[str] = """
CREATE TABLE IF NOT EXISTS mail (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL    NOT NULL,
    session_id    TEXT    NOT NULL,
    kind          TEXT    NOT NULL,
    to_label      TEXT    NOT NULL,
    subject       TEXT    NOT NULL,
    body          TEXT    NOT NULL,
    patient_name  TEXT    NOT NULL,
    patient_phone TEXT    NOT NULL,
    category      TEXT    NOT NULL DEFAULT ''
)
"""


def _resolve_default_root() -> Path:
    """Compute the default mail root, honouring ``PROSPER_MAIL_ROOT``."""
    override = os.environ.get("PROSPER_MAIL_ROOT")
    return Path(override) if override else Path(_DEFAULT_ROOT_NAME)


@dataclass(frozen=True, slots=True)
class MailMessage:
    """One simulated outbound message. Full PII — staff tier only."""

    ts: float
    session_id: str
    kind: str  # "booking_confirmation" | "handoff" | "bot_failed"
    to_label: str
    subject: str
    body: str
    patient_name: str
    patient_phone: str
    category: str = ""  # handoff / bot_failed only; "" for booking_confirmation

    def to_json(self) -> str:
        """Serialise to one JSON line."""
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> MailMessage:
        """Inverse of ``to_json``."""
        d = json.loads(raw)
        return cls(
            ts=float(d["ts"]),
            session_id=str(d["session_id"]),
            kind=str(d["kind"]),
            to_label=str(d["to_label"]),
            subject=str(d["subject"]),
            body=str(d["body"]),
            patient_name=str(d["patient_name"]),
            patient_phone=str(d["patient_phone"]),
            category=str(d.get("category", "")),
        )


def make_message(
    *,
    session_id: str,
    kind: str,
    to_label: str,
    subject: str,
    body: str,
    patient_name: str,
    patient_phone: str,
    category: str = "",
    ts: float | None = None,
) -> MailMessage:
    """Build a ``MailMessage``; ``ts`` defaults to ``time.time()``."""
    return MailMessage(
        ts=time.time() if ts is None else ts,
        session_id=session_id,
        kind=kind,
        to_label=to_label,
        subject=subject,
        body=body,
        patient_name=patient_name,
        patient_phone=patient_phone,
        category=category,
    )


class MailStore:
    """Unified, durable SQLite store of outbound mail (one ``mail`` table).

    All mail across all sessions lives in a single ``<root>/mail.db`` so the
    front desk reads one coherent inbox. ``write`` is async (the blocking
    sqlite3 call is offloaded to a thread so it never stalls the bot's event
    loop); a fresh connection per write keeps concurrent fire-and-forget writes
    from different async tasks safe and committed durably. Reads are synchronous
    — the SPA's 2-second poll is small and infrequent, so async overhead isn't
    worth it.
    """

    def __init__(self, root: Path | None = None) -> None:
        """Initialise with a target root dir holding ``mail.db`` (default ``data/mail/``)."""
        self._root: Path = root if root is not None else _resolve_default_root()

    @property
    def root(self) -> Path:
        """Mail root directory holding ``mail.db`` (read-only outside tests)."""
        return self._root

    @property
    def _db_path(self) -> Path:
        """Absolute path to the unified mail database file."""
        return self._root / _DB_FILENAME

    def _connect(self) -> sqlite3.Connection:
        """Open a connection to ``mail.db``, creating the dir + table on demand."""
        self._root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.execute(_CREATE_TABLE)
        return conn

    def _write_sync(self, message: MailMessage) -> None:
        """Blocking insert of one row, committed before returning."""
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO mail (ts, session_id, kind, to_label, subject, body, "
                "patient_name, patient_phone, category) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message.ts,
                    message.session_id,
                    message.kind,
                    message.to_label,
                    message.subject,
                    message.body,
                    message.patient_name,
                    message.patient_phone,
                    message.category,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    async def write(self, message: MailMessage) -> None:
        """Insert ``message`` as one durable row in the unified mail table."""
        await asyncio.to_thread(self._write_sync, message)

    def list_messages(self) -> list[MailMessage]:
        """All messages across all sessions, newest-first (by ``ts``)."""
        if not self._db_path.exists():
            return []
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT ts, session_id, kind, to_label, subject, body, "
                "patient_name, patient_phone, category FROM mail ORDER BY ts DESC, id DESC"
            ).fetchall()
        finally:
            conn.close()
        return [
            MailMessage(
                ts=float(r[0]),
                session_id=str(r[1]),
                kind=str(r[2]),
                to_label=str(r[3]),
                subject=str(r[4]),
                body=str(r[5]),
                patient_name=str(r[6]),
                patient_phone=str(r[7]),
                category=str(r[8]),
            )
            for r in rows
        ]
