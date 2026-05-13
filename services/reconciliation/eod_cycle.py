"""services/reconciliation/eod_cycle.py — wire FlexQuery → planner → apply.

Worker-PR-3b follow-up (post-pivot 2026-05-12). The pure-policy planner
in :mod:`services.reconciliation.recon` consumes a ``BackendView`` and
a ``BrokerView``; the apply orchestrator in
:mod:`services.reconciliation.apply` flushes the plan to the database;
the EOD scheduler in :mod:`services.reconciliation.scheduler` fires a
``CycleCallback`` at the 18:30 ET cutover. This module is the glue
that builds the views + ties the three pieces together into a single
callable ``run_eod_cycle`` callback the scheduler can consume.

**Pipeline at fire time:**

  1. Fetch IBKR FlexQuery snapshot via :class:`IbkrFlexQueryClient`.
  2. Build :class:`BackendView` by reading ``positions_current`` +
     latest ``balances`` row for the account.
  3. Build :class:`BrokerView` by aggregating FlexQuery positions by
     market + summing USD cash balances. Market symbols are prefixed
     with ``/`` for futures (FUT) per backend-spec §2.6.
  4. Call :func:`services.reconciliation.recon.plan_reconciliation_check`
     to produce a :class:`ReconciliationPlan`.
  5. Flush via :func:`services.reconciliation.apply.apply_reconciliation_plan`
     (audit-first per backend-spec §2.10.1).
  6. Log + return the apply result.

**Kill-switch hook deliberately NOT wired in Phase 1:** the apply layer
accepts an optional ``state_transition_hook`` callback that fires when
``plan.should_invoke_kill_switch`` is True. Wiring that up requires
threading the risk-state machine into this module; the cleaner cut is
to ship the read+write pipeline first and add the hook in a follow-up
once the dispatcher's state-transition contract is exercised. Until
then, an actionable break still lands in audit + reconciliation_breaks;
the operator can manually halt via the System page kill-switch UI.

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
**A05 enforced** — Decimals throughout; FlexQuery's XML parser already
returns Decimals.
**A06 enforced** — every datetime tz-aware UTC; FlexQuery's
``pulled_at_utc`` is the recon ``detected_at_utc``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Final
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from services.audit.event_types import AuditEventType
from services.audit.writer import Environment, PhaseAtEmit, append_audit_event
from services.reconciliation.apply import (
    AlertDispatchHook,
    ReconciliationApplyResult,
    StateTransitionHook,
    apply_reconciliation_plan,
)
from services.reconciliation.flex_query_fetcher import (
    FlexQueryFetchError,
    IbkrFlexQueryClient,
    ReconciliationSnapshot,
)
from services.reconciliation.recon import (
    BackendView,
    BrokerSource,
    BrokerView,
    PriorBreak,
    ReconciliationMetric,
    plan_reconciliation_check,
)

log = structlog.get_logger()


#: Map FlexQuery's ``assetCategory`` enum onto the canonical market-symbol
#: convention used by ``positions_current.market``: futures get a leading
#: slash (``MES`` → ``/MES``), everything else (STK / FUND / CASH) stays
#: as-is. Matches the convention enforced in
#: ``strategies.v1_trend_following.parameters.V1_CANDIDATE_UNIVERSE``.
_FUTURES_ASSET_CATEGORIES: Final[frozenset[str]] = frozenset({"FUT"})


#: Default lookback window for prior-break classification. T+1 (24h) +
#: a half-day buffer (12h) = 36h. Covers the operationally common case:
#: an EOD recon at 18:30 ET on day N detects a break; tomorrow's EOD at
#: 18:30 ET on day N+1 (~24h later) classifies the same break as a
#: grace-period continuation, not a fresh break worth re-alerting on.
#:
#: Friday-detected breaks need weekend coverage through Monday's recon
#: (~72h elapsed). The 36h window does NOT cover that case — Monday's
#: cycle would treat Friday's break as fresh. Documented as a known
#: limitation; operators monitor weekend breaks via the system page +
#: re-classify manually if needed. Phase 2 follow-up could switch the
#: window to business-days math (skip weekends) or extend to 72h
#: unconditionally if operator demand emerges.
DEFAULT_PRIOR_BREAKS_WINDOW_HOURS: Final[int] = 36


#: Source string for the per-recon ``balances`` row INSERTed by
#: :func:`refresh_backend_from_broker_snapshot`. Constrained by the
#: table CHECK constraint in alembic 0002. ``flexquery_eod`` is the
#: canonical post-pivot source for the daily snapshot.
BALANCE_SOURCE_FROM_FLEX: Final[str] = "flexquery_eod"


#: Quantization for the ``positions_current.unrealized_pnl`` UPDATE.
#: Schema is NUMERIC(20, 4); round half-even to match.
PNL_QUANTIZER: Final[Decimal] = Decimal("0.0001")


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


@dataclass(frozen=True, slots=True)
class EodCycleConfig:
    """Static configuration for one EOD reconciliation cycle.

    Constructed once at api lifespan startup; the same config flows
    into every scheduler fire. Field semantics:

    * ``account_id`` — the operator's accounts row UUID. Sourced via
      :class:`services.api.repos.phase1.PostgresPhase1QueryRepo.fetch_active_account_id`.
    * ``env`` — audit ``env`` enum (``paper`` / ``live-small`` /
      ``live-scale``). Mirrors the api's environment setting.
    * ``flex_query_id`` / ``flex_query_token`` — IBKR-portal template
      credentials (operator pre-creates the template + records both into
      sops as ``ibkr.flex_query_id`` + ``ibkr.flex_query_token``).
    """

    account_id: UUID
    env: Environment
    flex_query_id: int
    flex_query_token: str
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


def build_broker_view(snapshot: ReconciliationSnapshot) -> BrokerView:
    """Map a :class:`ReconciliationSnapshot` to a :class:`BrokerView`.

    Pure policy — no I/O. The FlexQuery snapshot carries per-currency
    cash balances; we sum the USD balance (Phase 1 universe is USD-
    denominated, so non-USD balances would be a data-quality concern
    surfaced separately). Positions are SUM-aggregated by market with
    the futures ``/`` prefix applied to the FlexQuery ``symbol`` so the
    backend's ``/MES`` matches the broker's ``MES`` ``assetCategory=FUT``.

    Zero-quantity positions are dropped (matches ``build_backend_view``)
    so the recon's symmetric-difference comparison doesn't generate
    false-positive breaks for closed positions FlexQuery still includes
    in the snapshot.
    """
    positions: dict[str, Decimal] = {}
    for pos in snapshot.positions:
        if pos.quantity == 0:
            continue
        market = f"/{pos.symbol}" if pos.sec_type in _FUTURES_ASSET_CATEGORIES else pos.symbol
        positions[market] = positions.get(market, Decimal(0)) + pos.quantity

    cash_usd = Decimal(0)
    for bal in snapshot.cash_balances:
        if bal.currency == "USD":
            cash_usd += bal.balance

    return BrokerView(
        positions=positions,
        cash_usd=cash_usd,
        source=BrokerSource.FLEXQUERY_EOD,
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


def _market_from_flex_symbol(symbol: str, sec_type: str) -> str:
    """Map a FlexQuery ``symbol`` + ``sec_type`` to the backend's
    ``positions_current.market`` convention. Futures get a leading ``/``;
    everything else passes through as-is. Mirrors the convention used
    by :func:`build_broker_view`."""
    return f"/{symbol}" if sec_type in _FUTURES_ASSET_CATEGORIES else symbol


async def refresh_backend_from_broker_snapshot(
    snapshot: ReconciliationSnapshot,
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
         snapshot's NLV + cash. Source = ``flexquery_eod``.
      3. For each FlexQuery position with non-zero quantity:
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

    A05 enforced: Decimals throughout. A06 enforced:
    ``snapshot.pulled_at_utc`` is tz-aware (asserted at module load
    of the flex_query_fetcher).
    """
    if snapshot.pulled_at_utc.tzinfo is None:
        raise ValueError("snapshot.pulled_at_utc must be tz-aware UTC per [A06]")

    audit_uuids: list[UUID] = []

    # Step 1+2: balance snapshot audit + INSERT.
    summary = snapshot.account_summary
    balance_audit_payload: dict[str, Any] = {
        "account_id": str(account_id),
        "snapshot_ts": snapshot.pulled_at_utc.isoformat(),
        "trigger": "eod_recon_refresh",
        "source": BALANCE_SOURCE_FROM_FLEX,
        "broker_cash_usd": str(summary.cash_usd),
        "broker_net_liquidation_usd": str(summary.net_liquidation_usd),
        "broker_stock_market_value_usd": str(summary.stock_market_value_usd),
        "broker_bond_market_value_usd": str(summary.bond_market_value_usd),
        "broker_futures_pnl_usd": str(summary.futures_pnl_usd),
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
                        "nlv": summary.net_liquidation_usd,
                        "cash": summary.cash_usd,
                        # Phase 1 placeholder: equals cash until broker-side
                        # excess_liquidity calc is wired (Phase 2+).
                        "excess": summary.cash_usd,
                        "margin": Decimal("0"),
                        "source": BALANCE_SOURCE_FROM_FLEX,
                    },
                )
            ).fetchone()
            if bal_row is not None:
                balance_row_id = UUID(str(bal_row.id))

    # Step 3: per-position mark-to-market.
    positions_marked = 0
    for pos in snapshot.positions:
        if pos.quantity == 0:
            continue  # closed; recon ignores zero-qty rows anyway

        market = _market_from_flex_symbol(pos.symbol, pos.sec_type)

        # Phase 1 contract_id=NULL match. If/when contract resolution
        # lands (Phase 2+), this query expands to take a contract_id.
        async with session_factory() as lookup_session:
            row = (
                await lookup_session.execute(
                    text(
                        "SELECT id, quantity, avg_cost FROM positions_current "
                        "WHERE account_id = :acct AND market = :market "
                        "  AND contract_id IS NULL"
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
                broker_quantity=str(pos.quantity),
            )
            continue

        prior_qty = int(row.quantity)
        prior_avg = Decimal(str(row.avg_cost))

        # Prefer broker-supplied unrealized_pnl; fall back to compute
        # from (market_price - avg_cost) * qty when missing.
        if pos.unrealized_pnl_usd is not None:
            new_upnl = pos.unrealized_pnl_usd
        elif pos.market_price_usd is not None:
            new_upnl = (pos.market_price_usd - prior_avg) * Decimal(prior_qty)
        else:
            log.warning(
                "reconciliation_refresh_no_mark_price",
                account_id=str(account_id),
                env=env,
                market=market,
                note="broker snapshot has neither market_price_usd nor unrealized_pnl_usd",
            )
            continue

        new_upnl_q = new_upnl.quantize(PNL_QUANTIZER, rounding=ROUND_HALF_EVEN)

        mark_audit_payload: dict[str, Any] = {
            "account_id": str(account_id),
            "position_id": str(row.id),
            "market": market,
            "trigger": "eod_recon_refresh",
            "source": BALANCE_SOURCE_FROM_FLEX,
            "quantity": prior_qty,
            "avg_cost": str(prior_avg),
            "broker_market_price_usd": (
                str(pos.market_price_usd) if pos.market_price_usd is not None else None
            ),
            "broker_unrealized_pnl_usd": (
                str(pos.unrealized_pnl_usd) if pos.unrealized_pnl_usd is not None else None
            ),
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
    flex_client_factory: Callable[[], IbkrFlexQueryClient] | None = None,
    alert_dispatch_hook: AlertDispatchHook | None = None,
    state_transition_hook: StateTransitionHook | None = None,
) -> ReconciliationApplyResult | None:
    """Execute one EOD reconciliation cycle end-to-end.

    Returns the :class:`ReconciliationApplyResult` on success. Returns
    ``None`` when the FlexQuery fetch failed (logged at WARNING; the
    scheduler keeps running so tomorrow's cycle still fires). Other
    errors propagate — the scheduler's surrounding try/except logs +
    swallows so the loop doesn't die.

    ``flex_client_factory`` is injectable for tests; production passes
    ``None`` and we construct the default :class:`IbkrFlexQueryClient`
    from config.
    """
    started_at = datetime.now(tz=UTC)
    log.info(
        "reconciliation_eod_cycle_starting",
        account_id=str(config.account_id),
        env=config.env,
        flex_query_id=config.flex_query_id,
    )

    if flex_client_factory is None:

        def _default_factory() -> IbkrFlexQueryClient:
            return IbkrFlexQueryClient(
                flex_query_id=config.flex_query_id,
                token=config.flex_query_token,
            )

        flex_client_factory = _default_factory

    try:
        flex_client = flex_client_factory()
        snapshot = await flex_client.fetch_snapshot()
    except FlexQueryFetchError as exc:
        log.warning(
            "reconciliation_eod_cycle_flex_fetch_failed",
            account_id=str(config.account_id),
            env=config.env,
            error_code=exc.error_code,
            message=exc.message,
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
    )
    duration_s = (datetime.now(tz=UTC) - started_at).total_seconds()
    log.info(
        "reconciliation_eod_cycle_completed",
        account_id=str(config.account_id),
        env=config.env,
        breaks_detected=len(plan.breaks_detected),
        breaks_resolved=len(plan.breaks_resolved),
        actionable_break_count=plan.actionable_break_count,
        kill_switch_invoked=result.kill_switch_invoked,
        duration_seconds=round(duration_s, 2),
    )
    return result


def make_cycle_callback(
    *,
    config: EodCycleConfig,
    session_factory: async_sessionmaker[Any],
    alert_dispatch_hook: AlertDispatchHook | None = None,
    state_transition_hook: StateTransitionHook | None = None,
) -> CycleCallback:
    """Build the :class:`CycleCallback` the scheduler invokes per session day.

    The returned callable closes over ``config`` + ``session_factory`` +
    (optional) ``alert_dispatch_hook`` + (optional)
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
            alert_dispatch_hook=alert_dispatch_hook,
            state_transition_hook=state_transition_hook,
        )

    return _cycle


__all__ = [
    "BALANCE_SOURCE_FROM_FLEX",
    "DEFAULT_PRIOR_BREAKS_WINDOW_HOURS",
    "PNL_QUANTIZER",
    "BackendRefreshResult",
    "CycleCallback",
    "EodCycleConfig",
    "build_backend_view",
    "build_broker_view",
    "fetch_prior_breaks_within_grace_window",
    "make_cycle_callback",
    "refresh_backend_from_broker_snapshot",
    "run_eod_cycle",
]
