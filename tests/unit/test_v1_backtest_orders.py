"""Unit tests for the LEAN-side backtest-only order placement (charter PR A + PR B).

``lean/v1_strategy.py`` is the LIVE production strategy. PR A adds a
backtest-only path that places REAL LEAN orders mirroring the signals V1 already
computes, so a backtest yields an authoritative equity curve. PR B (the
order-routing fix) makes those orders FILLABLE and the position state stable
across futures rolls:

  * the mapped front is explicitly subscribed each cycle (LEAN feeds per-expiry
    securities lazily under a continuous-only subscription — probe-measured
    months-long ``price == 0`` stretches during which orders are rejected
    Invalid);
  * position state is MARKET-level (legs summed across contract months — the
    per-expiry read was the daily re-emit bug);
  * rolls are RECORDED at the SymbolChangedEvent and consolidated at the next
    cycle (ordering at the event moment is impossible: the new front has no bar
    yet and LEAN rejects zero-priced orders synchronously);
  * reconciliation is pending-aware (calendar-day cycles would otherwise stack
    weekend duplicates of the same delta);
  * entry session dates are tracked so the MIN_HOLDING_DAYS gate engages in
    backtest (LEAN holdings carry no ``invested_since`` in this build).

The orders are sized by the CANONICAL Stage 0-5 pipeline
(``services/risk/sizing.py``) loaded by FILE PATH (the package ``__init__``
drags sqlalchemy + ``services.audit``, absent in the LEAN image). The hard
safety invariant is that this whole path is **provably unreachable in live
mode** — the live strategy stays POST-only (zero LEAN orders).

The module ``from __future__ import annotations`` + a stubbed ``QCAlgorithm``
lets us import it outside the LEAN runtime and unit-test the helpers + the
order-placement gate directly. The real sizer is loaded by path here too and
injected into ``algo._sizing_pipeline_module`` so the tests exercise the REAL
Stage 0-5 math — not a mock.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import types
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

# ---- Stub the LEAN runtime symbols the module needs, then import. ----
_stub = types.ModuleType("AlgorithmImports")


class _QCAlgorithm:  # minimal base; the methods under test don't call into it.
    ...


class _Resolution:  # referenced by the backtest-only subscription helpers
    DAILY = "daily"


_stub.QCAlgorithm = _QCAlgorithm
_stub.Resolution = _Resolution
_stub.__all__ = ["QCAlgorithm", "Resolution"]
sys.modules.setdefault("AlgorithmImports", _stub)

from lean import v1_strategy  # noqa: E402
from strategies.v1_trend_following.signals import Direction  # noqa: E402

# Test-order independence: other test files (test_lean_live_positions,
# test_lean_history_adapter, ...) register their OWN AlgorithmImports stub
# first under pytest's alphabetical collection, so the setdefault above loses
# and ``from AlgorithmImports import *`` ran against a stub WITHOUT
# ``Resolution``. The subscription helpers reference ``Resolution.DAILY`` at
# call time from the module globals — patch it onto the imported module so the
# helpers work regardless of which stub won.
if not hasattr(v1_strategy, "Resolution"):
    v1_strategy.Resolution = _Resolution

# ---------------------------------------------------------------------------
# Load the REAL Stage 0-5 sizer by file path — the same mechanism production
# uses (``_load_sizing_pipeline``), but pointed at the repo copy. This bypasses
# ``services/risk/__init__.py`` (sqlalchemy + services.audit), so the test has no
# DB dependency and exercises the genuine pipeline, not a fake.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_real_sizing():
    path = _REPO_ROOT / "services" / "risk" / "sizing.py"
    spec = importlib.util.spec_from_file_location("v1_backtest_sizing_pipeline_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: sizing.py's ``@dataclass(slots=True)`` under
    # ``from __future__ import annotations`` makes the dataclass machinery look up
    # ``sys.modules[cls.__module__]`` during class creation (mirrors the production
    # loader ``V1TrendFollowingAlgorithm._load_sizing_pipeline``).
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_REAL_SIZING = _load_real_sizing()


# ---------------------------------------------------------------------------
# Fakes for the LEAN runtime surfaces the order path touches.
# ---------------------------------------------------------------------------


class _SymbolProperties:
    def __init__(self, multiplier) -> None:
        self.contract_multiplier = multiplier


class _Security:
    def __init__(self, *, price, multiplier=1, mapped=None) -> None:
        self.price = price
        self.symbol_properties = _SymbolProperties(multiplier)
        self.mapped = mapped


class _Holding:
    def __init__(self, quantity: int, average_price=0) -> None:
        self.quantity = quantity
        self.average_price = average_price


class _FakeSymbol:
    """Duck-typed LEAN Symbol: hashable, equality by value, optional canonical.

    Real LEAN dated future contracts expose ``.canonical`` (their continuous
    parent); equities raise on the property — fakes for non-futures simply
    omit the attribute (plain strings), which the production helpers treat the
    same way (skip).
    """

    def __init__(self, value: str, canonical=None) -> None:
        self.value = value
        if canonical is not None:
            self.canonical = canonical

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"Sym({self.value})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, _FakeSymbol):
            return other.value == self.value
        if isinstance(other, str):
            return other == self.value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.value)


class _FakePortfolio(dict):
    """Dict whose iteration yields LEAN-style KeyValuePairs (``.key``/``.value``).

    Probe-verified: pythonnet portfolio iteration yields kvp objects with
    lowercase ``key``/``value``; ``portfolio[symbol]`` indexes directly.
    """

    def __iter__(self):  # type: ignore[override]
        return iter([types.SimpleNamespace(key=k, value=v) for k, v in self.items()])


class _FakeTransactions:
    def __init__(self, open_orders=()) -> None:
        self._open = list(open_orders)

    def get_open_orders(self):
        return list(self._open)


class _Signal:
    """Stand-in for ``CandidateSignal`` — only the fields the path reads."""

    def __init__(self, *, market, direction, decision_price=None, exit_reason=None) -> None:
        self.market = market
        self.direction = direction
        self.decision_price = decision_price
        self.exit_reason = exit_reason


class _ExitResult:
    def __init__(self, signals) -> None:
        self.signals = signals


class _Bar:
    def __init__(self, session_date, close) -> None:
        self.session_date = session_date
        self.close = close


class _Series:
    """Stand-in for ``BarSeries`` — exposes ``.bars[i].close`` + ``.session_date``."""

    def __init__(self, closes, *, start=date(2024, 1, 2)) -> None:
        self.bars = [_Bar(start + timedelta(days=i), Decimal(str(c))) for i, c in enumerate(closes)]


def _bare_algo(*, backtest: bool, with_sizer: bool = False):
    algo = object.__new__(v1_strategy.V1TrendFollowingAlgorithm)
    algo._backtest_orders_enabled = backtest
    algo._sizing_pipeline_module = _REAL_SIZING if with_sizer else None
    algo._market_subscriptions = {}
    algo._backtest_pending_rolls = {}
    algo._backtest_entry_dates = {}
    algo.log = lambda *_a, **_k: None
    algo._orders: list = []
    algo.market_order = lambda symbol, qty, tag=None: algo._orders.append((symbol, qty, tag))
    algo._subscribed: list = []
    algo.securities = {}
    algo.portfolio = _FakePortfolio()

    def _add_future_contract(symbol, *_a, **_k):
        algo._subscribed.append(symbol)
        existing = algo.securities.get(symbol)
        return existing if existing is not None else _Security(price=Decimal("0"))

    algo.add_future_contract = _add_future_contract
    return algo


def _mes_setup(algo, *, front_price="5000", legs=()):
    """Wire a /MES market: continuous 'MES_C', mapped front Sym(MESM6).

    ``legs`` = iterable of (value, qty, avg_price) dated-contract holdings,
    each with canonical == the continuous symbol.
    """
    front = _FakeSymbol("MESM6", canonical="MES_C")
    algo._market_subscriptions = {"/MES": "MES_C"}
    algo.securities = {
        "MES_C": _Security(price=Decimal(front_price), multiplier=5, mapped=front),
        front: _Security(price=Decimal(front_price), multiplier=5),
    }
    for value, qty, avg in legs:
        algo.portfolio[_FakeSymbol(value, canonical="MES_C")] = _Holding(qty, avg)
    return front


# A tight, strictly-varying low-vol series → small annualized vol so the Stage-2
# per-position cap (25% x equity) binds to a deterministic contract count, AND a
# strictly-positive variance (never a degenerate covariance window).
_LOWVOL = [100, 100.1, 100.05, 100.12, 100.08, 100.15, 100.1, 100.18, 100.12, 100.2]
_LOWVOL_2 = [100, 100.08, 100.13, 100.06, 100.16, 100.1, 100.17, 100.09, 100.15, 100.19]

_PLACE_KWARGS = dict(
    equity=Decimal("100000"),
    vol_target_pct_annual=Decimal("0.15"),
    instrument_vol_lookback_days=6,
    session_date=date(2024, 1, 15),
)


# ---------------------------------------------------------------------------
# Pure input-builder helpers (no LEAN runtime / no sizer needed).
# ---------------------------------------------------------------------------


class TestDailyReturnsByDate:
    def test_known_series(self) -> None:
        bars = _Series([100, 110, 99]).bars  # returns +0.10, -0.10
        out = v1_strategy._daily_returns_by_date(bars, 2)
        assert len(out) == 2
        vals = [out[d] for d in sorted(out)]
        assert abs(vals[0] - 0.10) < 1e-12
        assert abs(vals[1] - (-0.10)) < 1e-12

    def test_lookback_below_two_is_empty(self) -> None:
        bars = _Series([100, 110, 99]).bars
        assert v1_strategy._daily_returns_by_date(bars, 1) == {}
        assert v1_strategy._daily_returns_by_date([], 5) == {}

    def test_window_is_tail_of_lookback_plus_one(self) -> None:
        bars = _Series([1, 2, 3, 4, 5, 6]).bars
        # lookback 2 → last 3 closes (4,5,6) → 2 returns.
        out = v1_strategy._daily_returns_by_date(bars, 2)
        assert len(out) == 2


class TestAnnualizedCovariance:
    def test_single_market_is_1x1(self) -> None:
        import numpy as np

        rbm = {"/MES": v1_strategy._daily_returns_by_date(_Series(_LOWVOL).bars, 5)}
        result = v1_strategy._annualized_covariance(rbm)
        assert result is not None
        idx, sigma = result
        assert idx == ("/MES",)
        assert sigma.shape == (1, 1)
        # diagonal == sample variance * 252.
        row = [rbm["/MES"][d] for d in sorted(rbm["/MES"])]
        assert abs(float(sigma[0, 0]) - float(np.var(row, ddof=1)) * 252) < 1e-9

    def test_two_markets_symmetric_2x2(self) -> None:
        rbm = {
            "TLT": v1_strategy._daily_returns_by_date(_Series(_LOWVOL).bars, 6),
            "IEF": v1_strategy._daily_returns_by_date(_Series(_LOWVOL_2).bars, 6),
        }
        result = v1_strategy._annualized_covariance(rbm)
        assert result is not None
        idx, sigma = result
        assert set(idx) == {"TLT", "IEF"}
        assert sigma.shape == (2, 2)
        assert abs(float(sigma[0, 1]) - float(sigma[1, 0])) < 1e-12  # symmetric
        assert float(sigma[0, 0]) > 0 and float(sigma[1, 1]) > 0  # positive variances

    def test_zero_variance_market_dropped(self) -> None:
        rbm = {
            "TLT": v1_strategy._daily_returns_by_date(_Series(_LOWVOL).bars, 6),
            "FLAT": v1_strategy._daily_returns_by_date(_Series([100] * 8).bars, 6),  # constant
        }
        result = v1_strategy._annualized_covariance(rbm)
        assert result is not None
        idx, sigma = result
        assert idx == ("TLT",)  # the constant-price market is dropped
        assert sigma.shape == (1, 1)

    def test_too_few_common_dates_is_none(self) -> None:
        # Disjoint date ranges → empty intersection → None.
        a = _Series(_LOWVOL, start=date(2024, 1, 2)).bars
        b = _Series(_LOWVOL, start=date(2025, 1, 2)).bars
        rbm = {
            "A": v1_strategy._daily_returns_by_date(a, 6),
            "B": v1_strategy._daily_returns_by_date(b, 6),
        }
        assert v1_strategy._annualized_covariance(rbm) is None

    def test_all_markets_too_short_is_none(self) -> None:
        rbm = {"A": {}, "B": {date(2024, 1, 2): 0.01}}  # <2 returns each
        assert v1_strategy._annualized_covariance(rbm) is None


class TestTotalReturn:
    def test_known_return(self) -> None:
        bars = _Series([100, 101, 102, 103, 104, 110]).bars  # last over close 5 ago
        r = v1_strategy._total_return(bars, 5)
        assert r == (Decimal("110") - Decimal("100")) / Decimal("100")

    def test_insufficient_data_is_zero(self) -> None:
        assert v1_strategy._total_return(_Series([100, 101]).bars, 5) == Decimal("0")
        assert v1_strategy._total_return([], 5) == Decimal("0")


class TestClusterMap:
    def test_known_clusters(self) -> None:
        assert v1_strategy._V1_CLUSTER_MAP["/MES"] == "equity_index"
        assert v1_strategy._V1_CLUSTER_MAP["/MGC"] == "commodity"
        assert v1_strategy._V1_CLUSTER_MAP["/MBT"] == "crypto"
        assert v1_strategy._V1_CLUSTER_MAP["TLT"] == "rates"

    def test_covers_the_whole_v1_universe(self) -> None:
        # Guard against silent divergence: a market added to the canonical V1
        # universe but NOT to this hand-pinned map would fall back to a singleton
        # cluster (``_V1_CLUSTER_MAP.get(market, market)``) and distort the Stage-3
        # cluster cap. Fail loudly here instead of mis-sizing in a backtest.
        from strategies.v1_trend_following.parameters import V1_CANDIDATE_UNIVERSE

        missing = set(V1_CANDIDATE_UNIVERSE) - set(v1_strategy._V1_CLUSTER_MAP)
        assert not missing, f"V1 universe markets missing a cluster: {sorted(missing)}"


class TestLoadSizingPipeline:
    def test_missing_mount_raises_clear_error(self) -> None:
        # No injected module + the container path is absent in the test env →
        # a clear RuntimeError pointing at the missing services/ mount.
        algo = _bare_algo(backtest=True, with_sizer=False)
        with pytest.raises(RuntimeError, match="services/"):
            algo._load_sizing_pipeline()

    def test_injected_module_is_returned_cached(self) -> None:
        algo = _bare_algo(backtest=True, with_sizer=True)
        assert algo._load_sizing_pipeline() is _REAL_SIZING


# ---------------------------------------------------------------------------
# LIVE-MODE SAFETY: the gate is off → NO orders ever placed.
# ---------------------------------------------------------------------------


class TestLiveModePlacesNoOrders:
    def test_place_backtest_orders_is_noop_in_live(self) -> None:
        algo = _bare_algo(backtest=False)  # live: gate off (no sizer injected)
        algo._market_subscriptions = {"TLT": "TLT_SYM"}
        algo.securities = {"TLT_SYM": _Security(price=Decimal("90"), multiplier=1)}
        entry = _Signal(market="TLT", direction=Direction.LONG, decision_price=Decimal("90"))

        algo._place_backtest_orders(
            active_universe={"TLT": _Series(_LOWVOL)},
            entry_signals=(entry,),
            exit_result=None,
            **_PLACE_KWARGS,
        )
        assert algo._orders == []  # provably unreachable in live mode
        assert algo._subscribed == []  # no backtest subscriptions either

    def test_live_never_loads_the_sizer(self) -> None:
        # Extra belt-and-braces: even with NO sizer injected, live mode must not
        # try to load one (the gate returns first). A load attempt would raise.
        algo = _bare_algo(backtest=False)
        algo._market_subscriptions = {"TLT": "TLT_SYM"}
        algo.securities = {"TLT_SYM": _Security(price=Decimal("90"), multiplier=1)}
        entry = _Signal(market="TLT", direction=Direction.LONG, decision_price=Decimal("90"))
        algo._place_backtest_orders(
            active_universe={"TLT": _Series(_LOWVOL)},
            entry_signals=(entry,),
            exit_result=None,
            **_PLACE_KWARGS,
        )
        assert algo._sizing_pipeline_module is None  # never loaded in live mode

    def test_on_symbol_changed_events_is_noop_in_live(self) -> None:
        algo = _bare_algo(backtest=False)  # live: gate off
        algo.portfolio["OLD"] = _Holding(3)
        events = {"k": types.SimpleNamespace(old_symbol="OLD", new_symbol="NEW", symbol="MES_C")}

        algo.on_symbol_changed_events(events)
        assert algo._orders == []  # live roll handling is the api's job, not LEAN's
        assert algo._backtest_pending_rolls == {}  # nothing recorded either

    def test_consolidation_is_noop_in_live(self) -> None:
        algo = _bare_algo(backtest=False)
        algo._backtest_pending_rolls = {"/MES": ("OLD", "NEW")}
        algo.portfolio["OLD"] = _Holding(3)
        algo._consolidate_pending_rolls(date(2024, 1, 15))
        assert algo._orders == []
        assert algo._backtest_pending_rolls == {"/MES": ("OLD", "NEW")}  # untouched


class TestSourceTripwires:
    """Lock the live-safety shape of the SOURCE itself (PR-B hardening).

    The behavioral tests above pin "gate off → no orders" but inject the gate
    directly; these tripwires pin (a) the gate's DERIVATION from ``live_mode``
    and (b) that every LEAN order call site stays inside the two gated
    backtest-only methods — so a future edit can't quietly re-enable orders in
    live mode or add an ungated order path without failing here.
    """

    _SOURCE = (_REPO_ROOT / "lean" / "v1_strategy.py").read_text(encoding="utf-8")

    def test_gate_is_derived_from_live_mode(self) -> None:
        assert "self._backtest_orders_enabled: bool = not bool(self.live_mode)" in self._SOURCE, (
            "the master gate must be derived from live_mode, set once in initialize"
        )

    def test_gate_is_assigned_exactly_once(self) -> None:
        # A second assignment anywhere (e.g. `self._backtest_orders_enabled =
        # True` behind an env flag) would defeat the derivation without failing
        # the literal check above — lock single-assignment via the AST.
        tree = ast.parse(self._SOURCE)
        assignments = 0
        for node in ast.walk(tree):
            targets: list = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == "_backtest_orders_enabled":
                    assignments += 1
        assert assignments == 1, (
            f"_backtest_orders_enabled assigned {assignments} times; the master "
            "gate must be set exactly once (in initialize, from live_mode)"
        )

    def _calls_by_function(self) -> dict[str, set[str]]:
        tree = ast.parse(self._SOURCE)
        out: dict[str, set[str]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            attrs: set[str] = set()
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and isinstance(sub.func.value, ast.Name)
                    and sub.func.value.id == "self"
                ):
                    attrs.add(sub.func.attr)
            out[node.name] = attrs
        return out

    def test_market_order_sites_confined_to_gated_methods(self) -> None:
        # Both casings: pythonnet exposes snake_case AND PascalCase forms of
        # every LEAN API, so `self.MarketOrder(...)` would place orders too.
        callers = {
            name
            for name, attrs in self._calls_by_function().items()
            if attrs & {"market_order", "MarketOrder"}
        }
        assert callers == {"_consolidate_pending_rolls", "_place_backtest_orders"}, (
            f"market_order escaped the gated backtest-only methods: {sorted(callers)}"
        )

    def test_no_other_order_apis_exist(self) -> None:
        snake = {
            "order",
            "buy",
            "sell",
            "set_holdings",
            "liquidate",
            "limit_order",
            "stop_market_order",
            "stop_limit_order",
            "limit_if_touched_order",
            "trailing_stop_order",
            "market_on_open_order",
            "market_on_close_order",
            "combo_market_order",
            "combo_limit_order",
            "combo_leg_limit_order",
            "exercise_option",
        }
        pascal = {
            "Order",
            "Buy",
            "Sell",
            "SetHoldings",
            "Liquidate",
            "LimitOrder",
            "StopMarketOrder",
            "StopLimitOrder",
            "LimitIfTouchedOrder",
            "TrailingStopOrder",
            "MarketOnOpenOrder",
            "MarketOnCloseOrder",
            "ComboMarketOrder",
            "ComboLimitOrder",
            "ComboLegLimitOrder",
            "ExerciseOption",
        }
        forbidden = snake | pascal
        for name, attrs in self._calls_by_function().items():
            hit = attrs & forbidden
            assert not hit, f"forbidden order API {sorted(hit)} called in {name}"


# ---------------------------------------------------------------------------
# BACKTEST MODE: orders ARE placed, sized by the REAL Stage 0-5 pipeline.
# ---------------------------------------------------------------------------


class TestBacktestPlacesOrders:
    def test_stage_2_cap_binds_to_exact_count(self) -> None:
        # Single low-vol ETF at $100, $100k equity. Inverse-vol leverage drives
        # the unconstrained notional way past the 25% cap, so Stage 2 binds at
        # 0.25 x 100000 = $25,000 → 25000 / (1 x 100) = EXACTLY 250 contracts.
        # 250 is pure Stage-2-cap arithmetic — a naive/Stage-1-only path could
        # not produce it, so this proves the real pipeline is in the loop.
        algo = _bare_algo(backtest=True, with_sizer=True)
        algo._market_subscriptions = {"TLT": "TLT_SYM"}
        algo.securities = {"TLT_SYM": _Security(price=Decimal("100"), multiplier=1)}
        entry = _Signal(market="TLT", direction=Direction.LONG, decision_price=Decimal("100"))

        algo._place_backtest_orders(
            active_universe={"TLT": _Series(_LOWVOL)},
            entry_signals=(entry,),
            exit_result=None,
            **_PLACE_KWARGS,
        )
        assert algo._orders == [("TLT_SYM", 250, "v1_backtest:TLT")]
        # Fresh entry starts the MIN_HOLDING clock.
        assert algo._backtest_entry_dates == {"TLT": date(2024, 1, 15)}

    def test_short_entry_signs_negative(self) -> None:
        algo = _bare_algo(backtest=True, with_sizer=True)
        algo._market_subscriptions = {"TLT": "TLT_SYM"}
        algo.securities = {"TLT_SYM": _Security(price=Decimal("100"), multiplier=1)}
        entry = _Signal(market="TLT", direction=Direction.SHORT, decision_price=Decimal("100"))

        algo._place_backtest_orders(
            active_universe={"TLT": _Series(_LOWVOL)},
            entry_signals=(entry,),
            exit_result=None,
            **_PLACE_KWARGS,
        )
        assert algo._orders == [("TLT_SYM", -250, "v1_backtest:TLT")]  # short cap

    def test_futures_entry_trades_mapped_front(self) -> None:
        # /MES: 1-contract notional = 5 x 5000 = $25,000 = the 25% cap, so the
        # cap binds to exactly 1 contract — and it routes to the MAPPED front.
        algo = _bare_algo(backtest=True, with_sizer=True)
        front = _mes_setup(algo)
        entry = _Signal(market="/MES", direction=Direction.LONG, decision_price=Decimal("5000"))

        algo._place_backtest_orders(
            active_universe={"/MES": _Series(_LOWVOL)},
            entry_signals=(entry,),
            exit_result=None,
            **_PLACE_KWARGS,
        )
        assert algo._orders == [(front, 1, "v1_backtest:/MES")]
        # PR B: the front is explicitly subscribed each cycle (the cure for the
        # lazily-fed per-expiry security).
        assert algo._subscribed == [front]

    def test_two_market_batch_sizes_both(self) -> None:
        # Two rates ETFs entering long together: each Stage-2-capped at $25k →
        # 250 each. Proves the batch is co-sized through one pipeline call.
        algo = _bare_algo(backtest=True, with_sizer=True)
        algo._market_subscriptions = {"TLT": "TLT_SYM", "IEF": "IEF_SYM"}
        algo.securities = {
            "TLT_SYM": _Security(price=Decimal("100"), multiplier=1),
            "IEF_SYM": _Security(price=Decimal("100"), multiplier=1),
        }
        entries = (
            _Signal(market="TLT", direction=Direction.LONG, decision_price=Decimal("100")),
            _Signal(market="IEF", direction=Direction.LONG, decision_price=Decimal("100")),
        )

        algo._place_backtest_orders(
            active_universe={"TLT": _Series(_LOWVOL), "IEF": _Series(_LOWVOL_2)},
            entry_signals=entries,
            exit_result=None,
            **_PLACE_KWARGS,
        )
        by_symbol = {sym: (qty, tag) for sym, qty, tag in algo._orders}
        assert by_symbol == {
            "TLT_SYM": (250, "v1_backtest:TLT"),
            "IEF_SYM": (250, "v1_backtest:IEF"),
        }

    def test_held_with_no_signal_holds(self) -> None:
        algo = _bare_algo(backtest=True, with_sizer=True)
        _mes_setup(algo, legs=[("MESM6", 2, "4900")])  # long 2 in the front month

        algo._place_backtest_orders(
            active_universe={"/MES": _Series(_LOWVOL)},
            entry_signals=(),
            exit_result=_ExitResult([]),
            **_PLACE_KWARGS,
        )
        assert algo._orders == []  # delta 0 → no churn, no pyramiding

    def test_held_with_entry_signal_holds(self) -> None:
        # The order-layer anti-pyramiding backstop: an ENTRY signal against an
        # already-held market must not grow the book (signals can re-emit while
        # an order is pending or after a roll).
        algo = _bare_algo(backtest=True, with_sizer=True)
        _mes_setup(algo, legs=[("MESM6", 2, "4900")])
        entry = _Signal(market="/MES", direction=Direction.LONG, decision_price=Decimal("5000"))

        algo._place_backtest_orders(
            active_universe={"/MES": _Series(_LOWVOL)},
            entry_signals=(entry,),
            exit_result=None,
            **_PLACE_KWARGS,
        )
        assert algo._orders == []  # held + entry → hold unchanged

    def test_post_roll_old_month_position_suppresses_reentry(self) -> None:
        # THE re-emit bug (PR B): after a roll the holding sits in the OLD
        # month. The market-level quantity must still read it as held, so a
        # re-emitted breakout entry places NOTHING (the per-expiry read placed
        # one order per day — /MGC Sept-2024).
        algo = _bare_algo(backtest=True, with_sizer=True)
        _mes_setup(algo, legs=[("MESH6", 2, "4800")])  # OLD month, not the front
        entry = _Signal(market="/MES", direction=Direction.LONG, decision_price=Decimal("5000"))

        algo._place_backtest_orders(
            active_universe={"/MES": _Series(_LOWVOL)},
            entry_signals=(entry,),
            exit_result=None,
            **_PLACE_KWARGS,
        )
        assert algo._orders == []

    def test_trend_flip_exit_closes_position(self) -> None:
        algo = _bare_algo(backtest=True, with_sizer=True)
        front = _mes_setup(algo, legs=[("MESM6", 2, "4900")])
        exit_sig = _Signal(
            market="/MES",
            direction=Direction.FLAT,
            decision_price=Decimal("5000"),
            exit_reason="trend_flip",
        )

        algo._place_backtest_orders(
            active_universe={"/MES": _Series(_LOWVOL)},
            entry_signals=(),
            exit_result=_ExitResult([exit_sig]),
            **_PLACE_KWARGS,
        )
        assert algo._orders == [(front, -2, "v1_backtest:/MES")]  # close to flat
        assert "/MES" not in algo._backtest_entry_dates  # clock cleared

    def test_exit_closes_old_month_leg_not_front(self) -> None:
        # Exit with the position still in the OLD month (deferred/missed roll):
        # the close must target THE HELD LEG — a front-only delta would SHORT
        # the front against the old leg (a synthetic calendar spread, not flat).
        algo = _bare_algo(backtest=True, with_sizer=True)
        old_leg = _FakeSymbol("MESH6", canonical="MES_C")
        _mes_setup(algo)
        algo.portfolio[old_leg] = _Holding(2, "4800")
        exit_sig = _Signal(
            market="/MES",
            direction=Direction.FLAT,
            decision_price=Decimal("5000"),
            exit_reason="trend_flip",
        )

        algo._place_backtest_orders(
            active_universe={"/MES": _Series(_LOWVOL)},
            entry_signals=(),
            exit_result=_ExitResult([exit_sig]),
            **_PLACE_KWARGS,
        )
        assert algo._orders == [(old_leg, -2, "v1_backtest:/MES")]

    def test_reversal_flips_to_opposite_side(self) -> None:
        algo = _bare_algo(backtest=True, with_sizer=True)
        front = _mes_setup(algo, legs=[("MESM6", 2, "4900")])
        paired_entry = _Signal(
            market="/MES", direction=Direction.SHORT, decision_price=Decimal("5000")
        )
        exit_sig = _Signal(
            market="/MES",
            direction=Direction.FLAT,
            decision_price=Decimal("5000"),
            exit_reason="reversal",
        )

        algo._place_backtest_orders(
            active_universe={"/MES": _Series(_LOWVOL)},
            entry_signals=(paired_entry,),
            exit_result=_ExitResult([exit_sig]),
            **_PLACE_KWARGS,
        )
        assert len(algo._orders) == 1
        symbol, qty, tag = algo._orders[0]
        assert symbol == front
        assert tag == "v1_backtest:/MES"
        # target is short (the /MES cap → -1); current is +2 → delta = -3.
        assert qty == -3
        # The position changed sides — the MIN_HOLDING clock restarts.
        assert algo._backtest_entry_dates["/MES"] == date(2024, 1, 15)

    def test_reversal_without_paired_entry_closes(self) -> None:
        algo = _bare_algo(backtest=True, with_sizer=True)
        front = _mes_setup(algo, legs=[("MESM6", 2, "4900")])
        exit_sig = _Signal(
            market="/MES",
            direction=Direction.FLAT,
            decision_price=Decimal("5000"),
            exit_reason="reversal",
        )
        algo._place_backtest_orders(
            active_universe={"/MES": _Series(_LOWVOL)},
            entry_signals=(),  # no paired entry → defensive close
            exit_result=_ExitResult([exit_sig]),
            **_PLACE_KWARGS,
        )
        assert algo._orders == [(front, -2, "v1_backtest:/MES")]  # close to flat

    def test_zero_price_subscribes_then_skips_order(self) -> None:
        algo = _bare_algo(backtest=True, with_sizer=True)
        front = _mes_setup(algo, front_price="0")  # dead front (the artifact)
        entry = _Signal(market="/MES", direction=Direction.LONG, decision_price=Decimal("5000"))

        algo._place_backtest_orders(
            active_universe={"/MES": _Series(_LOWVOL)},
            entry_signals=(entry,),
            exit_result=None,
            **_PLACE_KWARGS,
        )
        assert algo._orders == []  # never order at a zero / unmapped price
        # PR B: ...but the front IS subscribed, so its feed starts next bar and
        # the skip self-heals in one cycle instead of lasting months.
        assert algo._subscribed == [front]

    def test_unmapped_front_skips_order(self) -> None:
        algo = _bare_algo(backtest=True, with_sizer=True)
        algo._market_subscriptions = {"/MES": "MES_C"}
        # Continuous present but not yet mapped to a front contract.
        algo.securities = {"MES_C": _Security(price=Decimal("5000"), multiplier=5, mapped=None)}
        entry = _Signal(market="/MES", direction=Direction.LONG, decision_price=Decimal("5000"))

        algo._place_backtest_orders(
            active_universe={"/MES": _Series(_LOWVOL)},
            entry_signals=(entry,),
            exit_result=None,
            **_PLACE_KWARGS,
        )
        assert algo._orders == []  # reconciles next cycle once the front maps


class TestPendingOrderAwareness:
    """Calendar-day cycles must not stack duplicates of an unfilled delta."""

    def test_open_order_on_market_skips_reconcile(self) -> None:
        algo = _bare_algo(backtest=True, with_sizer=True)
        front = _mes_setup(algo)
        algo.transactions = _FakeTransactions(open_orders=[types.SimpleNamespace(symbol=front)])
        entry = _Signal(market="/MES", direction=Direction.LONG, decision_price=Decimal("5000"))

        algo._place_backtest_orders(
            active_universe={"/MES": _Series(_LOWVOL)},
            entry_signals=(entry,),
            exit_result=None,
            **_PLACE_KWARGS,
        )
        assert algo._orders == []  # Friday's order is in flight; Sat/Sun add nothing

    def test_open_order_on_sibling_month_also_skips(self) -> None:
        # An open order on ANY leg of the market (e.g. a consolidation pair)
        # blocks reconciliation — matched via the canonical, not the exact front.
        algo = _bare_algo(backtest=True, with_sizer=True)
        _mes_setup(algo)
        sibling = _FakeSymbol("MESH6", canonical="MES_C")
        algo.transactions = _FakeTransactions(open_orders=[types.SimpleNamespace(symbol=sibling)])
        entry = _Signal(market="/MES", direction=Direction.LONG, decision_price=Decimal("5000"))

        algo._place_backtest_orders(
            active_universe={"/MES": _Series(_LOWVOL)},
            entry_signals=(entry,),
            exit_result=None,
            **_PLACE_KWARGS,
        )
        assert algo._orders == []

    def test_unrelated_open_order_does_not_block(self) -> None:
        algo = _bare_algo(backtest=True, with_sizer=True)
        front = _mes_setup(algo)
        algo.transactions = _FakeTransactions(open_orders=[types.SimpleNamespace(symbol="TLT_SYM")])
        entry = _Signal(market="/MES", direction=Direction.LONG, decision_price=Decimal("5000"))

        algo._place_backtest_orders(
            active_universe={"/MES": _Series(_LOWVOL)},
            entry_signals=(entry,),
            exit_result=None,
            **_PLACE_KWARGS,
        )
        assert algo._orders == [(front, 1, "v1_backtest:/MES")]

    def test_missing_transactions_api_fails_open(self) -> None:
        # No transactions surface (and: a read failure) must NOT freeze trading
        # — reconcile proceeds as before.
        algo = _bare_algo(backtest=True, with_sizer=True)
        front = _mes_setup(algo)
        entry = _Signal(market="/MES", direction=Direction.LONG, decision_price=Decimal("5000"))

        algo._place_backtest_orders(
            active_universe={"/MES": _Series(_LOWVOL)},
            entry_signals=(entry,),
            exit_result=None,
            **_PLACE_KWARGS,
        )
        assert algo._orders == [(front, 1, "v1_backtest:/MES")]


class TestRollRecordAndConsolidation:
    """PR B roll semantics: record at the event, consolidate at the next cycle."""

    def _events(self, *, old="MES_OLD", new="MES_NEW", canonical="MES_C"):
        return {"k": types.SimpleNamespace(old_symbol=old, new_symbol=new, symbol=canonical)}

    def test_roll_records_pending_and_places_no_orders(self) -> None:
        algo = _bare_algo(backtest=True)
        algo._market_subscriptions = {"/MES": "MES_C"}
        algo.portfolio["MES_OLD"] = _Holding(3)

        algo.on_symbol_changed_events(self._events())
        assert algo._orders == []  # ordering at the event moment is impossible
        assert algo._backtest_pending_rolls == {"/MES": ("MES_OLD", "MES_NEW")}

    def test_roll_with_no_position_records_nothing(self) -> None:
        algo = _bare_algo(backtest=True)
        algo._market_subscriptions = {"/MES": "MES_C"}
        algo.portfolio["MES_OLD"] = _Holding(0)

        algo.on_symbol_changed_events(self._events())
        assert algo._orders == []
        assert algo._backtest_pending_rolls == {}

    def test_roll_with_unresolvable_canonical_records_nothing(self) -> None:
        algo = _bare_algo(backtest=True)
        algo._market_subscriptions = {"/MES": "MES_C"}
        algo.portfolio["MES_OLD"] = _Holding(3)

        algo.on_symbol_changed_events(self._events(canonical="UNKNOWN_C"))
        assert algo._orders == []
        assert algo._backtest_pending_rolls == {}  # loud log; legs still summed

    def test_consolidation_carries_when_new_front_priced(self) -> None:
        algo = _bare_algo(backtest=True)
        algo._backtest_pending_rolls = {"/MES": ("MES_OLD", "MES_NEW")}
        algo._backtest_entry_dates = {"/MES": date(2024, 1, 2)}
        algo.portfolio["MES_OLD"] = _Holding(3)
        algo.securities = {"MES_NEW": _Security(price=Decimal("5100"))}

        algo._consolidate_pending_rolls(date(2024, 3, 18))
        assert algo._orders == [("MES_OLD", -3, "roll"), ("MES_NEW", 3, "roll")]
        assert algo._backtest_pending_rolls == {}
        # The carry is the SAME economic position — the MIN_HOLDING clock must
        # NOT restart (roll-nit (a) made real, then fixed properly).
        assert algo._backtest_entry_dates == {"/MES": date(2024, 1, 2)}

    def test_consolidation_defers_while_new_front_unpriced(self) -> None:
        algo = _bare_algo(backtest=True)
        algo._backtest_pending_rolls = {"/MES": ("MES_OLD", "MES_NEW")}
        algo.portfolio["MES_OLD"] = _Holding(3)
        # MES_NEW not in securities → the fake add_future_contract returns a
        # zero-priced security (a just-created subscription: first bar pending).

        algo._consolidate_pending_rolls(date(2024, 3, 18))
        assert algo._orders == []  # no zero-priced Invalid orders
        assert algo._backtest_pending_rolls == {"/MES": ("MES_OLD", "MES_NEW")}
        assert algo._subscribed == ["MES_NEW"]  # feed starts; retry next cycle

    def test_consolidation_drops_record_when_flat(self) -> None:
        algo = _bare_algo(backtest=True)
        algo._backtest_pending_rolls = {"/MES": ("MES_OLD", "MES_NEW")}
        # No holding under MES_OLD: the position was exited since the record.

        algo._consolidate_pending_rolls(date(2024, 3, 18))
        assert algo._orders == []
        assert algo._backtest_pending_rolls == {}

    def test_reconcile_skips_market_with_pending_roll(self) -> None:
        # While a consolidation is deferred, the daily reconcile must not fight
        # it (e.g. an exit would close legs the carry is about to move). The
        # FRONT stays priced here so /MES resolves in Step 1 and the skip
        # branch itself is exercised — the deferral comes from the pending
        # record's NEW month (MESU6) being unpriced (absent from securities →
        # the fake add_future_contract returns a zero-priced subscription).
        algo = _bare_algo(backtest=True, with_sizer=True)
        _mes_setup(algo, legs=[("MESH6", 2, "4800")])  # old month held
        algo._backtest_pending_rolls = {"/MES": ("MESH6", "MESU6")}
        exit_sig = _Signal(
            market="/MES",
            direction=Direction.FLAT,
            decision_price=Decimal("5000"),
            exit_reason="trend_flip",
        )

        algo._place_backtest_orders(
            active_universe={"/MES": _Series(_LOWVOL)},
            entry_signals=(),
            exit_result=_ExitResult([exit_sig]),
            **_PLACE_KWARGS,
        )
        # The deferred carry subscribed the new month, placed nothing, and the
        # reconcile skipped the market (roll_pending_skip) — the exit
        # re-derives next cycle from the unchanged position.
        assert "MESU6" in algo._subscribed
        assert algo._orders == []
        assert algo._backtest_pending_rolls == {"/MES": ("MESH6", "MESU6")}


class TestBacktestPositionSnapshot:
    def test_sums_legs_across_contract_months(self) -> None:
        algo = _bare_algo(backtest=True)
        algo._backtest_entry_dates = {"/MES": date(2024, 1, 2)}
        algo.portfolio[_FakeSymbol("MESH6", canonical="MES_C")] = _Holding(2, "4800")
        algo.portfolio[_FakeSymbol("MESM6", canonical="MES_C")] = _Holding(1, "5100")
        algo.portfolio[_FakeSymbol("MGCJ6", canonical="MGC_C")] = _Holding(7, "2600")  # other mkt

        position = algo._snapshot_backtest_position(market_key="/MES", continuous_symbol="MES_C")
        assert position.quantity == 3
        assert position.direction is Direction.LONG
        # abs-quantity-weighted: (4800*2 + 5100*1) / 3
        assert position.avg_cost == Decimal("4900")
        assert position.opened_at_session_date == date(2024, 1, 2)

    def test_flat_market_snapshot(self) -> None:
        algo = _bare_algo(backtest=True)
        position = algo._snapshot_backtest_position(market_key="/MES", continuous_symbol="MES_C")
        assert position.direction is Direction.FLAT
        assert position.quantity == 0
        assert position.opened_at_session_date is None

    def test_short_market_snapshot(self) -> None:
        algo = _bare_algo(backtest=True)
        algo.portfolio[_FakeSymbol("MESM6", canonical="MES_C")] = _Holding(-2, "5100")
        position = algo._snapshot_backtest_position(market_key="/MES", continuous_symbol="MES_C")
        assert position.quantity == -2
        assert position.direction is Direction.SHORT

    def test_etf_snapshot_uses_tracked_entry_date(self) -> None:
        algo = _bare_algo(backtest=True)
        algo._backtest_entry_dates = {"TLT": date(2024, 2, 5)}
        algo.portfolio["TLT_SYM"] = _Holding(250, "91.5")
        position = algo._snapshot_backtest_position(market_key="TLT", continuous_symbol="TLT_SYM")
        assert position.quantity == 250
        assert position.direction is Direction.LONG
        # LEAN holdings carry no invested_since in this build — the tracked
        # entry date restores the MIN_HOLDING gate.
        assert position.opened_at_session_date == date(2024, 2, 5)
