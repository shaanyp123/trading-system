"""EXPLORATORY (non-pre-registered) — volume-confirmation threshold x size matrix.

Operator-directed follow-up (2026-07-20) to the pre-registered v2 study: map the
response surface of the volume entry-confirmation filter across (a) the
confirmation threshold (x of 20d median volume) and (b) position-size profile.

NOT adoption evidence. The pre-registered study is REPORT.md; this sweep is run
AFTER seeing those results, so any cell picked from it is picked because it won
on this one historical sample. Its only legitimate outputs are (1) the SHAPE of
the threshold response curve (plateau = believable effect; spike = overfit) and
(2) hypotheses for a future pre-registered v2.1 with out-of-sample data.

Size axis: Amendment-A-style COHERENT scaling by factor s (all risk knobs move
together, mirroring the operator's locked 2x-profile precedent at s=2):
  v_target=0.40*s, per_trade_risk=0.025*s, daily_loss=-0.04*s, weekly=-0.08*s,
  gross_cap=2.0+max(0,s-1), dd tier LEVELS *s (multipliers unchanged),
  halt_frac=min(0.50, 0.50/s).

Run:  python3 research/crypto_perps_v2/explore_vol_matrix.py
Writes results_vol_matrix.json. Read with EXPLORATION-vol-matrix.md.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from backtest_v2 import (  # noqa: E402
    ASSETS,
    INCUMBENT_RESULTS,
    Params,
    compute_indicators,
    load_asset,
    run_backtest,
)

RESULTS_PATH = os.path.join(HERE, "results_vol_matrix.json")

START, END = "2017-01-01", "2026-06-30"
SIZE_SCALES = (0.5, 1.0, 1.5, 2.0)
THRESHOLDS = (None, 1.00, 1.25, 1.50, 1.75, 2.00)  # None = no volume filter


def cell_name(s: float, th: float | None, tag: str) -> str:
    return f"s{s:g}_th{'off' if th is None else th}{tag}"


def sized_params(s: float) -> Params:
    base = Params()
    return replace(
        base,
        v_target=base.v_target * s,
        per_trade_risk_frac=base.per_trade_risk_frac * s,
        daily_loss_limit=base.daily_loss_limit * s,
        weekly_loss_limit=base.weekly_loss_limit * s,
        gross_cap=base.gross_cap + max(0.0, s - 1.0),
        dd_tiers=tuple((min(lvl * s, 9.9), m) for lvl, m in base.dd_tiers),
        halt_frac=min(0.50, 0.50 / s),
    )


def main() -> None:
    # indicators are size/threshold-invariant (volc_window fixed at 20; rel_vol
    # is computed when volc_enabled and simply ignored by non-filter runs), so
    # compute the frames ONCE and share them across all cells
    ind_params = replace(Params(), volc_enabled=True)
    data = {sym: compute_indicators(load_asset(sym), ind_params) for sym in ASSETS}

    # parity guard: features-off run on the shared frames must still match the
    # committed incumbent base, proving frame-sharing introduced no drift
    parity = run_backtest(data, Params(), START, END)
    with open(INCUMBENT_RESULTS) as f:
        incumbent_base = json.load(f)["base"]
    for k in ("final_equity", "sharpe", "n_position_changes", "total_costs"):
        assert parity[k] == incumbent_base[k], (
            f"PARITY FAIL on {k}: {parity[k]} != {incumbent_base[k]}"
        )
    print("parity gate PASS (shared indicator frames reproduce incumbent base)\n")

    results: dict[str, dict[str, Any]] = {}
    for cost_mult, tag in ((1.0, ""), (2.0, "_2xcost")):
        for s in SIZE_SCALES:
            for th in THRESHOLDS:
                p = sized_params(s)
                if th is not None:
                    p = replace(p, volc_enabled=True, volc_thresh=th)
                p = replace(p, cost_mult=cost_mult)
                name = cell_name(s, th, tag)
                results[name] = run_backtest(data, p, START, END)
                r = results[name]
                print(
                    f"{name:22s} ret={r['total_return']:+8.1%} cagr={r['cagr']:+7.1%} "
                    f"sharpe={r['sharpe']:+5.2f} dd={r['maxdd']:6.1%} "
                    f"s23={r['sharpe_2023on']:+5.2f} t/yr={r['trades_per_year']:5.1f} "
                    f"mkt={r['pct_days_in_market']:5.1%} lev={r['avg_gross_leverage']:.2f} "
                    f"costs=${r['total_costs']:7.0f} halted={r['halted']}"
                )
        print()

    with open(RESULTS_PATH, "w") as f:
        json.dump(results, f, indent=2)
    print(f"results -> {RESULTS_PATH}")

    # compact Sharpe matrices for the terminal
    for tag, label in (("", "1x costs"), ("_2xcost", "2x costs")):
        print(f"\nSharpe matrix ({label}) — rows: size scale s; cols: threshold")
        header = "      " + "".join(
            f"{('off' if th is None else f'{th:.2f}'):>8s}" for th in THRESHOLDS
        )
        print(header)
        for s in SIZE_SCALES:
            cells = "".join(f"{results[cell_name(s, th, tag)]['sharpe']:8.2f}" for th in THRESHOLDS)
            print(f"s={s:<4g}{cells}")


if __name__ == "__main__":
    main()
