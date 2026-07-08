# Crypto Perps Strategy — Backtest Validation Report

**Date:** 2026-07-08 · **Strategy:** `Docs/crypto-perps-strategy.md` (§4–§7, parameters frozen, zero optimization) · **Verdict: PASS — all four §9 falsification criteria clear.** Proceed to spec/build → small-live, subject to the findings below.

## Headline results (2017-01-01 → 2026-06-30, $6,000 start)

| Scenario | CAGR | Sharpe | Max DD | Sharpe 2023+ | Halted? |
|---|---|---|---|---|---|
| **Base** (18 bps/RT, funding 10.95%/yr) | **+16.7%** | **1.04** | **27.8%** | **+0.44** | no |
| Costs 2× | +12.3% | 0.82 | 29.7% | +0.26 | no |
| Funding 0× | +17.1% | 1.07 | 27.4% | +0.49 | no |
| Funding 2× | +15.6% | 0.99 | 28.2% | +0.35 | no |
| Gross (no costs/funding) | +21.1% | 1.27 | 25.9% | +0.69 | no |

Per-year (base): 2017 +52.7% · 2018 +10.4% · 2019 +28.5% · 2020 +44.8% · 2021 +1.8% · 2022 +13.3% · 2023 +6.6% · 2024 +3.9% · 2025 **−2.0%** · 2026H1 **+9.0%** (a −53% BTC bear year — the strategy made money short/flat). Only one losing year in ten. Final equity $25,982.

Costs totaled $3,560 over 9.5y (~6.2% of starting equity/yr — inside the spec §8 prediction of 6–8%); funding paid $1,581. Trade mix: 775 signal-driven position changes, 49 stop-outs, 9 daily-loss flattens (~88 position changes/yr ≈ 44 round trips/yr, vs spec's 30–60 expectation — consistent).

## Falsification criteria (§9) — all PASS

1. **Full-sample net Sharpe ≥ 0.30, 2×-cost Sharpe ≥ 0:** PASS (1.04 / 0.82).
2. **2023–2026 subsample Sharpe ≥ 0:** PASS (+0.44; +20.9% total return over the window).
3. **No walk-forward year with max DD > 40%:** PASS (worst single-year DD 27.8%, in 2024).
4. **No sign flip under ±20% lookback perturbation:** PASS — remarkably flat: all 8 perturbations (each lookback ±20%, singly and jointly) land in Sharpe 0.97–1.10, total return +292% to +371%. No parameter cliff.

## Findings the operator must understand before build

**F-1. The strategy realizes ~16% portfolio vol, not the 40% headline target.** Average gross leverage was **0.21x** (max 0.74x — the 2.0x cap never came close to binding). The spec's own multiplicative dampeners stack: trend strength is 1/3 (not 1) on 38% of days; drawdown tiers shave size after any 10% dip; median EWMA vol (BTC 59%, ETH 79%) sits well above the 45% used in the spec's worked example; and the ETH<$2,000 rule (F-2). Consequence: **absolute return expectations should be set off ~16% realized vol — think high-single-digit to mid-teens CAGR in normal regimes, not 40%-vol returns.** This is conservative and is part of why the system survives everything thrown at it; it is not a bug. Sensitivity runs confirm the §7 per-trade risk cap is NOT the binding constraint (removing it entirely: avg leverage 0.22x, Sharpe 0.94).

**F-2. ETH is excluded by the spec's own $2,000 minimum-price rule for ~64% of the sample — including right now** (ETH ≈ $1,570 as of 2026-06-30). At go-live this is effectively a BTC-only system until ETH recovers. The backtest already reflects this.

**F-3. The funding gate never fires in backtest** (parametric funding is constant; the +30% veto and −10% short gate need real funding extremes). It is a live-only risk control, validated only by live gate B3. Related: the constant-funding model means backtested shorts *earn* funding; if real CDE funding flips negative in bears (it often does offshore), short-side returns will differ. Bounded by the funding 0×/2× scenarios (Sharpe 0.99–1.07).

**F-4. Spec internal inconsistency to resolve at implementation:** the §6 worked example ($3,556 BTC notional at E=$6,000, ATRp≈4%) violates the §7 per-trade risk cap (2×ATR stop ⇒ $284 expected stop-loss = 4.7% of E > 2.5%). The backtest enforced §7 as written (risk cap wins). Keep that precedence in the production engine, but the doc's example should be corrected.

**F-5. 2024-style chop is the realistic bad case:** +3.9% with a 27.8% DD — a year of fees and whipsaw. The spec predicted exactly this failure mode (self-critique #1). The 2025 result (−2.0%) is the only red year. Anyone deploying should expect flat-to-slightly-negative years to be common; the edge shows up in trend years (2017/2019/2020/2026).

## Implementation approximations (engine vs spec)

Daily-bar simulation necessarily approximates some live behaviors — all choices conservative or neutral:
- Decisions execute at the daily close (spec: 00:05 UTC, 5 min after close). Stops checked against next days' high/low; gap-throughs fill at the open (worse than stop price). Native 3×ATR backstop not simulated (the tighter 2×ATR client stop always fires first in daily bars; the backstop exists for bot-death, not modeled).
- Daily loss limit approximated close-to-close (live: intraday mark). Weekly close / maintenance windows not simulated (proxy data has no halts).
- Dead-band applies to same-direction rebalances only; full exits/entries/flips always execute (spec exit rules take precedence).
- Hysteresis interim: on an unconfirmed sign flip the book goes flat immediately, re-enters on confirmation (per §4 "flat is entered when TrendScore crosses 0 without confirmation").

## Data provenance & limitations

- **Prices:** FMP daily OHLCV `BTCUSD`/`ETHUSD`, 2016-06-01→2026-07-07, fetched via the FMP MCP API (this environment's network policy blocks Coinbase/Binance/Kraken/etc. directly). Validated: zero calendar gaps, zero duplicate dates, zero high/low consistency violations; landmark closes verified (2017 top, 2020-03-12 COVID crash, 2021-11-10 ATH, 2024-12-05); two rows re-fetched independently and matched byte-for-byte.
- **Funding:** no free funding-rate history was reachable from this environment, so funding is **parametric**: constant 10.95%/yr (the 0.01%/8h long-run perp mode), longs pay / shorts receive, stressed at 0× and 2×. This is the report's biggest limitation — real funding is regime-correlated (hot in euphoric rallies, negative in capitulation), which the constant model can't capture. The spec's live gate **B3** (realized CDE funding within 2× of proxy) is the control for this.
- **Spot as perp proxy:** signals and fills use spot bars (per §4, spot is the sanctioned signal input). CDE-specific microstructure (thin nano books, Friday close, hourly funding smoothing) is untested pre-live; that's what §10 Phase A measures.
- §9's other named sources (Binance klines/funding at `data.binance.vision`, Coinalyze) should be pulled as a cross-check whenever this backtest is re-run from a network-open environment.

## Reproduce

```bash
python3 research/crypto_perps/validate_data.py   # data sanity
python3 research/crypto_perps/backtest.py        # full suite -> results.json + falsification verdicts
```

## Amendment A — operator-directed 2× risk profile (2026-07-08)

After reviewing the base results, the operator (full-risk-capital mandate) directed a 2× profile: every risk knob scaled coherently — V_target 80%, per-trade risk 5%, daily/weekly loss limits −8%/−16%, drawdown tiers 20/40/70%, gross cap 3.0x — and asked to remove the 50% hard halt. Validation (same protocol, same data):

| Scenario | CAGR | Sharpe | Max DD | Sharpe 2023+ | Halted/bust? |
|---|---|---|---|---|---|
| Aggressive, halt at $1,500 (−75%) | **+31.1%** | 1.02 | **51.3%** | +0.40 | never |
| Aggressive, **no halt at all** | +31.1% | 1.02 | 51.3% | +0.40 | never |
| Aggressive, costs 2× | +23.2% | 0.82 | 52.7% | +0.27 | never |
| Aggressive, funding 2× | +32.0% | 1.03 | 51.8% | +0.36 | never |

Per-year (aggressive): 2017 +129% · 2018 +20% · 2019 +58% · 2020 +108% · 2021 **−7.1%** · 2022 +22% · 2023 +14% · 2024 +0.6% (with a **48.5% intra-year drawdown**) · 2025 −6.8% · 2026H1 +17%. Final equity $78,523 vs base $25,982. Avg gross leverage 0.41x, peak 1.44x. Costs $17,213 + funding $8,937 over 9.5y (cost drag scales with notional, as expected).

Sharpe is preserved (1.02 vs 1.04) — the 2× profile is the same edge at double size, not a different bet. The scaling is honest: returns roughly double and so do the pain numbers (2024: a 48.5% drawdown to finish +0.6%).

**Halt disposition:** the $1,500 (−75%) halt and no-halt runs are *identical* — the deep halt never triggered in 9.5 years including the 48.5% drawdown. It was therefore **retained at $1,500 as a malfunction circuit-breaker and debit-risk backstop** (a system down 75% is far more likely broken than unlucky, and FCM accounts can gap into negative balances). It costs nothing in backtest and is not a risk-tolerance bound. Recorded as Amendment A in `Docs/crypto-perps-strategy.md`.

## Recommendation

Deploy-gate **PASS**. Proceed to the backend/frontend delta spec and build **at the Amendment A profile**, with these riders: (1) expectations per Amendment A — ~30% CAGR central case with ~50% drawdowns and red years (2021/2025-shaped) as a normal part of the deal; (2) BTC-only at launch per F-2; (3) funding telemetry from day one and §7-over-§6 precedence in the sizing engine per F-3/F-4; (4) the $1,500 halt ships as a malfunction circuit-breaker — removing it entirely was evaluated and adds nothing (identical backtest) while removing the debit/runaway backstop.
