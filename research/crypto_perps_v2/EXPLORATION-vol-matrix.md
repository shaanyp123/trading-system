# EXPLORATORY — Volume-Confirmation Threshold × Position-Size Matrix

**Date:** 2026-07-20 · **Status: NON-PRE-REGISTERED EXPLORATION.** Operator-directed
follow-up to the pre-registered study (`REPORT.md`), run *after* seeing its results.
Nothing here is adoption evidence — cells are selected by this historical sample. Its
legitimate outputs are the *shape* of the threshold response curve and hypotheses for a
future pre-registered v2.1 with out-of-sample data. Engine: `explore_vol_matrix.py`
(shares `backtest_v2.py`; parity guard re-asserted on shared indicator frames). Raw
numbers: `results_vol_matrix.json`.

**Axes.** Threshold = volume-confirmation cut (entry/increase allowed only when the day's
volume ≥ threshold × its 20-day median; `off` = no filter = incumbent behavior).
Size scale `s` = Amendment-A-style coherent risk scaling (v_target 0.40·s, per-trade risk
0.025·s, daily/weekly loss limits ·s, gross cap 2.0+max(0,s−1), dd-tier levels ·s,
halt min(0.50, 0.50/s)); s=2 reproduces the Amendment A "2× profile" structure.

## Sharpe matrix — full sample 2017-01-01 → 2026-06-30 (base costs; 2× costs in brackets)

| | off | 1.00 | 1.25 | **1.50** | 1.75 | 2.00 |
|---|---|---|---|---|---|---|
| **s=0.5** | 1.01 (0.83) | 1.01 (0.77) | 0.99 (0.95) | **1.20 (1.20)** | 1.06 (0.93) | 1.07 (0.91) |
| **s=1.0** | 1.04 (0.82) | 0.97 (0.85) | 0.99 (0.90) | **1.26 (1.19)** | 1.08 (0.96) | 0.98 (0.97) |
| **s=1.5** | 1.03 (0.87) | 0.95 (0.79) | 0.96 (0.90) | **1.26 (1.18)** | 1.10 (0.96) | 1.05 (0.99) |
| **s=2.0** | 1.02 (0.82) | 0.95 (0.79) | 0.99 (0.88) | **1.31 (1.21)** | 1.10 (1.00) | 1.06 (0.95) |

Companion metrics at s=1: CAGR off +16.7% / th1.5 +17.3% / th1.75 +13.1%; max DD 27.8% /
21.7% / 16.2%; 2023+ Sharpe 0.44 / 1.21 / **1.39 (peak is at 1.75, not 1.50)**; trades/yr
88 / 29 / 22; costs $3,560 / $1,079 / $777; days-in-market 84% / 62% / 50%. At s=2 the
no-filter profile draws down 51.3%; th1.5 cuts it to 39.0%. No cell halted.

## Reading the shape — five observations

1. **The threshold curve is a SPIKE at 1.50, not a plateau.** Both neighbors are much
   lower (1.25 → 0.99, 1.75 → 1.08 at s=1), and 1.00 does nothing at all. Under the
   pre-registered framework's own logic this is the *unfavorable* outcome: a broad
   plateau would have suggested a real, parameter-insensitive effect; a one-column spike
   is what sample-specific luck looks like. The full-sample Sharpe peak and the 2023+
   peak even disagree (1.50 vs 1.75) — there is no single "right" threshold in this data.
2. **The spike's consistency across all four size rows is NOT independent confirmation.**
   Size scaling reuses the same signal sequence at scaled size (days-in-market is nearly
   identical down each column), so the four rows are near-replicas of one trade history,
   not four experiments. The matrix has effectively ONE informative axis.
3. **What does look structural: the cost/drawdown mechanics, especially under fee stress.**
   At 2× costs every threshold ≥1.25 beats no-filter at every size (e.g. s=1: 0.90/1.19/
   0.96/0.97 vs 0.82) — mechanically sensible: fewer, more selective entries cut the fee
   bleed that dominates the stressed scenario, and max DD falls monotonically with the
   threshold. This part does not depend on picking the magic 1.50.
4. **The entry-lag toll is real and concentrated in the mega-trend years:** th1.5 gives up
   roughly half of 2019 (+13.4% vs +28.5%) and a third of 2020 (+28.9% vs +44.8%). It is
   repaid after 2021: the filter wins EVERY year 2021–2026H1, including 2024 (+11.5% vs
   +3.9%), 2025 (+17.3% vs −2.0%) and 2026H1 (+20.3% vs +9.0%). Two readings, untestable
   on this sample alone: (a) a regime change — post-2021 crypto volume is more informative
   (institutionalization) and chop is more common, so the filter's era has arrived; or
   (b) recency overfit. Only out-of-sample data separates them.
5. **Exposure diagnostic:** days-in-market falls 84% → 62% → 50% → 42% as the threshold
   rises. Above ~1.75 the strategy is flat half the time — Sharpe stays decent while
   dollar-earning capacity shrinks (th2.0 CAGR +10.1% at s=1). The economically usable
   region tops out around 1.5–1.75.
6. **Size axis behaves as expected:** Sharpe is flat in s (same trades, scaled size);
   CAGR and drawdown scale together (s=2 no-filter: +31.1% CAGR, 51.3% DD). The filter's
   DD reduction is what would make higher s tolerable — relevant to the Amendment B
   production profile, which already runs the s=2 structure.

## Implication for v2.1 (unchanged from REPORT.md, now sharpened)

The case for volume confirmation rests on its cost/DD mechanics and its post-2021
behavior — not on the 1.50 Sharpe spike, which the neighboring thresholds refuse to
corroborate. A v2.1 pre-registration should: declare a threshold *band* (e.g. 1.4–1.8
evaluated jointly, not one winner); score on the metrics the filter actually moves (cost
load, max DD, 2023+ Sharpe) with a no-degradation floor on full-sample Sharpe; and use
2026H2+ live-period data (accruing now) as true out-of-sample. If the effect is regime
(reading 4a), it should show up there without any threshold tuning.

## Reproduce

```bash
python3 research/crypto_perps_v2/explore_vol_matrix.py   # parity guard + 48 runs -> results_vol_matrix.json
```
