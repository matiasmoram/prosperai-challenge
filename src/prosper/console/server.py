"""Embedded uvicorn server for the Operator Console.

The console runs in the same OS process as the voice bot — same event
loop, same ``ConsoleBus`` instance. We can't mount it on Pipecat's own
FastAPI server (pipecat-ai-cli does not expose a hook), so we spin up a
second uvicorn on a separate port and reuse the existing
``asyncio`` loop via ``uvicorn.Server.serve()``.

Default port is **7861**, one above the Pipecat browser client at
``:7860``. Override with ``PROSPER_CONSOLE_PORT``.

Lifecycle:
    async with ConsoleServer.run(bus, audit):
        # the voice bot does its thing; console serves on :7861
        ...

On exit, uvicorn is asked to stop gracefully (in-flight SSE streams are
torn down via their `is_disconnected` check, with up to one heartbeat
interval of delay — see spec §6.4).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

import uvicorn
from fastapi import FastAPI

from prosper.console.audit import AuditJSONLWriter
from prosper.console.bus import ConsoleBus
from prosper.console.sse import build_router, mount_static_on_app

logger = logging.getLogger(__name__)

# Default port. Sized so the bot's own browser client (7860) and the
# console (7861) are adjacent — easy to remember during the demo.
_DEFAULT_PORT: Final[int] = 7861

# Default bind address. Loopback in dev; override to 0.0.0.0 in a
# container env that fronts the console behind a reverse proxy.
_DEFAULT_HOST: Final[str] = "127.0.0.1"


def _resolve_port() -> int:
    """Read `PROSPER_CONSOLE_PORT` (else default to 7861)."""
    raw = os.environ.get("PROSPER_CONSOLE_PORT", str(_DEFAULT_PORT))
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError(f"PROSPER_CONSOLE_PORT must be an integer, got {raw!r}") from exc
    if not 1 <= port <= 65_535:
        raise ValueError(f"PROSPER_CONSOLE_PORT out of range: {port}")
    return port


def _resolve_host() -> str:
    """Read `PROSPER_CONSOLE_HOST` (else default to 127.0.0.1)."""
    return os.environ.get("PROSPER_CONSOLE_HOST", _DEFAULT_HOST)


def build_app(bus: ConsoleBus, audit: AuditJSONLWriter) -> FastAPI:
    """Build the FastAPI app that backs the operator console.

    The bus + audit instances are injected so tests can construct an
    isolated app without touching module-level state.
    """
    app = FastAPI(title="Prosper · Operator Console", docs_url=None, redoc_url=None)
    app.include_router(build_router(bus, audit))
    # Mount static AFTER include_router so the `/console/static` path
    # resolves correctly (the router itself sits under `/console`).
    mount_static_on_app(app)
    return app


@asynccontextmanager
async def run(
    bus: ConsoleBus,
    audit: AuditJSONLWriter,
    *,
    host: str | None = None,
    port: int | None = None,
) -> AsyncIterator[None]:
    """Run the console uvicorn server in the background for the context.

    Usage:
        async with run(bus, audit):
            await run_bot(...)
        # uvicorn shuts down cleanly when the context exits

    The server is started on the current event loop — no threads. The
    audit writer is also attached to the bus inside the same context, so
    a single ``async with run(bus, audit):`` covers both the live SSE
    stream AND the durable JSONL write side.
    """
    app = build_app(bus, audit)
    resolved_host = host or _resolve_host()
    resolved_port = port if port is not None else _resolve_port()
    config = uvicorn.Config(
        app,
        host=resolved_host,
        port=resolved_port,
        log_level="warning",  # uvicorn's INFO is too chatty next to the bot logs
        access_log=False,
        lifespan="off",  # we drive lifecycle from outside
    )
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    # Wait briefly for uvicorn to bind so the operator can connect
    # immediately after the bot logs "ready". 1 second is more than
    # enough on any modern machine; if it ever fails to bind in that
    # window, the user will see the error in uvicorn's own logs.
    for _ in range(20):
        if server.started:
            break
        await asyncio.sleep(0.05)
    else:
        logger.warning(
            "console uvicorn did not report `started` within 1s; "
            "the SSE endpoint may not yet be reachable on %s:%d",
            resolved_host,
            resolved_port,
        )
    logger.info("operator console live on http://%s:%d/console", resolved_host, resolved_port)
    try:
        async with audit.attach(bus):
            yield
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(serve_task, timeout=5.0)
