"""services/api/routes/fills.py — `/api/fills`.

Backend-spec §4.1.5b. Phase 0 returns ``fills=[]`` since the ``fills`` table
is empty (no execution path); the spec response shape requires
``signal_uuid`` from a JOIN to orders.signal_id which the repo handles.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from services.api.db import get_session
from services.api.repos.phase1 import Phase1QueryRepo, PostgresPhase1QueryRepo
from services.api.routes._pagination import clamp_limit
from services.api.schemas.fills import FillsResponse
from services.api.session import SessionContext, get_session_context

log = structlog.get_logger()

router = APIRouter()


def _get_repo(session: AsyncSession = Depends(get_session)) -> Phase1QueryRepo:
    return PostgresPhase1QueryRepo(session)


@router.get(
    "/api/fills",
    tags=["fills"],
    response_model=FillsResponse,
)
async def list_fills(
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    session: SessionContext = Depends(get_session_context),
    repo: Phase1QueryRepo = Depends(_get_repo),
) -> FillsResponse:
    account_id = await repo.fetch_active_account_id()
    if account_id is None:
        return FillsResponse(fills=[], next_cursor=None, has_more=False)

    _rows, next_cursor, has_more = await repo.fetch_fills_page(
        account_id,
        cursor=cursor,
        limit=clamp_limit(limit),
    )
    # Phase 0: fills table empty; mapper sketch in schemas/fills.py docstring
    return FillsResponse(
        fills=[],
        next_cursor=next_cursor,
        has_more=has_more,
    )


__all__ = ["router"]
