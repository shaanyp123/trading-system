"""services/api/routes/positions.py — `/api/positions/current`.

Backend-spec §4.1.5b; crypto-pivot §3.9 (``Docs/crypto-pivot-delta-spec.md``)
rebuilt the mapper for the CDE-perp book. Rows come from
``positions_current`` LEFT-JOINed to ``contracts`` (asset root +
runtime-discovered contract size, [A13]) and price via the three-rung
**mark ladder**, best rung first:

1. **Live WS mark** (``mark_source="live_ws"``) — the api-resident
   market-data worker's MarkStore via :mod:`services.api.marks`; marks
   older than the locked 3-minute staleness policy are treated as
   absent. uPnL = ``(mark - avg_cost) x qty x multiplier`` in Decimal.
2. **Recon EOD mark** (``mark_source="recon_eod"``) — the
   ``positions_current.unrealized_pnl`` column the 00:15 UTC recon cycle
   now writes (``services/reconciliation/eod_cycle.py`` — the old "no
   writer exists" note is stale since C0-B5). Venue-supplied uPnL only;
   ``current_price`` stays at ``avg_cost`` because the venue mark price
   isn't persisted on the row.
3. **Avg-cost fallback** (``mark_source="fallback_avg_cost"``) —
   unchanged Phase-1 contract via ``compute_position_mtm``.

Stop enrichment (additive §3.9 fields): the strategy worker's
``strategy_worker_status.engine_state`` JSONB (per-asset
``AssetRuntime.to_json`` — Decimals as strings) supplies the client
2xATR stop + the native-stop order ids; the native stop's resting price
resolves through the latest ``orders`` row for its client_order_id.

Backwards compatibility is the contract: every pre-pivot wire field
keeps its exact name/semantics; ``contract_month`` / ``prices_as_of``
stay (null) for the deployed frontend until the §3.9 frontend PR lands.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import structlog
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from services.api import marks
from services.api.db import get_session
from services.api.exposure import lookup_market_meta
from services.api.position_mtm import compute_position_mtm, unrealized_pnl
from services.api.repos.cycle import CycleQueryRepo, PostgresCycleQueryRepo
from services.api.repos.phase1 import Phase1QueryRepo, PostgresPhase1QueryRepo
from services.api.schemas.positions import Position, PositionsResponse
from services.api.session import SessionContext, get_session_context

log = structlog.get_logger()

router = APIRouter()


def _get_repo(session: AsyncSession = Depends(get_session)) -> Phase1QueryRepo:
    return PostgresPhase1QueryRepo(session)


def _get_cycle_repo(session: AsyncSession = Depends(get_session)) -> CycleQueryRepo:
    return PostgresCycleQueryRepo(session)


def _to_decimal(value: object) -> Decimal:
    """Coerce a DB numeric or None to Decimal (None → 0)."""
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _to_decimal_or_none(value: object) -> Decimal | None:
    """Loose object → Decimal; None on absent/unparseable (engine_state strings)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


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
    cycle_repo: CycleQueryRepo = Depends(_get_cycle_repo),
) -> PositionsResponse:
    now = datetime.now(tz=UTC)
    account_id = await repo.fetch_active_account_id()
    if account_id is None:
        return PositionsResponse(positions=[], as_of=now)

    rows = await repo.fetch_positions_current_with_contract(account_id)
    if not rows:
        return PositionsResponse(positions=[], as_of=now)

    nav = await repo.fetch_latest_balance_nav(account_id)
    # Guard: NAV must be strictly positive to use as denominator. Day-1
    # may have NAV=0 if no balance snapshot landed yet — use None to
    # signal "unavailable" so the pct field defaults to 0.
    nav_for_pct: Decimal | None = nav if (nav is not None and nav > 0) else None

    # Worker engine state (client/native stops), keyed by asset root —
    # absent (fresh DB / worker never ran) degrades every stop field to
    # None, never an error.
    worker_status = await cycle_repo.fetch_worker_status(account_id)
    engine_state = worker_status.engine_state if worker_status is not None else {}

    mark_store = marks.get_mark_store()

    positions: list[Position] = []
    for r in rows:
        market = str(r["market"])
        contract_id_raw = r.get("contract_id")
        qty = _to_int(r["quantity"])
        avg_cost = _to_decimal(r["avg_cost"])
        market_root_raw = r.get("market_root")
        market_root = str(market_root_raw) if market_root_raw is not None else None
        multiplier = _to_decimal_or_none(r.get("multiplier")) or Decimal("1")

        # --- mark ladder (module docstring) -----------------------------
        current_price: Decimal
        pnl: Decimal
        mark_source: str
        mark_observed_at: datetime | None = None
        live = marks.fresh_mark(mark_store, market, now) if mark_store is not None else None
        recon_upnl = _to_decimal_or_none(r.get("unrealized_pnl"))
        if live is not None:
            current_price, mark_observed_at = live
            pnl = unrealized_pnl(current_price, avg_cost, qty, multiplier)
            mark_source = "live_ws"
        elif recon_upnl is not None:
            # Rung 2: venue uPnL written by the 00:15 recon; no persisted
            # mark price on the row, so current_price honestly falls back
            # to avg_cost while the uPnL is real.
            current_price = avg_cost
            pnl = recon_upnl
            mark_source = "recon_eod"
            last_mark_ts = r.get("last_mark_ts")
            if isinstance(last_mark_ts, datetime):
                mark_observed_at = last_mark_ts
        else:
            mtm = compute_position_mtm(market_root or market, qty, avg_cost)
            current_price = mtm.current_price
            pnl = mtm.unrealized_pnl
            mark_source = "fallback_avg_cost"

        if nav_for_pct is None or pnl == 0:
            unrealized_pct = Decimal("0")
        else:
            unrealized_pct = (pnl / nav_for_pct).quantize(Decimal("0.0001"))

        # --- cluster: keyed by contracts.market_root so BTC/ETH map to
        # the crypto cluster (pre-§3.9 the raw product_id missed and
        # positions misbucketed) -----------------------------------------
        meta = lookup_market_meta(market, market_root=market_root)
        cluster = meta.cluster if meta is not None else None

        # --- stop enrichment from worker engine_state --------------------
        client_stop: Decimal | None = None
        native_stop_price: Decimal | None = None
        native_stop_resting: bool | None = None
        asset_state = engine_state.get(market_root) if market_root is not None else None
        if isinstance(asset_state, dict):
            client_stop = _to_decimal_or_none(asset_state.get("client_stop_level"))
            native_stop_resting = asset_state.get("native_stop_order_id") is not None
            stop_coid = asset_state.get("native_stop_client_order_id")
            if isinstance(stop_coid, str) and stop_coid:
                stop_order = await repo.fetch_latest_stop_order(stop_coid)
                if stop_order is not None:
                    native_stop_price = _to_decimal_or_none(stop_order.get("stop_price"))

        managed_by_version = str(r["managed_by_version"])
        # instrument_id: stable identifier. contract_id (UUID) when the
        # contracts row exists; the market string otherwise.
        instrument_id = str(contract_id_raw) if contract_id_raw is not None else market
        positions.append(
            Position(
                instrument_id=instrument_id,
                symbol=market,
                contract_month=None,  # retiring — null until the frontend PR
                qty=qty,
                avg_entry_price=avg_cost,
                current_price=current_price,
                unrealized_pnl=pnl,
                unrealized_pnl_pct_of_nav=unrealized_pct,
                cluster=cluster,
                managed_by_strategy_version=managed_by_version[:7],
                price_as_of=None,  # retiring — null until the frontend PR
                asset=market_root,
                client_stop_price=client_stop,
                native_stop_price=native_stop_price,
                native_stop_resting=native_stop_resting,
                mark_observed_at_utc=mark_observed_at,
                mark_source=mark_source,
            )
        )

    return PositionsResponse(positions=positions, as_of=now, prices_as_of=None)


__all__ = ["router"]
