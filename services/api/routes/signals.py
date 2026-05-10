"""services/api/routes/signals.py — `/api/signals*` Phase-1 endpoints.

Backend-spec §4.1.2:

  * ``GET  /api/signals?status=&limit=&cursor=`` — list signals.
  * ``POST /api/signals/:id/approve`` — approve a pending signal.
  * ``POST /api/signals/:id/reject``  — reject with embedded diary entry.
  * ``POST /api/signals/:id/defer``   — defer with embedded diary entry.

Day 15 ships the LIST endpoint as a real DB query (returns ``items=[]`` since
the signals table is empty in Phase 0); the three POST endpoints validate
their request bodies via Pydantic + return 501 ``SIGNAL_HANDLER_NOT_WIRED``
until the Week 4 Wed dispatcher PR (forbidden whitelist) wires the real
state-mutating handlers.

The Pydantic-side validation IS exercised today: bad payload bodies return
422 with the canonical error envelope (per the existing
``RequestValidationError`` handler in services/api/errors.py).
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from services.api.db import get_session
from services.api.errors import AppError
from services.api.repos.phase1 import (
    Phase1QueryRepo,
    PostgresPhase1QueryRepo,
)
from services.api.routes._pagination import clamp_limit
from services.api.schemas.signals import (
    SignalApproveRequest,
    SignalDeferRequest,
    SignalListResponse,
    SignalRejectRequest,
)
from services.api.session import SessionContext, get_session_context

log = structlog.get_logger()

router = APIRouter()


def _get_repo(session: AsyncSession = Depends(get_session)) -> Phase1QueryRepo:
    return PostgresPhase1QueryRepo(session)


@router.get(
    "/api/signals",
    tags=["signals"],
    response_model=SignalListResponse,
)
async def list_signals(
    status: Literal[
        "pending",
        "approved",
        "rejected",
        "deferred",
        "expired",
        "working",
        "partially_filled",
        "filled",
        "cancelled",
        "closed",
        "stopped_out",
    ]
    | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    session: SessionContext = Depends(get_session_context),
    repo: Phase1QueryRepo = Depends(_get_repo),
) -> SignalListResponse:
    account_id = await repo.fetch_active_account_id()
    if account_id is None:
        # No account row exists yet (Week 6 setup_token flow creates it).
        # Return empty list rather than 404; the frontend's first paint
        # works against an empty signals list and re-fetches when the
        # account materializes.
        return SignalListResponse(items=[], next_cursor=None, has_more=False)

    _rows, next_cursor, has_more = await repo.fetch_signals_page(
        account_id,
        status=status,
        cursor=cursor,
        limit=clamp_limit(limit),
    )
    # Phase 0: signals table is empty, so _rows is always []. The deliberate
    # empty-list return is the spec's scaffold-only shape; the row-to-
    # SignalSummary mapper lands when the dispatcher PR (Week 4 Wed) wires
    # signal emission and we have real data to map.
    return SignalListResponse(
        items=[],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.post(
    "/api/signals/{signal_id}/approve",
    tags=["signals"],
    status_code=501,
)
async def approve_signal(
    signal_id: UUID,
    body: SignalApproveRequest,
    session: SessionContext = Depends(get_session_context),
) -> None:
    """Body validation runs; the handler is 501-stubbed until Week 4 Wed."""
    log.info(
        "signal_approve_stubbed",
        signal_id=str(signal_id),
        override_size=body.override_size,
    )
    raise AppError(
        error_code="SIGNAL_HANDLER_NOT_WIRED",
        message=(
            "Signal approve/reject/defer handlers wire up in the Week 4 Wed "
            "risk-dispatcher PR (forbidden whitelist). Day 15 ships the route "
            "scaffold + body validation only."
        ),
        status_code=501,
    )


@router.post(
    "/api/signals/{signal_id}/reject",
    tags=["signals"],
    status_code=501,
)
async def reject_signal(
    signal_id: UUID,
    body: SignalRejectRequest,
    session: SessionContext = Depends(get_session_context),
) -> None:
    log.info(
        "signal_reject_stubbed",
        signal_id=str(signal_id),
        diary_tag=body.decision_diary_entry.tag,
    )
    raise AppError(
        error_code="SIGNAL_HANDLER_NOT_WIRED",
        message=(
            "Signal approve/reject/defer handlers wire up in the Week 4 Wed "
            "risk-dispatcher PR (forbidden whitelist). Day 15 ships the route "
            "scaffold + body validation only."
        ),
        status_code=501,
    )


@router.post(
    "/api/signals/{signal_id}/defer",
    tags=["signals"],
    status_code=501,
)
async def defer_signal(
    signal_id: UUID,
    body: SignalDeferRequest,
    session: SessionContext = Depends(get_session_context),
) -> None:
    log.info(
        "signal_defer_stubbed",
        signal_id=str(signal_id),
        diary_tag=body.decision_diary_entry.tag,
    )
    raise AppError(
        error_code="SIGNAL_HANDLER_NOT_WIRED",
        message=(
            "Signal approve/reject/defer handlers wire up in the Week 4 Wed "
            "risk-dispatcher PR (forbidden whitelist). Day 15 ships the route "
            "scaffold + body validation only."
        ),
        status_code=501,
    )


__all__ = ["router"]
