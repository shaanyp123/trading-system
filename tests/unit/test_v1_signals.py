"""End-to-end coverage for the ``SignalGenerationResult.market_evaluations``
field added in PR-A of ``Docs/signal-proximity-design.md``.

Complements ``test_v1_signal_proximity.py`` (which exercises the proximity
module in isolation) and ``test_strategy_v1.py`` (which locks the entry-
signal gate logic). The contract this file pins down:

* Every market in ``active_universe`` produces exactly one
  ``MarketProximity`` entry in ``result.market_evaluations`` — none dropped
  silently when the market is rejected or warming up.
* Decommissioned markets still surface populated proximity values per Q4
  of the design doc.
* The gate-evaluation logic is BIT-IDENTICAL after the snapshot-exposure
  refactor — the same rejections fire with the same reasons.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from strategies.v1_trend_following import (
    Bar,
    BarSeries,
    GateState,
    MarketProximity,
    RejectionReason,
    SignalGenerationResult,
    V1Parameters,
    V1TrendFollowing,
    default_v1_parameters,
)


def _make_bar(d: date, *, close: Decimal, ohl_offset: Decimal = Decimal("1.00")) -> Bar:
    return Bar(
        session_date=d,
        open=close - ohl_offset / 2,
        high=close + ohl_offset,
        low=close - ohl_offset,
        close=close,
        volume=10_000,
    )


def _trending_series(market: str, *, n_bars: int = 250) -> BarSeries:
    """Smooth uptrending series sufficient for snapshot computation."""
    start = date(2026, 1, 1)
    bars: list[Bar] = []
    for i in range(n_bars):
        drift = Decimal("100") * (Decimal("1.002") ** i)
        wave = Decimal(str(0.5 + 0.5 * ((i % 7) / 6)))
        close = (drift + wave).quantize(Decimal("0.01"))
        bars.append(_make_bar(start + timedelta(days=i), close=close))
    return BarSeries(market=market, bars=tuple(bars))


def _short_series(market: str, *, n_bars: int = 50) -> BarSeries:
    """Series with too few bars to satisfy ``min_required_bars`` (=200 default)."""
    start = date(2026, 1, 1)
    bars = [_make_bar(start + timedelta(days=i), close=Decimal("100")) for i in range(n_bars)]
    return BarSeries(market=market, bars=tuple(bars))


def _decommissioned_params() -> V1Parameters:
    """V1Parameters with the operator kill switch flipped on."""
    return dataclasses.replace(default_v1_parameters(), strategy_decommissioned=True)


# ---------------------------------------------------------------------------
# market_evaluations is populated for every market in the universe
# ---------------------------------------------------------------------------


def test_market_evaluations_populated_for_every_active_universe_market() -> None:
    strategy = V1TrendFollowing(default_v1_parameters())
    universe = {
        "/MES": _trending_series("/MES"),
        "/MNQ": _trending_series("/MNQ"),
        "TLT": _short_series("TLT"),
    }
    result = strategy.generate_signals(
        active_universe=universe,
        current_positions={},
        as_of_session_date=universe["/MES"].bars[-1].session_date,
        as_of_emitted_at_utc=datetime(2026, 5, 28, 21, 30, tzinfo=UTC),
    )
    assert isinstance(result, SignalGenerationResult)
    eval_markets = {ev.market for ev in result.market_evaluations}
    assert eval_markets == {"/MES", "/MNQ", "TLT"}
    for ev in result.market_evaluations:
        assert isinstance(ev, MarketProximity)


def test_warming_up_market_emits_proximity_with_history_sentinel() -> None:
    """Markets without enough bars STILL appear in ``market_evaluations``."""
    strategy = V1TrendFollowing(default_v1_parameters())
    universe = {"TLT": _short_series("TLT")}
    result = strategy.generate_signals(
        active_universe=universe,
        current_positions={},
        as_of_session_date=universe["TLT"].bars[-1].session_date,
    )
    # Rejection captured AND proximity emitted (not dropped).
    assert result.rejections == (("TLT", RejectionReason.INSUFFICIENT_BAR_HISTORY),)
    assert len(result.market_evaluations) == 1
    tlt = result.market_evaluations[0]
    assert tlt.market == "TLT"
    assert tlt.gate_status == "warming_up"
    assert tlt.last_close is None
    assert tlt.overall_state == GateState.FAIL
    assert tlt.closest_gate == "history"


# ---------------------------------------------------------------------------
# Q4: decommissioned markets still surface populated proximity values
# ---------------------------------------------------------------------------


def test_decommissioned_market_emits_proximity_with_values() -> None:
    strategy = V1TrendFollowing(_decommissioned_params())
    universe = {"/MES": _trending_series("/MES")}
    result = strategy.generate_signals(
        active_universe=universe,
        current_positions={},
        as_of_session_date=universe["/MES"].bars[-1].session_date,
    )
    assert result.signals == ()
    assert result.rejections == (("/MES", RejectionReason.STRATEGY_DECOMMISSIONED),)
    # Per Q4 — the row STILL surfaces what the gates would have said.
    assert len(result.market_evaluations) == 1
    mes = result.market_evaluations[0]
    assert mes.gate_status == "decommissioned"
    assert mes.last_close is not None
    # At least one of the per-gate headrooms is populated — the snapshot
    # was computed for proximity even though the decommission gate
    # short-circuited the entry pipeline.
    headrooms = [
        mes.long_donchian.headroom,
        mes.short_donchian.headroom,
        mes.efficiency.headroom,
    ]
    assert any(h is not None for h in headrooms)


def test_decommissioned_market_with_short_history_emits_warming_up_sentinel() -> None:
    """Decommissioned + warming up → gate_status='decommissioned' wins (per design)."""
    strategy = V1TrendFollowing(_decommissioned_params())
    universe = {"TLT": _short_series("TLT")}
    result = strategy.generate_signals(
        active_universe=universe,
        current_positions={},
        as_of_session_date=universe["TLT"].bars[-1].session_date,
    )
    assert result.rejections == (("TLT", RejectionReason.STRATEGY_DECOMMISSIONED),)
    assert len(result.market_evaluations) == 1
    tlt = result.market_evaluations[0]
    # Decommissioned takes precedence over warming_up in the gate_status sentinel.
    assert tlt.gate_status == "decommissioned"
    assert tlt.overall_state == GateState.FAIL
    assert tlt.closest_gate == "history"


# ---------------------------------------------------------------------------
# Bit-identical gate logic — the snapshot-exposure refactor MUST NOT
# change which rejections fire or under which conditions.
# ---------------------------------------------------------------------------


def test_gate_logic_unchanged_short_series_rejects_insufficient_history() -> None:
    """Reproduces the legacy ``test_insufficient_history_rejected`` shape.

    Re-run here to confirm the (outcome, snapshot) refactor is a pure
    return-type change — the same rejection reason fires for the same
    input. This is the key "bit-identical" invariant called out in the
    PR-A constraints.
    """
    strategy = V1TrendFollowing(default_v1_parameters())
    short_series = _short_series("/MES", n_bars=50)
    result = strategy.generate_signals(
        active_universe={"/MES": short_series},
        current_positions={},
        as_of_session_date=short_series.bars[-1].session_date,
    )
    assert result.signals == ()
    assert result.rejections == (("/MES", RejectionReason.INSUFFICIENT_BAR_HISTORY),)
    # Proximity row carried as well — pre-PR-A code didn't emit this; PR-A
    # adds it without disturbing the rejection.
    assert len(result.market_evaluations) == 1
    assert result.market_evaluations[0].gate_status == "warming_up"


def test_market_evaluations_default_is_empty_tuple_for_backwards_compat() -> None:
    """Direct dataclass construction without ``market_evaluations`` still works.

    Consumers that construct ``SignalGenerationResult`` manually (e.g., in
    legacy unit tests or downstream services) and don't set the new field
    pick up the default empty tuple — no TypeError, no surprise None.
    """
    result = SignalGenerationResult(
        signals=(),
        rejections=(),
        as_of_emitted_at_utc=datetime(2026, 5, 28, 21, 30, tzinfo=UTC),
    )
    assert result.market_evaluations == ()
