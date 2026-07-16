"""Unit tests for services/signal/strategy_worker.py (crypto-pivot C1 worker).

Fake store / broker / market-data / clock throughout ([A22]: no real audit
writes, no DB). The execution adapter is the REAL
:class:`~services.execution.coinbase_adapter.CoinbaseExecutionAdapter`
over the fake transport so the ladder + deterministic client_order_id +
stop-verification paths are exercised end-to-end.

Coverage map (task battery):

* decision-day dedupe (same-day re-run + restart) — ``TestDecisionDedupe``
* halt gate blocks dispatch in non-permitting states — ``TestHaltGate``
* 2xATR client-stop breach → flatten + lockout (no FSM; strategy §5) —
  ``TestClientStop``
* daily / weekly loss trips → FSM DAILY_LOSS_BREACH / v_target halving —
  ``TestLossLimits``
* hard-halt floor → FSM DECOMMISSION_FLOOR — ``TestLossLimits``
* restart idempotency (resume pending legs; duplicate client_order_id
  recovery) — ``TestRestartIdempotency``
* sizing-pipeline integration (engine target → Decimal re-check →
  adapter calls; m_combined + Phase-A clamps) — ``TestSizingIntegration``
* §7 outage policy (protected-hold vs flatten) — ``TestOutagePolicy``
* pure helpers (scheduling, params mapping, leg splitting, state
  serialization) — ``TestPureHelpers``
* adapter market-flatten + "flatten" purpose ids — ``TestMarketFlatten``
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID, uuid4

import pytest
import structlog

from services.execution.coinbase_adapter import (
    CoinbaseExecutionAdapter,
    deterministic_client_order_id,
)
from services.execution.types import (
    BestBidAsk,
    BrokerOrderAck,
    BrokerOrderRequest,
    BrokerOrderState,
    BrokerPosition,
    FuturesBalanceSummary,
    PerpProductRef,
)
from services.risk.crypto_parameters import AMENDMENT_B_CANONICAL_PARAMETERS
from services.risk.fill_processor import FillIngestPayload
from services.risk.state_machine import TransitionTrigger
from services.signal.crypto_trend import AMENDMENT_B_PARAMS
from services.signal.strategy_worker import (
    RISK_TICK_FAILURE_HALT_THRESHOLD,
    AssetRuntime,
    DecisionRow,
    FillPropagationOutcome,
    KillSwitchInvokeResult,
    MarksFeed,
    RiskStateSnapshot,
    StrategyWorker,
    StrategyWorkerConfig,
    StrategyWorkerStore,
    WorkerStatusRow,
    decision_due,
    deserialize_engine_state,
    equity_from_summary,
    normalize_liq_buffer,
    params_from_canonical,
    serialize_engine_state,
    split_delta_legs,
)

NOW = datetime(2026, 7, 9, 0, 6, tzinfo=UTC)
TODAY = NOW.date()
BTC_PID = "BIP-20DEC30-CDE"
ETH_PID = "EIP-20DEC30-CDE"

BTC_PRODUCT = PerpProductRef(
    product_id=BTC_PID,
    base_asset="BTC",
    contract_size=Decimal("0.01"),
    tick_size=Decimal("1"),
    trading_disabled=False,
)
ETH_PRODUCT = PerpProductRef(
    product_id=ETH_PID,
    base_asset="ETH",
    contract_size=Decimal("0.10"),
    tick_size=Decimal("0.1"),
    trading_disabled=False,
)

_EPOCH = date(2025, 1, 1)


def _synthetic_close(asset: str, session: date) -> float:
    """Deterministic gentle uptrend + mild wiggle (trend +1, low vol,
    vol_ratio ~= 1 so the S3 filter stays open)."""
    import math as _math

    base = 50_000.0 if asset == "BTC" else 3_000.0
    days = (session - _EPOCH).days
    return base * (1.0008**days) * (1.0 + 0.01 * _math.sin(days / 3.0))


class FakeMarketRest:
    """Public-candles fake serving the synthetic daily series."""

    def __init__(self, *, last_session: date) -> None:
        self.last_session = last_session

    async def get_daily_candles(
        self, product_id: str, *, start_unix: int, end_unix: int
    ) -> list[dict[str, Any]]:
        asset = "BTC" if product_id.startswith("BTC") else "ETH"
        out: list[dict[str, Any]] = []
        start = datetime.fromtimestamp(start_unix, tz=UTC).date()
        end = datetime.fromtimestamp(end_unix, tz=UTC).date()
        d = start
        while d < end:
            if d <= self.last_session:
                close = _synthetic_close(asset, d)
                out.append(
                    {
                        "start": str(int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())),
                        "open": str(close * 0.999),
                        "high": str(close * 1.013),
                        "low": str(close * 0.987),
                        "close": str(close),
                        "volume": "100",
                    }
                )
            d += timedelta(days=1)
        return out


class FakeBroker:
    """In-memory CoinbaseBrokerClient: instant fills, duplicate-cid rejection."""

    def __init__(self) -> None:
        self.products = [BTC_PRODUCT, ETH_PRODUCT]
        self.marks: dict[str, Decimal] = {
            BTC_PID: Decimal(str(_synthetic_close("BTC", TODAY - timedelta(days=1)))),
            ETH_PID: Decimal(str(_synthetic_close("ETH", TODAY - timedelta(days=1)))),
        }
        self.equity = Decimal("6000")
        self.liq_buffer_pct: Decimal | None = None
        self.positions: dict[str, Decimal] = {}
        self.orders: dict[str, BrokerOrderState] = {}
        self.by_cid: dict[str, str] = {}
        self.create_calls: list[BrokerOrderRequest] = []
        self.reject_kinds: set[str] = set()
        self.fill_orders = True
        self._seq = 0

    # -- helpers --------------------------------------------------------------

    def _next_id(self) -> str:
        self._seq += 1
        return f"venue-{self._seq}"

    def seed_order(
        self,
        *,
        client_order_id: str,
        product_id: str,
        side: Literal["buy", "sell"],
        contracts: Decimal,
        status: str = "filled",
        kind: str = "limit_post_only",
        price: Decimal | None = None,
    ) -> BrokerOrderState:
        order_id = self._next_id()
        state = BrokerOrderState(
            order_id=order_id,
            client_order_id=client_order_id,
            product_id=product_id,
            side=side,
            status=status,  # type: ignore[arg-type]
            contracts=contracts,
            filled_contracts=contracts if status == "filled" else Decimal(0),
            avg_fill_price=(price or self.marks[product_id]) if status == "filled" else None,
            total_fees_usd=Decimal("0.40") if status == "filled" else Decimal(0),
            kind=kind,  # type: ignore[arg-type]
        )
        self.orders[order_id] = state
        self.by_cid[client_order_id] = order_id
        return state

    # -- protocol -------------------------------------------------------------

    async def list_perp_products(self) -> list[PerpProductRef]:
        return list(self.products)

    async def get_best_bid_ask(self, product_id: str) -> BestBidAsk:
        px = self.marks[product_id]
        return BestBidAsk(
            product_id=product_id,
            bid=px * Decimal("0.999"),
            ask=px * Decimal("1.001"),
            observed_at_utc=NOW,
        )

    async def create_order(self, request: BrokerOrderRequest) -> BrokerOrderAck:
        self.create_calls.append(request)
        if request.client_order_id in self.by_cid:
            return BrokerOrderAck(
                client_order_id=request.client_order_id,
                order_id="",
                accepted=False,
                rejection_reason="duplicate client_order_id",
                submitted_at_utc=NOW,
            )
        if request.kind in self.reject_kinds:
            return BrokerOrderAck(
                client_order_id=request.client_order_id,
                order_id="",
                accepted=False,
                rejection_reason="kind rejected by test",
                submitted_at_utc=NOW,
            )
        order_id = self._next_id()
        fill_price = request.limit_price or self.marks[request.product_id]
        if request.kind == "stop_limit":
            status, filled = "open", Decimal(0)
        elif self.fill_orders:
            status, filled = "filled", request.contracts
            signed = request.contracts if request.side == "buy" else -request.contracts
            self.positions[request.product_id] = (
                self.positions.get(request.product_id, Decimal(0)) + signed
            )
        else:
            status, filled = "cancelled", Decimal(0)
        state = BrokerOrderState(
            order_id=order_id,
            client_order_id=request.client_order_id,
            product_id=request.product_id,
            side=request.side,
            status=status,  # type: ignore[arg-type]
            contracts=request.contracts,
            filled_contracts=filled,
            avg_fill_price=fill_price if filled > 0 else None,
            total_fees_usd=Decimal("0.40") if filled > 0 else Decimal(0),
            kind=request.kind,
            stop_trigger_price=request.stop_trigger_price,
        )
        self.orders[order_id] = state
        self.by_cid[request.client_order_id] = order_id
        return BrokerOrderAck(
            client_order_id=request.client_order_id,
            order_id=order_id,
            accepted=True,
            rejection_reason=None,
            submitted_at_utc=NOW,
        )

    async def cancel_orders(self, order_ids: list[str]) -> dict[str, bool]:
        out: dict[str, bool] = {}
        for oid in order_ids:
            state = self.orders.get(oid)
            if state is not None and not state.is_terminal:
                self.orders[oid] = BrokerOrderState(
                    order_id=state.order_id,
                    client_order_id=state.client_order_id,
                    product_id=state.product_id,
                    side=state.side,
                    status="cancelled",
                    contracts=state.contracts,
                    filled_contracts=state.filled_contracts,
                    avg_fill_price=state.avg_fill_price,
                    total_fees_usd=state.total_fees_usd,
                    kind=state.kind,
                    stop_trigger_price=state.stop_trigger_price,
                )
                out[oid] = True
            else:
                out[oid] = False
        return out

    async def get_order(self, order_id: str) -> BrokerOrderState:
        return self.orders[order_id]

    async def list_open_orders(self, product_id: str | None = None) -> list[BrokerOrderState]:
        return [
            o
            for o in self.orders.values()
            if o.status in ("open", "queued") and (product_id is None or o.product_id == product_id)
        ]

    async def find_order_by_client_id(
        self, product_id: str, client_order_id: str
    ) -> BrokerOrderState | None:
        order_id = self.by_cid.get(client_order_id)
        return self.orders.get(order_id) if order_id else None

    async def list_fills(
        self, *, product_id: str | None = None, order_id: str | None = None
    ) -> list[Any]:
        return []

    async def list_positions(self) -> list[BrokerPosition]:
        return [
            BrokerPosition(
                product_id=pid,
                contracts=contracts,
                entry_vwap=self.marks[pid],
                mark_price=self.marks[pid],
                unrealized_pnl_usd=Decimal(0),
            )
            for pid, contracts in self.positions.items()
            if contracts != 0
        ]

    async def get_futures_balance_summary(self) -> FuturesBalanceSummary:
        return FuturesBalanceSummary(
            total_usd_balance=self.equity,
            cbi_usd_balance=None,
            cfm_usd_balance=self.equity,
            available_margin=self.equity,
            initial_margin=Decimal(0),
            unrealized_pnl=Decimal(0),
            daily_realized_pnl=Decimal(0),
            liquidation_threshold=None,
            liquidation_buffer_amount=None,
            liquidation_buffer_percentage=self.liq_buffer_pct,
            snapshot_at_utc=NOW,
        )


class FakeStore(StrategyWorkerStore):
    """In-memory StrategyWorkerStore ([A22]: no audit writes, no SQL)."""

    def __init__(self) -> None:
        # Deliberately skip super().__init__ — no session factory exists.
        self._env = "paper"
        self._phase = 1
        self._log = structlog.get_logger()
        self.account = uuid4()
        self.risk: RiskStateSnapshot | None = RiskStateSnapshot("NORMAL", None, 0, False)
        self.capital_event_day: date | None = None
        self.param_head: tuple[str, dict[str, str]] | None = (
            "a" * 64,
            dict(AMENDMENT_B_CANONICAL_PARAMETERS),
        )
        self.slippage_id: UUID | None = uuid4()
        self.decisions: dict[date, dict[str, Any]] = {}
        self.status_row: WorkerStatusRow | None = None
        self.status_upserts = 0
        self.signals: dict[UUID, dict[str, Any]] = {}
        self.order_rows: dict[str, dict[str, Any]] = {}
        self.fills: list[tuple[str, FillIngestPayload]] = []
        self.fill_fallbacks: list[str] = []
        self.fill_fallback_causes: list[FillPropagationOutcome | None] = []
        self.terminals: list[tuple[str, str]] = []
        self.kill_switch_calls: list[TransitionTrigger] = []
        # (severity, category, message, detail)
        self.alerts: list[tuple[str, str, str, dict[str, Any]]] = []
        self.equity_history: dict[date, Decimal] = {}
        self._contract_ids: dict[str, UUID] = {}
        self.process_fill_supported = True
        self.insert_alert_delivers = True

    # -- reads ------------------------------------------------------------

    async def fetch_active_account_id(self) -> UUID | None:
        return self.account

    async def fetch_risk_state(self, account_id: UUID) -> RiskStateSnapshot | None:
        return self.risk

    async def fetch_last_threshold_met_capital_event_date(self, account_id: UUID) -> date | None:
        return self.capital_event_day

    async def fetch_parameter_head(self) -> tuple[str, dict[str, str]] | None:
        return self.param_head

    async def fetch_slippage_head_id(self) -> UUID | None:
        return self.slippage_id

    async def fetch_decision(self, account_id: UUID, decision_date: date) -> DecisionRow | None:
        raw = self.decisions.get(decision_date)
        if raw is None:
            return None
        return DecisionRow(
            decision_date=decision_date,
            status=raw["status"],
            equity_usd=raw.get("equity_usd"),
            outcome=json.loads(json.dumps(raw.get("outcome", {}), default=str)),
            engine_state=json.loads(json.dumps(raw.get("engine_state", {}), default=str)),
        )

    async def fetch_equity_on_or_before(
        self, account_id: UUID, on_date: date, *, lookback_days: int = 3
    ) -> Decimal | None:
        for offset in range(lookback_days + 1):
            v = self.equity_history.get(on_date - timedelta(days=offset))
            if v is not None:
                return v
        return None

    async def fetch_month_max_equity(self, account_id: UUID, month_start: date) -> Decimal | None:
        vals = [v for d, v in self.equity_history.items() if d >= month_start]
        return max(vals) if vals else None

    async def fetch_worker_status(self, account_id: UUID) -> WorkerStatusRow | None:
        return self.status_row

    # -- writes ------------------------------------------------------------

    async def upsert_worker_status(self, account_id: UUID, **kwargs: Any) -> None:
        self.status_upserts += 1
        self.status_row = WorkerStatusRow(
            day_start_date=kwargs["day_start_date"],
            day_start_equity_usd=kwargs["day_start_equity_usd"],
            weekly_halved_until=kwargs["weekly_halved_until"],
            flatten_seq=kwargs["flatten_seq"],
            engine_state=json.loads(json.dumps(kwargs["engine_state"], default=str)),
            last_decision_date=kwargs["last_decision_date"],
        )

    async def insert_decision(self, account_id: UUID, **kwargs: Any) -> None:
        d = kwargs["decision_date"]
        if d in self.decisions:
            return  # ON CONFLICT DO NOTHING
        self.decisions[d] = {
            "status": kwargs["status"],
            "equity_usd": kwargs.get("equity_usd"),
            "v_target": kwargs.get("v_target"),
            "m_combined": kwargs.get("m_combined_value"),
            "outcome": json.loads(json.dumps(kwargs.get("outcome", {}), default=str)),
            "engine_state": json.loads(json.dumps(kwargs.get("engine_state", {}), default=str)),
        }

    async def update_decision(self, account_id: UUID, **kwargs: Any) -> None:
        d = kwargs["decision_date"]
        row = self.decisions.setdefault(d, {})
        row["status"] = kwargs["status"]
        row["outcome"] = json.loads(json.dumps(kwargs.get("outcome", {}), default=str))
        row["engine_state"] = json.loads(json.dumps(kwargs.get("engine_state", {}), default=str))

    async def ensure_contract_row(self, *, asset: str, product: PerpProductRef) -> UUID:
        return self._contract_ids.setdefault(asset, uuid4())

    async def insert_signal_row(self, account_id: UUID, **kwargs: Any) -> UUID:
        signal_id = uuid4()
        self.signals[signal_id] = dict(kwargs)
        return signal_id

    async def update_signal_status(self, signal_id: UUID, *, status: str) -> None:
        if signal_id in self.signals:
            self.signals[signal_id]["status"] = status

    async def order_row_exists(self, client_order_id: str) -> str | None:
        row = self.order_rows.get(client_order_id)
        return str(row["status"]) if row else None

    async def insert_order_row(self, account_id: UUID, **kwargs: Any) -> UUID:
        order_id = uuid4()
        self.order_rows[kwargs["client_order_id"]] = {
            "id": order_id,
            "status": "pending",
            **kwargs,
        }
        return order_id

    async def process_fill(
        self, *, client_order_id: str, payload: FillIngestPayload
    ) -> FillPropagationOutcome:
        if not self.process_fill_supported:
            return FillPropagationOutcome(
                propagated=False,
                deferred=True,
                error_code="EXIT_PARTIAL_UNSUPPORTED",
                detail="phase 2 deferral (fake)",
            )
        self.fills.append((client_order_id, payload))
        if client_order_id in self.order_rows:
            self.order_rows[client_order_id]["status"] = "filled"
        return FillPropagationOutcome(propagated=True)

    async def minimal_fill_fallback(
        self,
        account_id: UUID,
        *,
        client_order_id: str,
        payload: FillIngestPayload,
        market: str,
        fully_filled: bool,
        cause: FillPropagationOutcome | None = None,
    ) -> None:
        self.fill_fallbacks.append(client_order_id)
        self.fill_fallback_causes.append(cause)
        if client_order_id in self.order_rows:
            self.order_rows[client_order_id]["status"] = (
                "filled" if fully_filled else "partially_filled"
            )

    async def process_terminal(
        self,
        *,
        client_order_id: str,
        broker_order_id: str,
        status_kind: Literal["cancelled", "rejected"],
        reason: str | None,
        now_utc: datetime,
    ) -> None:
        self.terminals.append((client_order_id, status_kind))
        if client_order_id in self.order_rows:
            self.order_rows[client_order_id]["status"] = status_kind

    async def insert_alert(
        self,
        account_id: UUID,
        *,
        severity: Literal["P0", "P1", "P2"],
        category: str,
        message: str,
        detail: dict[str, Any],
        now_utc: datetime,
    ) -> bool:
        # ``insert_alert_delivers`` models a DB-write outcome so the
        # one-shot helpers' flag-on-confirmed-delivery (#381) is testable;
        # defaults to True (the healthy path).
        if not self.insert_alert_delivers:
            return False
        self.alerts.append((severity, category, message, detail))
        return True

    async def invoke_kill_switch(
        self, account_id: UUID, *, trigger: TransitionTrigger, now_utc: datetime
    ) -> KillSwitchInvokeResult:
        if self.risk is None:
            return KillSwitchInvokeResult.NO_RISK_STATE_ROW
        if self.risk.state == "HALT_NEW":
            return KillSwitchInvokeResult.ALREADY_HALTED
        self.kill_switch_calls.append(trigger)
        self.risk = RiskStateSnapshot("HALT_NEW", "routine", 0, False)
        return KillSwitchInvokeResult.APPLIED


def _build_worker(
    *,
    store: FakeStore | None = None,
    broker: FakeBroker | None = None,
    config: StrategyWorkerConfig | None = None,
    now: datetime = NOW,
) -> tuple[StrategyWorker, FakeStore, FakeBroker]:
    store = store or FakeStore()
    broker = broker or FakeBroker()
    clock = lambda: now  # noqa: E731

    async def _no_sleep(_: float) -> None:
        return None

    adapter = CoinbaseExecutionAdapter(client=broker, clock=clock, sleep=_no_sleep)
    config = config or StrategyWorkerConfig(heartbeat_file=None)
    feed = MarksFeed(ws_url="wss://test.invalid", clock=clock)
    worker = StrategyWorker(
        config=config,
        store=store,
        broker=broker,
        adapter=adapter,
        market_rest=FakeMarketRest(last_session=now.date() - timedelta(days=1)),
        marks_feed=feed,
        clock=clock,
    )
    return worker, store, broker


async def _started_worker(
    *, confirmed_long: bool = True, **kwargs: Any
) -> tuple[StrategyWorker, FakeStore, FakeBroker]:
    worker, store, broker = _build_worker(**kwargs)
    await worker.startup_recovery()
    for pid, price in broker.marks.items():
        worker.marks.store.record(pid, price, observed_at_utc=kwargs.get("now", NOW))
    if confirmed_long:
        # S1 hysteresis requires 2 consecutive confirming closes before a
        # direction applies; most tests start from an already-confirmed
        # long regime (the fresh-state hold is asserted separately in
        # TestDailyDecision.test_fresh_state_first_decision_holds).
        for asset in ("BTC", "ETH"):
            if worker.state[asset].applied_dir == 0 and worker.state[asset].contracts == 0:
                worker.state[asset].applied_dir = 1
    return worker, store, broker


def _open_long(
    worker: StrategyWorker,
    broker: FakeBroker,
    asset: str = "BTC",
    contracts: int = 2,
) -> AssetRuntime:
    pid = BTC_PID if asset == "BTC" else ETH_PID
    mark = broker.marks[pid]
    rt = worker.state[asset]
    rt.contracts = contracts
    rt.applied_dir = 1
    rt.entry_vwap = str(mark)
    rt.client_stop_level = str(mark * Decimal("0.95"))
    rt.atrp_at_stop_set = "0.01"
    broker.positions[pid] = Decimal(contracts)
    return rt


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestPureHelpers:
    def test_decision_due_before_anchor(self) -> None:
        early = datetime(2026, 7, 9, 0, 4, tzinfo=UTC)
        assert decision_due(now_utc=early, last_decision_date=None) is None

    def test_decision_due_at_anchor(self) -> None:
        assert decision_due(now_utc=NOW, last_decision_date=None) == TODAY

    def test_decision_due_dedupes_same_day(self) -> None:
        assert decision_due(now_utc=NOW, last_decision_date=TODAY) is None

    def test_decision_due_mid_day_start_runs_once(self) -> None:
        midday = datetime(2026, 7, 9, 15, 0, tzinfo=UTC)
        assert decision_due(now_utc=midday, last_decision_date=TODAY - timedelta(days=1)) == TODAY
        assert decision_due(now_utc=midday, last_decision_date=TODAY) is None

    def test_decision_due_rejects_naive(self) -> None:
        with pytest.raises(ValueError, match="tz-aware"):
            decision_due(now_utc=datetime(2026, 7, 9), last_decision_date=None)

    def test_params_from_canonical_matches_engine_amendment_b(self) -> None:
        params = params_from_canonical(AMENDMENT_B_CANONICAL_PARAMETERS)
        # The seed and the parity-locked engine profile must agree on every
        # engine-consumed knob (the same lock test discipline as C0-B4).
        assert params.v_target == AMENDMENT_B_PARAMS.v_target
        assert params.per_trade_risk_frac == AMENDMENT_B_PARAMS.per_trade_risk_frac
        assert params.daily_loss_limit == AMENDMENT_B_PARAMS.daily_loss_limit
        assert params.weekly_loss_limit == AMENDMENT_B_PARAMS.weekly_loss_limit
        assert params.gross_cap == AMENDMENT_B_PARAMS.gross_cap
        assert params.hysteresis_hold is True
        assert params.band_edge_rebalance is True
        assert params.dd_tiers == ((9.9, 1.0),)
        assert params.sma_fast == 100
        assert params.sma_slow == 200
        assert params.eth_min_price == 2000.0
        assert params.client_stop_atr == 2.0
        assert params.lockout_days == 2

    def test_split_delta_legs(self) -> None:
        assert split_delta_legs(prior=0, target=0) == []
        assert split_delta_legs(prior=0, target=3) == [(3, "open")]
        assert split_delta_legs(prior=3, target=0) == [(-3, "close")]
        assert split_delta_legs(prior=3, target=5) == [(2, "expand")]
        assert split_delta_legs(prior=5, target=3) == [(-2, "reduce")]
        # Reversal = explicit close + open (fill-processor-compatible legs).
        assert split_delta_legs(prior=2, target=-3) == [(-2, "close"), (-3, "open")]
        assert split_delta_legs(prior=-4, target=1) == [(4, "close"), (1, "open")]

    def test_equity_from_summary_prefers_total(self) -> None:
        s = FuturesBalanceSummary(
            total_usd_balance=Decimal("6000"),
            cbi_usd_balance=Decimal("1000"),
            cfm_usd_balance=Decimal("4000"),
            available_margin=None,
            initial_margin=None,
            unrealized_pnl=None,
            daily_realized_pnl=None,
            liquidation_threshold=None,
            liquidation_buffer_amount=None,
            liquidation_buffer_percentage=None,
            snapshot_at_utc=NOW,
        )
        assert equity_from_summary(s) == Decimal("6000")

    def test_equity_from_summary_fail_safe_none(self) -> None:
        s = FuturesBalanceSummary(
            total_usd_balance=None,
            cbi_usd_balance=None,
            cfm_usd_balance=None,
            available_margin=None,
            initial_margin=None,
            unrealized_pnl=None,
            daily_realized_pnl=None,
            liquidation_threshold=None,
            liquidation_buffer_amount=None,
            liquidation_buffer_percentage=None,
            snapshot_at_utc=NOW,
        )
        assert equity_from_summary(s) is None  # never zero

    def test_normalize_liq_buffer(self) -> None:
        assert normalize_liq_buffer(Decimal("35")) == Decimal("0.35")
        assert normalize_liq_buffer(Decimal("0.35")) == Decimal("0.35")

    def test_asset_runtime_round_trip(self) -> None:
        rt = AssetRuntime(
            contracts=-3,
            applied_dir=-1,
            pending_dir=1,
            pending_count=1,
            lockout_until_ord=123456,
            lockout_dir=1,
            vol_blocked=True,
            stopped_on_date="2026-07-08",
            entry_vwap="165000.5",
            client_stop_level="170000.1",
            atrp_at_stop_set="0.012",
            native_stop_order_id="venue-9",
            stop_rearm_version=4,
        )
        state = deserialize_engine_state(serialize_engine_state({"BTC": rt, "ETH": AssetRuntime()}))
        assert state["BTC"] == rt
        assert state["ETH"] == AssetRuntime()

    def test_to_engine_state_lockout_and_stopped_semantics(self) -> None:
        decided = date(2026, 7, 8)
        rt = AssetRuntime(stopped_on_date="2026-07-08", lockout_until_ord=decided.toordinal() + 2)
        eng = rt.to_engine_state(decided_bar_date=decided)
        assert eng.stopped_today is True
        # Next bar: stopped_today clears but lockout ordinal still binds.
        eng2 = rt.to_engine_state(decided_bar_date=decided + timedelta(days=1))
        assert eng2.stopped_today is False
        assert (decided + timedelta(days=1)).toordinal() < rt.lockout_until_ord


# ---------------------------------------------------------------------------
# Daily decision happy path + sizing integration
# ---------------------------------------------------------------------------


class TestDailyDecision:
    async def test_happy_path_places_orders_and_arms_stops(self) -> None:
        worker, store, broker = await _started_worker()
        await worker.run_daily_decision(TODAY)

        row = store.decisions[TODAY]
        assert row["status"] == "completed"
        assets = row["outcome"]["assets"]
        # Uptrend synthetic series ⇒ long targets within Phase-A clamps.
        assert assets["BTC"]["final_target"] >= 1
        assert assets["BTC"]["final_target"] <= 2  # Phase-A max BTC
        assert assets["ETH"]["final_target"] <= 4  # Phase-A max ETH
        assert assets["BTC"]["action"] == "buy"
        # Venue: entry orders + resting protective stops per positioned asset.
        entry_orders = [c for c in broker.create_calls if c.kind == "limit_post_only"]
        stop_orders = [c for c in broker.create_calls if c.kind == "stop_limit"]
        assert entry_orders and stop_orders
        assert worker.state["BTC"].contracts == assets["BTC"]["final_target"]
        assert worker.state["BTC"].native_stop_order_id is not None
        assert worker.state["BTC"].client_stop_level is not None
        # Downstream pipeline fed: signals + orders rows + aggregate fills.
        assert store.signals and store.order_rows and store.fills
        # The sizing trace rode into the decision record ([A05]: strings).
        assert row["outcome"]["sizing_trace"]["schema_version"] == "crypto_sizing_trace_v1"
        # Engine target flowed through the Decimal re-check unclamped
        # (division-of-labor: normal operation is a no-op clamp).
        assert assets["BTC"]["engine_target"] == assets["BTC"]["sized_target"]

    async def test_fresh_state_first_decision_holds_flat(self) -> None:
        """S1 hysteresis: a brand-new state needs 2 confirming closes
        before a direction applies — day one holds flat (engine parity)."""
        worker, store, broker = await _started_worker(confirmed_long=False)
        await worker.run_daily_decision(TODAY)
        assets = store.decisions[TODAY]["outcome"]["assets"]
        assert assets["BTC"]["final_target"] == 0
        assert assets["BTC"]["action"] == "hold"
        assert [c for c in broker.create_calls if c.kind != "stop_limit"] == []
        # The pending confirmation advanced and persisted for tomorrow.
        assert worker.state["BTC"].pending_dir == 1
        assert worker.state["BTC"].pending_count == 1

    async def test_decision_row_carries_score_target_action_costs(self) -> None:
        worker, store, _ = await _started_worker()
        await worker.run_daily_decision(TODAY)
        entry = store.decisions[TODAY]["outcome"]["assets"]["BTC"]
        assert entry["row"]["trend"] == 1.0
        assert entry["est_cost_usd"] is not None
        assert entry["legs"][0]["status"] == "filled"
        assert entry["stop"]["native_stop_order_id"] is not None


class TestSizingIntegration:
    async def test_convalescent_m_combined_halves_targets(self) -> None:
        worker_n, store_n, _ = await _started_worker()
        await worker_n.run_daily_decision(TODAY)
        normal_targets = {
            a: store_n.decisions[TODAY]["outcome"]["assets"][a]["sized_target"]
            for a in ("BTC", "ETH")
        }

        store_c = FakeStore()
        store_c.risk = RiskStateSnapshot("CONVALESCENT", None, 2, False)
        worker_c, _, _ = await _started_worker(store=store_c)
        await worker_c.run_daily_decision(TODAY)
        row = store_c.decisions[TODAY]
        assert row["status"] == "completed"
        assert row["m_combined"] == Decimal("0.5")
        for asset in ("BTC", "ETH"):
            sized = row["outcome"]["assets"][asset]["sized_target"]
            assert sized == int(normal_targets[asset] * 0.5 // 1) or sized <= normal_targets[asset]
            engine = row["outcome"]["assets"][asset]["engine_target"]
            assert sized == (abs(engine) // 2) * (1 if engine > 0 else -1)

    async def test_capital_event_sessions_1_to_5_halve_targets(self) -> None:
        """Threshold-met event 3 UTC days back ⇒ m_capital_event=0.5."""
        store = FakeStore()
        store.capital_event_day = TODAY - timedelta(days=3)
        worker, _, _ = await _started_worker(store=store)
        await worker.run_daily_decision(TODAY)
        row = store.decisions[TODAY]
        assert row["status"] == "completed"
        assert row["m_combined"] == Decimal("0.5")
        assert row["outcome"]["capital_event_session_count"] == 3
        for asset in ("BTC", "ETH"):
            engine = row["outcome"]["assets"][asset]["engine_target"]
            sized = row["outcome"]["assets"][asset]["sized_target"]
            assert sized == (abs(engine) // 2) * (1 if engine > 0 else -1)

    async def test_capital_event_session_6_normalizes_multiplier(self) -> None:
        store = FakeStore()
        store.capital_event_day = TODAY - timedelta(days=6)
        worker, _, _ = await _started_worker(store=store)
        await worker.run_daily_decision(TODAY)
        row = store.decisions[TODAY]
        assert row["status"] == "completed"
        assert row["m_combined"] == Decimal("1.0")
        assert row["outcome"]["capital_event_session_count"] == 6

    async def test_capital_event_same_day_is_session_zero_full_size(self) -> None:
        """The event's own UTC day counts as session 0 — sessions 1-5
        are the five FOLLOWING days (the 00:05 decision predates an
        intraday event; CME precedent: the event session never halved)."""
        store = FakeStore()
        store.capital_event_day = TODAY
        worker, _, _ = await _started_worker(store=store)
        await worker.run_daily_decision(TODAY)
        row = store.decisions[TODAY]
        assert row["m_combined"] == Decimal("1.0")
        assert row["outcome"]["capital_event_session_count"] == 0

    async def test_capital_event_plus_convalescent_is_min_not_product(self) -> None:
        """§5.7 locked: MIN(0.5, 0.5) = 0.5, never 0.25."""
        store = FakeStore()
        store.risk = RiskStateSnapshot("CONVALESCENT", None, 2, False)
        store.capital_event_day = TODAY - timedelta(days=2)
        worker, _, _ = await _started_worker(store=store)
        await worker.run_daily_decision(TODAY)
        row = store.decisions[TODAY]
        assert row["status"] == "completed"
        assert row["m_combined"] == Decimal("0.5")

    async def test_capital_event_lapsed_window_full_size(self) -> None:
        """Event 31+ days back: mode over, even when risk_state still
        carries the Phase-0 absolute-counter fields (nothing clears
        them) — the date-derived count is authoritative."""
        store = FakeStore()
        store.risk = RiskStateSnapshot("NORMAL", None, 0, True)  # stale flag
        store.capital_event_day = TODAY - timedelta(days=31)
        worker, _, _ = await _started_worker(store=store)
        await worker.run_daily_decision(TODAY)
        row = store.decisions[TODAY]
        assert row["status"] == "completed"
        assert row["m_combined"] == Decimal("1.0")
        assert row["outcome"]["capital_event_session_count"] == 31

    async def test_capital_event_fetch_failure_fails_closed(self) -> None:
        """A DB failure sourcing the count must never size on a guess:
        the guarded decision fails loudly BEFORE any engine-state
        mutation or dispatch, places no orders, and leaves the date
        un-latched so the next 30 s tick retries."""
        store = FakeStore()

        async def _boom(account_id: UUID) -> date | None:
            raise RuntimeError("db down")

        store.fetch_last_threshold_met_capital_event_date = _boom  # type: ignore[method-assign]
        worker, _, broker = await _started_worker(store=store)
        await worker._run_decision_guarded(TODAY)
        assert broker.create_calls == []
        assert worker._last_decision_date != TODAY  # retried next tick

    async def test_phase_a_contract_clamp_applies(self) -> None:
        config = StrategyWorkerConfig(heartbeat_file=None, max_contracts={"BTC": 1, "ETH": 1})
        worker, store, _ = await _started_worker(config=config)
        await worker.run_daily_decision(TODAY)
        assets = store.decisions[TODAY]["outcome"]["assets"]
        for asset in ("BTC", "ETH"):
            assert abs(assets[asset]["final_target"]) <= 1

    async def test_sizing_error_fails_decision_without_orders(self) -> None:
        worker, store, broker = await _started_worker()
        # Poison the mark source: no marks + broker book failure ⇒ the
        # Decimal re-check must refuse (never size against a missing mark).
        worker.marks.store._marks.clear()

        async def _boom(product_id: str) -> BestBidAsk:
            raise RuntimeError("book down")

        broker.get_best_bid_ask = _boom  # type: ignore[method-assign]
        await worker.run_daily_decision(TODAY)
        assert store.decisions[TODAY]["status"] == "failed"
        assert not [c for c in broker.create_calls if c.kind != "stop_limit"]


# ---------------------------------------------------------------------------
# Dedupe + restart idempotency
# ---------------------------------------------------------------------------


class TestDecisionDedupe:
    async def test_same_day_rerun_is_noop(self) -> None:
        worker, _store, broker = await _started_worker()
        await worker.run_daily_decision(TODAY)
        calls_after_first = len(broker.create_calls)
        await worker.run_daily_decision(TODAY)
        assert len(broker.create_calls) == calls_after_first

    async def test_restarted_worker_does_not_double_run(self) -> None:
        worker, store, broker = await _started_worker()
        await worker.run_daily_decision(TODAY)
        calls_after_first = len(broker.create_calls)
        # Fresh worker instance, same store/broker (a container restart).
        worker2, _, _ = await _started_worker(store=store, broker=broker)
        await worker2.run_daily_decision(TODAY)
        assert len(broker.create_calls) == calls_after_first
        assert store.decisions[TODAY]["status"] == "completed"

    async def test_run_tick_spawns_decision_once(self) -> None:
        worker, store, broker = await _started_worker()
        await worker.run_tick(now_utc=NOW)
        assert worker._decision_task is not None
        await worker._decision_task
        assert store.decisions[TODAY]["status"] == "completed"
        calls = len(broker.create_calls)
        await worker.run_tick(now_utc=NOW + timedelta(seconds=30))
        if worker._decision_task is not None:
            await worker._decision_task
        assert len(broker.create_calls) == calls


class TestRestartIdempotency:
    async def test_resume_executes_only_pending_legs(self) -> None:
        store = FakeStore()
        engine_state = serialize_engine_state(
            {
                "BTC": AssetRuntime(contracts=1, applied_dir=1, entry_vwap="165000"),
                "ETH": AssetRuntime(applied_dir=1),
            }
        )
        store.decisions[TODAY] = {
            "status": "dispatching",
            "equity_usd": Decimal("6000"),
            "outcome": {
                "schema_version": "strategy_decision_v1",
                "assets": {
                    "BTC": {
                        "final_target": 1,
                        "action": "buy",
                        "legs": [{"seq": 0, "kind": "open", "delta": 1, "status": "filled"}],
                    },
                    "ETH": {
                        "final_target": 2,
                        "action": "buy",
                        "legs": [{"seq": 0, "kind": "open", "delta": 2, "status": "pending"}],
                    },
                },
            },
            "engine_state": engine_state,
        }
        broker = FakeBroker()
        broker.positions[BTC_PID] = Decimal(1)
        worker, _, _ = await _started_worker(store=store, broker=broker)
        await worker.run_daily_decision(TODAY)

        assert store.decisions[TODAY]["status"] == "completed"
        # Only the ETH leg traded; the filled BTC leg was not re-executed.
        entry_calls = [
            c
            for c in broker.create_calls
            if c.kind == "limit_post_only" and c.product_id == BTC_PID
        ]
        assert entry_calls == []
        eth_calls = [c for c in broker.create_calls if c.product_id == ETH_PID]
        assert any(c.kind == "limit_post_only" for c in eth_calls)
        assert worker.state["ETH"].contracts == 2
        assert worker.state["BTC"].contracts == 1  # restored, untouched

    async def test_duplicate_client_order_id_recovers_not_reorders(self) -> None:
        """Crash after venue placement, before DB write: the re-run's
        deterministic cid hits the duplicate rejection and recovers the
        original order instead of double-ordering (gate A3)."""
        store = FakeStore()
        store.decisions[TODAY] = {
            "status": "dispatching",
            "equity_usd": Decimal("6000"),
            "outcome": {
                "schema_version": "strategy_decision_v1",
                "assets": {
                    "BTC": {
                        "final_target": 1,
                        "action": "buy",
                        "legs": [{"seq": 0, "kind": "open", "delta": 1, "status": "pending"}],
                    },
                    "ETH": {"final_target": 0, "action": "hold", "legs": []},
                },
            },
            "engine_state": serialize_engine_state(
                {"BTC": AssetRuntime(applied_dir=1), "ETH": AssetRuntime()}
            ),
        }
        broker = FakeBroker()
        # The pre-crash venue order already exists under the deterministic id.
        cid = deterministic_client_order_id(
            decision_date=TODAY, asset="BTC", decision_seq=0, purpose="entry", stage=0
        )
        broker.seed_order(
            client_order_id=cid,
            product_id=BTC_PID,
            side="buy",
            contracts=Decimal(1),
            status="filled",
        )
        broker.positions[BTC_PID] = Decimal(1)
        pre_existing_orders = len(broker.orders)

        worker, _, _ = await _started_worker(store=store, broker=broker)
        await worker.run_daily_decision(TODAY)

        assert store.decisions[TODAY]["status"] == "completed"
        # No NEW entry order was created for BTC — only the stop was placed.
        non_stop_new = [
            o for o in broker.orders.values() if o.kind != "stop_limit" and o.client_order_id != cid
        ]
        assert non_stop_new == []
        assert len(broker.orders) >= pre_existing_orders
        assert worker.state["BTC"].contracts == 1
        # Propagation used the recovered venue fill.
        assert any(c == cid for c, _ in store.fills)


# ---------------------------------------------------------------------------
# Halt gate
# ---------------------------------------------------------------------------


class TestHaltGate:
    async def test_halted_state_skips_decision_no_orders(self) -> None:
        store = FakeStore()
        store.risk = RiskStateSnapshot("HALT_NEW", "routine", 0, False)
        worker, _, broker = await _started_worker(store=store)
        await worker.run_daily_decision(TODAY)
        assert store.decisions[TODAY]["status"] == "skipped_risk_state"
        assert broker.create_calls == []

    async def test_gate_rechecked_per_leg_mid_decision_halt(self) -> None:
        """A halt landing between legs stops the remaining legs."""
        store = FakeStore()
        worker, _, _broker = await _started_worker(store=store)

        original_execute = worker._adapter.execute_target_delta

        async def _halt_after_first(**kwargs: Any) -> Any:
            result = await original_execute(**kwargs)
            store.risk = RiskStateSnapshot("HALT_NEW", "routine", 0, False)
            return result

        worker._adapter.execute_target_delta = _halt_after_first  # type: ignore[method-assign]
        await worker.run_daily_decision(TODAY)
        row = store.decisions[TODAY]
        statuses = [
            leg["status"] for a in ("BTC", "ETH") for leg in row["outcome"]["assets"][a]["legs"]
        ]
        assert "skipped_risk_state" in statuses  # the post-halt leg never dispatched

    async def test_convalescent_permits_dispatch(self) -> None:
        store = FakeStore()
        store.risk = RiskStateSnapshot("CONVALESCENT", None, 1, False)
        worker, _, _broker = await _started_worker(store=store)
        await worker.run_daily_decision(TODAY)
        assert store.decisions[TODAY]["status"] == "completed"

    async def test_missing_risk_state_row_fails_closed_no_dispatch(self) -> None:
        """The gate must never treat "cannot answer" as permission: no
        orders, no decision row (the next tick retries), a ONE-SHOT P1,
        and the decision self-heals once the row is restored."""
        store = FakeStore()
        store.risk = None
        worker, _, broker = await _started_worker(store=store)

        await worker.run_daily_decision(TODAY)

        assert broker.create_calls == []
        assert TODAY not in store.decisions  # not terminally skipped
        assert [a[:2] for a in store.alerts] == [("P1", "incident_review_required")]
        assert store.alerts[0][3]["reason"] == "no_risk_state_row"

        await worker.run_daily_decision(TODAY)  # next tick: still blocked
        assert broker.create_calls == []
        assert len(store.alerts) == 1  # alert stays one-shot per episode

        store.risk = RiskStateSnapshot("NORMAL", None, 0, False)  # row restored
        await worker.run_daily_decision(TODAY)
        assert store.decisions[TODAY]["status"] == "completed"

    async def test_dispatch_blocked_one_shot_not_spent_on_failed_insert(self) -> None:
        """#381 review residual (flag-before-insert): a dropped BLOCKED P1
        INSERT must NOT spend the one-shot — the next blocked tick retries
        the alert until one lands."""
        store = FakeStore()
        store.risk = None
        worker, _, broker = await _started_worker(store=store)

        store.insert_alert_delivers = False
        await worker.run_daily_decision(TODAY)
        assert broker.create_calls == []
        assert store.alerts == []  # INSERT dropped
        assert worker._dispatch_blocked_no_risk_state_alerted is False  # one-shot NOT spent

        store.insert_alert_delivers = True
        await worker.run_daily_decision(TODAY)  # next tick: retry lands
        assert [a[:2] for a in store.alerts] == [("P1", "incident_review_required")]
        assert worker._dispatch_blocked_no_risk_state_alerted is True

    async def test_missing_risk_state_row_blocks_resume(self) -> None:
        """The crash-recovery resume path shares the fail-closed rule: a
        'dispatching' row with pending legs must not dispatch through an
        unanswerable gate — and must NOT be terminally skipped either."""
        store = FakeStore()
        store.risk = None
        store.decisions[TODAY] = {
            "status": "dispatching",
            "equity_usd": Decimal("6000"),
            "outcome": {
                "schema_version": "strategy_decision_v1",
                "assets": {
                    "BTC": {
                        "final_target": 1,
                        "action": "buy",
                        "legs": [{"seq": 0, "kind": "open", "delta": 1, "status": "pending"}],
                    },
                    "ETH": {"final_target": 0, "action": "hold", "legs": []},
                },
            },
            "engine_state": serialize_engine_state(
                {"BTC": AssetRuntime(applied_dir=1), "ETH": AssetRuntime()}
            ),
        }
        worker, _, broker = await _started_worker(store=store)

        await worker.run_daily_decision(TODAY)

        assert broker.create_calls == []
        assert store.decisions[TODAY]["status"] == "dispatching"  # retried next tick
        assert [a[:2] for a in store.alerts] == [("P1", "incident_review_required")]

    async def test_leg_gate_fails_closed_when_row_vanishes_mid_decision(self) -> None:
        """A risk_state row vanishing between legs is treated exactly like
        a halt landing between legs: the remaining legs skip."""
        store = FakeStore()
        worker, _, _broker = await _started_worker(store=store)

        original_execute = worker._adapter.execute_target_delta

        async def _drop_row_after_first(**kwargs: Any) -> Any:
            result = await original_execute(**kwargs)
            store.risk = None
            return result

        worker._adapter.execute_target_delta = _drop_row_after_first  # type: ignore[method-assign]
        await worker.run_daily_decision(TODAY)
        row = store.decisions[TODAY]
        statuses = [
            leg["status"] for a in ("BTC", "ETH") for leg in row["outcome"]["assets"][a]["legs"]
        ]
        assert "skipped_risk_state" in statuses  # the post-vanish leg never dispatched


# ---------------------------------------------------------------------------
# Risk loop: client stops, loss limits, halt floor, outage
# ---------------------------------------------------------------------------


class TestClientStop:
    async def test_stop_breach_flattens_and_locks_out_without_fsm(self) -> None:
        worker, store, broker = await _started_worker()
        rt = _open_long(worker, broker, "BTC", contracts=2)
        stop_level = Decimal(rt.client_stop_level or "0")
        worker.marks.store.record(BTC_PID, stop_level - Decimal(1), observed_at_utc=NOW)

        await worker.run_risk_checks(now_utc=NOW)

        # Market flatten hit the venue; position + protection state cleared.
        flattens = [c for c in broker.create_calls if c.kind == "market" and c.side == "sell"]
        assert len(flattens) == 1 and flattens[0].contracts == Decimal(2)
        assert "flatten" in flattens[0].client_order_id
        assert rt.contracts == 0
        assert rt.client_stop_level is None
        # §5 lockout: same-direction re-entry blocked for 2 daily closes.
        assert rt.lockout_dir == 1
        assert rt.lockout_until_ord == TODAY.toordinal() + 2
        assert rt.stopped_on_date == TODAY.isoformat()
        # Stops are routine strategy exits — NOT kill-switch material.
        assert store.kill_switch_calls == []
        # ... but the flatten IS alerted (F1a; existing category, remapped).
        assert ("P2", "margin_auto_trim") in {(a[0], a[1]) for a in store.alerts}
        # The exit propagated downstream (signal + order + fill records).
        assert any(s.get("signal_type") == "exit" for s in store.signals.values())

    async def test_stop_breach_blocks_next_same_direction_decision(self) -> None:
        worker, store, broker = await _started_worker()
        rt = _open_long(worker, broker, "BTC", contracts=2)
        worker.marks.store.record(
            BTC_PID, Decimal(rt.client_stop_level or "0") - 1, observed_at_utc=NOW
        )
        await worker.run_risk_checks(now_utc=NOW)
        assert rt.contracts == 0

        # The next 00:05 decision (decided bar = the stop day) holds flat.
        next_day = NOW + timedelta(days=1)
        worker2, _, _ = await _started_worker(store=store, broker=broker, now=next_day)
        worker2.state = worker.state
        await worker2.run_daily_decision(next_day.date())
        assets = store.decisions[next_day.date()]["outcome"]["assets"]
        assert assets["BTC"]["final_target"] == 0


class TestLossLimits:
    async def test_daily_loss_flattens_and_trips_fsm(self) -> None:
        worker, store, broker = await _started_worker()
        _open_long(worker, broker, "BTC", contracts=2)
        worker._day_start_date = TODAY
        worker._day_start_equity = Decimal("2000")
        broker.equity = Decimal("1700")  # -15% < -8% daily limit

        await worker.run_risk_checks(now_utc=NOW)

        assert store.kill_switch_calls == [TransitionTrigger.DAILY_LOSS_BREACH]
        assert worker.state["BTC"].contracts == 0
        assert any(c.kind == "market" for c in broker.create_calls)
        # F1a: the halt reached the operator alert surface.
        assert ("P1", "kill_switch_invoked") in {(a[0], a[1]) for a in store.alerts}

    async def test_daily_loss_reflattens_when_already_halted(self) -> None:
        """Risk-review 2026-07-16 re-review B1: a venue-recheck skip on
        the first daily-loss flatten must self-heal — the flatten
        re-fires while the breach persists and positions exist even
        under HALT_NEW (F4 pattern, mirroring the floor path); the FSM
        stays retrip-guarded."""
        store = FakeStore()
        store.risk = RiskStateSnapshot("HALT_NEW", "routine", 0, False)
        worker, _, broker = await _started_worker(store=store)
        _open_long(worker, broker, "BTC", contracts=2)
        worker._day_start_date = TODAY
        worker._day_start_equity = Decimal("2000")
        broker.equity = Decimal("1700")  # -15% < -8%, above the $1,500 floor

        await worker.run_risk_checks(now_utc=NOW)

        assert worker.state["BTC"].contracts == 0  # flattened under halt
        assert store.kill_switch_calls == []  # no FSM retrip
        assert any(
            a[0] == "P1" and a[1] == "kill_switch_invoked" and "already HALT_NEW" in a[2]
            for a in store.alerts
        )

    async def test_daily_loss_false_flat_read_self_heals_next_tick(self) -> None:
        """B1 end-to-end: tick 1's flatten venue-recheck gets a false-flat
        read (skip; halt lands); tick 2's recovered read re-flattens the
        stranded position under HALT_NEW."""
        worker, store, broker = await _started_worker()
        rt = _open_long(worker, broker, "BTC", contracts=2)
        worker._day_start_date = TODAY
        worker._day_start_equity = Decimal("2000")
        broker.equity = Decimal("1700")

        real_positions = dict(broker.positions)
        broker.positions = {}  # false-flat venue view
        await worker.run_risk_checks(now_utc=NOW)
        assert rt.contracts == 2  # flatten skipped on the false read
        assert store.kill_switch_calls == [TransitionTrigger.DAILY_LOSS_BREACH]

        broker.positions = dict(real_positions)  # venue reads recover
        store.risk = RiskStateSnapshot("HALT_NEW", "routine", 0, False)
        await worker.run_risk_checks(now_utc=NOW)

        assert rt.contracts == 0  # re-flattened under halt
        assert store.kill_switch_calls == [TransitionTrigger.DAILY_LOSS_BREACH]  # once

    async def test_daily_loss_within_limit_no_action(self) -> None:
        worker, store, broker = await _started_worker()
        _open_long(worker, broker, "BTC", contracts=2)
        worker._day_start_date = TODAY
        worker._day_start_equity = Decimal("2000")
        broker.equity = Decimal("1900")  # -5% > -8%

        await worker.run_risk_checks(now_utc=NOW)
        assert store.kill_switch_calls == []
        assert worker.state["BTC"].contracts == 2

    async def test_weekly_loss_halves_v_target_no_fsm(self) -> None:
        worker, store, broker = await _started_worker()
        store.equity_history[TODAY - timedelta(days=7)] = Decimal("2000")
        broker.equity = Decimal("1600")  # -20% < -16% weekly limit

        await worker.run_risk_checks(now_utc=NOW)

        assert worker._weekly_halved_until == TODAY + timedelta(days=7)
        assert store.kill_switch_calls == []  # engine semantics: halve, not halt

        # The halving reaches the next decision's v_target.
        await worker.run_daily_decision(TODAY)
        row = store.decisions[TODAY]
        assert row["status"] == "completed"
        assert row["outcome"]["weekly_halved"] is True

    async def test_hard_halt_floor_trips_decommission_floor(self) -> None:
        worker, store, broker = await _started_worker()
        _open_long(worker, broker, "BTC", contracts=2)
        broker.equity = Decimal("1400")  # <= $1,500 malfunction floor

        await worker.run_risk_checks(now_utc=NOW)

        assert store.kill_switch_calls == [TransitionTrigger.DECOMMISSION_FLOOR]
        assert worker.state["BTC"].contracts == 0
        assert ("P0", "kill_switch_invoked") in {(a[0], a[1]) for a in store.alerts}
        floor_alert = next(a for a in store.alerts if a[1] == "kill_switch_invoked")
        assert "HALT_NEW (decommission_floor)" in floor_alert[2]
        assert floor_alert[3]["transition_applied"] is True

    async def test_hard_halt_floor_missing_risk_row_alert_is_honest(self) -> None:
        """Tri-state honesty at the floor site: with no risk_state row the
        flatten still runs, but the P0 must say the halt did NOT land
        (and the next tick re-invokes since already_halted stays False)."""
        store = FakeStore()
        store.risk = None
        worker, _, broker = await _started_worker(store=store)
        _open_long(worker, broker, "BTC", contracts=2)
        broker.equity = Decimal("1400")

        await worker.run_risk_checks(now_utc=NOW)

        assert store.kill_switch_calls == []  # nothing transitioned
        assert worker.state["BTC"].contracts == 0  # protective flatten still ran
        floor_alert = next(a for a in store.alerts if a[1] == "kill_switch_invoked")
        assert floor_alert[0] == "P0"
        assert "NOT applied" in floor_alert[2]
        assert "HALT_NEW (decommission_floor)" not in floor_alert[2]
        assert floor_alert[3]["transition_applied"] is False
        assert floor_alert[3]["invoke_result"] == "no_risk_state_row"

    async def test_already_halted_does_not_retrip(self) -> None:
        store = FakeStore()
        store.risk = RiskStateSnapshot("HALT_NEW", "routine", 0, False)
        worker, _, broker = await _started_worker(store=store)
        worker._day_start_date = TODAY
        worker._day_start_equity = Decimal("2000")
        broker.equity = Decimal("1400")
        await worker.run_risk_checks(now_utc=NOW)
        assert store.kill_switch_calls == []

    async def test_liquidation_buffer_force_reduces(self) -> None:
        worker, store, broker = await _started_worker()
        _open_long(worker, broker, "BTC", contracts=4)
        broker.liq_buffer_pct = Decimal("0.10")  # < 30% floor

        await worker.run_risk_checks(now_utc=NOW)

        # Halved (4 → 2), not flattened, not halted.
        assert worker.state["BTC"].contracts == 2
        assert store.kill_switch_calls == []
        assert ("P1", "margin_warn") in {(a[0], a[1]) for a in store.alerts}


class TestOutagePolicy:
    async def test_stale_marks_protected_hold_when_stop_resting(self) -> None:
        worker, _store, broker = await _started_worker()
        rt = _open_long(worker, broker, "BTC", contracts=2)
        # A resting venue stop covers the position.
        stop = broker.seed_order(
            client_order_id="stop-cid",
            product_id=BTC_PID,
            side="sell",
            contracts=Decimal(2),
            status="open",
            kind="stop_limit",
        )
        rt.native_stop_order_id = stop.order_id
        rt.native_stop_client_order_id = "stop-cid"
        # Drop all marks ⇒ stale (the §7 outage condition).
        worker.marks.store._marks.clear()
        assert worker.marks.latest_price(BTC_PID) is None

        await worker.run_risk_checks(now_utc=NOW)

        assert worker.state["BTC"].contracts == 2  # protected hold
        assert not [c for c in broker.create_calls if c.kind == "market"]

    async def test_stale_marks_flattens_unprotected(self) -> None:
        worker, _store, broker = await _started_worker()
        rt = _open_long(worker, broker, "BTC", contracts=2)
        rt.native_stop_order_id = None
        broker.reject_kinds.add("stop_limit")  # re-arm attempts fail too
        worker.marks.store._marks.clear()  # stale marks (outage)

        await worker.run_risk_checks(now_utc=NOW)

        assert worker.state["BTC"].contracts == 0
        assert [c for c in broker.create_calls if c.kind == "market"]
        assert ("P1", "position_unprotected") in {(a[0], a[1]) for a in _store.alerts}

    async def test_outage_snapshot_fetched_under_trade_lock(self) -> None:
        """Risk-review 2026-07-16 note-9 follow-up: the open-orders
        snapshot + covered/unprotected classification must serialize with
        stop arms/cancels — the last stale-snapshot-outside-lock instance.
        The flatten still runs OUTSIDE the lock (it re-acquires; a nested
        acquire would deadlock — this test completing at all pins that)."""
        worker, _store, broker = await _started_worker()
        _open_long(worker, broker, "BTC", contracts=2)  # no resting stop
        lock_states: list[bool] = []
        orig = broker.list_open_orders

        async def _instrumented(product_id: str | None = None) -> list[BrokerOrderState]:
            lock_states.append(worker._trade_lock.locked())
            return await orig(product_id)

        broker.list_open_orders = _instrumented  # type: ignore[method-assign]
        await worker._apply_outage_policy(now_utc=NOW)

        assert lock_states and lock_states[0] is True  # snapshot under lock
        assert worker.state["BTC"].contracts == 0  # unprotected flatten ran

    async def test_native_stop_fill_detected_and_locked_out(self) -> None:
        worker, store, broker = await _started_worker()
        rt = _open_long(worker, broker, "BTC", contracts=2)
        stop = broker.seed_order(
            client_order_id="stop-cid-2",
            product_id=BTC_PID,
            side="sell",
            contracts=Decimal(2),
            status="filled",
            kind="stop_limit",
        )
        rt.native_stop_order_id = stop.order_id
        rt.native_stop_client_order_id = "stop-cid-2"
        broker.positions[BTC_PID] = Decimal(0)  # the venue stop closed it

        # A CONFIRMED stop-fill read bypasses the two-tick divergence
        # latch and acts on the FIRST tick (risk-review blocker: deferring
        # it would leave the flatten paths one tick of stale tracking).
        await worker.run_risk_checks(now_utc=NOW)

        assert rt.contracts == 0
        assert rt.lockout_dir == 1
        assert rt.stopped_on_date == TODAY.isoformat()
        assert any(cid == "stop-cid-2" for cid, _ in store.fills)


# ---------------------------------------------------------------------------
# Heartbeat + status persistence
# ---------------------------------------------------------------------------


class TestHeartbeat:
    async def test_every_tick_upserts_heartbeat_row(self) -> None:
        worker, store, _ = await _started_worker()
        before = store.status_upserts
        await worker.run_tick(now_utc=NOW)
        if worker._decision_task is not None:
            await worker._decision_task
        await worker.run_tick(now_utc=NOW + timedelta(seconds=30))
        assert store.status_upserts >= before + 2
        assert store.status_row is not None
        assert store.status_row.last_decision_date == TODAY

    async def test_heartbeat_file_touched(self, tmp_path: Any) -> None:
        hb = tmp_path / "hb"
        config = StrategyWorkerConfig(heartbeat_file=str(hb))
        worker, _, _ = await _started_worker(config=config)
        await worker.run_tick(now_utc=NOW)
        if worker._decision_task is not None:
            await worker._decision_task
        assert hb.exists()


# ---------------------------------------------------------------------------
# Fail-closed prerequisites
# ---------------------------------------------------------------------------


class TestFailClosed:
    async def test_missing_slippage_head_no_dispatch(self) -> None:
        store = FakeStore()
        store.slippage_id = None
        worker, _, broker = await _started_worker(store=store)
        await worker.run_daily_decision(TODAY)
        assert TODAY not in store.decisions  # transient: retried next tick
        assert broker.create_calls == []

    async def test_missing_parameter_head_no_dispatch(self) -> None:
        store = FakeStore()
        store.param_head = None
        worker, _, broker = await _started_worker(store=store)
        await worker.run_daily_decision(TODAY)
        assert TODAY not in store.decisions
        assert broker.create_calls == []

    async def test_stale_bars_terminal_skip_after_grace(self) -> None:
        worker, store, broker = _build_worker(
            now=datetime(2026, 7, 9, 1, 30, tzinfo=UTC)  # past the 01:00 grace
        )
        worker._market_rest = FakeMarketRest(
            last_session=TODAY - timedelta(days=3)  # venue candles lagging
        )
        await worker.startup_recovery()
        await worker.run_daily_decision(TODAY)
        assert store.decisions[TODAY]["status"] == "skipped_stale_bars"
        assert broker.create_calls == []

    async def test_stale_bars_within_grace_retries(self) -> None:
        worker, store, broker = _build_worker(now=datetime(2026, 7, 9, 0, 10, tzinfo=UTC))
        worker._market_rest = FakeMarketRest(last_session=TODAY - timedelta(days=3))
        await worker.startup_recovery()
        await worker.run_daily_decision(TODAY)
        assert TODAY not in store.decisions  # no terminal row: retry window
        assert broker.create_calls == []


# ---------------------------------------------------------------------------
# Adapter: market flatten + "flatten" purpose ids
# ---------------------------------------------------------------------------


class TestMarketFlatten:
    def test_flatten_purpose_distinct_and_deterministic(self) -> None:
        base = {
            "decision_date": TODAY,
            "asset": "BTC",
            "decision_seq": 7,
            "stage": 0,
        }
        flatten = deterministic_client_order_id(purpose="flatten", **base)  # type: ignore[arg-type]
        entry = deterministic_client_order_id(purpose="entry", **base)  # type: ignore[arg-type]
        stop = deterministic_client_order_id(purpose="stop", **base)  # type: ignore[arg-type]
        assert len({flatten, entry, stop}) == 3
        assert flatten == deterministic_client_order_id(purpose="flatten", **base)  # type: ignore[arg-type]

    async def test_execute_market_flatten_places_market_and_recovers_duplicate(
        self,
    ) -> None:
        broker = FakeBroker()

        async def _no_sleep(_: float) -> None:
            return None

        adapter = CoinbaseExecutionAdapter(client=broker, clock=lambda: NOW, sleep=_no_sleep)
        result = await adapter.execute_market_flatten(
            product=BTC_PRODUCT,
            position_contracts=Decimal(3),
            decision_date=TODAY,
            decision_seq=5,
        )
        assert result.filled_contracts == Decimal(3)
        assert broker.orders[result.order_id].side == "sell"
        n_orders = len(broker.orders)
        # Crash-replay: same seq ⇒ same cid ⇒ recovered, no double order.
        result2 = await adapter.execute_market_flatten(
            product=BTC_PRODUCT,
            position_contracts=Decimal(3),
            decision_date=TODAY,
            decision_seq=5,
        )
        assert result2.client_order_id == result.client_order_id
        assert len(broker.orders) == n_orders

    async def test_execute_market_flatten_rejects_zero_and_fractional(self) -> None:
        broker = FakeBroker()
        adapter = CoinbaseExecutionAdapter(client=broker, clock=lambda: NOW)
        with pytest.raises(ValueError, match="no position"):
            await adapter.execute_market_flatten(
                product=BTC_PRODUCT,
                position_contracts=Decimal(0),
                decision_date=TODAY,
                decision_seq=1,
            )
        with pytest.raises(ValueError, match="integer"):
            await adapter.execute_market_flatten(
                product=BTC_PRODUCT,
                position_contracts=Decimal("1.5"),
                decision_date=TODAY,
                decision_seq=1,
            )


# ---------------------------------------------------------------------------
# Fill-scenario fallback (EXIT_PARTIAL deferral stays visible, not silent)
# ---------------------------------------------------------------------------


class TestFillScenarioFallback:
    async def test_unsupported_scenario_falls_back_minimal(self) -> None:
        worker, store, broker = await _started_worker()
        store.process_fill_supported = False  # simulate EXIT_PARTIAL raise
        _open_long(worker, broker, "BTC", contracts=4)
        broker.liq_buffer_pct = Decimal("0.10")  # forces a partial reduce

        await worker.run_risk_checks(now_utc=NOW)

        assert store.fill_fallbacks  # orders/fills stay truthful
        assert worker.state["BTC"].contracts == 2
        # The failure SHAPE travels with the fallback so the audit note
        # can describe what actually happened (#383 review residual).
        cause = store.fill_fallback_causes[-1]
        assert cause is not None
        assert cause.propagated is False
        assert cause.deferred is True


class TestMinimalFallbackNarrative:
    """#383 review residual: the fallback's ORDER_FILLED audit note used
    to hardcode "EXIT_PARTIAL deferral" for EVERY shape — including
    pre-apply propagation refusals like DUPLICATE_OPEN_TRADE_FOR_MARKET.
    The narrative now keys on the FillPropagationOutcome cause."""

    @staticmethod
    def _store_with_order_row() -> tuple[StrategyWorkerStore, Any]:
        """Real store over a mock session factory whose SELECT returns an
        orders row and whose writes no-op."""
        from contextlib import asynccontextmanager
        from unittest.mock import MagicMock

        def _make_session() -> MagicMock:
            sess = MagicMock()

            @asynccontextmanager
            async def _begin_cm() -> Any:
                yield None

            sess.begin = MagicMock(side_effect=lambda: _begin_cm())

            async def _execute(stmt: Any, params: Any = None) -> MagicMock:
                result = MagicMock()
                row = MagicMock()
                row.id = uuid4()
                row.created_at = NOW
                result.fetchone = MagicMock(return_value=row)
                return result

            sess.execute = _execute
            return sess

        @asynccontextmanager
        async def _factory_cm() -> Any:
            yield _make_session()

        factory = MagicMock()
        factory.side_effect = lambda: _factory_cm()
        store = StrategyWorkerStore(session_factory=factory, env="paper")
        return store, factory

    @staticmethod
    def _payload() -> FillIngestPayload:
        return FillIngestPayload(
            broker_fill_id="cid-x:agg",
            cumulative_filled_quantity=1,
            fill_quantity=1,
            fill_price=Decimal("64000"),
            commission_usd=Decimal("1"),
            filled_at_utc=NOW,
        )

    async def _run_fallback(
        self,
        monkeypatch: pytest.MonkeyPatch,
        cause: FillPropagationOutcome | None,
    ) -> dict[str, Any]:
        from unittest.mock import AsyncMock, MagicMock

        from services.signal import strategy_worker as sw_module

        store, _ = self._store_with_order_row()
        monkeypatch.setattr(store, "order_row_exists", AsyncMock(return_value="pending"))
        captured: dict[str, Any] = {}

        async def _fake_append(session: Any, event_type: Any, payload: Any, **kwargs: Any) -> Any:
            captured["event_type"] = event_type
            captured["payload"] = payload
            rec = MagicMock()
            rec.event_uuid = uuid4()
            return rec

        monkeypatch.setattr(sw_module, "append_audit_event", _fake_append)
        await store.minimal_fill_fallback(
            uuid4(),
            client_order_id="cid-x",
            payload=self._payload(),
            market="BTC",
            fully_filled=True,
            cause=cause,
        )
        return captured

    async def test_refusal_cause_names_the_refusal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = await self._run_fallback(
            monkeypatch,
            FillPropagationOutcome(
                propagated=False,
                deferred=False,
                error_code="DUPLICATE_OPEN_TRADE_FOR_MARKET",
                detail="refuse-at-open: an open trade already exists …",
            ),
        )
        payload = captured["payload"]
        assert payload["fallback_cause"] == "propagation_refused"
        assert payload["error_code"] == "DUPLICATE_OPEN_TRADE_FOR_MARKET"
        assert "REFUSED" in payload["note"]
        assert "replay_leg_fill" in payload["note"]
        assert "EXIT_PARTIAL" not in payload["note"]  # the old hardcoded lie
        assert payload["error_detail"].startswith("refuse-at-open")

    async def test_deferred_cause_reads_as_phase2_deferral(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = await self._run_fallback(
            monkeypatch,
            FillPropagationOutcome(
                propagated=False,
                deferred=True,
                error_code="EXIT_PARTIAL_UNSUPPORTED",
                detail="phase 2 deferral",
            ),
        )
        payload = captured["payload"]
        assert payload["fallback_cause"] == "deferred_scenario"
        assert payload["error_code"] == "EXIT_PARTIAL_UNSUPPORTED"
        assert "Phase-2 deferral" in payload["note"]
        assert "REFUSED" not in payload["note"]

    async def test_no_cause_recorded_as_unspecified(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = await self._run_fallback(monkeypatch, None)
        payload = captured["payload"]
        assert payload["fallback_cause"] == "unspecified"
        assert payload["error_code"] is None
        assert payload["error_detail"] is None


# ---------------------------------------------------------------------------
# Tick-failure halt latch (fail-loud-with-retry)
# ---------------------------------------------------------------------------


class FlakyKillSwitchStore(FakeStore):
    """invoke_kill_switch raises for the first ``fail_times`` attempts,
    then delegates to the FakeStore happy path."""

    def __init__(self, fail_times: int) -> None:
        super().__init__()
        self.fail_times = fail_times
        self.invoke_attempts = 0

    async def invoke_kill_switch(
        self, account_id: UUID, *, trigger: TransitionTrigger, now_utc: datetime
    ) -> KillSwitchInvokeResult:
        self.invoke_attempts += 1
        if self.invoke_attempts <= self.fail_times:
            raise RuntimeError("transient db failure")
        return await super().invoke_kill_switch(account_id, trigger=trigger, now_utc=now_utc)


class TestTickFailureHaltLatch:
    async def test_halt_fires_once_at_threshold_then_latches(self) -> None:
        worker, store, _ = await _started_worker()
        worker._consecutive_tick_failures = RISK_TICK_FAILURE_HALT_THRESHOLD

        await worker._maybe_halt_on_tick_failures()
        await worker._maybe_halt_on_tick_failures()  # next tick: latched, no re-fire

        assert store.kill_switch_calls == [TransitionTrigger.UNHANDLED_EXCEPTION]
        assert worker._tick_failure_halt_fired is True
        assert [a[:2] for a in store.alerts] == [("P1", "kill_switch_invoked")]
        assert store.alerts[0][3]["transition_applied"] is True

    async def test_below_threshold_does_nothing(self) -> None:
        worker, store, _ = await _started_worker()
        worker._consecutive_tick_failures = RISK_TICK_FAILURE_HALT_THRESHOLD - 1

        await worker._maybe_halt_on_tick_failures()

        assert store.kill_switch_calls == []
        assert store.alerts == []
        assert worker._tick_failure_halt_fired is False

    async def test_raised_invoke_leaves_latch_unset_and_next_tick_retries(self) -> None:
        """The regression pin: a raised invoke_kill_switch must NOT latch —
        the halt retries on the next 30 s tick until it lands."""
        flaky = FlakyKillSwitchStore(fail_times=1)
        worker, _, _ = await _started_worker(store=flaky)
        worker._consecutive_tick_failures = RISK_TICK_FAILURE_HALT_THRESHOLD

        await worker._maybe_halt_on_tick_failures()  # attempt 1 raises

        assert worker._tick_failure_halt_fired is False
        # one-shot invoke-failure P1: operator sees "NOT halted yet" on the
        # alert surface instead of nothing (risk-review C2)
        assert [a[:2] for a in flaky.alerts] == [("P1", "kill_switch_invoked")]
        assert flaky.alerts[0][3]["invoke_failed"] is True
        assert flaky.alerts[0][3]["transition_applied"] is False

        await worker._maybe_halt_on_tick_failures()  # next tick: retry succeeds

        assert flaky.invoke_attempts == 2
        assert flaky.kill_switch_calls == [TransitionTrigger.UNHANDLED_EXCEPTION]
        assert worker._tick_failure_halt_fired is True
        assert [a[:2] for a in flaky.alerts] == [
            ("P1", "kill_switch_invoked"),
            ("P1", "kill_switch_invoked"),
        ]
        assert flaky.alerts[1][3]["transition_applied"] is True

    async def test_persistently_failing_invoke_never_latches(self) -> None:
        flaky = FlakyKillSwitchStore(fail_times=99)
        worker, _, _ = await _started_worker(store=flaky)
        worker._consecutive_tick_failures = RISK_TICK_FAILURE_HALT_THRESHOLD

        for _ in range(5):
            await worker._maybe_halt_on_tick_failures()

        assert flaky.invoke_attempts == 5  # one retry per tick, forever
        assert worker._tick_failure_halt_fired is False
        assert len(flaky.alerts) == 1  # the invoke-failure P1 fires ONCE, not per tick

    async def test_already_halted_short_circuit_latches_without_transition(self) -> None:
        worker, store, _ = await _started_worker()
        store.risk = RiskStateSnapshot("HALT_NEW", "routine", 0, False)
        worker._consecutive_tick_failures = RISK_TICK_FAILURE_HALT_THRESHOLD

        await worker._maybe_halt_on_tick_failures()

        assert store.kill_switch_calls == []  # facade short-circuit (already halted)
        assert worker._tick_failure_halt_fired is True  # goal state reached: stop retrying
        assert [a[:2] for a in store.alerts] == [("P1", "kill_switch_invoked")]
        assert store.alerts[0][3]["transition_applied"] is False  # honest alert record
        assert store.alerts[0][3]["invoke_result"] == "already_halted"

    async def test_no_risk_state_row_does_not_latch_and_retries(self) -> None:
        """#378 review N-A regression pin: the missing-row result is
        fail-OPEN — nothing transitioned, the account is NOT halted. The
        latch must stay unset (retry every tick) and the one-shot P1 must
        say "NOT halted", never claim resolution like the fail-safe
        already-halted short-circuit does."""
        worker, store, _ = await _started_worker()
        store.risk = None
        worker._consecutive_tick_failures = RISK_TICK_FAILURE_HALT_THRESHOLD

        await worker._maybe_halt_on_tick_failures()

        assert worker._tick_failure_halt_fired is False  # NOT latched
        assert store.kill_switch_calls == []
        assert [a[:2] for a in store.alerts] == [("P1", "kill_switch_invoked")]
        assert "NOT halted" in store.alerts[0][2]
        assert store.alerts[0][3]["transition_applied"] is False
        assert store.alerts[0][3]["invoke_result"] == "no_risk_state_row"

        await worker._maybe_halt_on_tick_failures()  # still no row
        assert worker._tick_failure_halt_fired is False
        assert len(store.alerts) == 1  # invoke-failure P1 stays one-shot

        store.risk = RiskStateSnapshot("NORMAL", None, 0, False)  # row restored
        await worker._maybe_halt_on_tick_failures()
        assert store.kill_switch_calls == [TransitionTrigger.UNHANDLED_EXCEPTION]
        assert worker._tick_failure_halt_fired is True
        assert store.alerts[1][3]["transition_applied"] is True

    async def test_successful_tick_rearms_halt_latch(self) -> None:
        """Per-episode semantics (risk-review C3): a successful tick ends the
        failure episode and re-arms both latches so a NEW streak can halt
        without a worker restart."""
        worker, _, _ = await _started_worker()
        worker._tick_failure_halt_fired = True
        worker._tick_failure_halt_invoke_alerted = True
        worker._consecutive_tick_failures = 3

        async def one_good_tick() -> None:
            worker.request_stop()

        async def no_marks() -> None:
            return None

        worker.run_tick = one_good_tick  # type: ignore[method-assign]
        worker.marks.run_forever = no_marks  # type: ignore[method-assign]
        await worker.run_forever()

        assert worker._consecutive_tick_failures == 0
        assert worker._tick_failure_halt_fired is False
        assert worker._tick_failure_halt_invoke_alerted is False

    async def test_resume_while_ticks_still_failing_rearms_and_rehalts(self) -> None:
        """#378 review C3: if the operator RESUMES the account while ticks
        are still failing (no clean tick to re-arm on), the latched
        tick-failure halt must re-arm on the observed resume so the
        continuing streak drives HALT_NEW again — never run un-halted on
        a dead risk loop."""
        worker, store, _ = await _started_worker()
        store.risk = RiskStateSnapshot("NORMAL", None, 0, False)
        worker._consecutive_tick_failures = RISK_TICK_FAILURE_HALT_THRESHOLD

        # First streak halts the account.
        await worker._maybe_halt_on_tick_failures()
        assert store.kill_switch_calls == [TransitionTrigger.UNHANDLED_EXCEPTION]
        assert worker._tick_failure_halt_fired is True
        assert store.risk.state == "HALT_NEW"

        # Still latched while genuinely HALT_NEW: no re-halt (negative arm).
        await worker._maybe_halt_on_tick_failures()
        assert len(store.kill_switch_calls) == 1
        assert worker._tick_failure_halt_fired is True

        # Operator resumes to CONVALESCENT while ticks keep failing.
        store.risk = RiskStateSnapshot("CONVALESCENT", None, 1, False)
        await worker._maybe_halt_on_tick_failures()

        # Re-armed on the observed resume → re-halted the continuing streak.
        assert store.kill_switch_calls == [
            TransitionTrigger.UNHANDLED_EXCEPTION,
            TransitionTrigger.UNHANDLED_EXCEPTION,
        ]
        assert worker._tick_failure_halt_fired is True
        assert store.risk.state == "HALT_NEW"
        # A fresh per-episode P1 fired on the re-halt.
        assert len(store.alerts) == 2

    async def test_resume_check_db_error_keeps_latch(self) -> None:
        """The resume check must fail SAFE: an unreadable risk_state
        snapshot keeps the latch (cannot confirm a resume — re-arming on
        a guess could hammer the kill switch)."""
        worker, store, _ = await _started_worker()
        store.risk = RiskStateSnapshot("NORMAL", None, 0, False)
        worker._consecutive_tick_failures = RISK_TICK_FAILURE_HALT_THRESHOLD
        await worker._maybe_halt_on_tick_failures()  # halts + latches
        assert worker._tick_failure_halt_fired is True

        async def _boom(account_id: UUID) -> RiskStateSnapshot | None:
            raise RuntimeError("db down")

        store.fetch_risk_state = _boom  # type: ignore[method-assign]
        await worker._maybe_halt_on_tick_failures()  # latched; resume check raises

        assert len(store.kill_switch_calls) == 1  # no re-halt on an unconfirmable resume
        assert worker._tick_failure_halt_fired is True  # latch kept

    async def test_invoke_alert_one_shot_not_spent_on_failed_insert(self) -> None:
        """#381 review residual (flag-before-insert): a transient DB blip
        that drops the not-landed P1 INSERT must NOT spend the one-shot —
        the next failing tick retries the alert until one lands."""
        worker, store, _ = await _started_worker()
        store.risk = None  # NO_RISK_STATE_ROW → not-landed one-shot path
        worker._consecutive_tick_failures = RISK_TICK_FAILURE_HALT_THRESHOLD

        store.insert_alert_delivers = False
        await worker._maybe_halt_on_tick_failures()
        assert store.alerts == []  # INSERT dropped
        assert worker._tick_failure_halt_invoke_alerted is False  # one-shot NOT spent

        store.insert_alert_delivers = True
        await worker._maybe_halt_on_tick_failures()  # retry lands
        assert [a[:2] for a in store.alerts] == [("P1", "kill_switch_invoked")]
        assert worker._tick_failure_halt_invoke_alerted is True  # now latched


# ---------------------------------------------------------------------------
# Runner config parsing (worker_main)
# ---------------------------------------------------------------------------


class TestWorkerMainConfig:
    def test_defaults_are_phase_a(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from services.signal import worker_main

        for name in (
            "STRATEGY_WORKER_E_EFFECTIVE_CAP_USD",
            "STRATEGY_WORKER_MAX_BTC_CONTRACTS",
            "STRATEGY_WORKER_MAX_ETH_CONTRACTS",
            "API_ENVIRONMENT",
        ):
            monkeypatch.delenv(name, raising=False)
        config = worker_main.build_config()
        assert config.env == "paper"
        assert config.e_effective_cap_usd == Decimal("1500")
        assert config.max_contracts == {"BTC": 2, "ETH": 4}
        assert config.risk_tick_interval_s == 30.0

    def test_scale_up_knobs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from services.signal import worker_main

        monkeypatch.setenv("STRATEGY_WORKER_E_EFFECTIVE_CAP_USD", "none")
        monkeypatch.setenv("STRATEGY_WORKER_MAX_BTC_CONTRACTS", "10")
        monkeypatch.setenv("STRATEGY_WORKER_MAX_ETH_CONTRACTS", "30")
        config = worker_main.build_config()
        assert config.e_effective_cap_usd is None  # C2 full-equity sizing
        assert config.max_contracts == {"BTC": 10, "ETH": 30}

    def test_bad_env_values_fall_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from services.signal import worker_main

        monkeypatch.setenv("STRATEGY_WORKER_E_EFFECTIVE_CAP_USD", "not-a-number")
        monkeypatch.setenv("STRATEGY_WORKER_MAX_BTC_CONTRACTS", "garbage")
        monkeypatch.setenv("API_ENVIRONMENT", "dev")
        config = worker_main.build_config()
        assert config.e_effective_cap_usd == Decimal("1500")
        assert config.max_contracts["BTC"] == 2
        assert config.env == "paper"  # audit env CHECK has no 'dev' trading env

    async def test_missing_credentials_fail_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from services.signal import worker_main

        monkeypatch.setenv("API_DATABASE_URL", "postgresql+asyncpg://s:s@127.0.0.1:0/s")
        monkeypatch.delenv("API_COINBASE_API_KEY_NAME", raising=False)
        monkeypatch.delenv("API_COINBASE_API_PRIVATE_KEY", raising=False)
        assert await worker_main._amain() == 2

    async def test_missing_database_url_fail_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from services.signal import worker_main

        monkeypatch.delenv("API_DATABASE_URL", raising=False)
        assert await worker_main._amain() == 2


# ---------------------------------------------------------------------------
# Protective-exit preemption of an in-flight decision (risk-review F6)
# ---------------------------------------------------------------------------


class TestProtectiveExitPreemption:
    async def test_flatten_preempts_in_flight_decision_task(self) -> None:
        """A protective flatten must not queue behind a ladder stage: the
        in-flight decision task is cancelled (its leg-grain crash-resume
        + deterministic client_order_ids make the interruption safe)."""
        import asyncio

        worker, _store, broker = await _started_worker()
        rt = _open_long(worker, broker, "BTC", contracts=2)

        started = asyncio.Event()

        async def _stuck_decision() -> None:
            async with worker._trade_lock:
                started.set()
                await asyncio.sleep(3600)  # a stage-1 post-only window

        worker._decision_task = asyncio.create_task(_stuck_decision())
        await started.wait()

        # The flatten completes promptly instead of waiting behind the lock.
        await asyncio.wait_for(
            worker._flatten_position("BTC", reason="client_stop", now_utc=NOW, lockout=True),
            timeout=5.0,
        )
        assert worker._decision_task.cancelled()
        assert rt.contracts == 0
        assert [c for c in broker.create_calls if c.kind == "market"]

    async def test_preempted_decision_resumes_next_tick(self) -> None:
        """After preemption the decision row stays 'dispatching' and the
        next attempt resumes it (dedupe never marks it done)."""
        worker, store, _broker = await _started_worker()
        # Seed a dispatching row as if the preempted run persisted it.
        store.decisions[TODAY] = {
            "status": "dispatching",
            "equity_usd": Decimal("6000"),
            "outcome": {
                "schema_version": "strategy_decision_v1",
                "assets": {
                    "BTC": {
                        "final_target": 1,
                        "action": "buy",
                        "legs": [{"seq": 0, "kind": "open", "delta": 1, "status": "pending"}],
                    },
                    "ETH": {"final_target": 0, "action": "hold", "legs": []},
                },
            },
            "engine_state": serialize_engine_state(
                {"BTC": AssetRuntime(applied_dir=1), "ETH": AssetRuntime()}
            ),
        }
        await worker.run_daily_decision(TODAY)
        assert store.decisions[TODAY]["status"] == "completed"
        assert worker.state["BTC"].contracts == 1


# ---------------------------------------------------------------------------
# 2026-07-12 incident regression locks
# ---------------------------------------------------------------------------


class TestFillPropagationSeam:
    """The real store's process_fill must NEVER let a fill-processor
    error escape and kill a decision post-execution (2026-07-12: the
    EXIT_NO_PRIOR_TRADE raise did exactly that)."""

    async def test_fill_processing_error_falls_back_not_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import MagicMock

        from services.risk.fill_processor import FillProcessingError
        from services.signal import strategy_worker as sw_module

        store = StrategyWorkerStore(session_factory=MagicMock(), env="paper")

        async def _boom(**kwargs: Any) -> Any:
            raise FillProcessingError(error_code="EXIT_NO_PRIOR_TRADE", message="no open trade row")

        monkeypatch.setattr(sw_module, "process_fill_event", _boom)
        payload = FillIngestPayload(
            broker_fill_id="cid-1:agg",
            cumulative_filled_quantity=2,
            fill_quantity=2,
            fill_price=Decimal("64000"),
            commission_usd=Decimal("1"),
            filled_at_utc=NOW,
        )
        result = await store.process_fill(client_order_id="cid-1", payload=payload)
        # Caller degrades to minimal_fill_fallback, and the outcome names
        # the REFUSAL shape (not a Phase-2 deferral) for the audit note.
        assert result.propagated is False
        assert result.deferred is False
        assert result.error_code == "EXIT_NO_PRIOR_TRADE"
        assert not result  # __bool__ mirrors .propagated (legacy contract)

    async def test_unsupported_scenario_still_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import MagicMock

        from services.risk.fill_processor import UnsupportedFillScenarioError
        from services.signal import strategy_worker as sw_module

        store = StrategyWorkerStore(session_factory=MagicMock(), env="paper")

        async def _boom(**kwargs: Any) -> Any:
            raise UnsupportedFillScenarioError("phase 2 deferral")

        monkeypatch.setattr(sw_module, "process_fill_event", _boom)
        payload = FillIngestPayload(
            broker_fill_id="cid-2:agg",
            cumulative_filled_quantity=1,
            fill_quantity=1,
            fill_price=Decimal("64000"),
            commission_usd=Decimal("1"),
            filled_at_utc=NOW,
        )
        outcome = await store.process_fill(client_order_id="cid-2", payload=payload)
        assert outcome.propagated is False
        assert outcome.deferred is True  # Phase-2 deferral shape


class TestExternalFlatOrphanedStop:
    """2026-07-12: syncing to venue-flat used to FORGET the native stop
    without cancelling it at the venue — a naked resting order."""

    async def test_external_flat_cancels_resting_stop_then_clears(self) -> None:
        worker, _, broker = await _started_worker()
        rt = worker.state["BTC"]
        rt.contracts = -2
        stop = broker.seed_order(
            client_order_id="stop-cid",
            product_id=BTC_PID,
            side="buy",
            contracts=Decimal(2),
            status="open",
            kind="stop_limit",
        )
        rt.native_stop_order_id = stop.order_id
        rt.native_stop_client_order_id = "stop-cid"

        # Venue reports FLAT (no broker.positions entry) while the worker
        # tracks -2; two consecutive ticks confirm before acting.
        await worker._detect_external_position_changes(now_utc=NOW)
        assert rt.contracts == -2  # first reading latches only
        await worker._detect_external_position_changes(now_utc=NOW)

        assert broker.orders[stop.order_id].status == "cancelled"
        assert rt.contracts == 0
        assert rt.native_stop_order_id is None

    async def test_external_flat_cancel_failure_retries_next_tick(self) -> None:
        worker, _, broker = await _started_worker()
        rt = worker.state["BTC"]
        rt.contracts = -2
        stop = broker.seed_order(
            client_order_id="stop-cid",
            product_id=BTC_PID,
            side="buy",
            contracts=Decimal(2),
            status="open",
            kind="stop_limit",
        )
        rt.native_stop_order_id = stop.order_id

        async def _cancel_boom(order_ids: list[str]) -> dict[str, bool]:
            raise RuntimeError("venue 5xx")

        broker.cancel_orders = _cancel_boom  # type: ignore[method-assign]
        await worker._detect_external_position_changes(now_utc=NOW)  # latch
        await worker._detect_external_position_changes(now_utc=NOW)  # act

        # Unsynced on purpose: the diff re-fires next tick and retries.
        assert rt.contracts == -2
        assert rt.native_stop_order_id == stop.order_id

    async def test_external_flat_cancel_rejected_in_http_200_retries(self) -> None:
        """Risk-review B1: the venue can reject a cancel INSIDE an HTTP
        200 (per-order success:false; adapter raises nothing). The
        worker must verify the book is actually clean before clearing —
        surviving orders leave state unsynced for the next-tick retry."""
        worker, _, broker = await _started_worker()
        rt = worker.state["BTC"]
        rt.contracts = -2
        stop = broker.seed_order(
            client_order_id="stop-cid",
            product_id=BTC_PID,
            side="buy",
            contracts=Decimal(2),
            status="open",
            kind="stop_limit",
        )
        rt.native_stop_order_id = stop.order_id

        async def _cancel_rejected(order_ids: list[str]) -> dict[str, bool]:
            return {oid: False for oid in order_ids}  # 200, but rejected

        broker.cancel_orders = _cancel_rejected  # type: ignore[method-assign]
        await worker._detect_external_position_changes(now_utc=NOW)  # latch
        await worker._detect_external_position_changes(now_utc=NOW)  # act

        assert broker.orders[stop.order_id].status == "open"  # still resting
        assert rt.contracts == -2  # unsynced => next tick retries
        assert rt.native_stop_order_id == stop.order_id  # tracking kept


# ---------------------------------------------------------------------------
# 2026-07-14 incident regression locks (engine-state wipe by stale snapshot)
# ---------------------------------------------------------------------------


class TestExternalDivergenceConfirmLatch:
    """2026-07-14: a positions snapshot captured before the trade lock —
    or a venue read lagging its write surface — reported FLAT while a
    freshly-filled short existed. Acting on that single stale reading
    zeroed tracked contracts AND hysteresis memory (applied_dir) on a
    live position, poisoning the persisted engine state for two nights.
    A divergence must repeat on two consecutive ticks before acting."""

    def _short_with_resting_stop(self, worker: StrategyWorker, broker: FakeBroker) -> AssetRuntime:
        rt = worker.state["BTC"]
        rt.contracts = -2
        rt.applied_dir = -1
        rt.entry_vwap = str(broker.marks[BTC_PID])
        stop = broker.seed_order(
            client_order_id="stop-cid",
            product_id=BTC_PID,
            side="buy",
            contracts=Decimal(2),
            status="open",
            kind="stop_limit",
        )
        rt.native_stop_order_id = stop.order_id
        rt.native_stop_client_order_id = "stop-cid"
        return rt

    async def test_transient_flat_reading_does_not_wipe_state(self) -> None:
        worker, _, broker = await _started_worker(confirmed_long=False)
        rt = self._short_with_resting_stop(worker, broker)

        # Tick 1: venue transiently reads flat (read-after-write gap).
        await worker._detect_external_position_changes(now_utc=NOW)
        assert rt.contracts == -2
        assert rt.applied_dir == -1  # hysteresis memory intact

        # Tick 2: the venue read surface catches up — latch clears.
        broker.positions[BTC_PID] = Decimal(-2)
        await worker._detect_external_position_changes(now_utc=NOW)
        assert rt.contracts == -2
        assert rt.applied_dir == -1
        assert worker._external_divergence_pending == {}

        # Tick 3: another transient flat is a FRESH suspect (no action).
        broker.positions[BTC_PID] = Decimal(0)
        await worker._detect_external_position_changes(now_utc=NOW)
        assert rt.contracts == -2
        assert rt.applied_dir == -1
        assert rt.native_stop_order_id is not None

    async def test_changing_divergent_readings_never_confirm(self) -> None:
        worker, _, broker = await _started_worker(confirmed_long=False)
        rt = self._short_with_resting_stop(worker, broker)
        for reading in (Decimal(0), Decimal(-1), Decimal(1)):
            if reading == 0:
                broker.positions.pop(BTC_PID, None)
            else:
                broker.positions[BTC_PID] = reading
            await worker._detect_external_position_changes(now_utc=NOW)
        assert rt.contracts == -2  # no reading repeated: never acted
        assert rt.applied_dir == -1

    async def test_confirmed_flat_still_acts_on_second_tick(self) -> None:
        """The real external-flat case (2026-07-16 shape) still works —
        one tick later, cancel-first + clear semantics unchanged."""
        worker, _, broker = await _started_worker(confirmed_long=False)
        rt = self._short_with_resting_stop(worker, broker)
        stop_order_id = rt.native_stop_order_id
        assert stop_order_id is not None

        await worker._detect_external_position_changes(now_utc=NOW)
        await worker._detect_external_position_changes(now_utc=NOW)

        assert rt.contracts == 0
        assert rt.applied_dir == 0
        assert broker.orders[stop_order_id].status == "cancelled"

    async def test_tracked_mutation_invalidates_pending_observation(self) -> None:
        """Risk-review finding 2: the latch is keyed on the (tracked,
        venue) pair — a leg/flatten changing tracked contracts between
        ticks must invalidate the pending observation, not confirm it
        against a still-stale venue read."""
        worker, _, broker = await _started_worker(confirmed_long=False)
        rt = self._short_with_resting_stop(worker, broker)
        stop_order_id = rt.native_stop_order_id
        assert stop_order_id is not None

        await worker._detect_external_position_changes(now_utc=NOW)  # (-2, 0) latched
        rt.contracts = 2  # a leg flipped the book between ticks
        rt.applied_dir = 1
        await worker._detect_external_position_changes(now_utc=NOW)  # (2, 0) != pending

        assert rt.contracts == 2  # not zeroed
        assert rt.applied_dir == 1  # hysteresis intact
        assert broker.orders[stop_order_id].status == "open"  # stop NOT cancelled

    async def test_confirmed_stop_fill_bypasses_latch_same_tick(self) -> None:
        """Risk-review blocker: a get_order read returning 'filled' is
        positive confirmation — it must act on the FIRST divergent tick,
        before the client-stop/outage flatten paths can run on stale
        tracked contracts."""
        worker, store, broker = await _started_worker(confirmed_long=False)
        rt = worker.state["BTC"]
        rt.contracts = -2
        rt.applied_dir = -1
        stop = broker.seed_order(
            client_order_id="stop-cid",
            product_id=BTC_PID,
            side="buy",
            contracts=Decimal(2),
            status="filled",  # the venue backstop fired
            kind="stop_limit",
        )
        rt.native_stop_order_id = stop.order_id
        rt.native_stop_client_order_id = "stop-cid"

        await worker._detect_external_position_changes(now_utc=NOW)

        assert rt.contracts == 0  # absorbed on tick 1, no latch deferral
        assert rt.stopped_on_date == TODAY.isoformat()
        assert any(cid == "stop-cid" for cid, _ in store.fills)

    async def test_positions_snapshot_fetched_under_trade_lock(self) -> None:
        """The snapshot must be captured INSIDE the trade lock — a
        pre-lock snapshot is stale by construction whenever a decision
        leg fills while the detector awaits the lock."""
        worker, _, broker = await _started_worker()
        lock_states: list[bool] = []
        orig = broker.list_positions

        async def _instrumented() -> list[BrokerPosition]:
            lock_states.append(worker._trade_lock.locked())
            return await orig()

        broker.list_positions = _instrumented  # type: ignore[method-assign]
        await worker._detect_external_position_changes(now_utc=NOW)
        assert lock_states == [True]

    async def test_positions_fetch_failure_is_a_no_op(self) -> None:
        worker, _, broker = await _started_worker(confirmed_long=False)
        rt = self._short_with_resting_stop(worker, broker)

        async def _boom() -> list[BrokerPosition]:
            raise RuntimeError("venue 5xx")

        broker.list_positions = _boom  # type: ignore[method-assign]
        await worker._detect_external_position_changes(now_utc=NOW)
        assert rt.contracts == -2
        assert rt.applied_dir == -1


class TestNativeStopFillSameTick:
    """Risk-review blocker regression: when the native 3xATR stop
    genuinely filled, the mark is beyond the 2xATR client level by
    construction — the fill must be absorbed in the SAME risk tick,
    BEFORE the client-stop path can market-flatten stale tracked
    contracts against a flat book (the flatten is not reduce-only: it
    would open a full-size reversed position)."""

    async def test_stop_fill_with_mark_through_client_level_no_reversal(self) -> None:
        worker, _store, broker = await _started_worker()
        rt = _open_long(worker, broker, "BTC", contracts=2)
        stop = broker.seed_order(
            client_order_id="stop-cid-3",
            product_id=BTC_PID,
            side="sell",
            contracts=Decimal(2),
            status="filled",
            kind="stop_limit",
        )
        rt.native_stop_order_id = stop.order_id
        rt.native_stop_client_order_id = "stop-cid-3"
        broker.positions[BTC_PID] = Decimal(0)  # venue flat post-fill
        # Mark moved through the client stop level (a realistic 3xATR fill).
        worker.marks.store.record(
            BTC_PID, Decimal(rt.client_stop_level or "0") - Decimal(1), observed_at_utc=NOW
        )

        await worker.run_risk_checks(now_utc=NOW)

        assert rt.contracts == 0  # fill absorbed same tick
        assert rt.stopped_on_date == TODAY.isoformat()
        # NO market order fired on the flat book.
        assert not [c for c in broker.create_calls if c.kind == "market"]


class TestFlattenVenueRecheck:
    """Risk-review blocker: the protective market flatten is not
    reduce-only at the venue — `_flatten_position` re-reads positions
    under the trade lock and refuses to fire against a book that does
    not carry the tracked position."""

    async def test_client_stop_flatten_skipped_when_venue_flat(self) -> None:
        worker, _store, broker = await _started_worker()
        rt = _open_long(worker, broker, "BTC", contracts=2)
        broker.positions[BTC_PID] = Decimal(0)  # venue already flat, unabsorbed
        worker.marks.store.record(
            BTC_PID, Decimal(rt.client_stop_level or "0") - Decimal(1), observed_at_utc=NOW
        )

        await worker.run_risk_checks(now_utc=NOW)

        # No market order on the flat book; tracking left for the
        # detector's two-tick reconcile.
        assert not [c for c in broker.create_calls if c.kind == "market"]
        assert rt.contracts == 2

    async def test_flatten_clamps_to_smaller_venue_size(self) -> None:
        worker, _store, broker = await _started_worker()
        rt = _open_long(worker, broker, "BTC", contracts=4)
        broker.positions[BTC_PID] = Decimal(2)  # venue partially closed already
        worker.marks.store.record(
            BTC_PID, Decimal(rt.client_stop_level or "0") - Decimal(1), observed_at_utc=NOW
        )

        await worker.run_risk_checks(now_utc=NOW)

        flattens = [c for c in broker.create_calls if c.kind == "market" and c.side == "sell"]
        assert len(flattens) == 1
        assert flattens[0].contracts == Decimal(2)  # clamped to venue truth

    async def test_flatten_proceeds_when_recheck_read_fails(self) -> None:
        """A protective exit must not be blocked by a venue read failure."""
        worker, _store, broker = await _started_worker()
        rt = _open_long(worker, broker, "BTC", contracts=2)
        worker.marks.store.record(
            BTC_PID, Decimal(rt.client_stop_level or "0") - Decimal(1), observed_at_utc=NOW
        )

        async def _boom() -> list[BrokerPosition]:
            raise RuntimeError("venue 5xx")

        broker.list_positions = _boom  # type: ignore[method-assign]
        await worker.run_risk_checks(now_utc=NOW)

        assert rt.contracts == 0  # flatten proceeded on tracked state
        assert [c for c in broker.create_calls if c.kind == "market"]


class TestStopArmLockDiscipline:
    """2026-07-14: the decision dispatch armed the native stop OUTSIDE
    the trade lock, so the external-change detector interleaved with the
    arm and mutated the same AssetRuntime mid-flight. Every stop-arm
    call site must hold the trade lock."""

    async def test_decision_stop_arm_runs_under_trade_lock(self) -> None:
        worker, store, _broker = await _started_worker()
        lock_states: list[tuple[str, bool]] = []
        orig_arm = worker._arm_native_stop

        async def _instrumented(asset: str, **kwargs: Any) -> None:
            lock_states.append((str(kwargs.get("reason", "")), worker._trade_lock.locked()))
            await orig_arm(asset, **kwargs)

        worker._arm_native_stop = _instrumented  # type: ignore[method-assign]
        await worker.run_daily_decision(TODAY)

        assert store.decisions[TODAY]["status"] == "completed"
        decision_arms = [(r, locked) for r, locked in lock_states if r == "decision"]
        assert decision_arms  # the confirmed-long decision opened + armed
        assert all(locked for _, locked in decision_arms)

    async def test_risk_loop_rearm_runs_under_trade_lock(self) -> None:
        worker, _store, broker = await _started_worker()
        rt = _open_long(worker, broker, "BTC", contracts=2)
        rt.native_stop_order_id = None  # missing backstop → 2b re-arm
        lock_states: list[tuple[str, bool]] = []
        orig_arm = worker._arm_native_stop

        async def _instrumented(asset: str, **kwargs: Any) -> None:
            lock_states.append((str(kwargs.get("reason", "")), worker._trade_lock.locked()))
            await orig_arm(asset, **kwargs)

        worker._arm_native_stop = _instrumented  # type: ignore[method-assign]
        await worker.run_risk_checks(now_utc=NOW)

        rearms = [(r, locked) for r, locked in lock_states if r == "risk_loop_rearm"]
        assert rearms
        assert all(locked for _, locked in rearms)
        assert rt.native_stop_order_id is not None


class TestEngineStateDivergenceTripwire:
    """2026-07-15 anomaly: the decision silently ran hysteresis-hold on a
    live short whose applied_dir had been wiped to 0. A live position
    with no (or opposite-sign) applied direction must alert same-day."""

    async def test_position_with_zero_applied_dir_alerts(self) -> None:
        worker, store, broker = await _started_worker(confirmed_long=False)
        rt = _open_long(worker, broker, "BTC", contracts=2)
        rt.applied_dir = 0  # the 2026-07-14 corruption shape

        await worker.run_daily_decision(TODAY)

        alerts = [a for a in store.alerts if a[1] == "incident_review_required"]
        assert len(alerts) == 1
        severity, _category, message, detail = alerts[0]
        assert severity == "P2"
        assert "diverged from position truth" in message
        assert detail["asset"] == "BTC"
        assert detail["applied_dir"] == 0
        assert detail["contracts"] == 2
        # The decision itself still ran on the engine's authority.
        assert store.decisions[TODAY]["status"] == "completed"

    async def test_sign_mismatch_alerts(self) -> None:
        worker, store, broker = await _started_worker(confirmed_long=False)
        rt = _open_long(worker, broker, "BTC", contracts=2)
        rt.applied_dir = -1  # opposite sign vs the long position

        await worker.run_daily_decision(TODAY)

        assert [a for a in store.alerts if a[1] == "incident_review_required"]

    async def test_healthy_state_no_alert(self) -> None:
        worker, store, broker = await _started_worker()
        _open_long(worker, broker, "BTC", contracts=2)  # applied_dir=1 set

        await worker.run_daily_decision(TODAY)

        assert not [a for a in store.alerts if a[1] == "incident_review_required"]

    async def test_resume_path_alerts_on_poisoned_row_state(self) -> None:
        """Risk-review finding 4: a crash-resume restores engine_state
        from the decision row — the exact 2026-07-14 corruption vector —
        and must run the same tripwire."""
        worker, store, broker = await _started_worker(confirmed_long=False)
        broker.positions[BTC_PID] = Decimal(-2)
        store.decisions[TODAY] = {
            "status": "dispatching",
            "equity_usd": Decimal("6000"),
            "outcome": {
                "schema_version": "strategy_decision_v1",
                "assets": {
                    "BTC": {
                        "final_target": -2,
                        "action": "sell",
                        "legs": [{"seq": 0, "kind": "open", "delta": -2, "status": "filled"}],
                    },
                    "ETH": {"final_target": 0, "action": "hold", "legs": []},
                },
            },
            "engine_state": serialize_engine_state(
                {
                    "BTC": AssetRuntime(contracts=-2, applied_dir=0),  # poisoned
                    "ETH": AssetRuntime(),
                }
            ),
        }

        await worker.run_daily_decision(TODAY)

        alerts = [a for a in store.alerts if a[1] == "incident_review_required"]
        assert len(alerts) == 1
        assert alerts[0][3]["source"] == "strategy_worker_resume_decision"

    async def test_alert_is_one_shot_per_decision_date_and_asset(self) -> None:
        """Note 5: a crash between the tripwire and the decision row leaves
        the fresh path re-entered every 30 s — the alert must not re-fire
        for the same (decision_date, asset) once delivered."""
        worker, store, broker = await _started_worker(confirmed_long=False)
        rt = _open_long(worker, broker, "BTC", contracts=2)
        rt.applied_dir = 0

        await worker._alert_engine_state_divergence(decision_date=TODAY, now_utc=NOW, source="test")
        await worker._alert_engine_state_divergence(decision_date=TODAY, now_utc=NOW, source="test")

        assert len([a for a in store.alerts if a[1] == "incident_review_required"]) == 1
