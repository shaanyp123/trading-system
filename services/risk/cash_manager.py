"""services/risk/cash_manager.py — Amendment B cash-yield worker (delta spec §3.6).

Daily cash-split manager, shipped **DORMANT** (``API_CASH_MANAGER_ENABLED``
defaults ``false``; activation is a C2 decision gated on delta-spec open
question #1 — same-day reclaim verified live). Target state, evaluated
once per UTC day (00:25 UTC — after the 00:15 recon and the 00:20 cash
capture):

    visible USD kept  =  initial margin
                       + 25% of gross notional (strategy Amendment B §6 buffer)
                       + operational headroom
    excess            →  USDC at CBI (Coinbase One rewards)
    shortfall         ←  reclaimed same-day (USDC→USD 1:1 conversion +
                         ``schedule_futures_sweep``)

**The USD/USDC equity-visibility boundary (decisions-log 2026-07-10 +
2026-07-18) is this module's central hazard and is modeled explicitly:**

* ``futures_balance_summary`` equity spans the WHOLE USD complex — the
  venue auto-sweeps between spot USD and CFM margin, so spot USD IS
  trading equity. A ``to_yield`` sweep (USD→USDC) therefore *genuinely
  shrinks* the equity the sizer and the risk loop read. That is the
  designed effect, and it is bounded: the planner NEVER sweeps visible
  equity below ``halt_floor + operational_headroom`` (the floor guard),
  so a sweep can never manufacture the Jul-17 zero-headroom shape. Under
  Amendment C the floor itself measures total capital (USD + the USDC
  capture), so the swept cash still counts toward the breaker — but the
  guard keeps the *visible* side padded regardless, because sizing and
  the loss limits remain USD-only by design.
* CBI USDC is INVISIBLE to equity. The planner reads it only to bound a
  reclaim (never sweep back more than exists) and treats a missing/stale
  capture as "unknown" → no reclaim is planned blind.

**Sweeps are INTERNAL TRANSFERS, not capital events.** External capital
in/out changes total capital; a sweep only moves it across the
visibility boundary. Concretely (the baseline-pollution guard):

* The worker NEVER calls ``plan_capital_event``/``apply_capital_event``,
  NEVER writes a ``capital_events`` row, and NEVER touches
  ``risk_state.capital_event_*`` — so the 5%-threshold half-size mode
  (``m_capital_event``) and the dd-baseline fields cannot be tripped by
  an internal transfer.
* The audit trail reuses the EXISTING capital-event *audit* taxonomy per
  the operator directive (no new enum values):
  ``CAPITAL_EVENT_WITHDRAWAL`` for ``to_yield`` (USD leaves the visible
  complex) and ``CAPITAL_EVENT_DEPOSIT`` for ``to_margin`` — with an
  unambiguous payload discriminator
  (``"kind": "cash_sweep_internal_transfer"``) so audit consumers can
  always separate sweeps from real capital events. The queryable ledger
  is the ``cash_sweeps`` table (created in the 2026-07-08 crypto-pivot
  migration for exactly this module; no schema change here).

Layering follows ``services/risk/capital_events.py``: pure planner
(:func:`plan_daily_sweep` — no I/O, no clock) → audit-first applier
(:func:`apply_cash_sweep`, §2.10.1) → a small daily job
(:class:`CashManagerJob`) wired through the generic once-per-UTC-day
scheduler, gated in the api lifespan behind the default-false setting.

Venue mechanics ([A27]): the conversion + sweep SDK calls are wrapped in
:class:`SdkCashSweepVenueClient` but are UNVERIFIED against the live
venue until C2 activation — the operator runbook
``deploy/cash_manager/README.md`` carries the fact-check checklist
(convert-quote contract, sweep timing, same-day reclaim = delta-spec
open question #1). Until then nothing constructs this client in
production (the lifespan starter early-returns on the disabled flag).

A02 BINDS — ``services/risk/**`` is on the forbidden whitelist;
``risk-review-approved`` required.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any, Literal, Protocol
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.audit.event_types import AuditEventType
from services.audit.writer import Environment, PhaseAtEmit, append_audit_event
from services.execution.types import BrokerPosition, FuturesBalanceSummary

log = structlog.get_logger()

#: Daily evaluation time — after the 00:15 recon + 00:20 cash capture.
DEFAULT_SWEEP_TIME_UTC = time(hour=0, minute=25)

#: Amendment B §6: margin buffer = 25% of gross notional.
DEFAULT_GROSS_BUFFER_FRAC = Decimal("0.25")

#: Visible-USD padding kept above BOTH the computed target and the halt
#: floor. Default matches the operator's current posture (~$750 ≈ 10
#: consecutive max per-trade losses at Phase-A sizing, decisions-log
#: 2026-07-18).
DEFAULT_OPERATIONAL_HEADROOM_USD = Decimal("750")

#: Churn guard: moves smaller than this are not worth a venue round trip.
DEFAULT_MIN_SWEEP_USD = Decimal("50")

#: Mirrors ``crypto_parameters.HALT_EQUITY_USD`` (locked $1,500).
DEFAULT_HALT_FLOOR_USD = Decimal("1500")

#: Reject USDC captures older than this when bounding a reclaim —
#: mirrors the Amendment C floor basis' 48 h freshness gate
#: (``strategy_worker.FLOOR_USDC_SNAPSHOT_MAX_AGE``). A stale capture
#: degrades to "unknown" ⇒ no blind reclaim.
USDC_CAPTURE_MAX_AGE = timedelta(hours=48)

_CENTS = Decimal("0.01")

SweepDirection = Literal["to_yield", "to_margin"]

#: Payload discriminator — audit consumers MUST use this to separate
#: internal sweeps from real capital events (same audit types reused).
SWEEP_AUDIT_KIND = "cash_sweep_internal_transfer"


def visible_usd_equity(summary: FuturesBalanceSummary) -> Decimal | None:
    """The venue USD complex the sizer/risk-loop read (fail-safe: None).

    Mirrors ``services.signal.strategy_worker.equity_from_summary``
    (total → CFM+CBI → CFM); duplicated here because importing the
    worker module from ``services.risk`` would invert the package
    dependency (the worker imports risk modules). Parity is pinned by
    ``tests/unit/test_cash_manager.py::test_visible_equity_parity``.
    """
    if summary.total_usd_balance is not None:
        return summary.total_usd_balance
    if summary.cfm_usd_balance is not None and summary.cbi_usd_balance is not None:
        return summary.cfm_usd_balance + summary.cbi_usd_balance
    return summary.cfm_usd_balance


def compute_gross_notional(
    positions: list[BrokerPosition],
    contract_sizes: dict[str, Decimal],
) -> Decimal | None:
    """Sum of |contracts| x contract_size x mark over venue positions.

    Fail-safe: any position missing its mark or contract size (or with a
    non-finite value) returns None — the planner then refuses to sweep
    rather than sizing the margin buffer on partial data. A flat book
    returns 0.
    """
    gross = Decimal(0)
    for position in positions:
        if position.contracts == 0:
            continue
        size = contract_sizes.get(position.product_id)
        mark = position.mark_price
        if size is None or mark is None:
            return None
        if not size.is_finite() or not mark.is_finite() or size <= 0 or mark <= 0:
            return None
        gross += abs(position.contracts) * size * mark
    return gross


@dataclass(frozen=True, slots=True)
class CashManagerParams:
    """Planner knobs (all Decimal, [A05])."""

    gross_buffer_frac: Decimal = DEFAULT_GROSS_BUFFER_FRAC
    operational_headroom_usd: Decimal = DEFAULT_OPERATIONAL_HEADROOM_USD
    min_sweep_usd: Decimal = DEFAULT_MIN_SWEEP_USD
    halt_floor_usd: Decimal = DEFAULT_HALT_FLOOR_USD


@dataclass(frozen=True, slots=True)
class CashSweepPlan:
    """One planned sweep + the full arithmetic that justified it."""

    direction: SweepDirection
    amount_usd: Decimal
    visible_equity_usd: Decimal
    cfm_target_usd: Decimal
    floor_guard_usd: Decimal
    initial_margin_usd: Decimal
    gross_notional_usd: Decimal
    cbi_usdc_usd: Decimal | None
    reason: str

    def audit_event_type(self) -> AuditEventType:
        """Existing capital-event audit taxonomy, payload-discriminated
        (operator directive: no new enum values). ``to_yield`` moves USD
        OUT of the visible complex → WITHDRAWAL; ``to_margin`` → DEPOSIT."""
        if self.direction == "to_yield":
            return AuditEventType.CAPITAL_EVENT_WITHDRAWAL
        return AuditEventType.CAPITAL_EVENT_DEPOSIT

    def audit_payload(self, *, now_utc: datetime) -> dict[str, Any]:
        return {
            "kind": SWEEP_AUDIT_KIND,
            "internal_transfer": True,
            "direction": self.direction,
            "amount_usd": str(self.amount_usd),
            "visible_equity_usd": str(self.visible_equity_usd),
            "cfm_target_usd": str(self.cfm_target_usd),
            "floor_guard_usd": str(self.floor_guard_usd),
            "initial_margin_usd": str(self.initial_margin_usd),
            "gross_notional_usd": str(self.gross_notional_usd),
            "cbi_usdc_usd": (str(self.cbi_usdc_usd) if self.cbi_usdc_usd is not None else None),
            "reason": self.reason,
            "not_a_capital_event": True,
            "ts": now_utc.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class CashSweepDecision:
    """Planner output: a plan, or an explicit named no-op."""

    plan: CashSweepPlan | None
    skip_reason: str | None = None


def plan_daily_sweep(
    *,
    summary: FuturesBalanceSummary,
    gross_notional_usd: Decimal | None,
    cbi_usdc_usd: Decimal | None,
    params: CashManagerParams,
) -> CashSweepDecision:
    """Pure daily sweep decision. Fail-safe throughout: ANY missing or
    malformed input yields a named no-op, never a guessed sweep.

    Rules:
      * target  = initial_margin + gross_buffer_frac x gross + headroom
      * guard   = halt_floor + headroom (visible equity NEVER swept below)
      * excess  = visible - max(target, guard) >= min_sweep → ``to_yield``
      * deficit = target - visible >= min_sweep → ``to_margin``, bounded
        by the known CBI-USDC balance (unknown balance ⇒ no blind reclaim)
    """
    visible = visible_usd_equity(summary)
    if visible is None or not visible.is_finite():
        return CashSweepDecision(plan=None, skip_reason="visible_equity_unavailable")
    if gross_notional_usd is None or not gross_notional_usd.is_finite():
        return CashSweepDecision(plan=None, skip_reason="gross_notional_unavailable")
    if gross_notional_usd < 0:
        return CashSweepDecision(plan=None, skip_reason="gross_notional_malformed")

    margin = summary.initial_margin
    if margin is None:
        if gross_notional_usd == 0:
            # Flat book: the venue omits margin fields; zero is truthful.
            margin = Decimal(0)
        else:
            return CashSweepDecision(plan=None, skip_reason="initial_margin_unavailable")
    if not margin.is_finite() or margin < 0:
        return CashSweepDecision(plan=None, skip_reason="initial_margin_malformed")

    usdc = cbi_usdc_usd
    if usdc is not None and (not usdc.is_finite() or usdc < 0):
        usdc = None  # malformed reads degrade to "unknown", never to 0 or garbage

    target = margin + (params.gross_buffer_frac * gross_notional_usd)
    target += params.operational_headroom_usd
    floor_guard = params.halt_floor_usd + params.operational_headroom_usd
    sweep_floor = max(target, floor_guard)

    excess = visible - sweep_floor
    if excess >= params.min_sweep_usd:
        # ROUND_DOWN: rounding UP by a sub-cent would put post-sweep
        # visible equity below the guard (risk-review finding 3).
        amount = excess.quantize(_CENTS, rounding=ROUND_DOWN)
        return CashSweepDecision(
            plan=CashSweepPlan(
                direction="to_yield",
                amount_usd=amount,
                visible_equity_usd=visible,
                cfm_target_usd=target,
                floor_guard_usd=floor_guard,
                initial_margin_usd=margin,
                gross_notional_usd=gross_notional_usd,
                cbi_usdc_usd=usdc,
                reason=(
                    f"visible {visible} exceeds max(target {target}, "
                    f"floor guard {floor_guard}) by {excess}"
                ),
            )
        )

    deficit = target - visible
    if deficit >= params.min_sweep_usd:
        if usdc is None:
            return CashSweepDecision(plan=None, skip_reason="reclaim_needed_usdc_unknown")
        # ROUND_DOWN: never over-ask the venue past the known USDC bound.
        amount = min(deficit, usdc).quantize(_CENTS, rounding=ROUND_DOWN)
        if amount < params.min_sweep_usd:
            return CashSweepDecision(plan=None, skip_reason="reclaim_needed_usdc_insufficient")
        return CashSweepDecision(
            plan=CashSweepPlan(
                direction="to_margin",
                amount_usd=amount,
                visible_equity_usd=visible,
                cfm_target_usd=target,
                floor_guard_usd=floor_guard,
                initial_margin_usd=margin,
                gross_notional_usd=gross_notional_usd,
                cbi_usdc_usd=usdc,
                reason=(
                    f"visible {visible} short of target {target} by {deficit}; "
                    f"reclaiming min(deficit, usdc {usdc})"
                ),
            )
        )

    return CashSweepDecision(plan=None, skip_reason="within_band")


# ---------------------------------------------------------------------------
# Venue client (conversion + futures sweep) — UNVERIFIED until C2
# ---------------------------------------------------------------------------


class CashSweepError(Exception):
    """Terminal venue/apply failure for one sweep leg."""

    def __init__(self, *, operation: str, detail: str) -> None:
        super().__init__(f"{operation}: {detail}")
        self.operation = operation
        self.detail = detail


class CashSweepVenueClient(Protocol):
    """The two venue legs a sweep can need. USDC↔USD is 1:1 instant
    conversion; ``schedule_futures_sweep`` pulls spot USD into CFM
    margin (delta spec §3.6)."""

    async def convert_cash(
        self, *, from_currency: str, to_currency: str, amount_usd: Decimal
    ) -> str: ...

    async def schedule_futures_sweep(self, *, amount_usd: Decimal) -> None: ...


class SdkCashSweepVenueClient:
    """coinbase-advanced-py-backed impl (convert quote → commit; sweep).

    Same lazy-import / ``to_thread`` / timeout conventions as
    ``SdkCoinbaseCashClient``. [A27]: the exact convert-quote and sweep
    response contracts are UNVERIFIED against the live venue until the
    C2 activation drill — see ``deploy/cash_manager/README.md``. No
    production code path constructs this class while the worker flag is
    false.
    """

    _DEFAULT_TIMEOUT_S = 30.0

    def __init__(
        self,
        *,
        api_key_name: str,
        api_private_key: str,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        self._api_key_name = api_key_name
        self._api_private_key = api_private_key
        self._timeout_s = timeout_s
        self._rest: Any = None
        self._log = log.bind(client="coinbase_cash_sweep")

    def _rest_client(self) -> Any:
        if self._rest is None:
            from coinbase.rest import RESTClient  # lazy: hosts without the SDK still import

            self._rest = RESTClient(
                api_key=self._api_key_name,
                api_secret=self._api_private_key,
                timeout=int(self._timeout_s),
            )
        return self._rest

    async def _call(self, operation: str, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(fn, *args, **kwargs), timeout=self._timeout_s
            )
        except TimeoutError as exc:
            raise CashSweepError(
                operation=operation, detail=f"venue call exceeded {self._timeout_s}s"
            ) from exc
        except Exception as exc:
            raise CashSweepError(operation=operation, detail=repr(exc)) from exc

    async def convert_cash(
        self, *, from_currency: str, to_currency: str, amount_usd: Decimal
    ) -> str:
        rest = self._rest_client()
        quote = await self._call(
            "create_convert_quote",
            rest.create_convert_quote,
            from_account=from_currency,
            to_account=to_currency,
            amount=str(amount_usd),
        )
        trade_id = getattr(quote, "trade", None)
        trade_id = getattr(trade_id, "id", None) if trade_id is not None else None
        if not isinstance(trade_id, str) or not trade_id:
            raise CashSweepError(
                operation="create_convert_quote", detail="no trade id in quote response"
            )
        await self._call(
            "commit_convert_trade",
            rest.commit_convert_trade,
            trade_id=trade_id,
            from_account=from_currency,
            to_account=to_currency,
        )
        return trade_id

    async def schedule_futures_sweep(self, *, amount_usd: Decimal) -> None:
        rest = self._rest_client()
        await self._call(
            "schedule_futures_sweep",
            rest.schedule_futures_sweep,
            usd_amount=str(amount_usd),
        )


# ---------------------------------------------------------------------------
# Applier — audit-first (§2.10.1), cash_sweeps ledger, never raises out
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CashSweepApplyResult:
    sweep_row_id: int | None
    audit_event_uuid: UUID | None
    status: Literal["completed", "failed", "audit_failed"]
    failure_detail: str | None = None


async def apply_cash_sweep(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    plan: CashSweepPlan,
    account_id: UUID,
    env: Environment,
    phase_at_emit: PhaseAtEmit,
    client: CashSweepVenueClient,
    now_utc: datetime,
) -> CashSweepApplyResult:
    """Audit event → ``cash_sweeps`` 'requested' row → venue legs →
    terminal row update. NEVER touches ``capital_events``/``risk_state``.

    Failure semantics: an audit failure aborts before any venue call
    (§2.10.1 — no unaudited money movement); a venue failure lands as a
    ``failed`` row with the detail preserved, and the next daily run
    re-plans from live balances (sweeps are idempotent at the target
    level, not the request level).
    """
    try:
        async with session_factory() as audit_session:
            record = await append_audit_event(
                audit_session,
                plan.audit_event_type(),
                plan.audit_payload(now_utc=now_utc),
                account_id=account_id,
                env=env,
                phase_at_emit=phase_at_emit,
            )
    except Exception as exc:
        log.error("cash_sweep_audit_write_failed", direction=plan.direction, error=repr(exc))
        return CashSweepApplyResult(
            sweep_row_id=None,
            audit_event_uuid=None,
            status="audit_failed",
            failure_detail=repr(exc),
        )

    async with session_factory() as session:
        async with session.begin():
            row = (
                await session.execute(
                    text(
                        "INSERT INTO cash_sweeps ("
                        "    requested_at_utc, direction, amount_usd, status,"
                        "    equity_usd_at_request, margin_reserve_usd_at_request,"
                        "    reason, audit_event_uuid"
                        ") VALUES ("
                        "    :requested_at, :direction, :amount, 'requested',"
                        "    :equity, :margin_reserve, :reason, :audit_uuid"
                        ") RETURNING id"
                    ),
                    {
                        "requested_at": now_utc,
                        "direction": plan.direction,
                        "amount": plan.amount_usd,
                        "equity": plan.visible_equity_usd,
                        "margin_reserve": plan.cfm_target_usd,
                        "reason": plan.reason,
                        "audit_uuid": record.event_uuid,
                    },
                )
            ).fetchone()
    if row is None:  # RETURNING on a successful INSERT — can't happen, but never assert-strip
        raise CashSweepError(operation="cash_sweeps_insert", detail="INSERT returned no row")
    sweep_row_id = int(row.id)

    failure_detail: str | None = None
    try:
        if plan.direction == "to_yield":
            await client.convert_cash(
                from_currency="USD", to_currency="USDC", amount_usd=plan.amount_usd
            )
        else:
            await client.convert_cash(
                from_currency="USDC", to_currency="USD", amount_usd=plan.amount_usd
            )
            await client.schedule_futures_sweep(amount_usd=plan.amount_usd)
    except Exception as exc:
        failure_detail = repr(exc)
        log.error(
            "cash_sweep_venue_leg_failed",
            direction=plan.direction,
            amount_usd=str(plan.amount_usd),
            error=failure_detail,
        )

    terminal: Literal["completed", "failed"] = "completed" if failure_detail is None else "failed"
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE cash_sweeps SET status = :status,"
                    "    completed_at_utc = :completed_at, failure_detail = :detail "
                    "WHERE id = :id"
                ),
                {
                    "status": terminal,
                    # Actual completion instant, not plan time (forensics).
                    "completed_at": (datetime.now(tz=UTC) if terminal == "completed" else None),
                    "detail": failure_detail,
                    "id": sweep_row_id,
                },
            )
    log.info(
        "cash_sweep_applied",
        direction=plan.direction,
        amount_usd=str(plan.amount_usd),
        status=terminal,
        sweep_row_id=sweep_row_id,
    )
    return CashSweepApplyResult(
        sweep_row_id=sweep_row_id,
        audit_event_uuid=record.event_uuid,
        status=terminal,
        failure_detail=failure_detail,
    )


# ---------------------------------------------------------------------------
# Daily job (scheduler callback) — inputs, plan, apply
# ---------------------------------------------------------------------------


class CashManagerBrokerReads(Protocol):
    """The two venue reads the job needs (satisfied by
    ``SdkCoinbaseBrokerClient``)."""

    async def get_futures_balance_summary(self) -> FuturesBalanceSummary: ...

    async def list_positions(self) -> list[BrokerPosition]: ...


@dataclass
class CashManagerJob:
    """Once-per-UTC-day sweep evaluation. ``run_once`` NEVER raises —
    a failed input read logs + skips the day (the book is never touched
    on partial data)."""

    broker: CashManagerBrokerReads
    sweep_client: CashSweepVenueClient
    session_factory: async_sessionmaker[AsyncSession]
    env: Environment
    phase_at_emit: PhaseAtEmit
    params: CashManagerParams = field(default_factory=CashManagerParams)

    async def _fetch_account_id(self) -> UUID | None:
        async with self.session_factory() as session:
            row = (
                await session.execute(
                    text("SELECT id FROM accounts WHERE active IS TRUE ORDER BY created_at LIMIT 1")
                )
            ).fetchone()
        return row.id if row is not None else None

    async def _fetch_contract_sizes(self) -> dict[str, Decimal]:
        """Latest ``product_metadata`` contract size per product."""
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT DISTINCT ON (product_id) product_id, contract_size "
                        "FROM product_metadata "
                        "ORDER BY product_id, captured_at_utc DESC"
                    )
                )
            ).fetchall()
        return {
            str(r.product_id): Decimal(str(r.contract_size))
            for r in rows
            if r.contract_size is not None
        }

    async def _fetch_cbi_usdc(self, *, now_utc: datetime) -> Decimal | None:
        """Latest daily CBI-USDC capture (same surface as the Amendment C
        floor basis; the 00:20 job writes it minutes before this runs).
        Captures older than 48 h degrade to unknown — same freshness
        posture as the floor basis (risk-review finding 4)."""
        async with self.session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT captured_at_utc, cbi_usdc FROM cash_balance_snapshots "
                        "ORDER BY snapshot_date_utc DESC LIMIT 1"
                    )
                )
            ).fetchone()
        if row is None or row.cbi_usdc is None or row.captured_at_utc is None:
            return None
        if now_utc - row.captured_at_utc > USDC_CAPTURE_MAX_AGE:
            log.warning("cash_manager_usdc_capture_stale")
            return None
        return Decimal(str(row.cbi_usdc))

    async def run_once(self, session_date_utc: date) -> None:
        log_bound = log.bind(job="cash_manager", session_date=session_date_utc.isoformat())
        try:
            account_id = await self._fetch_account_id()
            if account_id is None:
                log_bound.warning("cash_manager_no_active_account")
                return
            summary = await self.broker.get_futures_balance_summary()
            positions = await self.broker.list_positions()
            contract_sizes = await self._fetch_contract_sizes()
            gross = compute_gross_notional(positions, contract_sizes)
            cbi_usdc = await self._fetch_cbi_usdc(now_utc=datetime.now(tz=UTC))
        except Exception as exc:
            log_bound.error("cash_manager_inputs_failed", error=repr(exc))
            return

        decision = plan_daily_sweep(
            summary=summary,
            gross_notional_usd=gross,
            cbi_usdc_usd=cbi_usdc,
            params=self.params,
        )
        if decision.plan is None:
            log_bound.info("cash_manager_no_sweep", skip_reason=decision.skip_reason)
            return
        try:
            result = await apply_cash_sweep(
                self.session_factory,
                plan=decision.plan,
                account_id=account_id,
                env=self.env,
                phase_at_emit=self.phase_at_emit,
                client=self.sweep_client,
                now_utc=datetime.now(tz=UTC),
            )
        except Exception as exc:
            log_bound.error("cash_manager_apply_failed", error=repr(exc))
            return
        log_bound.info(
            "cash_manager_sweep_result",
            direction=decision.plan.direction,
            amount_usd=str(decision.plan.amount_usd),
            status=result.status,
        )


def make_sweep_callback(job: CashManagerJob) -> Any:
    """Adapt the job to the generic daily scheduler's ``CycleCallback``."""

    async def _callback(session_date_utc: date) -> None:
        await job.run_once(session_date_utc)

    return _callback


__all__ = [
    "DEFAULT_GROSS_BUFFER_FRAC",
    "DEFAULT_HALT_FLOOR_USD",
    "DEFAULT_MIN_SWEEP_USD",
    "DEFAULT_OPERATIONAL_HEADROOM_USD",
    "DEFAULT_SWEEP_TIME_UTC",
    "SWEEP_AUDIT_KIND",
    "USDC_CAPTURE_MAX_AGE",
    "CashManagerBrokerReads",
    "CashManagerJob",
    "CashManagerParams",
    "CashSweepApplyResult",
    "CashSweepDecision",
    "CashSweepError",
    "CashSweepPlan",
    "CashSweepVenueClient",
    "SdkCashSweepVenueClient",
    "apply_cash_sweep",
    "compute_gross_notional",
    "make_sweep_callback",
    "plan_daily_sweep",
    "visible_usd_equity",
]
