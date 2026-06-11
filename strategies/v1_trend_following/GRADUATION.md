# V1 graduation ledger

Append-only record of parameter sets that traversed the research → governance
bridge (design D6; ceremony: `Docs/graduation-runbook.md`). A row in this file
means: the candidate was backtested by the AUTHORITATIVE harness (the real
`lean/v1_strategy.py` in an isolated LEAN container, Stage 0-5 sized, explicit
costs), its delta against the production baseline was reviewed, and the PR
carrying it received the operator-applied `risk-review-approved` label.

This file is documentation only — it is never imported and has zero runtime
effect. The runtime source of truth for active parameters is the
`parameter_sets` DB head pointer; `V1_DEFAULTS` (parameters.py) is the seed /
fallback. A graduation that changes parameter VALUES must update BOTH (the
runbook covers the DB ceremony).

---

## 2026-06-11 — Re-affirmation of launch defaults (graduation pipeline dry-run, charter PR E)

**Candidate:** `V1_DEFAULTS` exactly as shipped — a deliberately ZERO-DELTA
candidate chosen to exercise the pipeline end-to-end without a strategy-logic
decision (charter PR E; the operator owns all strategy-logic changes).

| Parameter | Production | Candidate | Δ |
|---|---|---|---|
| LOOKBACK_DAYS_DONCHIAN | 60 | 60 | — |
| MA_FAST_DAYS | 50 | 50 | — |
| MA_SLOW_DAYS | 200 | 200 | — |
| EFFICIENCY_RATIO_THRESHOLD | 0.20 | 0.20 | — |
| STOP_DISTANCE_ATR_MULT | 3.0 | 3.0 | — |
| ATR_LOOKBACK_DAYS (LOCKED) | 20 | 20 | — |
| MIN_HOLDING_DAYS (LOCKED) | 14 | 14 | — |
| VOL_TARGET_PCT_ANNUAL | 0.15 | 0.15 | — |
| INSTRUMENT_VOL_LOOKBACK_DAYS | 60 | 60 | — |
| ROLL_DAYS_BEFORE_EXPIRY | 5 | 5 | — |
| STRATEGY_DECOMMISSIONED (OPERATOR-ONLY) | False | False | — |
| EXIT_AUTO_APPROVE | False | False | — |

**Authoritative backtest (candidate == baseline, so one run serves both; delta
= 0 by construction):** real engine, isolated container, 2023-09-01 →
2026-06-08, 1013 bars — **85 fills · 40 closed trades · +3.42% total
($100k → $103,420.76) · Sharpe 0.14 · realized vol 9.1% · max-DD 11.61% ·
0 margin events · no liquidation · total fees $223.03** (explicit IBKR cost
model + 1-tick slippage, PR #339; per-fill fee census matches the cost table).
Run artifact: `research/runs/lean_20260611T042002Z` (operator box);
numbers recorded in `research/lean/README.md` "Authoritative V1 P&L".

**Validity / multiple-testing:** no parameter search was performed (single
re-affirmation candidate, no sweep) — selection bias is structurally absent.
A REAL candidate must run the P4 walk-forward validity suite (#332) and report
the OOS-ranked result + multiple-testing haircut here.

**Trust bridge at graduation time (PR #340, measured 2026-06-11):**
date+market agreement 4/5 live-flagged markets (exact 95% CI [0.28, 0.99]);
side-verified strict 2/10 (the TLT side-flip traces to bar revision); ER-aligned
regime window 1/1; zero unexplained residual after attribution.

**Ruin / hard stops:** 0 margin events, no liquidation, no ruin banner — the
hard-stop conditions did not fire.

**Outcome:** ☐ operator review → ☐ `risk-review-approved` (operator-applied)
→ ☐ merge. (Checked off by the operator at merge; this dry-run row proves the
path — the first VALUE-changing graduation appends the next row.)
