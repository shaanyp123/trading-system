# v2 Exploration Pre-registration — Donchian + Volume

**Date:** 2026-07-20 · **Status: FROZEN before any variant was run.** This file is committed
*before* `backtest_v2.py` produces any variant results, so the design cannot be shaped by
peeking at outcomes. Operator directive (2026-07-20 session): "document that looking at
Donchian and volume might be something we want to look at in v2 of strategy" + "run the v2
backtest" — offline research only, **zero changes to the live C1 system**.

## Scope and non-goals

- Everything lives under `research/crypto_perps_v2/`. No `services/**`, no `lean/**`, no
  deploy surface, no parameter changes to the running strategy. The incumbent artifacts in
  `research/crypto_perps/` stay byte-frozen for comparability.
- The question under test: **does adding Donchian-channel information and/or a volume filter
  to the §4 trend ensemble improve the strategy net of costs, robustly?** This mirrors the
  "fourth ensemble member / filter" framing, not a full Turtle-style challenger system (that
  would be a separate, larger pre-registration).
- A "no" is an acceptable and useful verdict and will be documented as such.

## Engine

`backtest_v2.py` is a copy of the incumbent engine (`research/crypto_perps/backtest.py`,
sections 4–7 semantics, identical cost/funding model) extended with the feature switches
below, all **off by default**.

**Parity gate (must pass before any variant is interpreted):** with every v2 feature
disabled, the v2 engine must reproduce the incumbent `base` scenario **exactly** (same
final equity to the cent, same Sharpe, same trade count) against the committed
`research/crypto_perps/results.json`. If parity fails, fix the engine first; variant
numbers produced before parity passes are void.

## Variants (all defined now; parameters frozen)

Baseline for every comparison: incumbent `base` profile (v_target 0.40, §7 risk framework,
18 bps/RT, funding 10.95%/yr parametric), run in the same engine, same 2017-01-01 →
2026-06-30 window, $6,000 start.

**Donchian — 4th ensemble member.** Channel over the *prior* N days (exclusive of today):
`ch_hi_t = max(high[t-N..t-1])`, `ch_lo_t = min(low[t-N..t-1])`. N = **55** (classic Turtle
long lookback; deliberately not tuned).

- **D1 `don_latch`:** s_d latches +1 when `close_t > ch_hi_t` (N-day breakout up), −1 when
  `close_t < ch_lo_t`; otherwise holds its prior value. Seeded on the first valid bar by
  comparison to the channel midline.
- **D2 `don_mid`:** stateless: s_d = +1 if `close_t > (ch_hi_t + ch_lo_t)/2` else −1.

With four members, TrendScore = (s_a+s_b+s_c+s_d)/4 ∈ {−1, −½, 0, +½, +1}. Direction =
sign(TrendScore); **a score of exactly 0 maps to flat** (consistent with §4's "flat is
entered when TrendScore crosses 0 without confirmation"). Strength = |TrendScore|. Shorts
still require full-strength bearish (TrendScore = −1, i.e. all four members bearish) —
same "full-strength score" spirit as the incumbent short gate. Hysteresis unchanged
(2 consecutive closes to change direction).

**Volume — filter/scaler on FMP aggregate spot volume.** Always *relative* volume (raw
levels grew ~500× over the sample and are unusable).

- **V1 `vol_confirm`:** rv_t = volume_t / median(volume, 20d). New entries and position
  *increases* require **rv ≥ 1.25** on the decision bar; otherwise the increase is deferred
  (hold current position, re-check next day). Exits and reductions are never blocked. A
  direction flip executes its exit leg unconditionally; the new-direction entry leg needs
  confirmation (unconfirmed flip → flat, enter on the next confirmed bar).
- **V2 `vol_scale`:** size multiplier m_t = clip( mean(volume, 5d) / median(volume, 60d),
  **0.7, 1.3** ) applied to target notional (after strength, before risk caps — caps always
  win).

**Run matrix (fixed):** D1, D2, V1, V2, D1+V1, D1+V2, D2+V1, D2+V2 — each at 1× and 2×
costs — plus incumbent base at 1× and 2× costs in the same engine.

**Perturbations (fixed):** for every single-feature variant, ±20% on the *new* parameters
only: N ∈ {44, 66}; confirm threshold ∈ {1.00, 1.50}; confirm window ∈ {16, 24}; scale
clip ∈ {(0.76, 1.24), (0.64, 1.36)}; scale windows ∈ {(4, 48), (6, 72)}.

## Pass/fail criteria (frozen; ALL must hold for a variant to be "PROMISING")

- **P1** Sharpe ≥ incumbent-base Sharpe + 0.05 (must clear a real bar, not noise).
- **P2** 2×-cost Sharpe ≥ incumbent 2×-cost Sharpe (no fee-fragile wins).
- **P3** 2023+ subsample Sharpe ≥ incumbent 2023+ subsample Sharpe (no selling the recent
  regime to buy 2017).
- **P4** Worst single-year max DD ≤ 40% (§9 F3 carried over).
- **P5** Every pre-registered perturbation of the variant's own new parameters keeps total
  return positive AND Sharpe within ±0.15 of the variant's headline Sharpe (no parameter
  cliff).
- **P6** Total costs ≤ 1.5× incumbent's (turnover discipline; fees are the known killer).

Any variant failing any criterion is reported as **NOT ADOPTED** with its numbers shown.
If a combo is PROMISING, it additionally gets one confirmatory run under the production
(Amendment B) profile — reported, but adoption criteria are P1–P6 on the base profile only.

## Known limitations carried into interpretation

- FMP aggregate spot volume: provenance unclear, includes wash-prone exchange-reported
  volume; treat volume findings as *market-regime* evidence, not venue-liquidity evidence
  (live venue is thin CDE nano books).
- Parametric funding, daily bars, spot-as-perp proxy — all incumbent REPORT.md limitations
  apply unchanged.
- Even a PROMISING verdict here is **research-only**: production adoption would require a
  spec amendment, the full §9 falsification protocol on the amended spec, operator
  sign-off, and a `risk-review-approved` PR. Nothing in this exploration changes what runs.
