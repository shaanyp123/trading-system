"""services/api/routes/trades.py — `/api/trades` + `/api/trades/:id`.

Backend-spec §4.1.2 (lines 1766-1767). Phase 0 returns ``trades=[]`` since
the ``trades`` table is empty until the Week 7 Thu paper round-trip writes
the first fill-driven trade row. The route handlers mirror the Day-15
``orders`` / ``fills`` / ``alerts`` pattern — repo lookup, no-account
short-circuit, dataclass→Pydantic mapping at the route layer.

``id_prefix`` is the Phase-2 command-palette lookup key per spec §4.1.2;
Phase 1 accepts the param + threads it through to the repo even though
the palette UI itself doesn't ship until Phase 2 — locking the wire now
means the eventual palette doesn't trip a contract change.

``GET /api/trades/:id`` returns 404 ``TRADE_NOT_FOUND`` (NOT 404 with no
body) when the row doesn't exist, so the frontend's
``/trades/:id`` page can render a graceful "Trade not found" body
instead of Next's hard 404 — IG line 417 verbatim: "ensures Discord
deep-links don't 404".
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from services.api.db import get_session
from services.api.errors import AppError
from services.api.repos.phase1 import Phase1QueryRepo, PostgresPhase1QueryRepo
from services.api.routes._pagination import clamp_limit
from services.api.schemas.trades import (
    TradeDetail,
    TradesListResponse,
    TradeSummary,
)
from services.api.session import SessionContext, get_session_context

log = structlog.get_logger()

router = APIRouter()


def _get_repo(session: AsyncSession = Depends(get_session)) -> Phase1QueryRepo:
    return PostgresPhase1QueryRepo(session)


def _short_hash(full: str) -> str:
    """First 7 chars of the 40-char ``managed_by_version`` SHA-1.

    Mirrors ``git rev-parse --short`` default + frontend-spec §4.1.5b
    convention ("``managed_by_strategy_version: str  # 7-char short_hash``"
    on Position + Trade contracts).
    """
    return full[:7] if full else full


def _row_to_summary(row: dict[str, object]) -> TradeSummary:
    """Map a ``fetch_trades_page`` row dict to a ``TradeSummary``.

    Pydantic v2 coerces ``object`` values into the declared field type at
    construction time, so the per-field ``# type: ignore[arg-type]`` is the
    canonical pattern here — see ``services/api/schemas/fills.py`` docstring
    + the eventual fills mapper that lands when the table populates.
    """
    managed = str(row["managed_by_version"])
    return TradeSummary(
        id=str(row["id"]),
        env=str(row["env"]),
        market=str(row["market"]),
        direction=row["direction"],  # type: ignore[arg-type]  # TradeDirection literal
        state=row["state"],  # type: ignore[arg-type]  # TradeState literal
        opened_at_utc=row["opened_at_utc"],  # type: ignore[arg-type]
        closed_at_utc=row["closed_at_utc"],  # type: ignore[arg-type]
        total_quantity=row["total_quantity"],  # type: ignore[arg-type]
        avg_entry_price=row["avg_entry_price"],  # type: ignore[arg-type]
        avg_exit_price=row["avg_exit_price"],  # type: ignore[arg-type]
        realized_pnl_usd=row["realized_pnl_usd"],  # type: ignore[arg-type]
        entry_signal_id=str(row["entry_signal_id"]),
        managed_by_strategy_version=_short_hash(managed),
    )


def _row_to_detail(row: dict[str, object]) -> TradeDetail:
    """Map a ``fetch_trade_by_id`` row dict to a ``TradeDetail``."""
    managed = str(row["managed_by_version"])
    return TradeDetail(
        id=str(row["id"]),
        account_id=str(row["account_id"]),
        env=str(row["env"]),
        market=str(row["market"]),
        direction=row["direction"],  # type: ignore[arg-type]
        state=row["state"],  # type: ignore[arg-type]
        opened_at_utc=row["opened_at_utc"],  # type: ignore[arg-type]
        closed_at_utc=row["closed_at_utc"],  # type: ignore[arg-type]
        total_quantity=row["total_quantity"],  # type: ignore[arg-type]
        avg_entry_price=row["avg_entry_price"],  # type: ignore[arg-type]
        avg_exit_price=row["avg_exit_price"],  # type: ignore[arg-type]
        realized_pnl_usd=row["realized_pnl_usd"],  # type: ignore[arg-type]
        realized_commission_usd=row["realized_commission_usd"],  # type: ignore[arg-type]
        dividend_pnl_usd=row["dividend_pnl_usd"],  # type: ignore[arg-type]
        entry_signal_id=str(row["entry_signal_id"]),
        entry_order_id=str(row["entry_order_id"]),
        exit_order_id=str(row["exit_order_id"]) if row["exit_order_id"] is not None else None,
        managed_by_version=managed,
        managed_by_strategy_version=_short_hash(managed),
        strategy_hash=str(row["strategy_hash"]),
        parameter_set_hash=str(row["parameter_set_hash"]),
        slippage_calibration_version_id=str(row["slippage_calibration_version_id"]),
    )


@router.get(
    "/api/trades",
    tags=["trades"],
    response_model=TradesListResponse,
)
async def list_trades(
    from_utc: datetime | None = Query(default=None, alias="from"),
    to_utc: datetime | None = Query(default=None, alias="to"),
    market: str | None = Query(default=None, max_length=32),
    state: Literal["open_position", "closed", "stopped_out", "capacity_constrained"] | None = Query(
        default=None
    ),
    id_prefix: str | None = Query(default=None, max_length=36),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    session: SessionContext = Depends(get_session_context),
    repo: Phase1QueryRepo = Depends(_get_repo),
) -> TradesListResponse:
    account_id = await repo.fetch_active_account_id()
    if account_id is None:
        return TradesListResponse(trades=[], next_cursor=None, has_more=False)

    rows, next_cursor, has_more = await repo.fetch_trades_page(
        account_id,
        from_utc=from_utc,
        to_utc=to_utc,
        market=market,
        state=state,
        id_prefix=id_prefix,
        cursor=cursor,
        limit=clamp_limit(limit),
    )
    return TradesListResponse(
        trades=[_row_to_summary(r) for r in rows],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get(
    "/api/trades/{trade_id}",
    tags=["trades"],
    response_model=TradeDetail,
)
async def get_trade(
    trade_id: UUID = Path(...),
    session: SessionContext = Depends(get_session_context),
    repo: Phase1QueryRepo = Depends(_get_repo),
) -> TradeDetail:
    account_id = await repo.fetch_active_account_id()
    if account_id is None:
        raise AppError(
            error_code="TRADE_NOT_FOUND",
            message="No trade exists with the requested ID.",
            status_code=404,
            details={"trade_id": str(trade_id)},
        )
    row = await repo.fetch_trade_by_id(account_id, trade_id)
    if row is None:
        raise AppError(
            error_code="TRADE_NOT_FOUND",
            message="No trade exists with the requested ID.",
            status_code=404,
            details={"trade_id": str(trade_id)},
        )
    return _row_to_detail(row)


__all__ = ["router"]
