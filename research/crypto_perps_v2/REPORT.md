# v2 Exploration Report — Donchian + Volume

**Date:** 2026-07-20 · **Protocol:** `PREREGISTRATION.md` (committed before any variant ran) ·
**Engine:** `backtest_v2.py` (incumbent engine + feature switches) · **Window:** 2017-01-01 →
2026-06-30, $6,000 start, base profile, 18 bps/RT, parametric funding.

## Verdict: NOT ADOPTED — no variant clears the pre-registered bar (all 8 fail ≥1 of P1–P6)

**Parity gate: PASS.** With every v2 feature disabled, `backtest_v2.py` reproduces the
incumbent base scenario exactly (final equity $25,981.98, Sharpe 1.039, 4,296 position
changes, costs $3,560.26 — identical to committed `research/crypto_perps/results.json`).
Variant numbers are therefore attributable to the features, not engine drift.

## Headline table (incumbent base: Sharpe 1.039 · s23 0.44 · CAGR +16.7% · DD 27.8% · costs $3,560)

| Variant | Sharpe | Sharpe 2023+ | CAGR | Max DD | Costs | Trades/yr | Verdict (failed) |
|---|---|---|---|---|---|---|---|
| D1 don_latch(55) | 0.88 | 0.27 | +13.1% | 26.6% | $2,478 | 76 | NOT ADOPTED (P1, P2, P3) |
| D2 don_mid(55) | 1.06 | 0.49 | +17.2% | 25.0% | $3,489 | 76 | NOT ADOPTED (P1) |
| V1 vol_confirm(1.25×, 20d) | 0.99 | **1.07** | +14.5% | 23.0% | **$1,542** | 47 | NOT ADOPTED (P1, P5) |
| V2 vol_scale(0.7–1.3) | 1.06 | 0.53 | +17.3% | 26.8% | $3,699 | 92 | NOT ADOPTED (P1) |
| D1+V1 | 0.98 | 0.74 | +14.1% | 22.6% | $1,392 | 37 | NOT ADOPTED (P1, P5) |
| D1+V2 | 0.95 | 0.42 | +15.2% | 24.9% | $2,840 | 78 | NOT ADOPTED (P1, P2, P3) |
| D2+V1 | 1.01 | **1.09** | +14.8% | **21.9%** | $1,431 | 38 | NOT ADOPTED (P1, P5) |
| D2+V2 | **1.087** | 0.67 | **+18.8%** | 24.6% | $3,785 | 79 | NOT ADOPTED (P1, P5) |

P1 required Sharpe ≥ 1.089. The best full-sample performer (D2+V2) reached 1.087 — a miss
by 0.002, and it also fails the perturbation-stability check, so this is not a rounding
technicality: the criteria were set before the runs and they hold.

## What the study actually found

1. **The Donchian breakout latch (D1) is harmful in this framework** — Sharpe 0.88 vs 1.039,
   and it degrades further at 2× costs (0.63) and in 2023+ (0.27, turning negative at 2×
   costs). The latch holds stale breakout state through chop, exactly the failure mode the
   incumbent's SMA-state members avoid. This closes the "resurrect V1's entry signal"
   question with data: **no**.
2. **Donchian midline (D2) and volume-scaling (V2) are mild positives, not edges** —
   Sharpe ~1.06 each, +0.02 over incumbent net of costs, well under the +0.05 bar. They are
   diversifying-but-correlated variations of the same trend information the ensemble
   already has.
3. **Volume confirmation (V1) is the genuine lead — but not on the metric it was aimed at.**
   Full-sample Sharpe is *lower* (0.99), yet it halves turnover (47 vs 88 trades/yr), cuts
   costs 57% ($1,542 vs $3,560), trims max DD to 23.0%, and transforms the recent regime:
   2023+ Sharpe **1.07 vs 0.44**, and 2025 — the incumbent's only losing year (−2.0%) —
   becomes **+8.2%**. It fails P5 because the threshold matters a lot: at 1.50× the run
   jumps to Sharpe 1.26 / s23 1.21 / costs $1,079 (the best run in the entire study),
   breaching the ±0.15 stability band *upward*. Per the pre-registration that is parameter
   sensitivity and it fails — and promoting the 1.50 perturbation to a headline post-hoc
   would be textbook data mining, so it is explicitly **not** adopted here.
4. **Combos with V1 inherit its character:** D2+V1 posts the best drawdown (21.9%) and 2023+
   Sharpe (1.09) of any variant at a third of the incumbent's cost load, but the same
   full-sample Sharpe shortfall and threshold sensitivity apply.

## Honest limitations of this study

- **P5 for combos** was evaluated against the *parent single-feature* perturbation runs
  (combo-specific perturbations were not pre-registered or run), compared to the combo's own
  headline Sharpe — a conservative approximation. It changes no verdict: every combo
  independently fails P1.
- Volume is FMP aggregate spot volume (wash-prone, provenance unclear); all incumbent
  limitations (parametric funding, daily bars, spot-as-perp proxy) carry over unchanged.
- One sample, one asset pair. The V1 2023+ improvement is the kind of result that looks
  best right before it mean-reverts; it needs out-of-sample confirmation, not enthusiasm.

## Recommendation for a future v2.1 (requires fresh operator direction — nothing scheduled)

If this thread is picked up again, the pre-registered question should be **volume
confirmation only** (drop Donchian): fix the threshold family in advance (e.g. 1.25/1.375/1.50
all declared as co-primary, with a stability requirement *across* them), add the by-then
accumulated 2026H2+ data as out-of-sample, and evaluate on the metrics V1 actually moves —
cost load, drawdown, and recent-regime Sharpe — with an explicit no-degradation floor on
full-sample Sharpe. Adoption would still require: spec amendment, full §9 falsification
re-run, operator sign-off, `risk-review-approved` PR.

## Reproduce

```bash
python3 research/crypto_perps_v2/backtest_v2.py   # parity gate + full matrix -> results_v2.json
```
