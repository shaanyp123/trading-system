"""V1 adapter — replays the REAL production V1 logic as a ResearchStrategy.

These tests use SYNTHETIC trends (no snapshot dependency, CI-safe). They assert the
adapter (a) stays flat through warmup, (b) goes long on a clean uptrend and short on
a clean downtrend, and (c) agrees in DIRECTION with a direct call to the real V1
``generate_signals`` — the parity guard that the adapter faithfully reflects
production V1 and never inverts/drops its decisions.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import numpy as np

from research.data.bars import BarSeries
from research.strategy.v1_adapter import V1Adapter
from strategies.v1_trend_following.parameters import default_v1_parameters
from strategies.v1_trend_following.signals import Bar
from strategies.v1_trend_following.signals import BarSeries as V1BarSeries
from strategies.v1_trend_following.strategy import V1TrendFollowing

_N = 260  # > MA_SLOW(200) warmup + room to trade


def _trend_series(direction: int) -> BarSeries:
    """A clean linear trend (ER ≈ 1, MA-aligned, new Donchian extreme each bar).

    Base 300 / step 0.5 keeps prices well above 0 in BOTH directions over ``_N``
    bars (a 0 price is unreachable in real markets but would trip a division in
    V1's exit-proximity).
    """
    base = 300.0
    closes = np.asarray([base + direction * 0.5 * i for i in range(_N)], dtype=np.float64)
    dates = tuple(date(2024, 1, 1) + timedelta(days=i) for i in range(_N))
    return BarSeries(
        symbol="TLT",
        dates=dates,
        open=closes.copy(),
        high=closes + 0.1,
        low=closes - 0.1,
        close=closes,
        volume=np.full(_N, 1000.0, dtype=np.float64),
    )


def _direct_v1_direction(series: BarSeries) -> str | None:
    """Direction the REAL V1 generate_signals returns on the full window (flat book)."""
    v1 = V1TrendFollowing(default_v1_parameters())
    bars = tuple(
        Bar(
            session_date=series.dates[i],
            open=Decimal(str(float(series.open[i]))),
            high=Decimal(str(float(series.high[i]))),
            low=Decimal(str(float(series.low[i]))),
            close=Decimal(str(float(series.close[i]))),
            volume=int(series.volume[i]),
        )
        for i in range(len(series))
    )
    win = V1BarSeries(market=series.symbol, bars=bars)
    res = v1.generate_signals(
        active_universe={series.symbol: win},
        current_positions={},
        as_of_session_date=series.dates[-1],
    )
    sig = next((s for s in res.signals if s.market == series.symbol), None)
    return None if sig is None else str(sig.direction)


def test_name_and_warmup() -> None:
    adapter = V1Adapter()
    assert adapter.name == "v1_adapter"
    assert adapter.warmup_bars == 200  # max(donchian+1, MA_SLOW=200, atr+1, vol_lb)


def test_flat_through_warmup() -> None:
    pos = V1Adapter().target_positions(_trend_series(+1))
    # No position can be taken before the strategy's min_required_bars of history.
    assert int(np.sum(pos[:199] != 0)) == 0


def test_uptrend_goes_long_and_matches_real_v1() -> None:
    series = _trend_series(+1)
    pos = V1Adapter().target_positions(series)
    assert pos[-1] == 1  # holding long at the end of a clean uptrend
    assert _direct_v1_direction(series) == "long"  # parity: real V1 agrees


def test_downtrend_goes_short_and_matches_real_v1() -> None:
    series = _trend_series(-1)
    pos = V1Adapter().target_positions(series)
    assert pos[-1] == -1  # holding short at the end of a clean downtrend
    assert _direct_v1_direction(series) == "short"  # parity: real V1 agrees


def test_only_unit_directions() -> None:
    # The adapter emits DIRECTION only (+1/-1/0); the sizing scheme supplies size.
    pos = V1Adapter().target_positions(_trend_series(+1))
    assert set(pos.tolist()) <= {-1, 0, 1}


def test_trend_then_reverse_exits_and_flips() -> None:
    # Up for >warmup bars (enter long), then down (exit the long via stop/trend-flip/
    # reversal, then enter short). Guards the EXIT state machine, not just entries.
    up = [300.0 + 0.5 * i for i in range(230)]
    down = [up[-1] - 0.5 * (i + 1) for i in range(140)]
    closes = np.asarray(up + down, dtype=np.float64)
    n = len(closes)
    series = BarSeries(
        symbol="TLT",
        dates=tuple(date(2024, 1, 1) + timedelta(days=i) for i in range(n)),
        open=closes.copy(),
        high=closes + 0.1,
        low=closes - 0.1,
        close=closes,
        volume=np.full(n, 1000.0, dtype=np.float64),
    )
    pos = V1Adapter().target_positions(series)
    assert 1 in pos.tolist()  # went long in the uptrend
    assert -1 in pos.tolist()  # exited + went short in the downtrend
    first_long = next(i for i, p in enumerate(pos.tolist()) if p == 1)
    first_short = next(i for i, p in enumerate(pos.tolist()) if p == -1)
    assert first_long < first_short  # long BEFORE short (exited then flipped)


def test_config_registry_builds_v1_adapter() -> None:
    from research.config.schema import KNOWN_STRATEGIES
    from research.run import _build_strategy

    assert "v1_adapter" in KNOWN_STRATEGIES

    class _Cfg:
        strategy_ref = "v1_adapter"
        contracts = 1
        channel = None

    built = _build_strategy(_Cfg())  # type: ignore[arg-type]
    assert isinstance(built, V1Adapter)
