"""services/execution/coinbase_adapter.py — execution policy over the Coinbase transport.

Crypto-pivot C0-B2b (delta spec §3.1; strategy §5 execution rules).
Policy layer between the strategy/risk callers (C0-B3+) and the
:class:`~services.execution.coinbase_client.CoinbaseBrokerClient`
transport:

* **Execution ladder** (strategy §5, locked): post-only limit joining
  the touch → if unfilled after 10 minutes, cancel and cross with a
  limit at mid ± 5 bps IOC → if still unfilled, market order. Partial
  fills carry forward — each stage works the *remaining* contracts.
* **Deterministic ``client_order_id``** (§3.1): SHA-256 over
  ``(date, asset, decision-seq, purpose, stage)`` — the same decision
  always produces the same IDs, so crash recovery is idempotent: on a
  duplicate rejection or after restart the adapter resolves the
  existing venue order via ``find_order_by_client_id`` instead of
  double-ordering (small-live gate A3).
* **Native stop management** (§5 Layer 1 / §3.1): place or replace the
  venue-resting stop-limit at ``entry_ref x (1 ∓ 3.0 x ATRp)`` with the
  limit a further 1% through the trigger (wide limit ≈ stop-market; CDE
  has no native stop-market), then verify it is resting via the open-
  orders list (small-live gate A2 requires <10 s to verified-resting).
* **Startup reconcile**: positions + open orders + which positions lack
  a resting protective stop — the §7 restart rule's "re-arm missing
  native stops before doing anything else" input.
* **No intraday-margin opt-in** — not exposed by the transport Protocol
  at all (locked; strategy §7, delta spec §3.1, §6 locked decisions).

Everything time-based is injectable (``clock`` + ``sleep``) so the
ladder's 10-minute stage-1 window unit-tests instantly.

A02 BINDS — ``services/execution/**`` forbidden path;
``risk-review-approved`` required.
A05 — Decimal end-to-end. A06 — tz-aware UTC. A25 — structlog only.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal
from typing import Final, Literal

import structlog

from services.execution.coinbase_client import CoinbaseBrokerClient
from services.execution.types import (
    BrokerError,
    BrokerOrderRequest,
    BrokerOrderState,
    BrokerPosition,
    LadderStage,
    OrderSide,
    PerpProductRef,
    StopDirection,
)

log = structlog.get_logger()

#: Strategy §5 locked execution constants. Changing any of these is a
#: strategy amendment + decisions-log entry, not a tuning commit.
POST_ONLY_WAIT_SECONDS: Final[float] = 600.0  # 10 min at the touch
IOC_CROSS_BPS: Final[Decimal] = Decimal("5")  # mid ± 5 bps stage-2 limit
NATIVE_STOP_ATR_MULT: Final[Decimal] = Decimal("3.0")  # §5 Layer-1 backstop
STOP_LIMIT_THROUGH_FRAC: Final[Decimal] = Decimal("0.01")  # limit 1% past trigger
STOP_VERIFY_TIMEOUT_SECONDS: Final[float] = 10.0  # gate A2 resting deadline

#: Minimum window the order-terminal poll tolerates venue NOT_FOUND
#: before treating the 404 as terminal (2026-07-16 risk-review should-fix
#: #2). The stage deadlines bound how long we wait for a FILL; this
#: floor bounds how long we wait for the order to become VISIBLE at all
#: — the IOC/market stages' 10-20 s deadlines are only 10-20x the
#: observed 1 s read-after-write gap, and a stranded filled order costs
#: an EOD recon break + auto-halt (the exact 2026-07-16 incident).
#: Implementation-layer poll timing, NOT a §5 locked execution constant:
#: it never shortens a stage, never affects fill economics, and only
#: widens the retry window on the NOT_FOUND shape.
ORDER_POLL_NOT_FOUND_GRACE_SECONDS: Final[float] = 30.0

#: Ladder poll cadence while waiting on the stage-1 post-only order.
DEFAULT_POLL_INTERVAL_SECONDS: Final[float] = 5.0

#: Intraday-margin opt-in is FORBIDDEN (strategy §7; delta spec §3.1 /
#: §6 locked). The transport Protocol deliberately has no
#: ``set_intraday_margin_setting`` — this constant exists so the rule
#: is grep-able and its test locks the Protocol surface.
INTRADAY_MARGIN_OPTIN_FORBIDDEN: Final[bool] = True


class StopVerificationError(Exception):
    """Raised when a placed protective stop cannot be verified resting
    within :data:`STOP_VERIFY_TIMEOUT_SECONDS` (gate A2). The caller
    (risk loop) must treat the position as UNPROTECTED and apply the
    strategy §7 outage policy — never assume the stop exists.
    """


def deterministic_client_order_id(
    *,
    decision_date: date,
    asset: str,
    decision_seq: int,
    purpose: Literal["entry", "stop", "flatten"],
    stage: int = 0,
) -> str:
    """§3.1 deterministic order ID: hash of (date, asset, decision-seq).

    ``purpose`` separates the ladder's entry orders from protective
    stops and risk-loop market flattens (C1 strategy worker: client-stop
    / loss-limit / halt / outage-policy exits per strategy §5 Layer 2 +
    §7); ``stage`` separates ladder stages (0 = post-only, 1 = IOC,
    2 = market) and stop re-arms (version counter). Same inputs ⇒ same
    ID, across process restarts — the idempotency key for gate A3.
    """
    seed = f"{decision_date.isoformat()}|{asset.upper()}|{decision_seq}|{purpose}|{stage}"
    digest = hashlib.sha256(seed.encode("ascii")).hexdigest()[:16]
    return (
        f"cde-{decision_date.strftime('%Y%m%d')}-{asset.lower()}"
        f"-{decision_seq}-{purpose}{stage}-{digest}"
    )


def round_to_tick(
    price: Decimal, tick: Decimal, *, mode: Literal["nearest", "down", "up"] = "nearest"
) -> Decimal:
    """Quantize ``price`` onto the product's tick grid."""
    if tick <= 0:
        raise ValueError(f"tick must be positive; got {tick}")
    rounding = {"nearest": ROUND_HALF_UP, "down": ROUND_DOWN, "up": ROUND_UP}[mode]
    ticks = (price / tick).quantize(Decimal(1), rounding=rounding)
    return ticks * tick


def select_perp_products(
    refs: list[PerpProductRef], *, assets: tuple[str, ...]
) -> dict[str, PerpProductRef]:
    """Pick the tradable perp product per asset from runtime discovery.

    Filters to enabled products whose base asset is in ``assets``; if
    the venue ever lists more than one perp-style product per asset,
    picks deterministically (lexicographically smallest product_id) and
    logs the ambiguity for the operator — never a random choice.
    """
    out: dict[str, PerpProductRef] = {}
    for asset in assets:
        candidates = sorted(
            (r for r in refs if r.base_asset == asset.upper() and not r.trading_disabled),
            key=lambda r: r.product_id,
        )
        if not candidates:
            log.warning("coinbase_perp_product_not_found", asset=asset)
            continue
        if len(candidates) > 1:
            log.warning(
                "coinbase_perp_product_ambiguous",
                asset=asset,
                candidates=[c.product_id for c in candidates],
                chosen=candidates[0].product_id,
            )
        out[asset.upper()] = candidates[0]
    return out


def compute_stop_prices(
    *,
    position_contracts: Decimal,
    entry_ref: Decimal,
    atrp: Decimal,
    tick: Decimal,
) -> tuple[Decimal, Decimal, StopDirection, OrderSide]:
    """§5 Layer-1 stop geometry: (trigger, limit, direction, order side).

    Long: trigger below entry (``stop_down``), limit 1% below trigger,
    closing side = sell. Short: mirrored.
    """
    if position_contracts == 0:
        raise ValueError("no position, no stop")
    if entry_ref <= 0 or atrp <= 0:
        raise ValueError(f"invalid stop inputs entry_ref={entry_ref} atrp={atrp}")
    if position_contracts > 0:
        trigger = entry_ref * (1 - NATIVE_STOP_ATR_MULT * atrp)
        limit = trigger * (1 - STOP_LIMIT_THROUGH_FRAC)
        return (
            round_to_tick(trigger, tick, mode="down"),
            round_to_tick(limit, tick, mode="down"),
            "stop_down",
            "sell",
        )
    trigger = entry_ref * (1 + NATIVE_STOP_ATR_MULT * atrp)
    limit = trigger * (1 + STOP_LIMIT_THROUGH_FRAC)
    return (
        round_to_tick(trigger, tick, mode="up"),
        round_to_tick(limit, tick, mode="up"),
        "stop_up",
        "buy",
    )


@dataclass(frozen=True, slots=True)
class LadderStageResult:
    """What one ladder stage accomplished."""

    stage: LadderStage
    client_order_id: str
    order_id: str  # "" if the stage's order was rejected at submit
    requested_contracts: Decimal
    filled_contracts: Decimal
    avg_fill_price: Decimal | None
    fees_usd: Decimal
    # Venue-reported `last_fill_time` from the stage's final order state.
    # None when the stage produced no fill or the venue omitted the field;
    # the worker falls back explicitly (gate A2 measures fill→stop latency
    # off this — never substitute a local clock here).
    last_fill_time_utc: datetime | None = None


@dataclass(frozen=True, slots=True)
class LadderExecutionResult:
    """Outcome of one execute_target_delta call."""

    product_id: str
    side: OrderSide
    requested_contracts: Decimal
    filled_contracts: Decimal
    avg_fill_price: Decimal | None
    total_fees_usd: Decimal
    stages: tuple[LadderStageResult, ...]

    @property
    def fully_filled(self) -> bool:
        return self.filled_contracts >= self.requested_contracts


@dataclass(frozen=True, slots=True)
class StopEnsureResult:
    """Outcome of ensure_native_stop."""

    product_id: str
    action: Literal["kept", "placed", "replaced"]
    client_order_id: str
    order_id: str
    trigger_price: Decimal
    limit_price: Decimal
    contracts: Decimal
    verified_resting: bool


@dataclass(frozen=True, slots=True)
class StartupSnapshot:
    """Restart-recovery view (§7 outage/restart rule; gate A3)."""

    positions: tuple[BrokerPosition, ...]
    open_orders: tuple[BrokerOrderState, ...]
    unprotected_product_ids: tuple[str, ...]  # positions with no resting stop


def find_unprotected_positions(
    positions: list[BrokerPosition], open_orders: list[BrokerOrderState]
) -> list[str]:
    """Pure policy: which positioned products lack a resting stop_limit.

    A protective stop must be (a) kind ``stop_limit``, (b) on the
    position's closing side, (c) working (status open/queued), (d)
    sized at least the absolute position. Anything less counts as
    unprotected — under-sized stops protect only part of the book.
    """
    unprotected: list[str] = []
    for pos in positions:
        if pos.contracts == 0:
            continue
        closing_side: OrderSide = "sell" if pos.contracts > 0 else "buy"
        covered = any(
            o.kind == "stop_limit"
            and o.product_id == pos.product_id
            and o.side == closing_side
            and o.status in ("open", "queued")
            and o.contracts >= abs(pos.contracts)
            for o in open_orders
        )
        if not covered:
            unprotected.append(pos.product_id)
    return unprotected


class CoinbaseExecutionAdapter:
    """Strategy-§5 execution policy over a :class:`CoinbaseBrokerClient`."""

    def __init__(
        self,
        *,
        client: CoinbaseBrokerClient,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        post_only_wait_s: float = POST_ONLY_WAIT_SECONDS,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._client = client
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(tz=UTC))
        self._sleep: Callable[[float], Awaitable[None]] = sleep or asyncio.sleep
        self._post_only_wait_s = post_only_wait_s
        self._poll_interval_s = poll_interval_s
        self._log = log.bind(adapter="coinbase_execution")

    # -- idempotent placement ------------------------------------------------

    async def _place_or_recover(self, request: BrokerOrderRequest) -> tuple[str, str | None]:
        """Place ``request``; on duplicate-ID rejection, recover the
        original venue order. Returns ``(order_id, rejection_reason)``
        — ``order_id`` empty iff genuinely rejected for a non-duplicate
        reason (``rejection_reason`` then set).
        """
        ack = await self._client.create_order(request)
        if ack.accepted:
            return ack.order_id, None
        reason = (ack.rejection_reason or "").lower()
        if "duplicate" in reason or "client_order_id" in reason:
            existing = await self._client.find_order_by_client_id(
                request.product_id, request.client_order_id
            )
            if existing is not None:
                self._log.info(
                    "coinbase_order_recovered_by_client_id",
                    client_order_id=request.client_order_id,
                    order_id=existing.order_id,
                    status=existing.status,
                )
                return existing.order_id, None
        return "", ack.rejection_reason or "rejected"

    async def _await_order_terminal_or_deadline(
        self, order_id: str, *, deadline_s: float
    ) -> BrokerOrderState:
        """Poll one order until terminal or the deadline; return last state.

        **NOT_FOUND tolerance (2026-07-16 incident):** the venue can 404
        a ``get_order`` for a JUST-PLACED order (read-after-write
        visibility gap — observed live one second after a close order
        that in fact FILLED). A 404 inside the poll window is therefore
        treated as "not visible yet": log + keep polling. A 404 that
        PERSISTS through the deadline re-raises — at that point the id
        is either genuinely unknown or the venue is degraded, and the
        caller must treat venue state as unknown (the pre-existing
        BrokerError contract). Every non-404 BrokerError still raises
        immediately.

        The 404 window is ``max(deadline_s,
        ORDER_POLL_NOT_FOUND_GRACE_SECONDS)`` (risk-review should-fix
        #2): the short IOC/market stage deadlines bound the wait for a
        FILL, not the wait for venue VISIBILITY — under a degraded
        venue the read-after-write gap is unbounded, and giving up at
        10-20 s would re-create the stranded-fill incident this fix
        exists to close. The non-terminal return path keeps the plain
        ``deadline_s`` (stage economics unchanged); only the 404-retry
        arm uses the floored window.
        """
        started = self._clock()
        not_found_deadline_s = max(deadline_s, ORDER_POLL_NOT_FOUND_GRACE_SECONDS)
        state: BrokerOrderState | None = None
        while True:
            try:
                state = await self._client.get_order(order_id)
            except BrokerError as exc:
                if exc.http_status != 404:
                    raise
                elapsed = (self._clock() - started).total_seconds()
                if elapsed >= not_found_deadline_s:
                    raise
                self._log.warning(
                    "coinbase_order_poll_not_found_retrying",
                    order_id=order_id,
                    elapsed_s=round(elapsed, 1),
                    deadline_s=not_found_deadline_s,
                    note=(
                        "venue 404 on a just-placed order (read-after-write "
                        "gap); polling until the NOT_FOUND grace deadline"
                    ),
                )
                await self._sleep(self._poll_interval_s)
                continue
            if state.is_terminal:
                return state
            elapsed = (self._clock() - started).total_seconds()
            if elapsed >= deadline_s:
                return state
            await self._sleep(self._poll_interval_s)

    # -- execution ladder ------------------------------------------------------

    async def execute_target_delta(
        self,
        *,
        product: PerpProductRef,
        delta_contracts: Decimal,
        decision_date: date,
        decision_seq: int,
    ) -> LadderExecutionResult:
        """Work a signed contract delta through the §5 ladder.

        ``delta_contracts`` must be a non-zero integer-valued Decimal
        (nano contracts trade in integers; the §3.4 sizing layer owns
        rounding). Returns per-stage accounting; ``fully_filled`` False
        after the market stage means the venue itself failed the order
        — caller escalates (that is halt-worthy, not retry-worthy).
        """
        if delta_contracts == 0:
            raise ValueError("delta_contracts must be non-zero")
        if delta_contracts != delta_contracts.to_integral_value():
            raise ValueError(f"delta_contracts must be integer-valued; got {delta_contracts}")
        side: OrderSide = "buy" if delta_contracts > 0 else "sell"
        remaining = abs(delta_contracts)
        stages: list[LadderStageResult] = []
        log_bound = self._log.bind(
            product_id=product.product_id,
            side=side,
            contracts=str(remaining),
            decision_date=decision_date.isoformat(),
            decision_seq=decision_seq,
        )

        # -- stage 1: post-only at the touch (maker) --------------------------
        book = await self._client.get_best_bid_ask(product.product_id)
        join_price = round_to_tick(
            book.bid if side == "buy" else book.ask,
            product.tick_size,
            mode="down" if side == "buy" else "up",
        )
        cid_1 = deterministic_client_order_id(
            decision_date=decision_date,
            asset=product.base_asset,
            decision_seq=decision_seq,
            purpose="entry",
            stage=0,
        )
        order_id_1, rejection_1 = await self._place_or_recover(
            BrokerOrderRequest(
                client_order_id=cid_1,
                product_id=product.product_id,
                side=side,
                contracts=remaining,
                kind="limit_post_only",
                limit_price=join_price,
            )
        )
        if order_id_1:
            state = await self._await_order_terminal_or_deadline(
                order_id_1, deadline_s=self._post_only_wait_s
            )
            if not state.is_terminal:
                await self._client.cancel_orders([order_id_1])
                state = await self._await_order_terminal_or_deadline(
                    order_id_1, deadline_s=self._post_only_wait_s
                )
            stages.append(
                LadderStageResult(
                    stage="post_only",
                    client_order_id=cid_1,
                    order_id=order_id_1,
                    requested_contracts=remaining,
                    filled_contracts=state.filled_contracts,
                    avg_fill_price=state.avg_fill_price,
                    fees_usd=state.total_fees_usd,
                    last_fill_time_utc=state.last_fill_time_utc,
                )
            )
            remaining -= state.filled_contracts
        else:
            # Post-only rejected (book crossed while placing) — that is
            # exactly the "cross the spread" condition; fall through.
            log_bound.info("coinbase_ladder_post_only_rejected", reason=rejection_1)
            stages.append(
                LadderStageResult(
                    stage="post_only",
                    client_order_id=cid_1,
                    order_id="",
                    requested_contracts=remaining,
                    filled_contracts=Decimal(0),
                    avg_fill_price=None,
                    fees_usd=Decimal(0),
                )
            )

        # -- stage 2: cross at mid ± 5 bps, IOC --------------------------------
        if remaining > 0:
            book = await self._client.get_best_bid_ask(product.product_id)
            bump = book.mid * IOC_CROSS_BPS / Decimal(10_000)
            raw_px = book.mid + bump if side == "buy" else book.mid - bump
            cross_price = round_to_tick(
                raw_px, product.tick_size, mode="up" if side == "buy" else "down"
            )
            cid_2 = deterministic_client_order_id(
                decision_date=decision_date,
                asset=product.base_asset,
                decision_seq=decision_seq,
                purpose="entry",
                stage=1,
            )
            order_id_2, rejection_2 = await self._place_or_recover(
                BrokerOrderRequest(
                    client_order_id=cid_2,
                    product_id=product.product_id,
                    side=side,
                    contracts=remaining,
                    kind="limit_ioc",
                    limit_price=cross_price,
                )
            )
            filled_2 = Decimal(0)
            avg_2: Decimal | None = None
            fees_2 = Decimal(0)
            fill_time_2: datetime | None = None
            if order_id_2:
                # IOC settles server-side immediately; one bounded poll.
                state = await self._await_order_terminal_or_deadline(
                    order_id_2, deadline_s=self._poll_interval_s * 2
                )
                filled_2 = state.filled_contracts
                avg_2 = state.avg_fill_price
                fees_2 = state.total_fees_usd
                fill_time_2 = state.last_fill_time_utc
            else:
                log_bound.warning("coinbase_ladder_ioc_rejected", reason=rejection_2)
            stages.append(
                LadderStageResult(
                    stage="ioc_cross",
                    client_order_id=cid_2,
                    order_id=order_id_2,
                    requested_contracts=remaining,
                    filled_contracts=filled_2,
                    avg_fill_price=avg_2,
                    fees_usd=fees_2,
                    last_fill_time_utc=fill_time_2,
                )
            )
            remaining -= filled_2

        # -- stage 3: market ------------------------------------------------------
        if remaining > 0:
            cid_3 = deterministic_client_order_id(
                decision_date=decision_date,
                asset=product.base_asset,
                decision_seq=decision_seq,
                purpose="entry",
                stage=2,
            )
            order_id_3, rejection_3 = await self._place_or_recover(
                BrokerOrderRequest(
                    client_order_id=cid_3,
                    product_id=product.product_id,
                    side=side,
                    contracts=remaining,
                    kind="market",
                )
            )
            filled_3 = Decimal(0)
            avg_3: Decimal | None = None
            fees_3 = Decimal(0)
            fill_time_3: datetime | None = None
            if order_id_3:
                state = await self._await_order_terminal_or_deadline(
                    order_id_3, deadline_s=self._poll_interval_s * 4
                )
                filled_3 = state.filled_contracts
                avg_3 = state.avg_fill_price
                fees_3 = state.total_fees_usd
                fill_time_3 = state.last_fill_time_utc
            else:
                log_bound.error("coinbase_ladder_market_rejected", reason=rejection_3)
            stages.append(
                LadderStageResult(
                    stage="market",
                    client_order_id=cid_3,
                    order_id=order_id_3,
                    requested_contracts=remaining,
                    filled_contracts=filled_3,
                    avg_fill_price=avg_3,
                    fees_usd=fees_3,
                    last_fill_time_utc=fill_time_3,
                )
            )
            remaining -= filled_3

        total_requested = abs(delta_contracts)
        total_filled = total_requested - remaining
        weighted = Decimal(0)
        fees_total = Decimal(0)
        for s in stages:
            fees_total += s.fees_usd
            if s.avg_fill_price is not None:
                weighted += s.avg_fill_price * s.filled_contracts
        avg_price = weighted / total_filled if total_filled > 0 else None
        result = LadderExecutionResult(
            product_id=product.product_id,
            side=side,
            requested_contracts=total_requested,
            filled_contracts=total_filled,
            avg_fill_price=avg_price,
            total_fees_usd=fees_total,
            stages=tuple(stages),
        )
        log_bound.info(
            "coinbase_ladder_completed",
            filled=str(result.filled_contracts),
            requested=str(result.requested_contracts),
            avg_fill_price=str(result.avg_fill_price) if result.avg_fill_price else None,
            stages=[s.stage for s in stages],
            fully_filled=result.fully_filled,
        )
        return result

    # -- native stop management ---------------------------------------------------

    async def ensure_native_stop(
        self,
        *,
        product: PerpProductRef,
        position_contracts: Decimal,
        entry_ref: Decimal,
        atrp: Decimal,
        decision_date: date,
        decision_seq: int,
        version: int = 0,
    ) -> StopEnsureResult:
        """Place/replace the §5 Layer-1 stop; verify resting (gate A2).

        ``version`` increments each re-arm for the same decision so the
        replacement's client_order_id stays deterministic AND distinct
        from the order it replaces. Raises :class:`StopVerificationError`
        when the placed stop cannot be confirmed resting within
        :data:`STOP_VERIFY_TIMEOUT_SECONDS`.
        """
        trigger, limit, direction, close_side = compute_stop_prices(
            position_contracts=position_contracts,
            entry_ref=entry_ref,
            atrp=atrp,
            tick=product.tick_size,
        )
        contracts = abs(position_contracts)

        open_orders = await self._client.list_open_orders(product.product_id)
        existing = [
            o
            for o in open_orders
            if o.kind == "stop_limit" and o.side == close_side and o.status in ("open", "queued")
        ]
        keeper = next(
            (
                o
                for o in existing
                if o.contracts == contracts
                and o.stop_trigger_price is not None
                and abs(o.stop_trigger_price - trigger) <= product.tick_size
            ),
            None,
        )
        if keeper is not None:
            stale = [o.order_id for o in existing if o.order_id != keeper.order_id]
            if stale:
                await self._client.cancel_orders(stale)
            return StopEnsureResult(
                product_id=product.product_id,
                action="kept",
                client_order_id=keeper.client_order_id,
                order_id=keeper.order_id,
                trigger_price=keeper.stop_trigger_price or trigger,
                limit_price=limit,
                contracts=contracts,
                verified_resting=True,
            )

        if existing:
            await self._client.cancel_orders([o.order_id for o in existing])

        cid = deterministic_client_order_id(
            decision_date=decision_date,
            asset=product.base_asset,
            decision_seq=decision_seq,
            purpose="stop",
            stage=version,
        )
        order_id, rejection = await self._place_or_recover(
            BrokerOrderRequest(
                client_order_id=cid,
                product_id=product.product_id,
                side=close_side,
                contracts=contracts,
                kind="stop_limit",
                limit_price=limit,
                stop_trigger_price=trigger,
                stop_direction=direction,
            )
        )
        if not order_id:
            raise StopVerificationError(
                f"stop placement rejected for {product.product_id}: {rejection}"
            )

        # Verify resting via the open-orders list (gate A2 contract).
        deadline = STOP_VERIFY_TIMEOUT_SECONDS
        waited = 0.0
        while True:
            open_now = await self._client.list_open_orders(product.product_id)
            if any(o.order_id == order_id and o.status in ("open", "queued") for o in open_now):
                break
            try:
                state = await self._client.get_order(order_id)
            except BrokerError as exc:
                if exc.http_status != 404:
                    raise
                # Same read-after-write gap as the ladder poll
                # (2026-07-16): a just-placed stop can 404 on lookup.
                # Not visible yet ≠ terminal — keep waiting; the
                # timeout below still bounds the wait (and a timeout
                # raises StopVerificationError → gate A2 fail-safe:
                # re-arm retries every risk tick).
                state = None
            if state is not None and state.is_terminal:
                raise StopVerificationError(
                    f"stop order {order_id} went terminal ({state.status}) before resting"
                )
            if waited >= deadline:
                raise StopVerificationError(
                    f"stop order {order_id} not confirmed resting within {deadline}s"
                )
            await self._sleep(1.0)
            waited += 1.0

        action: Literal["placed", "replaced"] = "replaced" if existing else "placed"
        self._log.info(
            "coinbase_native_stop_ensured",
            product_id=product.product_id,
            action=action,
            order_id=order_id,
            trigger_price=str(trigger),
            limit_price=str(limit),
            contracts=str(contracts),
        )
        return StopEnsureResult(
            product_id=product.product_id,
            action=action,
            client_order_id=cid,
            order_id=order_id,
            trigger_price=trigger,
            limit_price=limit,
            contracts=contracts,
            verified_resting=True,
        )

    # -- risk-loop market flatten ---------------------------------------------------

    async def execute_market_flatten(
        self,
        *,
        product: PerpProductRef,
        position_contracts: Decimal,
        decision_date: date,
        decision_seq: int,
        stage: int = 0,
    ) -> LadderStageResult:
        """Immediate market-order flatten of an open position (strategy §5
        Layer-2 client stop + §7 flatten paths: loss limits, halt, outage
        policy). Deliberately NOT the §5 ladder — a protective exit must
        not wait 10 minutes at the touch.

        ``decision_seq`` must be unique per (date, asset, purpose) — the
        C1 strategy worker persists a monotonic flatten counter BEFORE
        placing so a crash-restart reuses the same deterministic
        client_order_id and recovers the original order instead of
        double-flattening (gate A3 discipline).
        """
        if position_contracts == 0:
            raise ValueError("no position to flatten")
        if position_contracts != position_contracts.to_integral_value():
            raise ValueError(f"position must be integer-valued; got {position_contracts}")
        side: OrderSide = "sell" if position_contracts > 0 else "buy"
        contracts = abs(position_contracts)
        cid = deterministic_client_order_id(
            decision_date=decision_date,
            asset=product.base_asset,
            decision_seq=decision_seq,
            purpose="flatten",
            stage=stage,
        )
        order_id, rejection = await self._place_or_recover(
            BrokerOrderRequest(
                client_order_id=cid,
                product_id=product.product_id,
                side=side,
                contracts=contracts,
                kind="market",
            )
        )
        filled = Decimal(0)
        avg_price: Decimal | None = None
        fees = Decimal(0)
        fill_time: datetime | None = None
        if order_id:
            state = await self._await_order_terminal_or_deadline(
                order_id, deadline_s=self._poll_interval_s * 4
            )
            filled = state.filled_contracts
            avg_price = state.avg_fill_price
            fees = state.total_fees_usd
            fill_time = state.last_fill_time_utc
        result = LadderStageResult(
            stage="market",
            client_order_id=cid,
            order_id=order_id,
            requested_contracts=contracts,
            filled_contracts=filled,
            avg_fill_price=avg_price,
            fees_usd=fees,
            last_fill_time_utc=fill_time,
        )
        self._log.info(
            "coinbase_market_flatten_executed",
            product_id=product.product_id,
            side=side,
            requested=str(contracts),
            filled=str(filled),
            order_id=order_id or None,
            rejection=rejection,
            client_order_id=cid,
        )
        return result

    # -- restart recovery ------------------------------------------------------------

    async def startup_reconcile(self) -> StartupSnapshot:
        """§7 restart rule input: venue positions/orders + unprotected set."""
        positions = await self._client.list_positions()
        open_orders = await self._client.list_open_orders()
        unprotected = find_unprotected_positions(positions, open_orders)
        self._log.info(
            "coinbase_startup_reconcile",
            positions={p.product_id: str(p.contracts) for p in positions},
            open_orders=len(open_orders),
            unprotected_product_ids=unprotected,
        )
        return StartupSnapshot(
            positions=tuple(positions),
            open_orders=tuple(open_orders),
            unprotected_product_ids=tuple(unprotected),
        )

    # -- emergency paths ------------------------------------------------------------

    async def cancel_all_orders(self, product_id: str | None = None) -> int:
        """Cancel every working order (kill-switch / §7 flatten support)."""
        open_orders = await self._client.list_open_orders(product_id)
        ids = [o.order_id for o in open_orders]
        if not ids:
            return 0
        results = await self._client.cancel_orders(ids)
        submitted = sum(1 for ok in results.values() if ok)
        self._log.info(
            "coinbase_cancel_all_orders",
            product_id=product_id,
            requested=len(ids),
            accepted=submitted,
        )
        return submitted


__all__ = [
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "INTRADAY_MARGIN_OPTIN_FORBIDDEN",
    "IOC_CROSS_BPS",
    "NATIVE_STOP_ATR_MULT",
    "POST_ONLY_WAIT_SECONDS",
    "STOP_LIMIT_THROUGH_FRAC",
    "STOP_VERIFY_TIMEOUT_SECONDS",
    "CoinbaseExecutionAdapter",
    "LadderExecutionResult",
    "LadderStageResult",
    "StartupSnapshot",
    "StopEnsureResult",
    "StopVerificationError",
    "compute_stop_prices",
    "deterministic_client_order_id",
    "find_unprotected_positions",
    "round_to_tick",
    "select_perp_products",
]
