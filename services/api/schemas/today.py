"""services/api/schemas/today.py — `/api/today/digest` schema.

Backend-spec §4.1.5b ``TodayDigestResponse`` + the embedded ``PnLSummary`` /
``ExposureBreakdown`` blocks. The digest endpoint denormalizes Today-page
landing data into a single payload so the frontend can render its first paint
without N round-trips.

Phase 0 paints with empty data: ``pnl`` = all zeros; ``exposure`` = all-zero
cluster map; ``queued_signals_count = 0``; ``active_alerts_count_by_severity``
all-zero; ``state = NORMAL``; ``agent_status = "disabled"`` (the agent surface
is Phase 2). The ``health_score`` body is the same payload returned by
``GET /api/health-score`` (denormalized to save a round-trip).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from services.api.schemas.health_score import HealthScoreResponse

ExposureCluster = Literal["equity_index", "commodity", "rates_bonds", "crypto", "fx"]


class PnLSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    daily_pnl: Decimal
    weekly_pnl: Decimal
    monthly_pnl: Decimal
    yearly_pnl: Decimal


class ExposureBreakdown(BaseModel):
    model_config = ConfigDict(extra="forbid")

    by_cluster: dict[ExposureCluster, Decimal]
    gross_exposure_pct_nav: Decimal
    net_exposure_pct_nav: Decimal


class TodayDigestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    health_score: HealthScoreResponse
    pnl: PnLSummary
    exposure: ExposureBreakdown
    queued_signals_count: int
    active_alerts_count_by_severity: dict[Literal["P0", "P1", "P2"], int]
    state: Literal["NORMAL", "HALT_NEW", "CONVALESCENT", "VACATION"]
    state_severity: Literal["routine", "defensive_envelope", "incident_review"] | None
    agent_status: Literal["idle", "working", "degraded", "disabled", "errored"]
    environment: Literal["paper", "live-small", "live-scale", "dev"]
    deployed_strategy_version: str


__all__ = [
    "ExposureBreakdown",
    "ExposureCluster",
    "PnLSummary",
    "TodayDigestResponse",
]
