"""services/risk/attribution.py — daily P&L attribution roll-up planner.

PR-K (post-pivot 2026-05-13). Per backend-spec §3.7, the
``attribution`` table stores expected-vs-realized comparison for each
closed trade. The Today page's D/W/M/Y P&L tiles read from this table;
without an emitter, the tiles paint zeros forever.

This module ships the pure-policy planner +
:func:`apply_attribution_plan` orchestrator. The end-to-end wiring
into ``services.reconciliation.eod_cycle.run_eod_cycle`` is deferred
to a Phase 1+ follow-up because:

  * Phase 1 signals are all entries; trades don't close until exit
    fills land (which is itself Phase 1+ scope per backend-spec §6.2).
  * The expected_* fields require a `signal-emit-time` attribution
    payload that LEAN doesn't currently include in the
    `signal_emitted` event. Adding that payload field needs a spec
    update (services/api/schemas/lean.py — §4.4 contract) which is
    outside PR-K's blast radius.

What PR-K delivers TODAY:

  * Pure-policy :func:`plan_daily_attribution` — given a list of
    closed trades for a session_date + per-trade expected payloads
    (sourced however the operator wires Phase 1+), returns an
    :class:`AttributionPlan` with one :class:`AttributionRow` per
    trade + a single ``ATTRIBUTION_ROLLUP_RECORDED`` audit event
    summarizing the day's aggregate.
  * I/O orchestrator :func:`apply_attribution_plan` that writes the
    audit row first (per §2.10.1) + INSERTs the attribution rows.
  * Tests covering the empty-day no-op, single-trade roll-up,
    multi-trade aggregation, Decimal precision, A06 enforcement.

A02 BINDS — ``services/risk/**`` is on the forbidden-modification
whitelist per dev-guide §11; PR carries the ``risk-review-approved``
label.
A04 enforced — ATTRIBUTION_ROLLUP_RECORDED is in the locked taxonomy
enum (services/audit/event_types.py).
A05 enforced — Decimals throughout.
A06 enforced — tz-aware UTC.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Final, Literal
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from services.audit.event_types import AuditEventType
from services.audit.writer import Environment, PhaseAtEmit, append_audit_event

log = structlog.get_logger()


#: Quantization for the ``attribution`` table's NUMERIC(20,4) P&L columns.
PNL_QUANTIZER: Final[Decimal] = Decimal("0.0001")

#: Quantization for the slippage_bps NUMERIC(10,4) column.
BPS_QUANTIZER: Final[Decimal] = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class ClosedTradeForAttribution:
    """Subset of the ``trades`` row + linked signal needed to compute
    attribution. The caller builds this dataclass from the joined
    trades + signals SELECT before invoking the planner.
    """

    trade_id: UUID
    entry_signal_id: UUID
    direction: Literal["long", "short"]
    total_quantity: int
    avg_entry_price: Decimal
    avg_exit_price: Decimal
    realized_pnl_usd: Decimal
    """Already-computed in trades.realized_pnl_usd by the exit-fill
    handler (Phase 1+). Carried here unchanged."""

    realized_commission_usd: Decimal
    opened_at_utc: datetime
    closed_at_utc: datetime
    expected_entry_price: Decimal
    """Sourced from the signal's ``expected_fill_price`` column."""

    expected_exit_price: Decimal | None
    """Phase 1+ — when the exit signal carries an expected_fill_price;
    None for trades closed by stop / manual / capacity_constrained."""

    expected_slippage_bps: Decimal
    """From signals.expected_slippage_bps."""

    expected_holding_days: int
    """Phase 1+ — derived from strategy parameters at signal-emit time;
    when unavailable, callers pass 0 (the planner accepts but emits a
    warning)."""

    expected_at_utc: datetime
    """When the signal was emitted (signals.emitted_at_utc)."""


@dataclass(frozen=True, slots=True)
class AttributionRow:
    """One row to INSERT into the ``attribution`` table.

    Mirrors the alembic 0002 column layout 1:1 except for ``id``
    (DB-generated) + ``created_at`` (DB default ``now()``).
    """

    trade_id: UUID
    account_id: UUID
    env: str
    expected_entry_price: Decimal
    expected_exit_price: Decimal | None
    expected_pnl_usd: Decimal
    expected_slippage_bps: Decimal
    expected_holding_days: int
    expected_at_utc: datetime
    realized_entry_price: Decimal
    realized_exit_price: Decimal
    realized_pnl_usd: Decimal
    realized_slippage_bps: Decimal
    realized_holding_days: int
    realized_at_utc: datetime


@dataclass(frozen=True, slots=True)
class AttributionPlan:
    """Pure-policy plan emitted by :func:`plan_daily_attribution`.

    Audit-first per spec §2.10.1: the apply step writes the audit
    event BEFORE the per-trade INSERTs. If audit fails, INSERTs are
    skipped + the operator sees the partial state on next cycle.
    """

    session_date_et: date
    account_id: UUID
    env: str
    audit_event_payload: dict[str, Any]
    """The ``ATTRIBUTION_ROLLUP_RECORDED`` event_type payload — summary
    of the day's aggregate (gross P&L + per-direction count + avg
    slippage)."""

    rows: tuple[AttributionRow, ...] = field(default_factory=tuple)
    """Empty tuple = no closed trades for this session_date. The apply
    step still writes the audit event (with zeros) so the Today page
    has a daily-roll-up row to point at."""


@dataclass(frozen=True, slots=True)
class AttributionApplyResult:
    """Outcome of :func:`apply_attribution_plan`."""

    audit_event_uuid: UUID
    inserted_row_count: int


class AttributionError(Exception):
    """Raised for terminal-failure attribution scenarios.

    Reasons:
      * Naive datetime in trade row (A06)
      * Negative quantity / nonsense Decimals (A05)
    """


# ---------------------------------------------------------------------------
# Pure-policy helpers
# ---------------------------------------------------------------------------


def _compute_realized_slippage_bps(
    *,
    direction: Literal["long", "short"],
    expected_price: Decimal,
    realized_price: Decimal,
) -> Decimal:
    """Compute slippage in basis points, signed so that positive means
    the trader paid worse than expected mid.

    Sign convention (mirrors ``services.calibration.calibration``):
      * Long entry / short exit (buy): realized > expected → trader
        paid more → positive bps.
      * Short entry / long exit (sell): realized < expected → trader
        received less → positive bps.

    For trades the relevant edge is on BOTH the entry and the exit.
    PR-K computes the avg of (entry slip + exit slip); when
    expected_exit is None (stop / manual), we use only entry slip.
    """
    if expected_price <= 0:
        raise AttributionError(f"expected_price must be > 0; got {expected_price}")
    raw_bps = ((realized_price - expected_price) / expected_price) * Decimal(10000)
    if direction == "short":
        raw_bps = -raw_bps
    return raw_bps.quantize(BPS_QUANTIZER, rounding=ROUND_HALF_EVEN)


def _holding_days(opened_at_utc: datetime, closed_at_utc: datetime) -> int:
    """Calendar-day delta between open + close, both tz-aware UTC.

    Returns 0 for same-day round-trips. Phase 1+ may switch to
    business-days math; the simple calendar diff is good enough for
    Today page tiles + the operator can refine later.
    """
    if opened_at_utc.tzinfo is None or closed_at_utc.tzinfo is None:
        raise AttributionError("holding_days requires tz-aware UTC timestamps per [A06]")
    return (closed_at_utc.date() - opened_at_utc.date()).days


def plan_daily_attribution(
    *,
    account_id: UUID,
    env: str,
    session_date_et: date,
    closed_trades_today: Sequence[ClosedTradeForAttribution],
    rollup_at_utc: datetime | None = None,
) -> AttributionPlan:
    """Build the attribution plan for one calendar day.

    Empty input → empty rows tuple + a zero-aggregate audit event
    (the Today page reads the audit event for the daily breadcrumb
    even on no-trade days).

    Pure policy — no I/O, no clock. ``rollup_at_utc`` defaults to
    ``datetime.now(tz=UTC)``; pass an explicit timestamp for tests +
    backfill scenarios.
    """
    if rollup_at_utc is None:
        rollup_at_utc = datetime.now(tz=UTC)
    if rollup_at_utc.tzinfo is None:
        raise AttributionError("rollup_at_utc must be tz-aware UTC per [A06]")

    rows: list[AttributionRow] = []
    gross_realized_pnl = Decimal("0")
    gross_expected_pnl = Decimal("0")
    long_count = 0
    short_count = 0

    for t in closed_trades_today:
        if t.opened_at_utc.tzinfo is None or t.closed_at_utc.tzinfo is None:
            raise AttributionError(
                f"trade_id={t.trade_id} datetimes must be tz-aware UTC per [A06]"
            )
        if t.total_quantity <= 0:
            raise AttributionError(
                f"trade_id={t.trade_id} total_quantity must be > 0; got {t.total_quantity}"
            )

        # Expected P&L: based on expected entry + (expected exit OR
        # realized exit when expected_exit is None — Phase 1 conservative
        # default carries the same exit price both sides so the slippage
        # only reflects entry edge in that case).
        effective_expected_exit = t.expected_exit_price or t.avg_exit_price
        if t.direction == "long":
            expected_pnl_per_unit = effective_expected_exit - t.expected_entry_price
        else:
            expected_pnl_per_unit = t.expected_entry_price - effective_expected_exit
        # Subtract expected commission proxy: use the realized as a
        # 1:1 stand-in (Phase 1 doesn't have an expected commission
        # estimator yet).
        expected_pnl = (
            expected_pnl_per_unit * Decimal(t.total_quantity)
        ) - t.realized_commission_usd
        expected_pnl_q = expected_pnl.quantize(PNL_QUANTIZER, rounding=ROUND_HALF_EVEN)

        # Realized slippage bps — average of entry + exit slippage.
        entry_slip_bps = _compute_realized_slippage_bps(
            direction=t.direction,
            expected_price=t.expected_entry_price,
            realized_price=t.avg_entry_price,
        )
        if t.expected_exit_price is not None:
            # For the exit leg, the direction flips: long entries close
            # via sell, short entries close via buy.
            exit_direction: Literal["long", "short"] = "short" if t.direction == "long" else "long"
            exit_slip_bps = _compute_realized_slippage_bps(
                direction=exit_direction,
                expected_price=t.expected_exit_price,
                realized_price=t.avg_exit_price,
            )
            realized_slip = ((entry_slip_bps + exit_slip_bps) / Decimal(2)).quantize(
                BPS_QUANTIZER, rounding=ROUND_HALF_EVEN
            )
        else:
            # Stop / manual exit — only entry slip is meaningful.
            realized_slip = entry_slip_bps

        realized_holding = _holding_days(t.opened_at_utc, t.closed_at_utc)

        rows.append(
            AttributionRow(
                trade_id=t.trade_id,
                account_id=account_id,
                env=env,
                expected_entry_price=t.expected_entry_price,
                expected_exit_price=t.expected_exit_price,
                expected_pnl_usd=expected_pnl_q,
                expected_slippage_bps=t.expected_slippage_bps,
                expected_holding_days=t.expected_holding_days,
                expected_at_utc=t.expected_at_utc,
                realized_entry_price=t.avg_entry_price,
                realized_exit_price=t.avg_exit_price,
                realized_pnl_usd=t.realized_pnl_usd.quantize(
                    PNL_QUANTIZER, rounding=ROUND_HALF_EVEN
                ),
                realized_slippage_bps=realized_slip,
                realized_holding_days=realized_holding,
                realized_at_utc=t.closed_at_utc,
            )
        )
        gross_realized_pnl += t.realized_pnl_usd
        gross_expected_pnl += expected_pnl_q
        if t.direction == "long":
            long_count += 1
        else:
            short_count += 1

    audit_payload: dict[str, Any] = {
        "session_date_et": session_date_et.isoformat(),
        "account_id": str(account_id),
        "env": env,
        "trade_count": len(rows),
        "long_count": long_count,
        "short_count": short_count,
        "gross_realized_pnl_usd": str(
            gross_realized_pnl.quantize(PNL_QUANTIZER, rounding=ROUND_HALF_EVEN)
        ),
        "gross_expected_pnl_usd": str(
            gross_expected_pnl.quantize(PNL_QUANTIZER, rounding=ROUND_HALF_EVEN)
        ),
        "rollup_at_utc": rollup_at_utc.isoformat(),
    }

    return AttributionPlan(
        session_date_et=session_date_et,
        account_id=account_id,
        env=env,
        audit_event_payload=audit_payload,
        rows=tuple(rows),
    )


# ---------------------------------------------------------------------------
# I/O orchestrator
# ---------------------------------------------------------------------------


async def apply_attribution_plan(
    plan: AttributionPlan,
    *,
    session_factory: async_sessionmaker[Any],
    env: Environment,
    phase_at_emit: PhaseAtEmit = 1,
    rollup_at_utc: datetime | None = None,
) -> AttributionApplyResult:
    """Flush an :class:`AttributionPlan` to the database.

    Audit-first per spec §2.10.1:

      1. Append ATTRIBUTION_ROLLUP_RECORDED audit row in its own
         SERIALIZABLE transaction.
      2. INSERT one ``attribution`` row per :class:`AttributionRow`
         in plan.rows. Each row carries the audit event's UUID in a
         future migration (the current schema doesn't reserve a column
         for this; backend-spec §3.7 doesn't link the row to its
         emit-audit row yet — Phase 1+ migration adds it).

    The rollup audit fires unconditionally — even when plan.rows is
    empty — so the Today page has a daily breadcrumb on no-trade
    days (renders as "P&L: $0" rather than "unknown").
    """
    if rollup_at_utc is None:
        rollup_at_utc = datetime.now(tz=UTC)

    async with session_factory() as audit_session:
        record = await append_audit_event(
            audit_session,
            AuditEventType.ATTRIBUTION_ROLLUP_RECORDED,
            plan.audit_event_payload,
            account_id=plan.account_id,
            env=env,
            phase_at_emit=phase_at_emit,
            source_clock_ts=rollup_at_utc,
        )

    inserted = 0
    if plan.rows:
        async with session_factory() as ins_session:
            async with ins_session.begin():
                for row in plan.rows:
                    await ins_session.execute(
                        text(
                            "INSERT INTO attribution ("
                            "    trade_id, account_id, env, "
                            "    expected_entry_price, expected_exit_price, "
                            "    expected_pnl_usd, expected_slippage_bps, "
                            "    expected_holding_days, expected_at_utc, "
                            "    realized_entry_price, realized_exit_price, "
                            "    realized_pnl_usd, realized_slippage_bps, "
                            "    realized_holding_days, realized_at_utc"
                            ") VALUES ("
                            "    :tid, :acct, :env, "
                            "    :exp_entry, :exp_exit, "
                            "    :exp_pnl, :exp_slip, "
                            "    :exp_hold, :exp_at, "
                            "    :real_entry, :real_exit, "
                            "    :real_pnl, :real_slip, "
                            "    :real_hold, :real_at"
                            ")"
                        ),
                        {
                            "tid": row.trade_id,
                            "acct": row.account_id,
                            "env": row.env,
                            "exp_entry": row.expected_entry_price,
                            "exp_exit": row.expected_exit_price,
                            "exp_pnl": row.expected_pnl_usd,
                            "exp_slip": row.expected_slippage_bps,
                            "exp_hold": row.expected_holding_days,
                            "exp_at": row.expected_at_utc,
                            "real_entry": row.realized_entry_price,
                            "real_exit": row.realized_exit_price,
                            "real_pnl": row.realized_pnl_usd,
                            "real_slip": row.realized_slippage_bps,
                            "real_hold": row.realized_holding_days,
                            "real_at": row.realized_at_utc,
                        },
                    )
                    inserted += 1

    log.info(
        "attribution_plan_applied",
        account_id=str(plan.account_id),
        env=env,
        session_date_et=plan.session_date_et.isoformat(),
        trade_count=len(plan.rows),
        inserted_count=inserted,
        audit_event_uuid=str(record.event_uuid),
    )
    return AttributionApplyResult(
        audit_event_uuid=record.event_uuid,
        inserted_row_count=inserted,
    )


__all__ = [
    "BPS_QUANTIZER",
    "PNL_QUANTIZER",
    "AttributionApplyResult",
    "AttributionError",
    "AttributionPlan",
    "AttributionRow",
    "ClosedTradeForAttribution",
    "apply_attribution_plan",
    "plan_daily_attribution",
]
