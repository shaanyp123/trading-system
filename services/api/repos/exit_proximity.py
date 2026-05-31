"""services/api/repos/exit_proximity.py — read + write for ``exit_proximity``.

PR-B of ``Docs/exit-proximity-design.md`` (SIGNED OFF 2026-05-30). The exit-side
mirror of ``signal_proximity`` — three surfaces:

  * ``enrich_exit_evaluation`` — the pure (no-DB) Q1 = (B) transform: take one
    LEAN-emitted ``PositionExitEvaluationItem`` (whose ``stop`` dimension is a
    NULL-state placeholder, see below) plus the joined working stop price, and
    produce the fully-enriched, ready-to-persist row.

  * ``insert_rows`` — append one row per open position on every LEAN cycle
    heartbeat. For each position it joins the latest WORKING ``stop_market``
    order (``_fetch_latest_working_stop_price``), enriches, then batch-INSERTs.
    Called from ``services/api/routes/internal/lean.py::post_lean_signal`` in a
    best-effort try/except (design §6.3): a DB hiccup logs + the handler returns
    202 rather than 5xx-ing the liveness signal.

  * ``fetch_latest_per_market`` — the latest row per open market for the
    ``GET /api/today/exit-proximity`` endpoint, ALREADY ordered closest-to-
    closing (TRIGGERED first, then NEAR ascending by headroom, then HOLDING —
    design §3.3). Uses ``DISTINCT ON`` (Postgres) for the latest-per-market
    pass, then sorts the small result set in Python (the closest-exit headroom
    depends on which trigger is limiting, which is awkward to express in SQL).

**Q1 = (B) — why the api re-derives the stop dimension.** LEAN does not know the
working protective stop (it lives in the api's ``orders`` table, managed by the
exit pipeline's ``replace_protective_stop``). So LEAN emits ``stop_price=None``
and a NULL-state ``stop`` trigger, and computes its ``overall_state`` /
``closest_exit`` WITHOUT the stop dimension. At persist time we join the real
stop, re-classify the stop dimension via ``classify_stop``, and then RE-DERIVE
``overall_state`` / ``closest_exit`` via ``combine_overall_exit`` — because a
NEAR/TRIGGERED stop can flip both away from the stop-blind values LEAN sent. We
never persist LEAN's stop-blind ``overall_state``/``closest_exit``.

**No audit_log row gated before write** — Q6 of the design (mirrors
``signal_proximity`` Q2): these rows are observational, NOT a state change. The
table IS the trail. The audit-first ordering rule
(``feedback_audit_first_ordering`` memory) is for state-change events; this
carve-out is explicit and locked.

The table has no FK to ``accounts`` — proximity is universe-scoped (which open
positions LEAN evaluated this cycle), not account-scoped, identical to
``signal_proximity``. The stop join is likewise market-only (single-account
paper); a multi-account expansion would add account scoping alongside the FK.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from services.api.schemas.lean import ExitTriggerItem, PositionExitEvaluationItem
from strategies.v1_trend_following.exit_proximity import (
    Direction,
    ExitState,
    ExitTriggerProximity,
    classify_stop,
    combine_overall_exit,
)
from strategies.v1_trend_following.parameters import V1_DEFAULTS

log = structlog.get_logger()

#: MIN_HOLDING_DAYS is LOCKED (PR-only to change — see ``parameters.py``), so the
#: ``V1_DEFAULTS`` value equals the active value in every parameter set. The wire
#: payload carries ``held_days`` but not ``held_days_headroom`` (LEAN computes
#: the latter internally only for its detail string), so the api re-derives it
#: here from the single locked source of truth.
_MIN_HOLDING_DAYS: int = int(V1_DEFAULTS["MIN_HOLDING_DAYS"])

#: Closest-to-closing sort: TRIGGERED group first, then NEAR, then HOLDING
#: (design §3.3). Within a group we sort ascending by the limiting trigger's
#: numeric headroom (smallest = closest to its line).
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


@dataclass(frozen=True, slots=True)
class EnrichedExitProximity:
    """The fully-enriched per-position payload, ready to persist.

    Output of :func:`enrich_exit_evaluation`. Holds every ``exit_proximity``
    column except the cycle-level ``cycle_ts_utc`` / ``session_date_et`` (those
    are the same for all positions on a heartbeat and are supplied by
    :func:`insert_rows`).
    """

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


def _to_trigger(item: ExitTriggerItem) -> ExitTriggerProximity:
    """Convert a wire ``ExitTriggerItem`` to the strategy dataclass.

    ``ExitState`` is a ``StrEnum`` whose values equal the wire ``state``
    literals, so ``ExitState(item.state)`` round-trips.
    """
    return ExitTriggerProximity(
        state=ExitState(item.state),
        headroom=item.headroom,
        detail=item.detail,
    )


def enrich_exit_evaluation(
    ev: PositionExitEvaluationItem,
    *,
    stop_price: Decimal | None,
) -> EnrichedExitProximity:
    """Apply the Q1 = (B) stop enrichment to one LEAN-emitted evaluation.

    Pure (no DB) so the flip is unit-testable. Steps:

    1. Re-classify the stop dimension from the joined ``stop_price`` + the
       emitted mark (``ev.last_close``) via :func:`classify_stop` — one source
       of truth for the band math. ``None`` ``stop_price`` yields the NULL-state
       HOLDING ("no stop on record", a latent POSITION_UNPROTECTED signal).
    2. RE-DERIVE ``overall_state`` / ``closest_exit`` via
       :func:`combine_overall_exit` on the *enriched* stop — never trust LEAN's
       stop-blind summary, which can disagree once the real stop is NEAR/
       TRIGGERED.
    3. Derive ``held_days_headroom`` from the locked ``MIN_HOLDING_DAYS``.

    The trend_flip / reversal / decommission dimensions + ``gate_status`` pass
    through unchanged (LEAN owns those indicator dimensions, ED1).
    """
    direction: Direction = ev.direction
    stop = classify_stop(last_close=ev.last_close, stop_price=stop_price, direction=direction)
    trend_flip = _to_trigger(ev.trend_flip)
    reversal = _to_trigger(ev.reversal)
    decommission = _to_trigger(ev.decommission)

    overall_state, closest_exit = combine_overall_exit(
        trend_flip=trend_flip, stop=stop, reversal=reversal, decommission=decommission
    )

    held_days_headroom = _MIN_HOLDING_DAYS - ev.held_days if ev.held_days is not None else None

    return EnrichedExitProximity(
        market=ev.market,
        direction=direction,
        held_days=ev.held_days,
        last_close=ev.last_close,
        stop_price=stop_price,
        trend_flip_state=ev.trend_flip.state,
        trend_flip_headroom=ev.trend_flip.headroom,
        held_days_headroom=held_days_headroom,
        stop_state=stop.state.value,
        stop_headroom_pct=stop.headroom,
        reversal_state=ev.reversal.state,
        decommission_state=ev.decommission.state,
        overall_state=overall_state.value,
        closest_exit=closest_exit,
        gate_status=ev.gate_status,
    )


async def _fetch_latest_working_stop_price(
    session: AsyncSession,
    *,
    market: str,
) -> Decimal | None:
    """Join the latest WORKING ``stop_market`` order's stop price for a market.

    Q1 = (B): the protective stop lives in ``orders`` (managed by the exit
    pipeline's ``replace_protective_stop``). V1 holds at most one position per
    market (no pyramiding — ``feedback_no_accidental_pyramiding``), so at steady
    state there is one working ``stop_market`` per held market; ``ORDER BY
    created_at DESC LIMIT 1`` takes the freshest. Market-only (no account
    filter) matches the universe-scoped table + single-account paper reality,
    and deliberately avoids the account mis-keying class of bug
    (``project_option_c_recon_fix``). Returns ``None`` when no working stop is on
    record — the caller leaves the stop dimension NULL (a latent
    POSITION_UNPROTECTED signal, design §3.4).
    """
    row = (
        await session.execute(
            text(
                "SELECT stop_price FROM orders "
                "WHERE market = :market "
                "  AND order_type = 'stop_market' "
                "  AND status = 'working' "
                "ORDER BY created_at DESC "
                "LIMIT 1"
            ),
            {"market": market},
        )
    ).fetchone()
    if row is None:
        return None
    # asyncpg renders NUMERIC as Decimal; the Row attribute is otherwise Any.
    stop_price: Decimal | None = row.stop_price
    return stop_price


async def insert_rows(
    session: AsyncSession,
    *,
    cycle_ts_utc: datetime,
    session_date_et: str,
    evaluations: list[PositionExitEvaluationItem],
) -> int:
    """Append one row per open position for this cycle. Returns rows inserted.

    For each position: join the latest working stop (one SELECT), enrich
    (re-derive the stop dimension + overall/closest + held_days_headroom), then
    batch-INSERT all positions in one round-trip. The caller commits (or lets
    the request-scoped session commit). Failures bubble up — the route handler
    wraps the call in try/except per design §6.3.

    Mirrors ``signal_proximity.insert_rows``; the only structural addition is
    the per-position stop join, which is the Q1 = (B) crux.
    """
    if not evaluations:
        return 0

    # asyncpg's DATE adapter wants a ``date`` object, not the ISO string the
    # schema layer carries on the wire — parse at the boundary (an invalid
    # string surfaces as ValueError, wrapped by the caller's best-effort
    # try/except into a log + 202).
    session_date_obj = date.fromisoformat(session_date_et)

    params: list[dict[str, object]] = []
    for ev in evaluations:
        stop_price = await _fetch_latest_working_stop_price(session, market=ev.market)
        enriched = enrich_exit_evaluation(ev, stop_price=stop_price)
        params.append(
            {
                "cycle_ts_utc": cycle_ts_utc,
                "session_date_et": session_date_obj,
                "market": enriched.market,
                "direction": enriched.direction,
                "held_days": enriched.held_days,
                "last_close": enriched.last_close,
                "stop_price": enriched.stop_price,
                "trend_flip_state": enriched.trend_flip_state,
                "trend_flip_headroom": enriched.trend_flip_headroom,
                "held_days_headroom": enriched.held_days_headroom,
                "stop_state": enriched.stop_state,
                "stop_headroom_pct": enriched.stop_headroom_pct,
                "reversal_state": enriched.reversal_state,
                "decommission_state": enriched.decommission_state,
                "overall_state": enriched.overall_state,
                "closest_exit": enriched.closest_exit,
                "gate_status": enriched.gate_status,
            }
        )

    await session.execute(
        text(
            "INSERT INTO exit_proximity ("
            "    cycle_ts_utc, session_date_et, market, direction,"
            "    held_days, last_close, stop_price,"
            "    trend_flip_state, trend_flip_headroom, held_days_headroom,"
            "    stop_state, stop_headroom_pct,"
            "    reversal_state, decommission_state,"
            "    overall_state, closest_exit, gate_status"
            ") VALUES ("
            "    :cycle_ts_utc, :session_date_et, :market, :direction,"
            "    :held_days, :last_close, :stop_price,"
            "    :trend_flip_state, :trend_flip_headroom, :held_days_headroom,"
            "    :stop_state, :stop_headroom_pct,"
            "    :reversal_state, :decommission_state,"
            "    :overall_state, :closest_exit, :gate_status"
            ")"
        ),
        params,
    )
    return len(params)


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
    "EnrichedExitProximity",
    "ExitProximityRow",
    "enrich_exit_evaluation",
    "fetch_latest_per_market",
    "insert_rows",
]
