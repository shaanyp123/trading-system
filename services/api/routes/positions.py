"""services/api/routes/positions.py — `/api/positions/current`.

Backend-spec §4.1.5b. Phase 1 (post-2026-05-27): live mapper from
``positions_current`` rows to ``Position`` Pydantic objects. The
``/M2K`` first-real-fired position (2026-05-27) surfaced that the
Phase 0 stub was discarding the fetched rows + returning ``[]``
unconditionally — the frontend's positions table on the home page
saw no data even though the row was in the DB.

Field defaults for Phase 1:

* ``current_price`` — Phase 0 spec fallback to ``avg_cost`` (per
  schemas/positions.py docstring). Real mark-to-market lands when
  the periodic position MTM job is wired (Phase 2+).
* ``unrealized_pnl`` — read from ``positions_current.unrealized_pnl``
  if populated; ``Decimal(0)`` otherwise (the MTM job hasn't filled
  it in Phase 1).
* ``unrealized_pnl_pct_of_nav`` — computed as ``unrealized_pnl / NAV``
  using the latest ``balances`` row; ``Decimal(0)`` when NAV is
  unavailable or zero.
* ``cluster`` + ``contract_month`` — None for Phase 1. Real values
  require a JOIN against ``contracts`` (deferred).
* ``instrument_id`` — ``str(contract_id)`` for futures; ``market``
  for ETFs (where ``contract_id IS NULL``).
* ``managed_by_strategy_version`` — first 7 chars of
  ``managed_by_version`` (CHAR(40)) per spec §4.1.5b.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import structlog
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from services.api.config import APISettings, get_settings
from services.api.db import get_session
from services.api.position_mtm import compute_position_mtm
from services.api.repos.phase1 import Phase1QueryRepo, PostgresPhase1QueryRepo
from services.api.schemas.positions import Position, PositionsResponse
from services.api.session import SessionContext, get_session_context

log = structlog.get_logger()

router = APIRouter()


def _get_repo(session: AsyncSession = Depends(get_session)) -> Phase1QueryRepo:
    return PostgresPhase1QueryRepo(session)


def _to_decimal(value: object) -> Decimal:
    """Coerce a DB numeric or None to Decimal (None → 0)."""
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _to_int(value: object) -> int:
    """Coerce a DB numeric to int (loose object → int)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return int(str(value))


@router.get(
    "/api/positions/current",
    tags=["positions"],
    response_model=PositionsResponse,
)
async def positions_current(
    session: SessionContext = Depends(get_session_context),
    repo: Phase1QueryRepo = Depends(_get_repo),
    settings: APISettings = Depends(get_settings),
) -> PositionsResponse:
    now = datetime.now(tz=UTC)
    account_id = await repo.fetch_active_account_id()
    if account_id is None:
        return PositionsResponse(positions=[], as_of=now)

    rows = await repo.fetch_positions_current(account_id)
    if not rows:
        return PositionsResponse(positions=[], as_of=now)

    nav = await repo.fetch_latest_balance_nav(account_id)
    # Guard: NAV must be strictly positive to use as denominator. Phase 1
    # day-1 may have NAV=0 if no balance snapshot landed yet — use None
    # to signal "unavailable" so the pct field defaults to 0.
    nav_for_pct: Decimal | None = nav if (nav is not None and nav > 0) else None
    data_root = Path(settings.bar_sync_data_root)

    positions: list[Position] = []
    for r in rows:
        market = str(r["market"])
        contract_id_raw = r.get("contract_id")
        qty = _to_int(r["quantity"])
        avg_cost = _to_decimal(r["avg_cost"])
        # Phase 1 (post-2026-05-27 Task 3): compute current_price +
        # unrealized_pnl on-read from the LEAN on-disk daily zips
        # (services/api/position_mtm.py). Falls back to avg_cost +
        # unrealized_pnl=0 when the zip is missing or the market is
        # outside V1_EXPOSURE_METADATA. We DO NOT consult the
        # positions_current.unrealized_pnl column today — no periodic
        # MTM job writes it; a future PR can swap that in once a
        # writer exists.
        mtm = compute_position_mtm(market, qty, avg_cost, data_root)
        current_price = mtm.current_price
        unrealized_pnl = mtm.unrealized_pnl
        if nav_for_pct is None or unrealized_pnl == 0:
            unrealized_pct = Decimal("0")
        else:
            unrealized_pct = (unrealized_pnl / nav_for_pct).quantize(Decimal("0.0001"))
        managed_by_version = str(r["managed_by_version"])
        # instrument_id: stable identifier. Futures have contract_id (UUID)
        # which IS the stable instrument id across roll boundaries (the
        # contract row maps to a specific expiry). ETFs have NULL
        # contract_id; we use the market symbol as the id.
        instrument_id = str(contract_id_raw) if contract_id_raw is not None else market
        positions.append(
            Position(
                instrument_id=instrument_id,
                symbol=market,
                contract_month=None,  # Phase 1: contracts JOIN deferred
                qty=qty,
                avg_entry_price=avg_cost,
                current_price=current_price,
                unrealized_pnl=unrealized_pnl,
                unrealized_pnl_pct_of_nav=unrealized_pct,
                cluster=None,  # Phase 1: contracts JOIN deferred
                managed_by_strategy_version=managed_by_version[:7],
                price_as_of=mtm.price_as_of,
            )
        )

    # Headline "prices as of" = the most-recent bar date across positions.
    # max() over ISO YYYY-MM-DD strings sorts chronologically. None when no
    # position read a fresh bar.
    bar_dates = [p.price_as_of for p in positions if p.price_as_of is not None]
    prices_as_of = max(bar_dates) if bar_dates else None

    return PositionsResponse(positions=positions, as_of=now, prices_as_of=prices_as_of)


__all__ = ["router"]
