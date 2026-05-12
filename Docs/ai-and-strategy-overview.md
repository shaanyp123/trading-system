# AI and Trading Strategy Overview

> **Plain-English companion to the technical specs.** This document explains, in finance terms, how the trading bot uses AI and exactly what rules govern its trading. It is **not** the canonical source of truth — those are `backend-spec.md`, `frontend-spec.md`, and `claude-dev-guide.md`. When this doc and the specs disagree, the specs win and this doc gets updated.
>
> **Audience:** the operator (a non-coding finance professional running this system solo) and outside readers — friends, family, and prospective allocators (prop firms, F&F principals) doing due diligence.
>
> **Last refreshed:** 2026-05-06 (Day 3 of Phase 0).

---

## Table of Contents

1. [What this system is](#1-what-this-system-is)
2. [How AI is used](#2-how-ai-is-used)
3. [The trading strategy — V1 Trend-Following](#3-the-trading-strategy--v1-trend-following)
4. [Position sizing (Stages 0–5)](#4-position-sizing-stages-05)
5. [Risk envelope and circuit breakers](#5-risk-envelope-and-circuit-breakers)
6. [Calibration, reconciliation, and the macro calendar](#6-calibration-reconciliation-and-the-macro-calendar)
7. [Parameter reference](#7-parameter-reference)
8. [The phase roadmap (Phase 0 → Phase 3)](#8-the-phase-roadmap-phase-0--phase-3)
9. [What changes vs. what stays locked across phases](#9-what-changes-vs-what-stays-locked-across-phases)
10. [Glossary](#10-glossary)

---

## 1. What this system is

A solo-operator algorithmic trading system. One person — the operator — funds, monitors, and is legally responsible for the account. AI does most of the engineering work and assists at runtime; the operator gates every risk-affecting change and holds the only set of broker credentials.

The trading edge being pursued: **systematic medium-term trend following** on a small basket of CME micro futures and US Treasury ETFs. The thesis is the standard Clenow-style trend-following bet — that persistent price moves on liquid, diversified markets, sized by inverse-volatility and capped per cluster, produce a long-run risk-adjusted return that survives transaction costs and the occasional whipsaw.

**Capital path:** start at $15–25k IBKR Pro live; scale to $50k+ as the track record accumulates; open the door to F&F ($250k cap) and prop-firm allocations once two-plus quarters of clean live performance exist.

**Sharpe targets** (rolling, live, after costs):

| Phase | Target | Floor |
|---|---|---|
| Phase 1 — first 3 months live | ≥ 0.8 | decommission below 0 over 30 days |
| Phase 2 — own infra | ≥ 1.2 | review below 0.8 for 60+ days |
| Phase 3 — capital scaling | ≥ 1.5 | review if missed by > 0.3 for 90+ days |

**Max drawdown ceiling:** 15% portfolio. Hitting 25% triggers the decommission floor (the strategy is shut off, not just paused).

---

## 2. How AI is used

There are **two distinct AI roles** in this project. They are intentionally separated — different jobs, different authority, different guardrails.

### 2.1 Claude Code — the dev-time AI

Claude Code is what writes the code. The operator is non-coding by background, so every line of Python (and the entire web frontend, Postgres schema, Docker setup, deploy automation, etc.) is authored by Claude Code under operator review.

**How it works:**

- The operator opens one Claude Code session per task and points it at the week's deliverable in `implementation-guide.md`.
- Claude Code drafts the code, tests it locally, and opens a pull request (PR).
- The operator reviews via an in-app **PR Review Surface** (see §2.3 below) — plain-English summary + risk impact + backtest delta + test results — and either approves or rejects.
- CI mechanically blocks merges that change "forbidden" code paths (anything in the trading hot path: risk engine, signal engine, audit log, execution, strategy logic, database migrations) unless the operator has applied the `risk-review-approved` label after reviewing.
- Two whitelists govern what changes need a PR vs. what can ship as a hot-fix:

| Path category | Examples | Change path |
|---|---|---|
| **Forbidden whitelist** (PR-required, label-gated) | `services/risk/`, `services/signal/`, `services/audit/`, `services/execution/`, `services/agent/decisions/`, all DB migrations | Operator must apply `risk-review-approved` label after review; CI blocks merge otherwise |
| **Hot-fix whitelist** (auto-deployable, monitored) | logging, retry helpers, monitoring, agent reporting templates, the Discord bot's response formatting | Auto-deploy with 30-min watch window; auto-rollback on metric breach |
| Everything else | Frontend changes, FastAPI scaffolding, infra | Standard PR, normal merge |

This split exists so that "AI changes the code" never becomes "AI silently changes how risk is sized or how stops are calculated." Strategy code goes through the operator. Plumbing fixes ship faster.

### 2.2 Claude Ops Agent — the runtime AI

Once the system is live, a second AI runs alongside it as an operational co-pilot. This is the **Claude Ops Agent**. It runs in the production VPS, queries the database (read-only), and uses a bounded set of tools to act on the operator's behalf.

**What it does day-to-day:**

| Trigger | Action |
|---|---|
| 08:00 ET daily | Generates a morning briefing — overnight P&L, current positions, signal queue, alerts, cost run-rate |
| Mondays 08:00 ET | Weekly summary — trades, attribution, parameter status, drift vs. backtest |
| 1st of month 09:00 ET | Monthly cost report (Anthropic API + Hetzner + QC + GitHub + Resend + S3) |
| Kill-switch fires | Drafts an incident summary with the audit-log evidence trail |
| Slippage outlier or vol regime anomaly | Renders context for the operator's decision (no autonomous action) |
| 7-day HALT_NEW dwell | Daily reminder briefing |
| Operator types `/ask <question>` in Discord or web | Free-text Q&A using read-only DB context |

**What it can change autonomously** (always with full audit, always reversible):

| Authority | Direction allowed | Example |
|---|---|---|
| Tighten a strategy parameter | One direction only — toward less risk | Raising the Donchian lookback from 60 → 70 days (longer = stricter) |
| Defensive position trim | Reduce exposure mid-session | Cap is −30% gross across an entire session; cannot increase positions |
| Invoke the kill switch | Stop new entries | Mirrors what the risk engine does autonomously; agent path is belt-and-suspenders |
| Hot-fix to whitelisted paths | Within hot-fix whitelist only | E.g., adjust a log line, retry interval — never strategy logic |

**What it cannot do — ever:**

| Hard-blocked action | Mechanism |
|---|---|
| Place an order directly | The `place_order` tool literally does not exist in its tool inventory. The agent has zero broker credentials. |
| Loosen a parameter (raise risk) | Refused by the tool wrapper before the request even reaches the model |
| Resume after a kill switch | Re-authentication is required and is web-only — agent has no session |
| Change strategy logic, risk math, or audit code | Files live in the forbidden whitelist; agent must instead `draft_pr` for the operator to review |
| Modify the audit log | Append-only at the database level; even the database superuser cannot UPDATE/DELETE/TRUNCATE without a "break-glass" role |

**Auto-revert.** Any parameter change the agent makes auto-reverts if any of these fire within 30 trading sessions:

- 30-day rolling portfolio Sharpe drops > 2 standard deviations from the pre-change baseline (with ≥ 30 portfolio-wide trades in the window)
- Portfolio max drawdown breaches −10% within 5 CME sessions
- 5+ consecutive losing trades portfolio-wide

After an auto-revert: parameter restored, full audit, alert, no further auto-changes to that parameter for 14 days.

**Cost budget.** $30–100/mo soft target on Anthropic API spend. Soft alert at $200/mo (cumulative across all providers). Hard ceiling at $300/mo triggers a "cost-review" state — investigation only, not a trading halt. (Cost is operational, not safety.)

### 2.3 The PR Review Surface

When either Claude Code (dev-time) or the Claude Ops Agent (runtime) wants to change strategy logic or a risk parameter outside its auto-action surface, it drafts a PR. The operator sees it in a purpose-built review screen (`/system/prs/:id`) that shows:

- **Plain-English summary** (≤ 200 words, agent-written) — *why* the change is being proposed
- **Risk impact** — direction (TIGHTEN / LOOSEN), within-range check, auto-revert thresholds, affected markets
- **Backtest delta** — Sharpe before/after, max DD before/after, trade count, equity curve, ten worst-divergence trades. Run on LEAN against the locked slippage calibration version (so the comparison is apples-to-apples).
- **Test results** — unit, integration, lint, type-check, secret scan
- **Diff** (collapsed by default — the operator gates on the summary, not the code)

The operator clicks Approve, Request Changes, or Reject. Approve requires re-authentication (WebAuthn or TOTP) for any risk-affecting PR; the merge is logged immutably to the audit log alongside the operator's session ID and re-auth timestamp.

---

## 3. The trading strategy — V1 Trend-Following

### 3.1 In plain English

Buy a market when (a) it has just broken above its 60-day high, (b) the 50-day moving average is above the 200-day moving average (uptrend confirmed), and (c) the price series has shown persistent (not mean-reverting) behaviour over the lookback window. Sell short on the symmetric short side. Size each position by inverse volatility, scaled to a portfolio-level annualized vol target. Hold until the price hits an ATR-based stop, the trend reverses, or the strategy is decommissioned. No profit target — let winners run.

This is a **classic medium-term trend follower**, closely modelled on Andreas Clenow's *Following the Trend* and *Stocks on the Move*. Nothing exotic. The edge — to the extent there is one — comes from execution discipline, position sizing, and risk control, not from the entry rule.

### 3.2 Markets traded

The Phase 1 candidate universe is **locked** at 11 markets — small enough to fit a $15–25k starting account, diversified enough to harvest trend across asset classes:

**CME Micro Futures** — small contract sizes that let a $15–25k account take meaningful positions:

| Symbol | Market | Notional per contract (approx.) |
|---|---|---|
| /MES | E-mini S&P 500 Micro | ~$26k @ 5,235 |
| /MNQ | E-mini Nasdaq-100 Micro | ~$36k @ 18,000 |
| /MYM | E-mini Dow Micro | ~$20k @ 40,000 |
| /M2K | E-mini Russell 2000 Micro | ~$10k @ 2,000 |
| /MGC | Gold Micro | ~$24k @ $2,400/oz |
| /MCL | WTI Crude Micro | ~$8k @ $80/bbl |
| /MBT | Bitcoin Micro | ~$10k @ $100k |

**NYSE Bond ETFs** — cash equity (no futures roll), 4-point Treasury curve coverage:

| Symbol | Market |
|---|---|
| TLT | 20+ Year Treasury (long duration) |
| IEF | 7–10 Year Treasury (intermediate) |
| SHY | 1–3 Year Treasury (short) |
| TIP | TIPS (inflation-linked) |

**At a $15–25k starting account**, the universe filter (Stage 0, see §4) likely excludes /MES, /MNQ, /MYM, and /MGC because a single contract exceeds 50% of equity. Realistic active universe at start: **the 4 bond ETFs + /MCL + /M2K + possibly /MBT**. As equity grows, more markets unlock automatically — the universe expansion is dynamic, not manually managed.

The universe is **closed for Phase 1**. Adding a market requires a PR (forbidden whitelist) and operator approval. New markets bring new data quality risks, new contract roll mechanics, and new correlation considerations — they are not free additions.

### 3.3 Entry rules

A long entry fires when **all three** conditions are met on the daily bar at 17:30 ET:

1. **Donchian breakout** — today's close prints above the trailing `LOOKBACK_DAYS_DONCHIAN`-day high (default 60 days)
2. **Trend filter** — `close > MA_FAST` AND `MA_FAST > MA_SLOW` (defaults: 50-day fast, 200-day slow)
3. **Persistence filter** — Hurst exponent over the same lookback ≥ `HURST_THRESHOLD` (default 0.55)

Short entries are the symmetric mirror (close below trailing low, MA_FAST < MA_SLOW, same Hurst threshold).

**Hurst exponent** is a measure of long-range dependence in a price series. H = 0.5 is a random walk; H > 0.5 indicates persistence (a trending series); H < 0.5 indicates mean reversion. The R/S (rescaled-range) estimator is used. The 0.55 threshold is calibrated to the small-sample upward bias of R/S on a 60-bar window (the estimator typically inflates H by ~0.05 on short series, so 0.55 is what 0.50 buys you on a clean signal).

**Signal cycle timing.** Signals are computed at 17:30 ET on every CME session. If settlement prices aren't available yet, the signal engine retries every 5 minutes; at 18:00 ET it falls back to the bid/ask midpoint and tags the signal `unsettled`; at 18:30 ET it drops the signal for that market and continues with the others.

**Order placement is queued, not immediate.** Futures orders go in at the next CME session start (~18:00 ET same evening after maintenance pause). ETF orders go in at the next NYSE open (09:30 ET). If the macro calendar puts a tier-1 event in the placement window (FOMC, CPI, NFP, etc.), placement is paused 5 min before through 30 min after — and if that pause plus 60 minutes of staleness exceeds the session, the signal is dropped (`macro_window_drop`) rather than placed late.

### 3.4 Exit rules

There is **no profit target**. Trends end when they end. Exits fire when **any of the following** are true:

1. **Stop hit** — stop-market exit at `entry_price ± STOP_DISTANCE_ATR_MULT × ATR(20)` (defaults: 3.0 × the 20-day Average True Range)
2. **Trend filter flips** AND the position has been held at least `MIN_HOLDING_DAYS` (default 14)
3. **Signal reversal** — a fresh entry in the opposite direction
4. **Strategy decommission** — the operator or risk engine pulls the plug

The `MIN_HOLDING_DAYS` floor exists so a trend filter that flips on day 2 doesn't whipsaw the system out before the trade has had time to develop. It is **locked** — agent cannot change it; only a PR can.

### 3.5 Contract rolls

Futures contracts expire. The system rolls open futures positions `ROLL_DAYS_BEFORE_EXPIRY` (default 5) calendar days before the front-month contract expires, using a calendar spread (when the broker supports it) or a sequential close-and-reopen otherwise. The roll happens during normal session hours, not at signal time, and is excluded from P&L attribution as roll cost (tracked separately in `cost_events`).

---

## 4. Position sizing (Stages 0–5)

Sizing is a deterministic 5-stage algorithm. Every signal that survives the entry rules above goes through it. The full intermediate trace is persisted to the database for every signal — there is no "we don't know why we sized it that way" outcome.

### Stage 0 — Universe filter

**Rule:** drop any market where one contract's notional value exceeds 50% of current equity.

This is what makes the universe dynamic. At $15k equity, /MES (~$26k notional per contract) is excluded. At $52k equity it qualifies. A market becoming eligible triggers a `universe_inclusion` audit event and an SSE notification. Existing positions in a now-excluded market continue to be managed; only **new** entries are blocked.

### Stage 1 — Inverse-volatility weighting

**Rule:** weight markets inversely to their realized volatility (less volatile = bigger weight), then scale the portfolio to a target annualized vol.

```
weight_i ∝ 1 / σ_i
portfolio_vol = √(wᵀ Σ w)         # using full covariance matrix Σ
scale = vol_target_daily / portfolio_vol
unconstrained_notional_i = weight_i × scale × equity
```

The portfolio vol target is `VOL_TARGET_PCT_ANNUAL` (default 15%) divided by √252 to get a daily target, multiplied by `m_combined` (the active vol multiplier composition — see §5.3).

The covariance matrix Σ is rebuilt over a rolling 60-day window. Before use it is run through a **PSD repair** (any negative eigenvalues clipped to zero, then reconstructed) so the matrix is always positive semi-definite. PSD repairs are audited.

### Stage 2 — Per-position cap

**Rule:** no single position exceeds 25% of equity in notional. Markets where a single contract is between 25%–50% of equity (i.e., one-contract-or-nothing sizing) get a hard floor at 50%.

### Stage 3 — Cluster shrink

Markets are grouped into **clusters** (equity index, rates, commodity, crypto). If any cluster's combined notional exceeds its cap, all positions in that cluster scale down proportionally. Iterates up to 10 times to convergence (0.1% tolerance). On non-convergence, the lowest-momentum signal in the binding cluster is dropped and the algorithm restarts.

### Stage 4 — Gross / Net exposure caps

**Rules:**
- Gross exposure (sum of |notionals|) ≤ 3.0 × equity
- Net exposure (signed sum) ≤ 1.5 × equity

If either is breached, all positions scale down proportionally.

### Stage 5 — Lot rounding

Notionals divided by per-contract notional give fractional contract counts. Round to integer contracts using banker's rounding. Any signal sizing < 0.5 contracts is **dropped as sub-minimum** — recorded but not traded, and excluded from the signal acceptance rate denominator.

The fully-sized output, with every intermediate stage's inputs and outputs, is persisted as `sizing_trace` JSON on every `signals` row. If a sizing decision is ever questioned in the future — by the operator, by a prop firm doing due diligence, or by a regulator — the full provenance is reproducible.

---

## 5. Risk envelope and circuit breakers

### 5.1 Risk rings

The risk engine evaluates a set of "risk rings" continuously:

- **At signal-emit time** — before any order is queued
- **Every 60 seconds during the CME session** — using live mark-to-market
- **On every fill** — immediate post-fill check

Any ring breach triggers the kill switch.

### 5.2 The kill-switch state machine

The system runs in one of three states:

| State | Behavior |
|---|---|
| **NORMAL** | Standard operation — entries, exits, sizing all live |
| **HALT_NEW** | No new entries. Existing positions hold. Exits continue (stops, exit signals, manual close). Three sub-flavors: `routine`, `defensive_envelope`, `incident_review` (escalating severity) |
| **CONVALESCENT** | Entries permitted but at half size (vol multiplier × 0.5). Auto-promotes to NORMAL after 5 clean CME sessions. Any new trigger sends it back to HALT_NEW. |

Triggers that move NORMAL → HALT_NEW (routine):

- Trailing drawdown breach
- Daily loss breach
- Signal storm (anomalous burst)
- Reconciliation mismatch with the broker
- Broker disconnect > 5 min
- Vol regime z-score > 2 (sudden volatility spike)
- Cross-market correlation > 0.85 (diversification has collapsed)
- Unhandled exception in the trading hot path
- Calendar unratified by 23:00 ET cutoff

Triggers that move NORMAL → HALT_NEW (defensive_envelope) — these are comms-breakdown failures, not market events:

- Operator engagement heartbeat fails (you stop responding to alerts for too long)
- QC ObjectStore unavailable > 10 min
- Both watchdog and Discord delivery fail simultaneously

Triggers that move NORMAL → HALT_NEW (incident_review) — these are integrity failures:

- Audit log write fails after 5 retries
- Hash chain integrity break detected
- Decommission floor triggered (Sharpe / DD / underperformance)

**Resume from HALT_NEW is human-only** and requires re-authentication on the web UI. Discord cannot resume the system. The agent cannot resume the system. This is intentional — re-entry after a halt is the highest-stakes click in the entire system, and it is firewalled accordingly.

### 5.3 Vol-target multiplier composition

The portfolio vol target is multiplied by `m_combined`, which is the **MIN** (not the product) of any active multipliers:

| Multiplier | Value | Triggered by |
|---|---|---|
| `m_capital_event` | 0.5 (sessions 1–5), 1.0 (sessions 6–30) | Recent deposit or withdrawal — equity scale just changed, sizing model needs to recalibrate |
| `m_convalescent` | 0.5 | While in CONVALESCENT state |
| `m_monthly_dd` | 0.5 | When the calendar-month drawdown is worse than −10% |
| ceiling | 1.0 | Always present |

Why MIN, not product? Because compounding two 0.5× multipliers gives 0.25× (75% smaller positions), which is over-conservative — they are addressing the same underlying concern (recent loss / regime uncertainty / scale mismatch) and the worst single one is the binding constraint, not all of them stacked.

**Asymmetry on capital events.** A deposit triggers the multiplier and resets the drawdown clock. A withdrawal triggers the multiplier but does **not** reset the drawdown clock — pulling money out shouldn't paper over an existing drawdown.

### 5.4 Margin protocol (graduated de-leverage)

Every 60s during the session, the risk engine checks IBKR's `used_margin / available_margin` ratio:

| Ratio | Action |
|---|---|
| < 70% | Continue normally |
| 70–85% | Warn alert |
| > 85% | Rank open positions ascending by momentum z-score (tie-break: largest absolute margin contribution); cut the weakest via marketable-limit order (1× spread retry → 2× spread on rejection) |
| Sweep > 30% gross in one session | Escalate to HALT_NEW (routine) |
| Still > 80% after sweep | Escalate to HALT_NEW (routine), no further trims, alert that IBKR may force-liquidate |

The point is to **shed the weakest positions first**, not blindly de-lever everything. Margin pressure usually means one or two positions are blowing out; cutting them releases the binding margin without sacrificing the rest of the book.

### 5.5 Decommission floor

The strategy is **shut off** (not paused) if any of the following trigger:

- Live 30-day Sharpe < 0
- Live max drawdown ≤ −25%
- 60-day Sharpe underperforms backtest by > 2 standard deviations

This is the "the strategy doesn't work" floor, distinct from the "we hit an operational issue" halts above. Resuming requires a written incident review and a fresh paper-trading validation cycle.

---

## 6. Calibration, reconciliation, and the macro calendar

### 6.1 Slippage calibration

Slippage is modelled per market as a linear function of order size relative to liquidity:

```
slippage_bps_market = α_market + β_market × (order_size / ADV_30d)
```

Coefficients are estimated by OLS on realized fills versus expected fill prices.

- **Phase 1:** monthly recalibration cron (1st of the month, 22:00 ET)
- **Phase 2:** quarterly recalibration
- **Bootstrap:** the first 30 days of live fills run with α = 0, β = 0 (zero-slippage prior). LEAN's built-in slippage model is also disabled in the bootstrap backtest run so that the comparison baseline is clean.
- **Calibration history is event-sourced** — every recalibration creates a new row in `slippage_calibration_versions`; the HEAD pointer is what's used by the live system. Past versions are immutable.
- **Trigger for unscheduled review:** if realized slippage exceeds modelled by 2× for any single market for 3 consecutive months, the operator (not the system) opens a strategy review.

### 6.2 Reconciliation

Every 60 seconds during the CME session, the reconciliation service compares the system's understanding of state — positions, balances, P&L — against the broker's authoritative state (IBKR's portfolio snapshot). At 18:30 ET daily, a full end-of-day reconciliation runs against the IBKR FlexQuery XML.

Tolerances are tight (per the Reconciliation Tolerances Table in the backend spec). Any breach trigger HALT_NEW (routine) until the operator investigates. Reconciliation staleness > 60s during the CME session also halts new entries — staleness means "we don't know what we own right now," which is a worse state than "we know we own X and the market is moving against us."

Tolerances **widen 2× for 24 hours** on dividend ex-dates (anchored to 17:00 ET MTM), because dividend cash flows don't always hit on the expected wire timing.

### 6.3 Macro calendar

A nightly cron at 22:00 ET pulls Forex Factory (primary) and Trading Economics (secondary) and writes tier-1 events (FOMC, CPI, NFP, GDP, PCE, ECB/BOJ/BOE, OPEC) to the `macro_events` table.

The operator must **ratify** the next-day's events via Discord (or web in Phase 2) by 23:00 ET. Ratification means: "I have read the calendar; I am awake to what is happening tomorrow." Without ratification, the system halts new entries at 23:00 ET (`reason=calendar_unratified`). The exception is **vacation mode** — the operator can pre-declare a vacation window during which the ratification gate is suspended.

Order placement (not signal generation) pauses 5 min before through 30 min after every tier-1 event. Signals are still computed; they're just held until the macro window clears.

---

## 7. Parameter reference

### 7.1 Strategy parameters (V1 Trend-Following)

These live in `strategies/v1_trend_following/parameters.py` and are persisted as a hashed `parameter_set` row in the database. Every signal carries the parameter set hash that produced it — so historical signals can always be reproduced exactly.

| Parameter | Default | Range | Tighten direction | Who can change |
|---|---|---|---|---|
| `LOOKBACK_DAYS_DONCHIAN` | 60 | [20, 252] | up (longer = stricter) | **Agent** (within range, tighten only) |
| `MA_FAST_DAYS` | 50 | [5, MA_SLOW_DAYS) | up (slower = fewer crossovers) | **Agent** |
| `MA_SLOW_DAYS` | 200 | (MA_FAST_DAYS, 400] | up (slower = stricter trend) | **Agent** |
| `HURST_THRESHOLD` | 0.55 | [0.40, 0.80] | up (more selective) | **Agent** above 0.50; **PR** to go below 0.50 |
| `STOP_DISTANCE_ATR_MULT` | 3.0 | [1.0, 6.0] | down (tighter stop) | **Agent** |
| `ATR_LOOKBACK_DAYS` | 20 | [5, 60] | n/a | **PR-only — locked** |
| `MIN_HOLDING_DAYS` | 14 | [1, 60] | n/a | **PR-only — locked** |
| `VOL_TARGET_PCT_ANNUAL` | 0.15 (15%) | [0.05, 0.40] | down (lower = smaller positions) | **Agent** |
| `INSTRUMENT_VOL_LOOKBACK_DAYS` | 60 | [20, 252] | n/a (no clear tighten direction) | **Agent** |
| `ROLL_DAYS_BEFORE_EXPIRY` | 5 | [1, 21] | up (roll earlier = more conservative) | **Agent** |

"Tighten direction" is the direction that **reduces risk**. The agent is hard-blocked from moving any parameter in the loosening direction at the tool layer — not as a prompt instruction, as actual code. Loosening always requires a PR.

### 7.2 Portfolio sizing parameters (locked by spec)

| Parameter | Value | Meaning |
|---|---|---|
| Universe notional cap | 50% of equity | Stage 0 — single-contract notional ceiling |
| Per-position target cap | 25% of equity | Stage 2 — soft cap |
| Per-position hard floor | 50% of equity | Stage 2 — for one-contract-or-nothing markets |
| Gross exposure cap | 3.0 × equity | Stage 4 |
| Net exposure cap | 1.5 × equity | Stage 4 |
| Sub-minimum threshold | 0.5 contracts | Stage 5 — drop below this |
| Cluster max iterations | 10 | Stage 3 convergence |
| Cluster tolerance | 0.1% | Stage 3 convergence |

These are **all locked**. Changing them is a strategy-logic change, not a parameter tweak — full PR with backtest delta required.

### 7.3 Risk envelope parameters (locked by spec)

| Parameter | Value | Meaning |
|---|---|---|
| `m_capital_event` (sessions 1–5) | 0.5 | Half size after a deposit/withdrawal |
| `m_capital_event` (sessions 6–30) | 1.0 | Mode active but multiplier normalized |
| `m_convalescent` | 0.5 | Half size while convalescent |
| `m_monthly_dd` threshold | −10% | Triggers half-size when calendar-month DD breaches |
| CONVALESCENT clean-session count | 5 | Sessions without a trigger before promotion to NORMAL |
| HALT_NEW dwell reminder | 7 trading days | Daily reminder kicks in (no auto-flatten) |
| Margin warn | > 70% | Alert |
| Margin trim trigger | > 85% | Start cutting weakest positions |
| Margin sweep cap | 30% gross / session | Cap on how much can be cut in a session before HALT |
| Margin escalate | > 80% post-sweep | HALT, no further trims |
| Decommission Sharpe floor | < 0 (30-day live) | Strategy off |
| Decommission DD floor | ≤ −25% | Strategy off |
| Decommission underperformance | 60-day live − backtest > 2 SD | Strategy off |
| Auto-revert DD breach | −10% within 5 sessions | Reverts agent param change |
| Auto-revert Sharpe drop | > 2 SD over 30 sessions, ≥ 30 trades | Reverts agent param change |
| Auto-revert losing streak | 5+ consecutive | Reverts agent param change |
| Cooldown after auto-revert | 14 days | No further auto-changes to that parameter |

### 7.4 Operational thresholds (locked by spec)

| Parameter | Value |
|---|---|
| Signal cycle | 17:30 ET daily, CME sessions only |
| Settlement retry interval | 5 min |
| Settlement bid/ask fallback | 18:00 ET |
| Settlement final drop | 18:30 ET |
| Macro pause window | −5 min through +30 min around tier-1 events |
| Calendar ratification cutoff | 23:00 ET |
| Calendar staleness halt | 48 h |
| Reconciliation cadence | 60 s during session |
| Reconciliation staleness halt | 60 s during session |
| Reconciliation EOD | 18:30 ET |
| Health check failure threshold | 3 consecutive (15 min) |
| Watchdog cron | 5 min |
| Monthly slippage recalibration (Phase 1) | 1st of month 22:00 ET |
| Quarterly slippage recalibration (Phase 2) | First weekend of quarter |
| Cost soft alert | $200/mo cumulative |
| Cost hard ceiling | $300/mo (cost-review state, no halt) |

### 7.5 Composite identity

Every signal, order, fill, and trade carries a **composite identity**:

```
(strategy_hash, parameter_set_hash, slippage_calibration_version_id)
```

`strategy_hash` is the git commit SHA of the strategy code. `parameter_set_hash` is a SHA-256 over the canonical (sorted, normalized) parameter values. `slippage_calibration_version_id` is the UUID of the calibration version pinned to that signal.

Why this matters: at any point in the future — for a regulator, a prop-firm DD process, a tax filing, or just sanity-checking a weird trade — you can answer "what code, what parameters, what slippage model produced this trade" without digging through git history or wondering if `parameters.py` changed last month.

---

## 8. The phase roadmap (Phase 0 → Phase 3)

The build is sequenced so that each phase de-risks the next. Skipping a phase is not an option — the criteria below are kill criteria, not "stretch goals."

### Phase 0 — Foundation (weeks 0–8 — current phase)

**Today is Day 3.** The plumbing is being built before any real money touches it.

**What's getting built:**

- Repo, CI, branch protection, secret encryption, deploy workflow
- The trading code itself: signal engine, risk engine, audit log, execution layer, reconciliation
- Database schema (audit log immutability triggers, hash chain, partitioning)
- The QC adapter (the bridge between QC's cloud and our backend in Phase 1)
- Web UI minimum (Today / Trades / System pages)
- Discord bot (alerts, briefings, kill switch from mobile)
- Watchdog (separate VPS in EU that pings the main system every 5 min and emails the operator if it's been unreachable for 15 min)
- 30 CME paper sessions on QuantConnect to validate end-to-end before any live capital

**Phase 0 success criteria:**

- 30 paper sessions, zero audit chain breaks
- LEAN ↔ vectorbt parity within 5% trade count, 0.5% P&L
- Sub-universe verified: ≥ 4 markets active at expected starting equity
- Operator can read logs, deploy, restart any service, kill the system from Discord, ratify the calendar
- v1 strategy backtest Sharpe ≥ 1.5 (otherwise: strategy review before Phase 1 starts)

### Phase 1 — Live Track Record (months 2–5; post-pivot 2026-05-12)

**The first real money trades.** $15–25k IBKR Pro live, **direct via `ib-async` to an `ib_gateway` container running on the operator's VPS**; LEAN runs locally in a `lean_local` container alongside. The backend holds IBKR credentials in sops-encrypted secrets. No QuantConnect Cloud involvement in production.

**Architecture (post-pivot):** the operator's Hetzner VPS runs `lean_local` (which hosts the v1 trend-following algorithm), `ib_gateway` (which holds the TWS API session to IBKR), the FastAPI `api` (which receives LEAN's signal POSTs at `/api/internal/lean/signals` and orchestrates risk + execution), plus the rest of the Docker Compose stack from Phase 0. Round-trip latency target: p99 ≤ 5 seconds.

> **Pre-pivot narrative (RETIRED):** "$15–25k IBKR Pro live, on a CME-Globex routing through QuantConnect's LEAN engine. The backend has no broker credentials in Phase 1 — every order is written to QC's ObjectStore and the QC algorithm executes it. Architecture quirk: the backend writes order instructions to a shared file store (QC ObjectStore), the QC algorithm polls every 5 seconds... Round-trip latency target: p99 ≤ 20 seconds." Retired per DP-025 (QC's `/object/get` is Institutional-tier-gated; the polling architecture is infeasible on a solo-operator budget). See `Docs/decisions-log.md` 2026-05-12 entry.

**Phase 1 success criteria (post-pivot):**

- 6-month rolling live Sharpe ≥ 0.8
- Max drawdown ≤ 15%
- Signal acceptance ≥ 90% (post-universe-filter, post-Stage-5-rounding)
- Zero audit chain breaks
- Zero `incident_review` halts
- Cost envelope ≤ $200/mo soft alert (post-pivot: the QC Researcher-$60/mo subscription drops — savings offset the higher Hetzner VPS spec needed to run LEAN Local + IBKR Gateway containers)
- First slippage recalibration on real fills (month 4)
- First agent-drafted parameter PR (month 4)
- **Kill-switch SLO ≤ 5s** (the original "Phase 2 success criterion" promoted into Phase 1 because the direct-IBKR path meets it from day 1)

### Phase 2 — `[RETIRED — pivot 2026-05-12]`

> **Status post-pivot:** "Phase 2: Custom Infra Hardened" was originally the cutover milestone — operator transitioned from QC-Cloud-mediated to direct-IBKR over Months 5–9. After the 2026-05-12 pivot, that transition is already complete at Phase 1 onset (via Pivot-PRs A through E). **There is no Phase 2 cutover event.** The architectural improvements originally scheduled here fold into continued Phase 1 evolution.
>
> Pre-pivot Phase 2 content preserved below for institutional memory.

**Pre-pivot Phase 2 plan (RETIRED):** Cutover to direct broker access. LEAN runs locally on the operator's VPS. IB Gateway connects directly to IBKR via the TWS API. The backend now holds broker credentials. Round-trip latency target: ≤ 5 seconds.

Cutover gate: eight automated pre-cutover checks ran the day before (LEAN Local backtest reproduces last 30 Phase 1 sessions ≤ 0.5% divergence; vectorbt parity; IB Gateway boot health; ib-async paper test; no HALT_NEW in 24h; audit chain integrity; S3 restore < 4h ago; slippage calibration head pinned). Any single failure aborted the cutover. Cutover itself happened at session close: positions flatten via QC algorithm, audit log records `phase_cutover_started`, QC enters drain mode for 24h. Audit log was continuous across the cutover — single hash chain spanning both phases.

Pre-pivot Phase 2 success criteria (RETIRED): zero audit gaps through cutover; first Phase 2 signal-to-fill round trip ≤ 5s SLO met; Phase 2 portfolio live Sharpe ≥ 1.2; kill-switch SLO ≤ 5s; operator can debug a degraded service via logs alone.

### Phase 3 — Capital Scaling and F&F Prep (months 9–12)

**Scaling, second strategy, and the legal structure for outside capital.**

- **LLC formed; securities lawyer consult.** The schema has supported multi-account from day one (every row carries an `account_id`), so adding F&F principals is an INSERT, not a migration.
- **Second strategy** added — sequentially, after Phase 1 live validation. New strategy version, full 30 CME paper sessions, walk-forward + held-out validation. Multi-strategy makes the `m_combined` composition decision interesting (per-strategy CONVALESCENT vs. global) — that decision is deferred to this phase.
- **Capacity analysis** at 5×, 10×, 25× current capital for both strategies — at what AUM does slippage start eating the edge?
- **Track-record export** for prop-firm allocation: CSV with the full hash-chain footer; verifiable against the audit log for any third party.

**Phase 3 success criteria:**

- Phase 3 portfolio live Sharpe ≥ 1.5
- Track record qualifies for prop-firm allocation OR first F&F commit (≤ $250k)
- Legal structure operational

---

## 9. What changes vs. what stays locked across phases

### Stays locked across all phases

- **The audit log.** Single continuous hash-chained log from Phase 0 day 1 onward. The cutover from Phase 1 → Phase 2 does not break the chain.
- **The parameter set hash convention.** Every signal/order/trade is tagged with the strategy + parameter + slippage version that produced it.
- **The risk envelope.** Vol target, sizing stages, kill-switch state machine, vol-target multiplier composition (MIN, not product), decommission floor — all unchanged.
- **The agent's authority limits.** Tighten only, never loosen. No `place_order` tool. Auto-revert thresholds. Read-only DB role. PR Review Surface for everything else.
- **The operator's role.** The operator is the only one who can resume from a halt, approve a risk-affecting PR, or change a forbidden-whitelist file.

### Changes between phases

| Capability | Phase 0 | Phase 1 (post-pivot 2026-05-12) | Phase 2 (RETIRED) | Phase 3 |
|---|---|---|---|---|
| Where the trading engine runs | QC Cloud (paper) — pre-pivot only; Day 4-28 work was QC paper | Backend VPS (LEAN Local from Day 29+) | RETIRED — was "Backend VPS (LEAN Local)" | Same as Phase 1+ |
| Who holds broker credentials | QC (paper) | Backend (sops-encrypted IBKR creds) | RETIRED — was "Backend" | Backend |
| Backend → broker latency | n/a | ≤ 5s direct via `ib-async` | RETIRED — was "≤ 5s" | ≤ 5s |
| Direct IBKR connection | ❌ | ✅ ib-async direct via `ib_gateway` | RETIRED — was "✅ ib-async" | ✅ |
| Slippage recalibration cadence | bootstrap (zero prior) | monthly | RETIRED — was "quarterly" | monthly (post-pivot keeps Phase-1 cadence; quarterly downgrade no longer planned) |
| Strategy count | 1 | 1 (v2 prep begins Month 5) | RETIRED | 2+ (sequential adds) |
| Account count | 1 | 1 | RETIRED | 2+ (F&F principals) |
| Web UI surface | minimal | full from Phase 1 (Day 20-27 work shipped Phase-1 surfaces in Phase 0; Phase 2 features fold forward) | RETIRED — was "full" | full + F&F dashboards |
| Polygon.io contingent | ❌ | only if QC bundled data gaps | RETIRED | only if data gaps |
| QC ObjectStore polling | ❌ (paper signals were QC-LEAN-native; no backend poll) | ❌ (RETIRED architecture; `services/qc_adapter/**` is dormant under `qc_adapter_backfill` profile gate per backend-spec §1.4) | n/a | ❌ |

**Post-pivot "what stays locked across all phases" additions:**
- The direct-IBKR + LEAN Local architecture is the canonical broker path from Phase 1 onset onward. No re-introduction of QC-mediated paths is planned.
- The `qc_adapter_backfill` profile gate is the only legitimate way to spin up `services/qc_adapter/` in production; it requires an explicit operator action + decision-diary entry.

### What never changes

The thing being optimized: **a verifiable, reproducible, defensible track record on a small basket of liquid markets, run by one person with AI assistance, that scales to F&F and prop allocations on its merits**. Every locked decision in this system is downstream of that goal.

---

## 10. Glossary

| Term | Meaning |
|---|---|
| **ADV** | Average Daily Volume — used in the slippage model |
| **ATR** | Average True Range — a volatility measure used to set stops |
| **Audit log** | An append-only, hash-chained, immutable record of every meaningful event in the system. Cannot be modified or deleted by any role except a "break-glass" superuser, and even those attempts are blocked at the database trigger level for normal operation. |
| **CME session** | Sun 18:00 ET → Fri 17:00 ET with daily 17:00–18:00 ET maintenance pause. The canonical session calendar throughout this system. |
| **Composite identity** | `(strategy_hash, parameter_set_hash, slippage_calibration_version_id)` — attached to every signal/trade so historical decisions are reproducible |
| **CONVALESCENT** | Risk state with vol multiplier 0.5; entries permitted; auto-promotes to NORMAL after 5 clean CME sessions |
| **Donchian channel** | The trailing N-day high and low; "Donchian breakout" = today's close prints above the trailing high (or below the low for shorts) |
| **F&F** | Friends and Family — the post-Phase-3 pool of outside capital |
| **FlexQuery** | IBKR's daily XML report — used for end-of-day reconciliation |
| **HALT_NEW** | No new entries; existing positions hold; exits continue (stops, profit-targets, manual close). Three sub-flavors by severity: `routine`, `defensive_envelope`, `incident_review` |
| **Hot-fix whitelist** | Code paths where Claude Code (or the agent) can deploy without a PR — logging, retry helpers, monitoring, agent reporting templates. Auto-rollback on metric breach. |
| **Hurst exponent** | A measure of long-range dependence in a price series. H > 0.5 = persistent / trending; H < 0.5 = mean reverting; H = 0.5 = random walk |
| **JCS** | RFC 8785 JSON Canonicalization Scheme — how the audit log serializes data before hashing |
| **`live-small`** | Real money, equity < $50k at signal time |
| **`live-scale`** | Real money, equity ≥ $50k at signal time |
| **`m_combined`** | The MIN of all active vol multipliers (NOT compounded) |
| **Macro window** | The 5-min-before / 30-min-after window around tier-1 macro events when order placement pauses |
| **PSD repair** | Replacing any negative eigenvalues of a covariance matrix with zero, then reconstructing — guarantees the matrix is positive semi-definite before use in vol calculations |
| **PR** | Pull Request — the canonical change-review mechanism for any code change |
| **QC / QuantConnect** | The cloud platform running LEAN; in Phase 1 the QC algorithm holds the broker credentials and executes orders |
| **R/S analysis** | Rescaled-range analysis — the classical estimator for the Hurst exponent |
| **Reconciliation** | Continuous (60s during session) and EOD comparison between the system's understanding of state and the broker's authoritative state |
| **Risk rings** | The set of pre-trade and post-trade risk checks that fire continuously |
| **Sharpe** | Sharpe ratio — annualized excess return / annualized volatility; used as the headline risk-adjusted performance number throughout |
| **Slippage calibration** | The OLS-fit linear model of how realized fill prices deviate from expected, per market — recalibrated monthly in Phase 1, quarterly in Phase 2+ |
| **Trailing drawdown** | Drawdown from the running peak equity high-water mark |
| **Universe filter** | Stage 0 of position sizing — markets where 1-contract notional > 50% × equity are excluded |
| **Vol target** | The annualized portfolio volatility the sizing algorithm is shooting for. Default 15%/year. Modulated by `m_combined`. |
| **Watchdog** | An independent VPS in a separate datacenter that pings `/api/health` every 5 min and emails the operator if the main system is unreachable. **Has no authority to halt or modify state — alerts only.** |

---

## Source documents

This document is a synthesis. The canonical, technical-detail-level sources are:

- `Docs/backend-spec.md` — full backend architecture (4,800+ lines). Especially §1 (architecture), §2.3–2.5 (signal/risk/execution engines), §2.10 (audit), §11 (phased build plan), §12 (Claude Ops Agent).
- `Docs/frontend-spec.md` — full frontend (4,900+ lines). PR Review Surface details, web UI for the operator.
- `Docs/claude-dev-guide.md` — coding conventions, locked decisions, position-sizing reference implementation, kill-switch state machine code (2,700+ lines).
- `Docs/decisions-log.md` — append-only log of where reality differs from the specs. Read this before assuming a number in the specs is current.
- `strategies/v1_trend_following/parameters.py` — the canonical default parameters and their validation ranges.
- `implementation-guide.md` — the operator's day-by-day handbook for the build itself.
