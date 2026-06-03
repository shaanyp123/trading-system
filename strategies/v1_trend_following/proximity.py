"""Per-gate proximity classification for the V1 trend-following strategy.

Pure-Python module — zero LEAN imports, pytest-friendly from the project
root. Owns the per-gate headroom math + state classification + threshold
constants. Single source of truth (D1 of ``Docs/signal-proximity-design.md``)
for "how close is each market to firing"; the api + frontend consume
pre-computed values from here and never recompute.

Inputs are individual indicator values, NOT the strategy's internal
``_IndicatorSnapshot`` type. Two reasons:

1. Decoupling — ``strategy.py`` already imports from this module
   (``compute_market_proximity`` / ``MarketProximity``); importing the
   snapshot type the other way creates a circular dependency.
2. Pytest-friendliness — unit tests can construct call sites with bare
   ``Decimal`` literals instead of a strategy-internal dataclass.

Thresholds are operator-tunable via the constants below. A small PR is
sufficient to retune the CLOSE-band widths; that is observation-only and
does NOT touch entry-signal logic, so it does not need the
``risk-review-approved`` label.

See ``Docs/signal-proximity-design.md`` §3.2 + §3.3 + §4.1 for the
canonical contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Literal

# ---------------------------------------------------------------------------
# Operator-tunable thresholds. Re-tuning these is a small PR (no risk-review
# required); they classify already-computed values into PASS/CLOSE/FAIL bands
# for UI display and do NOT influence what fires.
# ---------------------------------------------------------------------------

#: Donchian: within 1% of the breakout line = CLOSE.
DONCHIAN_CLOSE_BAND_PCT: Decimal = Decimal("0.01")

#: Trend: within 0.5% of crossover = CLOSE.
TREND_CLOSE_BAND_PCT: Decimal = Decimal("0.005")

#: Efficiency Ratio: within 0.05 (ER units) of the threshold = CLOSE.
EFFICIENCY_CLOSE_BAND: Decimal = Decimal("0.05")


class GateState(StrEnum):
    """Per-gate categorical state surfaced to the api + frontend."""

    PASS = "pass"  # noqa: S105 — enum value naming a gate state, not a password
    CLOSE = "close"
    FAIL = "fail"


#: Ordering for "worst gate wins" comparisons. PASS > CLOSE > FAIL. The
#: overall market state per §3.3 is the WORST gate; ``closest_gate`` names
#: the gate driving that worst state.
_STATE_RANK: dict[GateState, int] = {
    GateState.PASS: 2,
    GateState.CLOSE: 1,
    GateState.FAIL: 0,
}


#: Gate-status sentinel for the per-market record. Distinct from the per-
#: gate state because "warming up" (no data) and "decommissioned" (operator
#: kill switch) are NOT just "all gates failing" — they need separate UI
#: treatment per Q4 / §3.4.
GateStatus = Literal["ok", "warming_up", "decommissioned"]


@dataclass(frozen=True, slots=True)
class GateProximity:
    """One gate's proximity state + numeric headroom + optional detail."""

    state: GateState
    headroom: Decimal | None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class MarketProximity:
    """One market's per-gate proximity, plus the overall summary.

    ``last_close`` is None when the market is warming up. ``overall_state``
    is the worst of (closer-of-Donchian-directions, closer-of-Trend-directions,
    efficiency). ``closest_gate`` names which of ``'donchian'`` / ``'trend'`` /
    ``'efficiency'`` drives that worst state, or ``'history'`` when warming up.

    ``efficiency_value`` / ``efficiency_threshold`` are the RAW Efficiency
    Ratio and the active threshold at evaluation time. They are carried (in
    addition to the ``efficiency`` gate's state + headroom) so the live ER
    distribution is mineable directly from ``signal_proximity`` for the
    calibration that confirms / adjusts the 0.20 launch threshold (no
    backtester yet — the live distribution is the interim evidence). The
    other gates don't carry raw values because they have no equivalent
    calibration need. ``efficiency_value`` is None when warming up;
    ``efficiency_threshold`` is always known (it's a parameter).
    """

    market: str
    long_donchian: GateProximity
    short_donchian: GateProximity
    long_trend: GateProximity
    short_trend: GateProximity
    efficiency: GateProximity
    last_close: Decimal | None
    efficiency_value: Decimal | None
    efficiency_threshold: Decimal | None
    overall_state: GateState
    closest_gate: str
    gate_status: GateStatus = "ok"


def _classify_donchian(
    *,
    last_close: Decimal,
    threshold: Decimal,
    direction: Literal["long", "short"],
) -> GateProximity:
    """Per-direction Donchian classification.

    Long: passes when ``last_close > donchian_high``. Headroom expressed as
    ``(donchian_high - last_close) / last_close`` so positive values mean
    "still below the upside breakout line"; <= 0 means already broken.

    Short: passes when ``last_close < donchian_low``. Headroom expressed
    symmetrically as ``(last_close - donchian_low) / last_close``.
    """
    if direction == "long":
        # Positive when still below the upside breakout; <= 0 = broken upside.
        headroom = (threshold - last_close) / last_close
    else:
        # Positive when still above the downside breakout; <= 0 = broken downside.
        headroom = (last_close - threshold) / last_close
    if headroom <= 0:
        return GateProximity(state=GateState.PASS, headroom=headroom)
    if headroom <= DONCHIAN_CLOSE_BAND_PCT:
        return GateProximity(state=GateState.CLOSE, headroom=headroom)
    return GateProximity(state=GateState.FAIL, headroom=headroom)


def _classify_trend(
    *,
    last_close: Decimal,
    ma_fast: Decimal,
    ma_slow: Decimal,
    direction: Literal["long", "short"],
) -> GateProximity:
    """Per-direction trend-filter classification.

    Long passes when ``last_close > ma_fast > ma_slow``. Short is the mirror.
    Headroom = the SMALLER of the two gaps that have to flip for the gate to
    fail (or to start passing): close-vs-MA-fast and MA-fast-vs-MA-slow.
    Positive when both inequalities point the right way; negative when at
    least one is on the wrong side (the magnitude reflects how far off).
    """
    if direction == "long":
        passing = last_close > ma_fast > ma_slow
        close_vs_fast = (last_close - ma_fast) / last_close
        fast_vs_slow = (ma_fast - ma_slow) / last_close
    else:
        passing = last_close < ma_fast < ma_slow
        close_vs_fast = (ma_fast - last_close) / last_close
        fast_vs_slow = (ma_slow - ma_fast) / last_close
    closer_gap = min(close_vs_fast, fast_vs_slow)
    if passing:
        return GateProximity(state=GateState.PASS, headroom=closer_gap)
    if abs(closer_gap) <= TREND_CLOSE_BAND_PCT:
        return GateProximity(state=GateState.CLOSE, headroom=closer_gap)
    return GateProximity(state=GateState.FAIL, headroom=closer_gap)


def _classify_efficiency(
    *,
    efficiency_value: Decimal,
    efficiency_threshold: Decimal,
) -> GateProximity:
    """Direction-agnostic Efficiency Ratio classification.

    Mirrors the prior Hurst classification's shape. Passes when
    ``efficiency_value >= efficiency_threshold + EFFICIENCY_CLOSE_BAND``
    (strict-pass band); CLOSE when within ``EFFICIENCY_CLOSE_BAND`` either
    side of the threshold; FAIL otherwise. ``headroom`` is value minus
    threshold (in ER units), positive when above.
    """
    headroom = efficiency_value - efficiency_threshold
    if headroom >= EFFICIENCY_CLOSE_BAND:
        return GateProximity(state=GateState.PASS, headroom=headroom)
    if headroom >= -EFFICIENCY_CLOSE_BAND:
        return GateProximity(state=GateState.CLOSE, headroom=headroom)
    return GateProximity(state=GateState.FAIL, headroom=headroom)


def _warming_up_gate() -> GateProximity:
    """Sentinel gate proximity used for warming-up markets — FAIL with no headroom."""
    return GateProximity(state=GateState.FAIL, headroom=None, detail="warming_up")


def _worst(a: GateState, b: GateState) -> GateState:
    """Pick the WORSE state (FAIL < CLOSE < PASS); used for overall = min(gates)."""
    return a if _STATE_RANK[a] <= _STATE_RANK[b] else b


def _better(a: GateState, b: GateState) -> GateState:
    """Pick the BETTER state; used for per-gate closer-of-directions display."""
    return a if _STATE_RANK[a] >= _STATE_RANK[b] else b


def compute_market_proximity(
    *,
    market: str,
    last_close: Decimal | None,
    donchian_high: Decimal | None,
    donchian_low: Decimal | None,
    ma_fast: Decimal | None,
    ma_slow: Decimal | None,
    efficiency_value: Decimal | None,
    efficiency_threshold: Decimal,
    decommissioned: bool = False,
) -> MarketProximity:
    """Build the per-market proximity record.

    All snapshot fields are either present together (a complete snapshot was
    computable) or all None (warming up). The strategy decides which case
    applies and passes accordingly.

    ``decommissioned=True`` does NOT change the numeric classification —
    per Q4 of the design doc, decommissioned rows still surface what the
    gates WOULD have said. The flag only flips ``gate_status`` so the api +
    frontend can render the "view-only" banner.
    """
    warming_up = (
        last_close is None
        or donchian_high is None
        or donchian_low is None
        or ma_fast is None
        or ma_slow is None
        or efficiency_value is None
    )
    if warming_up:
        sentinel = _warming_up_gate()
        gate_status: GateStatus = "decommissioned" if decommissioned else "warming_up"
        return MarketProximity(
            market=market,
            long_donchian=sentinel,
            short_donchian=sentinel,
            long_trend=sentinel,
            short_trend=sentinel,
            efficiency=sentinel,
            last_close=last_close,
            efficiency_value=efficiency_value,
            efficiency_threshold=efficiency_threshold,
            overall_state=GateState.FAIL,
            closest_gate="history",
            gate_status=gate_status,
        )

    # Narrowing: the warming_up guard above proves all fields are non-None.
    assert last_close is not None
    assert donchian_high is not None
    assert donchian_low is not None
    assert ma_fast is not None
    assert ma_slow is not None
    assert efficiency_value is not None

    long_donchian = _classify_donchian(
        last_close=last_close, threshold=donchian_high, direction="long"
    )
    short_donchian = _classify_donchian(
        last_close=last_close, threshold=donchian_low, direction="short"
    )
    long_trend = _classify_trend(
        last_close=last_close, ma_fast=ma_fast, ma_slow=ma_slow, direction="long"
    )
    short_trend = _classify_trend(
        last_close=last_close, ma_fast=ma_fast, ma_slow=ma_slow, direction="short"
    )
    efficiency = _classify_efficiency(
        efficiency_value=efficiency_value, efficiency_threshold=efficiency_threshold
    )

    donchian_market_state = _better(long_donchian.state, short_donchian.state)
    trend_market_state = _better(long_trend.state, short_trend.state)

    overall_state = _worst(_worst(donchian_market_state, trend_market_state), efficiency.state)

    # closest_gate names the gate driving the worst state. When two gates tie
    # for worst, pick the first in the (donchian, trend, efficiency) order —
    # the operator's mental model walks the gate list in that sequence.
    if donchian_market_state == overall_state:
        closest_gate = "donchian"
    elif trend_market_state == overall_state:
        closest_gate = "trend"
    else:
        closest_gate = "efficiency"

    return MarketProximity(
        market=market,
        long_donchian=long_donchian,
        short_donchian=short_donchian,
        long_trend=long_trend,
        short_trend=short_trend,
        efficiency=efficiency,
        last_close=last_close,
        efficiency_value=efficiency_value,
        efficiency_threshold=efficiency_threshold,
        overall_state=overall_state,
        closest_gate=closest_gate,
        gate_status="decommissioned" if decommissioned else "ok",
    )


__all__ = [
    "DONCHIAN_CLOSE_BAND_PCT",
    "EFFICIENCY_CLOSE_BAND",
    "TREND_CLOSE_BAND_PCT",
    "GateProximity",
    "GateState",
    "GateStatus",
    "MarketProximity",
    "compute_market_proximity",
]
