"""Sizing schemes under the hard leverage cap (design §6.1)."""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from research.data.bars import BarSeries
from research.risk.sizing_schemes import (
    KNOWN_SCHEMES,
    SizingContext,
    SizingScheme,
    rolling_annualized_vol,
    simulate_sized_path,
    size_for_bar,
    wilders_atr,
)


def _ctx(**kw: float) -> SizingContext:
    base = dict(equity=100_000.0, price=100.0, direction=1, multiplier=50.0, leverage_cap=3.0)
    base.update(kw)
    return SizingContext(**base)  # type: ignore[arg-type]


def test_fixed_scheme_returns_signed_contracts() -> None:
    scheme = SizingScheme("fixed", {"contracts": 2})
    assert size_for_bar(scheme, _ctx(direction=1)) == 2
    assert size_for_bar(scheme, _ctx(direction=-1)) == -2
    assert size_for_bar(scheme, _ctx(direction=0)) == 0


def test_fixed_fractional_sizes_to_notional() -> None:
    # fraction 1.0, $100k equity, 1 contract = 50*100 = $5k ⇒ 20 contracts (1x lev < 3x cap).
    scheme = SizingScheme("fixed_fractional", {"fraction": 1.0})
    assert size_for_bar(scheme, _ctx()) == 20


def test_hard_cap_clamps_an_over_leveraged_request() -> None:
    # fraction 10.0 wants 200 contracts (10x); the 3x cap clamps to 60.
    scheme = SizingScheme("fixed_fractional", {"fraction": 10.0})
    assert size_for_bar(scheme, _ctx(leverage_cap=3.0)) == 60


def test_vol_target_warmup_nan_sizes_zero() -> None:
    scheme = SizingScheme("vol_target", {"vol_target_pct_annual": 0.15})
    assert size_for_bar(scheme, _ctx(sigma_annual=float("nan"))) == 0


def test_vol_target_sizes_by_sigma() -> None:
    # notional = 100k * 0.15 / 0.10 = 150k ⇒ /5k = 30 contracts (3x = exactly the cap).
    scheme = SizingScheme("vol_target", {"vol_target_pct_annual": 0.15})
    assert size_for_bar(scheme, _ctx(sigma_annual=0.10)) == 30


def test_atr_scheme_sizes_by_risk_budget() -> None:
    # risk 1% of 100k = $1000; stop 3*ATR(2.0)=6.0 price; per-contract risk = 6*50=300 ⇒ 3 contracts.
    scheme = SizingScheme("atr", {"risk_pct": 0.01, "stop_atr_mult": 3.0})
    assert size_for_bar(scheme, _ctx(atr=2.0)) == 3


def test_risk_parity_single_instrument_equals_vol_target() -> None:
    rp = SizingScheme("risk_parity", {"vol_target_pct_annual": 0.15})
    vt = SizingScheme("vol_target", {"vol_target_pct_annual": 0.15})
    ctx = _ctx(sigma_annual=0.10)
    assert size_for_bar(rp, ctx) == size_for_bar(vt, ctx)


def test_unknown_scheme_rejected() -> None:
    with pytest.raises(ValueError):
        SizingScheme("nonsense", {})  # type: ignore[arg-type]
    assert "fixed" in KNOWN_SCHEMES and "vol_target" in KNOWN_SCHEMES


def test_rolling_vol_warmup_is_nan_then_finite() -> None:
    close = np.linspace(100.0, 110.0, 20)
    vol = rolling_annualized_vol(close, lookback=5)
    assert np.isnan(vol[:5]).all()
    assert np.isfinite(vol[5:]).all()


def test_wilders_atr_warmup_then_positive() -> None:
    n = 30
    high = np.full(n, 101.0)
    low = np.full(n, 99.0)
    close = np.full(n, 100.0)
    atr = wilders_atr(high, low, close, lookback=14)
    assert np.isnan(atr[:14]).all()
    assert atr[14] == pytest.approx(2.0)  # constant 2-wide bars ⇒ ATR 2.0


def _ramp_series(n: int, start: float, step: float) -> BarSeries:
    close = np.array([start + step * i for i in range(n)], dtype=np.float64)
    dates = tuple(
        date(2024, 1, 1) + (date(2024, 12, 31) - date(2024, 1, 1)) * i // (n - 1) for i in range(n)
    )
    return BarSeries(
        symbol="X",
        dates=dates,
        open=close.copy(),
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=np.ones(n),
    )


def test_simulate_sized_path_produces_capped_leverage() -> None:
    series = _ramp_series(80, 100.0, 0.1)
    directions = np.ones(len(series), dtype=np.int64)
    scheme = SizingScheme("fixed_fractional", {"fraction": 10.0})  # wants 10x; cap binds
    sized = simulate_sized_path(
        series, directions, scheme, multiplier=1.0, starting_cash=100_000.0, leverage_cap=2.0
    )
    assert sized.result.positions[0] == 0  # bar 0 always flat
    # The cap binds near 2x (a profiting long drifts just under it as equity grows),
    # NOT the scheme's requested 10x — the hard cap clamped the position.
    assert sized.leverage.peak == pytest.approx(2.0, abs=0.05)
    assert sized.leverage.peak < 3.0
    assert sized.leverage_cap == 2.0
