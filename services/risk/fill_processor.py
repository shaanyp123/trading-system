"""services/risk/fill_processor.py — IBKR fill → backend-table propagation.

PR-G (post-pivot 2026-05-13). When an IBKR ``orderStatus`` event arrives
with ``status='filled'``, the previous wiring at
:meth:`services.risk.order_placement_worker.OrderPlacementWorker._on_order_status`
emitted a ``fill`` SSE envelope only — the ``fills`` / ``orders.status`` /
``positions_current`` / ``balances`` / ``trades`` tables stayed empty.
This module closes that gap.

The split mirrors the plan-then-apply convention used by
:mod:`services.risk.signal_dispatch`,
:mod:`services.reconciliation.recon`, and
:mod:`services.risk.dispatch`:

  * :func:`plan_fill_application` — pure policy. Takes the FillContext +
    FillIngestPayload + prior position/balance state and returns a
    :class:`FillApplyPlan` describing every audit event + table mutation
    the fill induces. No I/O.
  * :func:`apply_fill_plan` — I/O orchestrator. Writes audit events
    (audit-first per backend-spec §2.10.1, each in its own SERIALIZABLE
    transaction), then INSERTs ``fills``, UPDATEs ``orders.status``,
    UPSERTs ``positions_current``, INSERTs ``balances`` snapshot, and
    INSERT/UPDATEs the linked ``trades`` row. Returns a result dataclass
    with the minted audit_event_uuids + ids of the new rows for downstream
    SSE emission.

**Scenario classification** (since 2026-05-17 exit-path extension):

  * ``ENTRY`` — prior position is zero OR same-direction; fill grows
    the position. Emits ORDER_FILLED → POSITION_OPENED/POSITION_UPDATED
    → BALANCE_SNAPSHOT_RECORDED → optional TRADE_OPENED.
  * ``EXIT_FULL_CLOSE`` — opposite-direction fill that takes ``|prior|``
    to exactly zero (the bracket-order stop fires, or LEAN emits an
    explicit close signal). Emits ORDER_FILLED → POSITION_CLOSED →
    BALANCE_SNAPSHOT_RECORDED → TRADE_CLOSED. The position row is
    DELETEd; the trade row is UPDATEd with state='closed' +
    realized_pnl_usd + avg_exit_price + closed_at_utc + exit_order_id.
  * ``EXIT_PARTIAL`` (deferred Phase 2+) — opposite fill that reduces
    but doesn't reach zero. Raises :class:`UnsupportedFillScenarioError`.
  * ``EXIT_REVERSAL`` (deferred Phase 2+) — opposite fill that crosses
    zero into a new opposite-direction position. Raises.

For the EXIT_FULL_CLOSE path, the primary lookup convention is
**same-signal-id** — the bracket-order stop placed in the LEAN/CME era
carried ``signal_id = entry_signal.id`` (Option B per the 2026-05-17
session brief), so :func:`fetch_prior_trade(entry_signal_id=
context.signal_id)` resolved the open trade for entry-add and stop-fill
paths without extra plumbing.

**Market-level fallback (2026-07-12 incident):** the C1 strategy worker
mints a FRESH signal row for every leg, including decision-driven close
legs — so a close fill arrives under a signal_id that never opened any
trade and the same-signal lookup misses. The first live decision-driven
close (2026-07-12 00:05 UTC) hit exactly this: ``EXIT_NO_PRIOR_TRADE``
raised AFTER the venue fill, the decision died, and the un-propagated
fill surfaced as a recon break + auto-halt. The orchestrator now falls
back to the open trade row for ``(account_id, market)`` when the
same-signal lookup returns None (:func:`fetch_prior_open_trade_for_market`)
— well-defined because the system holds at most one open trade per
market. The same fallback makes same-direction ADD legs attach to the
existing open trade instead of minting a duplicate. The raise now means
the market genuinely has no open trade — real corruption, not a
linkage-convention mismatch.

**Audit event taxonomy** (`services/audit/event_types.py`):

  * ``ORDER_FILLED`` — the broker-side confirmation. Carries
    cumulative_filled_quantity + avg_fill_price + commission.
  * ``POSITION_OPENED`` — first non-zero quantity for an
    (account, market, contract_id) tuple.
  * ``POSITION_UPDATED`` — quantity change without reaching zero
    (same-direction adds).
  * ``POSITION_CLOSED`` — quantity reaches zero (exit-close path).
  * ``BALANCE_SNAPSHOT_RECORDED`` — new ``balances`` row with the cash +
    NAV deltas from the fill.
  * ``TRADE_OPENED`` — first fill against a signal (state=open_position).
  * ``TRADE_CLOSED`` — exit fill brings position to zero; realized P&L
    + state='closed' stamped.

Subsequent fills against the same signal (e.g., a 3-of-5 partial + a
2-of-5 completion when the IBKR adapter re-fires Filled with cumulative
totals — note: the worker's contract is that we act ONCE per
broker_order_id) only emit ORDER_FILLED + POSITION_UPDATED +
BALANCE_SNAPSHOT_RECORDED; the trade row is UPDATEd in place without an
event because TRADE_REALIZED (Phase 1+) is the lifecycle-final marker
the operator cares about.

**A02 BINDS** — ``services/risk/**`` is on the forbidden-modification
whitelist per dev-guide §11; PR carries the ``risk-review-approved`` label.

**A01 enforced** — all audit writes route through
:func:`services.audit.writer.append_audit_event`.
**A04 enforced** — every emitted ``event_type`` is in the locked taxonomy.
**A05 enforced** — Decimals throughout; floats raise via JCS canonicaliser.
**A06 enforced** — every datetime tz-aware UTC.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Final, Literal
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from services.audit.event_types import AuditEventType
from services.audit.writer import Environment, PhaseAtEmit, append_audit_event

log = structlog.get_logger()


#: Source string for the ``balances`` row INSERTed per fill. Constrained
#: by the table's CHECK in alembic 0002 (extended by the 2026-07-09
#: ``coinbase_recon_src`` migration): ``qc_objectstore | tws_api |
#: flexquery_eod | coinbase_eod``. ``tws_api`` is the IBKR-era per-fill
#: source string; the Coinbase re-feed of this processor (crypto-pivot
#: C1 fill wiring) revisits it. The end-of-day reconciliation refresh
#: writes ``coinbase_eod`` instead
#: (``services/reconciliation/eod_cycle.py::BALANCE_SOURCE_FROM_COINBASE``).
BALANCE_SOURCE_FROM_FILL: Final[str] = "tws_api"


#: Quantization for avg_cost recomputation. positions_current.avg_cost is
#: NUMERIC(20, 8); we round to 8 decimal places half-even to match.
AVG_COST_QUANTIZER: Final[Decimal] = Decimal("0.00000001")


# ---------------------------------------------------------------------------
# Input dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FillContext:
    """Pre-resolved context for one IBKR Filled status.

    The I/O step (``apply_fill_plan``'s caller) looks this up from the
    ``orders`` + ``signals`` tables BEFORE invoking the planner. All
    fields are required + non-nullable except ``contract_id`` which is
    NULL for non-futures markets per the alembic 0002 schema.
    """

    order_id: UUID
    order_created_at: datetime
    """orders table is partitioned by created_at; UPDATEs need both id +
    created_at to address the partition. The lookup helper joins on
    orders_id_idx + reads created_at into the FillContext."""

    signal_id: UUID
    account_id: UUID
    env: Environment
    market: str
    contract_id: UUID | None
    signal_direction: Literal["long", "short", "flat"]
    """Direction from the signals row. PR-G entry case: matches the
    order direction (long→buy, short→sell). Exit-pipeline PR-C
    (2026-05-27): widened to include 'flat' for ``signal_type='exit'``
    signals (the strategy emits direction=FLAT for explicit exits per
    Docs/exit-pipeline-design.md §5.2). The classifier branches on
    same_direction_add vs opposite-direction using
    ``prior_position_quantity * order_direction`` sign relationship —
    signal_direction is NOT load-bearing for the exit branch. For the
    entry branch, the classifier raises
    :class:`UnsupportedFillScenarioError` when signal_direction='flat'
    (orphan exit fill reaching the entry path is an invariant
    violation upstream)."""

    order_direction: Literal["buy", "sell"]
    """Direction from the orders row — the actual buy/sell flag IBKR
    received. PR-G entry case asserts long→buy, short→sell."""

    target_contracts: int
    """The signal's planned contract count. We compare ``cumulative_filled``
    against this to decide whether the order is now ``filled`` (==target)
    or ``partially_filled`` (<target)."""

    strategy_hash: str
    parameter_set_hash: str
    slippage_calibration_version_id: UUID
    decision_price: Decimal
    managed_by_version: str
    """CHAR(40) — same value as strategy_hash today; the slot is reserved
    for a future strategy-version registry (backend-spec §3.13)."""

    multiplier: Decimal = Decimal(1)
    """Contract multiplier (points → USD scaling). Sourced from
    ``contracts.multiplier`` per backend-spec §3.29. ``Decimal(1)`` for
    ETFs (where ``contract_id is None``); ``Decimal(5)`` for /MES,
    ``Decimal(2)`` for /MNQ, ``Decimal(10)`` for /MGC, etc.

    Threaded through the planner so:

      * ``realized_pnl_usd = (exit - entry) * qty * multiplier`` —
        without this scaling, futures trades record only the raw price
        delta as P&L (e.g., /MES 7375→7368.5 = -6.5 instead of the
        actual -$32.50 cash loss). The column name ``trades.realized_pnl_usd``
        carries the canonical ``_usd`` suffix per spec §3.6 + the
        consistent ``_usd``-means-cash convention elsewhere in the
        schema (``cash_usd``, ``dividend_pnl_usd``, etc.).
      * ``cash_delta`` semantics — futures only cross cash via realized
        P&L + commission (initial margin is held in a separate column);
        ETFs flow the full notional. ``context.contract_id is not None``
        is the canonical futures discriminator (alembic 0002 leaves
        ``orders.contract_id`` nullable; futures populate it via the
        contracts table FK).
      * ``post_fill_position_mtm_usd`` — ``quantity * fill_price *
        multiplier`` so NAV math composes correctly across mixed
        ETF+futures portfolios.

    Defaults to ``Decimal(1)`` so test fixtures + any historical caller
    that doesn't supply it preserves the ETF-shaped semantics. The
    real-world producer is :func:`fetch_fill_context` which LEFT JOINs
    ``contracts`` + COALESCEs to 1.
    """

    signal_type: Literal["entry", "exit"] = "entry"
    """Exit-pipeline PR-C (2026-05-27). Plumbed through from the
    ``signals.signal_type`` column via :func:`fetch_fill_context`.
    Today's planner reads this only for audit-trail observability
    (the scenario classification keys on direction/quantity geometry,
    not signal_type); future PRs may use it to differentiate
    EXIT_REVERSAL handling from EXIT_FULL_CLOSE more cleanly."""


@dataclass(frozen=True, slots=True)
class FillIngestPayload:
    """Inputs from one IBKR Filled OrderStatusUpdate.

    Field semantics match :class:`services.execution.types.OrderStatusUpdate`
    except already coerced to native types (Decimals + tz-aware UTC).
    Constructed by the I/O step from the adapter's update.
    """

    broker_fill_id: str
    """A stable identifier for this fill, used as the
    ``fills.broker_fill_id`` column. Conventionally
    ``f"{broker_order_id}:{fill_sequence_no}"``; for IBKR which aggregates
    fills into Filled status events, we use ``f"{broker_order_id}:agg"``
    so re-firing on reconnect doesn't insert a second row (UNIQUE
    constraint catches the dupe)."""

    cumulative_filled_quantity: int
    """IBKR's running total for the order — what this Filled event says
    is now filled. Magnitude (always positive)."""

    fill_quantity: int
    """The delta — how many contracts this Filled event represents
    relative to any prior PartiallyFilled status we observed. PR-G
    contract: the worker acts ONCE per broker_order_id (dedupe set), so
    fill_quantity equals cumulative_filled_quantity in practice. The
    distinction matters when a future PR (partial-fill handling) starts
    treating PartiallyFilled events too."""

    fill_price: Decimal
    """Volume-weighted average fill price for the order. Mapped to
    ``fills.fill_price`` AND used for the position avg_cost recompute."""

    commission_usd: Decimal
    """Total commission across all fills for this order, in USD. Mapped
    to ``fills.commission``."""

    filled_at_utc: datetime
    """tz-aware UTC. Mapped to ``fills.filled_at_utc``. The IBKR adapter
    uses the last_fill_at_utc if available else observed_at_utc."""


@dataclass(frozen=True, slots=True)
class PriorPositionRow:
    """Snapshot of an existing ``positions_current`` row for the same
    (account_id, market, contract_id) tuple.

    Returned by the lookup helper as ``None`` when no row exists — the
    fill is opening a new position. Quantity is signed (long=positive,
    short=negative).
    """

    position_id: UUID
    quantity: int
    avg_cost: Decimal


@dataclass(frozen=True, slots=True)
class PriorTradeRow:
    """Snapshot of an existing OPEN trade row for the same signal.

    Returned by the lookup helper as ``None`` when no trade row exists
    yet (first fill on this signal). For the entry-add path: this fill
    grows the trade; quantity + avg_entry_price recomputed. For the
    exit-close path: this snapshot supplies the entry-side cost basis
    + accumulated commission used to compute realized P&L.
    """

    trade_id: UUID
    trade_created_at: datetime
    """trades table is partitioned by created_at; UPDATEs need both id +
    created_at to address the partition."""

    total_quantity: int
    avg_entry_price: Decimal
    realized_commission_usd: Decimal
    """Cumulative commission across all fills on this trade so far. For
    the exit-close path the planner adds the exit fill's commission to
    this value to compute the trade's total commission against gross
    P&L. NUMERIC(20,4) per the trades schema."""


@dataclass(frozen=True, slots=True)
class PriorBalanceSnapshot:
    """Snapshot of the most-recent ``balances`` row for this account.

    Returned by the lookup helper as a zero-prior :class:`PriorBalanceSnapshot`
    (cash_usd=Decimal(0), net_liquidation=Decimal(0)) when no row exists
    — the new snapshot ends up as the first row.
    """

    cash_usd: Decimal
    net_liquidation: Decimal


# ---------------------------------------------------------------------------
# Plan output dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PendingAuditEvent:
    """An audit event the apply step will write via append_audit_event.

    Mirrors the shape used by
    :mod:`services.reconciliation.recon.PendingAuditEvent` so callers
    that aggregate plans across modules can treat both uniformly.
    """

    event_type: AuditEventType
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class OrderStatusChange:
    """The ``UPDATE orders SET status=...`` mutation the plan prescribes."""

    order_id: UUID
    order_created_at: datetime
    new_status: Literal["filled", "partially_filled"]


@dataclass(frozen=True, slots=True)
class FillInsert:
    """The new ``fills`` row to INSERT.

    ``id`` is generated by the DB via ``uuid_generate_v7()`` default;
    captured by the apply step via RETURNING.
    """

    account_id: UUID
    env: str
    order_id: UUID
    broker_fill_id: str
    filled_at_utc: datetime
    fill_price: Decimal
    fill_quantity: int
    commission_usd: Decimal
    exchange_fee_usd: Decimal


PositionAction = Literal["open", "update", "close"]


@dataclass(frozen=True, slots=True)
class PositionMutation:
    """The ``positions_current`` mutation the plan prescribes.

    ``action='open'`` → INSERT new row.
    ``action='update'`` → UPDATE existing row.
    ``action='close'`` → DELETE existing row (out of PR-G scope; the
    planner raises before producing this).
    """

    action: PositionAction
    position_id: UUID | None
    """Set on action='update' / 'close'. None on 'open' (DB generates)."""

    account_id: UUID
    market: str
    contract_id: UUID | None
    new_quantity: int
    new_avg_cost: Decimal
    last_mark_ts: datetime
    managed_by_version: str


@dataclass(frozen=True, slots=True)
class BalanceInsert:
    """The new ``balances`` row to INSERT.

    Decimals quantized per the table's NUMERIC(20,4) precision; the
    apply step does no additional rounding.
    """

    account_id: UUID
    snapshot_ts: datetime
    net_liquidation: Decimal
    cash_usd: Decimal
    excess_liquidity: Decimal
    used_margin_pct: Decimal
    source: str
    """Constrained by the alembic 0002 CHECK; always
    :data:`BALANCE_SOURCE_FROM_FILL` in this module."""


TradeAction = Literal["open", "update", "close"]


@dataclass(frozen=True, slots=True)
class TradeMutation:
    """The ``trades`` mutation the plan prescribes.

    Actions:

    * ``action='open'`` — first fill on signal; INSERT trades row.
    * ``action='update'`` — subsequent same-direction fill; UPDATE
      ``total_quantity`` + ``avg_entry_price`` + accumulate commission.
    * ``action='close'`` — exit fill brings position to zero; UPDATE
      ``state='closed'`` + stamp ``closed_at_utc`` + ``exit_order_id``
      + ``avg_exit_price`` + ``realized_pnl_usd`` + accumulate commission.
    """

    action: TradeAction
    trade_id: UUID | None
    """Set on action='update' / 'close'. None on 'open' (DB generates)."""

    trade_created_at: datetime | None
    """Partition key; needed for UPDATE/DELETE on partitioned table.
    Set on action='update'/'close', None on 'open'."""

    account_id: UUID
    env: str
    market: str
    contract_id: UUID | None
    entry_signal_id: UUID
    entry_order_id: UUID
    direction: Literal["long", "short"]
    opened_at_utc: datetime
    """Set to filled_at_utc on action='open'; preserved on 'update'/'close'."""

    new_total_quantity: int
    new_avg_entry_price: Decimal
    realized_commission_usd: Decimal
    """For 'open': total commission on this trade (= entry fill's commission).
    For 'update' / 'close': the FILL's commission DELTA; the apply step's
    UPDATE SQL accumulates via ``realized_commission_usd +
    :comm`` so the prior trade's running total is preserved."""

    managed_by_version: str
    strategy_hash: str
    parameter_set_hash: str
    slippage_calibration_version_id: UUID

    # ----- Close-action only fields (None on 'open'/'update') -----

    closed_at_utc: datetime | None = None
    """Tz-aware UTC when the trade closed. Set on action='close';
    typically equal to the exit fill's ``filled_at_utc``."""

    exit_order_id: UUID | None = None
    """The orders.id of the exit-side order (the stop-loss order whose
    fill triggered the close, or the explicit exit order). Set on
    action='close'."""

    avg_exit_price: Decimal | None = None
    """Volume-weighted average exit price; NUMERIC(20,8). Set on
    action='close'. PR-G full-close path: equals the exit fill's
    fill_price (single exit fill assumption)."""

    realized_pnl_usd: Decimal | None = None
    """Quantized to NUMERIC(20,4). Computed as
    ``(avg_exit - avg_entry) * qty - total_commission`` for longs,
    ``(avg_entry - avg_exit) * qty - total_commission`` for shorts.
    Set on action='close'."""


@dataclass(frozen=True, slots=True)
class FillApplyPlan:
    """Pure-policy result of :func:`plan_fill_application`.

    Audit events FIRST per spec §2.10.1; table mutations follow. The
    apply orchestrator iterates through the events writing each in its
    own SERIALIZABLE transaction, then executes the table mutations.
    """

    audit_events: tuple[PendingAuditEvent, ...]
    order_change: OrderStatusChange
    fill_insert: FillInsert
    position_mutation: PositionMutation
    balance_insert: BalanceInsert
    trade_mutation: TradeMutation


@dataclass(frozen=True, slots=True)
class FillApplyResult:
    """Outcome of :func:`apply_fill_plan`.

    Carries the minted audit_event_uuids in declared order + the new
    PKs of inserted rows so the caller (the worker) can emit an SSE
    envelope with the linkage stamped.
    """

    audit_event_uuids: tuple[UUID, ...]
    signal_id: UUID
    """The entry_signal_id from the FillContext — echoed here so the
    SSE consumer + #fills Discord embed can route to the operator's web
    /signals deep link without a second DB roundtrip."""

    account_id: UUID
    """Echoed from FillContext for symmetry with signal_id."""

    fill_id: UUID
    position_id: UUID
    """Either the newly-INSERTed row's id (action='open') or the prior
    row's id (action='update')."""

    trade_id: UUID
    """Either the newly-INSERTed row's id (action='open') or the prior
    row's id (action='update')."""

    balance_id: UUID
    new_order_status: Literal["filled", "partially_filled"]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FillProcessingError(Exception):
    """Raised for terminal-failure fill scenarios.

    The worker catches this + logs at ERROR; the audit + fills + orders
    rows that landed BEFORE the failure remain durable. The operator
    reconciles via the Audit page + psql.
    """

    def __init__(
        self, error_code: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.details = details or {}


class UnsupportedFillScenarioError(FillProcessingError):
    """Raised when the fill is outside PR-G scope (entry case only).

    Concretely: a fill whose side opposes an existing position
    direction (would reduce qty toward zero — the exit case). The worker
    catches + logs; the operator's manual-reconcile flow handles it.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(error_code="FILL_EXIT_CASE_DEFERRED", message=message, details=details)


# ---------------------------------------------------------------------------
# Pure policy
# ---------------------------------------------------------------------------


def _quantize_avg_cost(value: Decimal) -> Decimal:
    """ROUND_HALF_EVEN to 8 decimal places (positions_current.avg_cost
    is NUMERIC(20,8))."""
    return value.quantize(AVG_COST_QUANTIZER, rounding=ROUND_HALF_EVEN)


def _recompute_avg_cost(
    prior_quantity: int,
    prior_avg: Decimal,
    fill_quantity: int,
    fill_price: Decimal,
) -> Decimal:
    """Compute the new avg_cost after adding ``fill_quantity`` at
    ``fill_price`` to a position of (prior_quantity, prior_avg).

    Both prior_quantity + fill_quantity use MAGNITUDE here (signs
    cancel; the planner asserts the directions match before calling).
    Returns the new avg quantized to NUMERIC(20,8).
    """
    if prior_quantity == 0:
        return _quantize_avg_cost(fill_price)
    new_total_qty = prior_quantity + fill_quantity
    if new_total_qty == 0:
        # Degenerate path: should not happen in PR-G entry case (the
        # planner guards this with the direction-match check before
        # calling). Defensive return matches the prior avg.
        return _quantize_avg_cost(prior_avg)
    weighted = (Decimal(prior_quantity) * prior_avg) + (Decimal(fill_quantity) * fill_price)
    return _quantize_avg_cost(weighted / Decimal(new_total_qty))


#: Scenario classification for one fill event. The planner branches on
#: this value to choose the entry-path vs. exit-close-path mutation set.
#: Partial exits + direction reversals raise
#: :class:`UnsupportedFillScenarioError` (Phase 2+ scope).
FillScenario = Literal["ENTRY", "EXIT_FULL_CLOSE"]


def _classify_fill_scenario(
    *,
    signal_direction: Literal["long", "short", "flat"],
    order_direction: Literal["buy", "sell"],
    prior_position_quantity: int,
    fill_quantity: int,
) -> FillScenario:
    """Classify a fill into ENTRY or EXIT_FULL_CLOSE.

    Branches:

    * **ENTRY** — prior position is zero OR the fill is same-direction
      (buy adding to a long, sell adding to a short). The signal must
      align with the order side (``long→buy`` / ``short→sell``) and the
      signal direction must match the prior position direction.
    * **EXIT_FULL_CLOSE** — opposite-direction fill that takes
      ``|prior_position_quantity|`` to **exactly zero** (the bracket
      stop-loss fires, or LEAN emits an explicit close signal). The
      signal_direction may be the ORIGINAL entry direction
      (long/short — bracket-stop reuses the entry signal_id per
      Option B) OR ``'flat'`` (exit-pipeline PR-C: explicit exit
      signal emitted with direction=FLAT per design §5.2). Either
      way the exit branch keys off the sign of
      ``prior_position_quantity`` vs ``order_direction``, NOT
      ``signal_direction``.

    Raises :class:`UnsupportedFillScenarioError` for:

    * **EXIT_PARTIAL** — ``fill_quantity < |prior|`` (close part of
      the position). Deferred to Phase 2+ when partial-close signals
      land.
    * **EXIT_REVERSAL** — ``fill_quantity > |prior|`` (close + open
      opposite direction in a single fill). Deferred to Phase 2+.
    * Entry direction mismatches (signal_direction vs order_direction;
      signal_direction vs prior position direction) — these are
      almost-certainly programmer-error paths preserved from the
      pre-2026-05-17 ``_validate_entry_direction_match`` guard.
    * **Orphan exit on entry branch** — ``signal_direction='flat'``
      reaching the same_direction_add branch (prior=0 OR same-sided
      fill against a same-direction prior). An explicit-exit signal
      should always be opposite-direction against a non-zero prior;
      this case implies upstream invariants broke (operator-flatten
      between exit signal emit + worker dispatch raced past the
      POSITION_ALREADY_FLAT gate, or a programmer-side bug in
      :func:`services.risk.order_placement_worker._dispatch_exit_signal`).
      Raising here surfaces the violation rather than silently
      mis-routing through the entry planner.
    """
    # Same-direction (or first-fill) → ENTRY
    same_direction_add = (
        prior_position_quantity == 0
        or (prior_position_quantity > 0 and order_direction == "buy")
        or (prior_position_quantity < 0 and order_direction == "sell")
    )
    if same_direction_add:
        if signal_direction == "flat":
            # Exit-pipeline PR-C (2026-05-27) defensive guard: a
            # flat-direction exit fill should NEVER land on the same-
            # direction (entry) branch. The worker's POSITION_ALREADY_FLAT
            # gate runs before dispatching exits, so reaching here means
            # an invariant broke between gate + fill arrival.
            raise UnsupportedFillScenarioError(
                (
                    "Flat-direction signal classified onto entry branch "
                    "(prior_position_quantity is 0 or same-sided as fill). "
                    "Exit signals must close an opposite-direction position; "
                    "orphan exit fills imply an upstream invariant violation "
                    "(see order_placement_worker._dispatch_exit_signal's "
                    "POSITION_ALREADY_FLAT guard)."
                ),
                details={
                    "signal_direction": signal_direction,
                    "order_direction": order_direction,
                    "prior_position_quantity": prior_position_quantity,
                },
            )
        expected_order_side = "buy" if signal_direction == "long" else "sell"
        if order_direction != expected_order_side:
            raise UnsupportedFillScenarioError(
                (
                    f"Order direction {order_direction!r} does not match signal "
                    f"direction {signal_direction!r}; entry case requires "
                    f"(long→buy, short→sell)."
                ),
                details={
                    "signal_direction": signal_direction,
                    "order_direction": order_direction,
                    "prior_position_quantity": prior_position_quantity,
                },
            )
        if prior_position_quantity > 0 and signal_direction == "short":
            raise UnsupportedFillScenarioError(
                (
                    "Short signal with prior long position is a cross-direction "
                    "overlap; not a clean entry add nor an exit close."
                ),
                details={
                    "signal_direction": signal_direction,
                    "prior_position_quantity": prior_position_quantity,
                },
            )
        if prior_position_quantity < 0 and signal_direction == "long":
            raise UnsupportedFillScenarioError(
                (
                    "Long signal with prior short position is a cross-direction "
                    "overlap; not a clean entry add nor an exit close."
                ),
                details={
                    "signal_direction": signal_direction,
                    "prior_position_quantity": prior_position_quantity,
                },
            )
        return "ENTRY"

    # Opposite-direction → exit-class. Branch on magnitude.
    abs_prior = abs(prior_position_quantity)
    if fill_quantity == abs_prior:
        return "EXIT_FULL_CLOSE"
    if fill_quantity < abs_prior:
        raise UnsupportedFillScenarioError(
            (
                f"Partial exit fill (fill_quantity={fill_quantity} < "
                f"|prior_position_quantity|={abs_prior}); deferred to Phase 2+."
            ),
            details={
                "fill_quantity": fill_quantity,
                "prior_position_quantity": prior_position_quantity,
                "order_direction": order_direction,
            },
        )
    # fill_quantity > abs_prior → reversal
    raise UnsupportedFillScenarioError(
        (
            f"Exit reversal fill (fill_quantity={fill_quantity} > "
            f"|prior_position_quantity|={abs_prior}); deferred to Phase 2+. "
            "The fill would cross zero into a new opposite-direction "
            "position which Phase 1 does not support."
        ),
        details={
            "fill_quantity": fill_quantity,
            "prior_position_quantity": prior_position_quantity,
            "order_direction": order_direction,
        },
    )


def plan_fill_application(
    *,
    context: FillContext,
    payload: FillIngestPayload,
    prior_position: PriorPositionRow | None,
    prior_trade: PriorTradeRow | None,
    prior_balance: PriorBalanceSnapshot,
    timestamp_utc: datetime | None = None,
) -> FillApplyPlan:
    """Build the apply-plan for one IBKR Filled event.

    Pure policy — no I/O, no clock, no random. Same inputs always
    produce the same plan.

    Dispatches to one of two scenario-specific planners after classifying
    via :func:`_classify_fill_scenario`:

    * **ENTRY** → :func:`_plan_entry_fill_application`. Audit events:
      ``ORDER_FILLED`` → ``POSITION_OPENED|UPDATED`` →
      ``BALANCE_SNAPSHOT_RECORDED`` → optional ``TRADE_OPENED``.
    * **EXIT_FULL_CLOSE** → :func:`_plan_exit_close_fill_application`.
      Audit events: ``ORDER_FILLED`` → ``POSITION_CLOSED`` →
      ``BALANCE_SNAPSHOT_RECORDED`` → ``TRADE_CLOSED``.

    Raises :class:`UnsupportedFillScenarioError` for partial exits +
    direction reversals (Phase 2+ scope).
    """
    if timestamp_utc is None:
        timestamp_utc = payload.filled_at_utc
    if timestamp_utc.tzinfo is None:
        raise FillProcessingError(
            error_code="TIMEZONE_REQUIRED",
            message="timestamp_utc must be tz-aware UTC per [A06].",
        )
    if payload.filled_at_utc.tzinfo is None:
        raise FillProcessingError(
            error_code="TIMEZONE_REQUIRED",
            message="payload.filled_at_utc must be tz-aware UTC per [A06].",
        )
    if payload.fill_quantity <= 0:
        raise FillProcessingError(
            error_code="FILL_QUANTITY_NON_POSITIVE",
            message=f"fill_quantity={payload.fill_quantity}; must be > 0.",
        )
    if payload.cumulative_filled_quantity <= 0:
        raise FillProcessingError(
            error_code="CUMULATIVE_QUANTITY_NON_POSITIVE",
            message=(
                f"cumulative_filled_quantity={payload.cumulative_filled_quantity}; "
                "must be > 0 for a filled status."
            ),
        )

    prior_qty_signed = prior_position.quantity if prior_position is not None else 0
    scenario = _classify_fill_scenario(
        signal_direction=context.signal_direction,
        order_direction=context.order_direction,
        prior_position_quantity=prior_qty_signed,
        fill_quantity=payload.fill_quantity,
    )

    if scenario == "ENTRY":
        return _plan_entry_fill_application(
            context=context,
            payload=payload,
            prior_position=prior_position,
            prior_trade=prior_trade,
            prior_balance=prior_balance,
        )
    # EXIT_FULL_CLOSE
    if prior_position is None:
        # Classifier guarantees prior_position is not None on this branch
        # (you can't exit a position that doesn't exist). Defensive raise
        # surfaces the contract violation.
        raise FillProcessingError(
            error_code="EXIT_NO_PRIOR_POSITION",
            message=(
                "EXIT_FULL_CLOSE scenario reached with no prior position row; "
                "fill_processor invariant violated."
            ),
        )
    if prior_trade is None:
        raise FillProcessingError(
            error_code="EXIT_NO_PRIOR_TRADE",
            message=(
                "EXIT_FULL_CLOSE scenario reached but no open trade row exists "
                "for this market (same-signal-id lookup AND the market-level "
                "fallback both missed). The trade row was prematurely closed "
                "or never created. Operator must reconcile manually."
            ),
            details={
                "signal_id": str(context.signal_id),
                "market": context.market,
            },
        )
    return _plan_exit_close_fill_application(
        context=context,
        payload=payload,
        prior_position=prior_position,
        prior_trade=prior_trade,
        prior_balance=prior_balance,
    )


def _plan_entry_fill_application(
    *,
    context: FillContext,
    payload: FillIngestPayload,
    prior_position: PriorPositionRow | None,
    prior_trade: PriorTradeRow | None,
    prior_balance: PriorBalanceSnapshot,
) -> FillApplyPlan:
    """Build the apply-plan for an ENTRY-class fill.

    Audit-event order:

      1. ``ORDER_FILLED`` — broker-side confirmation, always first.
      2. ``POSITION_OPENED`` or ``POSITION_UPDATED`` — depending on
         whether a prior position row existed.
      3. ``BALANCE_SNAPSHOT_RECORDED`` — the new cash + NAV snapshot.
      4. ``TRADE_OPENED`` — only when no prior trade row existed for
         this signal. Subsequent fills against the same signal do
         NOT emit a new trade audit event; the trade row is UPDATEd
         in place + ORDER_FILLED + POSITION_UPDATED capture the state.
    """
    # Exit-pipeline PR-C (2026-05-27): :class:`FillContext.signal_direction`
    # is widened to include 'flat' for exit-signal fills. The classifier
    # guarantees the entry branch only sees 'long' / 'short' (the
    # 'flat' guard in same_direction_add raises), so we narrow here for
    # the TRADE_OPENED + TradeMutation paths that expect the entry-
    # direction literal. A defensive assert turns the contract violation
    # into a loud crash instead of a silent type-coercion bug.
    if context.signal_direction not in ("long", "short"):
        raise FillProcessingError(
            error_code="ENTRY_SIGNAL_DIRECTION_NOT_LONG_OR_SHORT",
            message=(
                f"Entry fill planner reached with signal_direction="
                f"{context.signal_direction!r}; classifier should have routed "
                "this to the exit branch or raised UnsupportedFillScenarioError."
            ),
        )
    entry_signal_direction: Literal["long", "short"] = context.signal_direction

    prior_qty_signed = prior_position.quantity if prior_position is not None else 0

    # Signed delta to apply to positions_current.quantity. Buys are
    # positive, sells are negative. Entry contract: signal long →
    # buy → positive delta; signal short → sell → negative delta.
    signed_fill_delta = (
        payload.fill_quantity if context.order_direction == "buy" else -payload.fill_quantity
    )
    new_quantity_signed = prior_qty_signed + signed_fill_delta
    if new_quantity_signed == 0:
        # Entry branch reaching 0 quantity means the prior position was
        # already 0 AND the order delta is 0 (which contradicts the
        # fill_quantity > 0 guard upstream). Defensive raise to surface
        # the invariant violation.
        raise FillProcessingError(
            error_code="POSITION_DELTA_ZERO",
            message=(
                "Computed new_quantity is 0 despite non-zero fill_quantity on "
                "entry branch; fill_processor invariant violated."
            ),
            details={
                "prior_quantity": prior_qty_signed,
                "signed_fill_delta": signed_fill_delta,
            },
        )

    # Order status: filled if cumulative reached target, else partially_filled.
    if payload.cumulative_filled_quantity >= context.target_contracts:
        new_order_status: Literal["filled", "partially_filled"] = "filled"
    else:
        new_order_status = "partially_filled"

    # Position avg_cost recompute uses MAGNITUDES; the sign cancels in
    # the same-direction add. Use the new_quantity magnitude for the
    # denominator so longs + shorts compute symmetrically.
    new_avg_cost = _recompute_avg_cost(
        prior_quantity=abs(prior_qty_signed),
        prior_avg=prior_position.avg_cost if prior_position is not None else Decimal(0),
        fill_quantity=payload.fill_quantity,
        fill_price=payload.fill_price,
    )

    # Position action: open if prior_position is None, else update.
    if prior_position is None:
        position_action: PositionAction = "open"
        audit_position_event_type = AuditEventType.POSITION_OPENED
    else:
        position_action = "update"
        audit_position_event_type = AuditEventType.POSITION_UPDATED

    # Trade action: open if no prior trade row for the signal, else update.
    # For the update path, ``realized_commission_usd`` on the
    # TradeMutation is the FILL's commission DELTA — the apply step
    # accumulates via ``realized_commission_usd = realized_commission_usd
    # + :comm`` SQL so the prior trade's running total is preserved
    # without needing it on the PriorTradeRow.
    if prior_trade is None:
        trade_action: TradeAction = "open"
        new_trade_total_quantity = payload.fill_quantity
        new_trade_avg_entry = _quantize_avg_cost(payload.fill_price)
        new_trade_commission = payload.commission_usd
        trade_opened_at_utc = payload.filled_at_utc
    else:
        trade_action = "update"
        new_trade_total_quantity = prior_trade.total_quantity + payload.fill_quantity
        new_trade_avg_entry = _recompute_avg_cost(
            prior_quantity=prior_trade.total_quantity,
            prior_avg=prior_trade.avg_entry_price,
            fill_quantity=payload.fill_quantity,
            fill_price=payload.fill_price,
        )
        new_trade_commission = payload.commission_usd  # DELTA; apply accumulates
        # opened_at_utc is unused on the UPDATE branch (we don't reset
        # the trade's open timestamp); the dataclass requires a value,
        # so we echo the prior trade's created_at as a documentation
        # crutch — the apply step's UPDATE SQL doesn't reference it.
        trade_opened_at_utc = prior_trade.trade_created_at

    # Balance snapshot: cash semantics differ for ETFs vs futures.
    #
    # ETFs (``context.contract_id is None``): buy = long entry (cash
    # out by full notional); sell = short entry (cash IN per short-sale
    # proceeds). The full notional crosses cash because the underlying
    # is a cash equity.
    #
    # Futures (``context.contract_id is not None``): only commission
    # crosses cash on entry. The full notional does NOT flow — futures
    # are margined, and IBKR holds initial margin in a separate slot
    # (``positions_current.margin_held``, kept in sync by the recon
    # path against IBKR's reqAccountSummary). Pre-2026-05-19, this
    # branch used the same notional-flow formula as ETFs, producing
    # wildly wrong balance rows (e.g., 2026-05-18 22:11:42 backend
    # cash=$-16,545 vs IBKR FlexQuery cash=$19,997.51 from drill 7+8
    # naked positions). The audit log in production already shows the
    # divergence; the fix here is forward-only — the EOD recon path
    # restores authoritative cash on the next EOD balance snapshot
    # (``coinbase_eod`` post-pivot; ``flexquery_eod`` at the time).
    notional_decimal = payload.fill_price * Decimal(payload.fill_quantity)
    is_futures = context.contract_id is not None
    if is_futures:
        # Futures entry: only commission crosses cash. Margin is held
        # separately and reconciled against IBKR; not modeled here.
        cash_delta = -payload.commission_usd
    elif context.order_direction == "buy":
        cash_delta = -(notional_decimal + payload.commission_usd)
    else:
        # ETF short entry: cash credit minus commission.
        cash_delta = notional_decimal - payload.commission_usd
    new_cash = prior_balance.cash_usd + cash_delta
    # NLV recompute: cash + sum(qty * mark for all open positions). We
    # don't have all positions here; use the simplification NLV = cash +
    # (new_quantity * fill_price * multiplier). The multiplier scales
    # the position's notional value into USD so a /MES 1-contract long
    # at $7375 contributes $36,875 of MTM (not $7,375). Close enough
    # for the post-fill snapshot until PR-I's broker-mark refresh runs;
    # for multi-position accounts (Phase 2+), the recon snapshot is
    # authoritative.
    new_position_mtm = Decimal(new_quantity_signed) * payload.fill_price * context.multiplier
    new_nav = new_cash + new_position_mtm
    # Quantize to NUMERIC(20,4) for the balances row.
    nav_q = new_nav.quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)
    cash_q = new_cash.quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)

    # ----- Audit events (audit-first) -----
    audit_events: list[PendingAuditEvent] = []

    audit_events.append(
        PendingAuditEvent(
            event_type=AuditEventType.ORDER_FILLED,
            payload={
                "signal_id": str(context.signal_id),
                "order_id": str(context.order_id),
                "account_id": str(context.account_id),
                "broker_fill_id": payload.broker_fill_id,
                "market": context.market,
                "order_direction": context.order_direction,
                "fill_quantity": payload.fill_quantity,
                "cumulative_filled_quantity": payload.cumulative_filled_quantity,
                "target_contracts": context.target_contracts,
                "fill_price": str(payload.fill_price),
                "commission_usd": str(payload.commission_usd),
                "new_order_status": new_order_status,
                "filled_at_utc": payload.filled_at_utc.isoformat(),
                "scenario": "ENTRY",
            },
        )
    )

    audit_events.append(
        PendingAuditEvent(
            event_type=audit_position_event_type,
            payload={
                "signal_id": str(context.signal_id),
                "order_id": str(context.order_id),
                "account_id": str(context.account_id),
                "market": context.market,
                "contract_id": str(context.contract_id) if context.contract_id else None,
                "prior_quantity": prior_qty_signed,
                "fill_quantity": signed_fill_delta,
                "new_quantity": new_quantity_signed,
                "prior_avg_cost": (
                    str(prior_position.avg_cost) if prior_position is not None else None
                ),
                "new_avg_cost": str(new_avg_cost),
                "last_mark_ts": payload.filled_at_utc.isoformat(),
            },
        )
    )

    audit_events.append(
        PendingAuditEvent(
            event_type=AuditEventType.BALANCE_SNAPSHOT_RECORDED,
            payload={
                "account_id": str(context.account_id),
                "snapshot_ts": payload.filled_at_utc.isoformat(),
                "trigger": "fill_propagation",
                "source": BALANCE_SOURCE_FROM_FILL,
                "prior_cash_usd": str(prior_balance.cash_usd),
                "cash_delta_usd": str(cash_delta),
                "new_cash_usd": str(cash_q),
                "new_net_liquidation_usd": str(nav_q),
                "post_fill_position_mtm_usd": str(
                    new_position_mtm.quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)
                ),
            },
        )
    )

    if trade_action == "open":
        audit_events.append(
            PendingAuditEvent(
                event_type=AuditEventType.TRADE_OPENED,
                payload={
                    "signal_id": str(context.signal_id),
                    "order_id": str(context.order_id),
                    "account_id": str(context.account_id),
                    "market": context.market,
                    "direction": entry_signal_direction,
                    "total_quantity": new_trade_total_quantity,
                    "avg_entry_price": str(new_trade_avg_entry),
                    "realized_commission_usd": str(new_trade_commission),
                    "opened_at_utc": payload.filled_at_utc.isoformat(),
                    "managed_by_version": context.managed_by_version,
                },
            )
        )

    # ----- Table mutations -----
    order_change = OrderStatusChange(
        order_id=context.order_id,
        order_created_at=context.order_created_at,
        new_status=new_order_status,
    )

    fill_insert = FillInsert(
        account_id=context.account_id,
        env=context.env,
        order_id=context.order_id,
        broker_fill_id=payload.broker_fill_id,
        filled_at_utc=payload.filled_at_utc,
        fill_price=payload.fill_price,
        fill_quantity=payload.fill_quantity,
        commission_usd=payload.commission_usd,
        exchange_fee_usd=Decimal("0"),
    )

    position_mutation = PositionMutation(
        action=position_action,
        position_id=prior_position.position_id if prior_position is not None else None,
        account_id=context.account_id,
        market=context.market,
        contract_id=context.contract_id,
        new_quantity=new_quantity_signed,
        new_avg_cost=new_avg_cost,
        last_mark_ts=payload.filled_at_utc,
        managed_by_version=context.managed_by_version,
    )

    balance_insert = BalanceInsert(
        account_id=context.account_id,
        snapshot_ts=payload.filled_at_utc,
        net_liquidation=nav_q,
        cash_usd=cash_q,
        excess_liquidity=cash_q,  # Phase 1 placeholder: equals cash until the broker-side
        # excess_liquidity calc lands. Same for buying_power below
        # (left NULL via the apply step).
        used_margin_pct=Decimal("0"),
        source=BALANCE_SOURCE_FROM_FILL,
    )

    trade_mutation = TradeMutation(
        action=trade_action,
        trade_id=prior_trade.trade_id if prior_trade is not None else None,
        trade_created_at=prior_trade.trade_created_at if prior_trade is not None else None,
        account_id=context.account_id,
        env=context.env,
        market=context.market,
        contract_id=context.contract_id,
        entry_signal_id=context.signal_id,
        entry_order_id=context.order_id,
        direction=entry_signal_direction,
        opened_at_utc=trade_opened_at_utc,
        new_total_quantity=new_trade_total_quantity,
        new_avg_entry_price=new_trade_avg_entry,
        realized_commission_usd=new_trade_commission,
        managed_by_version=context.managed_by_version,
        strategy_hash=context.strategy_hash,
        parameter_set_hash=context.parameter_set_hash,
        slippage_calibration_version_id=context.slippage_calibration_version_id,
    )

    return FillApplyPlan(
        audit_events=tuple(audit_events),
        order_change=order_change,
        fill_insert=fill_insert,
        position_mutation=position_mutation,
        balance_insert=balance_insert,
        trade_mutation=trade_mutation,
    )


def _plan_exit_close_fill_application(
    *,
    context: FillContext,
    payload: FillIngestPayload,
    prior_position: PriorPositionRow,
    prior_trade: PriorTradeRow,
    prior_balance: PriorBalanceSnapshot,
) -> FillApplyPlan:
    """Build the apply-plan for an EXIT_FULL_CLOSE-class fill.

    The bracket stop-loss fired (or LEAN emitted an explicit close
    signal). The fill takes ``|prior_position.quantity|`` to zero
    EXACTLY — partial closes + reversals raise upstream in
    :func:`_classify_fill_scenario`.

    Audit-event order:

      1. ``ORDER_FILLED`` — broker-side confirmation of the exit fill.
      2. ``POSITION_CLOSED`` — position row will be DELETEd by apply.
      3. ``BALANCE_SNAPSHOT_RECORDED`` — post-close cash + NAV (NAV
         equals cash since position quantity is now zero).
      4. ``TRADE_CLOSED`` — trade row will be UPDATEd to state='closed'
         with realized_pnl_usd + closed_at_utc + exit_order_id +
         avg_exit_price stamped.

    Realized P&L convention (per backend-spec §3.6):

    * Long close: ``(avg_exit - avg_entry) * qty - total_commission``
    * Short close: ``(avg_entry - avg_exit) * qty - total_commission``

    where ``total_commission`` is the trade's accumulated commission
    across all entry fills + this exit fill. Quantized to NUMERIC(20,4).
    """
    # Defensive invariants — should never trigger because the classifier
    # already gated on these. Raising here surfaces the contract violation
    # rather than producing a silent invalid plan.
    abs_prior_qty = abs(prior_position.quantity)
    if payload.fill_quantity != abs_prior_qty:
        raise FillProcessingError(
            error_code="EXIT_CLOSE_QTY_MISMATCH",
            message=(
                f"EXIT_FULL_CLOSE fill_quantity={payload.fill_quantity} != "
                f"|prior_position|={abs_prior_qty}; classifier invariant violated."
            ),
            details={
                "fill_quantity": payload.fill_quantity,
                "prior_position_quantity": prior_position.quantity,
            },
        )
    # The trade's quantity must align with the position being closed.
    # If they diverge, the operator has manually edited one but not the
    # other; bail and require manual reconcile.
    if prior_trade.total_quantity != abs_prior_qty:
        raise FillProcessingError(
            error_code="EXIT_CLOSE_TRADE_QTY_MISMATCH",
            message=(
                f"Trade total_quantity={prior_trade.total_quantity} does not match "
                f"|position|={abs_prior_qty}; row drift between trades + "
                "positions_current. Operator must reconcile manually before the "
                "exit fill can apply."
            ),
            details={
                "trade_id": str(prior_trade.trade_id),
                "trade_total_quantity": prior_trade.total_quantity,
                "prior_position_quantity": prior_position.quantity,
            },
        )

    # Trade direction is the sign of the (now-closing) position.
    trade_direction: Literal["long", "short"] = "long" if prior_position.quantity > 0 else "short"

    # Realized P&L computation.
    #
    # The contract multiplier scales raw price delta into USD per spec
    # §3.6 (``trades.realized_pnl_usd`` carries the canonical ``_usd``
    # suffix consistent with ``cash_usd``, ``dividend_pnl_usd``, etc.
    # — all of which are cash USD throughout the schema). For ETFs the
    # multiplier is 1 and the formula reduces to the prior behavior;
    # for futures it produces the actual cash P&L (e.g., /MES with
    # multiplier=5: a 6.5pt loss = -$32.50, not the raw -6.5).
    #
    # Pre-2026-05-19 this branch omitted the multiplier — drill 9 +
    # drill 10 retrospectives flagged the resulting trade row values
    # (drill 10 /MES exit showed realized_pnl_usd=-6.5 instead of the
    # canonical -32.5).
    avg_exit = payload.fill_price
    avg_entry = prior_trade.avg_entry_price
    qty = Decimal(abs_prior_qty)
    if trade_direction == "long":
        gross_pnl = (avg_exit - avg_entry) * qty * context.multiplier
    else:
        gross_pnl = (avg_entry - avg_exit) * qty * context.multiplier
    total_commission_usd = prior_trade.realized_commission_usd + payload.commission_usd
    realized_pnl_raw = gross_pnl - total_commission_usd
    # trades.realized_pnl_usd is NUMERIC(20,4).
    realized_pnl_usd = realized_pnl_raw.quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)
    # Quantize total_commission so the audit payload matches the
    # NUMERIC(20,4) schema for trades.realized_commission_usd.
    total_commission_q = total_commission_usd.quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)
    gross_pnl_q = gross_pnl.quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)

    # Order status: filled if cumulative reached target, else partially_filled.
    # Exit orders are typically full-fill (the stop fires once); but we
    # mirror the entry path's logic for safety.
    if payload.cumulative_filled_quantity >= context.target_contracts:
        new_order_status: Literal["filled", "partially_filled"] = "filled"
    else:
        new_order_status = "partially_filled"

    # Cash + NAV deltas. ETFs vs futures (matching the entry path):
    #
    # ETFs (``context.contract_id is None``):
    #   * Long close (sell): cash IN (full notional) - commission.
    #   * Short close (buy): cash OUT (full notional) + commission.
    #
    # Futures (``context.contract_id is not None``):
    #   * Cash crosses for realized P&L + commission only — initial
    #     margin releases into a separate slot reconciled against
    #     IBKR. The realized P&L is already multiplier-adjusted in
    #     ``gross_pnl`` above, so the cash delta is simply the
    #     gross_pnl minus the fill's commission (the prior trade's
    #     accumulated entry commission already crossed cash on the
    #     entry fill's ``-payload.commission_usd`` delta, so we don't
    #     re-debit it here).
    notional_decimal = payload.fill_price * Decimal(payload.fill_quantity)
    is_futures = context.contract_id is not None
    if is_futures:
        cash_delta = gross_pnl - payload.commission_usd
    elif context.order_direction == "buy":
        # ETF closing a short by buying back.
        cash_delta = -(notional_decimal + payload.commission_usd)
    else:
        # ETF closing a long by selling.
        cash_delta = notional_decimal - payload.commission_usd
    new_cash = prior_balance.cash_usd + cash_delta
    # Post-close: position quantity is 0, so MTM contribution is 0 and
    # NAV equals cash.
    nav_q = new_cash.quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)
    cash_q = new_cash.quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)

    closed_at_utc = payload.filled_at_utc

    # ----- Audit events (audit-first) -----
    audit_events: list[PendingAuditEvent] = [
        PendingAuditEvent(
            event_type=AuditEventType.ORDER_FILLED,
            payload={
                "signal_id": str(context.signal_id),
                "order_id": str(context.order_id),
                "account_id": str(context.account_id),
                "broker_fill_id": payload.broker_fill_id,
                "market": context.market,
                "order_direction": context.order_direction,
                "fill_quantity": payload.fill_quantity,
                "cumulative_filled_quantity": payload.cumulative_filled_quantity,
                "target_contracts": context.target_contracts,
                "fill_price": str(payload.fill_price),
                "commission_usd": str(payload.commission_usd),
                "new_order_status": new_order_status,
                "filled_at_utc": payload.filled_at_utc.isoformat(),
                "scenario": "EXIT_FULL_CLOSE",
            },
        ),
        PendingAuditEvent(
            event_type=AuditEventType.POSITION_CLOSED,
            payload={
                "signal_id": str(context.signal_id),
                "order_id": str(context.order_id),
                "account_id": str(context.account_id),
                "market": context.market,
                "contract_id": str(context.contract_id) if context.contract_id else None,
                "prior_quantity": prior_position.quantity,
                "fill_quantity_signed": (
                    payload.fill_quantity
                    if context.order_direction == "buy"
                    else -payload.fill_quantity
                ),
                "new_quantity": 0,
                "prior_avg_cost": str(prior_position.avg_cost),
                "closed_at_utc": closed_at_utc.isoformat(),
                "trade_id": str(prior_trade.trade_id),
            },
        ),
        PendingAuditEvent(
            event_type=AuditEventType.BALANCE_SNAPSHOT_RECORDED,
            payload={
                "account_id": str(context.account_id),
                "snapshot_ts": payload.filled_at_utc.isoformat(),
                "trigger": "fill_propagation",
                "source": BALANCE_SOURCE_FROM_FILL,
                "prior_cash_usd": str(prior_balance.cash_usd),
                "cash_delta_usd": str(cash_delta),
                "new_cash_usd": str(cash_q),
                "new_net_liquidation_usd": str(nav_q),
                "post_fill_position_mtm_usd": "0",
            },
        ),
        PendingAuditEvent(
            event_type=AuditEventType.TRADE_CLOSED,
            payload={
                "signal_id": str(context.signal_id),
                "trade_id": str(prior_trade.trade_id),
                "entry_order_id": None,  # echoed by audit; not load-bearing
                "exit_order_id": str(context.order_id),
                "account_id": str(context.account_id),
                "market": context.market,
                "direction": trade_direction,
                "total_quantity": abs_prior_qty,
                "avg_entry_price": str(avg_entry),
                "avg_exit_price": str(avg_exit),
                "gross_pnl_usd": str(gross_pnl_q),
                "realized_commission_usd": str(total_commission_q),
                "realized_pnl_usd": str(realized_pnl_usd),
                "closed_at_utc": closed_at_utc.isoformat(),
                "managed_by_version": context.managed_by_version,
            },
        ),
    ]

    # ----- Table mutations -----
    order_change = OrderStatusChange(
        order_id=context.order_id,
        order_created_at=context.order_created_at,
        new_status=new_order_status,
    )

    fill_insert = FillInsert(
        account_id=context.account_id,
        env=context.env,
        order_id=context.order_id,
        broker_fill_id=payload.broker_fill_id,
        filled_at_utc=payload.filled_at_utc,
        fill_price=payload.fill_price,
        fill_quantity=payload.fill_quantity,
        commission_usd=payload.commission_usd,
        exchange_fee_usd=Decimal("0"),
    )

    position_mutation = PositionMutation(
        action="close",
        position_id=prior_position.position_id,
        account_id=context.account_id,
        market=context.market,
        contract_id=context.contract_id,
        # new_quantity / new_avg_cost are unused on the close branch (the
        # apply step DELETEs the row) but the dataclass requires values.
        # Echo the post-close zero + the prior avg_cost as documentation.
        new_quantity=0,
        new_avg_cost=prior_position.avg_cost,
        last_mark_ts=payload.filled_at_utc,
        managed_by_version=context.managed_by_version,
    )

    balance_insert = BalanceInsert(
        account_id=context.account_id,
        snapshot_ts=payload.filled_at_utc,
        net_liquidation=nav_q,
        cash_usd=cash_q,
        excess_liquidity=cash_q,
        used_margin_pct=Decimal("0"),
        source=BALANCE_SOURCE_FROM_FILL,
    )

    trade_mutation = TradeMutation(
        action="close",
        trade_id=prior_trade.trade_id,
        trade_created_at=prior_trade.trade_created_at,
        account_id=context.account_id,
        env=context.env,
        market=context.market,
        contract_id=context.contract_id,
        # entry_signal_id and entry_order_id are preserved on the trade;
        # the apply UPDATE only touches the close-side fields. We echo
        # the entry_signal_id (== context.signal_id under the shared-
        # signal contract) here for dataclass completeness; entry_order_id
        # is NOT available in FillContext for the close path (the context
        # holds the EXIT order id), so we use the same context.order_id
        # as a placeholder — the apply UPDATE doesn't reference it.
        entry_signal_id=context.signal_id,
        entry_order_id=context.order_id,
        direction=trade_direction,
        opened_at_utc=prior_trade.trade_created_at,
        # new_total_quantity unchanged on close (the trade's total qty is
        # what was opened; close doesn't reduce it conceptually — the
        # POSITION goes to 0, not the trade's recorded size).
        new_total_quantity=prior_trade.total_quantity,
        new_avg_entry_price=avg_entry,
        realized_commission_usd=payload.commission_usd,  # DELTA; apply accumulates
        managed_by_version=context.managed_by_version,
        strategy_hash=context.strategy_hash,
        parameter_set_hash=context.parameter_set_hash,
        slippage_calibration_version_id=context.slippage_calibration_version_id,
        closed_at_utc=closed_at_utc,
        exit_order_id=context.order_id,
        avg_exit_price=avg_exit,
        realized_pnl_usd=realized_pnl_usd,
    )

    return FillApplyPlan(
        audit_events=tuple(audit_events),
        order_change=order_change,
        fill_insert=fill_insert,
        position_mutation=position_mutation,
        balance_insert=balance_insert,
        trade_mutation=trade_mutation,
    )


# ---------------------------------------------------------------------------
# I/O lookup helpers
# ---------------------------------------------------------------------------


async def fetch_fill_context(
    session_factory: async_sessionmaker[Any],
    *,
    client_order_id: str,
) -> FillContext | None:
    """Resolve the FillContext for an IBKR fill given the client_order_id.

    Returns ``None`` when no orders row matches — IBKR fired Filled on
    an order we have no record of (operator-manual TWS activity, or a
    race we want to investigate). The worker logs at WARNING.

    Schema-mapping notes:

    * ``orders`` is partitioned by ``created_at``; we capture it for
      downstream UPDATEs. The ``orders_id_idx`` index makes the lookup
      fast despite partitioning.
    * ``signals.direction`` is the canonical direction; orders.direction
      is the buy/sell mirror.
    * ``managed_by_version`` is sourced from the signal's strategy_hash
      today (no separate version registry yet, per backend-spec §3.13).
    """
    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT
                      o.id AS order_id,
                      o.created_at AS order_created_at,
                      o.signal_id,
                      o.account_id,
                      o.env,
                      o.market,
                      o.contract_id,
                      o.direction AS order_direction,
                      s.direction AS signal_direction,
                      s.signal_type,
                      s.target_contracts,
                      s.strategy_hash,
                      s.parameter_set_hash,
                      s.slippage_calibration_version_id,
                      s.decision_price,
                      COALESCE(c.multiplier, 1) AS multiplier
                    FROM orders o
                    JOIN signals s ON s.id = o.signal_id
                    LEFT JOIN contracts c ON c.id = o.contract_id
                    WHERE o.client_order_id = :cid
                    ORDER BY o.created_at DESC
                    LIMIT 1
                    """
                ),
                {"cid": client_order_id},
            )
        ).fetchone()
    if row is None:
        return None
    # Narrow ``signal_type`` literal — DB column is free TEXT; treat
    # unknown values as 'entry' for forwards-compat (consistent with
    # the order_placement_worker's narrowing convention).
    sig_type_raw = str(row.signal_type) if row.signal_type is not None else "entry"
    sig_type: Literal["entry", "exit"] = "exit" if sig_type_raw == "exit" else "entry"
    # Narrow ``signal_direction`` literal — DB CHECK already gates the
    # value to ('long','short','flat'), but mypy doesn't know about
    # database CHECK constraints.
    sig_dir_raw = str(row.signal_direction)
    sig_dir: Literal["long", "short", "flat"]
    if sig_dir_raw == "long":
        sig_dir = "long"
    elif sig_dir_raw == "short":
        sig_dir = "short"
    else:
        sig_dir = "flat"
    return FillContext(
        order_id=row.order_id,
        order_created_at=row.order_created_at,
        signal_id=row.signal_id,
        account_id=row.account_id,
        env=row.env,
        market=row.market,
        contract_id=row.contract_id,
        signal_direction=sig_dir,
        order_direction=row.order_direction,
        signal_type=sig_type,
        target_contracts=row.target_contracts,
        strategy_hash=row.strategy_hash,
        parameter_set_hash=row.parameter_set_hash,
        slippage_calibration_version_id=row.slippage_calibration_version_id,
        decision_price=Decimal(str(row.decision_price)),
        managed_by_version=row.strategy_hash,
        multiplier=Decimal(str(row.multiplier)),
    )


async def fetch_prior_position(
    session_factory: async_sessionmaker[Any],
    *,
    account_id: UUID,
    market: str,
    contract_id: UUID | None,
) -> PriorPositionRow | None:
    """Read positions_current for the (account, market, contract_id)
    tuple. Returns None when no row exists (opening a new position)."""
    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id, quantity, avg_cost FROM positions_current "
                    "WHERE account_id = :acct AND market = :market "
                    "  AND ((contract_id = :cid) OR (contract_id IS NULL AND :cid IS NULL))"
                ),
                {"acct": account_id, "market": market, "cid": contract_id},
            )
        ).fetchone()
    if row is None:
        return None
    return PriorPositionRow(
        position_id=row.id,
        quantity=int(row.quantity),
        avg_cost=Decimal(str(row.avg_cost)),
    )


async def fetch_prior_trade(
    session_factory: async_sessionmaker[Any],
    *,
    entry_signal_id: UUID,
) -> PriorTradeRow | None:
    """Read the existing open trade row for ``entry_signal_id`` if any.

    There is at most one trade per (entry_signal_id, state='open_position')
    in the current model. Returns None when no row exists (first fill).

    The bracket-order contract (Phase B): the stop-loss order reuses the
    entry signal_id, so when the stop fires the same lookup returns the
    open trade row — no separate exit-side query needed.
    """
    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id, created_at, total_quantity, avg_entry_price, "
                    "       realized_commission_usd "
                    "FROM trades "
                    "WHERE entry_signal_id = :sid AND state = 'open_position' "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"sid": entry_signal_id},
            )
        ).fetchone()
    if row is None:
        return None
    return PriorTradeRow(
        trade_id=row.id,
        trade_created_at=row.created_at,
        total_quantity=int(row.total_quantity),
        avg_entry_price=Decimal(str(row.avg_entry_price)),
        realized_commission_usd=Decimal(str(row.realized_commission_usd)),
    )


async def fetch_prior_open_trade_for_market(
    session_factory: async_sessionmaker[Any],
    *,
    account_id: UUID,
    market: str,
) -> PriorTradeRow | None:
    """Read the open trade row for ``(account_id, market)`` if any.

    The market-level fallback for the same-signal-id convention (module
    docstring, 2026-07-12 incident): the C1 strategy worker mints a
    fresh signal per leg, so decision-driven close/add fills carry a
    signal_id that never opened a trade. At most one open trade per
    market exists in this system (single position per market), so the
    market-level lookup is well-defined. Newest-first ORDER BY is a
    defensive tiebreak only.
    """
    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id, created_at, total_quantity, avg_entry_price, "
                    "       realized_commission_usd "
                    "FROM trades "
                    "WHERE account_id = :acct AND market = :market "
                    "  AND state = 'open_position' "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"acct": account_id, "market": market},
            )
        ).fetchone()
    if row is None:
        return None
    return PriorTradeRow(
        trade_id=row.id,
        trade_created_at=row.created_at,
        total_quantity=int(row.total_quantity),
        avg_entry_price=Decimal(str(row.avg_entry_price)),
        realized_commission_usd=Decimal(str(row.realized_commission_usd)),
    )


async def fetch_prior_balance(
    session_factory: async_sessionmaker[Any],
    *,
    account_id: UUID,
) -> PriorBalanceSnapshot:
    """Read the latest balances row for ``account_id``.

    Returns a zero-prior snapshot when no row exists (Phase 1 day-1).
    """
    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT cash_usd, net_liquidation FROM balances "
                    "WHERE account_id = :acct "
                    "ORDER BY snapshot_ts DESC LIMIT 1"
                ),
                {"acct": account_id},
            )
        ).fetchone()
    if row is None:
        return PriorBalanceSnapshot(cash_usd=Decimal(0), net_liquidation=Decimal(0))
    return PriorBalanceSnapshot(
        cash_usd=Decimal(str(row.cash_usd)),
        net_liquidation=Decimal(str(row.net_liquidation)),
    )


# ---------------------------------------------------------------------------
# I/O orchestrator
# ---------------------------------------------------------------------------


async def apply_fill_plan(
    plan: FillApplyPlan,
    *,
    session_factory: async_sessionmaker[Any],
    account_id: UUID,
    env: Environment,
    phase_at_emit: PhaseAtEmit = 1,
) -> FillApplyResult:
    """Flush a :class:`FillApplyPlan` to the database.

    Audit-first per backend-spec §2.10.1:

      1. For each :class:`PendingAuditEvent`: append via
         :func:`append_audit_event` in its own SERIALIZABLE transaction.
         Captures the resulting event_uuid.
      2. INSERT the fills row (RETURNING id).
      3. UPDATE orders.status (partition-addressed via id + created_at).
      4. UPSERT positions_current (INSERT or UPDATE depending on action).
      5. INSERT balances snapshot (RETURNING id).
      6. INSERT or UPDATE trades row (RETURNING id).

    Audit-event ↔ row alignment is positional inside the result tuple
    so callers can correlate. Errors propagate; the worker catches +
    logs at ERROR.
    """
    audit_uuids: list[UUID] = []
    for pending in plan.audit_events:
        async with session_factory() as audit_session:
            record = await append_audit_event(
                audit_session,
                pending.event_type,
                pending.payload,
                account_id=account_id,
                env=env,
                phase_at_emit=phase_at_emit,
            )
        audit_uuids.append(record.event_uuid)

    # Now the table mutations. Each in its own session to keep
    # transaction scopes tight + avoid SERIALIZABLE escalation.
    fill_id = await _insert_fill(plan.fill_insert, session_factory=session_factory)
    await _update_order_status(plan.order_change, session_factory=session_factory)
    position_id = await _apply_position_mutation(
        plan.position_mutation, session_factory=session_factory
    )
    balance_id = await _insert_balance(plan.balance_insert, session_factory=session_factory)
    trade_id = await _apply_trade_mutation(plan.trade_mutation, session_factory=session_factory)

    log.info(
        "fill_processor_applied",
        account_id=str(account_id),
        env=env,
        fill_id=str(fill_id),
        order_id=str(plan.order_change.order_id),
        new_order_status=plan.order_change.new_status,
        position_action=plan.position_mutation.action,
        position_id=str(position_id),
        balance_id=str(balance_id),
        trade_action=plan.trade_mutation.action,
        trade_id=str(trade_id),
        audit_event_count=len(audit_uuids),
    )
    return FillApplyResult(
        audit_event_uuids=tuple(audit_uuids),
        signal_id=plan.trade_mutation.entry_signal_id,
        account_id=account_id,
        fill_id=fill_id,
        position_id=position_id,
        trade_id=trade_id,
        balance_id=balance_id,
        new_order_status=plan.order_change.new_status,
    )


async def _insert_fill(
    insert: FillInsert,
    *,
    session_factory: async_sessionmaker[Any],
) -> UUID:
    async with session_factory() as session:
        async with session.begin():
            row = (
                await session.execute(
                    text(
                        "INSERT INTO fills ("
                        "    account_id, env, order_id, broker_fill_id, "
                        "    filled_at_utc, fill_price, fill_quantity, "
                        "    commission, exchange_fee"
                        ") VALUES ("
                        "    :acct, :env, :order_id, :fill_id, "
                        "    :filled_at, :price, :qty, "
                        "    :commission, :ex_fee"
                        ") RETURNING id"
                    ),
                    {
                        "acct": insert.account_id,
                        "env": insert.env,
                        "order_id": insert.order_id,
                        "fill_id": insert.broker_fill_id,
                        "filled_at": insert.filled_at_utc,
                        "price": insert.fill_price,
                        "qty": insert.fill_quantity,
                        "commission": insert.commission_usd,
                        "ex_fee": insert.exchange_fee_usd,
                    },
                )
            ).fetchone()
            assert row is not None
            return UUID(str(row.id))


async def _update_order_status(
    change: OrderStatusChange,
    *,
    session_factory: async_sessionmaker[Any],
) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE orders SET status = :status, "
                    "    acknowledged_at_utc = COALESCE(acknowledged_at_utc, NOW()) "
                    "WHERE id = :order_id AND created_at = :created_at"
                ),
                {
                    "status": change.new_status,
                    "order_id": change.order_id,
                    "created_at": change.order_created_at,
                },
            )


async def _apply_position_mutation(
    mutation: PositionMutation,
    *,
    session_factory: async_sessionmaker[Any],
) -> UUID:
    """Apply a ``positions_current`` mutation.

    * ``action='open'`` → INSERT new row + RETURNING id.
    * ``action='update'`` → UPDATE existing row's quantity / avg_cost /
      last_mark_ts. Returns the prior position_id.
    * ``action='close'`` → DELETE the positions_current row by id.
      Returns the now-deleted position_id (for SSE / result linkage).
    """
    async with session_factory() as session:
        async with session.begin():
            if mutation.action == "open":
                row = (
                    await session.execute(
                        text(
                            "INSERT INTO positions_current ("
                            "    account_id, market, contract_id, quantity, "
                            "    avg_cost, last_mark_ts, managed_by_version"
                            ") VALUES ("
                            "    :acct, :market, :cid, :qty, "
                            "    :avg, :mark, :ver"
                            ") RETURNING id"
                        ),
                        {
                            "acct": mutation.account_id,
                            "market": mutation.market,
                            "cid": mutation.contract_id,
                            "qty": mutation.new_quantity,
                            "avg": mutation.new_avg_cost,
                            "mark": mutation.last_mark_ts,
                            "ver": mutation.managed_by_version,
                        },
                    )
                ).fetchone()
                assert row is not None
                return UUID(str(row.id))
            if mutation.action == "update":
                assert mutation.position_id is not None
                await session.execute(
                    text(
                        "UPDATE positions_current SET "
                        "    quantity = :qty, avg_cost = :avg, "
                        "    last_mark_ts = :mark "
                        "WHERE id = :pid"
                    ),
                    {
                        "qty": mutation.new_quantity,
                        "avg": mutation.new_avg_cost,
                        "mark": mutation.last_mark_ts,
                        "pid": mutation.position_id,
                    },
                )
                return mutation.position_id
            # action == "close" — exit-fill path. DELETE the row.
            assert mutation.position_id is not None
            await session.execute(
                text("DELETE FROM positions_current WHERE id = :pid"),
                {"pid": mutation.position_id},
            )
            return mutation.position_id


async def _insert_balance(
    insert: BalanceInsert,
    *,
    session_factory: async_sessionmaker[Any],
) -> UUID:
    async with session_factory() as session:
        async with session.begin():
            row = (
                await session.execute(
                    text(
                        "INSERT INTO balances ("
                        "    account_id, snapshot_ts, net_liquidation, "
                        "    cash_usd, excess_liquidity, used_margin_pct, source"
                        ") VALUES ("
                        "    :acct, :ts, :nlv, "
                        "    :cash, :excess, :margin, :source"
                        ") RETURNING id"
                    ),
                    {
                        "acct": insert.account_id,
                        "ts": insert.snapshot_ts,
                        "nlv": insert.net_liquidation,
                        "cash": insert.cash_usd,
                        "excess": insert.excess_liquidity,
                        "margin": insert.used_margin_pct,
                        "source": insert.source,
                    },
                )
            ).fetchone()
            assert row is not None
            return UUID(str(row.id))


async def _apply_trade_mutation(
    mutation: TradeMutation,
    *,
    session_factory: async_sessionmaker[Any],
) -> UUID:
    """Apply a ``trades`` mutation.

    * ``action='open'`` → INSERT new trade row + RETURNING id.
    * ``action='update'`` → UPDATE total_quantity / avg_entry_price +
      accumulate commission. Returns the trade_id.
    * ``action='close'`` → UPDATE state='closed' + stamp closed_at_utc +
      exit_order_id + avg_exit_price + realized_pnl_usd + accumulate
      commission. Returns the trade_id.
    """
    async with session_factory() as session:
        async with session.begin():
            if mutation.action == "open":
                row = (
                    await session.execute(
                        text(
                            "INSERT INTO trades ("
                            "    account_id, env, market, contract_id, "
                            "    entry_signal_id, entry_order_id, "
                            "    direction, opened_at_utc, "
                            "    total_quantity, avg_entry_price, "
                            "    realized_commission_usd, state, "
                            "    managed_by_version, strategy_hash, "
                            "    parameter_set_hash, slippage_calibration_version_id"
                            ") VALUES ("
                            "    :acct, :env, :market, :cid, "
                            "    :sig, :ord, "
                            "    :dir, :opened, "
                            "    :qty, :avg, "
                            "    :comm, 'open_position', "
                            "    :ver, :strat, "
                            "    :param, :slip"
                            ") RETURNING id"
                        ),
                        {
                            "acct": mutation.account_id,
                            "env": mutation.env,
                            "market": mutation.market,
                            "cid": mutation.contract_id,
                            "sig": mutation.entry_signal_id,
                            "ord": mutation.entry_order_id,
                            "dir": mutation.direction,
                            "opened": mutation.opened_at_utc,
                            "qty": mutation.new_total_quantity,
                            "avg": mutation.new_avg_entry_price,
                            "comm": mutation.realized_commission_usd,
                            "ver": mutation.managed_by_version,
                            "strat": mutation.strategy_hash,
                            "param": mutation.parameter_set_hash,
                            "slip": mutation.slippage_calibration_version_id,
                        },
                    )
                ).fetchone()
                assert row is not None
                return UUID(str(row.id))
            if mutation.action == "update":
                assert mutation.trade_id is not None
                assert mutation.trade_created_at is not None
                await session.execute(
                    text(
                        "UPDATE trades SET "
                        "    total_quantity = :qty, "
                        "    avg_entry_price = :avg, "
                        "    realized_commission_usd = realized_commission_usd + :comm "
                        "WHERE id = :tid AND created_at = :tcreated"
                    ),
                    {
                        "qty": mutation.new_total_quantity,
                        "avg": mutation.new_avg_entry_price,
                        "comm": mutation.realized_commission_usd,
                        "tid": mutation.trade_id,
                        "tcreated": mutation.trade_created_at,
                    },
                )
                return mutation.trade_id
            # action == "close" — exit-fill path.
            assert mutation.trade_id is not None
            assert mutation.trade_created_at is not None
            assert mutation.closed_at_utc is not None
            assert mutation.exit_order_id is not None
            assert mutation.avg_exit_price is not None
            assert mutation.realized_pnl_usd is not None
            await session.execute(
                text(
                    "UPDATE trades SET "
                    "    state = 'closed', "
                    "    closed_at_utc = :closed_at, "
                    "    exit_order_id = :exit_oid, "
                    "    avg_exit_price = :avg_exit, "
                    "    realized_pnl_usd = :pnl, "
                    "    realized_commission_usd = realized_commission_usd + :comm "
                    "WHERE id = :tid AND created_at = :tcreated"
                ),
                {
                    "closed_at": mutation.closed_at_utc,
                    "exit_oid": mutation.exit_order_id,
                    "avg_exit": mutation.avg_exit_price,
                    "pnl": mutation.realized_pnl_usd,
                    "comm": mutation.realized_commission_usd,
                    "tid": mutation.trade_id,
                    "tcreated": mutation.trade_created_at,
                },
            )
            # Drill 5 follow-up #3 — flip the entry signal's status to
            # 'closed' so /api/signals?status=working doesn't leave a
            # stale row visible after the trade lifecycle completes.
            # The trade lifecycle authoritative state lives in
            # ``trades.state`` (just UPDATEd to 'closed' above); the
            # signal's status is now strictly cosmetic for the
            # operator-facing /signals page.
            #
            # No audit event emitted — the trade-lifecycle endpoint is
            # already captured by ``TRADE_CLOSED`` in the audit chain
            # (per ``plan.audit_events``); cosmetic-only signal-row
            # updates are not in the §3.30 audit taxonomy.
            #
            # Same transaction as the trades UPDATE so the two
            # mutations are atomic — if signals UPDATE fails for any
            # reason, trades UPDATE rolls back too. The
            # ``status != 'closed'`` guard makes the UPDATE idempotent
            # if the orchestrator re-applies the plan after a
            # transient mid-flight failure.
            await session.execute(
                text("UPDATE signals SET status = 'closed' WHERE id = :sig AND status != 'closed'"),
                {"sig": mutation.entry_signal_id},
            )
            return mutation.trade_id


# ---------------------------------------------------------------------------
# Worker-callable end-to-end facade
# ---------------------------------------------------------------------------


SSEEmitter = Callable[[str, dict[str, Any]], Awaitable[int]]


async def process_fill_event(
    *,
    session_factory: async_sessionmaker[Any],
    client_order_id: str,
    payload: FillIngestPayload,
    env: Environment,
    phase_at_emit: PhaseAtEmit = 1,
) -> FillApplyResult | None:
    """End-to-end facade — looks up FillContext + prior state, plans,
    applies. Returns the FillApplyResult on success.

    Returns ``None`` when no orders row matches the client_order_id —
    the worker logs at WARNING. The SSE emit is the worker's
    responsibility post-facade.

    Raises :class:`UnsupportedFillScenarioError` for exit / hedging
    cases the worker catches + logs at WARNING.
    """
    context = await fetch_fill_context(session_factory, client_order_id=client_order_id)
    if context is None:
        log.warning("fill_processor_unknown_order", client_order_id=client_order_id)
        return None

    prior_position = await fetch_prior_position(
        session_factory,
        account_id=context.account_id,
        market=context.market,
        contract_id=context.contract_id,
    )
    prior_trade = await fetch_prior_trade(session_factory, entry_signal_id=context.signal_id)
    if prior_trade is None:
        # Market-level fallback (2026-07-12): the strategy worker mints a
        # fresh signal per leg, so close/add fills never match the
        # same-signal-id convention. See module docstring.
        prior_trade = await fetch_prior_open_trade_for_market(
            session_factory, account_id=context.account_id, market=context.market
        )
        if prior_trade is not None:
            log.info(
                "fill_prior_trade_market_fallback",
                client_order_id=client_order_id,
                market=context.market,
                trade_id=str(prior_trade.trade_id),
            )
    prior_balance = await fetch_prior_balance(session_factory, account_id=context.account_id)

    plan = plan_fill_application(
        context=context,
        payload=payload,
        prior_position=prior_position,
        prior_trade=prior_trade,
        prior_balance=prior_balance,
        timestamp_utc=payload.filled_at_utc,
    )

    return await apply_fill_plan(
        plan,
        session_factory=session_factory,
        account_id=context.account_id,
        env=env,
        phase_at_emit=phase_at_emit,
    )


__all__ = [
    "AVG_COST_QUANTIZER",
    "BALANCE_SOURCE_FROM_FILL",
    "BalanceInsert",
    "FillApplyPlan",
    "FillApplyResult",
    "FillContext",
    "FillIngestPayload",
    "FillInsert",
    "FillProcessingError",
    "FillScenario",
    "OrderStatusChange",
    "PendingAuditEvent",
    "PositionAction",
    "PositionMutation",
    "PriorBalanceSnapshot",
    "PriorPositionRow",
    "PriorTradeRow",
    "SSEEmitter",
    "TradeAction",
    "TradeMutation",
    "UnsupportedFillScenarioError",
    "apply_fill_plan",
    "fetch_fill_context",
    "fetch_prior_balance",
    "fetch_prior_open_trade_for_market",
    "fetch_prior_position",
    "fetch_prior_trade",
    "plan_fill_application",
    "process_fill_event",
]
