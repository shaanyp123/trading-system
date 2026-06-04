"""Contract-spec reference data + universe-drift guard (design §5.3)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from research.data.contract_specs import SPECS, get_spec, is_future
from strategies.v1_trend_following.parameters import (
    V1_CANDIDATE_UNIVERSE,
    V1_SIDELINED_MARKETS,
)


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
