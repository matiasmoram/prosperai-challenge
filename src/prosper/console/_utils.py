"""Shared helpers for the console module.

Imported by both `audit.py` (filesystem boundary) and `sse.py` (HTTP
boundary). Keeping the session_id regex check in one place means any
future tightening — e.g. length cap, reserved word list — applies to
both boundaries simultaneously, eliminating the risk of one path
silently accepting an id the other would reject.
"""

from __future__ import annotations

import re
from typing import Final

# Tight regex on session_id: only alphanumerics, hyphens, underscores.
# Defence against path traversal AND against any reserved character a
# URL parser may special-case. Single source of truth.
_VALID_SESSION_ID: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]+$")


def check_session_id(session_id: str) -> None:
    """Raise `ValueError` if `session_id` would compose unsafely into a path.

    Called by `audit.AuditJSONLWriter.path_for` and by the HTTP layer's
    `_validate_session_id_or_raise`. Both boundaries share this check so
    a malformed id cannot reach either the file system or the bus
    subscriber filter.
    """
    if not session_id or not _VALID_SESSION_ID.fullmatch(session_id):
        raise ValueError(f"invalid session_id {session_id!r}: must match [A-Za-z0-9_-]+")
