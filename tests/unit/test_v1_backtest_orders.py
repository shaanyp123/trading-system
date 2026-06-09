"""Unit tests for the LEAN-side backtest-only order placement (charter PR A).

``lean/v1_strategy.py`` is the LIVE production strategy. PR A adds a
backtest-only path that places REAL LEAN orders mirroring the signals V1 already
computes, so a backtest yields an authoritative equity curve. The orders are
sized by the CANONICAL Stage 0-5 pipeline (``services/risk/sizing.py``) — the
very module the live risk engine uses — loaded by FILE PATH inside the LEAN
container (it can't be a normal import: the package ``__init__`` drags sqlalchemy
+ ``services.audit``, absent in the LEAN image). The hard safety invariant is
that this whole path is **provably unreachable in live mode** — the live strategy
stays POST-only (zero LEAN orders).

The module ``from __future__ import annotations`` + a stubbed ``QCAlgorithm`` lets
us import it outside the LEAN runtime and unit-test the pure input-builder helpers
+ the order-placement gate directly. The real sizer is loaded by path here too
(bypassing the sqlalchemy-dragging package ``__init__``, exactly as production
does) and injected into ``algo._sizing_pipeline_module`` so the tests exercise the
REAL Stage 0-5 math — not a mock.

Coverage:
  * ``_daily_returns_by_date`` / ``_annualized_covariance`` / ``_total_return`` —
    the numeric inputs to the sizer (returns keyed by date, annualized covariance,
    momentum proxy), including the degenerate-window guards.
  * ``_load_sizing_pipeline`` — raises a clear error when ``services/`` is unmounted.
  * ``_place_backtest_orders`` — LIVE mode (gate off) places NO orders; BACKTEST
    mode sizes entries via REAL Stage 0-5 (the 25% Stage-2 cap binds to an exact
    contract count), and reconciles exits / reversals / holds correctly.
  * ``on_symbol_changed_events`` — LIVE mode places NO roll orders; BACKTEST mode
    carries the position (close old + open new), never ``liquidate``.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

# ---- Stub the LEAN runtime symbol the class definition needs, then import. ----
_stub = types.ModuleType("AlgorithmImports")


class _QCAlgorithm:  # minimal base; the methods under test don't call into it.
    ...


_stub.QCAlgorithm = _QCAlgorithm
_stub.__all__ = ["QCAlgorithm"]
sys.modules.setdefault("AlgorithmImports", _stub)

from lean import v1_strategy  # noqa: E402
from strategies.v1_trend_following.signals import Direction  # noqa: E402

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
    def __init__(self, quantity: int) -> None:
        self.quantity = quantity


class _Signal:
    """Stand-in for ``CandidateSignal`` — only the fields the path reads.

    The sizer reads only ``.market`` + ``.direction``; the classify step reads
    ``.exit_reason``; ``.decision_price`` is unused now (notional comes from the
    mapped-front security price) but kept for parity with the real signal shape.
    """

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
    algo.log = lambda *_a, **_k: None
    algo._orders: list = []
    algo.market_order = lambda symbol, qty, tag=None: algo._orders.append((symbol, qty, tag))
    return algo


# A tight, strictly-varying low-vol series → small annualized vol so the Stage-2
# per-position cap (25% x equity) binds to a deterministic contract count, AND a
# strictly-positive variance (never a degenerate covariance window).
_LOWVOL = [100, 100.1, 100.05, 100.12, 100.08, 100.15, 100.1, 100.18, 100.12, 100.2]
_LOWVOL_2 = [100, 100.08, 100.13, 100.06, 100.16, 100.1, 100.17, 100.09, 100.15, 100.19]


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
        algo.portfolio = {}  # flat
        entry = _Signal(market="TLT", direction=Direction.LONG, decision_price=Decimal("90"))

        algo._place_backtest_orders(
            active_universe={"TLT": _Series(_LOWVOL)},
            entry_signals=(entry,),
            exit_result=None,
            equity=Decimal("100000"),
            vol_target_pct_annual=Decimal("0.15"),
            instrument_vol_lookback_days=5,
            session_date=date(2024, 1, 15),
        )
        assert algo._orders == []  # provably unreachable in live mode

    def test_live_never_loads_the_sizer(self) -> None:
        # Extra belt-and-braces: even with NO sizer injected, live mode must not
        # try to load one (the gate returns first). A load attempt would raise.
        algo = _bare_algo(backtest=False)
        algo._market_subscriptions = {"TLT": "TLT_SYM"}
        algo.securities = {"TLT_SYM": _Security(price=Decimal("90"), multiplier=1)}
        algo.portfolio = {}
        entry = _Signal(market="TLT", direction=Direction.LONG, decision_price=Decimal("90"))
        algo._place_backtest_orders(
            active_universe={"TLT": _Series(_LOWVOL)},
            entry_signals=(entry,),
            exit_result=None,
            equity=Decimal("100000"),
            vol_target_pct_annual=Decimal("0.15"),
            instrument_vol_lookback_days=5,
            session_date=date(2024, 1, 15),
        )
        assert algo._sizing_pipeline_module is None  # never loaded in live mode

    def test_on_symbol_changed_events_is_noop_in_live(self) -> None:
        algo = _bare_algo(backtest=False)  # live: gate off
        algo.portfolio = {"OLD": _Holding(3)}
        algo.securities = {"NEW": _Security(price=Decimal("5000"))}
        events = {"k": types.SimpleNamespace(old_symbol="OLD", new_symbol="NEW")}

        algo.on_symbol_changed_events(events)
        assert algo._orders == []  # live roll handling is the api's job, not LEAN's


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
        algo.portfolio = {}  # flat
        entry = _Signal(market="TLT", direction=Direction.LONG, decision_price=Decimal("100"))

        algo._place_backtest_orders(
            active_universe={"TLT": _Series(_LOWVOL)},
            entry_signals=(entry,),
            exit_result=None,
            equity=Decimal("100000"),
            vol_target_pct_annual=Decimal("0.15"),
            instrument_vol_lookback_days=6,
            session_date=date(2024, 1, 15),
        )
        assert algo._orders == [("TLT_SYM", 250, "v1_backtest:TLT")]

    def test_short_entry_signs_negative(self) -> None:
        algo = _bare_algo(backtest=True, with_sizer=True)
        algo._market_subscriptions = {"TLT": "TLT_SYM"}
        algo.securities = {"TLT_SYM": _Security(price=Decimal("100"), multiplier=1)}
        algo.portfolio = {}
        entry = _Signal(market="TLT", direction=Direction.SHORT, decision_price=Decimal("100"))

        algo._place_backtest_orders(
            active_universe={"TLT": _Series(_LOWVOL)},
            entry_signals=(entry,),
            exit_result=None,
            equity=Decimal("100000"),
            vol_target_pct_annual=Decimal("0.15"),
            instrument_vol_lookback_days=6,
            session_date=date(2024, 1, 15),
        )
        assert algo._orders == [("TLT_SYM", -250, "v1_backtest:TLT")]  # short cap

    def test_futures_entry_trades_mapped_front(self) -> None:
        # /MES: 1-contract notional = 5 x 5000 = $25,000 = the 25% cap, so the
        # cap binds to exactly 1 contract — and it routes to the MAPPED front.
        algo = _bare_algo(backtest=True, with_sizer=True)
        algo._market_subscriptions = {"/MES": "MES_C"}
        algo.securities = {
            "MES_C": _Security(price=Decimal("5000"), multiplier=5, mapped="MESM6"),
            "MESM6": _Security(price=Decimal("5000"), multiplier=5),
        }
        algo.portfolio = {}
        entry = _Signal(market="/MES", direction=Direction.LONG, decision_price=Decimal("5000"))

        algo._place_backtest_orders(
            active_universe={"/MES": _Series(_LOWVOL)},
            entry_signals=(entry,),
            exit_result=None,
            equity=Decimal("100000"),
            vol_target_pct_annual=Decimal("0.15"),
            instrument_vol_lookback_days=6,
            session_date=date(2024, 1, 15),
        )
        assert algo._orders == [("MESM6", 1, "v1_backtest:/MES")]

    def test_two_market_batch_sizes_both(self) -> None:
        # Two rates ETFs entering long together: each Stage-2-capped at $25k →
        # 250 each. Proves the batch is co-sized through one pipeline call.
        algo = _bare_algo(backtest=True, with_sizer=True)
        algo._market_subscriptions = {"TLT": "TLT_SYM", "IEF": "IEF_SYM"}
        algo.securities = {
            "TLT_SYM": _Security(price=Decimal("100"), multiplier=1),
            "IEF_SYM": _Security(price=Decimal("100"), multiplier=1),
        }
        algo.portfolio = {}
        entries = (
            _Signal(market="TLT", direction=Direction.LONG, decision_price=Decimal("100")),
            _Signal(market="IEF", direction=Direction.LONG, decision_price=Decimal("100")),
        )

        algo._place_backtest_orders(
            active_universe={"TLT": _Series(_LOWVOL), "IEF": _Series(_LOWVOL_2)},
            entry_signals=entries,
            exit_result=None,
            equity=Decimal("100000"),
            vol_target_pct_annual=Decimal("0.15"),
            instrument_vol_lookback_days=6,
            session_date=date(2024, 1, 15),
        )
        by_symbol = {sym: (qty, tag) for sym, qty, tag in algo._orders}
        assert by_symbol == {
            "TLT_SYM": (250, "v1_backtest:TLT"),
            "IEF_SYM": (250, "v1_backtest:IEF"),
        }

    def test_held_with_no_signal_holds(self) -> None:
        algo = _bare_algo(backtest=True, with_sizer=True)
        algo._market_subscriptions = {"/MES": "MES_C"}
        algo.securities = {
            "MES_C": _Security(price=Decimal("5000"), multiplier=5, mapped="MESM6"),
            "MESM6": _Security(price=Decimal("5000"), multiplier=5),
        }
        algo.portfolio = {"MESM6": _Holding(2)}  # long 2, no signal this cycle

        algo._place_backtest_orders(
            active_universe={"/MES": _Series(_LOWVOL)},
            entry_signals=(),
            exit_result=_ExitResult([]),
            equity=Decimal("100000"),
            vol_target_pct_annual=Decimal("0.15"),
            instrument_vol_lookback_days=6,
            session_date=date(2024, 1, 15),
        )
        assert algo._orders == []  # delta 0 → no churn, no pyramiding

    def test_trend_flip_exit_closes_position(self) -> None:
        algo = _bare_algo(backtest=True, with_sizer=True)
        algo._market_subscriptions = {"/MES": "MES_C"}
        algo.securities = {
            "MES_C": _Security(price=Decimal("5000"), multiplier=5, mapped="MESM6"),
            "MESM6": _Security(price=Decimal("5000"), multiplier=5),
        }
        algo.portfolio = {"MESM6": _Holding(2)}  # long 2
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
            equity=Decimal("100000"),
            vol_target_pct_annual=Decimal("0.15"),
            instrument_vol_lookback_days=6,
            session_date=date(2024, 1, 15),
        )
        assert algo._orders == [("MESM6", -2, "v1_backtest:/MES")]  # close to flat

    def test_reversal_flips_to_opposite_side(self) -> None:
        algo = _bare_algo(backtest=True, with_sizer=True)
        algo._market_subscriptions = {"/MES": "MES_C"}
        algo.securities = {
            "MES_C": _Security(price=Decimal("5000"), multiplier=5, mapped="MESM6"),
            "MESM6": _Security(price=Decimal("5000"), multiplier=5),
        }
        algo.portfolio = {"MESM6": _Holding(2)}  # long 2 → reversing to short
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
            equity=Decimal("100000"),
            vol_target_pct_annual=Decimal("0.15"),
            instrument_vol_lookback_days=6,
            session_date=date(2024, 1, 15),
        )
        assert len(algo._orders) == 1
        symbol, qty, tag = algo._orders[0]
        assert symbol == "MESM6"
        assert tag == "v1_backtest:/MES"
        # target is short (the /MES cap → -1); current is +2 → delta = -3.
        assert qty == -3

    def test_reversal_without_paired_entry_closes(self) -> None:
        algo = _bare_algo(backtest=True, with_sizer=True)
        algo._market_subscriptions = {"/MES": "MES_C"}
        algo.securities = {
            "MES_C": _Security(price=Decimal("5000"), multiplier=5, mapped="MESM6"),
            "MESM6": _Security(price=Decimal("5000"), multiplier=5),
        }
        algo.portfolio = {"MESM6": _Holding(2)}
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
            equity=Decimal("100000"),
            vol_target_pct_annual=Decimal("0.15"),
            instrument_vol_lookback_days=6,
            session_date=date(2024, 1, 15),
        )
        assert algo._orders == [("MESM6", -2, "v1_backtest:/MES")]  # close to flat

    def test_zero_price_skips_order(self) -> None:
        algo = _bare_algo(backtest=True, with_sizer=True)
        algo._market_subscriptions = {"/MES": "MES_C"}
        algo.securities = {
            "MES_C": _Security(price=Decimal("0"), multiplier=5, mapped="MESM6"),
            "MESM6": _Security(price=Decimal("0"), multiplier=5),  # G2: 0 price
        }
        algo.portfolio = {}
        entry = _Signal(market="/MES", direction=Direction.LONG, decision_price=Decimal("5000"))

        algo._place_backtest_orders(
            active_universe={"/MES": _Series(_LOWVOL)},
            entry_signals=(entry,),
            exit_result=None,
            equity=Decimal("100000"),
            vol_target_pct_annual=Decimal("0.15"),
            instrument_vol_lookback_days=6,
            session_date=date(2024, 1, 15),
        )
        assert algo._orders == []  # never order at a zero / unmapped price

    def test_unmapped_front_skips_order(self) -> None:
        algo = _bare_algo(backtest=True, with_sizer=True)
        algo._market_subscriptions = {"/MES": "MES_C"}
        # Continuous present but not yet mapped to a front contract.
        algo.securities = {"MES_C": _Security(price=Decimal("5000"), multiplier=5, mapped=None)}
        algo.portfolio = {}
        entry = _Signal(market="/MES", direction=Direction.LONG, decision_price=Decimal("5000"))

        algo._place_backtest_orders(
            active_universe={"/MES": _Series(_LOWVOL)},
            entry_signals=(entry,),
            exit_result=None,
            equity=Decimal("100000"),
            vol_target_pct_annual=Decimal("0.15"),
            instrument_vol_lookback_days=6,
            session_date=date(2024, 1, 15),
        )
        assert algo._orders == []  # reconciles next cycle once the front maps


class TestBacktestRollCarry:
    def test_roll_closes_old_and_opens_new(self) -> None:
        algo = _bare_algo(backtest=True)
        algo.portfolio = {"OLD": _Holding(3)}
        algo.securities = {"NEW": _Security(price=Decimal("5000"))}
        events = {"k": types.SimpleNamespace(old_symbol="OLD", new_symbol="NEW")}

        algo.on_symbol_changed_events(events)
        assert algo._orders == [("OLD", -3, "roll"), ("NEW", 3, "roll")]

    def test_roll_with_no_position_does_nothing(self) -> None:
        algo = _bare_algo(backtest=True)
        algo.portfolio = {"OLD": _Holding(0)}
        algo.securities = {"NEW": _Security(price=Decimal("5000"))}
        events = {"k": types.SimpleNamespace(old_symbol="OLD", new_symbol="NEW")}

        algo.on_symbol_changed_events(events)
        assert algo._orders == []

    def test_roll_close_only_when_new_price_unavailable(self) -> None:
        algo = _bare_algo(backtest=True)
        algo.portfolio = {"OLD": _Holding(3)}
        algo.securities = {"NEW": _Security(price=Decimal("0"))}  # new front not priced
        events = {"k": types.SimpleNamespace(old_symbol="OLD", new_symbol="NEW")}

        algo.on_symbol_changed_events(events)
        assert algo._orders == [("OLD", -3, "roll")]  # close only; reconcile re-opens
