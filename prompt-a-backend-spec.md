# PROMPT A — BACKEND TECH SPEC

## ROLE

You are a senior systems architect with deep production experience designing and building algorithmic trading infrastructure for small systematic CTAs and prop shops. You have shipped multiple live trading systems handling real money. You understand the difference between a research notebook and a system that doesn't lose track of an order at 9:31am Monday morning.

You are producing a comprehensive technical specification for the BACKEND of a single-operator algorithmic trading system. The system will be implemented primarily by Claude Code (an AI pair programmer) working with a non-technical solo operator. Your spec must be detailed and unambiguous enough that the implementing engineer never needs to make strategic decisions — only implementation choices.

## OPERATOR CONTEXT

- Solo trader, finance background (3 years banking, BS finance, SIE certification only — no Series 7/63/65/66)
- No coding ability; relies on AI pair-programming for implementation
- $30–35k total capital pool ($15–25k initial live trading, $10–15k reserve)
- Will add up to $250k of family capital after 12+ months of clean live track record AND legal structure (LLC + securities lawyer consult) is in place
- Goal: 6–12 month track record sufficient to qualify for prop firm allocation or first F&F commit
- Located in NJ, USA (US person, US tax)
- Trades alone for the first 12 months; no second operator
- Operator commits ~5–8 hrs/week to operational learning (Python basics, cloud, git) for first ~8 weeks while system is built

## LOCKED STRATEGIC AND SYSTEMS DECISIONS — DO NOT REOPEN

The decisions below are not advisory. They are constraints. Architect within them. If a section appears underspecified, default to the conservative interpretation and flag with `[CONSERVATIVE DEFAULT: ...]` rather than substituting your own preference.

### Canonical Session Calendar (referenced throughout)

**The canonical session calendar is CME RTH** (Sun 18:00 ET → Fri 17:00 ET, with daily 17:00–18:00 ET maintenance pause). This is the calendar used for:
- 17:00 ET daily MTM anchor
- CONVALESCENT 5-session counter
- Capital-event mode 30-session counter
- Trading-day counts in general

Where a behavior is specifically ETF-related (PDT rule, ETF order placement), the **NYSE calendar** (9:30–16:00 ET, Mon–Fri excluding NYSE holidays) is used. Half-days follow NYSE's published early-close schedule.

The two calendars differ on a small number of days per year (Black Friday early close, day after Thanksgiving, MLK day in some configurations). Implementer must read both calendars from a maintained source (e.g., `pandas_market_calendars`).

### Strategy

- **Phase 1 strategy:** multi-asset systematic trend-following on micro futures + bond ETFs
- **Universe:** ~8–12 markets — equity index micros (`/MES`, `/MNQ`, `/M2K`, `/MYM`), commodity micros (`/MCL`, `/MGC`, `/SIL` — CME micro silver; **NOT `/MSI` which is non-standard**), Bitcoin micro (`/MBT`), bond ETFs (TLT, IEF, SHY); optional FX micros (`/M6E`)
- **Signal type:** time-series momentum / breakout (Donchian channels, MA crossovers); vol-targeted sizing; daily bars
- **Daily bar definition (locked, per asset class):**
  - Futures (CME-listed: equity index micros, commodity micros, `/MBT`, `/M6E`): close = **CME daily settlement, 17:00 ET**
  - ETFs (TLT, IEF, SHY): close = **NYSE close, 16:00 ET**
- **Signal generation cadence:** scheduler runs at **17:30 ET** after both close anchors. **Settlement publication tolerance:** scheduler waits up to 30 min for missing CME settlement prints; if prints not available by 18:00 ET, use last available bid/ask midpoint with `unsettled` flag and proceed; if prints unavailable >60 min, halt signal generation for affected market that day.
- **Holding period:** 2 weeks to 6 months
- **Phase 2+:** add second uncorrelated strategy (likely defined-risk vol carry on SPX) only after Phase 1 live validation; sequential strategy addition, never parallel cold-start
- **Base currency:** USD only. No FX hedging. `/M6E` settles in USD via IBKR auto-conversion at end of day; intraday IBKR-side FX rounding may produce non-zero EUR balance briefly (covered by Reconciliation Tolerances Table FX-cash row).
- **Account model:** single live IBKR Pro account. Schema supports multi-account via `account_id` foreign key throughout.
- **PDT / Reg T constraints (locked):**
  - Futures use SPAN margin; PDT does not apply.
  - ETFs use Reg T (50% initial, 25% maintenance). PDT rule applies while account equity < $25k.
  - **PDT pre-check (specified):** before placing any new ETF entry order, if `account_equity < $25,000` AND `rolling_5_day_day_trade_count >= 3`, **refuse the entry**. Day-trade count maintained from FlexQuery + intraday TWS state. Conservative interpretation — under-trades rather than risk a PDT violation.
  - Portfolio Margin not in scope (requires $125k+).
- **Sharpe definition (canonical, used by all triggers):**
  - Annualization factor: 252
  - Risk-free rate: 0
  - Returns: daily close-to-close based on 17:00 ET MTM
  - `Sharpe = mean(daily_returns) × sqrt(252) / stdev(daily_returns)`
  - "X-day rolling Sharpe" uses last X CME RTH sessions

### Path / Phasing

- **Phase 0 (weeks 0–8, extended to absorb holiday risk):** foundation — operator upskilling, IBKR Pro account opening, QC subscription, repo + CI scaffolding, secrets management (sops), Hetzner VPS provisioned, audit schema designed and migrated. **Paper trading begins on QuantConnect in week 1 with the v1 strategy. QC ObjectStore audit adapter coded and golden-tested against custom-format target by week 4. 30 NYSE trading days of paper completed within weeks 1–7 (calendar buffer absorbs typical 1–2 holidays in window). Week 8 is buffer + Phase 1 handover.** If 30-day paper minimum slips past week 7 due to extended holiday cluster, Phase 0 extends until met (the CI gate is hard).
- **Phase 1 (months 2–5):** live trading on QuantConnect Cloud (LEAN). Real money, small size (`live-small`). Track record begins immediately.
- **Phase 2 (months 5–9):** custom infrastructure built and hardened; strategy execution migrates to LEAN Local (Docker-hosted) with vectorbt as fast research/sweep layer.
- **Phase 3 (months 9–12):** capital scaling, second-strategy preparation, family-money legal structure work.
- **Phase 1 → Phase 2 cutover (scheduled, not ad-hoc):**
  - Operator selects cutover date at least 5 trading days in advance via web UI
  - Pre-cutover automated checklist: positions reconciled (zero break in last 24h), no open stop-orders > 5σ from current price, all parameter sets canonicalized, custom backend passing all integration tests
  - **Abort condition:** any pre-cutover check fails OR any HALT_NEW state active in 24h prior — cutover deferred to next selected date
  - Cutover executes at session close on selected date: flatten all open positions on QC; reconcile audit log to terminal state; restart fresh on LEAN Local the following morning
  - **No position transfer across execution venues.** Audit log remains continuous; physical positions reset to flat.

### Tech Stack (locked)

- **Language:** Python 3.11+ end to end
- **Engine:** LEAN (QuantConnect Cloud Phase 1; LEAN Local self-hosted via Docker Phase 2). **LEAN is the AUTHORITATIVE backtest engine for the PR review surface.** vectorbt is research-only for parameter sweeps.
- **Research/sweep:** vectorbt (or vectorbt-pro)
- **Storage:** DuckDB on Parquet for historical/research/analytics; PostgreSQL 16 (containerized) for transactional state
- **Postgres driver:** asyncpg with SQLAlchemy 2.x async
- **Migrations:** Alembic
- **Broker library:** `ib-async` (community-maintained fork of `ib_insync`). Phase 1 routes via QC's IBKR integration; Phase 2 direct via `ib-async` to IB Gateway in Docker.
- **Margin model:** SPAN for futures; Reg T for ETFs.
- **"Used margin" definition (canonical):** `used_margin_pct = 1 − (ExcessLiquidity / NetLiquidation)`, both pulled from IBKR's `accountSummary`. Used in margin protocol thresholds.
- **Orchestration:** cron + APScheduler within Python services. **Scheduler and calendar service co-located in a single APScheduler-backed Python process** with **persistent (Postgres-backed) job store**. NO Airflow/Prefect/Dagster.
- **Real-time push:** SSE for browser one-way push; REST for everything else; **NO WebSocket.**
- **Deployment:** Single VPS, Hetzner Cloud Ashburn (US East), Ubuntu LTS, Docker Compose. NO Kubernetes.
- **Process supervision:** Docker Compose restart policies + systemd for the host; chrony for NTP
- **Logging:** `structlog` with JSON renderer
- **Validation:** pydantic v2
- **API exposure:** FastAPI on the VPS

### Data Sources (locked)

- **Phase 1:** QuantConnect bundled equities + futures data; IBKR real-time market data (free to account holders for our universe).
- **Phase 2 additions (Polygon.io is CONTINGENT, not committed):** Polygon.io Stocks Starter ($30/mo) **only if** QC bundled equity data has gaps the strategy notices in Phase 1 live. Default Phase 2 = no Polygon. **FRED** (free) for macro context. Economic calendar via Forex Factory or Trading Economics.
- **NOT in scope:** Norgate Data, alt data, NLP feeds, Bloomberg, Databento, multi-tier feeds.
- **Data correctness claims (precise, per leg):**
  - **ETFs/equities:** QC bundled data is **survivorship-bias-free** (delisted instruments included; corporate actions handled).
  - **Futures:** survivorship is not the relevant correctness criterion (futures contracts have finite life by design). The relevant criterion is **roll methodology**: QC uses Panama-method or open-interest-based continuous-contract construction. **LEAN execution uses physical contracts**, so backtest continuous-contract returns must reconcile to physical-fill returns at cutover dates. This reconciliation is a mandatory test (see Testing).

### Risk Framework (concrete math; locked)

#### Position sizing — full algorithm (locked)

Stage 1 — **Inverse-vol weighting (unconstrained):**
```
For each active market i:
  σ_i = rolling 60-day stdev of daily log returns
  raw_weight_i = 1 / σ_i
  total = Σ raw_weight_j (over all active j)
  unconstrained_weight_i = raw_weight_i / total
  unconstrained_notional_i = unconstrained_weight_i × (portfolio_vol_target / portfolio_realized_vol_at_unconstrained_weights) × equity
```
Where `portfolio_vol_target = VOL_TARGET_PCT_ANNUAL / sqrt(252)` (daily target).

Stage 2 — **Apply per-position cap:** for each i, `capped_notional_i = min(unconstrained_notional_i, 0.25 × equity)`.

Stage 3 — **Apply per-cluster cap (iterative shrink-to-fit):**
```
For each cluster c with cap C_c:
  cluster_total = Σ |capped_notional_i| for i in cluster c
  if cluster_total > C_c × equity:
    scale = (C_c × equity) / cluster_total
    for i in cluster c: capped_notional_i *= scale
Re-apply per-position cap after cluster scaling.
Iterate until all caps satisfied (typically converges in 1–2 passes).
If still infeasible: drop the lowest-momentum signal in the binding cluster; restart Stage 3.
```

Stage 4 — **Apply gross/net caps:**
```
gross = Σ |capped_notional_i|
if gross > 3.0 × equity: uniform shrink all positions by (3.0 × equity / gross)
net = Σ capped_notional_i (signed)
if |net| > 1.5 × equity: uniform shrink all positions by (1.5 × equity / |net|)
Re-apply per-position and per-cluster caps after rescale.
```

Stage 5 — **Lot-size rounding:**
```
For each market i:
  contract_count_i = capped_notional_i / (point_value_i × multiplier_i)
  rounded_contract_count_i = round_to_nearest_integer(contract_count_i, banker's_rounding)
  if rounded_contract_count_i == 0: drop signal; tag as 'sub_minimum_size'
  realized_notional_i = rounded_contract_count_i × point_value_i × multiplier_i
  rounding_deviation_i = (realized_notional_i - capped_notional_i) / capped_notional_i
  Track rounding_deviation in attribution
```

#### Equity and DD anchors (locked)
- **Daily-start MTM anchor:** **17:00 ET** (CME daily settlement boundary), portfolio-wide. Daily P&L = `MTM(t) − MTM(prior 17:00 ET snapshot)`.
- **Trailing DD reference:** peak intraday MTM equity since system inception, including unrealized.
- **Capital-event handling (deposit/withdrawal ≥ 5% of current equity):**
  - Trailing DD reference resets to current equity at deposit time
  - System enters "capital event mode" for **30 CME RTH sessions**: peak MTM tracked from deposit date forward; convalescent-style 50% vol target for first 5 sessions
  - Audit log entry with full provenance
- **Withdrawals:** symmetric except peak MTM does NOT reset on withdrawal.
- **Capital-event mode + CONVALESCENT stacking:** counters run independently; vol-target multipliers compose via MIN (not multiplied). E.g., capital-event-mode 50% AND CONVALESCENT 50% → effective multiplier = 0.5, not 0.25.

#### Risk rings (all units explicit)
All rings measured against **mark-to-market equity**, **gross/net in instrument notional terms** (not margin).

| Ring | Limit | Measurement Basis |
|---|---|---|
| Per-position max | 25% of equity notional | Sum of \|notional\| for that single market |
| Gross portfolio max | 300% of equity notional | Sum of \|notional\| across all positions |
| Net portfolio max | 150% of equity notional | Signed sum of notional across all positions |
| Equity-index cluster max | 60% gross | Combined `/MES`, `/MNQ`, `/M2K`, `/MYM` |
| Commodity cluster max | 80% gross | Combined `/MCL`, `/MGC`, `/SIL` |
| Rates/bonds cluster max | 80% gross | Combined TLT, IEF, SHY |
| Crypto cluster max | 40% gross | `/MBT` |
| FX cluster max | 30% gross | `/M6E` and any future FX micros |
| Realized cross-portfolio correlation | Alert at avg pairwise > 0.7; HALT_NEW at > 0.85 | 60-day rolling realized correlation matrix across open positions |
| Daily loss limit | -5% of daily-start MTM (17:00 ET anchor) | Portfolio-wide |
| Trailing drawdown limit | -20% from peak intraday MTM equity | Subject to capital-event reset rule |
| Monthly DD threshold | -10% in calendar month | Triggers vol-target halving (0.5×) for remainder of month |
| Strategy decommission floor | Auto-halt + human review (HALT_NEW) | (a) live 30-day Sharpe < 0, OR (b) live max DD breach -25%, OR (c) 60-day live Sharpe underperforms backtest by > 2 SD where SD is empirical SD of 30-day rolling Sharpes from walk-forward folds (pre-Phase-1) or from 30-day rolling windows in live track record (≥ 6 months live) |

**Decommission floor → workflow:**
1. State transitions to HALT_NEW with `reason = decommission_floor_<a|b|c>`
2. Strategy version flagged `decommissioned` in `strategy_versions` table with full reason
3. Audit entry with provenance
4. Resume requires either: explicit operator override (re-auth + audit entry justifying continuation) OR deployment of new strategy version (which resets `paper_days_for_version` counter; 30 new paper days required before live)

#### Vol regime detector
- Metric: 60-day rolling realized volatility of portfolio daily returns
- Z-score: current value vs. its own 60-day historical distribution (250 days of 60-day windows)
- Trigger: z-score > 2 → kill-switch fires (HALT_NEW)

#### Signal storm detector (recalibrated for actual trade frequency)
For our daily-bar, 8–12-market, 2-week-to-6-month-holding strategy, baseline mean daily trade count is small (often <1). Naive 3× multiplier would trip on normal days.

- Metric: portfolio total trade count in current CME RTH session
- Trigger: `session_count > max(5, 3 × rolling_90_day_mean_daily_trade_count)`
- The hard floor of 5 prevents spurious trips on low-baseline days; the 3×-90d guard catches genuine storms once the strategy has more activity

#### Margin protocol — graduated defensive de-leverage (NOT panic-flatten)

- 70% used → warn alert (no action)
- 85% used → auto-trim sequence:
  1. Compute momentum score for each open position: rolling 60-day z-score of price returns (instrument's own distribution). Lower z-score = weaker.
  2. Rank ascending; tie-break by largest absolute margin contribution.
  3. Cut via marketable-limit orders (1× spread, escalating to 2× on retry).
  4. **Hard cap per session: -30% of gross exposure across the entire sweep.**
  5. Cut until used margin < 60% OR session cap reached.
  6. If margin still > 80% after one full sweep, escalate to HALT_NEW — no further system-initiated trims.

**Acknowledged residual risk (locked, made explicit):** if HALT_NEW is reached with used margin still > 80%, **IBKR may issue a margin call and force-liquidate positions outside system control.** This is the broker's right; the system does not prevent it. The "no system-initiated panic-flatten" principle is preserved at the system layer; broker-mandated liquidation is outside scope. Operator alert language must explicitly call this risk out at HALT_NEW entry due to margin (different alert text from other HALT_NEW triggers).

#### Capacity tracking
- Rolling 30-day average daily volume (ADV) computed per market
- Order size as % of ADV computed at signal-emit time
- Alert at 0.5% ADV; partial-fill cap at 2% ADV
- **Capacity refusal at 2% ADV → partial fill at the cap** (size to 2% of ADV; tag remainder as `capacity_constrained`; position sized at actual filled fraction)

### Kill-Switch State Machine (explicit)

States:
- **NORMAL** — full operation
- **HALT_NEW** — cancel all working orders; hold all existing positions (no system-initiated liquidation; broker-mandated possible at margin extremes); no new entries; **all exit logic continues normally** (stops, profit-targets, manual close); manual human resume only
- **CONVALESCENT** — 50% vol target (or compose-via-MIN with capital-event mode); entries permitted; remains for **5 CME RTH sessions** portfolio-wide; auto-transitions to NORMAL on completion

**HALT_NEW maximum dwell time:** no auto-flatten. After **7 trading days in HALT_NEW**, system escalates to operator with daily reminder (Discord + email backup); operator can extend, manually flatten, or resume. Designed to never auto-flatten; operator stays in control.

Transitions:
- `NORMAL → HALT_NEW`: any kill-switch trigger fires
- `HALT_NEW → CONVALESCENT`: human resume (re-auth required; web-only)
- `CONVALESCENT → NORMAL`: 5 CME RTH sessions complete without breach
- `CONVALESCENT → HALT_NEW`: any kill-switch trigger fires; counter resets on next resume
- **No HALT_ALL or auto-liquidate state.** Margin auto-trim is graduated de-leverage (above), NOT panic-flatten.

#### CONVALESCENT counter reset events (locked, corrected for internal consistency)

| Event | Resets 5-session counter? |
|---|---|
| Any kill-switch trigger fires while in CONVALESCENT (returns to HALT_NEW) | YES — counter reset on next resume |
| Heartbeat engagement timeout (Defensive Risk Envelope path; transitions to HALT_NEW) | YES — heartbeat timeout IS a kill-switch trigger; consistent with row 1 |
| Reconciliation false-positive resolved within tolerance (no state transition) | NO — no state transition occurred |
| Calendar ratification grace window (no state transition) | NO — no state transition occurred |
| Capital event (deposit ≥ 5% of equity → triggers capital-event mode) | YES — independent reset; both modes active simultaneously, vol multiplier composes via MIN |

#### Kill-switch triggers (any → `→ HALT_NEW`)
- Trailing DD breach (-20% from peak MTM, capital-event-reset-aware)
- Daily loss breach (-5% of daily-start MTM, 17:00 ET anchor)
- Signal storm (recalibrated formula above)
- Reconciliation mismatch (delta exceeds Reconciliation Tolerances Table)
- Broker disconnect persisting > 5 minutes during CME RTH
- Vol regime detector trip
- Realized cross-portfolio correlation > 0.85
- Decommission floor trigger
- Audit log write failure
- Any unhandled exception in execution path
- **Heartbeat engagement failure (Defensive Risk Envelope path; identical state, escalated alert routing)**

### Defensive Risk Envelope (clarified)

Heartbeat engagement failure is a kill-switch trigger that transitions to HALT_NEW. "Defensive Risk Envelope" is the **label and alert routing** applied when the trigger reason is comms breakdown specifically: alerts escalate via email backup with higher priority, external watchdog also notified, retry cadence on Discord delivery increased. The state itself is identical to other HALT_NEW transitions.

### Vacation Mode

- Operator runs `/vacation start [days]` in Discord
- Engagement timeout extends to 7 days
- **NEW position entries auto-disabled.** All EXIT logic (stops, profit-targets, manual close) continues normally.
- Daily summary still posts; daily liveness probe still posted (see Communications)
- **Macro-event ratification gate suspended** (no entries to halt)
- On `/vacation end` or expiry, normal operation resumes

### Risk-Tightening Boundary (parameter changes vs. position trims)

Two distinct paths:
1. **Parameter changes** (within range): take effect at NEXT signal cycle, never mid-session. Used for sustained tightening.
2. **Defensive position trims:** mid-session direct order action via momentum-ranked auto-trim path; capped at -30% gross per session. Causally agent-initiated, **mechanically placed by the risk engine** (which holds broker credentials, not the agent). Audit records both the agent's trigger and the risk engine's placement.

### Auto-Revert Thresholds (parameter changes — recalibrated and parameter-aware)

A parameter change auto-reverts when **any**:
- 30-day rolling live Sharpe drops > **2 SD** (where SD baseline = empirical SD of 30-day rolling Sharpes from walk-forward folds pre-Phase-1, or from rolling 30-day windows in live track record post-6-months-live), AND minimum 30 trades on changed market(s) in window
- Max DD breaches -10% within 5 CME RTH sessions of the change
- **Consecutive losing trades attribution (parameter-aware):**
  - For *globally-applicable* params (e.g., `VOL_TARGET_PCT_ANNUAL`, `INSTRUMENT_VOL_LOOKBACK_DAYS`): 5+ consecutive losing trades portfolio-wide within window
  - For *market-specific or signal-specific* params (e.g., `LOOKBACK_DAYS_DONCHIAN`, `MA_FAST_DAYS`): 5+ consecutive losing trades on any affected market within window. If window doesn't yield 5 trades for that market (consequence of `MIN_HOLDING_DAYS`), this condition cannot fire — other revert conditions still apply.

Auto-revert action: parameter restored to pre-change value; full audit entry; alert; no further auto-changes to that parameter for 14 days.

### Logic-Change vs. Parameter-Change Boundary (clarified)

- **Logic change** (PR + human merge): rule logic, indicator selection, market universe, strategy structure, sizing model, risk-ring values, cluster definitions, parameter ranges themselves, hot-fix-whitelist itself
- **Parameter change** (auto with audit, within range): values within Parameter Ranges Table
- **Pre-approved range itself is logic.** Changing a range requires PR.
- **Parameter changes take effect at next signal cycle, never mid-session.**

### Parameter Ranges Table (LOCKED — full agent-mutable surface)

| Parameter | Min | Max | Default | Description |
|---|---|---|---|---|
| `LOOKBACK_DAYS_DONCHIAN` | 40 | 80 | 60 | Donchian channel lookback for breakout signal |
| `INSTRUMENT_VOL_LOOKBACK_DAYS` | 30 | 90 | 60 | Stdev window for instrument vol estimate |
| `VOL_TARGET_PCT_ANNUAL` | 10 | 16 | 14 | Portfolio annualized vol target |
| `MA_FAST_DAYS` | 10 | 30 | 20 | Fast moving average for trend filter |
| `MA_SLOW_DAYS` | 50 | 200 | 100 | Slow moving average for trend filter |
| `STOP_DISTANCE_ATR_MULT` | 1.5 | 3.5 | 2.5 | Stop distance in ATR multiples |
| `HURST_THRESHOLD` | 0.45 | 0.65 | 0.55 | Hurst exponent floor for trend signal |
| `ROLL_DAYS_BEFORE_EXPIRY` | 5 | 7 | 7 | Days before expiry to roll futures |
| `MIN_HOLDING_DAYS` | 5 | 21 | 10 | Minimum holding period before exit eligible |

**These are agent-mutable within Min/Max.** Outside Min/Max requires PR.

**`parameter_set_hash` SCOPE:** the hash is computed over **only the parameters in this table**. Risk-ring values, cluster caps, hot-fix whitelist, and all other code constants ride `strategy_hash` (changing them changes git SHA and thus `strategy_hash`).

### Slippage Calibration as Versioned Artifact (locked, separates from strategy version)

LEAN's slippage model parameters are recalibrated empirically from Phase 1 live fills. Recalibration:
- Lives in a separate `slippage_calibration` versioned table (`slippage_calibration_version`)
- Recalibration is logged as an audit event but **does NOT trigger paper-day counter reset** (it doesn't change live execution behavior, only the model of historical fills used in backtest)
- PR backtest delta computation always uses the **current `slippage_calibration_version` at PR-creation time**; the same delta with a different calibration version would produce different numbers, but the version is fixed at PR creation and re-used if PR is re-run later
- Trade records carry `slippage_calibration_version` alongside `strategy_hash` and `parameter_set_hash` for full reproducibility
- Recalibration cadence: monthly during Phase 1; quarterly Phase 2+
- Alert if realized > 2× modeled for any single market for 3 consecutive months → strategy review

### Reconciliation Tolerances Table (LOCKED)

A delta exceeding tolerance → kill-switch trigger.

| Metric | Tolerance | Grace Period |
|---|---|---|
| Position quantities (per instrument) | 0 (exact) | None |
| Cash balance (USD) | greater of $5 absolute or 1 bps of equity | T+1 grace for fees, dividends, interest postings |
| Margin balance | $10 absolute | None |
| FX-denominated cash (intraday for `/M6E` before USD auto-conversion) | $1 absolute | T+1 for FX rounding |
| Realized P&L (cumulative) | $1 absolute | T+1 |
| Unrealized P&L | $5 absolute | None |

**Tolerances widen by 2× during dividend ex-dates for +24h** (auto-detected from instrument calendar).

### Per-Service Degradation Matrix (LOCKED)

"Market hours" disambiguation per row:
- "CME RTH" = Sun 18:00 ET → Fri 17:00 ET (per canonical session calendar above)
- "NYSE hours" = 9:30–16:00 ET, Mon–Fri, NYSE-holiday-aware

| Failure | System Response |
|---|---|
| Risk engine down | Signal engine halts; HALT_NEW |
| Reconciliation stale > 60s during CME RTH | HALT_NEW (kill-switch trigger) |
| Calendar service can't reach FRED/Forex Factory | Use last successful import; alert; if last successful > 48h, hard halt new orders next session until manual ratification |
| QC ObjectStore poll fails 5–9 min | Alert only; transient is expected |
| QC ObjectStore poll fails > 10 min | Defensive Risk Envelope (HALT_NEW + escalated routing) |
| Backend can't reach IBKR > 5 min during CME RTH | HALT_NEW |
| Discord delivery fails | Email backup automatic; external watchdog covers VPS-down case |
| Database write fails (non-audit) | Retry 3× with backoff; on persistent failure, HALT_NEW |
| Database write fails (audit_log) | HARD HALT immediately |
| Anthropic Claude API down | Agent service degrades to read-only; trading continues; alert |
| External watchdog unreachable | Alert; if + Discord delivery also failing, defensive risk envelope |
| CME settlement prints unavailable > 60 min past 17:00 ET | Halt signal generation for affected market that day; resume next session |

### Data Quality Handling (LOCKED)

Per-bar validation at ingestion:

**Reject** (do not act on, log + alert):
- Close price = 0 or negative
- OHLC contains NaN
- High < Low
- Volume = 0 for futures during the relevant session window
- **Bar arrived > 60 min past relevant close anchor** (17:00 ET CME or 16:00 ET NYSE)

**Quarantine** (don't act on, log, no alert escalation):
- |close − prev_close| > 10× rolling 30-day daily range (price spike)
- Volume < 10% of rolling 30-day average for that market
- **Bar arrived 30–60 min past close anchor**

**On rejected or quarantined bar:** skip signal generation for that market that day; log to `audit_log` and `data_quality_events`; continue normal processing for other markets.

### Execution Mechanics

- **Order types:**
  - Entries: limit-marketable (last ± 0.5× spread); on retry, widen to 1× spread, then 1.5× spread
  - Exits (stop): stop-market for execution certainty
  - Profit-target exits: limit at target
  - Futures rolls: calendar spread orders when broker supports; otherwise leg with 60s stagger
  - Kill-switch action: cancel all working orders; **hold positions**
  - Margin auto-trim and defensive position trims: marketable-limit (1× spread, escalating to 2× on retry); never pure market
- **Retry logic:** rejection → 3 retries with exponential backoff (1s, 4s, 16s); after 3 failures, halt that market only, alert
- **Reconciliation source-of-truth (locked):**
  - Intraday risk decisions: TWS API real-time portfolio snapshot
  - End-of-day reconciliation and tax: IBKR FlexQuery (XML)
  - Both reconciled at session close; FlexQuery authoritative for tax + audit
  - Tolerance bands per Reconciliation Tolerances Table
- **Reconciliation cadence:** every session open + close + EOD; weekly summary report
- **Roll discipline:** futures rolled per `ROLL_DAYS_BEFORE_EXPIRY`, off-peak liquidity scheduling
- **Macro event handling:**
  - Auto-pause order placement from 5 min before through 30 min after scheduled tier-1 events (FOMC, CPI, NFP, GDP, PCE, ECB/BOJ/BOE if exposed, OPEC if `/MCL` exposed)
  - NO manual event mode override
  - Calendar imported nightly; user ratifies via Discord by 23:00 ET; default if no ratification: hard halt new orders next session until ratified
  - **Vacation mode exception:** ratification gate suspended (no entries to halt)
  - Macro window vs. session boundary collision: pause wins

### Audit & Track Record

- **Immutability mechanism (corrected for TRUNCATE):**
  - `audit_log` row triggers block `INSERT` modifications and prevent `UPDATE` and `DELETE` via `BEFORE` trigger that raises exception
  - **Row triggers do NOT fire on `TRUNCATE`. To block TRUNCATE: use a Postgres EVENT TRIGGER on `ddl_command_start` that aborts any `TRUNCATE` targeting `audit_log` AND `REVOKE TRUNCATE` from all roles except `dba_breakglass`.** Belt-and-suspenders.
  - Service role grants: `INSERT, SELECT` on `audit_log`; explicitly `REVOKE UPDATE, DELETE, TRUNCATE`
  - **Hash chain:** SHA-256 single-linked list ordered by INSERTION sequence (NOT event time). `prev_hash` references prior record's full hash; `record_hash = SHA-256(prev_hash || record_payload)`. Genesis record has `prev_hash` = 32 zero bytes.
  - **Backfill / repair handling:** backfilled records APPEND at chain tail with `repaired_for_sequence_no` and `repaired_for_event_timestamp` provenance. Original gap remains visible.
  - Backups: S3 with Object Lock (Compliance mode); retention 7 daily / 4 weekly / 12 monthly / permanent annual; quarterly restore drill

- **Postgres role hierarchy:**
  - `app_service` — `INSERT, SELECT` on `audit_log`; `SELECT, INSERT, UPDATE, DELETE` on non-audit tables
  - `app_owner` — schema owner; runs Alembic migrations; cannot bypass audit triggers
  - `dba_breakglass` — superuser; offline credential; documented break-glass procedure with high-severity audit entry on use; only role with TRUNCATE on `audit_log`

- **Strategy version + parameter set + slippage calibration composite identity:**
  - `strategy_hash` = git commit SHA at signal time
  - `parameter_set_hash` = SHA-256 over canonical-serialized active parameter values from Parameter Ranges Table at signal time
  - `slippage_calibration_version` = current calibration artifact version
  - Every trade tagged with all three; queryable by any
  - `parameters` table event-sourced with `valid_from`/`valid_to`

- **Track record portability:** identical audit schema between QC Phase 1 and custom Phase 2; QC adapter emits in custom-target schema; golden-test parity verified weekly

- **Environment tagging:**
  - `paper` = any non-real-money trade
  - `live-small` = real money, account equity < $50k at signal time
  - `live-scale` = real money, account equity ≥ $50k at signal time
  - Determined at signal-emit time; immutable per trade
  - Per-strategy-version: decommission floor and signal-storm thresholds applied per strategy version

- **Paper minimum:** 30 NYSE trading days paper before live deployment of new strategy version (per strategy version); CI gate

- **Trade-level attribution schema (mutability clarified):**
  - `attribution` table has two field groups on the same row:
    - `expected_*` columns (`expected_pnl`, `expected_slippage`, `vol_regime_at_emit`, `trend_regime_at_emit`): computed at signal-emit time; **immutable post-emit, enforced by Postgres trigger**
    - `realized_*` columns (`realized_pnl`, `realized_slippage`, `realized_holding_days`): nullable until trade closes; filled in post-trade
  - Audit log captures both stages with separate event types (`signal_emitted`, `trade_realized`)

### QuantConnect Audit Adapter (Phase 1 critical path)

- **Mechanism:** QC algorithm writes audit events to QC ObjectStore as JSONL with monotonic sequence numbers per session
- **Backend ingestion:** custom service polls QC ObjectStore via QC API every 60s during CME RTH; cursor-based; resumes from last cursor on restart
- **Schema:** identical to custom-emitted audit records; golden-test parity verified weekly (byte-for-byte)
- **Loss handling:** sequence gap → alert + pull from QC's own logs to fill; backfilled records APPENDED at current chain tail with provenance
- **Failure mode:** unavailable > 10 min → Defensive Risk Envelope (HALT_NEW)
- **Clock skew:** every event carries `source_clock_ts` (QC) and `ingest_clock_ts` (backend); monotonic ordering via `ingest_clock_ts` for chain hashing

### Tax Handling

- **Futures (Section 1256):** automatic 60/40 LTCG/STCG with mandatory year-end MTM; no election; system reports Form 6781
- **ETFs:** standard capital gains/losses with wash sale tracking; no 475(f) election by default; system supports both modes
- **CPA consultation REQUIRED before any election;** UI gate: election toggle requires "I have consulted a CPA" acknowledgment
- **Wash sale tracking** across all accounts via `account_id` linkage
- **Year-end harvest flagging:** unrealized losses with low-strategy-impact harvest opportunities surfaced
- **Tax export:** CSVs for Form 6781, Schedule D, Form 8949; PDF summary; importable by Drake/ProSeries/TurboTax. Annual Jan 31.

### Claude Ops Agent — Authority Matrix

| Category | Agent Authority | Implementation Note |
|---|---|---|
| Tighten risk via parameter change (within range, next-cycle) | AUTO with notification | Goes through risk engine API; effective next signal cycle |
| Tighten risk via defensive position trim (mid-session) | AUTO with notification | Causally agent-initiated; **mechanically placed by risk engine** (which holds broker creds). Agent has zero broker creds. Audit records both. |
| Loosen risk | HUMAN APPROVAL REQUIRED | Hard-coded denial |
| Hot-fix infrastructure (within whitelist) | AUTO-DEPLOY with notification + auto-rollback if metrics degrade within 30 min | Whitelist below |
| Strategy logic changes | DRAFTS PR; human reviews and merges | See Operator-Friendly PR Review Surface |
| Place / modify / cancel orders directly (as primary action) | NEVER, hard-coded block | Agent has no broker credentials physically |
| Invoke kill switch | AUTO on hard threshold breach | |
| Un-invoke kill switch | HUMAN APPROVAL ONLY (re-auth, web-only) | |
| Modify strategy parameters within pre-approved range | AUTO with full audit log + auto-revert | Effective only at next signal cycle |
| Generate reports, alerts, briefings, run diagnostics | AUTO | |

#### Hot-Fix Whitelist (LOCKED)

**ALLOWED for agent auto-deploy** (file paths; agent may modify and deploy without PR):
- `services/observability/**`
- `services/monitoring/**`
- `services/agent/**` (excluding decision-path code)
- `infrastructure/retry/**`
- `infrastructure/broker_reconnect/**`
- `infrastructure/logging/**`
- `Dockerfile`, `docker-compose.yml`
- `requirements*.txt`, `pyproject.toml`
- `deploy/**` (deploy configs, not strategy code)

**FORBIDDEN — PR required:**
- `services/risk/**`, `services/signal/**`, `services/audit/**`, `services/execution/**`, `services/reconciliation/**`
- `services/calibration/**` (slippage calibration)
- `alembic/**` (any DB migration)
- Any file containing constants matching `RISK_RING_*`, `KILL_SWITCH_*`, `MARGIN_*`, `CLUSTER_CAP_*`, `PARAMETER_RANGE_*` (enforced by pre-merge linter)

#### Hot-Fix Auto-Rollback Metrics (LOCKED)

Auto-rollback triggers within 30 min of hot-fix deploy if **any**:

| Metric | Threshold |
|---|---|
| Service error rate (5xx + uncaught exceptions) | > 2× 7-day pre-deploy baseline |
| p99 critical-path latency (signal-to-order, kill-switch invocation) | > 2× 7-day baseline |
| Kill-switch invocation frequency | > 2× 7-day baseline |
| Reconciliation break rate | > 2× 7-day baseline |
| Audit log write failure rate | > 0 (any failure → revert) |

Auto-rollback restores prior commit; alert; deploy disabled for that whitelist subtree for 24h.

### Operator-Friendly PR Review Surface

Every PR (agent-drafted or human) touching strategy logic or risk parameters surfaces:

1. **Plain-English summary** (≤ 200 words; agent-written): what changed, why, what behavior changes
2. **Risk impact summary** (auto-generated): which risk metrics affected, by how much, in plain numbers
3. **Backtest delta** (LEAN authoritative; uses current `slippage_calibration_version` at PR creation): equity curve overlay, key statistics delta table, ten worst-divergence trades
4. **Test results** (unit + integration + linting + type-check): pass/fail visible
5. **Diff view** (collapsed by default)
6. **Files affected** (one-line summary per file)
7. **In-app Approve / Reject / Request Changes** buttons (sync to GitHub via API)

For PRs that change *only parameters* (no code change): same git SHA, different `parameter_set_hash`. Backtest delta is computed by re-running LEAN with the proposed parameter set against the same code at the same `slippage_calibration_version`.

The operator's review competence is on items 1-3.

### Decision Diary

- **Operator writes:** mandatory minimum-10-character reasoning on every signal rejection, defer, or override
- **Agent writes:** suggestions / commentary, optional
- **Required fields:**
  - `tag`: `data_concern` | `regime_concern` | `size_concern` | `manual_judgment` | `other`
  - `timestamp`: UTC + monotonic
  - `author`: `operator` | `agent`
  - `reasoning_text`: free text, min 10 chars when author = operator
  - `linked_signal_id`: UUIDv7 reference

### Communications

- **Primary:** Discord bot via `discord.py`. Channels: `#daily-brief`, `#signals`, `#fills`, `#alerts`, `#critical`, `#ops`, `#ask-agent`, `#audit`
- **Backup:** email
- **Discord-bot-as-service architecture:** bot service (gateway WS inbound) and webhook-pusher service (backend → Discord outbound) run as separate services on shared internal Docker network with sops-decrypted secret bundle
- **Heartbeat — split semantics:**
  - **Delivery** = HTTP 2xx ack from Discord on push. Failure → email backup automatic.
  - **Engagement** = ANY of:
    - Discord reaction or reply on critical alerts
    - Email reply to email-backup notifications
    - Web app authenticated activity within session
    - Reply/reaction on **daily liveness probe** (see below)
  - No engagement for > 24h on any critical alert OR no engagement to daily liveness probe → Defensive Risk Envelope (HALT_NEW with escalated routing)
- **Daily liveness probe (locked):** at 09:00 ET each CME RTH session, post a short "system is alive — react/reply to acknowledge" message to `#daily-brief` (and email backup). Operator reaction or reply within 24h = engagement. Solves quiet-day false-positive halts.
- **Vacation mode:** `/vacation start [days]`: engagement timeout extends to 7 days; new entries disabled; exits continue; daily summary + liveness probe still post; ratification gate suspended; `/vacation end` or expiry resumes
- **NO SMS, NO voice escalation**
- **External watchdog (mandatory):** separate-region tiny VPS or AWS Lambda pings backend `/health` every 5 min. Unreachable > 15 min during CME RTH → email to operator. ~$5/month.

### Security

- **Secrets management:** Mozilla **sops** with **age** encryption. Encrypted files committed to repo. **Separate sops files per environment:** `secrets/dev.enc.yaml`, `secrets/paper.enc.yaml`, `secrets/live.enc.yaml`. Live and paper IBKR credentials are in different files; environment selects which.
- **Age key backup:** printed paper copy in offline cold storage
- **Rotation:** quarterly forced; immediate on compromise
- **Database backups:** daily encrypted to S3 with Object Lock (Compliance mode); quarterly restore drill
- **Encryption at rest:** Hetzner volume encryption + application-level for high-sensitivity columns
- **Auth (web):** WebAuthn primary + TOTP backup + 8 single-use printed backup codes
- **All-factors-lost recovery:** dba_breakglass + sops backup restore + manual identity re-establishment
- **Auth tokens:** JWT access (15 min) + refresh (7 days), HttpOnly + Secure + SameSite=Strict cookies, server-side session records, re-auth (WebAuthn UV) within last 5 min for risk-loosening actions only
- **Container hardening:** non-root, read-only fs where compatible, no privileged, Trivy in CI, distroless where compatible
- **Network egress allowlist:** IBKR endpoints, Anthropic API, S3, NTP, package mirrors, GitHub
- **Network ingress:** FastAPI public + SSH (key-only); internal services on internal Docker network
- **Repo / build-chain DR:** self-hosted Gitea on VPS (full GitHub mirror, daily sync); weekly encrypted repo archive to S3
- **GitHub workflow:** branch protection on `main` requires CI pass + at least one approval (operator self-approves agent-drafted PRs via in-app review surface, sync'd via backend's GitHub App install token); no CODEOWNERS (single operator); agent commits to feature branches `agent/...`

### Time and Clock

- **NTP:** chrony, primary `pool.ntp.org`, fallback `time.cloudflare.com`
- **Clock skew tolerance:** log warn at > 100ms; defensive halt at > 1s
- **Audit ordering:** `timestamp_utc` + `monotonic_ns` (within process); QC events also carry `source_clock_ts` and `ingest_clock_ts`
- **All schema timestamps:** `TIMESTAMPTZ` UTC, rendered `America/New_York`

### Idempotency

- **All writes:** UUIDv7 PKs
- **Order placement `client_order_id` (LOCKED budget under IBKR's ~50-char practical limit):**
  - Format: `{strategy_short}-{paramset_short}-{signal_short}-{retry_n}`
  - `strategy_short` = first 8 hex chars of `strategy_hash` (8 chars)
  - `paramset_short` = first 8 hex chars of `parameter_set_hash` (8 chars)
  - `signal_short` = last 12 hex chars of `signal_uuid` (12 chars; sufficient uniqueness within a session)
  - `retry_n` = 1-2 digit integer
  - Total: 8 + 1 + 8 + 1 + 12 + 1 + 2 = **33 chars** (well under 50)
  - Collision behavior: 8-char hash collisions extremely unlikely at our trade volume; on detected collision, signal_uuid is regenerated and prepend retry suffix
- **Audit writes:** UUIDv7
- **Webhook re-delivery:** dedupe by `event_uuid` for 7-day window via Postgres unique constraint

### SLO Budgets

- Signal-to-order latency: p50 ≤ 60s, p99 ≤ 5min
- Kill-switch invocation latency: ≤ 5s
- Reconciliation freshness during CME RTH: ≤ 60s
- Discord webhook delivery: ≤ 10s p99
- Backtest queue: p99 ≤ 30 min on QC tier; upgrade if persistently exceeded

### RPO / RTO

- RPO: 15 minutes (Postgres WAL ship to S3 every 15 min via `wal-g` or equivalent)
- RTO during CME RTH: 4 hours
- RTO outside CME RTH: 24 hours
- Single VPS accepted; no warm standby
- DR runbook: external watchdog email → TWS desktop → IBKR phone trading desk → restore from backup → reconcile audit log; flag affected trades `outage_period`

### Backtesting Validation

- Walk-forward: rolling 3-year train, 6-month out-of-sample, advance, repeat
- 70/30 in-sample / held-out test split; held-out touched ONCE
- Survivorship-bias / continuity per Data Sources section (precise per-leg)
- Realistic fills via slippage calibration artifact (versioned; recalibrated monthly Phase 1, quarterly Phase 2+)
- Tax modeling computed post-hoc on trade log
- Capacity analysis at 1×, 5×, 10×, 25× current capital; refuse migration if degradation > 30%
- 30 NYSE trading-day paper minimum per strategy version; CI gate

### Testing Discipline

- **Unit tests required:** risk engine (every state transition, every kill-switch trigger, every cluster-shrink iteration), position sizing (full algorithm including lot rounding), order routing, audit log immutability + hash-chain integrity (including backfill provenance and TRUNCATE blocking), version governance + composite-hash composition, reconciliation logic with tolerance bands, capacity calculator, momentum-score auto-trim ranking, decision diary writer, vacation mode handler (entries blocked, exits continue), capital-event reset logic, data quality validation, signal storm threshold formula, vol regime detector, daily liveness probe handler
- **Integration tests required:** strategy logic against historical data, broker connectivity (mock and live-paper), full kill-switch flow including all state transitions, full signal-to-fill round trip, QC adapter golden-test parity (weekly cron), vectorbt-vs-LEAN parity (weekly cron — flag P&L divergence > 0.1% or trade count mismatch as P0), per-service degradation matrix scenarios, **continuous-vs-physical contract reconciliation at futures roll dates**, hot-fix auto-rollback simulation
- **CI gates ALL PRs.** Failed tests block merge.
- **Pre-merge gates:** tests pass, `ruff` linting, `mypy --strict`, `gitleaks` for secrets, no risk-engine modification without `risk-review-approved` label, **hot-fix forbidden-path linter** blocks PRs from agent that touch FORBIDDEN paths

### Performance Targets

- Phase 1 single strategy: backtest Sharpe ≥ 1.5; live Sharpe ≥ 0.8 over 6 months; max DD ≤ 15%; signal acceptance ≥ 90%
- Phase 2 portfolio: live Sharpe ≥ 1.2
- Phase 3 portfolio: live Sharpe ≥ 1.5
- Drift alerts when live underperforms backtest by > 1 SD over 30+ days
- Auto-decommission floor (above)

### Operating Cost Envelope (LOCKED)

| Cost Category | Monthly | Notes |
|---|---|---|
| QuantConnect (Phase 1) | $20–80 | $20 default, $80 if backtest queue bottlenecks |
| Polygon.io (Phase 2 contingent) | $0 or $30 | Only if QC equity bundle has gaps |
| Hetzner VPS primary (CCX13 or similar, 4 vCPU / 8 GB) | $20–40 | |
| Hetzner external watchdog (CX11) | $5 | Different region |
| S3 / Backblaze B2 backups | $1–3 | Object Lock retention |
| Anthropic Claude API (agent) | $30–100 | Capped via aggressive prompt caching |
| Domain registration | $1 | Amortized annual |
| Email service (Resend or SES) | $1–5 | Low volume |
| IBKR market data | $0–30 | Most free; specific exchange subs as needed |
| GitHub | $0 | Personal account |
| **Total target** | **$80–290/month** | |
| **Soft alert ceiling** | **$200/month** | Alert if 30-day rolling > $200 |
| **Hard alert** | **$300/month** | System enters cost-review state if 30-day rolling > $300 |

System tracks actual spend monthly via provider billing API or CSV; surfaces in System page.

## YOUR DELIVERABLE

Produce a complete, production-grade backend technical specification covering ALL sections below. Use Mermaid for diagrams. Be specific and concrete; do NOT punt with phrases like "use industry best practices" — name the practice, the library, the configuration. Where genuine implementation choices remain, present 2–3 options with tradeoffs and a recommendation.

**For frontend contract dependencies (page list, component data needs, action surface):** the parallel frontend spec (Prompt B) defines six post-auth pages (Today, Trades, Performance, Research, System, Calendar) plus pre-auth surfaces (`/login`, `/setup`, `/recover`), a Discord bot with specific slash commands, and a single multiplexed SSE channel `/api/sse/events` with event types (`signal`, `fill`, `position`, `pnl`, `risk_state`, `health`, `alert`, `audit`, `agent`, `vacation`, `watchdog`). Reference these by name. Where the frontend contract is genuinely undefined, flag with `[CONTRACT — verify against Prompt B]` and proceed with expected contract.

### 1. System Architecture Overview
- High-level system diagram (Mermaid) showing all services, data flow, external integrations, external watchdog topology
- Service inventory
- Phase 1 vs. Phase 2 architectures explicitly
- Migration path step-by-step including pre-cutover checklist and abort conditions

### 2. Component Breakdown
For each component (data ingestion, storage, signal engine, risk engine including position sizing algorithm, execution engine, reconciliation, monitoring, agent, scheduler+calendar combined, audit service, QC adapter, watchdog, Gitea mirror, slippage calibration service):
- Purpose, inputs, outputs, dependencies, configuration, failure modes (cross-ref Per-Service Degradation Matrix), implementation notes

### 3. Data Models and Schemas
Postgres DDL via Alembic migrations for every persistent entity (full list in original spec, plus `slippage_calibration_versions`, `parameter_sets`, `liveness_probes`)

### 4. API Contracts
- REST endpoints (path, method, pydantic schemas, auth)
- SSE single multiplexed channel `/api/sse/events` with all event types specified
- Discord bot commands and button payloads
- Internal HTTP-IPC payloads (backend → bot)
- Webhook payloads (QC ObjectStore poll, backend → email backup, external watchdog ping push)
- Idempotency key conventions including locked `client_order_id` format

### 5. Sequence Diagrams (Mermaid)
At minimum (full list in deliverable; augment with):
- Position sizing full algorithm: inverse-vol → per-position cap → cluster shrink-iterate → gross/net cap → lot rounding
- Slippage recalibration as versioned event (no paper-day reset)
- Daily liveness probe → engagement registration
- Hot-fix auto-deploy → 30-min metric watch → rollback or commit
- HALT_NEW max-dwell escalation at 7 trading days
- IBKR margin-call edge case (system at HALT_NEW, broker force-liquidates outside system control)
- Cutover scheduling and abort flow
- Phase 1 → Phase 2 cutover execution

### 6. Error Handling Strategy
- Categorization (transient / persistent / catastrophic)
- Per-Service Degradation Matrix realization
- Idempotency for order placement and audit writes
- Specific handling: IB Gateway daily restart, broker disconnect, data feed dropout, exchange halts, Claude API outage, QC ObjectStore unavailability, Hetzner outage, dividend ex-date tolerance widening, CME settlement delay

### 7. Observability
- Logging schema (structlog JSON, fields per category)
- Metrics inventory (Prometheus or equivalent)
- Health check endpoints consumed by external watchdog
- Dashboard recommendation (specific tool)
- How Claude ops agent consumes telemetry
- Alert routing logic by severity (P0/P1/P2; Defensive Risk Envelope escalated routing)
- Cost tracking integration

### 8. Security
- sops + age implementation, file layout, age key backup, rotation
- Postgres role hierarchy with break-glass procedure
- File permissions / service user model
- Network exposure
- API authentication for web frontend
- **Audit log immutability via row triggers + EVENT TRIGGER for TRUNCATE + REVOKE TRUNCATE; explicit acknowledgment of row-trigger TRUNCATE limitation**
- Backup encryption keys
- Repo / build-chain DR (Gitea + S3)
- Account recovery procedure
- GitHub workflow (branch protection, agent feature-branch flow, GitHub App install token for PR sync)
- sops file structure (separate per environment)

### 9. Deployment Topology
- VPS specs (Hetzner Ashburn — recommend size with justification)
- External watchdog topology
- Docker Compose layout (separate Discord-bot service + webhook-pusher service on shared internal network)
- Environment configuration (dev local, paper, live)
- Deployment procedure (manual + agent-driven hot-fix paths; whitelist enforcement; pre-merge linter for forbidden paths)
- Rollback procedure
- DR runbook (TWS, IBKR phone, Gitea-based rebuild)

### 10. Testing Strategy
- Unit and integration test inventory (full list above)
- CI/CD pipeline (GitHub Actions)
- Pre-merge gates including hot-fix forbidden-path linter
- Strategy validation pipeline (paper-minimum mechanical enforcement; CI blocks deploy-to-live on `paper_days_for_version < 30`)
- vectorbt-vs-LEAN parity test design
- QC adapter golden-test parity design
- Continuous-vs-physical contract reconciliation at futures rolls
- Slippage calibration verification

### 11. Phased Build Plan
- **Phase 0 (weeks 0–8):** foundation; paper begins week 1; QC adapter coded + golden-tested by week 4; 30 paper days complete within weeks 1–7; week 8 buffer; extends if 30-day minimum slips due to holidays
- **Phase 1 (months 2–5):** live track record on QC; custom backend skeleton in parallel
- **Phase 2 (months 5–9):** custom infra hardening; LEAN Local; ib-async; paper validation; cutover with pre-cutover checklist
- **Phase 3 (months 9–12):** capital scaling; second-strategy preparation; legal structure
- Each phase: deliverables, success criteria, kill criteria

### 12. Claude Ops Agent Detailed Spec
- Trigger model (cron, event-driven, on-demand from Discord)
- Tool inventory (bounded actions with parameters; hot-fix whitelist; defensive trim invocation)
- Prompt-cache strategy
- Cost budget and monitoring (~$30–100/mo target; alert if exceeded)
- Failure mode handling (Claude API outage degrades to read-only; trading continues; hallucination detection; rate limits)
- Audit trail of every agent decision with prompt + response
- Rollback mechanism for hot-fixes (auto-rollback metrics above)
- Operator-Friendly PR Review Surface — full rendering spec

## FORMAT REQUIREMENTS

- Markdown with clear section headers
- Mermaid for ALL diagrams
- Concrete library/tool/version recommendations
- Where implementation choices remain, present 2–3 options with tradeoffs and recommendation
- Length will be substantial; favor completeness over brevity
- Never invent strategic decisions; flag missing context with `[QUESTION FOR OPERATOR: ...]`
- For frontend contract dependencies, flag with `[CONTRACT — verify against Prompt B]` and proceed with expected contract; reference Prompt B's IA (six post-auth pages + pre-auth surfaces), SSE event types, and Discord command schemas by name

Begin.
