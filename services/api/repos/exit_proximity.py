"""services/api/repos/exit_proximity.py — read surface for ``exit_proximity``.

Crypto-pivot C0-B4 (delta spec §3.4/§3.9): the V1-era WRITE path
(``enrich_exit_evaluation`` / ``insert_rows`` — LEAN-cycle enrichment
re-deriving the stop dimension via ``strategies/v1_trend_following``)
died with the LEAN signals ingress + the v1 package. What remains is
the READ surface for ``GET /api/today/exit-proximity``: latest row per
market, sorted closest-to-closing — which now serves only historical
CME rows until the §3.9 crypto proximity inputs (distance to hysteresis
flip / stop levels, computed by the strategy worker) land with their
own write path. The endpoint's empty-state contract is unchanged.

**No audit_log row gated before write** (design Q6, unchanged): these
rows are observational; the table IS the trail.

The table has no FK to ``accounts`` — proximity is universe-scoped.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger()


_STATE_SORT_RANK: dict[str, int] = {"triggered": 0, "near": 1, "holding": 2}


@dataclass(frozen=True, slots=True)
class ExitProximityRow:
    """One row from ``exit_proximity`` projected for the response model.

    Numeric columns surface as Decimal (or None when warming up / no stop on
    record). The response model (``services.api.schemas.exit_proximity``)
    renders them as strings per A05.
    """

    cycle_ts_utc: datetime
    session_date_et: str  # ISO date — Postgres DATE round-trips as datetime.date
    market: str
    direction: str
    held_days: int | None
    last_close: Decimal | None
    stop_price: Decimal | None
    trend_flip_state: str
    trend_flip_headroom: Decimal | None
    held_days_headroom: int | None
    stop_state: str
    stop_headroom_pct: Decimal | None
    reversal_state: str
    decommission_state: str
    overall_state: str
    closest_exit: str
    gate_status: str


def _closest_to_closing_key(row: ExitProximityRow) -> tuple[int, Decimal, str]:
    """Sort key: most-advanced-toward-closing first (design §3.3).

    Primary: overall state group (TRIGGERED < NEAR < HOLDING). Secondary: the
    LIMITING trigger's numeric headroom ascending (smallest = closest to its
    line); binary triggers (reversal / decommission) carry no headroom and sort
    last within their group via +Infinity. Tertiary: market, for a stable,
    deterministic order.
    """
    if row.closest_exit == "stop" and row.stop_headroom_pct is not None:
        headroom = row.stop_headroom_pct
    elif row.closest_exit == "trend_flip" and row.trend_flip_headroom is not None:
        headroom = row.trend_flip_headroom
    else:
        headroom = Decimal("Infinity")
    return (_STATE_SORT_RANK.get(row.overall_state, 99), headroom, row.market)


async def fetch_latest_per_market(
    session: AsyncSession,
) -> tuple[datetime | None, list[ExitProximityRow]]:
    """Return ``(as_of_cycle_ts_utc, latest row per open market)``.

    ``DISTINCT ON (market) ... ORDER BY market, cycle_ts_utc DESC`` walks the
    ``idx_exit_proximity_market_cycle`` index for the latest row per market; the
    result is then re-sorted closest-to-closing (§3.3) in Python.
    ``as_of_cycle_ts_utc`` is the MAX cycle_ts_utc across the returned set; when
    the table is empty, returns ``(None, [])`` so the endpoint renders the
    "no open positions / no data yet" empty state cleanly.
    """
    rows = (
        await session.execute(
            text(
                "SELECT DISTINCT ON (market) "
                "  cycle_ts_utc, session_date_et, market, direction, "
                "  held_days, last_close, stop_price, "
                "  trend_flip_state, trend_flip_headroom, held_days_headroom, "
                "  stop_state, stop_headroom_pct, "
                "  reversal_state, decommission_state, "
                "  overall_state, closest_exit, gate_status "
                "FROM exit_proximity "
                "ORDER BY market, cycle_ts_utc DESC"
            )
        )
    ).fetchall()
    if not rows:
        return None, []

    as_of = max(row.cycle_ts_utc for row in rows)
    items = [
        ExitProximityRow(
            cycle_ts_utc=row.cycle_ts_utc,
            session_date_et=row.session_date_et.isoformat(),
            market=row.market,
            direction=row.direction,
            held_days=row.held_days,
            last_close=row.last_close,
            stop_price=row.stop_price,
            trend_flip_state=row.trend_flip_state,
            trend_flip_headroom=row.trend_flip_headroom,
            held_days_headroom=row.held_days_headroom,
            stop_state=row.stop_state,
            stop_headroom_pct=row.stop_headroom_pct,
            reversal_state=row.reversal_state,
            decommission_state=row.decommission_state,
            overall_state=row.overall_state,
            closest_exit=row.closest_exit,
            gate_status=row.gate_status,
        )
        for row in rows
    ]
    items.sort(key=_closest_to_closing_key)
    return as_of, items


__all__ = [
    "ExitProximityRow",
    "fetch_latest_per_market",
]
