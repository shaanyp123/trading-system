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
from collections.abc import Awaitable, Callable
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
from services.execution.ibkr_adapter import PHASE1_TICK_SIZES, round_to_tick
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

    signal_type: Literal["entry", "exit"] = "entry"
    """Exit-pipeline PR-C (2026-05-27). Discriminator carried on every
    signal row. ``'entry'`` → the existing bracket-order entry path.
    ``'exit'`` → the new bracket-cancel + close-order path (see
    :func:`plan_exit_close_placement` + :func:`apply_exit_close_placement`).
    Default 'entry' preserves backwards compat for any historical test
    fixture or call site that doesn't construct the field explicitly."""


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

    Exit-pipeline PR-C (2026-05-27): adds optional ``error_code`` +
    ``details`` so the worker's exit branch can flag specific
    recoverable states (``POSITION_ALREADY_FLAT`` →
    signals.status='cancelled' flip) without string-matching the
    message. Backwards compat: positional message-only construction
    still works for the pre-PR-C call sites.
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.details = details or {}


@dataclass(frozen=True, slots=True)
class BracketStopRow:
    """Snapshot of the OPEN bracket-stop ``orders`` row associated with
    an entry signal — the row PR-C exit branch CANCELS before placing
    the close order.

    Resolved by :func:`fetch_open_bracket_stop_for_entry` from the
    entry signal_id derived via the open trade lookup. All fields are
    sourced from the ``orders`` row.
    """

    order_id: UUID
    order_created_at: datetime
    client_order_id: str
    broker_order_id: str | None
    """May be ``None`` only in the unlikely race where the bracket stop
    was pre-INSERTed but the IBKR ``placeOrder`` ack hadn't returned
    yet. Cancel by client_order_id in that case (ibkr_client supports
    that lookup via the internal client_order_id → order map)."""

    stop_price: Decimal
    parent_order_id: UUID
    """The original entry orders.id. The close order pre-INSERT uses
    this as its ``parent_order_id`` so the audit chain links cleanly
    (entry → bracket-stop → close) without inventing a new graph edge."""


@dataclass(frozen=True, slots=True)
class ExitClosePlan:
    """Pure-policy plan for an exit-close placement.

    Built by :func:`plan_exit_close_placement` from an
    :class:`ApprovedSignalRow` (signal_type='exit') + the
    :class:`BracketStopRow` resolved at I/O time + a FRESH
    ``positions_current`` read.

    Mirrors :class:`OrderPlacementPlan` for the entry path but carries
    extra fields the exit branch needs: the bracket-stop linkage (so
    apply can CANCEL it pre-place) and the prior-position snapshot
    (for the audit payload + POSITION_UNPROTECTED descriptor).

    Direction-sanity: ``close_side`` is computed from the FRESH
    position direction (sign of ``prior_position_quantity_signed``);
    long position → sell close, short position → buy close.
    """

    signal_id: UUID
    account_id: UUID
    env: Environment
    market: str
    # Close-order fields
    close_side: IbkrOrderSide
    close_quantity: Decimal
    """ALWAYS positive (magnitude). The direction is encoded in close_side."""

    close_limit_price: Decimal
    """Marketable-limit price — derived from signal.decision_price
    (the last close at signal-emit time). Tick-rounded."""

    close_client_order_id: str
    """``<strategy>-<param>-<sig>-<retry>-close`` suffix to distinguish
    from the original entry's CID."""

    # Bracket-stop linkage (the row apply will CANCEL pre-place)
    bracket_stop_order_id: UUID
    bracket_stop_order_created_at: datetime
    bracket_stop_client_order_id: str
    bracket_stop_broker_order_id: str | None
    bracket_stop_stop_price: Decimal
    parent_entry_order_id: UUID
    """The original entry orders.id — used as the close orders row's
    ``parent_order_id`` so the audit chain links cleanly."""

    # Prior position snapshot (audit-trail + POSITION_UNPROTECTED payload)
    prior_position_direction: Literal["long", "short"]
    prior_position_quantity: int
    """Signed magnitude. Long = positive, short = negative. The close
    quantity is ``abs()`` of this value."""

    strategy_hash: str
    parameter_set_hash: str
    decision_price: Decimal
    """Echoed from the signal row for symmetry with the entry
    :class:`OrderPlacementPlan` — same audit-payload conventions."""


@dataclass(frozen=True, slots=True)
class PositionUnprotectedAlertDescriptor:
    """Inputs the exit-failure handler hands to the alert dispatch hook.

    Worker constructs this from the in-scope plan + the audit row that
    JUST landed (``POSITION_UNPROTECTED``). Hook implementer (in
    ``services/api/main.py``) opens an httpx.AsyncClient, INSERTs the
    ``alerts`` row with category='position_unprotected', severity='P0',
    then invokes
    :func:`services.webhook_pusher.dispatcher.dispatch_alert` for the
    Discord #alerts + #critical + Resend email fan-out.

    A02 note: the hook lives in ``services/api/main.py`` (hot-fix
    whitelist), so wiring + signature changes don't require
    risk-review-approved. The worker only needs the hook *type* + an
    optional constructor arg.
    """

    account_id: UUID
    env: Environment
    signal_id: UUID
    """The EXIT signal_id. Hook embeds this in the Discord deep-link so
    the operator can navigate to /signals?id=... to see the original
    ``exit_reason`` (in the SIGNAL_EMITTED audit payload) without the
    worker needing to JOIN the audit_log JCS-bytes payload."""

    market: str
    prior_position_direction: Literal["long", "short"]
    prior_position_quantity: int
    """Signed magnitude from the FRESH positions_current read at the
    place-order attempt (NOT the signal's audit-trail value). This is
    the position that is now NAKED — the absolute value is the size the
    operator needs to protect."""

    last_known_stop_price: Decimal
    """Stop price from the bracket-stop orders row JUST cancelled.
    Operator uses this to place a replacement stop at the same level
    via TWS or ``replace_protective_stop.py`` (deferred PR)."""

    close_client_order_id: str
    """The close order's client_order_id that we attempted to place.
    Operator can search IBKR/TWS by this CID to confirm the broker
    side didn't accept it."""

    close_failure_reason: str
    """Human-readable reason — typically an IBKR rejection_detail or
    a TimeoutError message. The Discord embed surfaces this."""

    triggering_audit_event_uuid: UUID
    """The POSITION_UNPROTECTED audit row's event_uuid — links the
    alerts row to the audit chain via the ``triggering_audit_event_uuid``
    column (alembic 0004 schema)."""


#: Callback type the worker invokes from the cancel-success-place-fail
#: failure path. Hook MUST be idempotent on
#: ``descriptor.triggering_audit_event_uuid`` so a retry doesn't
#: double-fire the Discord embed. Returns ``None`` (hook fails open —
#: any exception is caught + logged at ERROR by the worker; the
#: audit + alerts rows remain durable for operator-side recovery).
PositionUnprotectedAlertHook = Callable[[PositionUnprotectedAlertDescriptor], Awaitable[None]]


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
    raw_stop_price = _extract_stop_price_from_sizing_trace(
        signal.sizing_trace,
        market=signal.market,
        direction=signal.direction,
        decision_price=signal.decision_price,
    )
    # Tick-rounding (drill 6 fix, 2026-05-18): IBKR rejects orders whose
    # lmtPrice / auxPrice is not aligned to the contract's tick size with
    # Error 110 "The price does not conform to the minimum price variation
    # for this contract." Round limit + stop to the market's tick BEFORE
    # building the plan so the worker's downstream place_order call is
    # always tick-aligned. Direction sanity (stop strictly below long /
    # strictly above short) was already enforced on the raw values above;
    # we re-validate after rounding because the rounding could flip the
    # inequality on edges (e.g., long entry rounds DOWN past the stop that
    # rounds UP). Both prices share the same tick map keyed by ``market``.
    try:
        limit_price = round_to_tick(signal.decision_price, market=signal.market)
        stop_price = round_to_tick(raw_stop_price, market=signal.market)
    except KeyError as exc:
        raise OrderPlacementError(
            f"market {signal.market!r} not in PHASE1_TICK_SIZES; "
            "see services.execution.ibkr_adapter for the Phase 1 universe map."
        ) from exc
    # Post-rounding direction sanity — catches the edge case where the
    # raw stop is < 1 tick from the entry (e.g., long entry 7402.20,
    # stop 7402.10 with /MES tick=0.25 → both round to 7402.25, stop
    # NO LONGER strictly below entry). Refuse the bracket rather than
    # ship a self-defeating order.
    if signal.direction == "long" and stop_price >= limit_price:
        raise OrderPlacementError(
            f"After tick-rounding: long entry limit_price={limit_price} but "
            f"stop_price={stop_price} is NOT strictly below; "
            f"raw decision_price={signal.decision_price}, raw stop={raw_stop_price}, "
            f"tick={PHASE1_TICK_SIZES.get(signal.market)}. "
            "Strategy needs a wider stop offset."
        )
    if signal.direction == "short" and stop_price <= limit_price:
        raise OrderPlacementError(
            f"After tick-rounding: short entry limit_price={limit_price} but "
            f"stop_price={stop_price} is NOT strictly above; "
            f"raw decision_price={signal.decision_price}, raw stop={raw_stop_price}, "
            f"tick={PHASE1_TICK_SIZES.get(signal.market)}. "
            "Strategy needs a wider stop offset."
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
        limit_price=limit_price,
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
                      s.signal_type,
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
    rows_out: list[ApprovedSignalRow] = []
    for r in rows:
        # Narrow signal_type literal — DB column is free TEXT; treat
        # unknown values as 'entry' for forwards-compat (the entry path
        # then raises a descriptive error if the shape is wrong).
        sig_type_raw = str(r.signal_type) if r.signal_type is not None else "entry"
        sig_type: Literal["entry", "exit"] = "exit" if sig_type_raw == "exit" else "entry"
        rows_out.append(
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
                signal_type=sig_type,
            )
        )
    return rows_out


# ---------------------------------------------------------------------------
# Exit-pipeline PR-C — bracket-stop + position lookups
# ---------------------------------------------------------------------------


async def fetch_entry_signal_id_for_open_trade(
    session_factory: async_sessionmaker[Any],
    *,
    account_id: UUID,
    market: str,
) -> UUID | None:
    """Resolve the entry signal_id for the currently-open trade in
    ``(account_id, market)``.

    Returns ``None`` when no open trade row exists — the operator
    flattened via TWS, a prior exit already filled, or the strategy
    emitted an exit for a position that no longer exists. The exit
    branch treats ``None`` as "nothing to close" and flips
    signals.status='cancelled' without placing an order or cancelling
    a bracket.

    Phase 1 invariant: ``trades.state = 'open_position'`` is unique per
    ``(account_id, market)`` (the fill_processor maintains this — entry
    fills INSERT; exit-close fills UPDATE state='closed'). If two rows
    exist the lookup picks the newest by ``created_at`` defensively.
    """
    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT entry_signal_id FROM trades "
                    "WHERE account_id = :acct AND market = :market "
                    "  AND state = 'open_position' "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"acct": account_id, "market": market},
            )
        ).fetchone()
    if row is None:
        return None
    return UUID(str(row.entry_signal_id))


async def fetch_open_bracket_stop_for_entry(
    session_factory: async_sessionmaker[Any],
    *,
    entry_signal_id: UUID,
) -> BracketStopRow | None:
    """SELECT the open bracket-stop ``orders`` row for an entry signal.

    Returns ``None`` when no row matches — either the bracket already
    fired (fill_processor flipped status='filled') or the bracket was
    never placed (entry rejected at the broker; design §11 R3
    edge case). The exit branch treats ``None`` as a no-op for the
    cancel step and proceeds straight to the close placement (with a
    structured log so the operator can audit).

    The lookup keys on the bracket-order Option B contract documented
    in :mod:`services.risk.fill_processor`: the stop reuses the entry
    signal_id; ``order_type='stop_market'``; ``status='working'``;
    ``parent_order_id IS NOT NULL``. There is at most one such row per
    entry signal in the current model.
    """
    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT id, created_at, client_order_id, broker_order_id,
                           stop_price, parent_order_id
                    FROM orders
                    WHERE signal_id = :sid
                      AND order_type = 'stop_market'
                      AND status = 'working'
                      AND parent_order_id IS NOT NULL
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {"sid": entry_signal_id},
            )
        ).fetchone()
    if row is None:
        return None
    return BracketStopRow(
        order_id=row.id,
        order_created_at=row.created_at,
        client_order_id=row.client_order_id,
        broker_order_id=str(row.broker_order_id) if row.broker_order_id is not None else None,
        stop_price=Decimal(str(row.stop_price)),
        parent_order_id=row.parent_order_id,
    )


async def fetch_current_position_quantity(
    session_factory: async_sessionmaker[Any],
    *,
    account_id: UUID,
    market: str,
) -> int:
    """Read FRESH ``positions_current.quantity`` for the (account, market)
    tuple.

    Returns ``0`` when no row exists (position already flat — the
    bracket fired between exit signal emission and operator approval,
    or the operator flattened manually). The exit planner uses this
    value DIRECTLY (per design §Q3 — dispatcher-side sizing); the
    signal's ``prior_position_quantity`` is for audit-trail only.

    Phase 1 invariant: at most one positions_current row per
    (account_id, market, contract_id) — but the bracket-order Option B
    contract puts contract_id on a NULL→FK boundary, so we sum across
    contract_id to handle both ETF (contract_id NULL) + futures
    (contract_id set) cleanly without the caller needing the contract
    map. Phase 2+ multi-leg positions would need a more nuanced view.
    """
    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT COALESCE(SUM(quantity), 0) AS total_quantity "
                    "FROM positions_current "
                    "WHERE account_id = :acct AND market = :market"
                ),
                {"acct": account_id, "market": market},
            )
        ).fetchone()
    if row is None:
        return 0
    return int(row.total_quantity)


# Note: ``exit_reason`` is NOT looked up here for the POSITION_UNPROTECTED
# alert payload. PR-B persists exit_reason in the SIGNAL_EMITTED audit
# payload only (no signals-table column, no audit_event_uuid back-pointer
# on signals.id). The audit_log table stores payload as ``payload_jcs
# BYTEA`` (not JSONB) per alembic 0001 — so a clean post-hoc lookup
# requires either an expensive ``convert_from(payload_jcs, 'UTF8')::jsonb``
# cast or a follow-up migration adding a signal_emitted_audit_event_uuid
# column to signals. Neither is justified for the rare POSITION_UNPROTECTED
# path. The Discord embed includes the exit signal_id; the operator
# deep-links to /signals to see the original exit_reason in context.


# ---------------------------------------------------------------------------
# Exit-pipeline PR-C — plan + apply
# ---------------------------------------------------------------------------


def plan_exit_close_placement(
    signal: ApprovedSignalRow,
    *,
    bracket_stop: BracketStopRow,
    current_position_quantity: int,
    env: Environment,
) -> ExitClosePlan:
    """Build the close-placement plan for an approved exit signal.

    Pure policy — no I/O, no clock, no random. Same inputs always
    produce the same plan, so the close order's client_order_id is
    deterministic per signal (necessary for IBKR retry idempotency).

    :param signal: the approved ``signal_type='exit'`` row. The signal's
        ``decision_price`` is the marketable limit; ``direction`` is
        ``'flat'`` (the strategy emits flat for exits per design §5.2)
        and ``target_contracts`` is ``0`` (dispatcher-side sizing per
        §Q3 — the *real* quantity comes from
        ``current_position_quantity``).
    :param bracket_stop: the OPEN bracket-stop orders row to cancel
        (resolved by :func:`fetch_open_bracket_stop_for_entry`). Its
        ``parent_order_id`` is the original entry orders.id — the
        close orders row chains its own ``parent_order_id`` here so
        the audit graph links entry → bracket-stop → close.
    :param current_position_quantity: signed quantity from the FRESH
        ``positions_current`` read (long > 0, short < 0). The close
        direction + magnitude derive from the sign + absolute value.

    Raises:
      * :class:`OrderPlacementError` (``error_code='POSITION_ALREADY_FLAT'``)
        when ``current_position_quantity == 0``. The worker's exit
        branch catches + flips signals.status='cancelled' (per operator
        decision 2026-05-27 — reuse 'cancelled' rather than add a new
        signals.status CHECK value). No close order placed; the bracket
        stop is NOT cancelled (caller's responsibility to gate the
        cancel on this check).
      * :class:`OrderPlacementError` when the signal shape is
        malformed (zero direction with non-zero target_contracts, etc.).
    """
    if signal.signal_type != "exit":
        raise OrderPlacementError(
            f"plan_exit_close_placement called with signal_type={signal.signal_type!r}; "
            "expected 'exit'.",
            error_code="WRONG_SIGNAL_TYPE",
        )
    if current_position_quantity == 0:
        raise OrderPlacementError(
            "POSITION_ALREADY_FLAT: positions_current.quantity is 0 at place-order time; "
            "either the bracket stop fired between exit-signal emit + approval, or the "
            "operator manually flattened via TWS.",
            error_code="POSITION_ALREADY_FLAT",
            details={
                "signal_id": str(signal.signal_id),
                "market": signal.market,
            },
        )

    # Direction + magnitude derive from the FRESH read (design §Q3).
    direction: Literal["long", "short"] = "long" if current_position_quantity > 0 else "short"
    close_side: IbkrOrderSide = "sell" if current_position_quantity > 0 else "buy"
    close_qty = Decimal(abs(current_position_quantity))

    # Client_order_id: reuse the entry-path generator with a ``-close``
    # suffix so it's distinguishable from the original entry CID + the
    # bracket-stop's ``-stop`` CID.
    entry_cid = _build_client_order_id(
        strategy_hash=signal.strategy_hash,
        parameter_set_hash=signal.parameter_set_hash,
        signal_id=signal.signal_id,
        retry_n=RETRY_N_PHASE_1,
    )
    close_cid = f"{entry_cid}-close"

    # Tick-rounding (mirrors entry path). Marketable-limit at the
    # signal's decision_price — for V1, this is the last close at
    # signal-emit time. Stale by up to ~24h by approval time but the
    # ``limit_marketable`` order type means IBKR fills at the prevailing
    # marketable side anyway; the limit is a slippage ceiling.
    try:
        close_limit_price = round_to_tick(signal.decision_price, market=signal.market)
    except KeyError as exc:
        raise OrderPlacementError(
            f"market {signal.market!r} not in PHASE1_TICK_SIZES; "
            "see services.execution.ibkr_adapter for the Phase 1 universe map.",
            error_code="UNKNOWN_MARKET_TICK",
        ) from exc

    return ExitClosePlan(
        signal_id=signal.signal_id,
        account_id=signal.account_id,
        env=env,
        market=signal.market,
        close_side=close_side,
        close_quantity=close_qty,
        close_limit_price=close_limit_price,
        close_client_order_id=close_cid,
        bracket_stop_order_id=bracket_stop.order_id,
        bracket_stop_order_created_at=bracket_stop.order_created_at,
        bracket_stop_client_order_id=bracket_stop.client_order_id,
        bracket_stop_broker_order_id=bracket_stop.broker_order_id,
        bracket_stop_stop_price=bracket_stop.stop_price,
        parent_entry_order_id=bracket_stop.parent_order_id,
        prior_position_direction=direction,
        prior_position_quantity=current_position_quantity,
        strategy_hash=signal.strategy_hash,
        parameter_set_hash=signal.parameter_set_hash,
        decision_price=signal.decision_price,
    )


async def apply_exit_close_placement(
    plan: ExitClosePlan,
    *,
    ibkr_client: IbkrClient,
    contract: IbkrContractRef,
    session_factory: async_sessionmaker[Any],
    phase_at_emit: PhaseAtEmit = 1,
    alert_dispatch_hook: PositionUnprotectedAlertHook | None = None,
    ibkr_call_timeout_seconds: float = DEFAULT_IBKR_CALL_TIMEOUT_SECONDS,
) -> OrderPlacementResult:
    """Execute an exit-close plan with audit-first ordering.

    Step sequence (design §Q4 + §Q5, with audit-first reordering per
    backend-spec §2.10.1):

      1. ``ORDER_CANCELLED`` audit (intent) for the bracket stop,
         reason ``'bracket_stop_replaced_by_exit_signal'``. Audit-first
         per §2.10.1 — records intent, THEN acts.
      2. ``ibkr_client.cancel_order(bracket_stop.client_order_id)``.
         Bounded by ``ibkr_call_timeout_seconds``. On
         :class:`IbkrPlacementError`: re-raise + the worker treats as
         "broker down" + retries next cycle (the cancel-intent audit
         row is durable; a retry is idempotent if the bracket already
         cancelled on the broker side).
      3. ``UPDATE orders SET status='cancelled'`` for the bracket-stop
         row.
      4. INSERT close orders row (PR-ζ pattern — pre-INSERT closes the
         orderStatusEvent race with the fill_processor).
         ``parent_order_id = plan.parent_entry_order_id`` so the audit
         chain links entry → close.
      5. ``ibkr_client.place_order(close)`` — limit-marketable, NO
         bracket (position is going to zero; no protective stop
         needed). Bounded by ``ibkr_call_timeout_seconds``.
      6. **POSITION_UNPROTECTED branch**: on place failure (broker
         rejection OR :class:`IbkrPlacementError` from the timeout
         wrapper), write a ``POSITION_UNPROTECTED`` audit row + invoke
         the alert dispatch hook (P0 Discord #alerts + #critical +
         Resend email) + raise :class:`OrderPlacementError`
         (``error_code='CLOSE_PLACEMENT_FAILED'``). The pre-placed
         close orders row stays in ``status='pending'`` for operator
         reconciliation.
      7. On place success: ``ORDER_PLACED`` audit for the close +
         UPDATE close orders row with broker_order_id + status +
         UPDATE ``signals.status='working'``.

    Returns the close order's :class:`OrderPlacementResult` (analogous
    to the entry path's return shape).
    """
    placed_at = datetime.now(tz=UTC)
    log_bound = log.bind(
        signal_id=str(plan.signal_id),
        market=plan.market,
        bracket_stop_client_order_id=plan.bracket_stop_client_order_id,
        close_client_order_id=plan.close_client_order_id,
        exit_path=True,
    )

    # Step 1: audit-first ORDER_CANCELLED for the bracket stop.
    cancel_audit_payload: dict[str, Any] = {
        "signal_id": str(plan.signal_id),
        "account_id": str(plan.account_id),
        "order_id": str(plan.bracket_stop_order_id),
        "client_order_id": plan.bracket_stop_client_order_id,
        "broker_order_id": plan.bracket_stop_broker_order_id,
        "market": plan.market,
        "stop_price": str(plan.bracket_stop_stop_price),
        "reason": "bracket_stop_replaced_by_exit_signal",
        "cancelled_at_utc": placed_at.isoformat(),
        "leg": "bracket_stop",
    }
    async with session_factory() as audit_session:
        cancel_audit_record = await append_audit_event(
            audit_session,
            AuditEventType.ORDER_CANCELLED,
            cancel_audit_payload,
            account_id=plan.account_id,
            env=plan.env,
            phase_at_emit=phase_at_emit,
            source_clock_ts=placed_at,
        )
    log_bound.info(
        "order_placement_exit_bracket_cancel_audit_written",
        cancel_audit_event_uuid=str(cancel_audit_record.event_uuid),
    )

    # Step 2: cancel the bracket at IBKR. Bounded by the wall-clock
    # ceiling so a hung cancel doesn't trap the worker.
    try:
        await _await_ibkr_with_timeout(
            ibkr_client.cancel_order(plan.bracket_stop_client_order_id),
            operation="cancelOrder.bracket_stop_replaced_by_exit",
            timeout_seconds=ibkr_call_timeout_seconds,
        )
    except IbkrPlacementError:
        # Broker-side cancel failed/timed out. The audit row is durable
        # (records our intent). Re-raise so the worker treats this as
        # "broker down" + retries next cycle. The bracket stop stays
        # 'working' in our orders row until the next cycle's recon or
        # a successful re-cancel observes the IBKR-side state.
        log_bound.exception("order_placement_exit_bracket_cancel_failed")
        raise

    # Step 3: UPDATE the bracket-stop orders row to status='cancelled'.
    # Partition addressed via (id, created_at) per alembic 0002 schema.
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE orders SET status = 'cancelled', "
                    "    rejection_reason = 'bracket_stop_replaced_by_exit_signal' "
                    "WHERE id = :oid AND created_at = :created"
                ),
                {
                    "oid": plan.bracket_stop_order_id,
                    "created": plan.bracket_stop_order_created_at,
                },
            )
    log_bound.info("order_placement_exit_bracket_cancel_row_updated")

    # Step 4: PR-ζ pre-INSERT the close orders row before the IBKR call.
    # ``parent_order_id = plan.parent_entry_order_id`` — the close is
    # graph-child of the ORIGINAL entry (NOT the bracket stop) so the
    # audit chain reads cleanly: entry → bracket_stop (now cancelled) +
    # entry → close (now in flight). The bracket and the close are
    # siblings parented at the entry.
    async with session_factory() as session:
        async with session.begin():
            close_row = (
                await session.execute(
                    text(
                        """
                        INSERT INTO orders (
                            account_id, env, signal_id, client_order_id, broker_order_id,
                            market, direction, order_type, quantity, limit_price,
                            placed_at_utc, status, rejection_reason, retry_n,
                            parent_order_id, strategy_hash, parameter_set_hash
                        ) VALUES (
                            :acct, :env, :sig, :cid, NULL,
                            :market, :side, 'limit_marketable', :qty, :lim,
                            :placed_at, 'pending', NULL, :retry_n,
                            :parent_id, :strat, :param
                        )
                        RETURNING id
                        """
                    ),
                    {
                        "acct": plan.account_id,
                        "env": plan.env,
                        "sig": plan.signal_id,
                        "cid": plan.close_client_order_id,
                        "market": plan.market,
                        "side": plan.close_side,
                        "qty": int(plan.close_quantity),
                        "lim": plan.close_limit_price,
                        "placed_at": placed_at,
                        "retry_n": RETRY_N_PHASE_1,
                        "parent_id": plan.parent_entry_order_id,
                        "strat": plan.strategy_hash,
                        "param": plan.parameter_set_hash,
                    },
                )
            ).fetchone()
            assert close_row is not None
            close_order_id: UUID = close_row.id
    log_bound.info(
        "order_placement_exit_close_row_pre_inserted",
        close_order_id=str(close_order_id),
    )

    # Step 5: place the close order at IBKR.
    close_request = IbkrPlaceOrderRequest(
        client_order_id=plan.close_client_order_id,
        contract=contract,
        side=plan.close_side,
        quantity=plan.close_quantity,
        order_type="limit_marketable",
        limit_price=plan.close_limit_price,
        time_in_force="DAY",
        # No bracket — position is going to zero, no protective stop
        # needed. transmit=True is the dataclass default; explicit here
        # so the contract is unambiguous for the close leg.
        transmit=True,
    )
    close_failure_reason: str | None = None
    close_broker_result: Any = None
    try:
        close_broker_result = await _await_ibkr_with_timeout(
            ibkr_client.place_order(close_request),
            operation="placeOrder.exit_close",
            timeout_seconds=ibkr_call_timeout_seconds,
        )
        # Treat broker-side rejection same as a placement error for
        # the POSITION_UNPROTECTED gate — a rejected close is
        # operationally equivalent to a failed place (no working
        # close order exists).
        close_db_status = _broker_status_to_orders_status(close_broker_result.status)
        if close_db_status == "rejected":
            close_failure_reason = (
                f"broker rejection: {getattr(close_broker_result, 'rejection_detail', 'unknown')}"
            )
    except IbkrPlacementError as exc:
        close_failure_reason = f"IbkrPlacementError: {exc!r}"
        close_broker_result = None
        close_db_status = "pending"

    # Step 6: POSITION_UNPROTECTED branch. The cancel succeeded (step 2
    # didn't raise + step 3 UPDATE landed) so the bracket is OFF; if
    # the close didn't establish a working order at the broker, the
    # position is now NAKED.
    if close_failure_reason is not None:
        unprotected_payload: dict[str, Any] = {
            "signal_id": str(plan.signal_id),
            "account_id": str(plan.account_id),
            "market": plan.market,
            "prior_position_direction": plan.prior_position_direction,
            "prior_position_quantity": plan.prior_position_quantity,
            "cancelled_stop_client_order_id": plan.bracket_stop_client_order_id,
            "cancelled_stop_broker_order_id": plan.bracket_stop_broker_order_id,
            "last_known_stop_price": str(plan.bracket_stop_stop_price),
            "close_client_order_id": plan.close_client_order_id,
            "close_failure_reason": close_failure_reason,
            "linked_cancel_audit_event_uuid": str(cancel_audit_record.event_uuid),
            "observed_at_utc": placed_at.isoformat(),
        }
        async with session_factory() as unprotected_audit_session:
            unprotected_audit_record = await append_audit_event(
                unprotected_audit_session,
                AuditEventType.POSITION_UNPROTECTED,
                unprotected_payload,
                account_id=plan.account_id,
                env=plan.env,
                phase_at_emit=phase_at_emit,
                source_clock_ts=placed_at,
            )
        log_bound.error(
            "order_placement_exit_position_unprotected",
            close_failure_reason=close_failure_reason,
            unprotected_audit_event_uuid=str(unprotected_audit_record.event_uuid),
        )

        # Invoke the alert dispatch hook (best-effort; never blocks the
        # raise back to the worker since the audit row is the durable
        # record). The hook owns alerts-row INSERT + dispatch_alert.
        if alert_dispatch_hook is not None:
            descriptor = PositionUnprotectedAlertDescriptor(
                account_id=plan.account_id,
                env=plan.env,
                signal_id=plan.signal_id,
                market=plan.market,
                prior_position_direction=plan.prior_position_direction,
                prior_position_quantity=plan.prior_position_quantity,
                last_known_stop_price=plan.bracket_stop_stop_price,
                close_client_order_id=plan.close_client_order_id,
                close_failure_reason=close_failure_reason,
                triggering_audit_event_uuid=unprotected_audit_record.event_uuid,
            )
            try:
                await alert_dispatch_hook(descriptor)
            except Exception:
                log_bound.exception("order_placement_exit_alert_dispatch_hook_failed")
        else:
            log_bound.warning(
                "order_placement_exit_alert_dispatch_hook_unwired",
                note=(
                    "POSITION_UNPROTECTED audit row is durable, but no P0 "
                    "Discord/email push was sent — operator should monitor "
                    "audit page directly until the hook is wired in api/main.py."
                ),
            )

        # The pre-placed close orders row stays in 'pending' so the
        # operator can reconcile via TWS / replay tooling. Raise so
        # the worker logs the failure + stops trying this signal.
        raise OrderPlacementError(
            f"CLOSE_PLACEMENT_FAILED signal_id={plan.signal_id} "
            f"market={plan.market} reason={close_failure_reason}",
            error_code="CLOSE_PLACEMENT_FAILED",
            details={
                "signal_id": str(plan.signal_id),
                "market": plan.market,
                "close_failure_reason": close_failure_reason,
                "unprotected_audit_event_uuid": str(unprotected_audit_record.event_uuid),
            },
        )

    # ---- Place succeeded ----
    assert close_broker_result is not None  # narrow for type-check
    close_broker_order_id = close_broker_result.broker_order_id
    close_broker_status = close_broker_result.status
    close_rejection_detail = getattr(close_broker_result, "rejection_detail", None)

    # Step 7a: ORDER_PLACED audit for the close.
    placed_audit_payload: dict[str, Any] = {
        "signal_id": str(plan.signal_id),
        "account_id": str(plan.account_id),
        "client_order_id": plan.close_client_order_id,
        "broker_order_id": (
            str(close_broker_order_id) if close_broker_order_id is not None else None
        ),
        "market": plan.market,
        "side": plan.close_side,
        "quantity": str(plan.close_quantity),
        "order_type": "limit_marketable",
        "limit_price": str(plan.close_limit_price),
        "time_in_force": "DAY",
        "broker_status": close_broker_status,
        "rejection_category": getattr(close_broker_result, "rejection_category", None),
        "rejection_detail": close_rejection_detail,
        "placed_at_utc": placed_at.isoformat(),
        "leg": "exit_close",
        "linked_cancel_audit_event_uuid": str(cancel_audit_record.event_uuid),
    }
    async with session_factory() as placed_audit_session:
        placed_audit_record = await append_audit_event(
            placed_audit_session,
            AuditEventType.ORDER_PLACED,
            placed_audit_payload,
            account_id=plan.account_id,
            env=plan.env,
            phase_at_emit=phase_at_emit,
            source_clock_ts=placed_at,
        )

    # Step 7b: UPDATE close orders row + signals.status='working'.
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE orders SET broker_order_id = :bid, status = :status, "
                    "    rejection_reason = :rej_reason "
                    "WHERE id = :oid"
                ),
                {
                    "bid": (
                        str(close_broker_order_id) if close_broker_order_id is not None else None
                    ),
                    "status": close_db_status,
                    "rej_reason": close_rejection_detail,
                    "oid": close_order_id,
                },
            )
            await session.execute(
                text("UPDATE signals SET status = 'working' WHERE id = :sid"),
                {"sid": plan.signal_id},
            )

    log_bound.info(
        "order_placement_exit_completed",
        close_order_id=str(close_order_id),
        close_broker_order_id=(
            str(close_broker_order_id) if close_broker_order_id is not None else None
        ),
        close_broker_status=close_broker_status,
        close_db_status=close_db_status,
        cancel_audit_event_uuid=str(cancel_audit_record.event_uuid),
        placed_audit_event_uuid=str(placed_audit_record.event_uuid),
    )

    # Best-effort SSE emit so the web /signals page + Discord webhook
    # pusher observe the close placement. Mirror of the entry path's
    # signal-placed SSE.
    try:
        from services.api.sse import emit_sse

        await emit_sse(
            "signal",
            {
                "action": "placed",
                "signal_id": str(plan.signal_id),
                "market": plan.market,
                "direction": plan.prior_position_direction,  # the CLOSING direction
                "side": plan.close_side,
                "quantity": str(plan.close_quantity),
                "order_id": str(close_order_id),
                "client_order_id": plan.close_client_order_id,
                "broker_order_id": (
                    str(close_broker_order_id) if close_broker_order_id is not None else None
                ),
                "broker_status": close_broker_status,
                "db_status": close_db_status,
                "rejection_category": getattr(close_broker_result, "rejection_category", None),
                "rejection_detail": close_rejection_detail,
                "placed_at_utc": placed_at.isoformat(),
                "environment": plan.env,
                "audit_event_uuid": str(placed_audit_record.event_uuid),
                "exit_close": True,
                "cancel_audit_event_uuid": str(cancel_audit_record.event_uuid),
                "cancelled_stop_client_order_id": plan.bracket_stop_client_order_id,
            },
        )
    except Exception:
        log_bound.exception("order_placement_exit_sse_emit_failed")

    return OrderPlacementResult(
        signal_id=plan.signal_id,
        order_id=close_order_id,
        audit_event_uuid=placed_audit_record.event_uuid,
        broker_order_id=(str(close_broker_order_id) if close_broker_order_id is not None else None),
        status=close_db_status,
    )


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
    """Execute the placement plan: INSERT orders → IBKR placeOrder → audit → UPDATE orders.

    **PR-ζ restructure (2026-05-18, drill 7 defect #6 fix):** the orders
    row is INSERTed BEFORE the IBKR ``place_order`` call returns, with
    ``broker_order_id=NULL`` + ``status='pending'``. After IBKR responds,
    the row is UPDATEd with the assigned broker_order_id + final status.
    This closes the race where IBKR's first ``orderStatusEvent`` (which
    fires ms after place_order acks) reaches the fill_processor BEFORE
    the orders row would have been INSERTed in the prior post-place
    order. Drill 7 measured the race window at ~43ms; fill_processor's
    lookup by ``client_order_id`` raced and dropped the entry fill,
    requiring a manual ``replay_executions.py`` recovery.

    Sequential steps:

    1. **PR-ζ INSERT entry orders row (pre-place)** — broker_order_id=NULL,
       status='pending'. Committed before the IBKR side effect so the
       fill_processor's lookup-by-CID finds the row no matter how fast
       the orderStatusEvent fires.
    2. ``ibkr_client.place_order(entry_request)`` — the broker-side
       side effect for the entry leg. Failure raises
       :class:`IbkrPlacementError` (broker connectivity) or returns an
       ``IbkrPlaceOrderResult`` with ``status='rejected'`` (broker
       validation rejection). On failure the pre-placed orders row
       remains in 'pending' — recoverable via reconciliation or
       operator action. The await is bounded by
       ``ibkr_call_timeout_seconds`` (default 30s); expiry raises
       :class:`IbkrPlacementError` so the existing except path handles
       it cleanly.
    3. **Bracket extension (2026-05-17):** if entry placed successfully
       (status not in rejected set), INSERT the stop orders row
       (PR-ζ) THEN place the stop-market exit order via
       ``ibkr_client.place_order(stop_request)``. On stop failure
       → best-effort cancel the entry + raise
       :class:`OrderPlacementError("STOP_PLACEMENT_FAILED")`. The
       cancel itself is also wrapped in the timeout so a hanging cancel
       doesn't trap the worker after the original stop failure.
    4. Audit-first (per spec §2.10.1): write ``ORDER_PLACED`` x N
       (1 if entry rejected, 2 if entry + stop both placed) — each in
       its own SERIALIZABLE transaction. Payload includes the
       client_order_id + broker_order_id.
    5. **PR-ζ UPDATE orders rows (post-place)** + UPDATE
       ``signals.status`` in one transaction. The UPDATE sets
       broker_order_id + final status + rejection_reason.

    Returns an :class:`OrderPlacementResult` reflecting the ENTRY
    order's status (the stop's status is logged + audited but not
    surfaced in the worker's return value — Phase 2+ may add a
    bracket-aware result wrapper).
    """
    placed_at = datetime.now(tz=UTC)

    # PR-ζ Step 0: INSERT entry orders row PRE-place (broker_order_id=NULL,
    # status='pending'). The COMMIT happens on session-context exit, BEFORE
    # the IBKR place_order call. This is the drill 7 defect #6 fix —
    # closes the race where IBKR's first orderStatusEvent reached the
    # fill_processor before the orders row was INSERTed.
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
                            :acct, :env, :sig, :cid, NULL,
                            :market, :side, :order_type, :qty, :lim,
                            :placed_at, 'pending', NULL, :retry_n,
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
                        "market": plan.market,
                        "side": plan.side,
                        "order_type": plan.order_type,
                        "qty": int(plan.quantity),
                        "lim": plan.limit_price,
                        "placed_at": placed_at,
                        "retry_n": RETRY_N_PHASE_1,
                        "strat": plan.strategy_hash,
                        "param": plan.parameter_set_hash,
                    },
                )
            ).fetchone()
            assert entry_order_row is not None
            order_id: UUID = entry_order_row.id
    log.info(
        "order_placement_entry_row_pre_inserted",
        signal_id=str(plan.signal_id),
        order_id=str(order_id),
        client_order_id=plan.client_order_id,
        note="PR-ζ: orders row INSERTed pre-IBKR-place to close fill_processor race (defect #6).",
    )

    # Step 1: IBKR side effect — entry order.
    # PR-eta: transmit=False holds the entry in PendingSubmit at IBKR until
    # the stop arrives with transmit=True + parentId=<entry_broker_order_id>.
    # IBKR then releases both atomically. This is the proper fix for
    # drill 9's Error 201 race (entry filled before parentId could register).
    # Single-order callers (operator-flatten via the manual close path,
    # recon-driven cancels) keep the default transmit=True.
    entry_request = IbkrPlaceOrderRequest(
        client_order_id=plan.client_order_id,
        contract=contract,
        side=plan.side,
        quantity=plan.quantity,
        order_type=plan.order_type,
        limit_price=plan.limit_price,
        time_in_force=plan.time_in_force,
        transmit=False,
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
            order_id=str(order_id),
            error=str(exc),
        )
        # The pre-placed orders row stays in 'pending' with broker_order_id=NULL.
        # Operator reconciles via the IBKR-side audit (FlexQuery / TWS) — the
        # row signals "we tried to place this but the broker call failed."
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
    stop_order_id: UUID | None = None
    if place_stop:
        # PR-ζ Step 0b: INSERT stop orders row PRE-place (broker_order_id=NULL,
        # status='pending', parent_order_id=entry's id). Same race-fix
        # reasoning as the entry pre-INSERT above.
        async with session_factory() as session:
            async with session.begin():
                stop_order_row = (
                    await session.execute(
                        text(
                            """
                            INSERT INTO orders (
                                account_id, env, signal_id, client_order_id, broker_order_id,
                                market, direction, order_type, quantity, stop_price,
                                placed_at_utc, status, rejection_reason, retry_n,
                                parent_order_id, strategy_hash, parameter_set_hash
                            ) VALUES (
                                :acct, :env, :sig, :cid, NULL,
                                :market, :side, 'stop_market', :qty, :stop_px,
                                :placed_at, 'pending', NULL, :retry_n,
                                :parent_id, :strat, :param
                            )
                            RETURNING id
                            """
                        ),
                        {
                            "acct": plan.account_id,
                            "env": plan.env,
                            "sig": plan.signal_id,
                            "cid": plan.stop_client_order_id,
                            "market": plan.market,
                            "side": plan.stop_side,
                            "qty": int(plan.quantity),
                            "stop_px": plan.stop_price,
                            "placed_at": placed_at,
                            "retry_n": RETRY_N_PHASE_1,
                            "parent_id": order_id,
                            "strat": plan.strategy_hash,
                            "param": plan.parameter_set_hash,
                        },
                    )
                ).fetchone()
                assert stop_order_row is not None
                stop_order_id = stop_order_row.id
        log.info(
            "order_placement_stop_row_pre_inserted",
            signal_id=str(plan.signal_id),
            stop_order_id=str(stop_order_id),
            stop_client_order_id=plan.stop_client_order_id,
            entry_order_id=str(order_id),
            note="PR-ζ: stop orders row INSERTed pre-IBKR-place (race fix).",
        )

        stop_request = IbkrPlaceOrderRequest(
            client_order_id=plan.stop_client_order_id,
            contract=contract,
            side=plan.stop_side,
            quantity=plan.quantity,
            order_type="stop_market",
            stop_price=plan.stop_price,
            time_in_force="GTC",
            parent_client_order_id=plan.client_order_id,
            # PR-eta (2026-05-19): re-enable PR-gamma's IBKR-side parentId
            # wiring now that the entry leg above carries transmit=False.
            # IBKR holds the parent in PendingSubmit until this stop arrives
            # with transmit=True + parentId=<entry_broker_order_id>, then
            # releases both atomically. The drill 9 race (entry filled
            # before parentId could validate) cannot recur because the
            # parent is gated on the bracket being fully wired.
            parent_broker_order_id=broker_order_id,
            # transmit=True is the dataclass default; explicit here for
            # readability — this is the leg that releases the entire bracket
            # to the exchange when IBKR validates the parent/child pair.
            transmit=True,
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
                stop_order_id=str(stop_order_id),
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

    # PR-ζ Step 4: UPDATE the pre-placed orders rows with broker_order_id +
    # final status + rejection_reason. The rows were INSERTed in steps 0/0b
    # BEFORE the IBKR call (drill 7 defect #6 fix). Single transaction so
    # the business-data view is consistent + the signals UPDATE lands
    # atomically with both orders UPDATEs.
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    UPDATE orders
                    SET broker_order_id = :bid,
                        status = :status,
                        rejection_reason = :rej_reason
                    WHERE id = :oid
                    """
                ),
                {
                    "bid": str(broker_order_id) if broker_order_id is not None else None,
                    "status": db_status,
                    "rej_reason": rejection_detail,
                    "oid": order_id,
                },
            )

            if stop_broker_result is not None:
                assert stop_order_id is not None
                await session.execute(
                    text(
                        """
                        UPDATE orders
                        SET broker_order_id = :bid,
                            status = :status,
                            rejection_reason = :rej_reason
                        WHERE id = :oid
                        """
                    ),
                    {
                        "bid": (
                            str(stop_broker_result.broker_order_id)
                            if stop_broker_result.broker_order_id is not None
                            else None
                        ),
                        "status": stop_db_status,
                        "rej_reason": getattr(stop_broker_result, "rejection_detail", None),
                        "oid": stop_order_id,
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
        position_unprotected_alert_hook: PositionUnprotectedAlertHook | None = None,
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
        # Exit-pipeline PR-C (2026-05-27): optional callback the
        # exit-branch invokes when bracket-cancel succeeds + close-place
        # fails (POSITION_UNPROTECTED P0 alert path). When unwired the
        # audit row still lands; only the Discord/email fan-out is
        # skipped (with a structured WARNING in the worker log). Hook
        # owner: ``services/api/main.py`` lifespan startup.
        self._position_unprotected_alert_hook = position_unprotected_alert_hook
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
                if signal.signal_type == "exit":
                    placed += await self._dispatch_exit_signal(signal)
                else:
                    placed += await self._dispatch_entry_signal(signal)
            except IbkrPlacementError as exc:
                # Broker connectivity error — log, leave the signal in
                # 'approved' for the next poll cycle to retry. This also
                # catches the _await_ibkr_with_timeout-translated
                # TimeoutError from resolveContract / placeOrder above.
                self._log.error(
                    "order_placement_broker_unavailable",
                    signal_id=str(signal.signal_id),
                    signal_type=signal.signal_type,
                    error=str(exc),
                )
                # Don't continue iterating — broker is down, fail fast.
                break
        return placed

    async def _dispatch_entry_signal(self, signal: ApprovedSignalRow) -> int:
        """Original (pre-PR-C) entry-path dispatch. Returns 1 on success,
        0 on a planner-side error.

        Extracted to a helper so the new exit branch (added 2026-05-27)
        sits beside it cleanly + run_once stays readable.
        """
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
            return 1
        except OrderPlacementError as exc:
            # Pure-policy planner error — the signal shape is wrong.
            # Log + skip; an operator-side correction is needed.
            self._log.error(
                "order_placement_plan_error",
                signal_id=str(signal.signal_id),
                signal_type="entry",
                error=str(exc),
            )
            return 0

    async def _dispatch_exit_signal(self, signal: ApprovedSignalRow) -> int:
        """Exit-pipeline PR-C (2026-05-27): dispatch an approved
        ``signal_type='exit'`` signal.

        Sequence:

          1. Look up the open trade for ``(account_id, market)`` to
             resolve the ENTRY signal_id. If absent → log + flip
             signals.status='cancelled' (the position was already
             closed by some other path; nothing to do).
          2. Look up the bracket-stop orders row for the entry. If
             absent → log + flip signals.status='cancelled' (the
             bracket already fired or was never placed; either way
             we have no stop to cancel + no need to close).
          3. Read FRESH positions_current.quantity. If 0 →
             flip signals.status='cancelled' WITHOUT cancelling the
             bracket (because if positions=0, the bracket already
             closed on its own).
          4. Call :func:`plan_exit_close_placement` + then
             :func:`apply_exit_close_placement`. The apply step is
             where audit-first ORDER_CANCELLED + ibkr_client.cancel +
             pre-place + place + POSITION_UNPROTECTED branch all
             land.

        Returns 1 on a successful close placement (even if the broker
        accepted-but-pending), 0 on the no-op or recoverable-failure
        paths. IbkrPlacementError propagates back to ``run_once``
        which treats it as "broker down" + fails fast.
        """
        log_bound = self._log.bind(
            signal_id=str(signal.signal_id),
            market=signal.market,
            signal_type="exit",
        )

        # Step 1: resolve the entry signal_id via the open trade.
        entry_signal_id = await fetch_entry_signal_id_for_open_trade(
            self._session_factory,
            account_id=signal.account_id,
            market=signal.market,
        )
        if entry_signal_id is None:
            log_bound.warning(
                "order_placement_exit_no_open_trade",
                note=(
                    "No open_position trade row for this (account, market). "
                    "Position may have already closed (bracket fired or "
                    "operator-flatten). Flipping signals.status='cancelled' "
                    "without placing a close order."
                ),
            )
            await self._flip_signal_to_cancelled(
                signal.signal_id,
                reason="exit_no_open_trade",
            )
            return 0

        # Step 2: resolve the bracket-stop orders row.
        bracket_stop = await fetch_open_bracket_stop_for_entry(
            self._session_factory,
            entry_signal_id=entry_signal_id,
        )
        if bracket_stop is None:
            log_bound.warning(
                "order_placement_exit_no_open_bracket_stop",
                entry_signal_id=str(entry_signal_id),
                note=(
                    "No working bracket-stop orders row for the entry signal. "
                    "Bracket may have already fired or was never placed. "
                    "Flipping signals.status='cancelled'."
                ),
            )
            await self._flip_signal_to_cancelled(
                signal.signal_id,
                reason="exit_no_open_bracket_stop",
            )
            return 0

        # Step 3: FRESH positions_current read.
        current_qty = await fetch_current_position_quantity(
            self._session_factory,
            account_id=signal.account_id,
            market=signal.market,
        )
        if current_qty == 0:
            # Position already flat — don't cancel the bracket (if it
            # still exists, it'll close on its own or the next cycle's
            # recon will reconcile). Flip status='cancelled' per the
            # operator-locked decision (no new signals.status value
            # required; no audit row required either, just a structured
            # log entry the operator can grep).
            log_bound.warning(
                "order_placement_exit_position_already_flat",
                entry_signal_id=str(entry_signal_id),
                bracket_stop_order_id=str(bracket_stop.order_id),
                note=(
                    "positions_current.quantity is 0 at place-order time; "
                    "skipping bracket cancel + close placement. "
                    "Signal flips to status='cancelled'."
                ),
            )
            await self._flip_signal_to_cancelled(
                signal.signal_id,
                reason="exit_position_already_flat",
            )
            return 0

        # Step 4: plan + apply the close placement.
        try:
            plan = plan_exit_close_placement(
                signal,
                bracket_stop=bracket_stop,
                current_position_quantity=current_qty,
                env=self._env,
            )
        except OrderPlacementError as exc:
            # POSITION_ALREADY_FLAT is guarded above so this path is
            # for genuine planner shape errors (unknown market tick,
            # wrong signal_type slipping through, etc.). Log + skip;
            # the signal stays 'approved' for the operator to
            # investigate. They can manually flip status='cancelled'
            # via psql if it's a known dead-end.
            log_bound.error(
                "order_placement_exit_plan_error",
                entry_signal_id=str(entry_signal_id),
                error_code=exc.error_code,
                error=str(exc),
            )
            return 0

        contract = await _await_ibkr_with_timeout(
            self._ibkr_client.resolve_contract(plan.market),
            operation="resolveContract.exit",
            timeout_seconds=self._ibkr_call_timeout_seconds,
        )

        try:
            await apply_exit_close_placement(
                plan,
                ibkr_client=self._ibkr_client,
                contract=contract,
                session_factory=self._session_factory,
                alert_dispatch_hook=self._position_unprotected_alert_hook,
                ibkr_call_timeout_seconds=self._ibkr_call_timeout_seconds,
            )
            return 1
        except OrderPlacementError as exc:
            # CLOSE_PLACEMENT_FAILED already emitted the
            # POSITION_UNPROTECTED audit + alert (in apply); the
            # pre-placed close orders row is in 'pending'; operator
            # must reconcile. Log here is the worker-side breadcrumb
            # that the iteration completed (even though the close
            # didn't get to 'working'). Signal stays at
            # 'approved'-but-orders-row-exists; the LEFT JOIN in
            # fetch_approved_signals means this signal will NOT be
            # re-polled (orders row INSERTed pre-place excludes it).
            # Operator has to intervene manually.
            log_bound.error(
                "order_placement_exit_apply_error",
                entry_signal_id=str(entry_signal_id),
                error_code=exc.error_code,
                error=str(exc),
            )
            return 0

    async def _flip_signal_to_cancelled(self, signal_id: UUID, *, reason: str) -> None:
        """Flip ``signals.status`` to 'cancelled' for an exit signal we
        cannot dispatch (no open trade, no open bracket, or position
        already flat).

        Per operator decision 2026-05-27: reuse the existing
        'cancelled' status value rather than add a new
        'position_already_flat' value via alembic CHECK migration.
        The forensic distinction lives in the structured log entry
        (``reason`` kwarg).

        No audit event is emitted — the SIGNAL_EMITTED row from the
        original emit is durable + the operator can correlate via
        signal_id. Adding an "ORDER_DROPPED" audit type would require
        an A02 enum extension + tests; deferred unless operational
        experience surfaces a forensic need.
        """
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "UPDATE signals SET status = 'cancelled' "
                        "WHERE id = :sid AND status = 'approved'"
                    ),
                    {"sid": signal_id},
                )
        self._log.info(
            "order_placement_exit_signal_cancelled",
            signal_id=str(signal_id),
            reason=reason,
        )

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
    "BracketStopRow",
    "ExitClosePlan",
    "OrderPlacementError",
    "OrderPlacementPlan",
    "OrderPlacementResult",
    "OrderPlacementWorker",
    "PositionUnprotectedAlertDescriptor",
    "PositionUnprotectedAlertHook",
    "apply_exit_close_placement",
    "apply_order_placement",
    "fetch_approved_signals",
    "fetch_current_position_quantity",
    "fetch_entry_signal_id_for_open_trade",
    "fetch_open_bracket_stop_for_entry",
    "plan_exit_close_placement",
    "plan_order_placement",
]
