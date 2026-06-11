"""Contract-spec reference data + universe-drift guard (design §5.3)."""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest

from research.data.contract_specs import (
    ETF_COMMISSION_MIN_PER_ORDER,
    ETF_COMMISSION_PER_SHARE,
    FILL_CONVENTION,
    SPECS,
    get_spec,
    is_future,
)
from strategies.v1_trend_following.parameters import (
    V1_CANDIDATE_UNIVERSE,
    V1_SIDELINED_MARKETS,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_specs_match_live_candidate_universe_exactly() -> None:
    # The research universe must equal the live V1 candidate universe — no drift.
    assert set(SPECS) == set(V1_CANDIDATE_UNIVERSE)


def test_sidelined_markets_excluded() -> None:
    assert "/MCL" not in SPECS
    assert set(SPECS).isdisjoint(V1_SIDELINED_MARKETS)


@pytest.mark.parametrize(
    ("symbol", "dollars_per_tick"),
    [
        ("/MES", Decimal("1.25")),
        ("/MNQ", Decimal("0.50")),
        ("/MYM", Decimal("0.50")),
        ("/M2K", Decimal("0.50")),
        ("/MGC", Decimal("1.00")),
        ("/MBT", Decimal("0.50")),
        ("TLT", Decimal("0.01")),
    ],
)
def test_dollars_per_tick(symbol: str, dollars_per_tick: Decimal) -> None:
    assert get_spec(symbol).dollars_per_tick == dollars_per_tick


def test_mym_routes_via_cbot() -> None:
    # /MYM lives under cbot, not cme — getting this wrong yields empty history.
    assert get_spec("/MYM").market_dir == "cbot"
    assert get_spec("/MES").market_dir == "cme"
    assert get_spec("/MGC").market_dir == "comex"


def test_notional_is_decimal_exact() -> None:
    # 2 /MES contracts at 5000.00 = 2 * $5 * 5000 = $50,000 exactly.
    assert get_spec("/MES").notional(2, Decimal("5000.00")) == Decimal("50000.00")


def test_is_future_classification() -> None:
    assert is_future("/MES") is True
    assert is_future("TLT") is False


def test_unknown_symbol_raises() -> None:
    with pytest.raises(KeyError):
        get_spec("/NOPE")


# ---------------------------------------------------------------------------
# PR B — explicit cost model (commission + slippage + fill conventions).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("symbol", "per_side"),
    [
        ("/MES", Decimal("0.62")),
        ("/MNQ", Decimal("0.62")),
        ("/MYM", Decimal("0.62")),
        ("/M2K", Decimal("0.62")),
        ("/MGC", Decimal("1.37")),
        ("/MBT", Decimal("3.42")),
    ],
)
def test_futures_commission_per_side(symbol: str, per_side: Decimal) -> None:
    # All-in IBKR fixed + exchange + NFA, per the module docstring (as-of +
    # tolerance stated there). LEAN's bundled model is stale — these are the
    # explicit research numbers.
    spec = get_spec(symbol)
    assert spec.commission_per_side == per_side
    assert spec.commission(3) == per_side * 3
    assert spec.commission(-3) == per_side * 3  # side carried by caller; size abs


def test_every_future_has_commission_and_slippage() -> None:
    for spec in SPECS.values():
        if spec.asset_class == "future":
            assert spec.commission_per_side is not None, spec.symbol
            assert spec.slippage_ticks == 1, spec.symbol
        else:
            assert spec.commission_per_side is None, spec.symbol
            assert spec.slippage_ticks == 0, spec.symbol


def test_etf_commission_per_share_with_minimum() -> None:
    # IBKR fixed: $0.005/share, $1.00 minimum per order.
    tlt = get_spec("TLT")
    assert tlt.commission(314) == Decimal("314") * ETF_COMMISSION_PER_SHARE  # $1.57
    assert tlt.commission(100) == ETF_COMMISSION_MIN_PER_ORDER  # $0.50 → min $1.00


def test_slippage_per_side_is_one_tick_dollar_value() -> None:
    assert get_spec("/MES").slippage_per_side == Decimal("1.25")  # $5 x 0.25 x 1
    assert get_spec("/MGC").slippage_per_side == Decimal("1.00")
    assert get_spec("TLT").slippage_per_side == Decimal("0")  # ETFs: 0 ticks


def test_fill_conventions_documented() -> None:
    assert "same-session close" in FILL_CONVENTION["future"]
    assert "next-session" in FILL_CONVENTION["etf"]


def _module_dict_literal(source: str, name: str) -> dict:
    """The literal value of a module-level assignment ``name = {...}``."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"no module-level literal assignment for {name}")


@pytest.mark.parametrize(
    ("path", "comm_name", "tick_name", "ticks_name"),
    [
        (
            "research/lean/projects/research_runner.py",
            "_FUTURES_COMMISSION_PER_SIDE",
            "_FUTURES_TICK_SIZE",
            "_FUTURES_SLIPPAGE_TICKS",
        ),
        (
            "lean/v1_strategy.py",
            "_BACKTEST_FUTURES_COMMISSION_PER_SIDE",
            "_BACKTEST_FUTURES_TICK_SIZE",
            "_BACKTEST_FUTURES_SLIPPAGE_TICKS",
        ),
    ],
)
def test_lean_side_cost_tables_match_canonical(
    path: str, comm_name: str, tick_name: str, ticks_name: str
) -> None:
    # The LEAN-side algorithm files cannot import research/ inside the
    # container, so each carries an inline copy of the cost table. Pin both
    # copies to THIS module (the canonical) via the AST so they can never
    # drift silently.
    source = (_REPO_ROOT / path).read_text(encoding="utf-8")
    futures = [s for s in SPECS.values() if s.asset_class == "future"]
    expected_comm = {s.symbol.lstrip("/"): float(s.commission_per_side) for s in futures}
    expected_tick = {s.symbol.lstrip("/"): float(s.tick_size) for s in futures}
    assert _module_dict_literal(source, comm_name) == expected_comm
    assert _module_dict_literal(source, tick_name) == expected_tick
    ticks = _module_dict_literal_scalar(source, ticks_name)
    assert {s.slippage_ticks for s in futures} == {ticks}


def _module_dict_literal_scalar(source: str, name: str) -> int:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    value = ast.literal_eval(node.value)
                    assert isinstance(value, int)
                    return value
    raise AssertionError(f"no module-level literal assignment for {name}")
