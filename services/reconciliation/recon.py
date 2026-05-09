"""Reconciliation diff — pure-policy plan-then-apply (backend-spec §2.6 + §3.15).

Same shape as ``services/risk/sizing.py``, ``services/risk/state_machine.py``,
``services/scheduler/vacation.py``, ``services/scheduler/calendar_import.py``,
and ``services/qc_adapter/poll.py``: returns a :class:`ReconciliationPlan`
struct as data; the caller (Week 4 dispatcher) owns the I/O — INSERT into
``reconciliation_breaks``, flush :class:`PendingAuditEvent` list via
``services.audit.writer.append_audit_event``, and invoke
``services.risk.state_machine.plan_invoke_kill_switch`` with
``TransitionTrigger.RECON_MISMATCH`` (severity ``routine`` per
``TRIGGER_SEVERITY``) when ``should_invoke_kill_switch`` is True.

Why split policy from I/O:

- Anti-pattern A22: unit tests must NOT write audit events. Pure policy is
  trivially testable with zero mocking — tests inspect the plan struct.
- The dispatcher that wires this module to the writer + kill-switch lives
  behind a separate forbidden-whitelist PR (Week 4); shipping the policy
  module early lets reviewers verify the plan shape independently.
- The plan struct IS the spec contract — a reviewer can inspect what the
  recon cycle WILL do without running it.

Tolerances (locked — backend-spec §2.6 + §10.1 unit-test row
``services/reconciliation/tolerance.py``):

- Position qty: tolerance = 0 (any qty mismatch is a break).
- Cash USD: tolerance = ``max($5 absolute, 1 bps x equity_baseline)``.
- Dividend ex-date widening: cash tolerance x 2 if any subscribed ETF has
  an ex-date today. Position tolerance is 0, so widening it is a no-op
  (the multiplier is applied uniformly but ``2 x 0 = 0``).

T+1 grace classification:

- A break detected today that matches a :class:`PriorBreak`
  (same metric+market+delta) → ``resolution_path = grace_period``,
  ``within_grace_period = True``, NOT actionable. The caller does NOT
  invoke the kill switch on grace-period breaks.
- A break with no prior match → actionable. Caller invokes
  ``plan_invoke_kill_switch(trigger=RECON_MISMATCH)``.
- This matches the operational intent of T+1 grace: the first time a break
  appears it triggers HALT_NEW; subsequent observations of the SAME break
  (during the recovery window) don't re-trigger. Caller is responsible for
  passing only "fresh" prior breaks (within the T+1 window).

Audit events emitted (taxonomy §3.30; mirrored in
``services/audit/event_types.py``):

- ``reconciliation_check_passed``: when there are zero breaks today.
  Includes ``dividend_ex_date_today`` so a downstream consumer can tell
  whether the wider cash tolerance was in effect.
- ``reconciliation_break_detected``: one per detected break (grace +
  actionable). Payload's ``within_grace_period`` distinguishes the two.
- ``reconciliation_break_resolved``: one per prior break ABSENT from
  today's snapshot. Default ``resolution_path = grace_period``;
  the API layer overrides to ``manual`` when a human acted.

A22 reminder: tests must NOT actually emit audit events. This module
returns :class:`PendingAuditEvent` dataclasses as data; tests inspect the
plan struct only.

Schema mapping (``alembic/versions/0004_ops_tables.py`` row
``reconciliation_breaks``):

- ``metric`` ↔ :class:`ReconciliationMetric`.
- ``resolution_path`` CHECK ↔ :class:`ResolutionPath`.
- ``source`` ↔ :class:`BrokerSource`.
- ``id``, ``account_id``, ``contract_id``, ``audit_event_uuid``,
  ``resolved_at_utc`` are caller-side (resolved at dispatcher boundary
  after audit write).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final, Literal

# ---------------------------------------------------------------------------
# Tolerances + constants
# ---------------------------------------------------------------------------

#: Position qty tolerance — exact match required (backend-spec §2.6 row
#: "Position qty"). Anything non-zero is a break.
POSITION_QTY_TOLERANCE: Final[Decimal] = Decimal("0")

#: Absolute cash tolerance floor (backend-spec §2.6 + §10.1 unit-test row).
#: Cash effective tolerance = ``max(this, equity_baseline x CASH_BPS)``.
CASH_ABS_TOLERANCE_USD: Final[Decimal] = Decimal("5.00")

#: Cash tolerance as a fraction of equity baseline (1 bps = 0.0001 = 0.01%).
CASH_BPS_TOLERANCE: Final[Decimal] = Decimal("0.0001")

#: Multiplicative widening of tolerances on dividend ex-dates (spec §2.6 row
#: "Special: dividend ex-date" — "Tolerances widen 2x for +24h"). Position
#: tolerance is 0 so widening it is a no-op; cash tolerance is doubled.
DIVIDEND_WIDENING_FACTOR: Final[Decimal] = Decimal("2")


# ---------------------------------------------------------------------------
# Enums (mirroring schema CHECK constraints)
# ---------------------------------------------------------------------------


class ReconciliationMetric(StrEnum):
    """``reconciliation_breaks.metric`` (free-form TEXT in schema; locked here).

    Backend-spec §3.15 cites ``'position_qty', 'cash_usd', ...`` — the trailing
    ellipsis allows future metrics (e.g. ``net_liquidation``, ``margin_used``).
    Today's pipeline only emits the two listed.
    """

    POSITION_QTY = "position_qty"
    CASH_USD = "cash_usd"


class ResolutionPath(StrEnum):
    """``reconciliation_breaks.resolution_path`` CHECK constraint
    (alembic/versions/0004_ops_tables.py:52)."""

    GRACE_PERIOD = "grace_period"
    MANUAL = "manual"
    KILL_SWITCH = "kill_switch"
    TOLERANCE_WIDENED_DIVIDEND = "tolerance_widened_dividend"


class BrokerSource(StrEnum):
    """``reconciliation_breaks.source`` and ``pdt_day_trade_log.source``.

    Mirrors the broker-snapshot taxonomy used across §2.6 and §3.23 — the
    same string set parses both columns.
    """

    QC_OBJECTSTORE = "qc_objectstore"
    FLEXQUERY_EOD = "flexquery_eod"
    TWS_API = "tws_api"


# Locked-taxonomy event types (backend-spec §3.30, mirrored in
# services/audit/event_types.py — RECONCILIATION_CHECK_PASSED,
# RECONCILIATION_BREAK_DETECTED, RECONCILIATION_BREAK_RESOLVED).
ReconciliationAuditEventType = Literal[
    "reconciliation_check_passed",
    "reconciliation_break_detected",
    "reconciliation_break_resolved",
]


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BackendView:
    """Backend's understanding of state at recon time.

    - ``positions``: ``market`` -> signed Decimal qty (long > 0, short < 0).
      Sourced from the ``positions`` table (backend-spec §3.10) at
      reconciliation time. Caller is responsible for materializing this
      from DB before calling ``plan_reconciliation_check``.
    - ``cash_usd``: net cash. Decimal per anti-pattern A05.
    - ``equity_baseline``: NLV (positions MTM + cash); used as the basis
      for the bps cash tolerance band. Same Decimal precision as cash.
    """

    positions: Mapping[str, Decimal]
    cash_usd: Decimal
    equity_baseline: Decimal


@dataclass(frozen=True, slots=True)
class BrokerView:
    """Broker's authoritative state at recon time.

    Phase 1: sourced from QC ObjectStore ``/state/portfolio.json`` push
    (intraday, ``BrokerSource.QC_OBJECTSTORE``) or ``/state/flexquery/<date>.xml``
    (EOD, ``BrokerSource.FLEXQUERY_EOD``) — see backend-spec §2.11.
    Phase 2: TWS API (``BrokerSource.TWS_API``).
    """

    positions: Mapping[str, Decimal]
    cash_usd: Decimal
    source: BrokerSource


@dataclass(frozen=True, slots=True)
class PriorBreak:
    """A break detected at the prior recon cycle.

    Caller passes only "fresh" prior breaks (within the T+1 grace window;
    typically the prior EOD recon's break set). Match key for grace
    classification is ``(metric, market, delta)``; ``delta`` is exact
    Decimal equality (no fuzzy matching).
    """

    metric: ReconciliationMetric
    market: str | None
    delta: Decimal


# ---------------------------------------------------------------------------
# Outputs (caller flushes these as data)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PendingAuditEvent:
    """An audit event the caller writes via ``append_audit_event()``.

    Same shape as ``services.risk.sizing.PendingAuditEvent``,
    ``services.risk.state_machine.PendingAuditEvent``,
    ``services.scheduler.vacation.PendingAuditEvent`` — single flush helper
    across modules. The writer's ``_normalize_event_type`` accepts both
    string and ``AuditEventType`` enum forms, so the Literal-string shape
    here doesn't need a retrofit when the writer's enum becomes the
    preferred form.
    """

    event_type: ReconciliationAuditEventType
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ReconciliationBreak:
    """One row to be inserted into ``reconciliation_breaks`` (backend-spec §3.15).

    Caller fills the schema columns this dataclass omits:

    - ``id``: DB-generated ``uuid_generate_v7()``.
    - ``account_id``: from caller context.
    - ``contract_id``: caller resolves ``market`` -> ``contracts.id``.
    - ``audit_event_uuid``: from the writer's :class:`AuditLogRecord` AFTER
      the corresponding ``reconciliation_break_detected`` audit write.
    - ``resolved_at_utc``: NULL on insert; set later when the break clears.

    ``resolution_path`` is set to :class:`ResolutionPath.GRACE_PERIOD`
    when the break matches a prior break; otherwise ``None`` (the
    column is nullable until the break is resolved).
    """

    metric: ReconciliationMetric
    market: str | None
    expected: Decimal
    actual: Decimal
    delta: Decimal
    tolerance: Decimal
    source: BrokerSource
    detected_at_utc: datetime
    within_grace_period: bool
    resolution_path: ResolutionPath | None


@dataclass(frozen=True, slots=True)
class ResolvedPriorBreak:
    """A prior break that no longer matches today's snapshot.

    Caller UPDATEs the matching ``reconciliation_breaks`` row's
    ``resolved_at_utc`` + ``resolution_path``. The match key is the same
    ``(metric, market, delta)`` triple used for grace classification.
    """

    metric: ReconciliationMetric
    market: str | None
    delta: Decimal


@dataclass(frozen=True, slots=True)
class ReconciliationPlan:
    """The full result of a reconciliation check.

    Caller (Week 4 dispatcher):

    1. For each break in ``breaks_detected``: ``append_audit_event(...)``
       for the corresponding ``reconciliation_break_detected`` event,
       capture the returned ``audit_event_uuid``, then INSERT one row
       into ``reconciliation_breaks`` with that uuid.
    2. For each resolved break in ``breaks_resolved``: append the
       ``reconciliation_break_resolved`` audit event, then UPDATE the
       prior ``reconciliation_breaks`` row's ``resolved_at_utc`` +
       ``resolution_path``.
    3. If ``len(breaks_detected) == 0``: append the
       ``reconciliation_check_passed`` audit event.
    4. If ``should_invoke_kill_switch`` is True: invoke
       ``plan_invoke_kill_switch(trigger=RECON_MISMATCH, ...)`` and apply
       the resulting ``StateTransitionPlan``.

    The ``audit_events`` tuple aggregates all three event types in a single
    ordered sequence so the caller can flush in one loop:
    ``break_detected`` events first (grace + actionable), then
    ``break_resolved`` events, then a single ``check_passed`` event when
    no breaks were detected today.
    """

    breaks_detected: tuple[ReconciliationBreak, ...]
    breaks_resolved: tuple[ResolvedPriorBreak, ...]
    audit_events: tuple[PendingAuditEvent, ...]
    actionable_break_count: int
    should_invoke_kill_switch: bool


class ReconciliationError(ValueError):
    """Raised on caller bugs (naive datetime, negative equity).

    ``ValueError``-subclass so FastAPI handlers can trap with one branch
    and return the canonical 400 envelope (dev-guide §3.6).
    """


# ---------------------------------------------------------------------------
# Plan: reconciliation check
# ---------------------------------------------------------------------------


def plan_reconciliation_check(
    *,
    backend_view: BackendView,
    broker_view: BrokerView,
    prior_breaks: tuple[PriorBreak, ...] = (),
    dividend_ex_date_today: bool = False,
    detected_at_utc: datetime,
) -> ReconciliationPlan:
    """Compute the reconciliation plan for one snapshot pair.

    Pure function — no I/O. Caller writes audit events + reconciliation_breaks
    rows + invokes kill switch per the contract on :class:`ReconciliationPlan`.

    Parameters
    ----------
    backend_view:
        Backend's view of state (positions, cash, equity baseline). Caller
        materializes from the ``positions`` and ``balances`` tables.
    broker_view:
        Broker's authoritative state. Source enum determines which payload
        format produced the values (intraday QC ObjectStore push vs. EOD
        FlexQuery vs. Phase-2 TWS).
    prior_breaks:
        Breaks from the prior recon cycle that are still within the T+1
        grace window. Caller filters by age before passing in. Default
        empty tuple makes "first run" trivial — every detected break is
        actionable.
    dividend_ex_date_today:
        True if any subscribed ETF has an ex-date today (caller queries
        ``dividend_history`` for ``ex_date = today``). Doubles the cash
        tolerance for the duration of this check; position tolerance is
        unaffected (0 x 2 = 0).
    detected_at_utc:
        Wall-clock for the detection — flows into each break's
        ``detected_at_utc`` and each audit event's ``ts``. MUST be
        timezone-aware (anti-pattern A06).

    Raises
    ------
    ReconciliationError
        If ``detected_at_utc`` is naive (no tzinfo).

    Returns
    -------
    ReconciliationPlan
        See class docstring for the caller contract.
    """
    if detected_at_utc.tzinfo is None:
        raise ReconciliationError("detected_at_utc must be timezone-aware (anti-pattern A06)")

    cash_tolerance = _effective_cash_tolerance(
        equity_baseline=backend_view.equity_baseline,
        dividend_ex_date_today=dividend_ex_date_today,
    )

    raw_breaks = _detect_breaks(
        backend_view=backend_view,
        broker_view=broker_view,
        cash_tolerance=cash_tolerance,
        detected_at_utc=detected_at_utc,
    )

    breaks_detected = _classify_grace(
        breaks=raw_breaks,
        prior_breaks=prior_breaks,
    )

    breaks_resolved = _resolve_prior_breaks(
        prior_breaks=prior_breaks,
        today_breaks=raw_breaks,
    )

    audit_events = _build_audit_events(
        breaks_detected=breaks_detected,
        breaks_resolved=breaks_resolved,
        broker_source=broker_view.source,
        dividend_ex_date_today=dividend_ex_date_today,
        detected_at_utc=detected_at_utc,
    )

    actionable_count = sum(1 for b in breaks_detected if not b.within_grace_period)

    return ReconciliationPlan(
        breaks_detected=breaks_detected,
        breaks_resolved=breaks_resolved,
        audit_events=audit_events,
        actionable_break_count=actionable_count,
        should_invoke_kill_switch=actionable_count > 0,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _effective_cash_tolerance(
    *,
    equity_baseline: Decimal,
    dividend_ex_date_today: bool,
) -> Decimal:
    """Cash tolerance = ``max($5 abs, equity x 1 bps)``, doubled on ex-date.

    A non-positive equity baseline (rare; possible right before bootstrap
    deposit) collapses the bps band to 0; the abs floor still binds. We
    don't reject equity_baseline <= 0 here — recon is a read-side concern
    and should be tolerant of the boundary states sizing.py rejects.
    """
    bps_band = equity_baseline * CASH_BPS_TOLERANCE
    if bps_band < Decimal("0"):
        bps_band = Decimal("0")
    base = max(CASH_ABS_TOLERANCE_USD, bps_band)
    if dividend_ex_date_today:
        return base * DIVIDEND_WIDENING_FACTOR
    return base


def _detect_breaks(
    *,
    backend_view: BackendView,
    broker_view: BrokerView,
    cash_tolerance: Decimal,
    detected_at_utc: datetime,
) -> tuple[ReconciliationBreak, ...]:
    """Compare backend vs broker; emit one ``ReconciliationBreak`` per metric.

    Position breaks: any market present in either view with a non-zero
    qty delta. Markets in only one view are diff'd against an implicit
    Decimal(0) on the other side.

    Cash break: a single break (no per-market) when the absolute cash
    delta exceeds the (possibly widened) tolerance.

    Markets are sorted alphabetically before iteration so the resulting
    break order is deterministic — important for plan-equality assertions
    in tests and for predictable audit-event sequence.
    """
    breaks: list[ReconciliationBreak] = []

    all_markets = set(backend_view.positions.keys()) | set(broker_view.positions.keys())
    for market in sorted(all_markets):
        backend_qty = backend_view.positions.get(market, Decimal("0"))
        broker_qty = broker_view.positions.get(market, Decimal("0"))
        delta = backend_qty - broker_qty
        if abs(delta) > POSITION_QTY_TOLERANCE:
            breaks.append(
                ReconciliationBreak(
                    metric=ReconciliationMetric.POSITION_QTY,
                    market=market,
                    expected=backend_qty,
                    actual=broker_qty,
                    delta=delta,
                    tolerance=POSITION_QTY_TOLERANCE,
                    source=broker_view.source,
                    detected_at_utc=detected_at_utc,
                    within_grace_period=False,
                    resolution_path=None,
                )
            )

    cash_delta = backend_view.cash_usd - broker_view.cash_usd
    if abs(cash_delta) > cash_tolerance:
        breaks.append(
            ReconciliationBreak(
                metric=ReconciliationMetric.CASH_USD,
                market=None,
                expected=backend_view.cash_usd,
                actual=broker_view.cash_usd,
                delta=cash_delta,
                tolerance=cash_tolerance,
                source=broker_view.source,
                detected_at_utc=detected_at_utc,
                within_grace_period=False,
                resolution_path=None,
            )
        )

    return tuple(breaks)


def _classify_grace(
    *,
    breaks: tuple[ReconciliationBreak, ...],
    prior_breaks: tuple[PriorBreak, ...],
) -> tuple[ReconciliationBreak, ...]:
    """Tag each break ``within_grace_period`` if it matches a prior break.

    Match key: ``(metric, market, delta)`` — exact Decimal equality on
    delta. A match means the break has been present at least one cycle
    earlier, so it's already been surfaced (and the kill switch fired
    on first detection if appropriate). Re-triggering HALT_NEW for the
    same persistent break is operational noise, so we mark it grace.
    """
    if not prior_breaks:
        return breaks

    prior_lookup: dict[tuple[ReconciliationMetric, str | None, Decimal], None] = {
        (pb.metric, pb.market, pb.delta): None for pb in prior_breaks
    }

    classified: list[ReconciliationBreak] = []
    for brk in breaks:
        key = (brk.metric, brk.market, brk.delta)
        if key in prior_lookup:
            classified.append(
                ReconciliationBreak(
                    metric=brk.metric,
                    market=brk.market,
                    expected=brk.expected,
                    actual=brk.actual,
                    delta=brk.delta,
                    tolerance=brk.tolerance,
                    source=brk.source,
                    detected_at_utc=brk.detected_at_utc,
                    within_grace_period=True,
                    resolution_path=ResolutionPath.GRACE_PERIOD,
                )
            )
        else:
            classified.append(brk)

    return tuple(classified)


def _resolve_prior_breaks(
    *,
    prior_breaks: tuple[PriorBreak, ...],
    today_breaks: tuple[ReconciliationBreak, ...],
) -> tuple[ResolvedPriorBreak, ...]:
    """Find prior breaks that are no longer present in today's snapshot.

    A prior break is "resolved" when no current break shares its
    ``(metric, market)`` pair. We don't require delta equality on
    resolution — any change in delta means the break has shifted, and
    we treat the prior observation as cleared (today's break, if any,
    is a fresh break with a different delta).
    """
    if not prior_breaks:
        return ()

    today_keys: set[tuple[ReconciliationMetric, str | None]] = {
        (b.metric, b.market) for b in today_breaks
    }

    resolved: list[ResolvedPriorBreak] = []
    for pb in prior_breaks:
        if (pb.metric, pb.market) not in today_keys:
            resolved.append(
                ResolvedPriorBreak(
                    metric=pb.metric,
                    market=pb.market,
                    delta=pb.delta,
                )
            )

    return tuple(resolved)


def _build_audit_events(
    *,
    breaks_detected: tuple[ReconciliationBreak, ...],
    breaks_resolved: tuple[ResolvedPriorBreak, ...],
    broker_source: BrokerSource,
    dividend_ex_date_today: bool,
    detected_at_utc: datetime,
) -> tuple[PendingAuditEvent, ...]:
    """Build the audit event tuple in canonical order.

    Order: detected events (grace + actionable, in detection order),
    then resolved events, then ``check_passed`` if no breaks today.
    The chain canonicalizer (services.audit.chain.jcs_serialize) sorts
    payload keys, so within a single event the field order doesn't
    matter; across events, order is preserved as written here.
    """
    events: list[PendingAuditEvent] = []
    ts_iso = detected_at_utc.isoformat()

    for brk in breaks_detected:
        events.append(
            PendingAuditEvent(
                event_type="reconciliation_break_detected",
                payload={
                    "metric": brk.metric.value,
                    "market": brk.market,
                    "expected": str(brk.expected),
                    "actual": str(brk.actual),
                    "delta": str(brk.delta),
                    "tolerance": str(brk.tolerance),
                    "source": brk.source.value,
                    "within_grace_period": brk.within_grace_period,
                    "resolution_path": (brk.resolution_path.value if brk.resolution_path else None),
                    "ts": ts_iso,
                },
            )
        )

    for resolved in breaks_resolved:
        events.append(
            PendingAuditEvent(
                event_type="reconciliation_break_resolved",
                payload={
                    "metric": resolved.metric.value,
                    "market": resolved.market,
                    "prior_delta": str(resolved.delta),
                    "resolution_path": ResolutionPath.GRACE_PERIOD.value,
                    "ts": ts_iso,
                },
            )
        )

    if not breaks_detected:
        events.append(
            PendingAuditEvent(
                event_type="reconciliation_check_passed",
                payload={
                    "source": broker_source.value,
                    "dividend_ex_date_today": dividend_ex_date_today,
                    "ts": ts_iso,
                },
            )
        )

    return tuple(events)


__all__ = [
    "CASH_ABS_TOLERANCE_USD",
    "CASH_BPS_TOLERANCE",
    "DIVIDEND_WIDENING_FACTOR",
    "POSITION_QTY_TOLERANCE",
    "BackendView",
    "BrokerSource",
    "BrokerView",
    "PendingAuditEvent",
    "PriorBreak",
    "ReconciliationAuditEventType",
    "ReconciliationBreak",
    "ReconciliationError",
    "ReconciliationMetric",
    "ReconciliationPlan",
    "ResolutionPath",
    "ResolvedPriorBreak",
    "plan_reconciliation_check",
]
