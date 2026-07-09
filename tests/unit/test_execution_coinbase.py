"""Unit tests for the Coinbase execution layer (crypto-pivot C0-B2b).

Covers services/execution/{types,coinbase_client,coinbase_adapter}:
pure parsers at the venue boundary, deterministic client_order_id,
tick rounding, stop geometry, the unprotected-position policy, the §5
execution ladder against a scripted fake broker with a synthetic clock
(the 10-minute stage-1 window runs instantly), native-stop management
incl. the gate-A2 verify path, startup reconcile, and the locked
no-intraday-margin surface.

Live-venue behavior (real fills, real stop acceptance on CDE products,
restart recovery against the real API) is covered by the operator-run
canary drills in deploy/coinbase_execution/README.md — the C0 offline
exit gates.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from services.execution.coinbase_adapter import (
    INTRADAY_MARGIN_OPTIN_FORBIDDEN,
    CoinbaseExecutionAdapter,
    StopVerificationError,
    compute_stop_prices,
    deterministic_client_order_id,
    find_unprotected_positions,
    round_to_tick,
    select_perp_products,
)
from services.execution.coinbase_client import (
    CoinbaseBrokerClient,
    SdkCoinbaseBrokerClient,
    parse_balance_summary,
    parse_fill,
    parse_order_state,
    parse_perp_product_ref,
    parse_position,
)
from services.execution.types import (
    BestBidAsk,
    BrokerOrderAck,
    BrokerOrderRequest,
    BrokerOrderState,
    BrokerPosition,
    PerpProductRef,
)

NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)
D = date(2026, 7, 9)

BTC_PERP = PerpProductRef(
    product_id="BIP-20DEC30-CDE",
    base_asset="BTC",
    contract_size=Decimal("0.01"),
    tick_size=Decimal("5"),
    trading_disabled=False,
)


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


class TestDeterministicClientOrderId:
    def test_same_inputs_same_id(self) -> None:
        a = deterministic_client_order_id(
            decision_date=D, asset="BTC", decision_seq=1, purpose="entry", stage=0
        )
        b = deterministic_client_order_id(
            decision_date=D, asset="btc", decision_seq=1, purpose="entry", stage=0
        )
        assert a == b  # case-insensitive on asset

    def test_distinct_by_every_component(self) -> None:
        base = dict(decision_date=D, asset="BTC", decision_seq=1, purpose="entry", stage=0)
        ids = {
            deterministic_client_order_id(**base),  # type: ignore[arg-type]
            deterministic_client_order_id(**{**base, "decision_date": D + timedelta(days=1)}),  # type: ignore[arg-type]
            deterministic_client_order_id(**{**base, "asset": "ETH"}),  # type: ignore[arg-type]
            deterministic_client_order_id(**{**base, "decision_seq": 2}),  # type: ignore[arg-type]
            deterministic_client_order_id(**{**base, "purpose": "stop"}),  # type: ignore[arg-type]
            deterministic_client_order_id(**{**base, "stage": 1}),  # type: ignore[arg-type]
        }
        assert len(ids) == 6

    def test_format(self) -> None:
        cid = deterministic_client_order_id(
            decision_date=D, asset="BTC", decision_seq=3, purpose="stop", stage=2
        )
        assert cid.startswith("cde-20260709-btc-3-stop2-")
        assert len(cid) < 64


class TestRoundToTick:
    def test_modes(self) -> None:
        tick = Decimal("5")
        assert round_to_tick(Decimal("100002.4"), tick, mode="nearest") == Decimal("100000")
        assert round_to_tick(Decimal("100002.6"), tick, mode="nearest") == Decimal("100005")
        assert round_to_tick(Decimal("100004.9"), tick, mode="down") == Decimal("100000")
        assert round_to_tick(Decimal("100000.1"), tick, mode="up") == Decimal("100005")

    def test_rejects_bad_tick(self) -> None:
        with pytest.raises(ValueError, match="tick"):
            round_to_tick(Decimal("1"), Decimal("0"))


class TestSelectPerpProducts:
    def test_filters_by_asset_and_enabled(self) -> None:
        eth = PerpProductRef(
            product_id="EIP-20DEC30-CDE",
            base_asset="ETH",
            contract_size=Decimal("0.10"),
            tick_size=Decimal("0.1"),
            trading_disabled=False,
        )
        disabled = PerpProductRef(
            product_id="XIP-20DEC30-CDE",
            base_asset="BTC",
            contract_size=Decimal("0.01"),
            tick_size=Decimal("5"),
            trading_disabled=True,
        )
        out = select_perp_products([BTC_PERP, eth, disabled], assets=("BTC", "ETH", "SOL"))
        assert out["BTC"].product_id == "BIP-20DEC30-CDE"
        assert out["ETH"].product_id == "EIP-20DEC30-CDE"
        assert "SOL" not in out

    def test_ambiguity_resolved_deterministically(self) -> None:
        other = PerpProductRef(
            product_id="AIP-20DEC31-CDE",
            base_asset="BTC",
            contract_size=Decimal("0.01"),
            tick_size=Decimal("5"),
            trading_disabled=False,
        )
        out = select_perp_products([BTC_PERP, other], assets=("BTC",))
        assert out["BTC"].product_id == "AIP-20DEC31-CDE"  # lexicographically first


class TestComputeStopPrices:
    def test_long_geometry(self) -> None:
        trigger, limit, direction, side = compute_stop_prices(
            position_contracts=Decimal(3),
            entry_ref=Decimal("100000"),
            atrp=Decimal("0.02"),
            tick=Decimal("5"),
        )
        # 100000 * (1 - 3*0.02) = 94000; limit 1% below = 93060
        assert trigger == Decimal("94000")
        assert limit == Decimal("93060")
        assert direction == "stop_down"
        assert side == "sell"

    def test_short_geometry(self) -> None:
        trigger, limit, direction, side = compute_stop_prices(
            position_contracts=Decimal(-2),
            entry_ref=Decimal("100000"),
            atrp=Decimal("0.02"),
            tick=Decimal("5"),
        )
        assert trigger == Decimal("106000")
        assert limit == Decimal("107060")
        assert direction == "stop_up"
        assert side == "buy"

    def test_rejects_flat_and_garbage(self) -> None:
        with pytest.raises(ValueError, match="no position"):
            compute_stop_prices(
                position_contracts=Decimal(0),
                entry_ref=Decimal("1"),
                atrp=Decimal("0.01"),
                tick=Decimal("1"),
            )
        with pytest.raises(ValueError, match="invalid stop inputs"):
            compute_stop_prices(
                position_contracts=Decimal(1),
                entry_ref=Decimal("0"),
                atrp=Decimal("0.01"),
                tick=Decimal("1"),
            )


def _order_state(**over: Any) -> BrokerOrderState:
    base: dict[str, Any] = dict(
        order_id="oid-1",
        client_order_id="cid-1",
        product_id=BTC_PERP.product_id,
        side="sell",
        status="open",
        contracts=Decimal(3),
        filled_contracts=Decimal(0),
        avg_fill_price=None,
        total_fees_usd=Decimal(0),
        kind="stop_limit",
        stop_trigger_price=Decimal("94000"),
    )
    base.update(over)
    return BrokerOrderState(**base)


class TestFindUnprotectedPositions:
    def _pos(self, contracts: str) -> BrokerPosition:
        return BrokerPosition(
            product_id=BTC_PERP.product_id,
            contracts=Decimal(contracts),
            entry_vwap=Decimal("100000"),
            mark_price=None,
            unrealized_pnl_usd=None,
        )

    def test_covered_long(self) -> None:
        assert find_unprotected_positions([self._pos("3")], [_order_state()]) == []

    def test_uncovered_when_no_stop(self) -> None:
        assert find_unprotected_positions([self._pos("3")], []) == [BTC_PERP.product_id]

    def test_undersized_stop_counts_unprotected(self) -> None:
        orders = [_order_state(contracts=Decimal(2))]
        assert find_unprotected_positions([self._pos("3")], orders) == [BTC_PERP.product_id]

    def test_wrong_side_stop_counts_unprotected(self) -> None:
        orders = [_order_state(side="buy")]
        assert find_unprotected_positions([self._pos("3")], orders) == [BTC_PERP.product_id]

    def test_short_position_covered_by_buy_stop(self) -> None:
        orders = [_order_state(side="buy")]
        assert find_unprotected_positions([self._pos("-3")], orders) == []

    def test_flat_position_ignored(self) -> None:
        assert find_unprotected_positions([self._pos("0")], []) == []


# ---------------------------------------------------------------------------
# boundary parsers
# ---------------------------------------------------------------------------


class TestParseOrderState:
    def _raw(self, **over: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "order_id": "ord-uuid",
            "client_order_id": "cde-20260709-btc-1-entry0-abc",
            "product_id": "BIP-20DEC30-CDE",
            "side": "BUY",
            "status": "OPEN",
            "filled_size": "1",
            "average_filled_price": "100010.5",
            "total_fees": "0.20",
            "order_configuration": {
                "limit_limit_gtc": {
                    "base_size": "3",
                    "limit_price": "100000",
                    "post_only": True,
                }
            },
        }
        base.update(over)
        return base

    def test_post_only_limit(self) -> None:
        state = parse_order_state(self._raw())
        assert state.kind == "limit_post_only"
        assert state.status == "open"
        assert state.side == "buy"
        assert state.contracts == Decimal(3)
        assert state.filled_contracts == Decimal(1)
        assert state.remaining_contracts == Decimal(2)
        assert state.avg_fill_price == Decimal("100010.5")
        assert not state.is_terminal

    def test_plain_gtc_limit_maps_to_ioc_kind_family(self) -> None:
        raw = self._raw(
            order_configuration={"limit_limit_gtc": {"base_size": "3", "post_only": False}}
        )
        assert parse_order_state(raw).kind == "limit_ioc"

    def test_market_and_stop_kinds(self) -> None:
        raw_m = self._raw(order_configuration={"market_market_ioc": {"base_size": "2"}})
        assert parse_order_state(raw_m).kind == "market"
        raw_s = self._raw(
            order_configuration={
                "stop_limit_stop_limit_gtc": {
                    "base_size": "3",
                    "limit_price": "93060",
                    "stop_price": "94000",
                }
            }
        )
        state = parse_order_state(raw_s)
        assert state.kind == "stop_limit"
        assert state.stop_trigger_price == Decimal("94000")

    def test_terminal_statuses(self) -> None:
        for wire, ours in (
            ("FILLED", "filled"),
            ("CANCELLED", "cancelled"),
            ("EXPIRED", "expired"),
            ("FAILED", "failed"),
        ):
            state = parse_order_state(self._raw(status=wire))
            assert state.status == ours
            assert state.is_terminal

    def test_unknown_status_maps_to_unknown(self) -> None:
        state = parse_order_state(self._raw(status="SOME_NEW_STATE"))
        assert state.status == "unknown"
        assert not state.is_terminal


class TestOtherParsers:
    def test_parse_fill(self) -> None:
        fill = parse_fill(
            {
                "entry_id": "f-1",
                "order_id": "ord-1",
                "client_order_id": "cid-1",
                "product_id": "BIP-20DEC30-CDE",
                "side": "SELL",
                "size": "2",
                "price": "100100.5",
                "commission": "0.40",
                "trade_time": "2026-07-09T12:00:00Z",
            }
        )
        assert fill.side == "sell"
        assert fill.contracts == Decimal(2)
        assert fill.price == Decimal("100100.5")
        assert fill.fee_usd == Decimal("0.40")
        assert fill.filled_at_utc == NOW

    def test_parse_position_signs_short(self) -> None:
        pos = parse_position(
            {
                "product_id": "BIP-20DEC30-CDE",
                "side": "SHORT",
                "number_of_contracts": "4",
                "avg_entry_price": "100000",
                "current_price": "99000",
                "unrealized_pnl": "40",
            }
        )
        assert pos.contracts == Decimal(-4)
        assert pos.entry_vwap == Decimal("100000")

    def test_parse_balance_summary_money_objects(self) -> None:
        summary = parse_balance_summary(
            {
                "total_usd_balance": {"value": "6000.50", "currency": "USD"},
                "cfm_usd_balance": {"value": "1500", "currency": "USD"},
                "available_margin": "1200.25",
                "liquidation_buffer_percentage": "85",
            },
            snapshot_at_utc=NOW,
        )
        assert summary.total_usd_balance == Decimal("6000.50")
        assert summary.cfm_usd_balance == Decimal("1500")
        assert summary.available_margin == Decimal("1200.25")
        assert summary.liquidation_buffer_percentage == Decimal("85")
        assert summary.initial_margin is None  # omitted → None, never zero

    @staticmethod
    def _live_perp_raw() -> dict[str, Any]:
        """Trimmed REAL-SHAPE BIP-20DEC30-CDE payload (live probes
        2026-07-09): EXPIRING label + far expiry, funding directly on
        future_product_details, EMPTY base_display_symbol /
        base_currency_id (contract_root_unit is the populated one),
        perpetual_details non-null but funding-empty (the trap)."""
        return {
            "product_id": "BIP-20DEC30-CDE",
            "product_type": "FUTURE",
            "product_venue": "FCM",
            "price_increment": "5",
            "base_display_symbol": "",
            "base_currency_id": "",
            "display_name": "Bitcoin Perpetual",
            "trading_disabled": False,
            "view_only": False,
            "future_product_details": {
                "venue": "cde",
                "contract_size": "0.01",
                "contract_expiry": "2030-12-20T16:00:00Z",
                "contract_expiry_type": "EXPIRING",
                "contract_root_unit": "BTC",
                "funding_interval": "3600s",
                "funding_rate": "0.000005",
                "funding_time": "2026-07-09T16:00:00Z",
                "perpetual_details": {"open_interest": "12345", "funding_rate": ""},
            },
        }

    def test_parse_perp_product_ref_live_shape(self) -> None:
        ref = parse_perp_product_ref(self._live_perp_raw())
        assert ref is not None
        assert ref.product_id == "BIP-20DEC30-CDE"
        # base extracted via the contract_root_unit fallback (the
        # base_display_symbol/base_currency_id fields are empty live)
        assert ref.base_asset == "BTC"
        assert ref.contract_size == Decimal("0.01")
        assert ref.tick_size == Decimal("5")
        assert ref.trading_disabled is False

    def test_parse_perp_product_ref_dated_is_none(self) -> None:
        raw = self._live_perp_raw()
        raw["product_id"] = "BIT-28AUG26-CDE"
        raw["view_only"] = True
        details = raw["future_product_details"]
        details["contract_expiry"] = "2026-08-28T16:00:00Z"
        details["funding_interval"] = None
        details["funding_rate"] = ""
        details["funding_time"] = None
        assert parse_perp_product_ref(raw) is None  # dated: no funding mechanics

    def test_parse_perp_product_ref_intx_is_none(self) -> None:
        # Offshore INTX perp: the only place the literal PERPETUAL label
        # lives. Rejected by venue even with funding + spec populated
        # (locked: no offshore venues, [A13] revised).
        raw = {
            "product_id": "BTC-PERP-INTX",
            "product_type": "FUTURE",
            "product_venue": "INTX",
            "price_increment": "1",
            "base_display_symbol": "BTC",
            "trading_disabled": False,
            "future_product_details": {
                "venue": "intx",
                "contract_size": "1",
                "contract_expiry_type": "PERPETUAL",
                "funding_interval": "3600s",
                "funding_rate": "0.0000021",
                "perpetual_details": {"funding_rate": "0.0000021"},
            },
        }
        assert parse_perp_product_ref(raw) is None

    def test_parse_perp_product_ref_missing_spec_is_none(self) -> None:
        raw = self._live_perp_raw()
        del raw["price_increment"]
        raw["future_product_details"].pop("contract_size")
        assert parse_perp_product_ref(raw) is None  # no contract/tick size

    def test_parse_perp_product_ref_spot_is_none(self) -> None:
        assert parse_perp_product_ref({"product_id": "BTC-USD", "product_type": "SPOT"}) is None


# ---------------------------------------------------------------------------
# fake broker + time machine
# ---------------------------------------------------------------------------


class _TimeMachine:
    """Injected clock + sleep: sleep(s) advances the clock instantly."""

    def __init__(self, start: datetime = NOW) -> None:
        self.now = start
        self.slept: list[float] = []

    def clock(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


class _FakeOrder:
    def __init__(self, request: BrokerOrderRequest, behavior: str) -> None:
        self.request = request
        self.behavior = behavior  # "fill" | "stay_open" | "partial_then_open" | "noop_terminal"
        self.cancelled = False
        self.polls = 0

    def state(self) -> BrokerOrderState:
        req = self.request
        filled = Decimal(0)
        status = "open"
        if self.behavior == "fill":
            filled = req.contracts
            status = "filled"
        elif self.behavior == "partial_then_open":
            filled = Decimal(1)
            status = "cancelled" if self.cancelled else "open"
        elif self.behavior == "stay_open":
            status = "cancelled" if self.cancelled else "open"
        elif self.behavior == "noop_terminal":
            status = "failed"
        kind = req.kind
        return BrokerOrderState(
            order_id=f"venue-{req.client_order_id}",
            client_order_id=req.client_order_id,
            product_id=req.product_id,
            side=req.side,
            status=status,  # type: ignore[arg-type]
            contracts=req.contracts,
            filled_contracts=filled,
            avg_fill_price=(req.limit_price or Decimal("100000")) if filled else None,
            total_fees_usd=Decimal("0.20") * filled,
            kind=kind,
            stop_trigger_price=req.stop_trigger_price,
        )


class _FakeBroker:
    """Scripted CoinbaseBrokerClient: per-kind order behaviors."""

    def __init__(
        self,
        *,
        bid: str = "100000",
        ask: str = "100010",
        post_only: str = "fill",
        ioc: str = "fill",
        market: str = "fill",
        stop: str = "stay_open",  # a resting stop shows as open
        reject_kinds: tuple[str, ...] = (),
        duplicate_reject_cids: tuple[str, ...] = (),
    ) -> None:
        self.bid = Decimal(bid)
        self.ask = Decimal(ask)
        self.behaviors = {
            "limit_post_only": post_only,
            "limit_ioc": ioc,
            "market": market,
            "stop_limit": stop,
        }
        self.reject_kinds = reject_kinds
        self.duplicate_reject_cids = duplicate_reject_cids
        self.orders: dict[str, _FakeOrder] = {}  # by venue order_id
        self.by_client_id: dict[str, _FakeOrder] = {}
        self.requests: list[BrokerOrderRequest] = []
        self.cancel_calls: list[list[str]] = []
        self.positions: list[BrokerPosition] = []

    async def list_perp_products(self) -> list[PerpProductRef]:
        return [BTC_PERP]

    async def get_best_bid_ask(self, product_id: str) -> BestBidAsk:
        return BestBidAsk(product_id=product_id, bid=self.bid, ask=self.ask, observed_at_utc=NOW)

    async def create_order(self, request: BrokerOrderRequest) -> BrokerOrderAck:
        self.requests.append(request)
        if request.client_order_id in self.duplicate_reject_cids:
            return BrokerOrderAck(
                client_order_id=request.client_order_id,
                order_id="",
                accepted=False,
                rejection_reason="DUPLICATE_ORDER: client_order_id already exists",
                submitted_at_utc=NOW,
            )
        if request.kind in self.reject_kinds:
            return BrokerOrderAck(
                client_order_id=request.client_order_id,
                order_id="",
                accepted=False,
                rejection_reason="INVALID_PRICE: post only order would cross",
                submitted_at_utc=NOW,
            )
        order = _FakeOrder(request, self.behaviors[request.kind])
        self.orders[f"venue-{request.client_order_id}"] = order
        self.by_client_id[request.client_order_id] = order
        return BrokerOrderAck(
            client_order_id=request.client_order_id,
            order_id=f"venue-{request.client_order_id}",
            accepted=True,
            rejection_reason=None,
            submitted_at_utc=NOW,
        )

    async def cancel_orders(self, order_ids: list[str]) -> dict[str, bool]:
        self.cancel_calls.append(order_ids)
        out = {}
        for oid in order_ids:
            order = self.orders.get(oid)
            if order is not None:
                order.cancelled = True
            out[oid] = order is not None
        return out

    async def get_order(self, order_id: str) -> BrokerOrderState:
        order = self.orders[order_id]
        order.polls += 1
        return order.state()

    async def list_open_orders(self, product_id: str | None = None) -> list[BrokerOrderState]:
        return [
            o.state()
            for o in self.orders.values()
            if o.state().status in ("open", "queued")
            and (product_id is None or o.request.product_id == product_id)
        ]

    async def find_order_by_client_id(
        self, product_id: str, client_order_id: str
    ) -> BrokerOrderState | None:
        order = self.by_client_id.get(client_order_id)
        return order.state() if order is not None else None

    async def list_fills(self, **kwargs: Any) -> list[Any]:
        return []

    async def list_positions(self) -> list[BrokerPosition]:
        return self.positions

    async def get_futures_balance_summary(self) -> Any:
        raise NotImplementedError


def _adapter(broker: _FakeBroker, tm: _TimeMachine) -> CoinbaseExecutionAdapter:
    return CoinbaseExecutionAdapter(
        client=broker,
        clock=tm.clock,
        sleep=tm.sleep,
        poll_interval_s=5.0,
    )


# ---------------------------------------------------------------------------
# execution ladder
# ---------------------------------------------------------------------------


class TestExecutionLadder:
    def test_stage1_full_fill_at_touch(self) -> None:
        tm = _TimeMachine()
        broker = _FakeBroker(post_only="fill")
        adapter = _adapter(broker, tm)
        result = asyncio.run(
            adapter.execute_target_delta(
                product=BTC_PERP, delta_contracts=Decimal(3), decision_date=D, decision_seq=1
            )
        )
        assert result.fully_filled
        assert [s.stage for s in result.stages] == ["post_only"]
        assert broker.cancel_calls == []
        req = broker.requests[0]
        assert req.kind == "limit_post_only"
        assert req.side == "buy"
        assert req.limit_price == Decimal("100000")  # joined the bid

    def test_sell_joins_ask(self) -> None:
        tm = _TimeMachine()
        broker = _FakeBroker(post_only="fill")
        adapter = _adapter(broker, tm)
        result = asyncio.run(
            adapter.execute_target_delta(
                product=BTC_PERP, delta_contracts=Decimal(-2), decision_date=D, decision_seq=1
            )
        )
        assert result.side == "sell"
        assert broker.requests[0].limit_price == Decimal("100010")

    def test_stage1_timeout_escalates_to_ioc(self) -> None:
        tm = _TimeMachine()
        broker = _FakeBroker(post_only="stay_open", ioc="fill")
        adapter = _adapter(broker, tm)
        result = asyncio.run(
            adapter.execute_target_delta(
                product=BTC_PERP, delta_contracts=Decimal(3), decision_date=D, decision_seq=1
            )
        )
        assert result.fully_filled
        assert [s.stage for s in result.stages] == ["post_only", "ioc_cross"]
        # stage-1 order was cancelled after the 10-min window
        assert broker.cancel_calls, "expected the post-only order to be cancelled"
        # 10 minutes of synthetic waiting elapsed before the cancel
        assert sum(tm.slept) >= 600.0
        ioc_req = broker.requests[1]
        assert ioc_req.kind == "limit_ioc"
        # mid = 100005; +5bps ≈ 100055.0025 → rounded UP to tick 5 = 100060
        assert ioc_req.limit_price == Decimal("100060")

    def test_post_only_rejection_skips_to_ioc(self) -> None:
        tm = _TimeMachine()
        broker = _FakeBroker(ioc="fill", reject_kinds=("limit_post_only",))
        adapter = _adapter(broker, tm)
        result = asyncio.run(
            adapter.execute_target_delta(
                product=BTC_PERP, delta_contracts=Decimal(3), decision_date=D, decision_seq=1
            )
        )
        assert result.fully_filled
        assert [s.stage for s in result.stages] == ["post_only", "ioc_cross"]
        assert result.stages[0].order_id == ""  # never placed
        assert broker.cancel_calls == []

    def test_partial_stage1_then_ioc_partial_then_market(self) -> None:
        tm = _TimeMachine()
        broker = _FakeBroker(post_only="partial_then_open", ioc="partial_then_open", market="fill")
        adapter = _adapter(broker, tm)
        result = asyncio.run(
            adapter.execute_target_delta(
                product=BTC_PERP, delta_contracts=Decimal(4), decision_date=D, decision_seq=1
            )
        )
        assert [s.stage for s in result.stages] == ["post_only", "ioc_cross", "market"]
        # 1 (post-only partial) + 1 (ioc partial) + 2 (market remainder)
        assert result.stages[0].filled_contracts == Decimal(1)
        assert result.stages[1].filled_contracts == Decimal(1)
        assert result.stages[2].requested_contracts == Decimal(2)
        assert result.fully_filled

    def test_market_failure_reports_shortfall(self) -> None:
        tm = _TimeMachine()
        broker = _FakeBroker(post_only="stay_open", ioc="noop_terminal", market="noop_terminal")
        adapter = _adapter(broker, tm)
        result = asyncio.run(
            adapter.execute_target_delta(
                product=BTC_PERP, delta_contracts=Decimal(3), decision_date=D, decision_seq=1
            )
        )
        assert not result.fully_filled
        assert result.filled_contracts == Decimal(0)

    def test_duplicate_rejection_recovers_existing_order(self) -> None:
        tm = _TimeMachine()
        cid = deterministic_client_order_id(
            decision_date=D, asset="BTC", decision_seq=1, purpose="entry", stage=0
        )
        broker = _FakeBroker(post_only="fill", duplicate_reject_cids=(cid,))
        # Pre-existing venue order from the "previous crash": same cid, filled.
        pre_request = BrokerOrderRequest(
            client_order_id=cid,
            product_id=BTC_PERP.product_id,
            side="buy",
            contracts=Decimal(3),
            kind="limit_post_only",
            limit_price=Decimal("100000"),
        )
        pre = _FakeOrder(pre_request, "fill")
        broker.orders[f"venue-{cid}"] = pre
        broker.by_client_id[cid] = pre

        adapter = _adapter(broker, tm)
        result = asyncio.run(
            adapter.execute_target_delta(
                product=BTC_PERP, delta_contracts=Decimal(3), decision_date=D, decision_seq=1
            )
        )
        assert result.fully_filled
        # No NEW order was registered beyond the recovered one; the create
        # attempt bounced with duplicate and we adopted the existing order.
        assert len(result.stages) == 1
        assert result.stages[0].order_id == f"venue-{cid}"

    def test_rejects_zero_and_fractional_deltas(self) -> None:
        tm = _TimeMachine()
        adapter = _adapter(_FakeBroker(), tm)
        with pytest.raises(ValueError, match="non-zero"):
            asyncio.run(
                adapter.execute_target_delta(
                    product=BTC_PERP, delta_contracts=Decimal(0), decision_date=D, decision_seq=1
                )
            )
        with pytest.raises(ValueError, match="integer-valued"):
            asyncio.run(
                adapter.execute_target_delta(
                    product=BTC_PERP,
                    delta_contracts=Decimal("1.5"),
                    decision_date=D,
                    decision_seq=1,
                )
            )


# ---------------------------------------------------------------------------
# native stop management
# ---------------------------------------------------------------------------


class TestEnsureNativeStop:
    def _ensure(
        self, broker: _FakeBroker, tm: _TimeMachine, *, contracts: str = "3", version: int = 0
    ) -> Any:
        adapter = _adapter(broker, tm)
        return asyncio.run(
            adapter.ensure_native_stop(
                product=BTC_PERP,
                position_contracts=Decimal(contracts),
                entry_ref=Decimal("100000"),
                atrp=Decimal("0.02"),
                decision_date=D,
                decision_seq=1,
                version=version,
            )
        )

    def test_places_and_verifies_new_stop(self) -> None:
        tm = _TimeMachine()
        broker = _FakeBroker(stop="stay_open")
        result = self._ensure(broker, tm)
        assert result.action == "placed"
        assert result.verified_resting
        assert result.trigger_price == Decimal("94000")
        assert result.limit_price == Decimal("93060")
        req = broker.requests[0]
        assert req.kind == "stop_limit"
        assert req.side == "sell"
        assert req.stop_direction == "stop_down"

    def test_keeps_matching_existing_stop(self) -> None:
        tm = _TimeMachine()
        broker = _FakeBroker(stop="stay_open")
        # Seed a matching resting stop.
        seed = BrokerOrderRequest(
            client_order_id="cid-old",
            product_id=BTC_PERP.product_id,
            side="sell",
            contracts=Decimal(3),
            kind="stop_limit",
            limit_price=Decimal("93060"),
            stop_trigger_price=Decimal("94000"),
            stop_direction="stop_down",
        )
        old = _FakeOrder(seed, "stay_open")
        broker.orders["venue-cid-old"] = old
        broker.by_client_id["cid-old"] = old
        result = self._ensure(broker, tm)
        assert result.action == "kept"
        assert result.order_id == "venue-cid-old"
        assert broker.requests == []  # nothing placed

    def test_replaces_mismatched_stop(self) -> None:
        tm = _TimeMachine()
        broker = _FakeBroker(stop="stay_open")
        seed = BrokerOrderRequest(
            client_order_id="cid-old",
            product_id=BTC_PERP.product_id,
            side="sell",
            contracts=Decimal(3),
            kind="stop_limit",
            limit_price=Decimal("90000"),
            stop_trigger_price=Decimal("91000"),  # far from the fresh 94000
            stop_direction="stop_down",
        )
        old = _FakeOrder(seed, "stay_open")
        broker.orders["venue-cid-old"] = old
        broker.by_client_id["cid-old"] = old
        result = self._ensure(broker, tm, version=1)
        assert result.action == "replaced"
        assert broker.cancel_calls == [["venue-cid-old"]]
        assert broker.requests[0].stop_trigger_price == Decimal("94000")

    def test_stop_rejected_raises_verification_error(self) -> None:
        tm = _TimeMachine()
        broker = _FakeBroker(reject_kinds=("stop_limit",))
        with pytest.raises(StopVerificationError, match="rejected"):
            self._ensure(broker, tm)

    def test_stop_terminal_before_resting_raises(self) -> None:
        tm = _TimeMachine()
        broker = _FakeBroker(stop="noop_terminal")
        with pytest.raises(StopVerificationError, match="terminal"):
            self._ensure(broker, tm)


# ---------------------------------------------------------------------------
# startup reconcile / cancel-all / locked surface
# ---------------------------------------------------------------------------


class TestStartupAndEmergency:
    def test_startup_reconcile_flags_unprotected(self) -> None:
        tm = _TimeMachine()
        broker = _FakeBroker()
        broker.positions = [
            BrokerPosition(
                product_id=BTC_PERP.product_id,
                contracts=Decimal(3),
                entry_vwap=Decimal("100000"),
                mark_price=None,
                unrealized_pnl_usd=None,
            )
        ]
        adapter = _adapter(broker, tm)
        snapshot = asyncio.run(adapter.startup_reconcile())
        assert snapshot.unprotected_product_ids == (BTC_PERP.product_id,)

    def test_cancel_all_orders(self) -> None:
        tm = _TimeMachine()
        broker = _FakeBroker(stop="stay_open")
        seed = BrokerOrderRequest(
            client_order_id="cid-x",
            product_id=BTC_PERP.product_id,
            side="sell",
            contracts=Decimal(1),
            kind="stop_limit",
            limit_price=Decimal("93060"),
            stop_trigger_price=Decimal("94000"),
            stop_direction="stop_down",
        )
        order = _FakeOrder(seed, "stay_open")
        broker.orders["venue-cid-x"] = order
        broker.by_client_id["cid-x"] = order
        adapter = _adapter(broker, tm)
        assert asyncio.run(adapter.cancel_all_orders()) == 1
        assert asyncio.run(adapter.cancel_all_orders()) == 0  # nothing left

    def test_intraday_margin_surface_is_locked_out(self) -> None:
        """Strategy §7 / delta spec §3.1: no intraday-margin opt-in path."""
        assert INTRADAY_MARGIN_OPTIN_FORBIDDEN is True
        assert not hasattr(CoinbaseBrokerClient, "set_intraday_margin_setting")
        assert not hasattr(SdkCoinbaseBrokerClient, "set_intraday_margin_setting")
        assert not hasattr(CoinbaseExecutionAdapter, "set_intraday_margin_setting")
