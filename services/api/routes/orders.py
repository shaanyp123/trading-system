"""services/api/routes/orders.py — `/api/orders`.

Backend-spec §4.1.5b. Phase 0 returns ``orders=[]`` since no execution path
emits orders yet (Week 4 Wed dispatcher wires the first INSERT).
"""

from __future__ import annotations

from typing import Literal

import structlog
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from services.api.db import get_session
from services.api.repos.phase1 import Phase1QueryRepo, PostgresPhase1QueryRepo
from services.api.routes._pagination import clamp_limit
from services.api.schemas.orders import OrdersResponse
from services.api.session import SessionContext, get_session_context

log = structlog.get_logger()

router = APIRouter()


def _get_repo(session: AsyncSession = Depends(get_session)) -> Phase1QueryRepo:
    return PostgresPhase1QueryRepo(session)


@router.get(
    "/api/orders",
    tags=["orders"],
    response_model=OrdersResponse,
)
async def list_orders(
    status: Literal["working", "filled", "cancelled"] | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    session: SessionContext = Depends(get_session_context),
    repo: Phase1QueryRepo = Depends(_get_repo),
) -> OrdersResponse:
    account_id = await repo.fetch_active_account_id()
    if account_id is None:
        return OrdersResponse(orders=[], next_cursor=None, has_more=False)

    _rows, next_cursor, has_more = await repo.fetch_orders_page(
        account_id,
        status=status,
        cursor=cursor,
        limit=clamp_limit(limit),
    )
    # Phase 0: orders table empty; mapper sketch in schemas/orders.py docstring
    return OrdersResponse(
        orders=[],
        next_cursor=next_cursor,
        has_more=has_more,
    )


__all__ = ["router"]
