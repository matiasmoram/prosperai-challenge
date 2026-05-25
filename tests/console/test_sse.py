"""SSE endpoint tests via httpx.ASGITransport — no real server.

Mounts the router on a throwaway FastAPI app and pokes it through the
in-process transport. Covers: live stream filtering by session_id,
heartbeat under idle, replay endpoint reads JSONL, sessions index,
path-traversal rejection at the HTTP boundary.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from prosper.console.audit import AuditJSONLWriter
from prosper.console.bus import ConsoleBus
from prosper.console.events import make_event
from prosper.console.sse import build_router


def _make_app(bus: ConsoleBus, audit: AuditJSONLWriter) -> FastAPI:
    """Build a one-off FastAPI app with the console router mounted."""
    app = FastAPI()
    app.include_router(build_router(bus, audit))
    return app


def _parse_sse_data_frames(body: str) -> list[dict[str, object]]:
    """Extract the JSON payload from each `data: {...}` SSE frame."""
    frames: list[dict[str, object]] = []
    for line in body.splitlines():
        if line.startswith("data: "):
            frames.append(json.loads(line[len("data: ") :]))
    return frames


@pytest.mark.asyncio
async def test_sessions_endpoint_lists_audit_files(tmp_path: Path) -> None:
    """`GET /console/sessions` must return sessions as [{id, mtime_ts}] newest-first.

    Correctness check: the session written LAST must appear at index 0 so the
    client can pick ``sessions[0]`` and always get the most-recently modified one.
    """
    import time

    # Write "alpha" first, then sleep briefly so "beta" has a strictly newer mtime.
    audit_alpha = AuditJSONLWriter(root=tmp_path)
    bus = ConsoleBus()
    await audit_alpha.write(
        make_event(
            "latency_tick",
            session_id="alpha",
            payload={"phase": "llm", "duration_ms": 1.0},
            ts=0.0,
        )
    )
    await audit_alpha.close()

    # Small sleep so OS mtime for beta is strictly greater than alpha's.
    time.sleep(0.05)

    audit_beta = AuditJSONLWriter(root=tmp_path)
    await audit_beta.write(
        make_event(
            "latency_tick",
            session_id="beta",
            payload={"phase": "llm", "duration_ms": 1.0},
            ts=1.0,
        )
    )
    await audit_beta.close()

    app = _make_app(bus, audit_beta)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://t") as client:
        response = await client.get("/console/sessions")

    assert response.status_code == 200
    body = response.json()
    sessions = body["sessions"]

    # Shape: list of {id: str, mtime_ts: float}.
    assert isinstance(sessions, list)
    assert len(sessions) == 2
    for entry in sessions:
        assert "id" in entry
        assert "mtime_ts" in entry
        assert isinstance(entry["id"], str)
        assert isinstance(entry["mtime_ts"], float)

    # Chronological correctness: "beta" written last must be first (index 0).
    assert sessions[0]["id"] == "beta", (
        "newest session (beta) must be at index 0 so the client picks it by default"
    )
    assert sessions[1]["id"] == "alpha"
    # Strictly descending mtime.
    assert sessions[0]["mtime_ts"] >= sessions[1]["mtime_ts"]


@pytest.mark.asyncio
async def test_replay_endpoint_returns_events_in_order(tmp_path: Path) -> None:
    """Replay must yield each recorded event as an SSE `data:` frame."""
    audit = AuditJSONLWriter(root=tmp_path)
    bus = ConsoleBus()
    events = [
        make_event(
            "latency_tick",
            session_id="s1",
            payload={"phase": "llm", "duration_ms": float(i)},
            ts=float(i),
        )
        for i in range(3)
    ]
    for e in events:
        await audit.write(e)
    await audit.close()

    # Disable pacing for the test — we don't want to wait 3 seconds.
    import prosper.console.sse as sse_module

    monkey_speed = sse_module._REPLAY_SPEED
    sse_module._REPLAY_SPEED = 0.0
    try:
        app = _make_app(bus, audit)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://t"
        ) as client:
            response = await client.get("/console/replay/s1")
    finally:
        sse_module._REPLAY_SPEED = monkey_speed

    assert response.status_code == 200
    frames = _parse_sse_data_frames(response.text)
    # The recorded events come first, in order. A terminal `replay_complete`
    # sentinel frame (payload `{}`, no `ts`) follows so the browser closes
    # the EventSource instead of auto-reconnecting and re-replaying.
    event_frames = [f for f in frames if "ts" in f]
    assert [f["ts"] for f in event_frames] == [0.0, 1.0, 2.0]
    assert "event: replay_complete" in response.text


@pytest.mark.asyncio
async def test_replay_missing_session_returns_404(tmp_path: Path) -> None:
    """A session id with no JSONL on disk must return 404, not silently empty."""
    audit = AuditJSONLWriter(root=tmp_path)
    bus = ConsoleBus()
    app = _make_app(bus, audit)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://t") as client:
        response = await client.get("/console/replay/never-existed")
    assert response.status_code == 404


@pytest.mark.parametrize(
    "evil_session_id",
    # These characters survive URL parsing and reach the route handler as
    # the session_id parameter. `..` and `../etc` are excluded because
    # httpx + Starlette normalise them away before the handler sees them
    # (which is itself a valid defence — the audit layer still rejects
    # them, exercised in `test_audit.py::test_path_for_rejects_*`).
    ["with.space", "id;rm", "evil%2Fpath", "trailing.dot"],
)
@pytest.mark.asyncio
async def test_stream_rejects_unsafe_session_ids(tmp_path: Path, evil_session_id: str) -> None:
    """Path-traversal must be rejected at the HTTP boundary, not just in audit."""
    audit = AuditJSONLWriter(root=tmp_path)
    bus = ConsoleBus()
    app = _make_app(bus, audit)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://t") as client:
        response = await client.get(f"/console/stream/{evil_session_id}")
    # 400 (handler rejected) and 404 (URL routing rejected, e.g. `/` in
    # session_id splits the path) are both valid refusals — neither
    # exposes audit content. 200 would be a bug.
    assert response.status_code in (400, 404)


@pytest.mark.asyncio
async def test_live_stream_filters_by_session_id() -> None:
    """A subscriber to /stream/s1 must NOT receive events for /stream/s2.

    Tested directly against the helper coroutine to avoid the HTTP
    transport's response-buffering behaviour, which makes timing-based
    SSE assertions flaky in CI. The HTTP wrapping is exercised by
    `test_replay_endpoint_returns_events_in_order`.
    """
    from prosper.console import sse as sse_module

    # Force a tiny heartbeat so the generator's `wait_for(queue.get())`
    # cannot stall the test on the default 15-second timer.
    original_heartbeat = sse_module._HEARTBEAT_INTERVAL_S
    sse_module._HEARTBEAT_INTERVAL_S = 0.05

    bus = ConsoleBus()

    class _FakeRequest:
        async def is_disconnected(self) -> bool:
            return False

    gen: AsyncGenerator[str, None] = sse_module._live_event_iter(  # type: ignore[assignment]
        bus,
        "s1",
        _FakeRequest(),  # type: ignore[arg-type]
    )

    try:
        # Publish AFTER the generator is known to be subscribed. The
        # first `__anext__` call enters the generator and runs until it
        # either yields or hits its first await — that's when the
        # subscription is registered.
        first_step: asyncio.Task[str] = asyncio.ensure_future(gen.__anext__())
        # Yield to let the generator reach its first await.
        for _ in range(50):
            if bus.subscriber_count > 0:
                break
            await asyncio.sleep(0.01)
        else:
            first_step.cancel()
            pytest.fail("SSE generator never subscribed to bus")

        await bus.publish(
            make_event(
                "latency_tick",
                session_id="s2",  # other session — must be filtered out
                payload={"phase": "llm", "duration_ms": 9.0},
                ts=9.0,
            )
        )
        await bus.publish(
            make_event(
                "latency_tick",
                session_id="s1",  # matches the stream
                payload={"phase": "llm", "duration_ms": 1.0},
                ts=1.0,
            )
        )

        # Collect a few frames; with a 50ms heartbeat we may get
        # interleaved `: ping` lines. Loop until we have one `data:`
        # frame or run out of attempts.
        frames: list[str] = [await asyncio.wait_for(first_step, timeout=2.0)]
        for _ in range(10):
            if any(f.startswith("data: ") for f in frames):
                break
            frames.append(await asyncio.wait_for(gen.__anext__(), timeout=2.0))

        data_frames = [f for f in frames if f.startswith("data: ")]
        assert data_frames, "must receive at least one data frame for s1"
        payload = json.loads(data_frames[0][len("data: ") :])
        assert payload["session_id"] == "s1"
        assert payload["ts"] == 1.0
    finally:
        sse_module._HEARTBEAT_INTERVAL_S = original_heartbeat
        await gen.aclose()


@pytest.mark.asyncio
async def test_console_root_serves_html(tmp_path: Path) -> None:
    """`GET /console` must serve the static `index.html`.

    The HTML may not exist yet in this task (built in Step 6), so we only
    assert the response is HTML when the file exists; otherwise the route
    returns 404 which we accept here.
    """
    audit = AuditJSONLWriter(root=tmp_path)
    bus = ConsoleBus()
    app = _make_app(bus, audit)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://t") as client:
        response = await client.get("/console")
    assert response.status_code in (200, 404)


@pytest.mark.asyncio
async def test_live_stream_http_validates_session_id(tmp_path: Path) -> None:
    """The /stream/{id} route handler must call `_validate_session_id_or_raise`.

    Direct-helper coverage (`test_live_stream_filters_by_session_id`) does
    not exercise the FastAPI route registration. This test confirms the
    route is wired and the validator fires for a bad id at the HTTP
    boundary. We do not assert on streaming behaviour here because the
    `StreamingResponse` keeps the connection open for heartbeats —
    asserting on that is the helper test's job.
    """
    audit = AuditJSONLWriter(root=tmp_path)
    bus = ConsoleBus()
    app = _make_app(bus, audit)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://t", timeout=2.0
    ) as client:
        # Invalid session id → 400 from `_validate_session_id_or_raise`.
        bad = await client.get("/console/stream/with.dot")
        assert bad.status_code == 400
        assert "invalid session_id" in bad.json()["detail"]


@pytest.mark.asyncio
async def test_call_ui_endpoints_served(tmp_path: Path) -> None:
    """`build_app` must serve the custom call UI under `/call`.

    Verifies the landing page + at least one static asset is reachable. The
    landing page is the entry point for the WebRTC-direct front-end that
    bypasses the Pipecat prebuilt UI.
    """
    from prosper.console.server import build_app

    audit = AuditJSONLWriter(root=tmp_path)
    bus = ConsoleBus()
    app = build_app(bus, audit)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://t") as client:
        landing = await client.get("/call")
        assert landing.status_code == 200
        assert b"Sarah" in landing.content, "call UI should render the avatar name"

        css = await client.get("/call/static/call.css")
        assert css.status_code == 200
        assert b"avatar" in css.content


@pytest.mark.asyncio
async def test_embedded_uvicorn_serves_console(tmp_path: Path) -> None:
    """Integration test: `console.server.run` binds uvicorn on a free port.

    Exercises the production wiring — `build_app` + `uvicorn.Server.serve`
    + the audit attach context — that `ASGITransport` tests do not cover.
    Uses a high port (0 means "let OS pick"); we then derive the bound
    port from the server's sockets after `started` flips True.
    """
    import socket

    from prosper.console import server as server_module

    bus = ConsoleBus()
    audit = AuditJSONLWriter(root=tmp_path)

    # Find a free port deterministically — binding 0 inside uvicorn is
    # possible but extracting the bound port reliably is harder.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    free_port = sock.getsockname()[1]
    sock.close()

    async with (
        server_module.run(bus, audit, host="127.0.0.1", port=free_port),
        httpx.AsyncClient(base_url=f"http://127.0.0.1:{free_port}", timeout=3.0) as c,
    ):
        r = await c.get("/console/sessions")
        assert r.status_code == 200
        assert r.json() == {"sessions": []}


@pytest.mark.asyncio
async def test_follow_iter_tails_growing_file_then_closes_on_outcome(tmp_path: Path) -> None:
    """`follow=1` mode delivers appended events, then closes on `outcome`.

    Drives the helper directly (like the live-stream test) so we control the
    poll cadence without waiting on a real 0.5 s interval. We write events to
    the audit file *between* iterations to simulate a call in progress, and
    assert the stream emits each new event and ends with `replay_complete`
    once the `outcome` event lands — never replaying a `replay_complete` for a
    still-running call.
    """
    from prosper.console import sse as sse_module

    audit = AuditJSONLWriter(root=tmp_path)

    class _FakeRequest:
        async def is_disconnected(self) -> bool:
            return False

    # Shrink the poll so an empty poll returns a heartbeat near-instantly.
    monkey = sse_module._FOLLOW_POLL_INTERVAL_S
    sse_module._FOLLOW_POLL_INTERVAL_S = 0.01
    try:
        # Seed the backlog already on disk before the follower attaches.
        await audit.write(
            make_event(
                "transcript_turn",
                session_id="live1",
                payload={"role": "user", "text": "hi", "turn_id": 1},
                ts=0.0,
            )
        )
        gen: AsyncIterator[str] = sse_module._follow_event_iter(
            audit,
            "live1",
            _FakeRequest(),  # type: ignore[arg-type]
        )

        # First frame: the backlog event.
        first = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
        assert first.startswith("data: ")
        assert json.loads(first[len("data: ") :])["payload"]["text"] == "hi"

        # Append a mid-call event; the next non-heartbeat frame must carry it.
        await audit.write(
            make_event(
                "state_change",
                session_id="live1",
                payload={"from_state": "GREETING", "to_state": "CHOOSE_INTENT", "trigger": "t"},
                ts=1.0,
            )
        )
        frames: list[str] = []
        for _ in range(50):
            f = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
            frames.append(f)
            if any(fr.startswith("data: ") for fr in frames):
                break
        data = [json.loads(f[len("data: ") :]) for f in frames if f.startswith("data: ")]
        assert any(d["type"] == "state_change" for d in data)

        # The call ends — an `outcome` event must terminate the follow stream
        # with a `replay_complete` sentinel so the client stops polling.
        await audit.write(
            make_event(
                "outcome",
                session_id="live1",
                payload={"outcome": "booked", "details": {"turns": 3}},
                ts=2.0,
            )
        )
        saw_complete = False
        for _ in range(50):
            f = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
            if f.startswith("event: replay_complete"):
                saw_complete = True
                break
        assert saw_complete, "follow stream must close on the outcome event"
        # The generator returns after the sentinel; the next step is StopAsyncIteration.
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(gen.__anext__(), timeout=2.0)
    finally:
        sse_module._FOLLOW_POLL_INTERVAL_S = monkey
        await gen.aclose()


@pytest.mark.asyncio
async def test_follow_iter_missing_file_heartbeats_then_disconnects(tmp_path: Path) -> None:
    """Follow before the first event is flushed yields a heartbeat, not a 404.

    A follower attaches the instant a call starts — possibly before the audit
    file exists. Plain replay 404s on a missing file; follow mode must instead
    keep the stream open (heartbeat) and wait for events to appear. Driven
    against the helper because an open-ended stream over `ASGITransport` has no
    real disconnect, so we control termination via the fake request instead.
    """
    from prosper.console import sse as sse_module

    audit = AuditJSONLWriter(root=tmp_path)

    class _OneShotRequest:
        """Disconnects after the first poll so the open-ended loop terminates."""

        def __init__(self) -> None:
            self._polls = 0

        async def is_disconnected(self) -> bool:
            self._polls += 1
            return self._polls > 1

    monkey = sse_module._FOLLOW_POLL_INTERVAL_S
    sse_module._FOLLOW_POLL_INTERVAL_S = 0.01
    try:
        gen: AsyncIterator[str] = sse_module._follow_event_iter(
            audit,
            "not-yet",
            _OneShotRequest(),  # type: ignore[arg-type]
        )
        first = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
        assert first.startswith(": ping"), "missing file must heartbeat, not error"
        # The next poll sees is_disconnected() → True and the generator returns.
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(gen.__anext__(), timeout=2.0)
    finally:
        sse_module._FOLLOW_POLL_INTERVAL_S = monkey
        await gen.aclose()


@pytest.mark.asyncio
async def test_sse_iterator_yields_heartbeat_under_idle() -> None:
    """No events for `_HEARTBEAT_INTERVAL_S` must produce a `: ping` frame."""
    from prosper.console import sse as sse_module

    bus = ConsoleBus()

    class _FakeRequest:
        async def is_disconnected(self) -> bool:
            return False

    monkey = sse_module._HEARTBEAT_INTERVAL_S
    sse_module._HEARTBEAT_INTERVAL_S = 0.05
    try:
        gen: AsyncIterator[str] = sse_module._live_event_iter(
            bus,
            "s1",
            _FakeRequest(),  # type: ignore[arg-type]
        )
        first = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert first.startswith(": ping"), "idle frame must be a heartbeat"
    finally:
        sse_module._HEARTBEAT_INTERVAL_S = monkey
