"""services/api/schemas/health_score.py — `/api/health-score` schema.

Backend-spec §4.1.5b ``HealthScoreComponent`` + ``HealthScoreResponse``.

Phase-0 emits ``insufficient_data=True`` with all five components carrying
``score=None`` and ``insufficient_data=True``. The composite math (weighted
sum of components per §4.1.5b table) lands when the source signals exist:
slippage calibration (Week 5 Thu), recon break history, fill-attribution
data, etc. The 5 component names are LOCKED per the spec literal — neither
add nor remove without a spec amendment.

The 5 weights MUST sum to 100. Day 15 picks an even 20-20-20-20-20 split as
the Phase-0 placeholder; Week 5+ tuning replaces this once each component
has a real score.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

HealthScoreComponentName = Literal[
    "live_sharpe_vs_backtest",
    "slippage_drift",
    "hit_rate",
    "capacity_headroom",
    "days_since_recon_break",
]


class HealthScoreComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: HealthScoreComponentName
    weight_pct: int = Field(ge=0, le=100)
    window: str
    score: int | None = Field(default=None, ge=0, le=100)
    insufficient_data: bool


class HealthScoreResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    composite: int = Field(ge=0, le=100)
    traffic_light: Literal["green", "yellow", "red"]
    components: list[HealthScoreComponent]
    insufficient_data: bool
    computed_at: datetime


__all__ = [
    "HealthScoreComponent",
    "HealthScoreComponentName",
    "HealthScoreResponse",
]
