"""services/api/repos/signal_proximity.py — read + write for ``signal_proximity``.

PR-B of ``Docs/signal-proximity-design.md`` (SIGNED OFF 2026-05-28). Two
surfaces:

  * ``insert_rows`` — append one row per market on every LEAN cycle
    heartbeat. Called from
    ``services/api/routes/internal/lean.py::post_lean_signal`` in a
    best-effort try/except (per design §6.3): a DB hiccup logs and the
    handler returns 202 rather than 5xx-ing the liveness signal.

  * ``fetch_latest_per_market`` — return the latest row per market for
    the ``GET /api/signals/proximity`` endpoint. Uses ``DISTINCT ON``
    (Postgres) ordered by ``cycle_ts_utc DESC`` to walk the
    ``idx_signal_proximity_market_cycle`` index in one pass.

The table itself has no FK to ``accounts`` — proximity is universe-scoped
(what LEAN evaluated this cycle), not account-scoped. Multi-account
expansion can add the FK without invalidating existing rows.

Per Q2 of the design (re-confirmed by the operator 2026-05-28), these
rows do NOT require an audit_log entry before the write — they ARE the
audit trail for the daily evaluation. The audit-first ordering rule
(``feedback_audit_first_ordering`` memory) is for state-change events;
this carve-out is explicit and locked.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from services.api.schemas.lean import MarketEvaluationItem

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class SignalProximityRow:
    """One row from ``signal_proximity`` projected for the response model.

    Numeric columns surface as Decimal (or None when warming up). The
    response model (``services.api.schemas.signal_proximity``) renders
    them as strings per A05.
    """

    cycle_ts_utc: datetime
    session_date_et: str  # ISO date — Postgres DATE round-trips as datetime.date
    market: str
    last_close: Decimal | None
    long_donchian_state: str
    long_donchian_headroom_pct: Decimal | None
    short_donchian_state: str
    short_donchian_headroom_pct: Decimal | None
    long_trend_state: str
    long_trend_gap_pct: Decimal | None
    short_trend_state: str
    short_trend_gap_pct: Decimal | None
    hurst_state: str
    hurst_headroom: Decimal | None
    overall_state: str
    closest_gate: str
    gate_status: str


async def insert_rows(
    session: AsyncSession,
    *,
    cycle_ts_utc: datetime,
    session_date_et: str,
    evaluations: list[MarketEvaluationItem],
) -> int:
    """Append one row per market for this cycle. Returns the row count inserted.

    The caller is responsible for committing the session (or letting the
    request-scoped session do so on success). Failures bubble up — the
    route handler wraps the call in try/except per design §6.3.

    Uses ``executemany`` semantics via SQLAlchemy's parameterized INSERT
    batch — one round-trip per cycle, not one per market.
    """
    if not evaluations:
        return 0

    # asyncpg's DATE adapter wants a ``date`` object, not an ISO string —
    # the schema layer carries session_date_et as ``str`` (it's a YYYY-MM-DD
    # pattern, not a calendar type, on the wire) so we parse at the
    # boundary. Invalid strings surface as ValueError here, which the
    # caller's best-effort try/except wraps into a log + 202.
    session_date_obj = date.fromisoformat(session_date_et)

    params = [
        {
            "cycle_ts_utc": cycle_ts_utc,
            "session_date_et": session_date_obj,
            "market": ev.market,
            "last_close": ev.last_close,
            "long_donchian_state": ev.long_donchian.state,
            "long_donchian_headroom_pct": ev.long_donchian.headroom,
            "short_donchian_state": ev.short_donchian.state,
            "short_donchian_headroom_pct": ev.short_donchian.headroom,
            "long_trend_state": ev.long_trend.state,
            "long_trend_gap_pct": ev.long_trend.headroom,
            "short_trend_state": ev.short_trend.state,
            "short_trend_gap_pct": ev.short_trend.headroom,
            "hurst_state": ev.hurst.state,
            "hurst_headroom": ev.hurst.headroom,
            "overall_state": ev.overall_state,
            "closest_gate": ev.closest_gate,
            "gate_status": ev.gate_status,
        }
        for ev in evaluations
    ]
    await session.execute(
        text(
            "INSERT INTO signal_proximity ("
            "    cycle_ts_utc, session_date_et, market, last_close,"
            "    long_donchian_state, long_donchian_headroom_pct,"
            "    short_donchian_state, short_donchian_headroom_pct,"
            "    long_trend_state, long_trend_gap_pct,"
            "    short_trend_state, short_trend_gap_pct,"
            "    hurst_state, hurst_headroom,"
            "    overall_state, closest_gate, gate_status"
            ") VALUES ("
            "    :cycle_ts_utc, :session_date_et, :market, :last_close,"
            "    :long_donchian_state, :long_donchian_headroom_pct,"
            "    :short_donchian_state, :short_donchian_headroom_pct,"
            "    :long_trend_state, :long_trend_gap_pct,"
            "    :short_trend_state, :short_trend_gap_pct,"
            "    :hurst_state, :hurst_headroom,"
            "    :overall_state, :closest_gate, :gate_status"
            ")"
        ),
        params,
    )
    return len(params)


async def fetch_latest_per_market(
    session: AsyncSession,
) -> tuple[datetime | None, list[SignalProximityRow]]:
    """Return (as_of_cycle_ts_utc, latest row per market).

    Uses ``DISTINCT ON (market) ... ORDER BY market, cycle_ts_utc DESC`` —
    the ``idx_signal_proximity_market_cycle`` index is the natural fit.
    ``as_of_cycle_ts_utc`` is the MAX cycle_ts_utc across the returned set
    (the most recent cycle that contributed any row); when the table is
    empty, returns ``(None, [])`` so the endpoint can render the
    "no proximity data yet today" empty state cleanly.
    """
    rows = (
        await session.execute(
            text(
                "SELECT DISTINCT ON (market) "
                "  cycle_ts_utc, session_date_et, market, last_close, "
                "  long_donchian_state, long_donchian_headroom_pct, "
                "  short_donchian_state, short_donchian_headroom_pct, "
                "  long_trend_state, long_trend_gap_pct, "
                "  short_trend_state, short_trend_gap_pct, "
                "  hurst_state, hurst_headroom, "
                "  overall_state, closest_gate, gate_status "
                "FROM signal_proximity "
                "ORDER BY market, cycle_ts_utc DESC"
            )
        )
    ).fetchall()
    if not rows:
        return None, []

    as_of = max(row.cycle_ts_utc for row in rows)
    items = [
        SignalProximityRow(
            cycle_ts_utc=row.cycle_ts_utc,
            session_date_et=row.session_date_et.isoformat(),
            market=row.market,
            last_close=row.last_close,
            long_donchian_state=row.long_donchian_state,
            long_donchian_headroom_pct=row.long_donchian_headroom_pct,
            short_donchian_state=row.short_donchian_state,
            short_donchian_headroom_pct=row.short_donchian_headroom_pct,
            long_trend_state=row.long_trend_state,
            long_trend_gap_pct=row.long_trend_gap_pct,
            short_trend_state=row.short_trend_state,
            short_trend_gap_pct=row.short_trend_gap_pct,
            hurst_state=row.hurst_state,
            hurst_headroom=row.hurst_headroom,
            overall_state=row.overall_state,
            closest_gate=row.closest_gate,
            gate_status=row.gate_status,
        )
        for row in rows
    ]
    return as_of, items


__all__ = [
    "SignalProximityRow",
    "fetch_latest_per_market",
    "insert_rows",
]
