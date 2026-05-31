"""Per-position exit-proximity classification for the V1 trend-following strategy.

Pure-Python module — zero LEAN imports, pytest-friendly from the project
root. The mirror image of ``proximity.py`` (entry-side "how close is each
flat market to *opening*") for the exit side: for each **open position**,
how close is it to being **closed** by each V1 exit trigger.

Single source of truth (ED1 of ``Docs/exit-proximity-design.md``) for the
**indicator-driven** exit dimensions (trend_flip holding-days + deterioration,
reversal, decommission). The api + frontend consume pre-computed values from
here and never recompute. The exit-trigger *decision* logic in
``strategy.generate_exit_candidates`` is unchanged — this module exposes +
classifies already-computed values, so the display can never drift from what
actually closes (see the drift test in ``tests/unit/test_v1_exit_proximity.py``).

Inputs are bare indicator values (``last_close`` / ``ma_fast`` / ``held_days``
/ ...), NOT the strategy's internal ``_IndicatorSnapshot`` — same two reasons
as ``proximity.py``: (1) decoupling avoids a circular import (``strategy.py``
imports *this* module), and (2) unit tests construct call sites with bare
``Decimal`` literals.

**Stop dimension + Q1 = (B).** The protective stop lives in the api's
``orders`` table, not the strategy. Under the signed-off Q1 = (B) resolution,
LEAN emits the indicator dimensions only and passes ``stop_price=None``; the
api joins the latest working ``stop_market`` order at persist time (PR-B) and
re-classifies the stop dimension via :func:`classify_stop`. This module fully
handles a supplied ``stop_price`` (unit-tested) so PR-B reuses one classifier.

Thresholds are operator-tunable via the constants below. Re-tuning the
NEAR-band widths is a small observation-only PR (no ``risk-review-approved``
required); they classify already-computed values for UI display and do NOT
influence what actually closes.

See ``Docs/exit-proximity-design.md`` §3 + §4 for the canonical contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from strategies.v1_trend_following.proximity import GateState, MarketProximity

# ---------------------------------------------------------------------------
# Operator-tunable NEAR-band thresholds. Re-tuning these is a small PR (no
# risk-review required); they classify already-computed values into
# HOLDING/NEAR/TRIGGERED bands for UI display and do NOT influence what closes.
# ---------------------------------------------------------------------------

#: Trend-flip deterioration: within 0.5% of ``close`` crossing ``MA_FAST`` =
#: NEAR. Mirrors the entry-side ``TREND_CLOSE_BAND_PCT``.
EXIT_TREND_NEAR_BAND_PCT: Decimal = Decimal("0.005")

#: Stop distance: within 1% of the protective stop (as-of the daily mark) =
#: NEAR.
EXIT_STOP_NEAR_BAND_PCT: Decimal = Decimal("0.01")

#: MIN_HOLDING gate: within 3 calendar days of the floor clearing = NEAR (the
#: trend-flip exit becomes eligible soon).
EXIT_HOLDING_DAYS_NEAR_DAYS: int = 3


class ExitState(StrEnum):
    """Per-trigger categorical state surfaced to the api + frontend.

    Valence is the *inverse* of the entry-side ``GateState`` — exit-green
    (HOLDING) means "position safe / far from this exit", exit-red
    (TRIGGERED) means "closing this cycle". A dedicated enum (not a reuse of
    ``GateState``) prevents a color/meaning collision in shared chip
    components (design Q4).
    """

    HOLDING = "holding"  # comfortably far from this exit trigger
    NEAR = "near"  # within the NEAR band — about to exit
    TRIGGERED = "triggered"  # this trigger fired (or would fire) THIS cycle


#: Ordering for "most-advanced-toward-closing wins" comparisons.
#: TRIGGERED > NEAR > HOLDING. The overall position state per §3.3 is the
#: WORST (most-advanced) trigger; ``closest_exit`` names the trigger driving it.
_STATE_RANK: dict[ExitState, int] = {
    ExitState.TRIGGERED: 2,
    ExitState.NEAR: 1,
    ExitState.HOLDING: 0,
}

#: Ranking of the entry-side ``GateState`` for the reversal derivation. The
#: opposite-direction entry fires only when ALL gates PASS, so the opposite
#: entry's overall readiness is its *worst* (limiting) gate: FAIL < CLOSE < PASS.
_GATE_RANK: dict[GateState, int] = {
    GateState.PASS: 2,
    GateState.CLOSE: 1,
    GateState.FAIL: 0,
}

#: Stable final tie-break when two triggers share BOTH state + numeric
#: headroom (e.g. a warming-up row where every trigger is HOLDING with no
#: numeric headroom). trend_flip + stop — the dimensions that normally carry a
#: numeric headroom — rank ahead of the binary reversal/decommission so the
#: label defaults to a meaningful indicator exit rather than the kill switch.
#: (A genuinely-armed decommission is handled by its own precedence branch
#: before this tie-break is consulted.)
_CLOSEST_EXIT_PRECEDENCE: dict[str, int] = {
    "trend_flip": 0,
    "stop": 1,
    "reversal": 2,
    "decommission": 3,
}

Direction = Literal["long", "short"]

#: Position-level discriminator. ``active`` = evaluated normally;
#: ``warming_up`` = insufficient history for the indicator dimensions;
#: ``min_holding_blocked`` = trend-flip cannot fire yet (held < MIN_HOLDING);
#: ``decommissioned`` = operator kill switch armed.
GateStatus = Literal["active", "warming_up", "min_holding_blocked", "decommissioned"]


@dataclass(frozen=True, slots=True)
class ExitTriggerProximity:
    """One exit trigger's state + numeric headroom + optional detail."""

    state: ExitState
    headroom: Decimal | None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class PositionExitProximity:
    """One open position's per-trigger exit proximity, plus the summary.

    ``trend_flip`` combines the MIN_HOLDING-days gate + the deterioration gate
    (§3.2): its ``headroom`` carries the deterioration pct, while ``held_days``
    on this record lets the api derive ``held_days_headroom``. ``stop`` is
    NULL-state when no working stop is known (LEAN under Q1 = (B); the api
    fills it in PR-B). ``reversal`` is derived from the opposite direction's
    entry proximity (ED4). ``overall_state`` is the state of ``closest_exit``,
    the most-advanced-toward-closing trigger.
    """

    market: str
    direction: Direction
    held_days: int | None
    trend_flip: ExitTriggerProximity
    stop: ExitTriggerProximity
    reversal: ExitTriggerProximity
    decommission: ExitTriggerProximity
    last_close: Decimal | None
    stop_price: Decimal | None
    overall_state: ExitState
    closest_exit: str  # 'stop' | 'reversal' | 'trend_flip' | 'decommission'
    gate_status: GateStatus = "active"


def _worst_gate(*states: GateState) -> GateState:
    """Pick the limiting (worst) entry gate — FAIL < CLOSE < PASS."""
    return min(states, key=lambda s: _GATE_RANK[s])


def classify_stop(
    *,
    last_close: Decimal | None,
    stop_price: Decimal | None,
    direction: Direction,
) -> ExitTriggerProximity:
    """Classify the mark-vs-stop distance (exit trigger (a)).

    Long: ``stop_headroom_pct = (mark - stop) / mark`` — positive while the
    mark sits above the protective stop; ``<= 0`` means the stop is breached
    as-of the mark. Short is the mirror. ``mark`` is the daily last close
    (ED3 / Q1 = (B)); a live intraday mark is deferred.

    Returns a NULL-state (HOLDING + ``headroom=None`` + a "no stop" detail)
    when either the stop or the mark is unknown — for LEAN this is the normal
    Q1 = (B) case (the api enriches in PR-B); a genuinely missing stop on a
    held position is also a latent POSITION_UNPROTECTED signal (§3.4).
    """
    if stop_price is None or last_close is None or last_close == 0:
        return ExitTriggerProximity(
            state=ExitState.HOLDING, headroom=None, detail="no stop price on record"
        )
    if direction == "long":
        headroom = (last_close - stop_price) / last_close
    else:
        headroom = (stop_price - last_close) / last_close
    if headroom <= 0:
        return ExitTriggerProximity(state=ExitState.TRIGGERED, headroom=headroom)
    if headroom <= EXIT_STOP_NEAR_BAND_PCT:
        return ExitTriggerProximity(state=ExitState.NEAR, headroom=headroom)
    return ExitTriggerProximity(state=ExitState.HOLDING, headroom=headroom)


def classify_reversal(reversal_entry_state: GateState | None) -> ExitTriggerProximity:
    """Map the opposite direction's entry state to a reversal-exit state (ED4).

    The reversal exit (b) fires when the opposite direction passes all 3 entry
    filters. The opposite direction's entry ``overall_state`` therefore *is*
    the reversal closeness — no new math:

    * entry PASS  → reversal TRIGGERED (opposite would open → this closes)
    * entry CLOSE → reversal NEAR
    * entry FAIL  → reversal HOLDING

    ``None`` (no entry proximity available, e.g. an ``--exits-only`` run)
    degrades to HOLDING with an explanatory detail.
    """
    if reversal_entry_state is None:
        return ExitTriggerProximity(
            state=ExitState.HOLDING, headroom=None, detail="reversal proximity unavailable"
        )
    if reversal_entry_state is GateState.PASS:
        return ExitTriggerProximity(state=ExitState.TRIGGERED, headroom=None)
    if reversal_entry_state is GateState.CLOSE:
        return ExitTriggerProximity(state=ExitState.NEAR, headroom=None)
    return ExitTriggerProximity(state=ExitState.HOLDING, headroom=None)


def directional_reversal_entry_state(
    *,
    market_proximity: MarketProximity | None,
    held_direction: Direction,
) -> GateState | None:
    """Derive the opposite-direction entry readiness from an entry proximity row.

    For a held LONG the reversal is a SHORT entry (and vice versa). The
    opposite entry fires only when its Donchian + trend + Hurst gates all
    PASS, so its readiness is the *worst* of those three gates. Returns
    ``None`` when no entry proximity row is available for the market.
    """
    if market_proximity is None:
        return None
    if held_direction == "long":
        donchian = market_proximity.short_donchian.state
        trend = market_proximity.short_trend.state
    else:
        donchian = market_proximity.long_donchian.state
        trend = market_proximity.long_trend.state
    return _worst_gate(donchian, trend, market_proximity.hurst.state)


def _classify_decommission(*, decommissioned: bool) -> ExitTriggerProximity:
    """Binary: TRIGGERED when the operator kill switch is armed, else HOLDING."""
    return ExitTriggerProximity(
        state=ExitState.TRIGGERED if decommissioned else ExitState.HOLDING,
        headroom=None,
    )


def compute_position_exit_proximity(
    *,
    market: str,
    direction: Direction,
    held_days: int | None,
    last_close: Decimal | None,
    ma_fast: Decimal | None,
    min_holding_days: int,
    decommissioned: bool,
    stop_price: Decimal | None = None,
    reversal_entry_state: GateState | None = None,
) -> PositionExitProximity:
    """Build the per-position exit-proximity record (one held position).

    Snapshot fields (``last_close`` / ``ma_fast``) are either both present (a
    complete snapshot was computable) or both None (warming up). The strategy
    decides which case applies and passes accordingly — exactly like
    ``compute_market_proximity``.

    ``decommissioned=True`` does NOT change the numeric classification of the
    other triggers — per §3.3 + the entry-side Q4 precedent, decommissioned
    rows still surface what the indicator triggers WOULD have said. The flag
    flips ``decommission`` to TRIGGERED + takes ``closest_exit`` precedence so
    the frontend renders the "closing next cycle" banner.
    """
    warming_up = last_close is None or ma_fast is None
    held_days_headroom = (min_holding_days - held_days) if held_days is not None else None

    # --- (c) trend_flip: MIN_HOLDING-days gate + deterioration gate --------
    if warming_up:
        trend_flip = ExitTriggerProximity(
            state=ExitState.HOLDING, headroom=None, detail="warming_up"
        )
        gate_status: GateStatus = "warming_up"
    else:
        # Narrowed by the warming_up guard above.
        assert last_close is not None
        assert ma_fast is not None
        if direction == "long":
            deterioration = (last_close - ma_fast) / last_close
        else:
            deterioration = (ma_fast - last_close) / last_close
        if deterioration <= 0:
            det_state = ExitState.TRIGGERED  # close already crossed MA_FAST
        elif deterioration <= EXIT_TREND_NEAR_BAND_PCT:
            det_state = ExitState.NEAR
        else:
            det_state = ExitState.HOLDING

        if held_days_headroom is not None and held_days_headroom > 0:
            # MIN_HOLDING blocks the flip — it CANNOT fire yet even if close
            # has crossed MA_FAST. Cap the state on the holding-days gate; keep
            # the deterioration pct as the headroom so the UI still shows how
            # far the close sits from MA_FAST.
            hd_state = (
                ExitState.NEAR
                if held_days_headroom <= EXIT_HOLDING_DAYS_NEAR_DAYS
                else ExitState.HOLDING
            )
            trend_flip = ExitTriggerProximity(
                state=hd_state,
                headroom=deterioration,
                detail=f"blocked by MIN_HOLDING ({held_days_headroom}d left)",
            )
            gate_status = "min_holding_blocked"
        else:
            # MIN_HOLDING satisfied (or held_days unknown → strategy skips the
            # floor, §3.4). Deterioration governs the trend-flip state.
            detail = "holding-days unknown" if held_days is None else None
            trend_flip = ExitTriggerProximity(
                state=det_state, headroom=deterioration, detail=detail
            )
            gate_status = "active"

    # --- (a) stop, (b) reversal, (d) decommission -------------------------
    stop = classify_stop(last_close=last_close, stop_price=stop_price, direction=direction)
    reversal = classify_reversal(reversal_entry_state)
    decommission = _classify_decommission(decommissioned=decommissioned)

    if decommissioned:
        # The kill switch overrides every indicator dimension for the *status*
        # banner, but the numeric trigger states above are preserved.
        gate_status = "decommissioned"

    # --- overall state + closest_exit (§3.3) ------------------------------
    triggers: dict[str, ExitTriggerProximity] = {
        "trend_flip": trend_flip,
        "stop": stop,
        "reversal": reversal,
        "decommission": decommission,
    }
    if decommission.state is ExitState.TRIGGERED:
        # Decommission takes precedence (operator kill switch closes everything).
        overall_state = ExitState.TRIGGERED
        closest_exit = "decommission"
    else:
        # Most-advanced-toward-closing wins; tie-break by smallest numeric
        # headroom (closest to its line), then the stable precedence order.
        def _sort_key(item: tuple[str, ExitTriggerProximity]) -> tuple[int, Decimal, int]:
            name, trigger = item
            headroom_key = trigger.headroom if trigger.headroom is not None else Decimal("Infinity")
            return (-_STATE_RANK[trigger.state], headroom_key, _CLOSEST_EXIT_PRECEDENCE[name])

        closest_exit, closest_trigger = min(triggers.items(), key=_sort_key)
        overall_state = closest_trigger.state

    return PositionExitProximity(
        market=market,
        direction=direction,
        held_days=held_days,
        trend_flip=trend_flip,
        stop=stop,
        reversal=reversal,
        decommission=decommission,
        last_close=last_close,
        stop_price=stop_price,
        overall_state=overall_state,
        closest_exit=closest_exit,
        gate_status=gate_status,
    )


__all__ = [
    "EXIT_HOLDING_DAYS_NEAR_DAYS",
    "EXIT_STOP_NEAR_BAND_PCT",
    "EXIT_TREND_NEAR_BAND_PCT",
    "ExitState",
    "ExitTriggerProximity",
    "GateStatus",
    "PositionExitProximity",
    "classify_reversal",
    "classify_stop",
    "compute_position_exit_proximity",
    "directional_reversal_entry_state",
]
