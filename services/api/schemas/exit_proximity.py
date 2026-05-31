"""services/api/schemas/exit_proximity.py — response models for /api/today/exit-proximity.

PR-B of ``Docs/exit-proximity-design.md``. Mirrors ``signal_proximity``'s
schema: a top-level ``as_of_cycle_ts_utc`` + a list of per-OPEN-POSITION records
with one sub-object per exit trigger (stop / trend_flip / reversal /
decommission).

The internal repo model (``ExitProximityRow``) is flat for SQL efficiency; the
API response is nested for frontend ergonomics — the Today page's "Exits"
section (PR-C) renders a chip per trigger + a "Closest exit" column. This file
owns the mapping.

The categorical Literals are imported from ``services.api.schemas.lean`` (the
canonical wire-shape definitions, which PR-A pinned to the strategy enums) so
there is one source of truth shared by the inbound heartbeat schema, the
``exit_proximity`` CHECK constraints, and this outbound response.

All numeric fields are emitted as Decimal-as-string per A05 / dev-guide §3.8.
None values survive as JSON null (warming-up, no stop on record, or the binary
triggers' absent headroom).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from services.api.repos.exit_proximity import ExitProximityRow
from services.api.schemas.lean import (
    ClosestExitLiteral,
    ExitGateStatusLiteral,
    ExitStateLiteral,
)

DirectionLiteral = Literal["long", "short"]


class ExitTriggerView(BaseModel):
    """Per-trigger response sub-object — state + numeric headroom.

    ``headroom`` is the deterioration pct for ``trend_flip``, the mark-vs-stop
    pct for ``stop``, and always None for the binary ``reversal`` /
    ``decommission`` triggers (and for the warming-up / no-stop-on-record
    cases).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: ExitStateLiteral
    headroom: Decimal | None = Field(
        default=None,
        description=(
            "Numeric headroom. trend_flip: deterioration pct (close vs MA_FAST); "
            "stop: (mark - stop)/mark for a long, mirrored for a short. None for "
            "the binary reversal/decommission triggers, when warming up, or when "
            "no working stop is on record."
        ),
    )


class PositionExitProximityView(BaseModel):
    """One open position's row in the /api/today/exit-proximity response.

    The Today page's "Exits" section renders one row per item; the closest-to-
    closing position sorts to the top (the endpoint returns rows already ordered
    per design §3.3, so the frontend can render in receipt order).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    market: str
    direction: DirectionLiteral
    cycle_ts_utc: datetime = Field(
        ...,
        description=(
            "The LEAN cycle this row was emitted on. The top-level "
            "``as_of_cycle_ts_utc`` is the MAX across all returned rows; per-row "
            "cycle_ts_utc lets the frontend show 'last seen N hours ago'."
        ),
    )
    session_date_et: str = Field(
        ...,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="CME session date (ET wall-clock) — YYYY-MM-DD.",
    )
    held_days: int | None = Field(
        default=None,
        description="Calendar days held (None when opened_at is unknown).",
    )
    held_days_headroom: int | None = Field(
        default=None,
        description=(
            "MIN_HOLDING_DAYS - held_days. Positive = the trend-flip exit is "
            "still blocked by the MIN_HOLDING floor (N days left); <= 0 = the "
            "floor has cleared. None when held_days is unknown."
        ),
    )
    last_close: Decimal | None
    stop_price: Decimal | None = Field(
        default=None,
        description=(
            "The joined working stop_market order's stop price (Q1 = (B)). None "
            "when no working stop is on record — a latent POSITION_UNPROTECTED "
            "signal (the stop chip renders 'no stop on record')."
        ),
    )
    trend_flip: ExitTriggerView
    stop: ExitTriggerView
    reversal: ExitTriggerView
    decommission: ExitTriggerView
    overall_state: ExitStateLiteral
    closest_exit: ClosestExitLiteral
    gate_status: ExitGateStatusLiteral


class ExitProximityResponse(BaseModel):
    """Top-level response for ``GET /api/today/exit-proximity``.

    ``as_of_cycle_ts_utc`` is None when ``positions`` is empty — the
    no-open-positions / pre-first-cycle empty state (design §7).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of_cycle_ts_utc: datetime | None
    positions: list[PositionExitProximityView]


def row_to_view(row: ExitProximityRow) -> PositionExitProximityView:
    """Map a repo row to the response sub-object.

    The repo row is flat (one column per trigger state + one per headroom) so
    SQL stays readable; this function does the small nesting walk so the API
    contract matches the design. The binary reversal/decommission triggers carry
    no headroom column, so their ``ExitTriggerView.headroom`` is always None.
    """
    return PositionExitProximityView(
        market=row.market,
        direction=_direction_literal(row.direction),
        cycle_ts_utc=row.cycle_ts_utc,
        session_date_et=row.session_date_et,
        held_days=row.held_days,
        held_days_headroom=row.held_days_headroom,
        last_close=row.last_close,
        stop_price=row.stop_price,
        trend_flip=ExitTriggerView(
            state=_exit_state_literal(row.trend_flip_state),
            headroom=row.trend_flip_headroom,
        ),
        stop=ExitTriggerView(
            state=_exit_state_literal(row.stop_state),
            headroom=row.stop_headroom_pct,
        ),
        reversal=ExitTriggerView(
            state=_exit_state_literal(row.reversal_state),
            headroom=None,
        ),
        decommission=ExitTriggerView(
            state=_exit_state_literal(row.decommission_state),
            headroom=None,
        ),
        overall_state=_exit_state_literal(row.overall_state),
        closest_exit=_closest_exit_literal(row.closest_exit),
        gate_status=_gate_status_literal(row.gate_status),
    )


def _exit_state_literal(value: str) -> ExitStateLiteral:
    """Narrow a DB string to ExitStateLiteral. The CHECK constraint on
    ``exit_proximity`` already restricts the universe; this is the type-narrow
    for mypy + a defense-in-depth re-validation."""
    if value not in ("holding", "near", "triggered"):
        raise ValueError(f"unexpected exit state in exit_proximity row: {value!r}")
    return value  # type: ignore[return-value]


def _closest_exit_literal(value: str) -> ClosestExitLiteral:
    if value not in ("stop", "reversal", "trend_flip", "decommission"):
        raise ValueError(f"unexpected closest_exit in exit_proximity row: {value!r}")
    return value  # type: ignore[return-value]


def _gate_status_literal(value: str) -> ExitGateStatusLiteral:
    if value not in ("active", "warming_up", "min_holding_blocked", "decommissioned"):
        raise ValueError(f"unexpected gate_status in exit_proximity row: {value!r}")
    return value  # type: ignore[return-value]


def _direction_literal(value: str) -> DirectionLiteral:
    if value not in ("long", "short"):
        raise ValueError(f"unexpected direction in exit_proximity row: {value!r}")
    return value  # type: ignore[return-value]


__all__ = [
    "DirectionLiteral",
    "ExitProximityResponse",
    "ExitTriggerView",
    "PositionExitProximityView",
    "row_to_view",
]
