"""services/api/routes/fills.py — `/api/fills`.

Backend-spec §4.1.5b. Phase 1 (post-2026-05-27): live mapper from
``fills`` JOIN ``orders`` rows to ``Fill`` Pydantic objects. The Phase
0 stub was discarding the fetched rows + returning ``[]`` even after
real fills landed — surfaced by the 2026-05-27 dashboard audit
("Recent Fills" widget showing 0 despite 5+ fills in the DB).

Field defaults for Phase 1:

* ``instrument_id`` — uses ``market`` (same Phase 1 simplification as
  positions). Real contract_id-vs-market discrimination requires a
  contracts JOIN, deferred.
* ``slippage_bps`` — ``fills.realized_slippage_bps`` if set;
  ``Decimal(0)`` otherwise (the slippage calibration job populates
  it asynchronously per backend-spec §2.14).
* ``expected_price`` — ``Decimal(0)`` Phase 1 placeholder. Real value
  needs ``signals.expected_fill_price`` via the orders.signal_id JOIN
  (signals doesn't always carry it; spec field is non-nullable so we
  default to 0 — a follow-up PR will widen the schema to Optional).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import structlog
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from services.api.db import get_session
from services.api.repos.phase1 import Phase1QueryRepo, PostgresPhase1QueryRepo
from services.api.routes._pagination import clamp_limit
from services.api.schemas.fills import Fill, FillSide, FillsResponse
from services.api.session import SessionContext, get_session_context

log = structlog.get_logger()

router = APIRouter()


def _get_repo(session: AsyncSession = Depends(get_session)) -> Phase1QueryRepo:
    return PostgresPhase1QueryRepo(session)


def _to_decimal(value: object) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _to_int(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return int(str(value))


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

    rows, next_cursor, has_more = await repo.fetch_fills_page(
        account_id,
        cursor=cursor,
        limit=clamp_limit(limit),
    )

    fills: list[Fill] = []
    for r in rows:
        side_raw = str(r["side"]) if r.get("side") is not None else "buy"
        side: FillSide = "buy" if side_raw == "buy" else "sell"
        market = str(r["market"]) if r.get("market") is not None else ""
        filled_at_raw = r["filled_at_utc"]
        filled_at = (
            filled_at_raw
            if isinstance(filled_at_raw, datetime)
            else datetime.fromisoformat(str(filled_at_raw))
        )
        fills.append(
            Fill(
                fill_uuid=str(r["fill_id"]),
                order_uuid=str(r["order_id"]),
                signal_uuid=str(r["signal_id"]) if r.get("signal_id") is not None else "",
                # Phase 1 simplification: use market symbol as instrument_id
                # (matches positions endpoint's ETF fallback; contracts JOIN
                # for futures-specific contract_id deferred).
                instrument_id=market,
                side=side,
                qty=_to_int(r["fill_quantity"]),
                price=_to_decimal(r["fill_price"]),
                slippage_bps=_to_decimal(r.get("realized_slippage_bps")),
                expected_price=Decimal("0"),  # Phase 1 placeholder
                filled_at=filled_at,
            )
        )

    return FillsResponse(
        fills=fills,
        next_cursor=next_cursor,
        has_more=has_more,
    )


__all__ = ["router"]
