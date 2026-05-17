"""services/risk/order_placement_worker.py — approved-signal → IBKR order pipeline.

Worker-PR-1 (post-pivot 2026-05-12). Background async loop that polls the
``signals`` table for ``status='approved'`` rows without a matching
``orders`` row, builds an IBKR placeOrder request via the pure-policy
planner, dispatches via :class:`services.execution.ibkr_client.IbkrClient`,
writes an ``order_placed`` audit row, INSERTs the orders row, and updates
the signals row's ``status`` to ``'working'``.

**Bracket-order extension (2026-05-17).** When the planner can derive a
stop price from the signal's ``sizing_trace`` (the V1 strategy populates
``stage_0_universe.strategy_inputs[market].stop_price``), the worker
places BOTH the entry order AND a stop-market exit order in the same
apply step. Two audit ``ORDER_PLACED`` rows + two ``orders`` rows + one
``signals.status='working'`` UPDATE land atomically. The stop order
reuses the entry's ``signal_id`` per the bracket-order Option B contract
documented in :mod:`services.risk.fill_processor` — when the stop fires,
``fetch_fill_context`` resolves to the entry signal's direction +
the EXIT_FULL_CLOSE branch handles the close.

If stop placement fails after a successful entry placement, the worker
attempts to cancel the entry via ``ibkr_client.cancel_order`` and raises
:class:`OrderPlacementError`. **The cancel is best-effort** — for ETFs
which paper-fill immediately, the entry may have already filled before
the cancel reaches IBKR, leaving a naked position the operator must
manually flatten via TWS + audit-event manual entry.

**Audit-first ordering per backend-spec §2.10.1.** The placeOrder call to
the broker happens BEFORE the audit write — IBKR's broker_order_id is
load-bearing for the audit payload, and the broker call is the
non-idempotent side-effect that must succeed before we record it. If the
broker call succeeds but the audit write fails, the order is in flight
on IBKR and the operator must reconcile via the IBKR portal + manually
write the audit row through the agent-actions surface. This is the
classical "tail-risk" path documented at backend-spec §6.3.

**Phase 1 cadence**: 5-second poll interval. Signal approve cadence is
~once per day (operator-driven via /signals page); polling tighter
than that catches manual-approve flows quickly without unnecessary DB
pressure. Tunable via the ``poll_interval_seconds`` constructor arg.

**Client order ID format** (backend-spec §2.5 LOCKED):
``<strategy_short>-<paramset_short>-<signal_short>-<retry_n>`` for the
entry; the stop order appends a ``-stop`` suffix:
``<strategy_short>-<paramset_short>-<signal_short>-<retry_n>-stop``.
Phase 1 retry_n=0.

**Anti-pattern enforcement:**

* A01: audit writes via :func:`services.audit.writer.append_audit_event`
  only — never raw INSERTs into audit_log.
* A02: ``services/risk/**`` is on the forbidden-modification whitelist
  per dev-guide §11; this PR carries the ``risk-review-approved`` label.
* A05: all monetary values pass through :class:`decimal.Decimal` at the
  module boundary. The pure-policy planner is float-free.
* A06: all timestamps are tz-aware UTC; tests enforce.

**NOT in Worker-PR-1 scope** (deferred to Worker-PR-3 + Phase 5 ceremony):

* Order-fill subscription / order_filled audit events (the IBKR
  ``orderStatus`` event stream wiring lives in the reconciliation
  scheduler — Worker-PR-3 — not here).
* Retry-on-rejection with exponential backoff. Worker-PR-1 surfaces
  the rejection_category in the orders row + writes order_rejected
  audit; retry strategy belongs to a future iteration.
* SSE invalidation of the web /signals page. The signals.status flip
  to 'working' is observable via polling for now; SSE wiring lands
  with the broader event-push infrastructure (Worker-PR-2).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, Literal
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from services.audit.event_types import AuditEventType
from services.audit.writer import Environment, PhaseAtEmit, append_audit_event
from services.execution.ibkr_client import IbkrClient
from services.execution.types import (
    IbkrContractRef,
    IbkrOrderSide,
    IbkrOrderType,
    IbkrPlacementError,
    IbkrPlaceOrderRequest,
    OrderStatusUpdate,
)

log = structlog.get_logger()

#: Phase 1 poll cadence. Operator approves ~once per day; 5s catches the
#: manual-approve flow without taxing the DB.
DEFAULT_POLL_INTERVAL_SECONDS: Final[float] = 5.0

#: Default hard timeout for every IBKR adapter call this worker makes
#: (``resolve_contract``, ``place_order`` entry + stop, ``cancel_order``,
#: ``subscribe_order_status``). Added 2026-05-17 after three production
#: drills surfaced a silent-worker pattern: IBKR's paper API can leave
#: ``ib-async`` awaits hung indefinitely (no internal timeout fires) when
#: the broker side is sluggish or unresponsive. Without an outer
#: ``asyncio.wait_for`` the worker task never raises, never logs, and
#: never re-polls — the entire pipeline goes silent.
#:
#: 30s is a deliberate trade-off: long enough for a healthy IBKR round-
#: trip even under transient latency, short enough that the worker
#: recovers within one poll cycle's worth of patience. Tunable via
#: ``API_IBKR_CALL_TIMEOUT_SECONDS``.
DEFAULT_IBKR_CALL_TIMEOUT_SECONDS: Final[float] = 30.0

#: Phase 1 retry count for the client_order_id suffix. retry_n=0 always
#: in Worker-PR-1. Retry semantics land in a future iteration.
RETRY_N_PHASE_1: Final[int] = 0

#: Maps signals.direction (long/short/flat) → IBKR order side (buy/sell).
#: 'flat' direction means close-position; Phase 1 doesn't emit flat
#: signals from the algorithm but the mapping is here for completeness.
_DIRECTION_TO_SIDE: Final[dict[str, IbkrOrderSide]] = {
    "long": "buy",
    "short": "sell",
}


@dataclass(frozen=True, slots=True)
class ApprovedSignalRow:
    """Snapshot of an ``signals`` row in ``status='approved'`` state.

    The worker SELECTs this shape on every poll cycle. Mirrors the
    columns we need for downstream order construction; deliberately
    NOT a full ORM model to keep the worker decoupled from sqlalchemy
    declarative.
    """

    signal_id: UUID
    account_id: UUID
    env: str
    market: str
    direction: Literal["long", "short", "flat"]
    target_contracts: int
    decision_price: Decimal
    strategy_hash: str
    parameter_set_hash: str
    sizing_trace: dict[str, Any]
    """Raw ``signals.sizing_trace`` JSONB column as a Python dict. Used to
    derive the bracket stop_price via
    ``sizing_trace["stage_0_universe"]["strategy_inputs"][market]["stop_price"]``
    (V1 strategy's canonical position). Bracket-order extension 2026-05-17."""


@dataclass(frozen=True, slots=True)
class OrderPlacementPlan:
    """Pure-policy plan for placing one approved signal as an IBKR order.

    Built by :func:`plan_order_placement` from an :class:`ApprovedSignalRow`.
    The orchestrator :func:`apply_order_placement` consumes this + an
    :class:`IbkrClient` instance to do the actual placement.

    All ID derivation is deterministic — same signal row → same
    client_order_id always (necessary for retry idempotency at the
    broker boundary).

    Bracket-order extension (2026-05-17): when the signal's sizing_trace
    contains ``stage_0_universe.strategy_inputs[market].stop_price`` the
    planner also populates ``stop_client_order_id`` + ``stop_price`` +
    ``stop_side`` so the apply step places BOTH the entry order and
    the protective stop-market exit order. When stop info is missing
    the planner raises (no entry without a stop, per V1 design).
    """

    signal_id: UUID
    account_id: UUID
    env: Environment
    market: str
    side: IbkrOrderSide
    quantity: Decimal
    order_type: IbkrOrderType
    limit_price: Decimal
    time_in_force: Literal["DAY", "GTC"]
    client_order_id: str
    strategy_hash: str
    parameter_set_hash: str
    decision_price: Decimal
    # ----- Bracket stop-loss fields -----
    stop_client_order_id: str
    """Stop's client_order_id; entry's CID + ``-stop`` suffix."""
    stop_price: Decimal
    """Stop price computed from the strategy's pre-computed
    sizing_trace.stop_price (V1: ATR-based exit stop). For longs,
    stop_price < limit_price; for shorts, stop_price > limit_price.
    Validated in plan_order_placement."""
    stop_side: IbkrOrderSide
    """Opposite of entry side — sell for long entries (close-long via
    sell-stop), buy for short entries (close-short via buy-stop)."""


@dataclass(frozen=True, slots=True)
class OrderPlacementResult:
    """Outcome of a successful :func:`apply_order_placement` call.

    On rejection by IBKR the orchestrator still returns this — the
    ``status`` field reflects the broker's response (``rejected``,
    ``pending_submit``, etc.) and the caller decides how to react.
    """

    signal_id: UUID
    order_id: UUID
    audit_event_uuid: UUID
    broker_order_id: str | None
    status: str


class OrderPlacementError(Exception):
    """Raised for problems the worker can't recover from.

    Distinct from :class:`services.execution.types.IbkrPlacementError`
    (which is a broker-side error). This is for our-side issues:
    invalid signal row shape, audit write failure, etc.
    """


def _build_client_order_id(
    *,
    strategy_hash: str,
    parameter_set_hash: str,
    signal_id: UUID,
    retry_n: int,
) -> str:
    """Build the 33-char client_order_id per backend-spec §2.5 LOCKED.

    Format: ``<strat8>-<param8>-<sig8>-<retry_n>``. retry_n is 0-9 in
    Phase 1 (single digit, no padding). The hashes are pre-validated
    by the signal_emitted handler: strategy_hash is 40-char hex,
    parameter_set_hash is 64-char hex. signal_id is a UUID; we take
    the first 8 chars of its hex form (without dashes).
    """
    if len(strategy_hash) < 8:
        raise OrderPlacementError(f"strategy_hash {strategy_hash!r} too short; need ≥ 8 chars")
    if len(parameter_set_hash) < 8:
        raise OrderPlacementError(
            f"parameter_set_hash {parameter_set_hash!r} too short; need ≥ 8 chars"
        )
    if retry_n < 0 or retry_n > 9:
        raise OrderPlacementError(f"retry_n must be 0..9 for Phase 1; got {retry_n}")
    signal_hex = signal_id.hex
    return f"{strategy_hash[:8]}-{parameter_set_hash[:8]}-{signal_hex[:8]}-{retry_n}"


def _extract_stop_price_from_sizing_trace(
    sizing_trace: dict[str, Any],
    *,
    market: str,
    direction: Literal["long", "short", "flat"],
    decision_price: Decimal,
) -> Decimal:
    """Pull ``stop_price`` from the V1 sizing_trace canonical position.

    Lookup path:
    ``sizing_trace["stage_0_universe"]["strategy_inputs"][market]["stop_price"]``

    The V1 strategy populates this in
    :meth:`lean.v1_strategy.V1TrendFollowingAlgorithm._build_minimal_sizing_trace`
    using ``decision_price ± ATR x STOP_DISTANCE_ATR_MULT``. Phase 2+
    strategies that don't follow this convention need their own
    extension to this function (or a strategy-keyed dispatch).

    Sanity-check side direction: for ``direction='long'`` the stop must
    be STRICTLY BELOW the decision price; for ``direction='short'`` the
    stop must be STRICTLY ABOVE. Violations raise
    :class:`OrderPlacementError` — better to refuse the entry than
    submit a self-defeating stop (a sell-stop above the long entry would
    fire immediately).

    Raises :class:`OrderPlacementError` for any of: missing nested key,
    unparseable value (negative, zero, non-Decimal-castable), direction
    inversion.
    """
    try:
        stage_0 = sizing_trace["stage_0_universe"]
        strategy_inputs = stage_0["strategy_inputs"]
        market_inputs = strategy_inputs[market]
        raw_stop = market_inputs["stop_price"]
    except (KeyError, TypeError) as exc:
        raise OrderPlacementError(
            f"sizing_trace missing canonical stop_price path "
            f"(stage_0_universe.strategy_inputs.{market}.stop_price): {exc!r}"
        ) from exc
    if raw_stop is None or raw_stop == "":
        raise OrderPlacementError(
            f"sizing_trace stop_price for market {market!r} is empty/None; "
            "V1 strategy must populate decision_price ± ATR x STOP_DISTANCE_ATR_MULT."
        )
    try:
        # Strategy persists Decimal as string per A05; tolerate Decimal too.
        stop_price = Decimal(str(raw_stop))
    except (ArithmeticError, ValueError) as exc:
        raise OrderPlacementError(
            f"sizing_trace stop_price {raw_stop!r} not Decimal-castable: {exc!r}"
        ) from exc
    if stop_price <= 0:
        raise OrderPlacementError(f"sizing_trace stop_price={stop_price} must be > 0.")
    # Direction sanity check. Defensive — a self-defeating stop fires
    # immediately, draining the operator's account via slippage.
    if direction == "long" and stop_price >= decision_price:
        raise OrderPlacementError(
            f"Long entry decision_price={decision_price} but stop_price={stop_price} "
            "is NOT strictly below; rejecting to avoid an immediate self-defeating fill."
        )
    if direction == "short" and stop_price <= decision_price:
        raise OrderPlacementError(
            f"Short entry decision_price={decision_price} but stop_price={stop_price} "
            "is NOT strictly above; rejecting to avoid an immediate self-defeating fill."
        )
    return stop_price


def plan_order_placement(signal: ApprovedSignalRow, *, env: Environment) -> OrderPlacementPlan:
    """Build the placement plan for one approved signal.

    Pure policy — no I/O, no clock, no random. Same inputs always
    produce the same plan, which means client_order_id is
    deterministic per signal (necessary for IBKR retry idempotency).

    Bracket-order extension (2026-05-17): derives stop_price from
    ``signal.sizing_trace`` and includes it in the plan. The apply step
    places both entry + stop in one operation.

    Raises :class:`OrderPlacementError` for shape problems that should
    have been caught upstream — these are runtime sanity checks.
    """
    side = _DIRECTION_TO_SIDE.get(signal.direction)
    if side is None:
        raise OrderPlacementError(
            f"signal.direction={signal.direction!r} not placeable; expected long or short"
        )
    if signal.target_contracts <= 0:
        raise OrderPlacementError(
            f"signal.target_contracts={signal.target_contracts}; must be > 0 for placement"
        )

    client_order_id = _build_client_order_id(
        strategy_hash=signal.strategy_hash,
        parameter_set_hash=signal.parameter_set_hash,
        signal_id=signal.signal_id,
        retry_n=RETRY_N_PHASE_1,
    )

    # Bracket extension: derive stop fields from the strategy's
    # canonical position in sizing_trace.
    stop_price = _extract_stop_price_from_sizing_trace(
        signal.sizing_trace,
        market=signal.market,
        direction=signal.direction,
        decision_price=signal.decision_price,
    )
    # Opposite-side stop for the bracket.
    stop_side: IbkrOrderSide = "sell" if side == "buy" else "buy"
    stop_client_order_id = f"{client_order_id}-stop"

    return OrderPlacementPlan(
        signal_id=signal.signal_id,
        account_id=signal.account_id,
        env=env,
        market=signal.market,
        side=side,
        quantity=Decimal(signal.target_contracts),
        order_type="limit_marketable",
        limit_price=signal.decision_price,
        time_in_force="DAY",
        client_order_id=client_order_id,
        strategy_hash=signal.strategy_hash,
        parameter_set_hash=signal.parameter_set_hash,
        decision_price=signal.decision_price,
        stop_client_order_id=stop_client_order_id,
        stop_price=stop_price,
        stop_side=stop_side,
    )


async def fetch_approved_signals(
    session_factory: async_sessionmaker[Any],
    *,
    account_id: UUID,
    env: str,
    limit: int = 32,
) -> list[ApprovedSignalRow]:
    """SELECT approved signals that don't yet have an orders row.

    Phase 1 LIMIT 32 — generous bound; the operator approves ~one signal
    per day so 32 is far more than realistic. Prevents a runaway loop
    if the join breaks for some reason.

    Uses a LEFT JOIN to find signals.id NOT IN (SELECT signal_id FROM
    orders) without the costly NOT IN sub-select.
    """
    async with session_factory() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT
                      s.id AS signal_id,
                      s.account_id,
                      s.env,
                      s.market,
                      s.direction,
                      s.target_contracts,
                      s.decision_price,
                      s.strategy_hash,
                      s.parameter_set_hash,
                      s.sizing_trace
                    FROM signals s
                    LEFT JOIN orders o ON o.signal_id = s.id
                    WHERE s.account_id = :acct
                      AND s.env = :env
                      AND s.status = 'approved'
                      AND o.id IS NULL
                    ORDER BY s.emitted_at_utc ASC
                    LIMIT :limit
                    """
                ),
                {"acct": account_id, "env": env, "limit": limit},
            )
        ).fetchall()
    return [
        ApprovedSignalRow(
            signal_id=r.signal_id,
            account_id=r.account_id,
            env=r.env,
            market=r.market,
            direction=r.direction,
            target_contracts=r.target_contracts,
            decision_price=r.decision_price,
            strategy_hash=r.strategy_hash,
            parameter_set_hash=r.parameter_set_hash,
            # asyncpg returns JSONB as a Python dict already (no json.loads needed).
            sizing_trace=dict(r.sizing_trace) if r.sizing_trace is not None else {},
        )
        for r in rows
    ]


async def _await_ibkr_with_timeout(
    coro: Any,
    *,
    operation: str,
    timeout_seconds: float,
) -> Any:
    """Wrap an IBKR adapter ``await`` with a hard timeout.

    The IBKR TWS API (and the ``ib-async`` adapter layered over it) does
    not always honor its own internal timeouts when the broker side
    becomes sluggish or unresponsive. Concretely: ``qualifyContractsAsync``,
    ``placeOrder``, and ``reqExecutions`` can each hang indefinitely with
    no ``asyncio.TimeoutError`` being raised. When that happens the
    worker's ``run_once`` await-blocks forever — no exception, no log
    line, and the asyncio task is still ``not .done()`` so the
    :class:`services.api.async_task_monitor.AsyncTaskMonitor` doesn't
    flag it as dead. This is the silent-worker pattern observed in the
    2026-05-17 drill 2 ceremony.

    This helper enforces a hard wall-clock deadline. On expiry:

    * The underlying coroutine is cancelled (asyncio.wait_for contract).
    * :class:`services.execution.types.IbkrPlacementError` is raised
      with ``operation`` + a structured ``"timed out after Xs"`` detail.
      The existing ``except IbkrPlacementError`` blocks in the worker's
      ``run_once`` / ``apply_order_placement`` then take their normal
      "broker unavailable" recovery path — log, fail this iteration,
      continue polling.

    Defense in depth alongside ``AsyncTaskMonitor``: that monitor catches
    tasks that have already died; this helper prevents the tasks from
    ever silently hanging in the first place.
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout_seconds)
    except TimeoutError as exc:
        raise IbkrPlacementError(
            operation=operation,
            detail=f"{operation} timed out after {timeout_seconds}s",
            underlying_exception_class="TimeoutError",
            occurred_at_utc=datetime.now(tz=UTC),
        ) from exc


async def apply_order_placement(
    plan: OrderPlacementPlan,
    *,
    ibkr_client: IbkrClient,
    contract: IbkrContractRef,
    session_factory: async_sessionmaker[Any],
    phase_at_emit: PhaseAtEmit = 1,
    ibkr_call_timeout_seconds: float = DEFAULT_IBKR_CALL_TIMEOUT_SECONDS,
) -> OrderPlacementResult:
    """Execute the placement plan: IBKR placeOrder (entry + stop) → audit → INSERT orders.

    Sequential steps:

    1. ``ibkr_client.place_order(entry_request)`` — the broker-side
       side effect for the entry leg. Failure raises
       :class:`IbkrPlacementError` (broker connectivity) or returns an
       ``IbkrPlaceOrderResult`` with ``status='rejected'`` (broker
       validation rejection). The await is bounded by
       ``ibkr_call_timeout_seconds`` (default 30s); expiry raises
       :class:`IbkrPlacementError` so the existing except path handles
       it cleanly.
    2. **Bracket extension (2026-05-17):** if entry placed successfully
       (status not in rejected set), build + place the stop-market exit
       order via ``ibkr_client.place_order(stop_request)``. On stop
       failure → best-effort cancel the entry + raise
       :class:`OrderPlacementError("STOP_PLACEMENT_FAILED")`. The
       cancel itself is also wrapped in the timeout so a hanging cancel
       doesn't trap the worker after the original stop failure.
    3. Audit-first (per spec §2.10.1): write ``ORDER_PLACED`` x N
       (1 if entry rejected, 2 if entry + stop both placed) — each in
       its own SERIALIZABLE transaction. Payload includes the
       client_order_id + broker_order_id.
    4. INSERT ``orders`` x N + UPDATE ``signals.status`` in one
       transaction. The stop's ``parent_order_id`` references the
       entry's id (set via RETURNING from the first INSERT).

    Returns an :class:`OrderPlacementResult` reflecting the ENTRY
    order's status (the stop's status is logged + audited but not
    surfaced in the worker's return value — Phase 2+ may add a
    bracket-aware result wrapper).
    """
    placed_at = datetime.now(tz=UTC)

    # Step 1: IBKR side effect — entry order.
    entry_request = IbkrPlaceOrderRequest(
        client_order_id=plan.client_order_id,
        contract=contract,
        side=plan.side,
        quantity=plan.quantity,
        order_type=plan.order_type,
        limit_price=plan.limit_price,
        time_in_force=plan.time_in_force,
    )
    try:
        broker_result = await _await_ibkr_with_timeout(
            ibkr_client.place_order(entry_request),
            operation="placeOrder.entry",
            timeout_seconds=ibkr_call_timeout_seconds,
        )
    except IbkrPlacementError as exc:
        log.error(
            "order_placement_broker_error",
            signal_id=str(plan.signal_id),
            client_order_id=plan.client_order_id,
            error=str(exc),
        )
        raise

    broker_order_id = broker_result.broker_order_id
    broker_status = broker_result.status
    rejection_category = getattr(broker_result, "rejection_category", None)
    rejection_detail = getattr(broker_result, "rejection_detail", None)
    db_status = _broker_status_to_orders_status(broker_status)

    # Step 2: IBKR side effect — stop-market exit order (bracket).
    # Skip the stop placement if the entry was rejected at the broker;
    # there's no position to protect.
    place_stop = db_status != "rejected"
    stop_broker_result = None
    stop_db_status: str | None = None
    if place_stop:
        stop_request = IbkrPlaceOrderRequest(
            client_order_id=plan.stop_client_order_id,
            contract=contract,
            side=plan.stop_side,
            quantity=plan.quantity,
            order_type="stop_market",
            stop_price=plan.stop_price,
            time_in_force="GTC",
            parent_client_order_id=plan.client_order_id,
        )
        try:
            stop_broker_result = await _await_ibkr_with_timeout(
                ibkr_client.place_order(stop_request),
                operation="placeOrder.stop",
                timeout_seconds=ibkr_call_timeout_seconds,
            )
            stop_db_status = _broker_status_to_orders_status(stop_broker_result.status)
            if stop_db_status == "rejected":
                # Stop rejected; treat as failure of the bracket pair.
                raise OrderPlacementError(
                    f"Stop placement rejected by broker: {stop_broker_result.rejection_detail!r}"
                )
        except (IbkrPlacementError, OrderPlacementError) as stop_exc:
            log.error(
                "order_placement_stop_failed",
                signal_id=str(plan.signal_id),
                entry_client_order_id=plan.client_order_id,
                stop_client_order_id=plan.stop_client_order_id,
                entry_broker_order_id=str(broker_order_id),
                error=str(stop_exc),
            )
            # Best-effort cancel of the entry. For ETFs that paper-fill
            # immediately the cancel may race the fill and lose; the
            # operator must reconcile via TWS + manual audit entry.
            # The cancel itself is bounded by ibkr_call_timeout_seconds
            # so a hung cancel doesn't trap the worker after the
            # original stop failure already raised.
            try:
                await _await_ibkr_with_timeout(
                    ibkr_client.cancel_order(plan.client_order_id),
                    operation="cancelOrder.entry_after_stop_failure",
                    timeout_seconds=ibkr_call_timeout_seconds,
                )
                log.warning(
                    "order_placement_entry_cancel_submitted_after_stop_failure",
                    entry_client_order_id=plan.client_order_id,
                    entry_broker_order_id=str(broker_order_id),
                )
            except Exception:
                log.exception(
                    "order_placement_entry_cancel_failed_after_stop_failure",
                    entry_client_order_id=plan.client_order_id,
                    entry_broker_order_id=str(broker_order_id),
                )
            raise OrderPlacementError(
                f"STOP_PLACEMENT_FAILED entry_cid={plan.client_order_id} "
                f"stop_cid={plan.stop_client_order_id}: {stop_exc!r}"
            ) from stop_exc

    # Step 3: audit (audit-first per spec §2.10.1; the broker side effect
    # already happened so we record what actually was done).
    entry_audit_payload: dict[str, Any] = {
        "signal_id": str(plan.signal_id),
        "account_id": str(plan.account_id),
        "client_order_id": plan.client_order_id,
        "broker_order_id": str(broker_order_id) if broker_order_id is not None else None,
        "market": plan.market,
        "side": plan.side,
        "quantity": str(plan.quantity),
        "order_type": plan.order_type,
        "limit_price": str(plan.limit_price),
        "time_in_force": plan.time_in_force,
        "broker_status": broker_status,
        "rejection_category": rejection_category,
        "rejection_detail": rejection_detail,
        "placed_at_utc": placed_at.isoformat(),
        "leg": "entry",
    }
    # Pre-append payload observability: log the audit payload key set so
    # future invocations of the silent-"leg field missing" defect (2026-05-17
    # drill 1, audit row seq=45 written WITHOUT the leg field even though
    # the deployed code unambiguously sets it here) leave a breadcrumb
    # showing exactly what was passed in to append_audit_event. The
    # mystery there is unresolved; this log is the diagnostic for the
    # next occurrence. Logging keys-only (not the full payload) keeps
    # the log line small + avoids leaking signal-specific data.
    log.info(
        "order_placement_audit_payload_pre_append",
        signal_id=str(plan.signal_id),
        leg="entry",
        payload_keys=sorted(entry_audit_payload.keys()),
        payload_key_count=len(entry_audit_payload),
        has_leg_field="leg" in entry_audit_payload,
    )
    async with session_factory() as audit_session:
        audit_record = await append_audit_event(
            audit_session,
            AuditEventType.ORDER_PLACED,
            entry_audit_payload,
            account_id=plan.account_id,
            env=plan.env,
            phase_at_emit=phase_at_emit,
            source_clock_ts=placed_at,
        )

    stop_audit_record = None
    if stop_broker_result is not None:
        stop_audit_payload: dict[str, Any] = {
            "signal_id": str(plan.signal_id),
            "account_id": str(plan.account_id),
            "client_order_id": plan.stop_client_order_id,
            "broker_order_id": (
                str(stop_broker_result.broker_order_id)
                if stop_broker_result.broker_order_id is not None
                else None
            ),
            "market": plan.market,
            "side": plan.stop_side,
            "quantity": str(plan.quantity),
            "order_type": "stop_market",
            "stop_price": str(plan.stop_price),
            "time_in_force": "GTC",
            "broker_status": stop_broker_result.status,
            "rejection_category": getattr(stop_broker_result, "rejection_category", None),
            "rejection_detail": getattr(stop_broker_result, "rejection_detail", None),
            "placed_at_utc": placed_at.isoformat(),
            "leg": "stop",
            "parent_client_order_id": plan.client_order_id,
        }
        # Pre-append payload observability — same diagnostic as the
        # entry leg above. Logs the stop leg's payload keys so the
        # "missing leg field" mystery from drill 1 has a breadcrumb
        # on every future occurrence.
        log.info(
            "order_placement_audit_payload_pre_append",
            signal_id=str(plan.signal_id),
            leg="stop",
            payload_keys=sorted(stop_audit_payload.keys()),
            payload_key_count=len(stop_audit_payload),
            has_leg_field="leg" in stop_audit_payload,
        )
        async with session_factory() as stop_audit_session:
            stop_audit_record = await append_audit_event(
                stop_audit_session,
                AuditEventType.ORDER_PLACED,
                stop_audit_payload,
                account_id=plan.account_id,
                env=plan.env,
                phase_at_emit=phase_at_emit,
                source_clock_ts=placed_at,
            )

    # Step 4: INSERT orders x {1, 2} + UPDATE signal. Single transaction
    # so the business-data view is consistent.
    async with session_factory() as session:
        async with session.begin():
            entry_order_row = (
                await session.execute(
                    text(
                        """
                        INSERT INTO orders (
                            account_id, env, signal_id, client_order_id, broker_order_id,
                            market, direction, order_type, quantity, limit_price,
                            placed_at_utc, status, rejection_reason, retry_n,
                            strategy_hash, parameter_set_hash
                        ) VALUES (
                            :acct, :env, :sig, :cid, :bid,
                            :market, :side, :order_type, :qty, :lim,
                            :placed_at, :status, :rej_reason, :retry_n,
                            :strat, :param
                        )
                        RETURNING id
                        """
                    ),
                    {
                        "acct": plan.account_id,
                        "env": plan.env,
                        "sig": plan.signal_id,
                        "cid": plan.client_order_id,
                        "bid": str(broker_order_id) if broker_order_id is not None else None,
                        "market": plan.market,
                        "side": plan.side,
                        "order_type": plan.order_type,
                        "qty": int(plan.quantity),
                        "lim": plan.limit_price,
                        "placed_at": placed_at,
                        "status": db_status,
                        "rej_reason": rejection_detail,
                        "retry_n": RETRY_N_PHASE_1,
                        "strat": plan.strategy_hash,
                        "param": plan.parameter_set_hash,
                    },
                )
            ).fetchone()
            assert entry_order_row is not None
            order_id: UUID = entry_order_row.id

            if stop_broker_result is not None:
                # INSERT stop row with parent_order_id pointing at entry.
                await session.execute(
                    text(
                        """
                        INSERT INTO orders (
                            account_id, env, signal_id, client_order_id, broker_order_id,
                            market, direction, order_type, quantity, stop_price,
                            placed_at_utc, status, rejection_reason, retry_n,
                            parent_order_id, strategy_hash, parameter_set_hash
                        ) VALUES (
                            :acct, :env, :sig, :cid, :bid,
                            :market, :side, 'stop_market', :qty, :stop_px,
                            :placed_at, :status, :rej_reason, :retry_n,
                            :parent_id, :strat, :param
                        )
                        """
                    ),
                    {
                        "acct": plan.account_id,
                        "env": plan.env,
                        "sig": plan.signal_id,
                        "cid": plan.stop_client_order_id,
                        "bid": (
                            str(stop_broker_result.broker_order_id)
                            if stop_broker_result.broker_order_id is not None
                            else None
                        ),
                        "market": plan.market,
                        "side": plan.stop_side,
                        "qty": int(plan.quantity),
                        "stop_px": plan.stop_price,
                        "placed_at": placed_at,
                        "status": stop_db_status,
                        "rej_reason": getattr(stop_broker_result, "rejection_detail", None),
                        "retry_n": RETRY_N_PHASE_1,
                        "parent_id": order_id,
                        "strat": plan.strategy_hash,
                        "param": plan.parameter_set_hash,
                    },
                )

            # Flip the signals row status to reflect placement outcome.
            new_signal_status = "working" if db_status != "rejected" else "rejected"
            await session.execute(
                text("UPDATE signals SET status = :status WHERE id = :sid"),
                {"status": new_signal_status, "sid": plan.signal_id},
            )

    log.info(
        "order_placement_completed",
        signal_id=str(plan.signal_id),
        order_id=str(order_id),
        client_order_id=plan.client_order_id,
        broker_order_id=str(broker_order_id) if broker_order_id is not None else None,
        broker_status=broker_status,
        db_status=db_status,
        audit_event_uuid=str(audit_record.event_uuid),
        stop_placed=stop_broker_result is not None,
        stop_client_order_id=plan.stop_client_order_id if stop_broker_result else None,
        stop_broker_order_id=(
            str(stop_broker_result.broker_order_id) if stop_broker_result else None
        ),
        stop_broker_status=(stop_broker_result.status if stop_broker_result else None),
        stop_audit_event_uuid=(str(stop_audit_record.event_uuid) if stop_audit_record else None),
    )

    # Best-effort SSE fan-out so the webhook_pusher subscriber + web UI
    # observe the broker-side state transition without polling. The api's
    # SSE multiplexer is in-process (single-replica Phase 1) so the
    # emit lands in the same event loop the worker runs on. Failures
    # MUST NOT fail the placement — the orders row + audit event are
    # already durable; SSE is a notification surface, consumers reconnect
    # with Last-Event-ID + catch up from the replay buffer.
    try:
        from services.api.sse import emit_sse

        await emit_sse(
            "signal",
            {
                "action": "placed",
                "signal_id": str(plan.signal_id),
                "market": plan.market,
                "direction": "long" if plan.side == "buy" else "short",
                "side": plan.side,
                "quantity": str(plan.quantity),
                "order_id": str(order_id),
                "client_order_id": plan.client_order_id,
                "broker_order_id": (str(broker_order_id) if broker_order_id is not None else None),
                "broker_status": broker_status,
                "db_status": db_status,
                "rejection_category": rejection_category,
                "rejection_detail": rejection_detail,
                "placed_at_utc": placed_at.isoformat(),
                "environment": plan.env,
                "audit_event_uuid": str(audit_record.event_uuid),
                # Bracket extension: stop info echoed for the consumer.
                "stop_placed": stop_broker_result is not None,
                "stop_client_order_id": (plan.stop_client_order_id if stop_broker_result else None),
                "stop_price": str(plan.stop_price) if stop_broker_result else None,
                "stop_broker_order_id": (
                    str(stop_broker_result.broker_order_id)
                    if stop_broker_result is not None
                    else None
                ),
                "stop_audit_event_uuid": (
                    str(stop_audit_record.event_uuid) if stop_audit_record else None
                ),
            },
        )
    except Exception:
        log.exception(
            "order_placement_sse_emit_failed",
            signal_id=str(plan.signal_id),
            order_id=str(order_id),
        )

    return OrderPlacementResult(
        signal_id=plan.signal_id,
        order_id=order_id,
        audit_event_uuid=audit_record.event_uuid,
        broker_order_id=str(broker_order_id) if broker_order_id is not None else None,
        status=db_status,
    )


def _broker_status_to_orders_status(broker_status: str) -> str:
    """Map :class:`IbkrPlaceOrderResult.status` → ``orders.status`` enum.

    The orders.status CHECK constraint (alembic 0002) accepts:
    ``pending, working, partially_filled, filled, cancelled, rejected, expired``.
    The IBKR side surfaces a richer enum (``submitted``,
    ``pending_submit``, etc.); we collapse those to the orders enum.
    """
    if broker_status in ("submitted", "working"):
        return "working"
    if broker_status == "pending_submit":
        return "pending"
    if broker_status == "rejected":
        return "rejected"
    if broker_status in ("filled", "partially_filled", "cancelled", "expired"):
        return broker_status
    # Unknown statuses default to 'pending' for safety; the reconciliation
    # scheduler will catch up via orderStatus event subscription.
    log.warning("order_placement_unknown_broker_status", broker_status=broker_status)
    return "pending"


class OrderPlacementWorker:
    """Async polling loop that drains approved signals into IBKR orders.

    Long-lived task; constructor wires the dependencies + ``run_forever``
    is the supervisor entry point. SIGTERM-triggered shutdown via
    :meth:`request_stop`.

    Phase 1 single-instance — runs inside the api process for simplicity.
    Phase 2+ may move to a dedicated worker container when signal
    volume justifies separate lifecycle.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[Any],
        ibkr_client: IbkrClient,
        account_id: UUID,
        env: Environment,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        ibkr_call_timeout_seconds: float = DEFAULT_IBKR_CALL_TIMEOUT_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._ibkr_client = ibkr_client
        self._account_id = account_id
        self._env: Environment = env
        self._poll_interval = poll_interval_seconds
        # Per-call hard timeout on every IBKR adapter await (resolve,
        # place_order, cancel_order, subscribe_order_status). Translates
        # to IbkrPlacementError on expiry so the existing except paths
        # handle it as a transient broker-unavailable event. See the
        # DEFAULT_IBKR_CALL_TIMEOUT_SECONDS constant docstring above for
        # the silent-worker context this protects against.
        self._ibkr_call_timeout_seconds = ibkr_call_timeout_seconds
        self._stop_event = asyncio.Event()
        self._order_status_subscribed = False
        # Tracks broker_order_ids we've already emitted a "fill" SSE for
        # so a re-sent ``Filled`` status from IBKR on reconnect doesn't
        # duplicate the embed. The set is in-process only; a process
        # restart re-emits, which is acceptable — the dispatcher dedupes
        # by `order_filled:<order_id>` at the webhook_pusher layer.
        self._emitted_fill_order_ids: set[int] = set()
        self._log = log.bind(
            worker="order_placement",
            account_id=str(account_id),
            env=env,
        )

    def request_stop(self) -> None:
        """Signal the run_forever loop to exit at the next iteration boundary."""
        self._stop_event.set()

    async def run_once(self) -> int:
        """Drain all currently-approved signals in one pass.

        PR-H: Before draining, reads the current risk_state. If
        ``HALT_NEW``, the cycle is a no-op + a structured WARNING fires
        so the operator sees the gating in the api logs. NORMAL +
        CONVALESCENT permit the drain.

        Returns the number of signals successfully dispatched to IBKR
        (regardless of broker-side acceptance vs rejection). Caller uses
        this for liveness telemetry — 0 is the steady-state.
        """
        # Lazy import to keep the worker self-contained at module load
        # (signal_dispatch imports the audit writer which is a heavier
        # surface than what the worker itself needs).
        from services.risk.signal_dispatch import (
            RISK_STATES_PERMITTING_DISPATCH,
            fetch_current_risk_state,
        )

        risk_state = await fetch_current_risk_state(
            self._session_factory, account_id=self._account_id
        )
        if risk_state is not None and risk_state not in RISK_STATES_PERMITTING_DISPATCH:
            self._log.warning(
                "order_placement_worker_skipped_by_halt",
                risk_state=risk_state,
                note=(
                    "System is HALT_NEW; approved signals will accumulate "
                    "in the signals table until the operator resumes via "
                    "/system page (HALT_NEW → CONVALESCENT)."
                ),
            )
            return 0

        signals = await fetch_approved_signals(
            self._session_factory, account_id=self._account_id, env=self._env
        )
        if not signals:
            return 0

        placed = 0
        for signal in signals:
            try:
                plan = plan_order_placement(signal, env=self._env)
                # Bound the IBKR contract-qualification call. ib-async's
                # qualifyContractsAsync can hang indefinitely under
                # broker-side sluggishness (silent-worker pattern from
                # the 2026-05-17 drill 2). 30s default is the same
                # ceiling apply_order_placement uses for placeOrder.
                contract = await _await_ibkr_with_timeout(
                    self._ibkr_client.resolve_contract(plan.market),
                    operation="resolveContract",
                    timeout_seconds=self._ibkr_call_timeout_seconds,
                )
                await apply_order_placement(
                    plan,
                    ibkr_client=self._ibkr_client,
                    contract=contract,
                    session_factory=self._session_factory,
                    ibkr_call_timeout_seconds=self._ibkr_call_timeout_seconds,
                )
                placed += 1
            except OrderPlacementError as exc:
                # Pure-policy planner error — the signal shape is wrong.
                # Log + skip; an operator-side correction is needed.
                self._log.error(
                    "order_placement_plan_error",
                    signal_id=str(signal.signal_id),
                    error=str(exc),
                )
            except IbkrPlacementError as exc:
                # Broker connectivity error — log, leave the signal in
                # 'approved' for the next poll cycle to retry. This also
                # catches the _await_ibkr_with_timeout-translated
                # TimeoutError from resolveContract / placeOrder above.
                self._log.error(
                    "order_placement_broker_unavailable",
                    signal_id=str(signal.signal_id),
                    error=str(exc),
                )
                # Don't continue iterating — broker is down, fail fast.
                break
        return placed

    async def run_forever(self) -> None:
        """Supervisor entry point — poll-dispatch loop until request_stop."""
        self._log.info("order_placement_worker_started", poll_interval=self._poll_interval)
        try:
            while not self._stop_event.is_set():
                # Best-effort: subscribe to the IBKR orderStatus stream on
                # first iteration once the broker is reachable. We retry
                # in subsequent iterations if it fails so a flapping
                # connection at startup eventually wires the listener.
                if not self._order_status_subscribed:
                    try:
                        # Bound the subscription call. ib-async's
                        # reqOpenOrdersAsync (used internally by
                        # subscribe_order_status to attach the event
                        # handler) can hang under the same broker-
                        # sluggishness pattern as place_order. A
                        # bounded retry on the next poll iteration
                        # eventually subscribes once broker is healthy.
                        await _await_ibkr_with_timeout(
                            self._ibkr_client.subscribe_order_status(self._on_order_status),
                            operation="subscribeOrderStatus",
                            timeout_seconds=self._ibkr_call_timeout_seconds,
                        )
                        self._order_status_subscribed = True
                        self._log.info("order_placement_orderstatus_subscribed")
                    except Exception as exc:
                        self._log.warning(
                            "order_placement_orderstatus_subscribe_deferred",
                            error=str(exc),
                        )
                try:
                    await self.run_once()
                except Exception as exc:
                    self._log.exception("order_placement_worker_iteration_failed", error=str(exc))
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_interval)
                except TimeoutError:
                    pass  # normal cadence
        finally:
            self._log.info("order_placement_worker_stopped")

    async def _on_order_status(self, update: OrderStatusUpdate) -> None:
        """Callback registered with the IBKR adapter's orderStatusEvent.

        Acts on terminal-fill transitions only. PR-G (post-pivot 2026-05-13)
        wires the fill end-to-end through
        :func:`services.risk.fill_processor.process_fill_event`, which
        writes the audit chain (ORDER_FILLED → POSITION_OPENED/UPDATED →
        BALANCE_SNAPSHOT_RECORDED → optional TRADE_OPENED), then INSERTs
        into ``fills``, UPDATEs ``orders.status``, UPSERTs
        ``positions_current``, INSERTs ``balances``, and INSERT/UPDATEs
        ``trades``. Then emits the canonical ``fill`` SSE envelope with
        the new audit_event_uuid stamped for cross-reference.

        Pre-PR-G: this callback emitted SSE only; the tables stayed
        empty. Post-PR-G: a single Filled status from IBKR is the
        complete propagation event.

        Dedupe semantics preserved: the worker acts ONCE per
        broker_order_id. IBKR may re-fire Filled on reconnect; the
        in-process set short-circuits those.

        Error handling:
          * :class:`fill_processor.UnsupportedFillScenarioError` —
            exit / hedging case (out of PR-G scope). Logged at WARNING;
            the operator manually reconciles via the Audit page +
            psql. The dedupe set is NOT updated so a follow-up retry
            (after the operator's manual repair) can proceed.
          * :class:`fill_processor.FillProcessingError` — terminal
            failure. Logged at ERROR; same retry semantics as above.
          * Any other exception — logged at EXCEPTION; SSE NOT emitted
            because we don't have an audit linkage to stamp.

        **Non-fill terminal statuses (Defect #2 fix, 2026-05-16):**

        ``cancelled`` / ``inactive`` (IBKR's name for various reject
        modes including validation errors) propagate through
        :func:`services.risk.order_terminal_status_processor.process_terminal_status_event`
        which writes an ``order_cancelled`` or ``order_rejected`` audit
        row + UPDATEs ``orders.status`` + ``orders.rejection_reason``.
        Same dedupe set guards against IBKR re-firing on reconnect.

        ``PartiallyFilled`` is observed-only for now; the bookkeeping
        for partial fills (multiple fills rows per order, position
        running sum, trade lifecycle) is a Phase 1+ follow-up.

        ``Submitted`` / ``pending_submit`` are observed-only because
        the placeOrder path already wrote the ``order_placed`` audit
        row + emitted the SSE event.
        """
        if update.status in ("cancelled", "rejected", "inactive"):
            await self._process_terminal_status(update)
            return
        if update.status != "filled":
            return
        if update.broker_order_id in self._emitted_fill_order_ids:
            self._log.info(
                "order_placement_fill_already_emitted",
                broker_order_id=update.broker_order_id,
                client_order_id=update.client_order_id,
            )
            return

        # Lazy import to avoid module-load cycle (fill_processor imports
        # audit.writer which imports models; routing risk → audit → models
        # is OK but keeping the import inside the callback is consistent
        # with the existing import-locality pattern in this file).
        from services.risk.fill_processor import (
            FillIngestPayload,
            FillProcessingError,
            UnsupportedFillScenarioError,
            process_fill_event,
        )

        filled_at = update.last_fill_at_utc or update.observed_at_utc
        fill_price = update.avg_fill_price or Decimal(0)
        commission_usd = update.total_commission_usd
        # broker_fill_id convention: aggregate IBKR fills are exposed via
        # a single Filled status with cumulative totals (no per-execution
        # exec_id surfaced through ib-async's orderStatusEvent). Use a
        # stable f"{broker_order_id}:agg" suffix so a re-fired Filled on
        # reconnect hits the UNIQUE(broker_fill_id, created_at)
        # constraint and we catch the dupe at the I/O layer too (defense
        # in depth on top of the dedupe set).
        broker_fill_id = f"{update.broker_order_id}:agg"
        payload = FillIngestPayload(
            broker_fill_id=broker_fill_id,
            cumulative_filled_quantity=int(update.cumulative_filled_quantity),
            fill_quantity=int(update.cumulative_filled_quantity),
            fill_price=fill_price,
            commission_usd=commission_usd,
            filled_at_utc=filled_at,
        )

        try:
            result = await process_fill_event(
                session_factory=self._session_factory,
                client_order_id=update.client_order_id,
                payload=payload,
                env=self._env,
            )
        except UnsupportedFillScenarioError as exc:
            self._log.warning(
                "order_placement_fill_unsupported_scenario",
                client_order_id=update.client_order_id,
                broker_order_id=update.broker_order_id,
                error_code=exc.error_code,
                error=str(exc),
                details=exc.details,
            )
            return
        except FillProcessingError as exc:
            self._log.error(
                "order_placement_fill_processing_failed",
                client_order_id=update.client_order_id,
                broker_order_id=update.broker_order_id,
                error_code=exc.error_code,
                error=str(exc),
                details=exc.details,
            )
            return
        except Exception:
            self._log.exception(
                "order_placement_fill_unexpected_error",
                client_order_id=update.client_order_id,
                broker_order_id=update.broker_order_id,
            )
            return

        if result is None:
            self._log.warning(
                "order_placement_fill_unknown_order",
                client_order_id=update.client_order_id,
                broker_order_id=update.broker_order_id,
            )
            return

        # SSE emit — best-effort, after the durable writes have landed.
        # The audit_event_uuid in the envelope is the ORDER_FILLED row
        # (audit_event_uuids[0]) — the broker-side confirmation event
        # the operator deep-links from. Position / balance / trade audit
        # uuids are NOT in the envelope to keep the wire shape stable;
        # downstream consumers can pull them via the audit-event page if
        # needed.
        order_filled_audit_uuid = (
            str(result.audit_event_uuids[0]) if result.audit_event_uuids else None
        )
        try:
            from services.api.sse import emit_sse

            await emit_sse(
                "fill",
                {
                    "order_id": str(update.broker_order_id),
                    "client_order_id": update.client_order_id,
                    "signal_id": str(result.signal_id),
                    "market": update.market,
                    "side": update.side,
                    "quantity": str(update.cumulative_filled_quantity),
                    "fill_price": str(fill_price),
                    "commission_usd": str(commission_usd),
                    "filled_at_utc": filled_at.isoformat(),
                    "environment": self._env,
                    "fill_id": str(result.fill_id),
                    "position_id": str(result.position_id),
                    "trade_id": str(result.trade_id),
                    "balance_id": str(result.balance_id),
                    "new_order_status": result.new_order_status,
                    "audit_event_uuid": order_filled_audit_uuid,
                },
            )
        except Exception:
            self._log.exception(
                "order_placement_fill_sse_emit_failed",
                client_order_id=update.client_order_id,
                broker_order_id=update.broker_order_id,
            )
            # We DO add to the dedupe set even on SSE failure — the audit +
            # tables are durable, this is just a notification path. A
            # re-fired Filled would write a second fills row (UNIQUE
            # constraint catches it at the SQL layer too).
            self._emitted_fill_order_ids.add(update.broker_order_id)
            return

        self._emitted_fill_order_ids.add(update.broker_order_id)
        self._log.info(
            "order_placement_fill_propagated",
            client_order_id=update.client_order_id,
            broker_order_id=update.broker_order_id,
            fill_id=str(result.fill_id),
            position_id=str(result.position_id),
            trade_id=str(result.trade_id),
            balance_id=str(result.balance_id),
            new_order_status=result.new_order_status,
            quantity=str(update.cumulative_filled_quantity),
            fill_price=str(fill_price),
            commission_usd=str(commission_usd),
            audit_event_uuid=order_filled_audit_uuid,
        )

    async def _process_terminal_status(self, update: OrderStatusUpdate) -> None:
        """Handle cancelled / rejected / inactive orderStatus events.

        Defect #2 fix (2026-05-16). Pre-this-method, the worker dropped
        these events silently and ``orders.status`` stayed at
        ``pending`` forever even after IBKR had terminally rejected the
        order at validation time.

        Delegates to
        :func:`services.risk.order_terminal_status_processor.process_terminal_status_event`
        which:
          1. Looks up the orders row by client_order_id
          2. If already terminal, no-ops (idempotent vs IBKR re-fires)
          3. Appends an ``order_cancelled`` / ``order_rejected`` audit row
          4. UPDATEs orders.status + rejection_reason
          5. Returns the audit linkage for SSE stamping

        Dedupes via the same ``_emitted_fill_order_ids`` set as the fill
        path. An order can only have ONE terminal state in practice
        (fill OR cancel OR reject); the unified set is the simplest
        correctness guarantee.

        SSE emit fires the canonical ``order`` envelope with
        ``action=cancelled`` or ``action=rejected`` so the web /signals
        page + the Discord webhook pusher can subscribe. Best-effort —
        an SSE failure does NOT roll back the durable writes.
        """
        if update.broker_order_id in self._emitted_fill_order_ids:
            self._log.info(
                "order_placement_terminal_already_processed",
                broker_order_id=update.broker_order_id,
                client_order_id=update.client_order_id,
                status=update.status,
            )
            return

        # Lazy import to avoid the same module-load cycle as fill_processor.
        from services.risk.order_terminal_status_processor import (
            OrderTerminalStatusError,
            TerminalStatusPayload,
            process_terminal_status_event,
        )

        # The OrderStatusKind Literal ("cancelled", "rejected", "inactive",
        # "filled", "submitted", "partially_filled") is the same shape as
        # the terminal processor's TerminalOrderStatus Literal for these
        # three values. Narrow defensively.
        if update.status not in ("cancelled", "rejected", "inactive"):
            # Unreachable in production (_on_order_status gates this);
            # defensive for direct-call test paths.
            return

        payload = TerminalStatusPayload(
            broker_order_id=update.broker_order_id,
            status_kind=update.status,
            rejection_reason=update.rejection_reason,
            observed_at_utc=update.observed_at_utc,
        )

        try:
            result = await process_terminal_status_event(
                session_factory=self._session_factory,
                client_order_id=update.client_order_id,
                payload=payload,
                env=self._env,
            )
        except OrderTerminalStatusError as exc:
            self._log.error(
                "order_placement_terminal_processing_failed",
                client_order_id=update.client_order_id,
                broker_order_id=update.broker_order_id,
                error_code=exc.error_code,
                error=str(exc),
                details=exc.details,
            )
            return
        except Exception:
            self._log.exception(
                "order_placement_terminal_unexpected_error",
                client_order_id=update.client_order_id,
                broker_order_id=update.broker_order_id,
            )
            return

        if result is None:
            # Either order_not_found or already_terminal — both logged
            # at INFO by process_terminal_status_event. Mark the
            # dedupe set since we don't need to act on it again.
            self._emitted_fill_order_ids.add(update.broker_order_id)
            return

        # SSE emit
        try:
            from services.api.sse import emit_sse

            await emit_sse(
                "order",
                {
                    "action": result.new_status,  # "cancelled" | "rejected"
                    "order_id": str(result.order_id),
                    "client_order_id": update.client_order_id,
                    "signal_id": str(result.signal_id) if result.signal_id is not None else None,
                    "market": result.market,
                    "broker_order_id": str(update.broker_order_id),
                    "rejection_reason": update.rejection_reason,
                    "environment": self._env,
                    "new_status": result.new_status,
                    "audit_event_uuid": str(result.audit_event_uuid),
                    "audit_sequence_no": result.audit_sequence_no,
                },
            )
        except Exception:
            self._log.exception(
                "order_placement_terminal_sse_emit_failed",
                client_order_id=update.client_order_id,
                broker_order_id=update.broker_order_id,
            )

        self._emitted_fill_order_ids.add(update.broker_order_id)
        self._log.info(
            "order_placement_terminal_propagated",
            client_order_id=update.client_order_id,
            broker_order_id=update.broker_order_id,
            order_id=str(result.order_id),
            new_status=result.new_status,
            audit_event_uuid=str(result.audit_event_uuid),
        )


__all__ = [
    "DEFAULT_IBKR_CALL_TIMEOUT_SECONDS",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "RETRY_N_PHASE_1",
    "ApprovedSignalRow",
    "OrderPlacementError",
    "OrderPlacementPlan",
    "OrderPlacementResult",
    "OrderPlacementWorker",
    "apply_order_placement",
    "fetch_approved_signals",
    "plan_order_placement",
]
