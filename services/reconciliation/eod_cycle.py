"""services/reconciliation/eod_cycle.py — wire Coinbase fetch → planner → apply.

Worker-PR-3b follow-up (post-pivot 2026-05-12); fetch path swapped to
Coinbase in crypto-pivot C0 §3.5 (2026-07-09). The pure-policy planner
in :mod:`services.reconciliation.recon` consumes a ``BackendView`` and
a ``BrokerView``; the apply orchestrator in
:mod:`services.reconciliation.apply` flushes the plan to the database;
the EOD scheduler in :mod:`services.reconciliation.scheduler` fires a
``CycleCallback`` at the 00:15 UTC cutover (after the 00:05 UTC daily
decision). This module is the glue that builds the views + ties the
three pieces together into a single callable ``run_eod_cycle`` callback
the scheduler can consume. The diff/apply engine itself (``recon.py``,
``apply.py``) is deliberately UNCHANGED by the §3.5 fetcher swap.

**Pipeline at fire time:**

  1. Fetch the venue snapshot via the injected
     :class:`~services.reconciliation.coinbase_fetcher.ReconSnapshotFetcher`
     (production: :class:`~services.reconciliation.coinbase_fetcher.CoinbaseEodFetcher`
     over the ``CoinbaseBrokerClient`` transport — ``list_positions`` +
     ``get_futures_balance_summary`` + best-effort ``list_fills``).
  2. Build :class:`BackendView` by reading ``positions_current`` +
     latest ``balances`` row for the account.
  3. Build :class:`BrokerView` from the snapshot's venue-neutral
     :class:`ReconPosition` rows (``market`` = BASE-ASSET symbol — the
     fetcher normalizes venue product ids via the runtime discovery
     map, matching the worker's ``positions_current`` convention) + the
     balance summary's aggregate USD cash.
  4. Call :func:`services.reconciliation.recon.plan_reconciliation_check`
     to produce a :class:`ReconciliationPlan`.
  5. Flush via :func:`services.reconciliation.apply.apply_reconciliation_plan`
     (audit-first per backend-spec §2.10.1).
  6. Log + return the apply result.

**Kill-switch hook (PR-J) wired via the api lifespan:** the apply layer's
optional ``state_transition_hook`` fires when
``plan.should_invoke_kill_switch`` is True; ``services/api/main.py``
builds the closure over the risk dispatcher so this module stays free
of risk-layer imports.

**CONVALESCENT clean-day tick (2026-07-09 amendment):** after the apply
step, the cycle invokes the optional ``convalescent_tick`` hook — the
production wiring for the 3-clean-UTC-days graduation counter
(``services/risk/dispatch.py::apply_convalescent_clean_day_tick``,
built as a closure in the api lifespan, same decoupling as the
kill-switch hook). Ordering is load-bearing: the tick runs AFTER
``apply_reconciliation_plan`` so a break detected by THIS cycle has
already halted the account before the tick reads state — a breach day
can never count as clean. Tick failures are best-effort (logged, never
fail the cycle); a missed tick only delays graduation.

**Alert dispatch hook:** the ``alert_dispatch_hook`` parameter flows
through to :func:`services.reconciliation.apply.apply_reconciliation_plan`
unchanged. When set, every actionable break in ``plan.alerts`` fires the
hook (which the api lifespan wires to insert an alerts row + invoke
``services.webhook_pusher.dispatcher.dispatch_alert``). When unset
(Phase 1 day-1 boot, before sops Discord webhook URLs are populated +
the api wiring follow-up PR lands), alerts are dropped on the floor +
a structured warning is logged so the operator knows to wire it.

**Prior-breaks lookup wired:** the planner's ``prior_breaks`` parameter
classifies today's breaks as within-grace vs. actionable. We materialize
prior breaks from ``reconciliation_breaks`` via
:func:`fetch_prior_breaks_within_grace_window` — unresolved rows whose
``detected_at_utc`` falls within the T+1 grace window
(:data:`DEFAULT_PRIOR_BREAKS_WINDOW_HOURS` = 36h, which is T+1 + a
half-day buffer for late-Friday breaks bridging the weekend).

When the table is empty (Phase 1 day-1, before the first cycle inserts
anything), the helper returns ``()`` cleanly + everything detected today
is actionable. Resolved rows + rows older than the window are excluded
so the planner doesn't fire a stale grace-classification long after the
operator has manually closed a break.

**A02 BINDS** — ``services/reconciliation/**`` is on the forbidden
whitelist; `risk-review-approved` required.
**A01 enforced** — audit writes flow through the apply orchestrator
which uses ``append_audit_event``.
**A05 enforced** — Decimals throughout; the Coinbase transport already
coerces at the venue boundary.
**A06 enforced** — every datetime tz-aware UTC; the fetcher's
``pulled_at_utc`` is the recon ``detected_at_utc``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Final
from uuid import UUID
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from services.audit.event_types import AuditEventType
from services.audit.writer import Environment, PhaseAtEmit, append_audit_event
from services.reconciliation.apply import (
    AlertDispatchContext,
    AlertDispatchHook,
    ReconciliationApplyResult,
    StateTransitionHook,
    apply_reconciliation_plan,
)
from services.reconciliation.coinbase_fetcher import (
    CoinbaseReconFetchError,
    CoinbaseReconSnapshot,
    ReconPosition,
    ReconSnapshotFetcher,
)
from services.reconciliation.recon import (
    AlertCategoryLiteral,
    AlertDescriptor,
    AlertSeverityLiteral,
    BackendView,
    BrokerSource,
    BrokerView,
    PriorBreak,
    ReconciliationMetric,
    plan_reconciliation_check,
)

log = structlog.get_logger()


#: Default lookback window for prior-break classification. T+1 (24h) +
#: a half-day buffer (12h) = 36h. Covers the operationally common case:
#: an EOD recon at 00:15 UTC on day N detects a break; tomorrow's EOD at
#: 00:15 UTC on day N+1 (~24h later) classifies the same break as a
#: grace-period continuation, not a fresh break worth re-alerting on.
#: Crypto trades 24/7 so there is no weekend gap to bridge (the CME-era
#: Friday→Monday limitation no longer applies — the cycle fires every
#: UTC calendar day).
DEFAULT_PRIOR_BREAKS_WINDOW_HOURS: Final[int] = 36


#: Coverage-gap threshold for the consecutive-soft-fail alert (#375
#: review follow-up C2): when a fetch soft-fail lands and the newest
#: successful snapshot (latest ``balances`` row with
#: ``source='coinbase_eod'``) is older than this many hours, the cycle
#: emits a P1 ``reconciliation_data_source_degraded`` alert instead of
#: only the WARNING log line.
#:
#: **Why N = 2 consecutive soft-fails, expressed as a 36 h age:** cycles
#: fire once per UTC day, so at the k-th consecutive soft-fail the last
#: success is ~k*24 h old — a 36 h cutoff tolerates exactly one failed
#: cycle (age ~24 h, the designed venue-outage contract: "tomorrow
#: retries") and alerts on the second (age ~48 h), with 12 h of jitter
#: margin on both sides. The value is deliberately tied to
#: :data:`DEFAULT_PRIOR_BREAKS_WINDOW_HOURS`: once the venue has been
#: dark longer than the prior-breaks grace window, open break rows age
#: out of grace classification (they then need manual resolution — the
#: #375 accepted-risk note) — i.e. the recon machinery has degraded past
#: self-healing, which is exactly when the operator must be paged.
#:
#: Deliberately NOT a one-shot: the alert re-fires on every subsequent
#: soft-failing cycle until a snapshot succeeds — nominally once per
#: day (the cycle cadence), though a restart-driven catch-up cycle that
#: also soft-fails can add a same-day repeat; every alert maps to a
#: real failed cycle, so there is no storm surface. The age is derived
#: from the DB so the signal survives api-container restarts. Severity
#: P1 → Discord ``#alerts`` only (locked routing).
RECON_COVERAGE_DEGRADED_AFTER_HOURS: Final[int] = DEFAULT_PRIOR_BREAKS_WINDOW_HOURS

#: Severity + category for the coverage-gap alert. Category reuses the
#: existing ``reconciliation_data_source_degraded`` member of the
#: alembic ``alert_category`` enum (added 2026-05-29 for the retired
#: FlexQuery fallback; dormant since C0 §3.5 — no new category, per the
#: locked taxonomy). P1 matches the backend-spec §3.27 routing recorded
#: for this category.
RECON_COVERAGE_ALERT_SEVERITY: Final[AlertSeverityLiteral] = "P1"
RECON_COVERAGE_ALERT_CATEGORY: Final[AlertCategoryLiteral] = "reconciliation_data_source_degraded"


#: Source string for the per-recon ``balances`` row INSERTed by
#: :func:`refresh_backend_from_broker_snapshot`. Constrained by the
#: table CHECK constraint in alembic 0002 (extended by the 2026-07-09
#: ``coinbase_recon_src`` migration). ``coinbase_eod`` is the canonical
#: crypto-pivot source for the daily snapshot; the api's reconciliation
#: summary (``services/api/repos/phase1.py::_RECON_BALANCE_SOURCE``)
#: filters on the same string — keep them in sync.
BALANCE_SOURCE_FROM_COINBASE: Final[str] = "coinbase_eod"


#: Quantization for the ``positions_current.unrealized_pnl`` UPDATE.
#: Schema is NUMERIC(20, 4); round half-even to match.
PNL_QUANTIZER: Final[Decimal] = Decimal("0.0001")


#: Sentinel ``triggering_break_index`` for an :class:`AlertDescriptor` that is
#: NOT tied to a reconciliation break. The api-side alert hook ignores this
#: field (it stamps ``alerts.triggering_audit_event_uuid`` from the
#: AlertDispatchContext, not from positional break alignment), so ``-1`` is a
#: harmless "not a break" marker. Mirrors the heartbeat probe + async-task
#: monitor precedents that route non-break alerts through the same recon
#: AlertDispatchContext seam.
_NO_TRIGGERING_BREAK_INDEX: Final[int] = -1


# Reverse map from the schema's TEXT ``metric`` column → the planner's
# enum. Schema values are free-form per backend-spec §3.15 + alembic
# 0004; we lock the reverse here to the canonical pair the planner emits.
# Unknown values (e.g., a Phase 2 ``net_liquidation`` metric) skip the
# row rather than crash — the planner's grace-match key requires a
# known metric, so an unknown row would never match anyway.
_METRIC_BY_STRING: Final[dict[str, ReconciliationMetric]] = {
    ReconciliationMetric.POSITION_QTY.value: ReconciliationMetric.POSITION_QTY,
    ReconciliationMetric.CASH_USD.value: ReconciliationMetric.CASH_USD,
}


CycleCallback = Callable[[date], Awaitable[None]]
"""Re-export of the scheduler's callback shape for convenience."""


ConvalescentTickHook = Callable[[], Awaitable[bool]]
"""Post-apply CONVALESCENT clean-day tick (module docstring).

Built by the api lifespan as a closure over
``services.risk.dispatch.apply_convalescent_clean_day_tick`` + the SSE
fan-out, keeping this module free of risk-layer imports (the
``StateTransitionHook`` precedent). Returns True when the tick GRADUATED
the account back to NORMAL — surfaced in the cycle-completed log line."""


def _dec_str_or_none(value: Decimal | None) -> str | None:
    """Decimal-as-string for audit payloads ([A05]); None passes through."""
    return str(value) if value is not None else None


#: Quantization for the ``balances.used_margin_pct`` column — NUMERIC(10, 8).
_MARGIN_PCT_QUANTIZER: Final[Decimal] = Decimal("0.00000001")


def _used_margin_pct(*, initial_margin: Decimal | None, net_liquidation: Decimal) -> Decimal:
    """``initial_margin / NLV`` as a fraction; 0 when un-derivable.

    Pure policy. Returns ``Decimal(0)`` when the venue omitted
    ``initial_margin`` or NLV is non-positive (dividing by <= 0 would
    produce nonsense; the raw fields are in the audit payload either
    way). Quantized half-even to the column's NUMERIC(10, 8) scale.
    """
    if initial_margin is None or net_liquidation <= 0:
        return Decimal(0)
    return (initial_margin / net_liquidation).quantize(
        _MARGIN_PCT_QUANTIZER, rounding=ROUND_HALF_EVEN
    )


@dataclass(frozen=True, slots=True)
class EodCycleConfig:
    """Static configuration for one EOD reconciliation cycle.

    Constructed once at api lifespan startup; the same config flows
    into every scheduler fire. Field semantics:

    * ``account_id`` — the operator's accounts row UUID. Sourced via
      :class:`services.api.repos.phase1.PostgresPhase1QueryRepo.fetch_active_account_id`.
    * ``env`` — audit ``env`` enum (``paper`` / ``live-small`` /
      ``live-scale``). Mirrors the api's environment setting.

    Crypto-pivot C0 §3.5 note: the FlexQuery template credentials that
    lived here died with the FlexQuery fetch path. Venue credentials
    (the CDP key pair) belong to the injected fetcher's transport, not
    to this config — the cycle itself is venue-credential-free.
    """

    account_id: UUID
    env: Environment
    phase_at_emit: PhaseAtEmit = 1


# ---------------------------------------------------------------------------
# View builders
# ---------------------------------------------------------------------------


async def build_backend_view(
    session_factory: async_sessionmaker[Any], *, account_id: UUID
) -> BackendView:
    """Materialize a :class:`BackendView` from ``positions_current`` + ``balances``.

    Position aggregation: SUM(quantity) GROUP BY market — handles the
    rare case where the same market has multiple ``positions_current``
    rows (different contract_ids; e.g., a roll where the front + new
    contracts both have non-zero qty for a moment). Phase 1's single-
    contract-per-market reality makes this a no-op SUM but the shape is
    future-proof.

    Cash + NAV come from the most-recent ``balances`` row by ``snapshot_ts``;
    if no row exists (Phase 0 single-operator with no prior fills),
    cash + NAV both default to ``Decimal(0)`` so the planner can still
    compare against the broker view (it'll flag a `cash_usd` break,
    which is the truth: backend has no cash record yet).
    """
    async with session_factory() as session:
        pos_rows = (
            await session.execute(
                text(
                    "SELECT market, SUM(quantity) AS qty "
                    "FROM positions_current "
                    "WHERE account_id = :acct "
                    "GROUP BY market"
                ),
                {"acct": account_id},
            )
        ).fetchall()
        positions: dict[str, Decimal] = {}
        for row in pos_rows:
            qty = Decimal(str(row.qty)) if row.qty is not None else Decimal(0)
            if qty != 0:
                positions[row.market] = qty

        bal_row = (
            await session.execute(
                text(
                    "SELECT cash_usd, net_liquidation FROM balances "
                    "WHERE account_id = :acct "
                    "ORDER BY snapshot_ts DESC LIMIT 1"
                ),
                {"acct": account_id},
            )
        ).fetchone()

    if bal_row is None:
        cash = Decimal(0)
        nav = Decimal(0)
    else:
        cash = Decimal(str(bal_row.cash_usd))
        nav = Decimal(str(bal_row.net_liquidation))

    return BackendView(
        positions=positions,
        cash_usd=cash,
        equity_baseline=nav,
    )


def build_broker_view(
    snapshot: CoinbaseReconSnapshot,
    *,
    positions_override: tuple[ReconPosition, ...] | None = None,
    source: BrokerSource = BrokerSource.COINBASE_EOD,
) -> BrokerView:
    """Map a :class:`CoinbaseReconSnapshot` to a :class:`BrokerView`.

    Pure policy — no I/O. Positions are SUM-aggregated by market from
    the snapshot's venue-neutral :class:`ReconPosition` rows (``market``
    is the BASE-ASSET symbol — the fetcher already normalized venue
    product ids via the runtime discovery map, so no symbol work runs
    here; the FlexQuery-era ``/`` root-ticker prefixing died with the
    CME universe). Zero-quantity rows are re-guarded for symmetry with
    ``build_backend_view`` even though the fetcher already drops them.

    **Position override seam.** When ``positions_override`` is ``None``
    (the default), ``snapshot.positions`` populates the dict. When a
    tuple of :class:`ReconPosition` is supplied, THOSE positions
    populate it instead (their ``market`` must already be canonical)
    and ``source`` should stamp the real origin onto
    :class:`BrokerView.source`. The seam was deliberately preserved
    through crypto-pivot C0-B2b for the §3.5 Coinbase fetcher and stays
    for the Phase C1 intraday probe (delta spec §3.5: "intraday probe
    reuses the REST position endpoint").

    Cash is the snapshot's ``cash_usd`` — the venue balance summary's
    ``total_usd_balance`` (aggregate USD across CFM + CBI; see the
    fetcher module docstring for the field-semantics rationale).
    """
    positions: dict[str, Decimal] = {}
    rows = snapshot.positions if positions_override is None else positions_override
    for recon_pos in rows:
        if recon_pos.quantity == 0:
            continue
        positions[recon_pos.market] = (
            positions.get(recon_pos.market, Decimal(0)) + recon_pos.quantity
        )

    return BrokerView(
        positions=positions,
        cash_usd=snapshot.cash_usd,
        source=source,
    )


async def fetch_prior_breaks_within_grace_window(
    session_factory: async_sessionmaker[Any],
    *,
    account_id: UUID,
    window_hours: int = DEFAULT_PRIOR_BREAKS_WINDOW_HOURS,
) -> tuple[PriorBreak, ...]:
    """Materialize unresolved prior breaks within the T+1 grace window.

    Reads from ``reconciliation_breaks`` filtered by:

      * ``account_id`` — the cycle's account.
      * ``resolved_at_utc IS NULL`` — operator hasn't manually closed it
        and no subsequent cycle has re-resolved it.
      * ``detected_at_utc > NOW() - INTERVAL '<window_hours> hours'`` —
        within the grace window. Older unresolved breaks are stale; the
        operator should have triaged them by now and the planner doesn't
        re-classify them as grace continuations on every cycle.

    Returns a tuple of :class:`PriorBreak` matching the planner's input
    shape. Empty tuple on first run / empty table is the normal Phase 1
    boot path — every detected break today is then actionable.

    Rows whose ``metric`` is unknown (not in :data:`_METRIC_BY_STRING`)
    are skipped silently — a Phase 2 metric like ``net_liquidation``
    landing in the schema before the planner knows about it would never
    match a current break anyway, so the row contributes nothing.

    Uses ``app_service`` Postgres role (inherited via session_factory)
    per alembic 0006 grants. SELECT-only.

    A05 enforced: ``Decimal(str(row.delta))`` round-trip preserves
    precision and avoids float coercion. A06: ``NOW()`` is timezone-aware
    on Postgres 16 with the ``timestamptz`` column type.
    """
    async with session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT metric, market, delta "
                    "FROM reconciliation_breaks "
                    "WHERE account_id = :acct "
                    "  AND resolved_at_utc IS NULL "
                    "  AND detected_at_utc > NOW() - "
                    "      (:window_hours * INTERVAL '1 hour') "
                    "ORDER BY detected_at_utc DESC"
                ),
                {"acct": account_id, "window_hours": window_hours},
            )
        ).fetchall()

    prior: list[PriorBreak] = []
    for row in rows:
        metric_str = row.metric
        if metric_str not in _METRIC_BY_STRING:
            log.warning(
                "reconciliation_eod_cycle_prior_break_unknown_metric",
                metric=metric_str,
                market=row.market,
            )
            continue
        prior.append(
            PriorBreak(
                metric=_METRIC_BY_STRING[metric_str],
                market=row.market,
                delta=Decimal(str(row.delta)),
            )
        )
    return tuple(prior)


# ---------------------------------------------------------------------------
# Coverage-gap alert on consecutive fetch soft-fails (#375 follow-up C2)
# ---------------------------------------------------------------------------


async def fetch_last_successful_snapshot_ts(
    session_factory: async_sessionmaker[Any], *, account_id: UUID
) -> datetime | None:
    """Newest ``balances.snapshot_ts`` written by a successful recon cycle.

    Every successful cycle INSERTs exactly one ``balances`` row stamped
    ``source = 'coinbase_eod'`` (:func:`refresh_backend_from_broker_snapshot`,
    unconditional), so the newest such row IS the last time the recon
    safety net actually saw the venue. ``None`` means no cycle has ever
    completed against this account (fresh bootstrap). SELECT-only.
    """
    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT MAX(snapshot_ts) AS last_success_ts FROM balances "
                    "WHERE account_id = :acct AND source = :source"
                ),
                {"acct": account_id, "source": BALANCE_SOURCE_FROM_COINBASE},
            )
        ).fetchone()
    if row is None or row.last_success_ts is None:
        return None
    ts: datetime = row.last_success_ts
    return ts


async def alert_recon_coverage_gap(
    *,
    session_factory: async_sessionmaker[Any],
    config: EodCycleConfig,
    failed_operation: str,
    failed_detail: str,
    alert_dispatch_hook: AlertDispatchHook | None,
    now_utc: datetime | None = None,
) -> bool:
    """Escalate consecutive recon fetch soft-fails to a P1 alert.

    Called from :func:`run_eod_cycle`'s soft-fail branch AFTER the
    WARNING log line. A single soft-fail is the designed venue-outage
    contract (tomorrow retries) and stays log-only; once the newest
    successful snapshot is older than
    :data:`RECON_COVERAGE_DEGRADED_AFTER_HOURS` (~= the 2nd consecutive
    failed cycle; see the constant's docstring for the N=2
    justification) — or NO successful snapshot exists at all
    (fail-visible: a bootstrap whose venue credentials never worked is
    exactly the unmonitored gap this closes) — the cycle:

      1. Appends a ``RECONCILIATION_DATA_SOURCE_DEGRADED`` audit event
         (existing enum member — its recorded "natural future producer"
         is exactly this recon-coverage surface) — audit-first per
         §2.10.1.
      2. Dispatches a P1 ``reconciliation_data_source_degraded`` alert
         through the same ``alert_dispatch_hook`` seam the break alerts
         use (heartbeat-probe precedent: ``triggering_break_index=-1``
         marks "not a reconciliation break"). Dispatch is best-effort:
         a hook failure logs at WARNING and keeps the audit row (the
         durable record). Hook unset → the apply module's
         dropped-no-hook warning shape.

    Returns True when the degradation threshold was crossed (audit row
    appended), False when the soft-fail was within tolerance. Raises
    only on audit-write failure or a DB error in the last-success
    lookup — the caller wraps this helper so a broken check can never
    mask the soft-fail contract (``run_eod_cycle`` still returns None).
    """
    now = now_utc if now_utc is not None else datetime.now(tz=UTC)
    if now.tzinfo is None:
        raise ValueError("now_utc must be tz-aware UTC per [A06]")

    last_success = await fetch_last_successful_snapshot_ts(
        session_factory, account_id=config.account_id
    )
    hours_since: int | None = None
    estimated_failed_cycles: int | None = None
    if last_success is not None:
        hours_since = int((now - last_success).total_seconds() // 3600)
        # Round age to the nearest whole cycle (24h), counting tonight's
        # failure: ~48h ± jitter reads "2", and the exact 36h boundary
        # also reads "2" — consistent with the threshold's N=2 semantics
        # (a plain floor read "1" under a title implying the 2nd failure).
        estimated_failed_cycles = max(1, (hours_since + 12) // 24)
        if hours_since < RECON_COVERAGE_DEGRADED_AFTER_HOURS:
            log.info(
                "reconciliation_coverage_soft_fail_within_tolerance",
                account_id=str(config.account_id),
                env=config.env,
                last_successful_snapshot_utc=last_success.isoformat(),
                hours_since_last_success=hours_since,
                threshold_hours=RECON_COVERAGE_DEGRADED_AFTER_HOURS,
            )
            return False

    if last_success is not None:
        title = f"Reconciliation coverage degraded: no successful venue snapshot for {hours_since}h"
        last_success_line = (
            f"Last successful venue snapshot: {last_success.isoformat()} "
            f"({hours_since}h ago; ~{estimated_failed_cycles} consecutive "
            f"failed cycles)."
        )
    else:
        title = "Reconciliation coverage degraded: no successful venue snapshot on record"
        last_success_line = (
            "No successful venue snapshot has EVER been recorded for this "
            "account — if this is not a fresh bootstrap, the recon safety "
            "net has been dark since day one (check CDP key permissions)."
        )
    body = (
        f"Tonight's EOD recon fetch soft-failed again "
        f"({failed_operation}: {failed_detail}).\n"
        f"{last_success_line}\n"
        "While this persists the nightly safety net is dark: backend-vs-"
        "venue divergence (position/cash breaks, including un-propagated "
        "fills) goes undetected, and open break rows age past the "
        f"{DEFAULT_PRIOR_BREAKS_WINDOW_HOURS}h grace window (manual "
        "resolution needed).\n"
        "Check Coinbase CFM status + the CDP key's View permission; "
        "inspect api logs for reconciliation_eod_cycle_coinbase_fetch_"
        "failed. Recon retries at the next 00:15 UTC cycle; this alert "
        "re-fires nightly until a snapshot succeeds."
    )
    payload: dict[str, Any] = {
        "trigger": "consecutive_recon_fetch_soft_fails",
        "failed_operation": failed_operation,
        "failed_detail": failed_detail,
        "last_successful_snapshot_utc": (
            last_success.isoformat() if last_success is not None else None
        ),
        "hours_since_last_success": hours_since,
        "estimated_consecutive_failed_cycles": estimated_failed_cycles,
        "threshold_hours": RECON_COVERAGE_DEGRADED_AFTER_HOURS,
        "balance_source": BALANCE_SOURCE_FROM_COINBASE,
        "observed_at_utc": now.isoformat(),
        "alert_severity": RECON_COVERAGE_ALERT_SEVERITY,
        "alert_category": RECON_COVERAGE_ALERT_CATEGORY,
    }

    # Audit-first (§2.10.1): the durable breadcrumb lands before the
    # operator-visible side effect. Raises propagate to the caller's
    # guard — no alert without the audit row.
    async with session_factory() as audit_session:
        record = await append_audit_event(
            audit_session,
            AuditEventType.RECONCILIATION_DATA_SOURCE_DEGRADED,
            payload,
            account_id=config.account_id,
            env=config.env,
            phase_at_emit=config.phase_at_emit,
            source_clock_ts=now,
        )
    log.error(
        "reconciliation_coverage_degraded",
        account_id=str(config.account_id),
        env=config.env,
        audit_event_uuid=str(record.event_uuid),
        last_successful_snapshot_utc=(
            last_success.isoformat() if last_success is not None else None
        ),
        hours_since_last_success=hours_since,
        estimated_consecutive_failed_cycles=estimated_failed_cycles,
        threshold_hours=RECON_COVERAGE_DEGRADED_AFTER_HOURS,
        failed_operation=failed_operation,
    )

    if alert_dispatch_hook is None:
        log.warning(
            "reconciliation_coverage_alert_dropped_no_hook",
            account_id=str(config.account_id),
            env=config.env,
            audit_event_uuid=str(record.event_uuid),
            note=(
                "alert_dispatch_hook is None; the coverage-gap alert will "
                "not be delivered. The audit breadcrumb landed. Wire the "
                "hook in the api lifespan (see services/api/main.py)."
            ),
        )
        return True

    try:
        await alert_dispatch_hook(
            AlertDispatchContext(
                descriptor=AlertDescriptor(
                    triggering_break_index=_NO_TRIGGERING_BREAK_INDEX,
                    severity=RECON_COVERAGE_ALERT_SEVERITY,
                    category=RECON_COVERAGE_ALERT_CATEGORY,
                    title=title,
                    body=body,
                    payload=payload,
                ),
                triggering_audit_event_uuid=record.event_uuid,
                account_id=config.account_id,
                env=config.env,
            )
        )
    except Exception:
        # Best-effort dispatch (heartbeat-probe precedent): the audit
        # row is the durable record; a Discord/DB hiccup here must not
        # abort the caller's soft-fail contract.
        log.warning(
            "reconciliation_coverage_alert_dispatch_failed",
            account_id=str(config.account_id),
            env=config.env,
            audit_event_uuid=str(record.event_uuid),
            exc_info=True,
        )
    return True


# ---------------------------------------------------------------------------
# PR-I: Broker-snapshot refresh (writes BEFORE recon planner runs)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BackendRefreshResult:
    """Outcome of :func:`refresh_backend_from_broker_snapshot`.

    Returned to ``run_eod_cycle`` for structured logging; not required
    by the caller for correctness (the audit + table mutations are
    durable independent of this object).
    """

    balance_row_id: UUID | None
    """PK of the new balances row, or None if INSERT was skipped (e.g.,
    snapshot had no account_summary)."""

    positions_marked_count: int
    """Count of positions_current rows whose unrealized_pnl + last_mark_ts
    were UPDATEd. Equals the number of broker positions whose
    (account_id, market, contract_id) matched a backend row."""

    audit_event_uuids: tuple[UUID, ...]
    """Minted audit_event_uuids in declared order:
    [BALANCE_SNAPSHOT_RECORDED, POSITION_MARK_TO_MARKET x N]."""


async def refresh_backend_from_broker_snapshot(
    snapshot: CoinbaseReconSnapshot,
    *,
    session_factory: async_sessionmaker[Any],
    account_id: UUID,
    env: Environment,
    phase_at_emit: PhaseAtEmit = 1,
) -> BackendRefreshResult:
    """Write the broker snapshot's cash + positions to the backend BEFORE
    the recon planner runs.

    Audit-first per spec §2.10.1:

      1. Append BALANCE_SNAPSHOT_RECORDED audit row (own SERIALIZABLE
         transaction via append_audit_event). Capture event_uuid.
      2. INSERT new ``balances`` row carrying the audit_event_uuid + the
         snapshot's NLV + cash. Source = ``coinbase_eod``.
      3. For each venue position with non-zero contracts:
         a. If a matching ``positions_current`` row exists for
            ``(account_id, market, contract_id=NULL)`` — append a
            POSITION_MARK_TO_MARKET audit row + UPDATE the row's
            ``unrealized_pnl + last_mark_ts``.
         b. If no row exists — skip silently. The recon planner that
            runs next will flag the divergence as a position-qty break
            (backend has 0, broker has N) and the alert fires through
            the standard path.

    Why audit-first matters here: the unrealized_pnl + balances values
    are derivative state. If we UPDATE the row without an audit event
    and the audit append later fails, the operator sees a row with
    mark data they can't trace back to a recon cycle. Audit-first
    inverts: the audit row anchors the change.

    No state-transition hook is invoked from this function. Recon
    detection + kill-switch fan-out happens in
    :func:`apply_reconciliation_plan` after the planner runs against
    the refreshed backend view. PR-J wires the state hook there.

    Mark source: only the venue-supplied ``unrealized_pnl_usd`` is
    written. The FlexQuery-era fallback that computed uPnL as
    ``(market_price - avg_cost) * qty`` is deliberately NOT ported —
    for futures that formula omits the contract multiplier
    (``contract_size``), which this module does not have; a wrong mark
    is worse than a skipped mark. When the venue omits uPnL the row is
    skipped with a WARNING (product-metadata-based computation is a
    Phase C1 follow-up once the §3.7 ``product_metadata`` snapshot is
    wired into the cycle).

    A05 enforced: Decimals throughout. A06 enforced:
    ``snapshot.pulled_at_utc`` tz-awareness re-asserted here.
    """
    if snapshot.pulled_at_utc.tzinfo is None:
        raise ValueError("snapshot.pulled_at_utc must be tz-aware UTC per [A06]")

    audit_uuids: list[UUID] = []

    # Step 1+2: balance snapshot audit + INSERT.
    summary = snapshot.balance_summary
    balance_audit_payload: dict[str, Any] = {
        "account_id": str(account_id),
        "snapshot_ts": snapshot.pulled_at_utc.isoformat(),
        "trigger": "eod_recon_refresh",
        "source": BALANCE_SOURCE_FROM_COINBASE,
        "broker_cash_usd": str(snapshot.cash_usd),
        "broker_net_liquidation_usd": str(snapshot.net_liquidation_usd),
        # Raw venue fields (Decimal-as-string or None) so any venue-side
        # semantics surprise in total/CBI/CFM/uPnL is forensically
        # visible from the audit chain alone.
        "broker_total_usd_balance": _dec_str_or_none(summary.total_usd_balance),
        "broker_cbi_usd_balance": _dec_str_or_none(summary.cbi_usd_balance),
        "broker_cfm_usd_balance": _dec_str_or_none(summary.cfm_usd_balance),
        "broker_unrealized_pnl": _dec_str_or_none(summary.unrealized_pnl),
        "broker_available_margin": _dec_str_or_none(summary.available_margin),
        "broker_initial_margin": _dec_str_or_none(summary.initial_margin),
        "broker_liquidation_buffer_percentage": _dec_str_or_none(
            summary.liquidation_buffer_percentage
        ),
        "fill_count_in_snapshot": len(snapshot.fills),
    }
    async with session_factory() as audit_session:
        bal_record = await append_audit_event(
            audit_session,
            AuditEventType.BALANCE_SNAPSHOT_RECORDED,
            balance_audit_payload,
            account_id=account_id,
            env=env,
            phase_at_emit=phase_at_emit,
            source_clock_ts=snapshot.pulled_at_utc,
        )
    audit_uuids.append(bal_record.event_uuid)

    balance_row_id: UUID | None = None
    async with session_factory() as bal_session:
        async with bal_session.begin():
            bal_row = (
                await bal_session.execute(
                    text(
                        "INSERT INTO balances ("
                        "    account_id, snapshot_ts, net_liquidation, "
                        "    cash_usd, excess_liquidity, used_margin_pct, source"
                        ") VALUES ("
                        "    :acct, :ts, :nlv, :cash, :excess, :margin, :source"
                        ") RETURNING id"
                    ),
                    {
                        "acct": account_id,
                        "ts": snapshot.pulled_at_utc,
                        "nlv": snapshot.net_liquidation_usd,
                        "cash": snapshot.cash_usd,
                        # Venue-native excess-liquidity analogue; falls
                        # back to cash when the venue omits it.
                        "excess": (
                            summary.available_margin
                            if summary.available_margin is not None
                            else snapshot.cash_usd
                        ),
                        "margin": _used_margin_pct(
                            initial_margin=summary.initial_margin,
                            net_liquidation=snapshot.net_liquidation_usd,
                        ),
                        "source": BALANCE_SOURCE_FROM_COINBASE,
                    },
                )
            ).fetchone()
            if bal_row is not None:
                balance_row_id = UUID(str(bal_row.id))

    # Step 3: per-position mark-to-market.
    positions_marked = 0
    for pos in snapshot.position_details:
        if pos.contracts == 0:
            continue  # closed; recon ignores zero-qty rows anyway
        if not pos.product_id:
            continue  # already warned by the fetcher's mapping pass

        # Same product_id → base-asset normalization the fetcher applied
        # to the planner view; positions_current is keyed by asset (#355).
        market = snapshot.product_to_asset.get(pos.product_id, pos.product_id)

        # Match by (account, market) regardless of contract_id: the C1
        # strategy worker's fill pipeline writes positions_current rows
        # WITH a real contract_id (ensure_contract_row), so the original
        # Phase-1 "contract_id IS NULL" filter matched nothing and the
        # uPnL refresh silently skipped every worker-held position
        # (observed live 2026-07-11 17:40: positions_marked=0 with a
        # matching BTC row present). One row per market in practice;
        # LIMIT 1 newest is a defensive tiebreak.
        async with session_factory() as lookup_session:
            row = (
                await lookup_session.execute(
                    text(
                        "SELECT id, quantity, avg_cost FROM positions_current "
                        "WHERE account_id = :acct AND market = :market "
                        "ORDER BY id DESC LIMIT 1"
                    ),
                    {"acct": account_id, "market": market},
                )
            ).fetchone()
        if row is None:
            log.info(
                "reconciliation_refresh_position_not_in_backend",
                account_id=str(account_id),
                env=env,
                market=market,
                broker_quantity=str(pos.contracts),
            )
            continue

        prior_qty = int(row.quantity)
        prior_avg = Decimal(str(row.avg_cost))

        # Venue-supplied unrealized_pnl only — no local recomputation
        # (see docstring: the price-minus-cost formula is wrong for
        # futures without the contract multiplier).
        if pos.unrealized_pnl_usd is None:
            log.warning(
                "reconciliation_refresh_no_broker_upnl",
                account_id=str(account_id),
                env=env,
                market=market,
                note=(
                    "venue position row omitted unrealized_pnl; mark skipped "
                    "(no contract-multiplier-safe local fallback in Phase C0)"
                ),
            )
            continue
        new_upnl = pos.unrealized_pnl_usd

        new_upnl_q = new_upnl.quantize(PNL_QUANTIZER, rounding=ROUND_HALF_EVEN)

        mark_audit_payload: dict[str, Any] = {
            "account_id": str(account_id),
            "position_id": str(row.id),
            "market": market,
            "venue_product_id": pos.product_id,
            "trigger": "eod_recon_refresh",
            "source": BALANCE_SOURCE_FROM_COINBASE,
            "quantity": prior_qty,
            "avg_cost": str(prior_avg),
            "broker_contracts": str(pos.contracts),
            "broker_entry_vwap": _dec_str_or_none(pos.entry_vwap),
            "broker_mark_price_usd": _dec_str_or_none(pos.mark_price),
            "broker_unrealized_pnl_usd": str(pos.unrealized_pnl_usd),
            "computed_unrealized_pnl_usd": str(new_upnl_q),
            "last_mark_ts": snapshot.pulled_at_utc.isoformat(),
        }
        async with session_factory() as audit_session:
            mark_record = await append_audit_event(
                audit_session,
                AuditEventType.POSITION_MARK_TO_MARKET,
                mark_audit_payload,
                account_id=account_id,
                env=env,
                phase_at_emit=phase_at_emit,
                source_clock_ts=snapshot.pulled_at_utc,
            )
        audit_uuids.append(mark_record.event_uuid)

        async with session_factory() as upd_session:
            async with upd_session.begin():
                await upd_session.execute(
                    text(
                        "UPDATE positions_current SET "
                        "    unrealized_pnl = :upnl, last_mark_ts = :ts "
                        "WHERE id = :pid"
                    ),
                    {"upnl": new_upnl_q, "ts": snapshot.pulled_at_utc, "pid": row.id},
                )
        positions_marked += 1

    log.info(
        "reconciliation_backend_refreshed",
        account_id=str(account_id),
        env=env,
        balance_row_id=str(balance_row_id) if balance_row_id else None,
        positions_marked=positions_marked,
        audit_event_count=len(audit_uuids),
    )
    return BackendRefreshResult(
        balance_row_id=balance_row_id,
        positions_marked_count=positions_marked,
        audit_event_uuids=tuple(audit_uuids),
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


async def run_eod_cycle(
    *,
    config: EodCycleConfig,
    session_factory: async_sessionmaker[Any],
    fetcher: ReconSnapshotFetcher,
    alert_dispatch_hook: AlertDispatchHook | None = None,
    state_transition_hook: StateTransitionHook | None = None,
    convalescent_tick: ConvalescentTickHook | None = None,
) -> ReconciliationApplyResult | None:
    """Execute one EOD reconciliation cycle end-to-end.

    Returns the :class:`ReconciliationApplyResult` on success. Returns
    ``None`` when the venue snapshot fetch failed (logged at WARNING;
    the scheduler keeps running so tomorrow's cycle still fires). Other
    errors propagate — the scheduler's surrounding try/except logs +
    swallows so the loop doesn't die.

    ``fetcher`` is the injected snapshot source (production: a
    :class:`~services.reconciliation.coinbase_fetcher.CoinbaseEodFetcher`
    constructed by the api lifespan over the ``SdkCoinbaseBrokerClient``
    transport; tests inject fakes at the
    :class:`~services.reconciliation.coinbase_fetcher.ReconSnapshotFetcher`
    Protocol seam).
    """
    started_at = datetime.now(tz=UTC)
    log.info(
        "reconciliation_eod_cycle_starting",
        account_id=str(config.account_id),
        env=config.env,
    )

    try:
        snapshot = await fetcher.fetch_snapshot()
    except CoinbaseReconFetchError as exc:
        log.warning(
            "reconciliation_eod_cycle_coinbase_fetch_failed",
            account_id=str(config.account_id),
            env=config.env,
            operation=exc.operation,
            detail=exc.detail,
        )
        # #375 follow-up C2: consecutive soft-fails must not stay an
        # unmonitored coverage gap. Guarded so a broken check can never
        # change the soft-fail contract (the scheduler survives, and
        # tomorrow's cycle still fires either way).
        try:
            await alert_recon_coverage_gap(
                session_factory=session_factory,
                config=config,
                failed_operation=exc.operation,
                failed_detail=exc.detail,
                alert_dispatch_hook=alert_dispatch_hook,
            )
        except Exception:
            log.exception(
                "reconciliation_eod_cycle_coverage_check_failed",
                account_id=str(config.account_id),
                env=config.env,
            )
        return None

    # PR-I: write broker-side state (cash + NLV + position marks) to
    # backend BEFORE building the backend view. After this step, the
    # backend view reflects the freshly-written rows; recon's diff is
    # against current state (not stale data). The recon planner still
    # runs unchanged and will flag any remaining divergence — e.g.,
    # position qty mismatches between backend's positions_current and
    # the broker's positions (which the refresh path doesn't reconcile
    # automatically; that's the recon's job).
    refresh_result = await refresh_backend_from_broker_snapshot(
        snapshot,
        session_factory=session_factory,
        account_id=config.account_id,
        env=config.env,
        phase_at_emit=config.phase_at_emit,
    )
    log.info(
        "reconciliation_eod_cycle_backend_refreshed",
        account_id=str(config.account_id),
        env=config.env,
        balance_row_id=(
            str(refresh_result.balance_row_id) if refresh_result.balance_row_id else None
        ),
        positions_marked=refresh_result.positions_marked_count,
        refresh_audit_event_count=len(refresh_result.audit_event_uuids),
    )

    backend_view = await build_backend_view(session_factory, account_id=config.account_id)
    broker_view = build_broker_view(snapshot)

    # Resilience signal: backend has open positions but the broker view
    # returned ZERO positions. Non-blocking: the planner still runs +
    # the breaks still land in audit + reconciliation_breaks; the log
    # line just names the suspected root cause so triage is faster.
    if backend_view.positions and not broker_view.positions:
        missing_positions_hint = (
            "Backend has open positions but the venue returned zero "
            "position rows. Likely root causes: the CDP key lacks the "
            "View permission for CFM futures, the venue liquidated/"
            "expired the positions, or list_positions is degraded. "
            "Recon will continue to flag a position_qty break per "
            "backend market until resolved."
        )
        log.warning(
            "reconciliation_eod_cycle_broker_view_missing_positions",
            account_id=str(config.account_id),
            env=config.env,
            broker_view_source=str(broker_view.source),
            backend_markets=sorted(backend_view.positions),
            backend_position_count=len(backend_view.positions),
            broker_position_count=len(broker_view.positions),
            hint=missing_positions_hint,
        )

    prior_breaks = await fetch_prior_breaks_within_grace_window(
        session_factory, account_id=config.account_id
    )
    log.info(
        "reconciliation_eod_cycle_prior_breaks_loaded",
        account_id=str(config.account_id),
        env=config.env,
        count=len(prior_breaks),
        window_hours=DEFAULT_PRIOR_BREAKS_WINDOW_HOURS,
    )

    plan = plan_reconciliation_check(
        backend_view=backend_view,
        broker_view=broker_view,
        prior_breaks=prior_breaks,
        detected_at_utc=snapshot.pulled_at_utc,
    )

    result = await apply_reconciliation_plan(
        plan,
        session_factory=session_factory,
        account_id=config.account_id,
        env=config.env,
        phase_at_emit=config.phase_at_emit,
        alert_dispatch_hook=alert_dispatch_hook,
        state_transition_hook=state_transition_hook,
        # The planner's breaks_resolved are machine re-resolutions (this
        # cycle observed the divergence gone) — stamp the honest path
        # (2026-07-13 migration; the #375 "manual" cosmetic), not the
        # operator-action default.
        resolution_path="auto_rereconciled",
    )

    # CONVALESCENT clean-day tick (2026-07-09 amendment; module docstring).
    # MUST run after apply_reconciliation_plan: the apply's kill-switch
    # hook has already halted on any actionable break, so the tick sees
    # post-halt state and a break day never counts as clean. Best-effort:
    # a tick failure only delays graduation (fail-safe), never fails the
    # cycle.
    convalescent_graduated = False
    if convalescent_tick is not None:
        try:
            convalescent_graduated = await convalescent_tick()
        except Exception:
            log.exception(
                "reconciliation_eod_cycle_convalescent_tick_failed",
                account_id=str(config.account_id),
                env=config.env,
            )

    # PR-K wiring (2026-05-16): roll up today's closed-trade attribution.
    # Fires AFTER recon so cash + position state has already been
    # refreshed + diffed. Emits one ATTRIBUTION_ROLLUP_RECORDED audit
    # event per cycle (even on zero-closed-trade days — the audit gives
    # the Today page a daily breadcrumb to render "$0 P&L" instead of
    # "unknown").
    #
    # Phase 1 reality: trades.state='closed' is only populated by the
    # PR-G exit-fill path which is NOT yet shipped — so this wiring
    # writes the daily breadcrumb today + auto-fires the per-row INSERTs
    # the day exit fills start landing. Zero impact on the recon
    # convergence semantics.
    try:
        attribution_count = await _emit_daily_attribution_rollup(
            session_factory=session_factory,
            account_id=config.account_id,
            env=config.env,
            phase_at_emit=config.phase_at_emit,
            snapshot_pulled_at_utc=snapshot.pulled_at_utc,
        )
    except Exception:
        # PR-K is best-effort: a failure here MUST NOT take down the recon
        # scheduler. The recon path already succeeded by this point + the
        # operator's daily breadcrumb just stays missing for today.
        log.exception(
            "reconciliation_eod_cycle_attribution_failed",
            account_id=str(config.account_id),
            env=config.env,
        )
        attribution_count = 0

    duration_s = (datetime.now(tz=UTC) - started_at).total_seconds()
    log.info(
        "reconciliation_eod_cycle_completed",
        account_id=str(config.account_id),
        env=config.env,
        breaks_detected=len(plan.breaks_detected),
        breaks_resolved=len(plan.breaks_resolved),
        actionable_break_count=plan.actionable_break_count,
        kill_switch_invoked=result.kill_switch_invoked,
        convalescent_graduated=convalescent_graduated,
        attribution_rows_inserted=attribution_count,
        duration_seconds=round(duration_s, 2),
    )
    return result


async def _emit_daily_attribution_rollup(
    *,
    session_factory: async_sessionmaker[Any],
    account_id: UUID,
    env: Environment,
    phase_at_emit: PhaseAtEmit,
    snapshot_pulled_at_utc: datetime,
) -> int:
    """Roll up today's closed trades into the attribution table.

    Returns the number of attribution rows INSERTed. Today (pre-exit-
    fill-path) this is always 0 + the audit row still fires.

    Anchors session_date on America/New_York wall clock per dev-guide
    §3.7 — the recon cycle's snapshot_pulled_at_utc is the natural
    "as-of" timestamp for today's rollup.

    Crypto-pivot §3.5 note: the recon now fires at 00:15 UTC, which is
    19:15/20:15 ET the *prior* calendar day — so the ET-anchored rollup
    covers the day that just ended, which is the intent. Re-anchoring
    attribution itself onto UTC days is a Phase C1 decision (the
    attribution module's API is ET-dated); today the query returns zero
    closed trades either way (exit-fill path pending).
    """
    from services.risk.attribution import (
        apply_attribution_plan,
        plan_daily_attribution,
    )

    et = ZoneInfo("America/New_York")
    session_date_et = snapshot_pulled_at_utc.astimezone(et).date()

    closed_trades = await fetch_closed_trades_for_session_date(
        session_factory,
        account_id=account_id,
        env=env,
        session_date_et=session_date_et,
    )
    plan = plan_daily_attribution(
        account_id=account_id,
        env=env,
        session_date_et=session_date_et,
        closed_trades_today=closed_trades,
        rollup_at_utc=snapshot_pulled_at_utc,
    )
    result = await apply_attribution_plan(
        plan,
        session_factory=session_factory,
        env=env,
        phase_at_emit=phase_at_emit,
        rollup_at_utc=snapshot_pulled_at_utc,
    )
    log.info(
        "reconciliation_eod_cycle_attribution_emitted",
        account_id=str(account_id),
        env=env,
        session_date_et=session_date_et.isoformat(),
        closed_trade_count=len(closed_trades),
        rows_inserted=result.inserted_row_count,
        audit_event_uuid=str(result.audit_event_uuid),
    )
    return result.inserted_row_count


async def fetch_closed_trades_for_session_date(
    session_factory: async_sessionmaker[Any],
    *,
    account_id: UUID,
    env: Environment,
    session_date_et: date,
) -> tuple[Any, ...]:
    """Materialize the closed-trade rows for one session date.

    Returns a tuple of :class:`services.risk.attribution.ClosedTradeForAttribution`
    instances. Today (pre-exit-fill-path) this query always returns
    empty — no trades have ``state='closed'`` because the exit path
    raises ``UnsupportedFillScenarioError`` in
    ``services/risk/fill_processor.py``. Returns ``()`` cleanly.

    The JOIN with ``signals`` is necessary because expected_entry_price
    + expected_slippage_bps live on the signal row (the planner needs
    them to compute the realized-vs-expected delta).

    Phase 1+ when the exit path lands:
      * `trades.state = 'closed'` AND `trades.closed_at_utc::date = :session_date`
      * JOIN signals ON signals.id = trades.entry_signal_id
      * Optional LEFT JOIN signals AS exit_sig ON signals.id =
        trades.exit_signal_id (Phase 1+ may carry the exit signal id;
        today the column is nullable + the planner accepts None)

    A05 enforced: Decimal(str(...)) round-trip on numeric columns.
    A06 enforced: every datetime tz-aware UTC.
    """
    from services.risk.attribution import ClosedTradeForAttribution

    async with session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT "
                    "  t.id AS trade_id, "
                    "  t.entry_signal_id, "
                    "  t.direction, "
                    "  t.total_quantity, "
                    "  t.avg_entry_price, "
                    "  t.avg_exit_price, "
                    "  t.realized_pnl_usd, "
                    "  t.realized_commission_usd, "
                    "  t.opened_at_utc, "
                    "  t.closed_at_utc, "
                    "  s.expected_fill_price AS expected_entry_price, "
                    "  s.expected_slippage_bps AS expected_slippage_bps, "
                    "  s.emitted_at_utc AS expected_at_utc "
                    "FROM trades t "
                    "JOIN signals s ON s.id = t.entry_signal_id "
                    "WHERE t.account_id = :acct "
                    "  AND t.env = :env "
                    "  AND t.state = 'closed' "
                    "  AND t.closed_at_utc IS NOT NULL "
                    "  AND (t.closed_at_utc AT TIME ZONE 'America/New_York')::date = :session "
                ),
                {"acct": account_id, "env": env, "session": session_date_et},
            )
        ).fetchall()

    closed: list[Any] = []
    for r in rows:
        if r.avg_exit_price is None or r.realized_pnl_usd is None:
            # Defensive: trades.state='closed' SHOULD imply both columns
            # populated. Skip with a warning rather than crash.
            log.warning(
                "attribution_fetch_skipped_incomplete_trade",
                trade_id=str(r.trade_id),
                avg_exit_price_is_none=r.avg_exit_price is None,
                realized_pnl_is_none=r.realized_pnl_usd is None,
            )
            continue
        closed.append(
            ClosedTradeForAttribution(
                trade_id=UUID(str(r.trade_id)),
                entry_signal_id=UUID(str(r.entry_signal_id)),
                direction=r.direction,
                total_quantity=int(r.total_quantity),
                avg_entry_price=Decimal(str(r.avg_entry_price)),
                avg_exit_price=Decimal(str(r.avg_exit_price)),
                realized_pnl_usd=Decimal(str(r.realized_pnl_usd)),
                realized_commission_usd=Decimal(str(r.realized_commission_usd or 0)),
                opened_at_utc=r.opened_at_utc,
                closed_at_utc=r.closed_at_utc,
                expected_entry_price=Decimal(str(r.expected_entry_price))
                if r.expected_entry_price is not None
                else Decimal("0"),
                expected_exit_price=None,  # Phase 1+ exit-signal carries this
                expected_slippage_bps=Decimal(str(r.expected_slippage_bps))
                if r.expected_slippage_bps is not None
                else Decimal("0"),
                expected_holding_days=0,  # Phase 1+ derived from strategy params
                expected_at_utc=r.expected_at_utc,
            )
        )
    return tuple(closed)


def make_cycle_callback(
    *,
    config: EodCycleConfig,
    session_factory: async_sessionmaker[Any],
    fetcher: ReconSnapshotFetcher,
    alert_dispatch_hook: AlertDispatchHook | None = None,
    state_transition_hook: StateTransitionHook | None = None,
    convalescent_tick: ConvalescentTickHook | None = None,
) -> CycleCallback:
    """Build the :class:`CycleCallback` the scheduler invokes per session day.

    The returned callable closes over ``config`` + ``session_factory`` +
    ``fetcher`` + (optional) ``alert_dispatch_hook`` + (optional)
    ``state_transition_hook`` so the scheduler doesn't need to know
    about any of them. It signals to the scheduler by raising on
    terminal failure (which the scheduler catches + logs) or returning
    normally on success / soft-failure.

    PR-J: when ``state_transition_hook`` is wired, the recon apply
    orchestrator fires it whenever ``plan.should_invoke_kill_switch``
    is True (an actionable break exists outside the grace window).
    The hook is responsible for issuing the
    NORMAL/CONVALESCENT → HALT_NEW transition (auto-halt).

    The api lifespan glue layer constructs both hooks; Phase 1 day-1
    boots without them — actionable breaks still land in audit +
    reconciliation_breaks rows.
    """

    async def _cycle(session_date: date) -> None:
        del session_date  # unused; we don't anchor the cycle on calendar date today
        await run_eod_cycle(
            config=config,
            session_factory=session_factory,
            fetcher=fetcher,
            alert_dispatch_hook=alert_dispatch_hook,
            state_transition_hook=state_transition_hook,
            convalescent_tick=convalescent_tick,
        )

    return _cycle


__all__ = [
    "BALANCE_SOURCE_FROM_COINBASE",
    "DEFAULT_PRIOR_BREAKS_WINDOW_HOURS",
    "PNL_QUANTIZER",
    "RECON_COVERAGE_ALERT_CATEGORY",
    "RECON_COVERAGE_ALERT_SEVERITY",
    "RECON_COVERAGE_DEGRADED_AFTER_HOURS",
    "BackendRefreshResult",
    "ConvalescentTickHook",
    "CycleCallback",
    "EodCycleConfig",
    "ReconPosition",
    "alert_recon_coverage_gap",
    "build_backend_view",
    "build_broker_view",
    "fetch_last_successful_snapshot_ts",
    "fetch_prior_breaks_within_grace_window",
    "make_cycle_callback",
    "refresh_backend_from_broker_snapshot",
    "run_eod_cycle",
]
