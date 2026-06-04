"""Per-bar leverage series + the hard-cap clamp (design §6.1)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from research.risk.leverage import (
    cap_contracts_to_leverage,
    compute_leverage_series,
    max_contracts_for_leverage,
)


def test_max_contracts_floors_to_cap() -> None:
    # cap 3x, $100k equity, 1 contract = $25k notional ⇒ floor(3*100k/25k) = 12.
    assert max_contracts_for_leverage(100_000.0, 5_000.0, 5.0, 3.0) == 12


def test_max_contracts_zero_on_nonpositive_equity() -> None:
    assert max_contracts_for_leverage(0.0, 5_000.0, 5.0, 3.0) == 0
    assert max_contracts_for_leverage(-10.0, 5_000.0, 5.0, 3.0) == 0


def test_cap_preserves_sign_and_clamps_magnitude() -> None:
    # Wants 100 long but the 3x cap only allows 12.
    assert (
        cap_contracts_to_leverage(100, equity=100_000.0, price=5_000.0, multiplier=5.0, cap=3.0)
        == 12
    )
    # Short side: sign preserved.
    assert (
        cap_contracts_to_leverage(-100, equity=100_000.0, price=5_000.0, multiplier=5.0, cap=3.0)
        == -12
    )
    # Already inside the cap ⇒ unchanged.
    assert (
        cap_contracts_to_leverage(2, equity=100_000.0, price=5_000.0, multiplier=5.0, cap=3.0) == 2
    )


def test_cap_zero_stays_zero() -> None:
    assert (
        cap_contracts_to_leverage(0, equity=100_000.0, price=5_000.0, multiplier=5.0, cap=3.0) == 0
    )


def test_leverage_series_basic() -> None:
    positions = np.array([0, 2, 2], dtype=np.int64)
    prices = np.array([100.0, 100.0, 100.0], dtype=np.float64)
    equity = np.array([100_000.0, 100_000.0, 100_000.0], dtype=np.float64)
    series = compute_leverage_series(positions, prices, equity, multiplier=50.0)
    # bar 1/2: |2| * 50 * 100 / 100_000 = 0.1x; bar 0 flat = 0.
    assert series.leverage[0] == pytest.approx(0.0)
    assert series.leverage[1] == pytest.approx(0.1)
    assert series.peak == pytest.approx(0.1)
    assert series.wiped is False


def test_leverage_series_flags_wipeout_with_nan() -> None:
    positions = np.array([1, 1, 1], dtype=np.int64)
    prices = np.array([100.0, 100.0, 100.0], dtype=np.float64)
    equity = np.array([100_000.0, 10_000.0, -500.0], dtype=np.float64)  # blows up
    series = compute_leverage_series(positions, prices, equity, multiplier=50.0)
    assert series.wiped is True
    assert series.first_wipe_index == 2
    assert math.isnan(series.leverage[2])  # leverage undefined on non-positive equity
    # Realized leverage rises as equity erodes (the ruin drift, design §6.2).
    assert series.leverage[1] > series.leverage[0]


def test_leverage_series_length_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        compute_leverage_series(
            np.array([0, 1], dtype=np.int64),
            np.array([100.0], dtype=np.float64),
            np.array([100.0, 100.0], dtype=np.float64),
            multiplier=1.0,
        )
