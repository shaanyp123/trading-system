"""services/api/routes/today.py — `/api/health-score`, `/api/today/digest`,
`/api/today/exit-proximity`.

Backend-spec §4.1.5b. Phase 0 returns the Today landing-page first-paint
envelope with empty/zero data so the frontend (when it lands Week 6) can
render against a stable contract from day one.

Crypto-pivot §3.9 rewired ``/api/today/exit-proximity`` from the dormant
CME ``exit_proximity`` table to a live composition over the held book +
strategy-worker engine state (client 2xATR / native 3xATR stops) + the
api-resident mark store. See ``list_exit_proximity``.

Both endpoints share a ``_build_phase0_health_score`` helper since
``/today/digest`` denormalizes the health score body (saves a round-trip on
landing-page paint per the spec: "denormalized: includes ``health_score``
body for landing-page paint without an extra round-trip").
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from services.api import marks
from services.api.config import APISettings, get_settings
from services.api.db import get_session
from services.api.exposure import compute_exposure_breakdown
from services.api.repos.cycle import CycleQueryRepo, PostgresCycleQueryRepo
from services.api.repos.phase1 import Phase1QueryRepo, PostgresPhase1QueryRepo
from services.api.schemas.exit_proximity import (
    ExitProximityResponse,
    PositionStopProximityView,
    build_position_stop_view,
    sort_stop_views,
)
from services.api.schemas.health_score import (
    HealthScoreComponent,
    HealthScoreResponse,
)
from services.api.schemas.today import (
    PnLSummary,
    TodayDigestResponse,
)
from services.api.session import SessionContext, get_session_context

log = structlog.get_logger()

router = APIRouter()


def _get_repo(session: AsyncSession = Depends(get_session)) -> Phase1QueryRepo:
    return PostgresPhase1QueryRepo(session)


def _get_cycle_repo(session: AsyncSession = Depends(get_session)) -> CycleQueryRepo:
    return PostgresCycleQueryRepo(session)


# Five locked component names per backend-spec §4.1.5b. Order matters —
# matches the spec's table; OpenAPI schema serializes deterministically.
_COMPONENT_DEFS: list[tuple[str, str]] = [
    ("live_sharpe_vs_backtest", "60-day rolling"),
    ("slippage_drift", "30-day rolling"),
    ("hit_rate", "60-day rolling"),
    ("capacity_headroom", "current"),
    ("days_since_recon_break", "current"),
]


def _build_phase0_health_score(now: datetime) -> HealthScoreResponse:
    """Compose the empty-data Health Score envelope.

    Phase 0 returns ``insufficient_data=True`` with all components at
    ``score=None`` and equal 20% weights. The composite is 0 (the schema
    requires a non-null int 0-100 even when ``insufficient_data=True`` so
    the frontend can render a degraded state without a NoneType crash).
    Traffic light is "yellow" — neither green (would be misleading) nor
    red (would imply something has actually gone wrong).
    """
    components = [
        HealthScoreComponent(
            name=name,  # type: ignore[arg-type]
            weight_pct=20,
            window=window,
            score=None,
            insufficient_data=True,
        )
        for name, window in _COMPONENT_DEFS
    ]
    return HealthScoreResponse(
        composite=0,
        traffic_light="yellow",
        components=components,
        insufficient_data=True,
        computed_at=now,
    )


@router.get(
    "/api/health-score",
    tags=["today"],
    response_model=HealthScoreResponse,
)
async def health_score(
    session: SessionContext = Depends(get_session_context),
) -> HealthScoreResponse:
    return _build_phase0_health_score(datetime.now(tz=UTC))


@router.get(
    "/api/today/digest",
    tags=["today"],
    response_model=TodayDigestResponse,
)
async def today_digest(
    session: SessionContext = Depends(get_session_context),
    repo: Phase1QueryRepo = Depends(_get_repo),
    settings: APISettings = Depends(get_settings),
) -> TodayDigestResponse:
    now = datetime.now(tz=UTC)
    account_id = await repo.fetch_active_account_id()

    queued_signals = await repo.count_pending_signals(account_id) if account_id else 0
    alert_counts = await repo.count_open_alerts_by_severity(account_id) if account_id else None
    risk_row = await repo.fetch_risk_state_current(account_id) if account_id else None
    state_value: Literal["NORMAL", "HALT_NEW", "CONVALESCENT", "VACATION"] = (
        risk_row.state if risk_row else "NORMAL"
    )
    state_severity = risk_row.severity if risk_row else None
    if risk_row and risk_row.vacation_active:
        state_value = "VACATION"
        state_severity = None

    # Phase 1 (post-2026-05-27): real realized-PnL aggregates from the
    # trades table. Spec §2.2.2 B asks for session-aligned (17:00 ET)
    # boundaries; this query uses UTC-calendar-aligned boundaries as a
    # pragmatic Phase 1 simplification — the two diverge only for
    # trades closed between 00:00 UTC and 21:00 UTC. Unrealized PnL on
    # open positions is NOT included here (lives on
    # ``Position.unrealized_pnl_pct_of_nav``).
    if account_id is not None:
        daily_pnl, weekly_pnl, monthly_pnl, yearly_pnl = await repo.fetch_realized_pnl_aggregates(
            account_id
        )
        positions_rows = await repo.fetch_positions_current(account_id)
        nav = await repo.fetch_latest_balance_nav(account_id)
    else:
        daily_pnl = weekly_pnl = monthly_pnl = yearly_pnl = Decimal("0")
        positions_rows = []
        nav = None

    return TodayDigestResponse(
        health_score=_build_phase0_health_score(now),
        pnl=PnLSummary(
            daily_pnl=daily_pnl,
            weekly_pnl=weekly_pnl,
            monthly_pnl=monthly_pnl,
            yearly_pnl=yearly_pnl,
        ),
        exposure=compute_exposure_breakdown(positions_rows, nav),
        queued_signals_count=queued_signals,
        active_alerts_count_by_severity={
            "P0": alert_counts.p0 if alert_counts else 0,
            "P1": alert_counts.p1 if alert_counts else 0,
            "P2": alert_counts.p2 if alert_counts else 0,
        },
        state=state_value,
        state_severity=state_severity,
        agent_status="disabled",  # Phase 2 surface; Phase 0 = disabled
        environment=settings.environment,
        # 7-char convention per backend-spec §4.1.5b
        # (``git rev-parse --short`` default). In dev/test ``settings.version``
        # is a short tag like "dev" or "test" — pass through as-is; the schema
        # has no length constraint. Production substitutes the build's SHA at
        # image-build time per services/api/Dockerfile.
        deployed_strategy_version=(settings.version or "0000000")[:7],
    )


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


async def _build_stop_proximity_rows(
    repo: Phase1QueryRepo,
    cycle_repo: CycleQueryRepo,
    account_id: UUID,
    now: datetime,
) -> list[PositionStopProximityView]:
    """Compose the §3.9 stop-proximity rows for one account.

    Inputs (all degrade to None/False fields, never a 5xx):

      * ``positions_current`` + ``contracts`` JOIN — the held book, netted
        per market (a roll boundary can legally split one product across
        rows; mirrors ``fetch_positions_for_lean_cycle``'s netting).
      * Worker ``engine_state`` — client 2xATR stop, native-stop order
        ids, stopped_on_date; ``marks_stale`` from the same status row
        (True — conservative — when the worker has never reported).
      * Latest native-stop ``orders`` row — the venue-resting price.
      * Live mark store (``services/api/marks``) keyed by product_id.
    """
    rows = await repo.fetch_positions_current_with_contract(account_id)
    worker = await cycle_repo.fetch_worker_status(account_id)
    engine_state = worker.engine_state if worker is not None else {}
    marks_stale = worker.marks_stale if worker is not None else True
    today_iso = now.date().isoformat()
    mark_store = marks.get_mark_store()

    # Net per market (product): SUM(quantity) + qty-weighted avg_cost.
    net_qty: dict[str, int] = {}
    weighted_cost: dict[str, Decimal] = {}
    market_root_by_market: dict[str, str | None] = {}
    for r in rows:
        market = str(r["market"])
        qty = _to_int(r["quantity"])
        avg_cost = _to_decimal_or_none(r.get("avg_cost")) or Decimal("0")
        net_qty[market] = net_qty.get(market, 0) + qty
        weighted_cost[market] = weighted_cost.get(market, Decimal("0")) + avg_cost * qty
        if market_root_by_market.get(market) is None:
            root_raw = r.get("market_root")
            market_root_by_market[market] = str(root_raw) if root_raw is not None else None

    views: list[PositionStopProximityView] = []
    for market in sorted(net_qty):
        contracts = net_qty[market]
        if contracts == 0:
            continue  # legs net flat — nothing at risk of a stop
        entry_vwap = (weighted_cost[market] / contracts).quantize(Decimal("0.00000001"))
        market_root = market_root_by_market.get(market)

        client_stop: Decimal | None = None
        native_stop_price: Decimal | None = None
        native_stop_resting = False
        stopped_today = False
        asset_state = engine_state.get(market_root) if market_root is not None else None
        if isinstance(asset_state, dict):
            client_stop = _to_decimal_or_none(asset_state.get("client_stop_level"))
            native_stop_resting = asset_state.get("native_stop_order_id") is not None
            stopped_today = asset_state.get("stopped_on_date") == today_iso
            stop_coid = asset_state.get("native_stop_client_order_id")
            if isinstance(stop_coid, str) and stop_coid:
                stop_order = await repo.fetch_latest_stop_order(stop_coid)
                if stop_order is not None:
                    native_stop_price = _to_decimal_or_none(stop_order.get("stop_price"))

        mark: Decimal | None = None
        if mark_store is not None:
            live = marks.fresh_mark(mark_store, market, now)
            if live is not None:
                mark = live[0]

        views.append(
            build_position_stop_view(
                product_id=market,
                asset=market_root,
                contracts=contracts,
                entry_vwap=entry_vwap,
                mark=mark,
                marks_stale=marks_stale,
                client_stop_price=client_stop,
                native_stop_price=native_stop_price,
                native_stop_resting=native_stop_resting,
                stopped_today=stopped_today,
            )
        )
    return sort_stop_views(views)


@router.get(
    "/api/today/exit-proximity",
    tags=["today"],
    response_model=ExitProximityResponse,
)
async def list_exit_proximity(
    session: SessionContext = Depends(get_session_context),
    repo: Phase1QueryRepo = Depends(_get_repo),
    cycle_repo: CycleQueryRepo = Depends(_get_cycle_repo),
) -> ExitProximityResponse:
    """Per-position stop proximity for the Today "Stops" view.

    Crypto-pivot §3.9 recomposition (URL preserved, shape rewritten): the
    dormant ``exit_proximity`` table is no longer read; rows compose from
    the held book + worker engine state + native-stop orders + live marks
    (see :func:`_build_stop_proximity_rows`). Ordered closest-to-stop,
    stopped/unprotected rows first.

    **Auth.** Mirrors the other Today endpoints + ``GET /api/signals/proximity``
    — the same ``Depends(get_session_context)`` dependency enforces the
    operator session cookie / bearer. No special service-account or role check.

    **Empty-state contract.** No active account or no open positions
    returns ``{"positions": [], "server_now": ...}`` — HTTP 200, never a
    5xx (fresh-DB contract).

    The session variable is unused; binding it keeps the auth dependency in the
    function signature (FastAPI resolves dependencies for side effects even when
    the result isn't used) — identical to ``list_signal_proximity``.
    """
    _ = session  # auth enforced via the dependency above; result unused
    now = datetime.now(tz=UTC)
    account_id = await repo.fetch_active_account_id()
    if account_id is None:
        return ExitProximityResponse(positions=[], server_now=now)
    positions = await _build_stop_proximity_rows(repo, cycle_repo, account_id, now)
    return ExitProximityResponse(positions=positions, server_now=now)


__all__ = ["router"]
