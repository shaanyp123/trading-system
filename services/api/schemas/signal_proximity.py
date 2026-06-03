"""services/api/schemas/signal_proximity.py — response models for /api/signals/proximity.

PR-B of ``Docs/signal-proximity-design.md``. Mirrors the wire shape
documented in §6.4: a top-level ``as_of_cycle_ts_utc`` + a list of
per-market records with one sub-object per gate.

The internal repo model (``SignalProximityRow``) is flat for SQL
efficiency; the API response is nested for frontend ergonomics — the
frontend's "Watching" component renders chips per gate. This file owns
the mapping.

All numeric fields are emitted as Decimal-as-string per A05 / dev-guide
§3.8. None values survive as JSON null.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from services.api.repos.signal_proximity import SignalProximityRow

GateStateLiteral = Literal["pass", "close", "fail"]
GateStatusLiteral = Literal["ok", "warming_up", "decommissioned"]
# ``'hurst'`` RETAINED for historic rows emitted before the 2026-06-02
# Hurst→Efficiency-Ratio gate swap; new rows emit ``'efficiency'``.
ClosestGateLiteral = Literal["donchian", "trend", "hurst", "efficiency", "history"]


class GateView(BaseModel):
    """Per-gate response sub-object — state + numeric headroom."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: GateStateLiteral
    headroom: Decimal | None = Field(
        default=None,
        description=(
            "Numeric headroom. Donchian: pct of last_close needed to reach "
            "the breakout line; Trend: smaller of the two cross gaps; "
            "Efficiency Ratio: value minus threshold (ER units). None when "
            "warming up."
        ),
    )


class MarketProximityView(BaseModel):
    """One market's row in the /api/signals/proximity response.

    Matches design §6.4 sample payload. The frontend's ``WatchingSection``
    component renders one row per item; the closest-to-firing market
    sorts to the top (sort key implemented client-side per design §3.3).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    market: str
    cycle_ts_utc: datetime = Field(
        ...,
        description=(
            "The LEAN cycle this row was emitted on. The top-level "
            "``as_of_cycle_ts_utc`` is the MAX across all returned rows; "
            "per-row cycle_ts_utc lets the frontend show "
            "'last seen N hours ago' for stale markets."
        ),
    )
    session_date_et: str = Field(
        ...,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="CME session date (ET wall-clock) — YYYY-MM-DD.",
    )
    last_close: Decimal | None
    long_donchian: GateView
    short_donchian: GateView
    long_trend: GateView
    short_trend: GateView
    efficiency: GateView
    overall_state: GateStateLiteral
    closest_gate: ClosestGateLiteral
    gate_status: GateStatusLiteral


class SignalProximityResponse(BaseModel):
    """Top-level response for ``GET /api/signals/proximity``.

    ``as_of_cycle_ts_utc`` is None when ``markets`` is empty — the
    pre-first-cycle empty state per design §7.3.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of_cycle_ts_utc: datetime | None
    markets: list[MarketProximityView]


def row_to_view(row: SignalProximityRow) -> MarketProximityView:
    """Map a repo row to the response sub-object.

    The repo row is flat (one column per gate state + one per headroom)
    so SQL stays readable; this function does the small nesting walk so
    the API contract matches design §6.4.
    """
    return MarketProximityView(
        market=row.market,
        cycle_ts_utc=row.cycle_ts_utc,
        session_date_et=row.session_date_et,
        last_close=row.last_close,
        long_donchian=GateView(
            state=_gate_state_literal(row.long_donchian_state),
            headroom=row.long_donchian_headroom_pct,
        ),
        short_donchian=GateView(
            state=_gate_state_literal(row.short_donchian_state),
            headroom=row.short_donchian_headroom_pct,
        ),
        long_trend=GateView(
            state=_gate_state_literal(row.long_trend_state),
            headroom=row.long_trend_gap_pct,
        ),
        short_trend=GateView(
            state=_gate_state_literal(row.short_trend_state),
            headroom=row.short_trend_gap_pct,
        ),
        efficiency=GateView(
            state=_gate_state_literal(row.efficiency_ratio_state),
            headroom=row.efficiency_ratio_headroom,
        ),
        overall_state=_gate_state_literal(row.overall_state),
        closest_gate=_closest_gate_literal(row.closest_gate),
        gate_status=_gate_status_literal(row.gate_status),
    )


def _gate_state_literal(value: str) -> GateStateLiteral:
    """Narrow a DB string to the GateStateLiteral. The CHECK constraint
    on ``signal_proximity`` already restricts the universe; this is the
    type-narrow for mypy + a defense-in-depth re-validation."""
    if value not in ("pass", "close", "fail"):
        raise ValueError(f"unexpected gate state in signal_proximity row: {value!r}")
    return value  # type: ignore[return-value]


def _closest_gate_literal(value: str) -> ClosestGateLiteral:
    # 'hurst' retained for historic rows pre-dating the 2026-06-02 gate swap.
    if value not in ("donchian", "trend", "hurst", "efficiency", "history"):
        raise ValueError(f"unexpected closest_gate in signal_proximity row: {value!r}")
    return value  # type: ignore[return-value]


def _gate_status_literal(value: str) -> GateStatusLiteral:
    if value not in ("ok", "warming_up", "decommissioned"):
        raise ValueError(f"unexpected gate_status in signal_proximity row: {value!r}")
    return value  # type: ignore[return-value]


__all__ = [
    "ClosestGateLiteral",
    "GateStateLiteral",
    "GateStatusLiteral",
    "GateView",
    "MarketProximityView",
    "SignalProximityResponse",
    "row_to_view",
]
