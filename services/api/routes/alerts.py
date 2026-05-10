"""services/api/routes/alerts.py — `/api/alerts`.

Backend-spec §4.1.5b. Phase 0 returns ``alerts=[]`` until the Week 4 Wed
dispatcher wires the first kill_switch_invoked / recon_break alert into the
``alerts`` table (then via the existing webhook_pusher pipeline → Discord +
Resend per services/webhook_pusher PR #44 / #50).

Note: the spec ``status`` filter values (``open|acknowledged|resolved``) map
to the DB columns:

  * ``open``         → ``NOT acknowledged AND resolved_at_utc IS NULL``
  * ``acknowledged`` → ``acknowledged AND resolved_at_utc IS NULL``
  * ``resolved``     → ``resolved_at_utc IS NOT NULL``

The repo handles the WHERE-clause translation; the route just passes the
filter string through.
"""

from __future__ import annotations

from typing import Literal

import structlog
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from services.api.db import get_session
from services.api.repos.phase1 import Phase1QueryRepo, PostgresPhase1QueryRepo
from services.api.routes._pagination import clamp_limit
from services.api.schemas.alerts import AlertsResponse
from services.api.session import SessionContext, get_session_context

log = structlog.get_logger()

router = APIRouter()


def _get_repo(session: AsyncSession = Depends(get_session)) -> Phase1QueryRepo:
    return PostgresPhase1QueryRepo(session)


@router.get(
    "/api/alerts",
    tags=["alerts"],
    response_model=AlertsResponse,
)
async def list_alerts(
    status: Literal["open", "acknowledged", "resolved"] | None = Query(default=None),
    severity: Literal["P0", "P1", "P2"] | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    session: SessionContext = Depends(get_session_context),
    repo: Phase1QueryRepo = Depends(_get_repo),
) -> AlertsResponse:
    account_id = await repo.fetch_active_account_id()
    if account_id is None:
        return AlertsResponse(alerts=[], next_cursor=None, has_more=False)

    _rows, next_cursor, has_more = await repo.fetch_alerts_page(
        account_id,
        status=status,
        severity=severity,
        cursor=cursor,
        limit=clamp_limit(limit),
    )
    # Phase 0: alerts table empty; the dispatcher wires the first INSERT
    # Week 4 Wed. Mapper concrete shape lives in services/api/schemas/alerts.py.
    return AlertsResponse(
        alerts=[],
        next_cursor=next_cursor,
        has_more=has_more,
    )


__all__ = ["router"]
