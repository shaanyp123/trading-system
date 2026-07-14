# Macro-Events Tier-1 Study — Scheduled-Event Entry Filters on the Reference Backtest

**Date:** 2026-07-14 · **Scope:** research only (no production code, no migrations, no `services/**` changes) · **Authority:** the reference backtest (`research/crypto_perps/`, Amendment B `production` scenario) is LOCKED; every variant here is a study, never a new baseline.

**Headline: the data does not support an entry filter.** A 24 h pre-event entry filter is a wash (return −0.20 pt CAGR suppressed / +0.15 pt half-sized — both inside path noise), gives **zero max-drawdown protection** in 9.5 years, and a 12 h window is **structurally empty** (it cannot even catch the incident that motivated this study). The counterfactual sample of real entries inside event windows is 15 trades in 9.5 years — too small to justify a behavior change on its own. Recommendation: **option (i), observation-only wiring** (details in the decision prompt at the end).

## 1. Motivation

On 2026-07-14 the 00:05 UTC decision opened a short immediately before an uptrend; a cold CPI print at 12:30 UTC that morning (~12.4 h after the decision instant) was on the public calendar weeks in advance but invisible to the system. The 2026-07-14 brainstorm settled a tiered plan: **Tier 1** = scheduled-event calendar awareness (deterministic, backtestable — this study); **Tier 2** = event-outcome reaction (deferred: at the daily 00:05 UTC cadence the bar absorbs the print before the system can react); **Tier 3** = LLM/news sentiment (rejected: non-deterministic, breaks parity/audit reproducibility).

## 2. Baseline reproduction (gate before any variant work)

`research/crypto_perps/backtest.py` was run **unmodified** in a fresh venv (`uv venv .venv --python 3.11 && uv pip install -e ".[dev]"`; `validate_data.py` clean). The freshly produced `results.json` is **byte-identical to the committed one** (all 19 scenarios), and the headline production numbers match REPORT.md exactly: **+40.7 % CAGR, Sharpe 1.14, max DD 58.6 %, no halt**. The locked authority reproduces; variant work was allowed to proceed.

## 3. Variant engine and parity gate

`variant_backtest.py::run_backtest_filtered` is a verbatim copy of the locked engine's event loop with exactly one addition: an entry-window filter applied to the per-asset targets inside the decision block. Before any variant runs, a **parity gate** requires the filtered engine with *no events* to reproduce the locked engine's summary by dict equality, for both the `production` and `base` profiles. **Both gates are green** (and stay green when re-run — the suite is deterministic). Float arithmetic is retained deliberately: parity with the locked float engine is the correctness gate, and no live money flows through this study (the standing Decimal rule governs production code and money-handling scripts; `structlog` is used throughout the study scripts).

**Decision-time mapping (the load-bearing convention):** bar `ts` closes at 00:00 UTC on `ts+1` and the live decision fires at **00:05 UTC on `ts+1`**, so the filter tests events in `[decision_dt, decision_dt + N h]` with `decision_dt = ts + 1 day + 5 min`. This is exactly the incident geometry: the 2026-07-14 short was opened 12 h 25 min before that morning's CPI.

**Filter semantics (risk-reducing actions are never filtered):**
- *suppress*: fresh entries from flat → 0; the **entering leg** of a flip → 0 (the exit leg always executes); same-direction adds → capped at the current position.
- *half-size (m = 0.5)*: the risk-increasing component is halved, floored toward zero in whole contracts. Nano-scale caveat: a 1-contract entry halves to 0, i.e. degenerates to suppression at today's live size.
- Exits, reductions, stops, daily-loss flattens, and halts are untouched in every variant.

Event sets: **core** = CPI + NFP + FOMC decisions (322 instances); **extended** (sensitivity) = core + PCE + FOMC minutes (522). Windows N ∈ {12, 24, 48} h. Profiles: `production` (Amendment B — what live runs) plus a `base`-profile sensitivity run.

## 4. Calendar data provenance

**Source:** FMP `economics-calendar` API (via this environment's FMP MCP server — the same sanctioned vendor as the locked backtest's price data), pulled in 6-month chunks covering 2016-06-01 → 2026-07-07 and aggregated by `build_calendar.py` into `data/tier1_calendar.csv` (committed, 522 release instances, UTC).

**Why not BLS/Fed pages directly:** this environment's network policy blocks `bls.gov` and `federalreserve.gov` (HTTP 403 at the egress proxy). Provenance was instead anchored by spot checks, enforced as hard assertions in `build_calendar.py`: four CPI instances verified against BLS news-release archive URLs surfaced by web search (2025-02-12, 2025-03-12, 2025-04-10, 2026-02-13 — all 08:30 ET), the 2026-06-10 CPI, the 2017-02-03 NFP, and the publicly known 14:00 ET FOMC statement times for 2017-02-01 and 2022-06-15. Per-year counts land exactly on the official cadence (CPI 12/yr, NFP 12/yr, FOMC 8/yr) in every normal year.

**Point-in-time honesty (rules applied, all encoded in `build_calendar.py`):**
1. FMP carries **duplicate CPI/NFP rows** under a second naming family stamped with bogus early-morning UTC times; only the family at the true BLS release time (08:30 ET = 12:30/13:30 UTC, DST-correct) is kept. The kept CPI series covers all 121 months in range with zero gaps.
2. The 2024-08-21 "Non Farm Payrolls (Mar)" row is the QCEW **annual benchmark revision**, not the monthly print → dropped.
3. FOMC actions of **2020-03-03 and 2020-03-15 were unscheduled emergency cuts** → excluded (not knowable ex ante). The cancelled 2020-03-17/18 scheduled meeting is *not* re-added as a phantom event; one event in 9.5 years, immaterial either way.
4. Residual caveat: FMP timestamps are *actual* release datetimes. For CPI/NFP/FOMC these equal the *scheduled* datetimes in essentially all cases; the visible exceptions are the late-2025 government-shutdown reschedules (Sep-2025 CPI released 10-24; Nov-2025 CPI on 12-18; one NFP month missing in 2025). For those, the final announced schedule was still public days-to-weeks ahead — well outside the 12–48 h windows studied — so the ex-ante property holds for this use.

| year | CPI | NFP | FOMC dec. | FOMC min. | PCE |
|---|---|---|---|---|---|
| 2016 (Jun–) | 7 | 7 | 5 | 4 | 7 |
| 2017–2024 (each) | 12 | 12 | 7–8¹ | 8 | 11–12 |
| 2025 | 12 | 11² | 8 | 9 | 11² |
| 2026 (–Jul 7) | 6 | 7 | 4 | 3 | 7 |

¹ 2020 = 7 after excluding the two emergency actions (the March scheduled meeting was cancelled). ² Government-shutdown gaps (real, not data loss).

## 5. Results

Baseline (locked `production`): **+40.69 % CAGR · Sharpe 1.136 · max DD 58.64 % · total return +2,461 %** (2017-01-01 → 2026-06-30, $6,000 start).

### 5.1 Variant matrix (production profile)

| Variant | CAGR | ΔCAGR | Sharpe | Max DD | ΔDD | Affected decisions |
|---|---|---|---|---|---|---|
| core · suppress · 12 h | 40.69 % | ±0.00 | 1.136 | 58.64 % | ±0.00 | **0** |
| core · suppress · 24 h | 40.49 % | −0.20 | 1.135 | 59.15 % | **+0.51** | 164 |
| core · suppress · 48 h | 38.62 % | **−2.07** | 1.100 | 58.82 % | +0.18 | 354 |
| core · half-size · 12 h | 40.69 % | ±0.00 | 1.136 | 58.64 % | ±0.00 | **0** |
| core · half-size · 24 h | 40.84 % | +0.15 | 1.142 | 58.69 % | +0.05 | 165 |
| core · half-size · 48 h | 39.58 % | −1.11 | 1.118 | 58.57 % | −0.07 | 356 |
| ext · suppress · 24 h | 40.40 % | −0.29 | 1.134 | 59.21 % | +0.57 | 282 |
| ext · suppress · 48 h | 39.75 % | −0.94 | 1.132 | 58.76 % | +0.12 | 577 |
| ext · half-size · 24 h | 40.58 % | −0.11 | 1.137 | 58.69 % | +0.05 | 280 |
| ext · half-size · 48 h | 40.14 % | −0.55 | 1.136 | 58.20 % | −0.44 | 582 |

Base-profile sensitivity (core, 24 h): suppress +0.49 pt CAGR / Sharpe 1.066 vs 1.039; half-size +0.86 pt / 1.089 — mildly positive where production is mildly negative, i.e. **the sign of the effect is not even stable across profiles**, the signature of noise rather than edge.

Of the 164 affected decisions (core/24 h/suppress): **149 are same-direction adds** (rebalance top-ups the dead-band/band-edge machinery mostly re-absorbs later) and only **15 are fresh entries**; 124 BTC / 40 ETH.

### 5.2 Counterfactual: what did the would-be-suppressed entries actually do?

Position episodes (flat-to-flat, flip-split, cash-flow PnL excluding funding) reconstructed from the unfiltered production run: 214 episodes, 213 closed, **win rate 32.9 %, mean +$585, median −$496** — the classic trend-following shape (many small losses, few large wins pay for everything).

| Window (before core events) | Episodes opened in window | Win rate | Mean PnL | Median PnL | Sum PnL |
|---|---|---|---|---|---|
| 12 h | 0 | — | — | — | — |
| 24 h | 15 | 26.7 % | −$870 | −$947 | −$13,046 |
| 48 h | 32 | 34.4 % | −$346 | −$599 | −$11,082 |
| ext 24 h | 27 | 29.6 % | −$626 | −$765 | −$16,901 |
| ext 48 h | 50 | 32.0 % | +$6 | −$599 | +$277 |

In-window entries did run modestly worse than the 32.9 % unconditional rate at 24 h — but **n = 15**, and the deficit is one or two big winners away from vanishing (the ext/48 h cell already flips positive on sum-PnL). And crucially, the full-path suppress-24h variant recovers almost none of that −$13 k: **suppression mostly defers an entry the trend re-takes at the next decision**, at a slightly different price, so the "avoided" losses largely come back through re-entry. Win rate is also the wrong success metric for a trend system — cutting entries cuts lottery tickets, and the tickets are the edge.

### 5.3 Findings

- **F-1 (structural): a 12 h window is empty.** The 00:05 UTC decision instant sits 12 h 25 min before a 12:30 UTC print (08:30 ET, DST summer) and 13 h 25 min in winter; FOMC statements are ~18 h out. **No tier-1 US release occurs within 12 h of the decision** — a 12 h filter would not have caught the 2026-07-14 CPI that motivated this workstream. 24 h is the minimum meaningful window at this cadence; it catches every same-UTC-day event.
- **F-2: 24 h filters are a wash.** ΔCAGR −0.20/+0.15 pt on a +40.7 % CAGR path, ΔSharpe ≤ 0.006, and max DD moves the *wrong* way for suppress (+0.51 pt). Nothing here clears path noise (the fine-scale sweep in the reference REPORT.md treats ~±2 pt CAGR as one path-noise step).
- **F-3: 48 h filters clearly cost.** −0.9 to −2.1 pt CAGR with no compensating DD relief: at ~34 core events/yr, 48 h windows blank out ~25 % of decision days and the strategy starts missing trend on-ramps.
- **F-4: no drawdown protection anywhere.** The strategy's big drawdowns are trend reversals and chop regimes, not event-day gaps; per-trade risk is already stop-capped (§7 risk cap), so the event filter has no DD channel to work through. The one cell with a visible DD improvement (ext/half/48 h, −0.44 pt) costs −0.55 pt CAGR and is not corroborated by its neighbors.
- **F-5: the overlap sample is tiny and this study is directional.** 15 fresh entries in 9.5 years at 24 h. Framed as evidence, this can neither justify nor damn a filter; it CAN justify not hard-coding one now.

## 6. Limitations (honest list)

1. **Daily bars cannot see intraday event reactions** — the bar absorbs the print; what a filter would have saved intraday is invisible. This systematically *understates* any true event-risk both ways (avoided losses AND missed post-print momentum).
2. **Calendar timestamps are actual, not archived-ex-ante, schedules** (§4 caveat) — equal for this venue/window size, but a production import should carry the ratification fields the Phase-0 `macro_events` schema already has.
3. **Parametric funding** (locked engine limitation) — event-window funding spikes aren't modeled.
4. **Path-dependence** — affected-decision counts are measured on the variant's own path; the counterfactual episode table is measured on the baseline path. Both stated as such.
5. **Episode PnL excludes funding** (small at 0.01 %/8 h over multi-day holds; sign-neutral across long/short mix).
6. **One history** — 9.5 years, one regime sequence, BTC-dominant (ETH excluded by the $2k rule most of the sample).

## 7. Reproduce

```bash
uv venv .venv --python 3.11 && uv pip install -e ".[dev]"
.venv/bin/python research/crypto_perps/validate_data.py     # data sanity
.venv/bin/python research/crypto_perps/backtest.py          # locked baseline (must match committed results.json)
.venv/bin/python research/macro_events/variant_backtest.py  # parity gate + variant suite -> results_variants.json
# calendar rebuild (needs raw FMP economics-calendar dumps):
# .venv/bin/python research/macro_events/build_calendar.py --src '<glob of FMP dumps>'
```

## 8. DECISION PROMPT (operator decides; this report only recommends)

Standing constraints first: **no live-behavior change may even be proposed for inside the C1 ≥45-day §10 gate window** (started 2026-07-10) — any filter adoption is post-gate at the earliest; strategy/risk changes are operator-escalation domain; the Phase-0 `macro_events` table (alembic 0004) already exists and is unconsumed.

**Option (i) — wire Tier-1 observation-only (RECOMMENDED).**
Import ceremony (`scripts/operator_tools/import_macro_events.py`, CSV → `macro_events`, dry-run default) + decision-row annotation (worker logs upcoming-window events alongside each 00:05 decision) + a `/cycle` digest warning line ("⚠ CPI in 12 h"). **No behavior change of any kind.** What it buys: the operator is never again surprised by a known print; every future decision row carries the event context needed to judge a filter on *live* evidence; Tier-2 stays honestly evaluable. Build size: **~2 PRs, 1–2 sessions.** The digest line + import tool are non-A02 (`services/discord_shared/**`, `scripts/**`); the decision-row annotation touches `services/signal/**` → **one A02 PR with risk review**. No migration needed if imported rows use the existing `source='manual'` CHECK value (adding `'fmp'` to the CHECK would be an `alembic/**` A02 migration — not worth it for observation-only).

**Option (ii) — observation + queue a filter-policy amendment for post-gate consideration.**
Everything in (i) plus a drafted amendment to `Docs/crypto-perps-strategy.md` (24 h/half-size is the least-bad shape found) queued for post-gate decision. Build size: (i) + ~1 session of docs now, +1–2 A02 sessions later if adopted. **The data does not currently support this**: the filter's best cell is +0.15 pt CAGR on a 15-trade overlap sample, it protects nothing on drawdown, and its sign flips between profiles. Queuing it now would put a change with no measured benefit in line for the risk surface.

**Option (iii) — drop the workstream.**
Zero build. Defensible on the backtest evidence alone — but it leaves the actual 07-14 failure (operator-visible blindness to a known event) unaddressed, and leaves Tier-2 permanently unevaluable for lack of an annotated live record.

**Recommendation: (i).** The backtest says the *filter* isn't worth a risk-surface change (so not (ii)); the incident says the *blindness* is real and cheap to fix (so not (iii)). Observation-only is deterministic, auditable, parity-neutral, and produces exactly the live evidence a post-gate filter decision would need. Revisit (ii) only if the annotated live record ever shows event-window entries failing at a rate the backtest could not see.
