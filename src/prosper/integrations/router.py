"""FastAPI router for the staff front-desk surface (Mail + Calendar).

Mounted on the console uvicorn under ``/frontdesk`` (see console/server.py),
reading the full-PII MailStore — a distinct staff trust tier. The calendar
fetch is injected (async callable) so this module does not import the EHR
client; bot.py supplies the real fetcher. See spec §8.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from loguru import logger

from prosper.integrations.mail import MailStore

_STATIC_DIR: Path = Path(__file__).parent / "static"

# Type alias for the injected calendar fetch callable.
# bot.py supplies an implementation backed by the real EHR client; tests
# supply a simple async stub. This module never imports EHRClient directly.
CalendarFetch = Callable[[date, date], Awaitable[list[dict[str, Any]]]]


def build_frontdesk_router(store: MailStore, calendar_fetch: CalendarFetch) -> APIRouter:
    """Return the ``/frontdesk`` router wired to a mail store + calendar fetcher."""
    router = APIRouter(prefix="/frontdesk", tags=["frontdesk"])

    @router.get("/health")
    async def health() -> JSONResponse:
        """Wiring probe: mail count + whether the EHR calendar is reachable.

        ``ehr_reachable`` attempts a today..today calendar fetch and reports
        ``False`` (rather than raising) when the EHR is down, so a staffer can
        confirm at a glance whether the front-desk is fully wired without
        reading server logs.
        """
        today = date.today()
        try:
            await calendar_fetch(today, today)
            ehr_reachable = True
        except Exception as exc:  # degrade to a False health flag, never raise
            logger.warning("frontdesk health: calendar unreachable: {}", exc)
            ehr_reachable = False
        return JSONResponse(
            {"mail_count": len(store.list_messages()), "ehr_reachable": ehr_reachable}
        )

    @router.get("/mail")
    async def mail_list() -> JSONResponse:
        """Newest-first outbound mail (full PII — staff tier only)."""
        return JSONResponse({"mail": [asdict(m) for m in store.list_messages()]})

    @router.get("/appointments")
    async def appointments(
        from_: date = Query(alias="from"),
        to: date = Query(...),
    ) -> JSONResponse:
        """Calendar entries for [from, to], proxied from the EHR."""
        try:
            entries = await calendar_fetch(from_, to)
        except Exception as exc:
            # Read-only staff convenience view: if the EHR is unreachable,
            # degrade to a 503 with the reason rather than letting the exception
            # bubble into an opaque 500 in the front-desk browser (rev-spec2 MED).
            logger.warning("frontdesk calendar fetch failed: {}", exc)
            return JSONResponse(
                {"error": "calendar_unavailable", "detail": str(exc)},
                status_code=503,
            )
        return JSONResponse({"entries": entries})

    @router.get("", include_in_schema=False)
    async def root() -> FileResponse:
        """Serve the single-page front-desk app."""
        return FileResponse(_STATIC_DIR / "index.html")

    @router.get("/static/{filename}", include_in_schema=False)
    async def static_asset(filename: str) -> FileResponse:
        """Serve a front-desk static asset by filename.

        An explicit route rather than ``router.mount(StaticFiles(...))`` —
        mounting a sub-app on a *prefixed* APIRouter does not route reliably
        (the asset 404s), so the SPA's own JS never loaded. Restricted to plain
        files directly inside the static dir: any path separator / ``..`` makes
        ``(_STATIC_DIR / filename).parent`` differ from the static root, so a
        traversal can never escape it.
        """
        target = (_STATIC_DIR / filename).resolve()
        if target.parent != _STATIC_DIR.resolve() or not target.is_file():
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(target)

    return router
