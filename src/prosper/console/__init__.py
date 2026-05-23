"""Operator Console module: live event bus, audit log, SSE endpoint, frontend.

See `docs/superpowers/specs/2026-05-20-operator-console-design.md` for the
load-bearing design decisions and `docs/adr/004-operator-console-event-stream.md`
for the irreversible architectural choices.

Public surface:
    ConsoleEvent — frozen dataclass for one telemetry event
    ConsoleBus   — in-memory async pub/sub with bounded per-subscriber queues
    AuditJSONLWriter — disk subscriber, one .jsonl file per session
    router       — FastAPI router exposing /console/* endpoints

Everything is asyncio-native; no threads, no shared mutable state outside
the bus's subscriber list.
"""

from prosper.console.bus import ConsoleBus
from prosper.console.events import (
    EVENT_TYPES,
    ConsoleEvent,
    validate_event,
)

__all__ = [
    "EVENT_TYPES",
    "ConsoleBus",
    "ConsoleEvent",
    "validate_event",
]
