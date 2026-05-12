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

**Prior-breaks lookup deliberately empty in Phase 1:** the planner's
``prior_breaks`` parameter classifies today's breaks as within-grace
vs. actionable. Phase 1 starts the recon ledger from zero (no prior
breaks); the empty tuple means everything detected today is actionable.
The follow-up PR adding the prior-breaks query lands when the recon
table has its first row of history to query against.

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
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from services.audit.writer import Environment, PhaseAtEmit
from services.reconciliation.apply import (
    ReconciliationApplyResult,
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
    plan_reconciliation_check,
)

log = structlog.get_logger()


#: Map FlexQuery's ``assetCategory`` enum onto the canonical market-symbol
#: convention used by ``positions_current.market``: futures get a leading
#: slash (``MES`` → ``/MES``), everything else (STK / FUND / CASH) stays
#: as-is. Matches the convention enforced in
#: ``strategies.v1_trend_following.parameters.V1_CANDIDATE_UNIVERSE``.
_FUTURES_ASSET_CATEGORIES: Final[frozenset[str]] = frozenset({"FUT"})


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


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


async def run_eod_cycle(
    *,
    config: EodCycleConfig,
    session_factory: async_sessionmaker[Any],
    flex_client_factory: Callable[[], IbkrFlexQueryClient] | None = None,
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

    backend_view = await build_backend_view(session_factory, account_id=config.account_id)
    broker_view = build_broker_view(snapshot)

    plan = plan_reconciliation_check(
        backend_view=backend_view,
        broker_view=broker_view,
        prior_breaks=(),
        detected_at_utc=snapshot.pulled_at_utc,
    )

    result = await apply_reconciliation_plan(
        plan,
        session_factory=session_factory,
        account_id=config.account_id,
        env=config.env,
        phase_at_emit=config.phase_at_emit,
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
) -> CycleCallback:
    """Build the :class:`CycleCallback` the scheduler invokes per session day.

    The returned callable closes over ``config`` + ``session_factory``
    so the scheduler doesn't need to know about either. It signals to
    the scheduler by raising on terminal failure (which the scheduler
    catches + logs) or returning normally on success / soft-failure.
    """

    async def _cycle(session_date: date) -> None:
        del session_date  # unused; we don't anchor the cycle on calendar date today
        await run_eod_cycle(config=config, session_factory=session_factory)

    return _cycle


__all__ = [
    "CycleCallback",
    "EodCycleConfig",
    "build_backend_view",
    "build_broker_view",
    "make_cycle_callback",
    "run_eod_cycle",
]
