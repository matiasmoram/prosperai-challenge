"""Durable, full-PII outbound-mail records for the front-desk surface.

One JSONL file per session at ``<root>/<session_id>.jsonl``. Unlike the
operator console, records carry the patient's real name + phone — a callback
or a confirmation is useless masked. Deliberately a different trust tier: the
``/frontdesk`` surface that reads it is staff-only (behind auth in prod,
loopback in the demo). Two channels share one record via ``kind``. See spec §7.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

import aiofiles

_DEFAULT_ROOT_NAME: Final[str] = "data/mail"


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
        """Serialise to one JSONL line."""
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
    """Append-only JSONL store of outbound mail, one file per session.

    Files live under ``<root>/<session_id>.jsonl``; each line is one
    serialised ``MailMessage``. Writes are async (aiofiles). Reads are
    synchronous — directory scans and file reads are small and infrequent
    (2-second poll from the SPA), so the async overhead is not worth it.
    """

    def __init__(self, root: Path | None = None) -> None:
        """Initialise with a target root dir (default ``data/mail/``)."""
        self._root: Path = root if root is not None else _resolve_default_root()

    @property
    def root(self) -> Path:
        """Mail root directory (read-only outside tests)."""
        return self._root

    async def write(self, message: MailMessage) -> None:
        """Append ``message`` as one JSON line to its session file."""
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._root / f"{message.session_id}.jsonl"
        async with aiofiles.open(path, mode="a", encoding="utf-8") as handle:
            await handle.write(message.to_json() + "\n")
            await handle.flush()

    def list_messages(self) -> list[MailMessage]:
        """All messages across all sessions, newest-first (by ``ts``).

        Bad lines are skipped silently so a partially-truncated file
        does not poison the entire list.
        """
        if not self._root.exists():
            return []
        out: list[MailMessage] = []
        for path in self._root.glob("*.jsonl"):
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped:
                    with contextlib.suppress(KeyError, ValueError):
                        out.append(MailMessage.from_json(stripped))
        out.sort(key=lambda m: m.ts, reverse=True)
        return out
