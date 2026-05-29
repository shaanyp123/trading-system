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
from typing import Any, Final, Literal
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
from services.reconciliation.flex_query_fetcher import (
    FlexQueryFetchError,
    IbkrFlexQueryClient,
    ReconciliationSnapshot,
)
from services.reconciliation.ibkr_intraday import (
    ReconPosition,
    ReconPositionsFetchError,
    fetch_recon_positions,
)
from services.reconciliation.recon import (
    AlertDescriptor,
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
    * ``position_source`` — Option C (2026-05-28) feature flag. ``"flexquery"``
      (default) keeps the position-quantity check on the FlexQuery XML
      snapshot. ``"reqpositions"`` sources the broker position list from
      IBKR's real-time TWS API (``reqPositions`` via clientId=4) instead,
      eliminating the same-day-fill settlement-lag false positives. Cash /
      NAV / position MTM stay on FlexQuery either way. PR-B ships the
      ``"flexquery"`` default (merging changes nothing in prod); PR-C flips
      it after empirical cycle observation.
    * ``ibkr_host`` / ``ibkr_port`` / ``ibkr_account_id`` — ib_gateway
      connection params for the ``"reqpositions"`` source. Mirror the
      order worker's settings (``API_IBKR_HOST`` / ``API_IBKR_PORT`` /
      ``API_IBKR_ACCOUNT``) so recon talks to the SAME gateway + account,
      isolated only by clientId (recon claims the reserved ``clientId=4``
      per dev-guide §1.5, distinct from the worker's 1 + bar_sync's 3).
      ``ibkr_account_id`` is the IBKR account NUMBER (e.g. ``"U25655583"``),
      distinct from ``account_id`` (the backend accounts-row UUID); ``None``
      lets the adapter use the default account on the single-account login.
      Unused when ``position_source="flexquery"``.
    """

    account_id: UUID
    env: Environment
    flex_query_id: int
    flex_query_token: str
    phase_at_emit: PhaseAtEmit = 1
    position_source: Literal["flexquery", "reqpositions"] = "flexquery"
    ibkr_host: str = "ib_gateway"
    ibkr_port: int = 4004
    ibkr_account_id: str | None = None


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
    snapshot: ReconciliationSnapshot,
    *,
    positions_override: tuple[ReconPosition, ...] | None = None,
    source: BrokerSource = BrokerSource.FLEXQUERY_EOD,
) -> BrokerView:
    """Map a :class:`ReconciliationSnapshot` to a :class:`BrokerView`.

    Pure policy — no I/O. The FlexQuery snapshot carries per-currency
    cash balances; we sum the USD balance (Phase 1 universe is USD-
    denominated, so non-USD balances would be a data-quality concern
    surfaced separately). Positions are SUM-aggregated by market with
    the futures ``/`` prefix applied to the FlexQuery root ticker so the
    backend's ``/M2K`` matches the broker's ``M2K`` ``assetCategory=FUT``.

    **Position source (Option C 2026-05-28).** When ``positions_override``
    is ``None`` (the default), the position dict is built from the
    FlexQuery snapshot's ``OpenPositions`` rows as described above. When
    a tuple of :class:`ReconPosition` is supplied (the
    ``position_source="reqpositions"`` path in :func:`run_eod_cycle`),
    THOSE positions populate the dict instead — their ``market`` is
    already canonical (the adapter's ``_contract_from_ib`` applied the
    ``/`` prefix), so no FlexQuery symbol normalization runs. Cash is
    ALWAYS sourced from the FlexQuery snapshot regardless of the position
    source (``reqPositions`` doesn't carry cash). ``source`` stamps the
    resulting :class:`BrokerView.source` — callers pass
    :attr:`BrokerSource.TWS_API` alongside an override so the audit
    payload records the hybrid reality (positions from TWS, cash from
    FlexQuery); it defaults to :attr:`BrokerSource.FLEXQUERY_EOD` for the
    unoverridden FlexQuery path.

    **Futures symbol normalization (post-2026-05-27 fix):** the FlexQuery
    XML reports two attributes that can identify a FUT position:

    * ``symbol`` — what the template is configured to print; can be
      EITHER the root (``"M2K"``) or the contract-month form
      (``"M2KM6"``, where ``M6`` = June 2026 expiry).
    * ``underlyingSymbol`` — IBKR's root ticker, populated unconditionally
      on derivative rows in standard templates. Parsed into
      :attr:`FlexPosition.underlying_symbol`.

    The backend's ``positions_current.market`` convention is root-only
    with a leading slash (``"/M2K"``). When the FlexQuery template uses
    contract-month symbols (the common configuration), comparing
    ``f"/{symbol}"`` (= ``"/M2KM6"``) against backend's ``"/M2K"`` produces
    a false-positive break every cycle (the EOD recon at 2026-05-27 22:30
    UTC fired this exact failure mode: broker view showed market
    ``"/M2K"`` qty 0, backend showed qty 1, a routine break landed).

    Fix: for FUT (and OPT) positions, prefer ``pos.underlying_symbol``
    when populated; fall back to ``pos.symbol`` when it's None. For
    non-derivative rows (STK / CASH / FUND) the ``symbol`` is already
    the canonical identifier so the underlying_symbol field is ignored.

    Zero-quantity positions are dropped (matches ``build_backend_view``)
    so the recon's symmetric-difference comparison doesn't generate
    false-positive breaks for closed positions FlexQuery still includes
    in the snapshot.

    **Cash source selection (post-2026-05-16 fix):** the FlexQuery XML
    response carries cash in TWO independent sections:

    * ``EquitySummaryByReportDateInBase.cash`` → parsed into
      ``snapshot.account_summary.cash_usd``. Populated unconditionally
      by any FlexQuery template with the AccountInformation section
      enabled (the default).
    * ``CashReportCurrency.endingCash`` per-currency rows → parsed into
      ``snapshot.cash_balances``. Populated ONLY when the template has
      the "Cash Report" section explicitly enabled.

    ``refresh_backend_from_broker_snapshot`` (PR-I) writes to the
    backend ``balances`` table from ``account_summary.cash_usd``; this
    function was reading ``cash_balances``. When the operator's template
    is missing the Cash Report section, the two sources disagree —
    backend gets the correct cash, but the recon planner sees broker
    cash = 0 and flags a false-positive break every cycle.

    Fix: ``cash_balances`` remains the primary source (more granular,
    per-currency, preferred when available). When ``cash_balances`` is
    empty AND ``account_summary.cash_usd`` is non-zero, fall back to
    the account-summary value with a structured log line so the
    operator knows the template is producing partial data + can update
    the template if desired. The fallback ensures recon converges
    even with a partially-configured FlexQuery template.
    """
    positions: dict[str, Decimal] = {}
    if positions_override is None:
        for pos in snapshot.positions:
            if pos.quantity == 0:
                continue
            market = _market_from_flex_symbol(
                pos.symbol, pos.sec_type, underlying_symbol=pos.underlying_symbol
            )
            positions[market] = positions.get(market, Decimal(0)) + pos.quantity
    else:
        # reqPositions path: markets are already canonical (the adapter
        # prefixed FUT roots with "/"); zero-qty rows are already dropped
        # by fetch_recon_positions but we re-guard for symmetry with the
        # FlexQuery branch + build_backend_view.
        for recon_pos in positions_override:
            if recon_pos.quantity == 0:
                continue
            positions[recon_pos.market] = (
                positions.get(recon_pos.market, Decimal(0)) + recon_pos.quantity
            )

    cash_usd = Decimal(0)
    cash_balances_had_usd_row = False
    for bal in snapshot.cash_balances:
        if bal.currency == "USD":
            cash_usd += bal.balance
            cash_balances_had_usd_row = True

    if not cash_balances_had_usd_row:
        # FlexQuery template is missing the Cash Report section (or it
        # returned no USD row). Fall back to the account summary's cash
        # value, which is populated by AccountInformation in every
        # template. Log so the operator can see why we fell back.
        fallback_cash = snapshot.account_summary.cash_usd
        log.info(
            "recon_broker_view_cash_fallback_to_account_summary",
            cash_balances_count=len(snapshot.cash_balances),
            account_summary_cash_usd=str(fallback_cash),
            hint=(
                "FlexQuery template appears to be missing the 'Cash Report' "
                "section. Backend will use account_summary.cash_usd; consider "
                "updating the template to include Cash Report for granular "
                "per-currency reconciliation."
            ),
        )
        cash_usd = fallback_cash

    return BrokerView(
        positions=positions,
        cash_usd=cash_usd,
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


def _market_from_flex_symbol(
    symbol: str, sec_type: str, *, underlying_symbol: str | None = None
) -> str:
    """Map a FlexQuery ``symbol`` + ``sec_type`` (+ optional
    ``underlying_symbol``) to the backend's ``positions_current.market``
    convention.

    For FUT (and OPT-like derivative categories in ``_FUTURES_ASSET_CATEGORIES``),
    prefer ``underlying_symbol`` when populated — IBKR's FlexQuery reports
    the contract-month form (``"M2KM6"``) in ``symbol`` when the template
    is configured that way, while ``underlyingSymbol`` carries the root
    ticker (``"M2K"``). Falls back to ``symbol`` when ``underlying_symbol``
    is None (older templates / parser samples that pre-date the
    underlyingSymbol field).

    Futures get a leading ``/`` (matches the backend's
    ``positions_current.market`` convention + the
    ``V1_CANDIDATE_UNIVERSE`` strings). Everything else (STK / FUND /
    CASH) passes through as-is. Mirrors the convention used by
    :func:`build_broker_view`.
    """
    if sec_type in _FUTURES_ASSET_CATEGORIES:
        root = underlying_symbol if underlying_symbol else symbol
        return f"/{root}"
    return symbol


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

        market = _market_from_flex_symbol(
            pos.symbol, pos.sec_type, underlying_symbol=pos.underlying_symbol
        )

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


def _select_position_source(config: EodCycleConfig) -> str:
    """Return the active position-source label for this cycle.

    Thin, named indirection so the ``eod_cycle_position_source_selected``
    log line and the source-branch in :func:`run_eod_cycle` read the
    selection from one place. Today it's a straight field read; a future
    cycle that needs an env-gated or kill-switch-forced fallback would
    centralize that logic here without touching the call sites.
    """
    return config.position_source


async def _emit_data_source_degraded_alert(
    *,
    session_factory: async_sessionmaker[Any],
    account_id: UUID,
    env: Environment,
    phase_at_emit: PhaseAtEmit,
    exc: ReconPositionsFetchError,
    alert_dispatch_hook: AlertDispatchHook | None,
) -> None:
    """Page the operator when the reqPositions source degraded to FlexQuery.

    Option C recon-fix follow-up (2026-05-29). When the
    ``position_source="reqpositions"`` per-cycle reqPositions fetch fails
    terminally, ``run_eod_cycle`` falls back to the FlexQuery position list
    (preserving recon coverage) but the fallback silently reintroduces the
    same-day-fill settlement-lag false-break behavior for one cycle. This
    helper turns that previously log-only event into an active operator
    push:

      1. Writes a durable :data:`AuditEventType.RECONCILIATION_DATA_SOURCE_DEGRADED`
         audit row (audit-first per backend-spec §2.10.1).
      2. Dispatches a ``P1`` alert (category
         ``reconciliation_data_source_degraded`` → Discord #alerts only;
         NO #critical / email — a single-cycle coverage downgrade, not
         money at risk) through the existing ``alert_dispatch_hook`` /
         :class:`AlertDispatchContext` seam, reusing the
         heartbeat-probe / async-task-monitor precedent for non-break
         alerts.

    **This helper NEVER raises.** The Option C design (Q3) policy is
    "degrade, don't skip recon" — the caller's FlexQuery fallback MUST run
    regardless of whether the audit write or the alert dispatch succeeds.
    Failure modes:

      * Audit write fails → log ERROR + return WITHOUT dispatching (an
        :class:`AlertDispatchContext` requires a
        ``triggering_audit_event_uuid``, which we cannot supply). The
        caller still falls back to FlexQuery.
      * No hook wired (Phase 1 day-1 boot before sops Discord URLs land)
        → log WARNING + return; the audit row is still the durable record.
      * Hook raises (Discord outage) → log ERROR + return; the audit row
        already landed.
    """
    payload: dict[str, Any] = {
        "degraded_source": "reqpositions",
        "fallback_source": "flexquery",
        "operation": exc.operation,
        "reason": exc.detail,
        "underlying_exception_class": exc.underlying_exception_class,
    }

    # Audit-first (backend-spec §2.10.1): the durable breadcrumb lands
    # BEFORE the operator-visible push. A21: the writer opens its own
    # transaction, so we hand it a fresh session.
    try:
        async with session_factory() as audit_session:
            record = await append_audit_event(
                audit_session,
                AuditEventType.RECONCILIATION_DATA_SOURCE_DEGRADED,
                payload,
                account_id=account_id,
                env=env,
                phase_at_emit=phase_at_emit,
            )
    except Exception:
        log.error(
            "eod_cycle_data_source_degraded_audit_write_failed",
            account_id=str(account_id),
            env=env,
            operation=exc.operation,
            reason=exc.detail,
            exc_info=True,
        )
        return

    audit_uuid = record.event_uuid

    if alert_dispatch_hook is None:
        log.warning(
            "eod_cycle_data_source_degraded_alert_skipped_no_hook",
            account_id=str(account_id),
            env=env,
            audit_event_uuid=str(audit_uuid),
        )
        return

    try:
        title = "Reconciliation position source degraded"
        body = (
            "The per-cycle reqPositions (real-time TWS, clientId=4) fetch "
            "failed; EOD reconciliation fell back to the FlexQuery position "
            "list for this cycle. Cash / NAV / position checks still ran, but "
            "same-day-fill settlement-lag false breaks may reappear until the "
            "next cycle restores the real-time source. "
            f"Failed operation: {exc.operation}. Reason: {exc.detail}."
        )
        # ``severity="P1"`` is a valid AlertSeverityLiteral member; only
        # ``category`` needs the type-ignore because AlertCategoryLiteral is
        # locked to "reconciliation_break" in the pure planner (recon.py).
        # We deliberately do NOT widen that literal — recon.py stays pure;
        # the api-side hook accepts any AlertCategory enum value. Mirrors
        # the heartbeat-probe precedent.
        descriptor = AlertDescriptor(
            triggering_break_index=_NO_TRIGGERING_BREAK_INDEX,
            severity="P1",
            category="reconciliation_data_source_degraded",  # type: ignore[arg-type]
            title=title,
            body=body,
            payload=payload,
        )
        ctx = AlertDispatchContext(
            descriptor=descriptor,
            triggering_audit_event_uuid=audit_uuid,
            account_id=account_id,
            env=env,
        )
        await alert_dispatch_hook(ctx)
    except Exception:
        log.error(
            "eod_cycle_data_source_degraded_alert_dispatch_failed",
            account_id=str(account_id),
            env=env,
            audit_event_uuid=str(audit_uuid),
            exc_info=True,
        )
        return

    log.info(
        "eod_cycle_data_source_degraded_alert_emitted",
        account_id=str(account_id),
        env=env,
        audit_event_uuid=str(audit_uuid),
        operation=exc.operation,
    )


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

    position_source = _select_position_source(config)
    log.info(
        "eod_cycle_position_source_selected",
        account_id=str(config.account_id),
        env=config.env,
        source=position_source,
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
    if position_source == "reqpositions":
        try:
            recon_positions = await fetch_recon_positions(
                account_id=config.ibkr_account_id,
                host=config.ibkr_host,
                port=config.ibkr_port,
            )
        except ReconPositionsFetchError as exc:
            # The real-time TWS view is unavailable this cycle. Fall back
            # to the FlexQuery position list (today's behavior) so the
            # cycle still runs cash/NAV + position checks rather than
            # skipping reconciliation entirely — the settlement-lag false
            # positive may reappear for this one cycle, but recon coverage
            # is preserved. Logged at ERROR (not WARNING) because we've
            # silently reverted to the known-broken source.
            log.error(
                "eod_cycle_reqpositions_failed",
                account_id=str(config.account_id),
                env=config.env,
                operation=exc.operation,
                reason=exc.detail,
                underlying_exception_class=exc.underlying_exception_class,
                fallback="flexquery",
            )
            # Option C recon-fix follow-up (2026-05-29): turn the silent
            # fallback into an active operator push. Writes a
            # RECONCILIATION_DATA_SOURCE_DEGRADED audit row (audit-first)
            # then dispatches a P1 alert (Discord #alerts). Fully defensive
            # — it NEVER raises, so the FlexQuery fallback below always runs
            # regardless of the audit / alert outcome.
            await _emit_data_source_degraded_alert(
                session_factory=session_factory,
                account_id=config.account_id,
                env=config.env,
                phase_at_emit=config.phase_at_emit,
                exc=exc,
                alert_dispatch_hook=alert_dispatch_hook,
            )
            broker_view = build_broker_view(snapshot)
        else:
            broker_view = build_broker_view(
                snapshot,
                positions_override=recon_positions,
                source=BrokerSource.TWS_API,
            )
    else:
        broker_view = build_broker_view(snapshot)

    # Resilience signal: when backend has futures positions but the broker
    # view returned ZERO futures positions, the FlexQuery template is
    # almost certainly missing the OpenPositions FUT section (or it
    # filtered FUT rows out). Without this warning, the recon planner
    # silently emits one position_qty break per backend FUT market every
    # cycle — operator sees "false break" with no clear pointer at the
    # template config. This warning is non-blocking: the planner still
    # runs + the breaks still land in audit + reconciliation_breaks (the
    # operator may want to investigate via psql / Audit page), but the
    # log line names the suspected root cause so triage is faster.
    backend_fut_markets = {m for m in backend_view.positions if m.startswith("/")}
    broker_fut_markets = {m for m in broker_view.positions if m.startswith("/")}
    if backend_fut_markets and not broker_fut_markets:
        log.warning(
            "reconciliation_eod_cycle_broker_view_missing_futures",
            account_id=str(config.account_id),
            env=config.env,
            backend_futures_markets=sorted(backend_fut_markets),
            backend_futures_count=len(backend_fut_markets),
            broker_position_count=len(broker_view.positions),
            hint=(
                "Backend has open FUT positions but the broker view returned "
                "zero FUT rows. Likely root cause: the FlexQuery template is "
                "missing the OpenPositions section for futures, or the "
                "section is configured to filter out FUT rows. Update the "
                "template in IBKR portal (Reports → Flex Queries) to include "
                "OpenPositions for Futures. Recon will continue to flag a "
                "position_qty break per backend FUT market until fixed."
            ),
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
