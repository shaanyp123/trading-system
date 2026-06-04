"""Vol-target parity pin: research sizing == the LIVE sizer's Stage-1 (design §6.1).

The research ``vol_target`` scheme re-implements V1's Clenow vol-targeting for the
leverage sweep. This test PINS it against the public result of the live authority
``services/risk/sizing.py`` (a FORBIDDEN path — we IMPORT its result, never modify
it) so the two can never silently diverge. The comparison re-materializes the
research float as ``Decimal`` at the boundary (design D8 / §13 / R7).

For a single active market Stage 1 collapses to the closed form
``unconstrained_notional = equity * vol_target * m_combined / sigma`` — exactly what
:func:`research.risk.sizing_schemes.vol_target_notional` computes.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import numpy as np
import pytest

from research.risk.sizing_schemes import vol_target_notional
from services.risk.sizing import (
    ContractMetadata,
    SizingInputs,
    run_sizing_pipeline,
)
from strategies.v1_trend_following.parameters import default_v1_parameters
from strategies.v1_trend_following.signals import CandidateSignal, Direction

#: Boundary tolerance: 1 cent on a six-figure notional (both sides round to 2dp).
_TOL = Decimal("0.01")


def _live_stage1_notional(
    *, equity: Decimal, annual_vol: float, vol_target: Decimal, m_combined: Decimal
) -> Decimal:
    """Run the LIVE pipeline for one market and return Stage-1 unconstrained notional."""
    signal = CandidateSignal(
        market="/MES",
        direction=Direction.LONG,
        signal_type="donchian_breakout",
        session_date=date(2026, 1, 1),
        decision_price=Decimal("5000"),
        stop_price=Decimal("4900"),
        indicators_snapshot={},
    )
    inputs = SizingInputs(
        candidate_signals=(signal,),
        equity=equity,
        contracts_metadata={"/MES": ContractMetadata("/MES", Decimal("25000"), "equity_index")},
        # Covariance: variance on the diagonal ⇒ annual_vol**2 for one market.
        sigma_annual=np.array([[annual_vol**2]], dtype=np.float64),
        sigma_index=("/MES",),
        momentum_z_scores={"/MES": Decimal("0")},
        vol_target_pct_annual=vol_target,
        m_combined=m_combined,
    )
    result = run_sizing_pipeline(inputs)
    notional = result.sizing_trace["stage_1_inverse_vol"]["unconstrained_notional"]["/MES"]
    return Decimal(notional)


@pytest.mark.parametrize(
    ("annual_vol", "m_combined"),
    [(0.20, Decimal("1.0")), (0.12, Decimal("1.0")), (0.35, Decimal("0.8"))],
)
def test_vol_target_matches_live_stage1(annual_vol: float, m_combined: Decimal) -> None:
    equity = Decimal("1000000")
    vol_target = default_v1_parameters().vol_target_pct_annual  # V1's locked 0.15

    live = _live_stage1_notional(
        equity=equity, annual_vol=annual_vol, vol_target=vol_target, m_combined=m_combined
    )
    research = vol_target_notional(
        equity=float(equity),
        sigma_annual=annual_vol,
        vol_target_pct_annual=float(vol_target),
        m_combined=float(m_combined),
    )
    # Re-materialize the research float as Decimal at the governance boundary (R7).
    research_dec = Decimal(str(research)).quantize(Decimal("0.01"))
    assert abs(research_dec - live) <= _TOL, (
        f"research vol-target {research_dec} diverged from live Stage-1 {live} "
        f"(vol={annual_vol}, m={m_combined}) — the sizers have drifted apart"
    )


def test_vol_target_closed_form_value() -> None:
    # equity 1e6, vol 0.15 target, sigma 0.20 ⇒ 1e6 * 0.15 / 0.20 = 750_000.
    assert vol_target_notional(
        equity=1_000_000.0, sigma_annual=0.20, vol_target_pct_annual=0.15
    ) == pytest.approx(750_000.0)


def test_vol_target_rejects_nonpositive_sigma() -> None:
    with pytest.raises(ValueError):
        vol_target_notional(equity=1_000_000.0, sigma_annual=0.0, vol_target_pct_annual=0.15)
