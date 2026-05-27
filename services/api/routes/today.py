"""services/api/routes/today.py — `/api/health-score` + `/api/today/digest`.

Backend-spec §4.1.5b. Phase 0 returns the Today landing-page first-paint
envelope with empty/zero data so the frontend (when it lands Week 6) can
render against a stable contract from day one.

Both endpoints share a ``_build_phase0_health_score`` helper since
``/today/digest`` denormalizes the health score body (saves a round-trip on
landing-page paint per the spec: "denormalized: includes ``health_score``
body for landing-page paint without an extra round-trip").
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

import structlog
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from services.api.config import APISettings, get_settings
from services.api.db import get_session
from services.api.repos.phase1 import Phase1QueryRepo, PostgresPhase1QueryRepo
from services.api.schemas.health_score import (
    HealthScoreComponent,
    HealthScoreResponse,
)
from services.api.schemas.today import (
    ExposureBreakdown,
    PnLSummary,
    TodayDigestResponse,
)
from services.api.session import SessionContext, get_session_context

log = structlog.get_logger()

router = APIRouter()


def _get_repo(session: AsyncSession = Depends(get_session)) -> Phase1QueryRepo:
    return PostgresPhase1QueryRepo(session)


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
    else:
        daily_pnl = weekly_pnl = monthly_pnl = yearly_pnl = Decimal("0")

    return TodayDigestResponse(
        health_score=_build_phase0_health_score(now),
        pnl=PnLSummary(
            daily_pnl=daily_pnl,
            weekly_pnl=weekly_pnl,
            monthly_pnl=monthly_pnl,
            yearly_pnl=yearly_pnl,
        ),
        exposure=ExposureBreakdown(
            by_cluster={
                "equity_index": Decimal("0"),
                "commodity": Decimal("0"),
                "rates_bonds": Decimal("0"),
                "crypto": Decimal("0"),
                "fx": Decimal("0"),
            },
            gross_exposure_pct_nav=Decimal("0"),
            net_exposure_pct_nav=Decimal("0"),
        ),
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


__all__ = ["router"]
