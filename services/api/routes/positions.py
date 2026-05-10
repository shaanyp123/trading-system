"""services/api/routes/positions.py — `/api/positions/current`.

Backend-spec §4.1.5b. Phase 0 returns ``positions=[]`` since the
``positions_current`` table is empty (no fills landed yet).
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from services.api.db import get_session
from services.api.repos.phase1 import Phase1QueryRepo, PostgresPhase1QueryRepo
from services.api.schemas.positions import PositionsResponse
from services.api.session import SessionContext, get_session_context

log = structlog.get_logger()

router = APIRouter()


def _get_repo(session: AsyncSession = Depends(get_session)) -> Phase1QueryRepo:
    return PostgresPhase1QueryRepo(session)


@router.get(
    "/api/positions/current",
    tags=["positions"],
    response_model=PositionsResponse,
)
async def positions_current(
    session: SessionContext = Depends(get_session_context),
    repo: Phase1QueryRepo = Depends(_get_repo),
) -> PositionsResponse:
    now = datetime.now(tz=UTC)
    account_id = await repo.fetch_active_account_id()
    if account_id is None:
        return PositionsResponse(positions=[], as_of=now)

    _rows = await repo.fetch_positions_current(account_id)
    # Phase 0: positions_current is empty so _rows is always []. Mapper
    # sketch lives in services/api/schemas/positions.py docstring; concrete
    # row-to-Position transformation lands when fills start populating
    # positions_current via the Week 4 Wed dispatcher + reconciliation
    # services.
    return PositionsResponse(
        positions=[],
        as_of=now,
    )


__all__ = ["router"]
