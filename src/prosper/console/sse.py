"""FastAPI router exposing the Operator Console HTTP surface.

Three endpoints (see spec §6.4):

- `GET /console/stream/{session_id}` — Server-Sent Events. If the session
  is in-memory (live), tail the bus; if it has only an audit file, replay
  from JSONL using the same SSE format.
- `GET /console/sessions` — JSON index of every session on disk.
- `GET /console/replay/{session_id}` — explicit replay, even for an
  in-flight session (useful for QA).

The frontend at `static/` consumes these via `EventSource`. The router is
mounted lazily by `bot.py` only when `PROSPER_CONSOLE_ENABLED` is on,
keeping the import surface in tests minimal.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from prosper.console._utils import check_session_id
from prosper.console.audit import AuditJSONLWriter
from prosper.console.bus import ConsoleBus
from prosper.console.events import ConsoleEvent

logger = logging.getLogger(__name__)

# Path to the static HTML/JS shipped with the console module.
_STATIC_DIR: Path = Path(__file__).parent / "static"

# Heartbeat interval — keeps proxies from idle-closing the SSE connection.
# 15s is the lowest sensible value (browsers reconnect on idle ~30s on some
# corporate proxies). Override via env if needed. Module-level globals
# (not Final) so tests can monkey-patch — production code never mutates.
_HEARTBEAT_INTERVAL_S: float = float(os.environ.get("PROSPER_CONSOLE_HEARTBEAT_S", "15.0"))

# Replay pacing — how fast to feed JSONL events back to the client. `1.0`
# means "real time" (sleep between events to match recorded gaps). `0.0`
# means "as fast as possible" (good for QA, bad for demo replay).
_REPLAY_SPEED: float = float(os.environ.get("PROSPER_CONSOLE_REPLAY_SPEED", "1.0"))

# Cap any replay pause at this many seconds — even a 10-minute idle in the
# original session should not stall the replay. Env-tunable so CI / load
# tests can set it to 0 and stream the entire JSONL with no pacing at all.
_REPLAY_MAX_GAP_S: float = float(os.environ.get("PROSPER_CONSOLE_REPLAY_MAX_GAP_S", "5.0"))


def _sse_format(event: ConsoleEvent) -> str:
    """Encode `event` as one SSE frame (`data:` line + blank line)."""
    return f"data: {event.to_json()}\n\n"


def _sse_heartbeat() -> str:
    """A bare SSE comment used as a keep-alive — clients ignore it."""
    return ": ping\n\n"


def build_router(
    bus: ConsoleBus,
    audit: AuditJSONLWriter,
) -> APIRouter:
    """Return a FastAPI router wired to the given bus + audit instances.

    Building the router takes its dependencies as arguments so we can
    construct multiple, isolated routers in tests without touching
    module-level state.
    """
    router = APIRouter(prefix="/console", tags=["console"])

    @router.get("/sessions")
    async def list_sessions() -> JSONResponse:
        """Return every session that has a `.jsonl` on disk, newest-first.

        Response shape: ``{"sessions": [{"id": str, "mtime_ts": float}, …]}``
        sorted descending by ``mtime_ts`` so the client can take ``[0]``
        for the most-recently modified session without re-sorting.
        """
        return JSONResponse({"sessions": audit.list_sessions_with_meta()})

    @router.get("/stream/{session_id}")
    async def stream(session_id: str, request: Request) -> StreamingResponse:
        """Live SSE stream for an active session.

        Tails the bus for new events. If the client disconnects, the
        subscription is cleaned up via the context manager.
        """
        _validate_session_id_or_raise(session_id)
        return StreamingResponse(
            _live_event_iter(bus, session_id, request),
            media_type="text/event-stream",
            headers=_sse_headers(),
        )

    @router.get("/replay/{session_id}")
    async def replay(session_id: str, request: Request) -> StreamingResponse:
        """SSE replay of a recorded session from the audit JSONL."""
        _validate_session_id_or_raise(session_id)
        if not audit.path_for(session_id).exists():
            raise HTTPException(status_code=404, detail="session not found")
        return StreamingResponse(
            _replay_event_iter(audit, session_id, request),
            media_type="text/event-stream",
            headers=_sse_headers(),
        )

    @router.get("/{session_id}", include_in_schema=False)
    async def session_view(session_id: str) -> FileResponse:
        """Serve the single-page console for one session."""
        _validate_session_id_or_raise(session_id)
        return FileResponse(_STATIC_DIR / "index.html")

    @router.get("", include_in_schema=False)
    async def console_root() -> FileResponse:
        """Serve the console landing page (most recent session picker)."""
        return FileResponse(_STATIC_DIR / "index.html")

    # Static assets (console.js, any future css). Mount on the parent
    # application path because APIRouter.mount does NOT inherit the
    # router `prefix="/console"` for sub-mounts — files would resolve at
    # `/static/...` instead of `/console/static/...`. We expose a helper
    # so the caller mounts on the FastAPI app with the right path. The
    # router-level mount stays useful when the router is included on the
    # root path (e.g. tests), so we keep it; the production wiring in
    # `bot.py` adds the prefixed mount explicitly.
    if _STATIC_DIR.exists():
        router.mount(
            "/static",
            StaticFiles(directory=str(_STATIC_DIR)),
            name="console-static",
        )

    return router


def mount_static_on_app(app: object) -> None:
    """Mount console + call-UI static assets on a FastAPI app.

    APIRouter.mount does not inherit the router's prefix; to serve
    `/console/static/console.js` correctly in production, callers must
    mount the static dir directly on the parent FastAPI application
    *after* `include_router`. This helper keeps the path string in one
    place so the URL never drifts from what the front-end requests.

    The call UI (`/call` landing + `/call/static/*` assets) is a sibling
    surface served from the same uvicorn process — it's a custom WebRTC
    front-end that talks directly to the Pipecat runner on :7860, giving
    a phone-call look-and-feel instead of the prebuilt UI.
    """
    if not _STATIC_DIR.exists():
        return
    # Late import: keeps `mount_static_on_app` callable from places that
    # only have a duck-typed `app` (e.g. tests with a stub).
    from fastapi import FastAPI

    if not isinstance(app, FastAPI):
        return
    app.mount(
        "/console/static",
        StaticFiles(directory=str(_STATIC_DIR)),
        name="console-static",
    )

    call_dir = _STATIC_DIR / "call"
    if call_dir.exists():
        app.mount(
            "/call/static",
            StaticFiles(directory=str(call_dir)),
            name="call-static",
        )

        @app.get("/call", include_in_schema=False)
        async def call_root() -> FileResponse:
            """Serve the call-style WebRTC front-end."""
            return FileResponse(call_dir / "index.html")


def _validate_session_id_or_raise(session_id: str) -> None:
    """Reject path-traversal attempts at the HTTP boundary (HTTP 400)."""
    try:
        check_session_id(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _sse_headers() -> dict[str, str]:
    """HTTP headers required for SSE to work behind common reverse proxies."""
    return {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        # Disable buffering on nginx / cloudflare — without this the events
        # would be batched and the live feel breaks.
        "X-Accel-Buffering": "no",
    }


async def _live_event_iter(
    bus: ConsoleBus,
    session_id: str,
    request: Request,
) -> AsyncIterator[str]:
    """Yield SSE frames for the live bus, filtered to `session_id`.

    Sends a heartbeat every `_HEARTBEAT_INTERVAL_S` seconds even when no
    events arrive — without it, idle clients get disconnected by proxies.
    """
    # Subscribe scoped to this session so a chatty unrelated session cannot
    # evict this stream's events from a shared bounded queue (audit F-010).
    async with bus.subscribe(session_id) as queue:
        while True:
            if await request.is_disconnected():
                return
            try:
                event = await asyncio.wait_for(
                    queue.get(),
                    timeout=_HEARTBEAT_INTERVAL_S,
                )
            except asyncio.TimeoutError:
                yield _sse_heartbeat()
                continue
            # Defence in depth: the bus already filters by session_id; keep the
            # guard so a future unfiltered subscription can't leak cross-session.
            if event.session_id != session_id:
                continue
            yield _sse_format(event)


async def _replay_event_iter(
    audit: AuditJSONLWriter,
    session_id: str,
    request: Request,
) -> AsyncIterator[str]:
    """Yield SSE frames replayed from the audit JSONL.

    Pacing uses the recorded `ts` deltas, scaled by `_REPLAY_SPEED`. Any
    single gap longer than `_REPLAY_MAX_GAP_S` is capped — the operator
    watching a replay does not want to wait through a 5-minute idle.
    """
    last_ts: float | None = None
    async for event in audit.iter_events(session_id):
        if await request.is_disconnected():
            return
        if last_ts is not None and _REPLAY_SPEED > 0:
            gap = max(0.0, event.ts - last_ts) / _REPLAY_SPEED
            await asyncio.sleep(min(gap, _REPLAY_MAX_GAP_S))
        last_ts = event.ts
        yield _sse_format(event)
    # Terminal sentinel: a named SSE event the client listens for so it
    # can close the EventSource. Without it the browser treats the closed
    # stream as a dropped connection and auto-reconnects, replaying the
    # whole session again and duplicating every rendered row.
    yield "event: replay_complete\ndata: {}\n\n"
