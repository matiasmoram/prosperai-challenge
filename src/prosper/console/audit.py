"""Append-only JSONL audit writer + replay reader for the Operator Console.

One file per session at `<audit_root>/<session_id>.jsonl`. Lines are the
output of `ConsoleEvent.to_json()` — one event per line. Writes happen
inside a background drain task subscribing to a `ConsoleBus`; replays are a
pure file read.

The audit log is the *durable* counterpart to the in-memory bus: if the
process restarts, the live SSE stream of a past session resumes from the
file. See spec §3.2 (event flow) and §6.3 (interface).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Final

import aiofiles

from prosper.console._utils import check_session_id
from prosper.console.bus import ConsoleBus
from prosper.console.events import ConsoleEvent
from prosper.observability.redact import redact_pii

if TYPE_CHECKING:
    from aiofiles.threadpool.text import AsyncTextIOWrapper

logger = logging.getLogger(__name__)


def _redact_event_for_audit(event: ConsoleEvent) -> ConsoleEvent:
    """Scrub free-form PII from an event before it is persisted to disk.

    ``transcript_turn`` carries the caller's (and bot's) raw utterance in
    ``payload['text']``, which can contain a spoken phone number or DOB. The
    live SSE stream keeps the raw text (the operator legitimately needs it),
    but the durable JSONL must not (audit F-007). Every other event type
    already arrives PII-redacted at the publish boundary
    (``dispatcher._redact_tool_args`` + the bus ``*_masked`` check).
    """
    if event.type == "transcript_turn":
        text = event.payload.get("text")
        if isinstance(text, str) and text:
            new_payload = dict(event.payload)
            new_payload["text"] = redact_pii(text)
            return replace(event, payload=new_payload)
    return event


# Default audit root. Override via env var for tests that want an isolated
# directory without touching the repo's `data/audit/`. The env var is read
# at instance construction time, never at module import, so changing it
# between tests is safe.
_DEFAULT_AUDIT_ROOT_NAME: Final[str] = "data/audit"


def _resolve_default_root() -> Path:
    """Compute the default audit root, honouring `PROSPER_CONSOLE_AUDIT_ROOT`."""
    override = os.environ.get("PROSPER_CONSOLE_AUDIT_ROOT")
    return Path(override) if override else Path(_DEFAULT_AUDIT_ROOT_NAME)


class AuditJSONLWriter:
    """Subscribes to a `ConsoleBus` and writes each event to one JSONL per session.

    Files are append-only. The writer keeps one open `aiofiles` handle per
    active `session_id`; the handle is closed when the writer is shut down
    or when the bus subscription exits.
    """

    def __init__(self, root: Path | None = None) -> None:
        """Initialise the writer with a target root directory.

        Args:
            root: Directory under which per-session `.jsonl` files live.
                Defaults to `PROSPER_CONSOLE_AUDIT_ROOT` (env) or
                `data/audit/` (repo-relative). The directory is created
                lazily on first write.
        """
        self._root: Path = root if root is not None else _resolve_default_root()
        self._handles: dict[str, AsyncTextIOWrapper] = {}
        self._lock = asyncio.Lock()

    @property
    def root(self) -> Path:
        """Audit root directory (read-only outside tests)."""
        return self._root

    def path_for(self, session_id: str) -> Path:
        """Return the JSONL file path for `session_id` without creating it."""
        check_session_id(session_id)
        return self._root / f"{session_id}.jsonl"

    async def write(self, event: ConsoleEvent) -> None:
        """Append `event` as one JSON line to its session's file.

        Opens (and caches) one handle per session. Calls `fsync` on
        `outcome` events — those are the durability-critical ones,
        anything else can survive a few seconds of writeback lag.

        `fsync` runs in the default executor (thread pool) so the
        blocking syscall does not stall the event loop on slow disks.
        """
        check_session_id(event.session_id)
        event = _redact_event_for_audit(event)
        async with self._lock:
            handle = self._handles.get(event.session_id)
            if handle is None:
                self._root.mkdir(parents=True, exist_ok=True)
                handle = await aiofiles.open(
                    self.path_for(event.session_id),
                    mode="a",
                    encoding="utf-8",
                )
                self._handles[event.session_id] = handle
            await handle.write(event.to_json() + "\n")
            await handle.flush()
            if event.type == "outcome":
                # Outcome is the only event where durability matters: if the
                # process crashes mid-call we still need to know whether the
                # appointment was booked or cancelled. `os.fsync` is a
                # blocking syscall — offload to the default executor so the
                # event loop is not stalled on slow storage.
                fd = handle.fileno()
                await asyncio.get_running_loop().run_in_executor(None, os.fsync, fd)

    async def close(self) -> None:
        """Close all open handles. Safe to call multiple times."""
        async with self._lock:
            for handle in self._handles.values():
                await handle.close()
            self._handles.clear()

    @asynccontextmanager
    async def attach(self, bus: ConsoleBus) -> AsyncIterator[None]:
        """Drain `bus` into the audit log for the lifetime of the context.

        Usage:
            async with audit.attach(bus):
                await run_call(...)
            # all writes flushed and handles closed on exit

        The drain runs as a background task. `attach` does not yield
        until the drain has subscribed — otherwise the first publish
        could race past an un-subscribed bus and be lost. On exit, the
        context waits for the drain's queue to fully drain
        (`asyncio.Queue.join`) before cancelling the task, so every
        event published before exit lands on disk even if the caller
        publishes in the final microsecond.
        """
        loop = asyncio.get_running_loop()
        queue_future: asyncio.Future[asyncio.Queue[ConsoleEvent]] = loop.create_future()
        drain_task = asyncio.create_task(self._drain(bus, queue_future))
        queue = await queue_future
        try:
            yield
        finally:
            # Wait for the drain to write every event we publish before
            # exiting — `queue.join` blocks until `task_done()` has been
            # called for every put.
            await queue.join()
            drain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await drain_task
            await self.close()

    async def _drain(
        self,
        bus: ConsoleBus,
        queue_future: asyncio.Future[asyncio.Queue[ConsoleEvent]],
    ) -> None:
        """Subscribe to `bus` and write every event to disk until cancelled.

        Sets `queue_future`'s result to the live subscriber queue so
        `attach` knows the subscription is established and has a handle
        for the `queue.join()` shutdown barrier.
        """
        async with bus.subscribe() as queue:
            queue_future.set_result(queue)
            while True:
                event = await queue.get()
                try:
                    await self.write(event)
                except (OSError, ValueError):
                    # We do not crash the call path on audit failure; we log
                    # and continue draining so future events still land.
                    logger.exception(
                        "audit-writer dropped event (session=%s type=%s)",
                        event.session_id,
                        event.type,
                    )
                finally:
                    # Always mark task done — even on write failure — so
                    # the shutdown `queue.join()` does not deadlock.
                    queue.task_done()

    async def iter_events(self, session_id: str) -> AsyncIterator[ConsoleEvent]:
        """Yield every event recorded for `session_id`, in write order.

        Reads the file line-by-line; bad lines are skipped with a warning
        so a partially-truncated audit (e.g. mid-write crash) still
        replays the surviving events.
        """
        path = self.path_for(session_id)
        if not path.exists():
            return
        async with aiofiles.open(path, encoding="utf-8") as handle:
            async for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    yield ConsoleEvent.from_json(stripped)
                except (ValueError, KeyError):
                    logger.warning(
                        "audit-replay skipping malformed line in session=%s: %r",
                        session_id,
                        stripped[:200],
                    )

    def list_sessions(self) -> list[str]:
        """Return all session ids that have a `.jsonl` file on disk.

        Synchronous because directory listings are O(n) and small; no
        async benefit. Returned in lexicographic order (callers that
        want chronological order should sort by file mtime themselves).
        """
        if not self._root.exists():
            return []
        return sorted(p.stem for p in self._root.glob("*.jsonl"))

    def list_sessions_with_meta(self) -> list[dict[str, object]]:
        """Return session metadata sorted newest-first by file mtime.

        Each entry is ``{"id": session_id, "mtime_ts": float}`` where
        ``mtime_ts`` is the file's last-modified time as a UTC epoch
        float (``Path.stat().st_mtime``). The list is sorted descending
        so ``[0]`` is always the most-recently modified session.

        All filesystem/mtime knowledge stays in ``audit.py``; ``sse.py``
        delegates here instead of re-globbing the directory.  The
        existing ``list_sessions() -> list[str]`` signature is preserved
        unchanged for backward compatibility.
        """
        if not self._root.exists():
            return []
        # Collect (mtime, id) pairs for sorting, then build the final dicts.
        # Using a typed intermediate list avoids the mypy `object`-narrowing
        # issue that arises from sorting a `list[dict[str, object]]` by a
        # value whose type the checker cannot prove is float at the sort site.
        pairs: list[tuple[float, str]] = [
            (p.stat().st_mtime, p.stem) for p in self._root.glob("*.jsonl")
        ]
        pairs.sort(key=lambda t: t[0], reverse=True)
        return [{"id": sid, "mtime_ts": mtime} for mtime, sid in pairs]
