"""services/risk/order_placement_worker.py — approved-signal → IBKR order pipeline.

Worker-PR-1 (post-pivot 2026-05-12). Background async loop that polls the
``signals`` table for ``status='approved'`` rows without a matching
``orders`` row, builds an IBKR placeOrder request via the pure-policy
planner, dispatches via :class:`services.execution.ibkr_client.IbkrClient`,
writes an ``order_placed`` audit row, INSERTs the orders row, and updates
the signals row's ``status`` to ``'working'``.

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
``<strategy_short>-<paramset_short>-<signal_short>-<retry_n>`` — 33 chars
total via the 8-char prefixes of strategy_hash + parameter_set_hash +
signal UUID short form. Phase 1 retry_n=0 (no retry on rejection yet;
Phase 2+ adds exponential backoff per spec §6.3).

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


@dataclass(frozen=True, slots=True)
class OrderPlacementPlan:
    """Pure-policy plan for placing one approved signal as an IBKR order.

    Built by :func:`plan_order_placement` from an :class:`ApprovedSignalRow`.
    The orchestrator :func:`apply_order_placement` consumes this + an
    :class:`IbkrClient` instance to do the actual placement.

    All ID derivation is deterministic — same signal row → same
    client_order_id always (necessary for retry idempotency at the
    broker boundary).
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


def plan_order_placement(signal: ApprovedSignalRow, *, env: Environment) -> OrderPlacementPlan:
    """Build the placement plan for one approved signal.

    Pure policy — no I/O, no clock, no random. Same inputs always
    produce the same plan, which means client_order_id is
    deterministic per signal (necessary for IBKR retry idempotency).

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
                      s.parameter_set_hash
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
        )
        for r in rows
    ]


async def apply_order_placement(
    plan: OrderPlacementPlan,
    *,
    ibkr_client: IbkrClient,
    contract: IbkrContractRef,
    session_factory: async_sessionmaker[Any],
    phase_at_emit: PhaseAtEmit = 1,
) -> OrderPlacementResult:
    """Execute the placement plan: IBKR placeOrder → audit → INSERT orders.

    Three sequential steps:

    1. ``ibkr_client.place_order(...)`` — the broker-side side effect.
       Failure raises :class:`IbkrPlacementError` (broker connectivity)
       or returns an ``IbkrPlaceOrderResult`` with ``status='rejected'``
       (broker validation rejection).
    2. ``append_audit_event(AuditEventType.ORDER_PLACED, ...)`` — its
       own SERIALIZABLE transaction. Payload includes the
       client_order_id + broker_order_id (when present) for downstream
       reconciliation traceability.
    3. ``INSERT INTO orders ...`` carrying the audit_event_uuid as FK
       linkage + UPDATE signals.status='working'. Single transaction so
       both rows land or neither does.

    Returns an :class:`OrderPlacementResult` regardless of whether IBKR
    accepted the order — rejection vs success is reflected in the
    ``status`` + ``broker_order_id`` fields.
    """
    placed_at = datetime.now(tz=UTC)

    # Step 1: IBKR side effect.
    request = IbkrPlaceOrderRequest(
        client_order_id=plan.client_order_id,
        contract=contract,
        side=plan.side,
        quantity=plan.quantity,
        order_type=plan.order_type,
        limit_price=plan.limit_price,
        time_in_force=plan.time_in_force,
    )
    try:
        broker_result = await ibkr_client.place_order(request)
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

    # Step 2: audit (audit-first per spec §2.10.1; the broker side effect
    # already happened so we record what actually was done).
    audit_payload: dict[str, Any] = {
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
    }
    async with session_factory() as audit_session:
        audit_record = await append_audit_event(
            audit_session,
            AuditEventType.ORDER_PLACED,
            audit_payload,
            account_id=plan.account_id,
            env=plan.env,
            phase_at_emit=phase_at_emit,
            source_clock_ts=placed_at,
        )

    # Step 3: INSERT orders + UPDATE signal. Single transaction so the
    # business-data view is consistent.
    db_status = _broker_status_to_orders_status(broker_status)
    async with session_factory() as session:
        async with session.begin():
            order_row = (
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
            assert order_row is not None
            order_id: UUID = order_row.id

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
    ) -> None:
        self._session_factory = session_factory
        self._ibkr_client = ibkr_client
        self._account_id = account_id
        self._env: Environment = env
        self._poll_interval = poll_interval_seconds
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
                contract = await self._ibkr_client.resolve_contract(plan.market)
                await apply_order_placement(
                    plan,
                    ibkr_client=self._ibkr_client,
                    contract=contract,
                    session_factory=self._session_factory,
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
                # 'approved' for the next poll cycle to retry.
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
                        await self._ibkr_client.subscribe_order_status(self._on_order_status)
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

        Other transitions (Submitted, PartiallyFilled, Cancelled,
        Rejected) are observed-only for now; the placeOrder path's
        ``order_placed`` audit + SSE emit already covers Submitted, and
        terminal cancel/reject paths land alongside the reconciliation
        + retry follow-ups.
        """
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


__all__ = [
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
