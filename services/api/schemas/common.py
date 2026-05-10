"""services/api/schemas/common.py — shared request/response primitives.

Pulled into its own module so cursor-pagination + the never-run-sentinel
constant don't have to live inside an unrelated endpoint file.

The ``EPOCH_SENTINEL_UTC`` is the canonical "never-run" value for nullable
timestamps that the frontend renders via "X minutes ago"-style helpers; using
1970-01-01 UTC means the formatter always has a real datetime to subtract from
``server_now`` (just yields a comically large duration when no real ping has
landed yet). Documented here so future endpoint authors have one place to
look.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

#: Sentinel timestamp used for "never observed" fields in Phase 0 responses.
#: Mapped onto fields like ``watchdog_last_ping_utc`` / ``last_check_utc`` when
#: the underlying table is empty. Frontend renders these as ages relative to
#: ``server_now`` — a 1970 baseline produces an obviously-stale display rather
#: than a NoneType crash.
EPOCH_SENTINEL_UTC: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)


class CursorPageParams(BaseModel):
    """Standard cursor-pagination query parameters per backend-spec §4.1.6.

    ``cursor`` is opaque to the client; the server encodes ``(created_at, id)``
    pairs internally. ``limit`` defaults to 50, max 200 — values outside that
    range fail Pydantic validation with the canonical envelope.
    """

    model_config = ConfigDict(extra="forbid")

    cursor: str | None = None
    limit: int = Field(default=50, ge=1, le=200)


__all__ = ["EPOCH_SENTINEL_UTC", "CursorPageParams"]
