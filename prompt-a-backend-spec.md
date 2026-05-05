# PROMPT A — BACKEND TECH SPEC

## ROLE

You are a senior systems architect with deep production experience designing and building algorithmic trading infrastructure for small systematic CTAs and prop shops. You have shipped multiple live trading systems handling real money. You understand the difference between a research notebook and a system that doesn't lose track of an order at 9:31am Monday morning.

You are producing a comprehensive technical specification for the BACKEND of a single-operator algorithmic trading system. The system will be implemented primarily by Claude Code (an AI pair programmer) working with a non-technical solo operator. Your spec must be detailed and unambiguous enough that the implementing engineer never needs to make strategic decisions — only implementation choices.

## OPERATOR CONTEXT

- Solo trader, finance background (3 years banking, BS finance, SIE certification only — no Series 7/63/65/66)
- **No coding ability.** Operator does NOT author strategy or system code; relies on AI pair-programming.
- $30–35k total capital pool ($15–25k initial live trading, $10–15k reserve)
- Will add up to $250k of family capital after 12+ months of clean live track record AND legal structure (LLC + securities lawyer consult) is in place
- Goal: 6–12 month track record sufficient to qualify for prop firm allocation or first F&F commit
- Located in NJ, USA (US person, US tax)
- Trades alone for the first 12 months; no second operator
- **Operator's "upskilling" (~5–8 hrs/week for first ~8 weeks)** = reading and understanding code Claude Code produces; ratifying or pushing back; learning Python basics, cloud, git for OPERATIONAL competence (deploy, restart, read logs). NOT to author strategy logic. v1 strategy is authored by Claude Code with operator review/approval; operator never types strategy code.

## LOCKED STRATEGIC AND SYSTEMS DECISIONS — DO NOT REOPEN

The decisions below are not advisory. They are constraints. Architect within them. If a section appears underspecified, default to the conservative interpretation and flag with `[CONSERVATIVE DEFAULT: ...]` rather than substituting your own preference.

### Canonical Session Calendar (referenced throughout — TERMINOLOGY CORRECTED)

**The canonical session calendar is the CME 23-hour Globex session** (Sun 18:00 ET → Fri 17:00 ET with daily 17:00–18:00 ET maintenance pause). Throughout this spec we abbreviate this as **"CME session"** (not "CME RTH" — which has an industry meaning of cash-session 09:30–16:00 ET that we do NOT use). Used for:
- 17:00 ET daily MTM anchor
- CONVALESCENT 5-session counter
- Capital-event mode 30-session counter
- **Paper-day counter (30-session paper minimum) — CME sessions, NOT NYSE**
- Cutover scheduling
- Trading-day counts in general

Where a behavior is specifically ETF-related (PDT rule, ETF order placement, NYSE exchange holiday), the **NYSE calendar** is used.

Both calendars read from `pandas_market_calendars` (or equivalent). Implementer must explicitly choose CME or NYSE calendar per behavior — never conflate.

### Strategy

- **Phase 1 strategy:** multi-asset systematic trend-following on micro futures + bond ETFs
- **Universe (canonical full target at scale):** ~8–12 markets — equity index micros (`/MES`, `/MNQ`, `/M2K`, `/MYM`), commodity micros (`/MCL`, `/MGC`, `/SIL` micro silver — verify CME ticker against QC's symbology in Phase 0; resolve to whatever QC accepts; **NOT `/MSI`**), Bitcoin micro (`/MBT`), bond ETFs (TLT, IEF, SHY); optional FX micros (`/M6E`)
- **Phase 1 sub-universe:** verified during Phase 0 weeks 0–2 (single window; finalized by end of week 2). Two filters apply:
  - **(a) Data executability:** market must have sufficient QC bundled data; failure → exclude (likely /M6E)
  - **(b) Per-position-cap-feasibility (see Per-Position Cap Override below):** market must satisfy 1-contract-notional ≤ 50% of current equity; failure → exclude at this equity tier
- **Signal type:** time-series momentum / breakout (Donchian channels, MA crossovers); vol-targeted sizing; daily bars
- **Daily bar definition (locked, per asset class):**
  - Futures: close = **CME daily settlement, 17:00 ET**
  - ETFs (TLT, IEF, SHY): close = **NYSE close, 16:00 ET**, with **dividend back-adjustment computed on-the-fly at signal time** from raw bars + dividend history table; **raw bars never restated** (preserves audit reproducibility at CPU cost)
- **Signal generation cadence and per-market wait policy:**
  - Scheduler runs at **17:30 ET** after both close anchors
  - Per-market: if settlement available, generate signal immediately; else retry every 5 min until 18:00 ET (30-min tolerance)
  - 18:00 ET (30 min late): use last available bid/ask midpoint with `unsettled` flag; proceed
  - 18:30 ET (60 min late): drop signal generation for that market that day (`market_drop_settlement_unavailable`); other markets unaffected
  - Partial signal generation across markets is normal
- **Order placement timing (locked):**
  - Futures: orders **queued** at 17:30 ET signal cycle; **placed at next CME session start** (typically ~18:00 ET same evening after maintenance pause). Macro pause windows apply to PLACEMENT.
  - ETFs: orders **queued** at 17:30 ET; **placed at next NYSE 09:30 open**. Macro pauses apply.
  - If pause + 60-min staleness exceeds session, signal dropped (`macro_window_drop`).
  - Both: limit-marketable at the open with widening retry per Execution Mechanics.
- **Holding period:** **minimum 14 days** (per `MIN_HOLDING_DAYS` default of **14**, locked); **typical realized** 2 weeks to 6 months; max bound by stop-out, signal reversal, decommission.
- **Phase 2+:** add second uncorrelated strategy only after Phase 1 live validation; sequential addition
- **Base currency:** USD only. No FX hedging. `/M6E` (if active) settles in USD via IBKR auto-conversion.
- **Account model:** single live IBKR Pro account. Schema includes `account_id` foreign key throughout from day 1; multi-account = INSERT + sops file + service config update (no migration).

#### Per-Position Cap Override / Single-Contract Infeasibility (CRITICAL — locked)

The 25% per-position cap is a TARGET ceiling. **At small equity, integer-contract minimums make the 25% cap infeasible for many markets** (e.g., 1 contract of /MES at ~$26.5k notional is >100% of $20k equity). The position-sizing algorithm handles this with a TWO-RULE override:

**Hard exclusion rule (applied during Phase 1 sub-universe verification AND continuously thereafter):**
- A market is EXCLUDED from the active universe at any equity tier where `single_contract_notional > 0.50 × current_equity` (the 50% single-contract threshold)
- Exclusion is automatic on equity changes; logged as `audit_event_type = universe_exclusion` with `reason = single_contract_notional_exceeds_50pct_equity`
- As equity grows (capital events, P&L), markets re-enter automatically when threshold is crossed

**Soft override (within active universe):**
- Markets within the active universe are permitted to violate the 25% per-position cap up to the 50% hard floor (i.e., 1 contract may produce 25–50% concentration)
- Stage 5 lot-rounding still applies; `sub_minimum_size` drop is reserved for edge cases where vol-targeting would request 0 contracts even allowing the 1-contract override

**Worked example tables (illustrative; QC verification confirms in Phase 0):**

Approximate single-contract notional (will vary with prices; recompute at runtime):

| Market | Notional/contract | Min equity for inclusion (notional ≤ 50% × equity) |
|---|---|---|
| /MCL | ~$6,000 | ~$12,000 |
| /MBT | ~$9,500 | ~$19,000 |
| /M2K | ~$11,000 | ~$22,000 |
| /MYM | ~$20,000 | ~$40,000 |
| /MGC | ~$24,000 | ~$48,000 |
| /MES | ~$26,500 | ~$53,000 |
| /SIL | ~$30,000 | ~$60,000 |
| /MNQ | ~$36,000 | ~$72,000 |
| TLT, IEF, SHY (ETFs) | per-share, fits any equity | always in |

**Resulting accessible-universe by equity tier (Phase 1 starting equity $15–25k):**
- $15k: /MCL + ETFs (~4 markets) — minimal but functional
- $25k: + /MBT + /M2K (~6 markets) — workable
- $50k: + /MYM, /MGC, marginal /MES (~8 markets)
- $100k+: full canonical universe

**Signal acceptance rate denominator (refined to fit):**
`signal_acceptance_rate = orders_placed / signals_emitted_post_data_quality_filter_AND_post_universe_filter`
The denominator EXCLUDES signals for markets currently outside the active universe (universe-excluded markets don't generate signals; nothing to count). Phase 1 signal acceptance ≥ 90% target is structurally achievable under this definition.

#### Position Granularity (locked)
- Futures: contract-level (per expiration); risk rings combine notional across active expirations of same root (during roll, old + new count together)
- ETFs: symbol-level
- Roll-window double-exposure: brief overlap; if cap binding, roll completes via momentum-ranked partial reduction of front-month before adding back-month

#### PDT / Reg T (locked, refined)
- Futures use SPAN; PDT does not apply.
- ETFs use Reg T (50% initial, 25% maintenance). PDT rule applies while account equity < $25k.
- **PDT pre-check:** on any new ETF entry, if `account_equity < $25,000 AND rolling_5_NYSE_session_day_trade_count >= 3`, refuse entry. Conservative under-trade.
- **PDT day-trade count source:**
  - **Phase 1:** QC algorithm pushes intraday day-trade count to ObjectStore (with each fill); backend reads cursor + cross-references with FlexQuery EOD. Phase 1 backend has NO direct TWS connection.
  - **Phase 2:** TWS API real-time + FlexQuery EOD per main reconciliation source-of-truth
- Portfolio Margin not in scope (requires $125k+).

#### Sharpe Definition (canonical)
- Annualization: 252
- Risk-free rate: 0
- Returns: daily close-to-close based on 17:00 ET MTM
- `Sharpe = mean(daily_returns) × sqrt(252) / stdev(daily_returns)`
- "X-day rolling Sharpe" uses last X CME sessions

#### Signal Acceptance Rate Definition (locked, fixed for math feasibility)
`signal_acceptance_rate = orders_placed / signals_emitted_post_data_quality_filter_AND_post_universe_filter`
- **Numerator:** signals that resulted in actual broker order placement (NOT capacity-constrained-to-zero, NOT operator-rejected, NOT refused by PDT/risk pre-check, NOT macro-window-dropped, NOT sub-minimum-size-after-rounding)
- **Denominator:** signals emitted by strategy after data-quality validation AND after universe filter (universe-excluded markets generate no signal; not in denominator); includes `unsettled`-flag signals
- Target: ≥ 90% (Phase 1)

### Path / Phasing

- **Phase 0 (weeks 0–8, holiday-buffered):** foundation — operator upskilling for OPERATIONAL competence (read logs, deploy, restart; NOT author code), IBKR Pro account opening, QC subscription, repo + CI scaffolding (**v1 strategy code authored BY Claude Code with operator review/approval**), secrets management (sops), Hetzner VPS provisioned, audit schema designed and migrated. **Paper trading begins on QC week 1 with v1 strategy. Phase 1 sub-universe verification completed by end of week 2 (data executability + per-position cap feasibility per current equity). QC ObjectStore audit adapter coded and golden-tested by week 4. 30 CME paper sessions completed within weeks 1–7 (calendar buffer absorbs 1–2 holidays). Week 8 buffer + Phase 1 handover.** If 30-session minimum slips past week 7, Phase 0 extends.
- **Phase 1 (months 2–5):** live trading on QuantConnect Cloud (LEAN). Real money, small size (`live-small`). Track record begins.
- **Phase 2 (months 5–9):** custom infrastructure built and hardened; LEAN Local + vectorbt research; track record continuous via QC adapter audit ingestion.
- **Phase 3 (months 9–12):** capital scaling, second-strategy preparation, family-money legal structure.
- **Phase 1 → Phase 2 cutover:** operator selects date ≥5 **CME sessions** in advance; pre-cutover automated checklist; abort on any check fail OR HALT_NEW state in 24h prior; cutover at session close → flatten on QC → restart fresh on LEAN Local next morning. **No position transfer.** Audit log continuous.

### Tech Stack (locked)

- **Language:** Python 3.11+ end to end
- **Engine:** LEAN (QC Cloud Phase 1; LEAN Local Phase 2). LEAN authoritative for backtest PR review surface. vectorbt research-only.
- **Storage:** DuckDB on Parquet (analytics); PostgreSQL 16 (transactional; asyncpg + SQLAlchemy 2.x async; Alembic)
- **Broker library:** `ib-async >= 0.9.x, < 2.0`. **Phase 1: NO direct IBKR connection** (market data + broker state via QC algorithm push to ObjectStore). **Phase 2: direct via `ib-async` to IB Gateway in Docker.**
- **Margin model:** SPAN for futures (broker-computed); Reg T for ETFs.
- **"Used margin" canonical:** `used_margin_pct = 1 − (ExcessLiquidity / NetLiquidation)` from IBKR `accountSummary`.
- **Orchestration:**
  - **cron** = OS-level housekeeping ONLY (daily backup, log rotation, cert renewal, NTP)
  - **APScheduler** within Python services = trading loop, signal generation, reconciliation, slippage recalibration; persistent Postgres-backed job store
  - DST-aware: scheduler is wall-clock ET via `zoneinfo.ZoneInfo("America/New_York")`; session-counted windows use canonical session calendar
- **Real-time push:** SSE for browser one-way push (single multiplexed `/api/sse/events`); REST otherwise; NO WebSocket
- **Deployment:** Single VPS, Hetzner Cloud Ashburn, Ubuntu LTS, Docker Compose. NO Kubernetes.
- **Frontend co-located on same VPS** (no Vercel)
- **Reverse proxy:** Caddy (auto Let's Encrypt)
- **Process supervision:** Docker Compose restart policies + systemd; chrony for NTP
- **Logging:** `structlog` JSON renderer; local file via logrotate (daily rotation, 30-day local retention); compressed daily copy uploaded to S3 (90-day S3 retention). NO central log aggregator Phase 1.
- **Validation:** pydantic v2
- **API exposure:** FastAPI on the VPS

### Data Sources (locked, with criticality flags)

- **Phase 1:** QuantConnect bundled equities + futures data (Phase 1 sub-universe verified at Phase 0 weeks 0–2); IBKR real-time market data **routed through QC** (Phase 1 backend has no direct IBKR connection); **economic calendar via Forex Factory or Trading Economics — IS critical-path from Phase 1** (used for tier-1 macro pause auto-detection); **FRED — non-critical from Phase 1** (macro context display only).
- **Phase 2 additions:** Polygon.io Stocks Starter ($30/mo) **only if** QC bundled equity data has notable gaps in Phase 1 live (else $0); direct IBKR market data via TWS API.
- **CRITICALITY:**
  - FRED: NICE-TO-HAVE. Outage → degraded macro context display only. No halt.
  - Economic calendar: CRITICAL from Phase 1. Outage > 48h → hard halt new orders (severity=routine HALT_NEW) next session until manual ratification.
- **Tier-1 macro event taxonomy (source-agnostic):** system maintains its own tier-1 list (FOMC, CPI, NFP, GDP, PCE, ECB/BOJ/BOE if exposed, OPEC if /MCL exposed); matches against feeds by event-name pattern.
- **NOT in scope:** Norgate, alt data, NLP feeds, Bloomberg, Databento, multi-tier feeds.
- **Data correctness claims (per leg):**
  - **ETFs/equities:** QC bundled is survivorship-bias-free
  - **Futures:** roll methodology (Panama / open-interest); LEAN execution uses physical contracts; backtest continuous-vs-physical reconciliation at roll dates is mandatory test
- **ETF dividend handling (locked, on-the-fly):** dividends back-adjusted **on-the-fly at signal time** from raw bars + `dividend_history` table; raw bars never restated. Reinvested into MTM at ex-date; tracked separately as `dividend_pnl` in attribution. Reconciliation tolerance widens 2× during ex-dates for +24h.

### Risk Framework (concrete math; locked)

#### Position sizing — full algorithm (locked)

**Stage 0 — Active universe filter (NEW):**
- Apply per-position cap override / single-contract infeasibility rule (above)
- Mark each market as `active_in_universe: bool` based on current equity
- Markets where `active_in_universe = false` skip all subsequent stages and emit no signal

**Stage 1 — Inverse-vol weighting (unconstrained):**
```
For each active market i:
  σ_i = rolling INSTRUMENT_VOL_LOOKBACK_DAYS-day stdev of daily log returns (default 60d)
  raw_weight_i = 1 / σ_i
  total = Σ raw_weight_j (over all active j)
  unconstrained_weight_i = raw_weight_i / total
  unconstrained_notional_i = unconstrained_weight_i × (effective_vol_target / portfolio_realized_vol_at_unconstrained_weights) × equity
```

**Covariance / Σ estimator (locked):** 60-day rolling sample covariance matrix from daily log returns; **no shrinkage Phase 1** (Ledoit-Wolf consideration Phase 2+); asynchronous closes handled by using each market's own daily-close return series with its asset-class anchor (futures 17:00 ET, ETFs 16:00 ET); missing data dropped from estimation pair-wise. Same Σ used for portfolio realized vol (Stage 1) and the realized-correlation kill-switch ring.

`effective_vol_target = m_combined × VOL_TARGET_PCT_ANNUAL / sqrt(252)` (daily target). See Vol-Target Multiplier Composition.

**Stage 2 — Per-position cap (with 50% override floor for sub-$50k equity):**
```
target_cap_i = 0.25 × equity (the 25% target)
hard_floor_i = 0.50 × equity (single-contract override ceiling)
capped_notional_i = min(unconstrained_notional_i, max(target_cap_i, single_contract_notional_i if single_contract_notional_i ≤ hard_floor_i else 0))
```
In plain English: prefer the 25% cap; permit up to 50% only if 1-contract minimum forces it; if 1 contract exceeds 50%, market should not be in active universe (caught at Stage 0).

**Stage 3 — Per-cluster cap (iterative shrink-to-fit, locked convergence):**
```
For each cluster c with cap C_c:
  cluster_total = Σ |capped_notional_i| for i in cluster c
  if cluster_total > C_c × equity:
    scale = (C_c × equity) / cluster_total
    for i in cluster c: capped_notional_i *= scale
Re-apply per-position cap (Stage 2) after cluster scaling.
Iterate up to 10 times; tolerance 0.1% (all caps satisfied within 0.1%).
On non-convergence at 10 iterations: drop the lowest-momentum signal in the binding cluster and restart.
"Lowest momentum" = same metric as margin auto-trim: rolling 60-day z-score of returns, ascending = weakest.
```

**Stage 4 — Gross/net caps:**
```
gross = Σ |capped_notional_i|
if gross > 3.0 × equity: uniform shrink × (3.0 / (gross/equity))
net = Σ capped_notional_i
if |net| > 1.5 × equity: uniform shrink × (1.5 / (|net|/equity))
Re-apply per-position and per-cluster caps.
```
Net cap rationale: deliberately conservative; under-realizes synchronized-trend payoffs in exchange for bounded directional exposure.

**Stage 5 — Lot-size rounding:**
```
contract_count_i = capped_notional_i / (point_value_i × multiplier_i)
rounded_i = round_to_nearest_integer(contract_count_i, banker's_rounding)
if rounded_i == 0 (edge case after override): drop signal; tag 'sub_minimum_size'
realized_notional_i = rounded_i × point_value_i × multiplier_i
rounding_deviation_i = (realized_notional_i - capped_notional_i) / capped_notional_i
Track in attribution.
```

#### Vol-Target Multiplier Composition (LOCKED)

Each reduction has multiplier `m ∈ (0, 1]` such that `effective_vol_target = m_combined × VOL_TARGET_PCT_ANNUAL`.

| Reduction | Multiplier | Active when |
|---|---|---|
| `m_capital_event` | 0.5 | First 5 CME sessions of capital-event mode (sessions 1–5); 1.0 sessions 6–30 |
| `m_convalescent` | 0.5 | During CONVALESCENT state; 1.0 otherwise |
| `m_monthly_dd` | 0.5 | Remainder of calendar month after monthly DD threshold (-10%) breached; 1.0 otherwise |

**Combined:** `m_combined = min(m_capital_event, m_convalescent, m_monthly_dd, 1.0)` — MIN, not compounded.

#### Capital-Event Mode (locked, asymmetry made explicit)

| Capital event | Trailing DD reset? | Capital-event mode timer | Vol multiplier |
|---|---|---|---|
| Deposit ≥ 5% of current equity | YES — reset to current equity at deposit time | Starts; 30 CME sessions | `m_capital_event = 0.5` sessions 1–5; 1.0 sessions 6–30 |
| Withdrawal ≥ 5% of current equity | NO — peak MTM unchanged (no perverse incentive to withdraw and reset DD) | Starts; 30 CME sessions | Same multiplier behavior |

Deposits / withdrawals < 5% of equity: no capital event; no mode triggered.

Mode-active flag persists for sessions 6–30 (after vol multiplier normalizes) for: trailing-DD baseline tracking from event date, audit tagging of trades during window. Auto-deactivates session 31+.

#### Equity and DD Anchors

- **Daily-start MTM anchor:** 17:00 ET, portfolio-wide. Daily P&L = `MTM(t) − MTM(prior 17:00 ET)`.
- **17:00 ET ETF price source (locked):** **last NYSE close (16:00 ET)** — most defensible; no extended-hours quote.
- **Trailing DD reference:** peak intraday MTM since system inception, subject to capital-event reset on deposit only.
- **Intraday MTM cadence (locked):** every 60s during CME session (matches reconciliation freshness SLO). Data source: Phase 1 = QC algorithm push to ObjectStore; Phase 2 = TWS API portfolio snapshot.

#### Risk Rings

| Ring | Limit | Measurement Basis |
|---|---|---|
| Per-position max | 25% target / 50% hard floor for single-contract override | Sum of \|notional\| for that single market (combined active expirations for futures) |
| Gross portfolio max | 300% equity notional | Sum of \|notional\| across all positions |
| Net portfolio max | 150% equity notional (deliberately conservative) | Signed sum |
| Equity-index cluster max | 60% gross | Combined `/MES`, `/MNQ`, `/M2K`, `/MYM` |
| Commodity cluster max | 80% gross | Combined `/MCL`, `/MGC`, `/SIL` |
| Rates/bonds cluster max | 80% gross | Combined TLT, IEF, SHY |
| Crypto cluster max | 40% gross | `/MBT` |
| FX cluster max | 30% gross | `/M6E` and any future FX micros |
| Realized cross-portfolio correlation | Alert at avg pairwise > 0.7; HALT_NEW at > 0.85 | Same Σ as position sizing |
| Daily loss limit | -5% of daily-start MTM | 17:00 ET anchor |
| Trailing drawdown limit | -20% from peak intraday MTM | Capital-event reset on deposit only |
| Monthly DD threshold | -10% in calendar month | Activates `m_monthly_dd = 0.5` for remainder of month |
| Strategy decommission floor | HALT_NEW (severity=incident_review) + human review | (a) live 30-day Sharpe < 0, OR (b) live max DD ≤ -25%, OR (c) 60-day live Sharpe underperforms backtest by > 2 SD |

**Decommission floor SD baseline (locked):**
- Pre-Phase-1 live AND Phase-1 live days 1–179: empirical SD of 30-day rolling Sharpes from walk-forward folds during backtest
- Phase-1 live days 180+: empirical SD of rolling 30-day windows from live track record
- Same baseline for auto-revert thresholds

**Decommission workflow:**
1. State → HALT_NEW with `severity=incident_review`
2. Strategy version flagged `decommissioned` in `strategy_versions`
3. Audit entry with provenance
4. Post-incident review write-up (separate `incident_reviews` table — see schemas) logged before resume
5. Resume: explicit operator override + audit justification, OR new strategy version deployment (resets `paper_days_for_version`; 30 new CME paper sessions required)

#### Vol Regime Detector
- Metric: 60-day rolling realized vol of portfolio daily returns
- Z-score: vs. own 60-day historical distribution (250 samples)
- Trigger: z > 2 → HALT_NEW

#### Signal Storm Detector
- `session_count > max(5, 3 × rolling_90_day_mean_daily_trade_count)`
- Floor of 5 prevents low-baseline trip

#### Margin Protocol — Graduated De-leverage

- 70% used → warn alert
- 85% used → auto-trim sequence:
  1. Compute momentum score per open position (rolling 60-day z-score of returns); rank ascending
  2. Tie-break: largest absolute margin contribution
  3. Cut via marketable-limit (1× spread, escalating to 2× on retry)
  4. **Hard cap: -30% of gross exposure across the entire sweep**
  5. Cut until used margin < 60% OR session cap reached
  6. If used margin still > 80% after sweep → escalate to HALT_NEW; no further trims

**Acknowledged residual risk:** at HALT_NEW with high used margin, **IBKR may force-liquidate outside system control.** Alert text at HALT_NEW-due-to-margin must call this out.

#### Capacity Tracking
- Rolling 30-day ADV per market
- Order size as % of ADV at signal-emit
- Alert at 0.5%; partial-fill cap at 2% (size to 2% ADV; remainder tagged `capacity_constrained`)

### Kill-Switch State Machine

States:
- **NORMAL** — full operation
- **HALT_NEW** — cancel all working orders; hold positions (no system-initiated liquidation); no new entries; all exits continue (stops, profit-targets, manual close); manual human resume
- **CONVALESCENT** — `m_convalescent = 0.5`; entries permitted; 5 CME sessions; auto → NORMAL on completion

**HALT_NEW severity flag (REPLACES "hard halt" terminology — audit all uses):**

`severity` enum on HALT_NEW transitions:
- `severity=routine` — standard kill-switch trigger; standard alert routing
- `severity=defensive_envelope` — comms breakdown trigger (heartbeat engagement failure, calendar service outage > 48h, QC ObjectStore unavailable > 10 min); escalated alert routing (email backup priority + external watchdog notify + Discord retry cadence increased)
- `severity=incident_review` — formerly "hard halt." Triggers:
  - Audit log write failure
  - Postgres data corruption / hash chain integrity break
  - Decommission floor trigger
  
  Behavior: HALT_NEW state PLUS:
  - Immediate full database snapshot to S3 (not just WAL)
  - Page operator via all channels
  - Auto-resume permanently disabled; resume requires post-incident review write-up logged to `incident_reviews` table before re-auth permitted
  - All-channel alert with explicit "incident review required" language

**Throughout this spec, "hard halt new orders" (which appears in earlier prose for calendar-ratification-missing path) is realized as HALT_NEW (severity=routine).** Do not use the phrase "hard halt" as if it were a separate state.

**HALT_NEW max dwell:** 7 trading days → daily reminder escalation; never auto-flatten.

Transitions:
- `NORMAL → HALT_NEW`: any trigger fires (severity per trigger taxonomy)
- `HALT_NEW (routine|defensive_envelope) → CONVALESCENT`: human resume (re-auth, web-only)
- `HALT_NEW (incident_review) → CONVALESCENT`: human resume + audit-logged review write-up to `incident_reviews` (re-auth, web-only)
- `CONVALESCENT → NORMAL`: 5 CME sessions complete without breach
- `CONVALESCENT → HALT_NEW`: any trigger fires; counter resets on next resume

#### CONVALESCENT Counter — Reset/Independence

| Event | CONVALESCENT counter |
|---|---|
| Any kill-switch trigger fires while in CONVALESCENT (returns to HALT_NEW) | RESET; new 5-session counter on next resume |
| Heartbeat engagement timeout (kill-switch trigger, severity=defensive_envelope) | RESET (consistent) |
| Reconciliation false-positive within tolerance (no state transition) | NO CHANGE |
| Calendar ratification grace (no state transition) | NO CHANGE |
| Capital event (deposit ≥ 5% equity) | **NO RESET** to CONVALESCENT counter; capital event starts ITS OWN INDEPENDENT 30-session timer; both run independently; vol multipliers compose via MIN |

#### Kill-Switch Triggers (any → HALT_NEW)
- Trailing DD breach [routine]
- Daily loss breach [routine]
- Signal storm [routine]
- Reconciliation mismatch (delta exceeds tolerance) [routine]
- Broker disconnect > 5 min during CME session [routine]
- Vol regime z > 2 [routine]
- Realized cross-portfolio correlation > 0.85 [routine]
- Decommission floor [incident_review]
- Audit log write failure [incident_review]
- Postgres data corruption / hash chain break [incident_review]
- Any unhandled exception in execution path [routine]
- Heartbeat engagement failure [defensive_envelope]
- Calendar service outage > 48h or unratified calendar at 23:00 ET cutoff [routine; `reason=calendar_unratified` or `calendar_service_outage`]

### Vacation Mode

- `/vacation start [days]` in Discord
- Engagement timeout extends to 7 days
- NEW position entries auto-disabled. EXIT logic continues.
- Pending working orders cancelled at vacation start; existing positions hold; stops + profit-targets remain active
- Daily summary + liveness probe still post
- Macro-event ratification gate suspended
- `/vacation end` (web-only, re-auth required) or expiry resumes

### Risk-Tightening Boundary

Two paths:
1. **Parameter changes** (within range, tighten direction): take effect at NEXT signal cycle, never mid-session
2. **Defensive position trims:** mid-session direct order action; capped at -30% gross per session; **causally agent-initiated, mechanically placed by risk engine** (which holds broker creds in Phase 2; Phase 1 trims execute via QC algorithm-side trim logic triggered by audit-driven instruction)

#### Per-Parameter "Tighten" Direction (LOCKED)

| Parameter | Tighten | Rationale |
|---|---|---|
| `LOOKBACK_DAYS_DONCHIAN` | Increase | Stronger breakout required |
| `INSTRUMENT_VOL_LOOKBACK_DAYS` | n/a | Statistical estimator |
| `VOL_TARGET_PCT_ANNUAL` | Decrease | Lower vol = less risk |
| `MA_FAST_DAYS` | Increase | Slower fast MA |
| `MA_SLOW_DAYS` | Increase | Stronger trend required |
| `STOP_DISTANCE_ATR_MULT` | Decrease | Tighter stop |
| `HURST_THRESHOLD` | Increase | Stronger trend evidence |
| `ROLL_DAYS_BEFORE_EXPIRY` | Increase | Earlier roll |
| `MIN_HOLDING_DAYS` | n/a | Risk-neutral |

Agent moves WITHIN Min/Max AND in tighten direction. Loosening or n/a → human/PR.

### Auto-Revert Thresholds (parameter changes — refined for feasibility)

Auto-reverts when **any**:
- 30-day rolling live Sharpe drops > 2 SD from pre-change baseline within 30 sessions, AND minimum 30 trades on changed market(s) **for globally-applicable params only** (refined per below)
- Max DD breaches -10% within 5 CME sessions of the change
- Consecutive losing trades:
  - **Globally-applicable params** (`VOL_TARGET_PCT_ANNUAL`, `INSTRUMENT_VOL_LOOKBACK_DAYS`): Sharpe-drop AND 30-trade-window AND 5+ consecutive losers portfolio-wide
  - **Market-specific params** (lookbacks, MA, ATR, Hurst, roll, holding): Sharpe condition uses **5+ consecutive losers on affected market within window** (drops the 30-trade threshold which is structurally unreachable given `MIN_HOLDING_DAYS=14` on a single market within 30 sessions); DD condition still applies

Auto-revert: parameter restored; full audit; alert; no further auto-changes to that parameter for 14 days.

### Logic-Change vs. Parameter-Change Boundary

- **Logic change** (PR + human merge): rule logic, indicator selection, market universe, strategy structure, sizing model, risk-ring values, cluster definitions, parameter ranges themselves, hot-fix-whitelist itself, tighten direction table itself
- **Parameter change** (auto with audit, within range AND tighten direction): values within Parameter Ranges Table
- Parameter changes effective at next signal cycle, never mid-session.

### Parameter Ranges Table (LOCKED)

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
| `MIN_HOLDING_DAYS` | 5 | 21 | **14** (locked single value) | Minimum holding period before exit eligible |

Agent-mutable within Min/Max AND in tighten direction. Outside Min/Max OR loosening direction → PR.

`parameter_set_hash` SCOPE: hash over **only the parameters in this table**.

### Slippage Calibration (LOCKED — full procedure)

- Versioned `slippage_calibration_versions` table
- Recalibration is logged audit event; does NOT reset paper-day counter (doesn't change live execution)
- **Live execution: uses CURRENT HEAD `slippage_calibration_version`**
- **Backtest at PR creation: pins to current version at PR creation time**
- Trade records carry `slippage_calibration_version` alongside `strategy_hash` and `parameter_set_hash`

**Calibration procedure (locked):**
- **Functional form:** per-market, `slippage_bps = α_market + β_market × (order_size / ADV)` (linear-in-vol-adjusted-size)
- **Data source:** realized fills compared to decision price (LEAN-emitted expected_price at signal time); per-fill `realized_slippage_bps = 10000 × (actual_price − expected_price) / expected_price` (signed for buy/sell)
- **Estimator:** OLS fit of `realized_slippage_bps ~ (order_size / ADV)` per market; coefficients are α_market, β_market
- **Bootstrap (Phase 1 first month, before live fills accumulate):** initial α, β derived from QC backtest fills using the same OLS procedure on backtest data
- **Cadence:** monthly cron during Phase 1; quarterly Phase 2+ (APScheduler job)
- **Trigger condition for unscheduled recalibration:** realized > 2× modeled for any single market for 3 consecutive months → strategy review (NOT automatic recalibration; human-initiated)

### Reconciliation Tolerances Table

A delta exceeding tolerance → kill-switch (severity=routine).

| Metric | Tolerance | Grace Period |
|---|---|---|
| Position quantities (per instrument-contract) | 0 (exact) | None |
| Cash balance (USD) | greater of $5 absolute or 1 bps of equity | T+1 grace for fees, dividends, interest |
| Margin balance | $10 absolute | None |
| FX-denominated cash (intraday `/M6E`) | $1 absolute | T+1 for FX rounding |
| Realized P&L (cumulative) | $1 absolute | T+1 |
| Unrealized P&L | $5 absolute | None |

Tolerances widen 2× during dividend ex-dates for +24h.

### Per-Service Degradation Matrix

| Failure | System Response |
|---|---|
| Risk engine down | Signal engine halts; HALT_NEW (routine) |
| Reconciliation stale > 60s during CME session | HALT_NEW (routine) |
| Calendar service can't reach Forex Factory/Trading Economics | Use last successful import; alert; if last successful > 48h, HALT_NEW (routine) next session until manual ratification |
| FRED unreachable | Degraded macro-context display only; no halt |
| QC ObjectStore poll fails 5–9 min | Alert only |
| QC ObjectStore poll fails > 10 min | HALT_NEW (defensive_envelope) |
| Backend can't reach IBKR > 5 min during CME session (Phase 2) | HALT_NEW (routine) |
| Backend can't reach QC ObjectStore > 5 min during CME session (Phase 1) | HALT_NEW (routine) |
| Discord delivery fails | Email backup automatic; external watchdog covers VPS-down |
| Database write fails (non-audit) | Retry 3× with backoff; persistent failure → HALT_NEW (routine) |
| Database write fails (audit_log) | HALT_NEW (incident_review) immediately |
| Postgres corruption / hash chain break | HALT_NEW (incident_review) |
| Anthropic Claude API down | Agent service degrades to read-only; trading continues; alert |
| External watchdog unreachable | Alert; if + Discord delivery also failing, HALT_NEW (defensive_envelope) |
| CME settlement prints unavailable > 60 min past 17:00 ET | Drop signal generation for affected market that day; resume next session |

### Order Rejection Taxonomy (LOCKED)

| Rejection Reason | Behavior |
|---|---|
| Margin insufficient at order time | Halt that market for the day; alert; do NOT retry |
| Instrument unavailable / halted symbol | Wait 60s; retry once; if still unavailable, halt market for day |
| Regulatory rejection (trading halt, circuit breaker) | Halt market for day; alert; review at session end |
| Generic rejection (transient) | Retry per existing logic (3× exponential backoff: 1s, 4s, 16s) |
| Pre-trade risk check rejection (internal) | Log + alert; do NOT retry; never bypass |
| PDT pre-check rejection | Log; do NOT retry that session; signal expires |

### Data Quality Handling

Per-bar validation at ingestion:

**Reject (no action, log + alert):**
- Close ≤ 0
- OHLC contains NaN
- High < Low
- Volume = 0 for futures during relevant session
- Bar arrived > 60 min past relevant close anchor

**Quarantine (no action, log, no escalation):**
- |close − prev_close| > 10× rolling 30-day daily range
- Volume < 10% of rolling 30-day average
- Bar arrived 30–60 min past close anchor

On rejected/quarantined bar: skip signal generation for that market that day; log to `audit_log` and `data_quality_events`.

### Execution Mechanics

- **Order types:**
  - Entries: limit-marketable (last ± 0.5× spread); retry widens to 1× spread, 1.5× spread
  - Exits (stop): stop-market for execution certainty
  - Profit-target exits: limit at target
  - Futures rolls: calendar spread when broker supports; otherwise leg with 60s stagger
  - Kill-switch action: cancel working orders; **hold positions** (no liquidation)
  - Margin auto-trim and defensive trims: marketable-limit (1× → 2× retry); never market
- **Retry logic:** rejection per Order Rejection Taxonomy
- **Reconciliation source-of-truth:**
  - Phase 1 intraday: QC algorithm push to ObjectStore (positions, cash, day-trade count, margin) every 60s during CME session
  - Phase 2 intraday: TWS API real-time portfolio snapshot
  - EOD/tax/audit (both phases): IBKR FlexQuery (XML; authoritative) — Phase 1 pulled by QC algorithm, pushed via ObjectStore; Phase 2 pulled directly by backend
- **Cadence:** session open + close + EOD; weekly summary
- **Roll discipline:** futures rolled per `ROLL_DAYS_BEFORE_EXPIRY`; off-peak liquidity
- **Macro event handling:**
  - **Auto-pause applies to ORDER PLACEMENT only.** Signal generation at 17:30 ET runs regardless. Orders queued from 17:30 cycle delayed if next-session open intersects pause.
  - Pause window: 5 min before through 30 min after scheduled tier-1 events
  - If pause + 60-min staleness exceeds session, signal dropped (`macro_window_drop`)
  - Calendar imported nightly; user ratifies via Discord by 23:00 ET; default if no ratification: HALT_NEW (routine, `reason=calendar_unratified`) next session until ratified
  - Vacation mode exception: ratification gate suspended
  - Macro window vs. session boundary: pause wins

### HALT_NEW → CONVALESCENT Resume — Re-Signal Behavior

On resume:
- System waits for next 17:30 ET scheduled cycle; does NOT regenerate signals immediately
- Cancelled working orders NOT re-instantiated
- New signals at next cycle reflect current state under `m_convalescent = 0.5` (composed via MIN with other active multipliers)

### SLO Budgets (timing definitions clarified to fix designed-delay conflict)

`t_0` = **order placement attempted** (scheduler dispatches order to broker after queueing window has cleared, including any macro pause and CME maintenance pause); clock starts here
`t_1` = broker order acknowledged (order ID issued or rejection received)

- **Signal-to-order placement latency: p50 ≤ 60s, p99 ≤ 5 min (`t_1 − t_0`)** — measures the actual placement-to-ack time only, NOT the intentional queue wait between signal emission and next-session-open
- **Signal-emit-to-placement-attempt time** (`t_0 − signal_emit_time`) is NOT a bounded SLO — it can be hours (overnight queue from 17:30 ET futures signal to 18:00 ET CME re-open; ETF signals from 17:30 ET to next-day 09:30 NYSE)
- Kill-switch invocation: ≤ 5s from trigger to broker cancellation request
- Reconciliation freshness during CME session: ≤ 60s
- Discord webhook delivery: ≤ 10s p99
- Backtest queue: p99 ≤ 30 min on QC tier

The 30-min CME settlement wait is BEFORE order queueing, not part of latency budget.

### Audit & Track Record

- **Immutability mechanism:**
  - Postgres `BEFORE UPDATE OR DELETE on audit_log` trigger raises exception. **INSERT is permitted** (chain is append-only). UPDATE/DELETE blocked even by app_owner.
  - **TRUNCATE blocking:** Postgres EVENT TRIGGER on `ddl_command_start` aborts any TRUNCATE targeting `audit_log` AND `REVOKE TRUNCATE` from all roles except `dba_breakglass`. (Row triggers do NOT fire on TRUNCATE.)
  - Service role: `INSERT, SELECT` on `audit_log`; `REVOKE UPDATE, DELETE, TRUNCATE`
  - Hash chain: SHA-256 single-linked, ordered by INSERTION sequence; `prev_hash`, `record_hash = SHA-256(prev_hash || record_payload)`. Genesis `prev_hash` = 32 zero bytes.
  - Backfill/repair: APPEND at chain tail with `repaired_for_sequence_no` and `repaired_for_event_timestamp` provenance. Original gap visible.
  - Backups: S3 Object Lock (Compliance mode); retention 7 daily / 4 weekly / 12 monthly / permanent annual; quarterly restore drill.
- **Postgres role hierarchy:**
  - `app_service` — `INSERT, SELECT` on `audit_log`; `SELECT, INSERT, UPDATE, DELETE` on non-audit
  - `app_owner` — schema owner; runs Alembic; cannot bypass audit triggers
  - `dba_breakglass` — superuser; offline credential (paper in fireproof safe + safety deposit box; annual rotation); high-severity audit on use; only role with TRUNCATE
  - dba_breakglass DB password stored as SCRAM-SHA-256 hash in `pg_authid`; printed paper holds plaintext for one-time use
- **Composite identity (every trade tagged):**
  - `strategy_hash` = git commit SHA at signal time
  - `parameter_set_hash` = SHA-256 over canonical-serialized active parameter values from Parameter Ranges Table at signal time
  - `slippage_calibration_version` = current head at signal time (live) or pinned-at-PR-creation (backtest)
- **Track record portability:** identical schema between QC Phase 1 and custom Phase 2; QC adapter golden-test parity weekly (byte-for-byte)
- **Environment tagging:**
  - `paper` = any non-real-money trade
  - `live-small` = real money, equity < $50k at signal time
  - `live-scale` = real money, equity ≥ $50k at signal time
  - Decommission floor and signal-storm thresholds applied per strategy version
- **Paper minimum:** 30 CME sessions paper before live deployment of new strategy version (CI gate)
- **Trade-level attribution schema:**
  - Same row, two field groups; `expected_*` immutable post-emit (BEFORE UPDATE trigger allows updates only to `realized_*` columns); `realized_*` nullable until trade closes
  - Audit log: `signal_emitted` and `trade_realized` event types
- **audit_log partitioning (locked, day-1):** schema partitions by year from day 1 with empty future partitions (one Alembic op creates partitions for current year + next 5 years); new yearly partitions added annually via Alembic op; Year 5+ cold tier (S3 only, dropped from live DB)
- **`incident_reviews` table:** separate table FK to `audit_log` and optionally `alerts`; stores write-up text, author, timestamp, resolved status; required entry before incident_review HALT_NEW resume

### vectorbt-vs-LEAN Parity (locked trade-count rule)

- Weekly cron golden-test: same strategy + same data through both engines
- **Trade definition (canonical, both engines):** entry-to-exit round-trip = 1 trade; partial fills consolidated into the parent trade
- Threshold: cumulative-since-inception P&L divergence > 0.1% of starting equity at end of run, OR any single trade-count mismatch → P0 bug

### QuantConnect Audit Adapter (Phase 1 critical path)

- QC algorithm writes audit events to QC ObjectStore as JSONL with monotonic sequence numbers per session
- QC algorithm ALSO pushes intraday state (positions, cash, day-trade count, margin) every 60s during CME session for reconciliation source
- Backend polls QC ObjectStore every 60s during CME session; cursor-based; resumes from last cursor on restart
- Schema identical to custom-emitted records; weekly golden-test parity (byte-for-byte)
- Loss handling: gap → alert + pull from QC's logs; backfilled records APPENDED at current tail with provenance
- Failure mode: unavailable > 10 min → HALT_NEW (defensive_envelope)
- Clock skew: every event carries `source_clock_ts` (QC) and `ingest_clock_ts` (backend); chain hashed by `ingest_clock_ts`

### Tax Handling

- Futures (1256): automatic 60/40 LTCG/STCG, no election; system reports Form 6781
- ETFs: capital gains/losses with wash sale tracking; no 475(f) by default; system supports both modes
- CPA consultation required before any election; UI gate (verbatim text capture per frontend spec)
- Wash sale tracking across all `account_id`s
- Year-end harvest flagging
- Tax export: CSVs for Form 6781, Schedule D, Form 8949; PDF summary; annual Jan 31

### Claude Ops Agent — Authority Matrix

| Category | Authority | Note |
|---|---|---|
| Tighten risk via parameter change (within range, tighten-direction, next-cycle) | AUTO + notify | |
| Tighten risk via defensive position trim (mid-session) | AUTO + notify | Phase 1: agent triggers QC algorithm-side trim via ObjectStore instruction. Phase 2: agent triggers risk engine which holds broker creds. Either way agent has zero broker creds directly. |
| Loosen risk | HUMAN APPROVAL | Hard-coded denial |
| Hot-fix infrastructure (within whitelist) | AUTO-DEPLOY + auto-rollback | |
| Strategy logic changes | DRAFTS PR | |
| Place orders directly | NEVER | No broker creds physically |
| Invoke kill switch | AUTO on threshold | |
| Un-invoke kill switch (resume) | HUMAN ONLY (re-auth, web-only) | |
| Modify strategy params within range AND tighten direction | AUTO + audit + auto-revert | |
| Generate reports, alerts, briefings, diagnostics | AUTO | |

#### Hot-Fix Whitelist (LOCKED)

**ALLOWED for agent auto-deploy:**
- `services/observability/**`
- `services/monitoring/**`
- `services/agent/reporting/**` — report formatting, briefing templates
- `services/agent/monitoring/**` — telemetry consumption code
- `services/agent/integrations/**` — external API clients (e.g., billing API pull for cost tracking)
- `services/agent/prompts/system/**` — **system prompts only**: response formatting instructions, tool-use schemas, reporting boilerplate, agent identity / persona templates, error-message handling
- `infrastructure/retry/**`, `infrastructure/broker_reconnect/**`, `infrastructure/logging/**`
- `Dockerfile`, `docker-compose.yml`, `requirements*.txt`, `pyproject.toml`
- `deploy/**`

**FORBIDDEN — PR required:**
- `services/risk/**`, `services/signal/**`, `services/audit/**`, `services/execution/**`, `services/reconciliation/**`
- `services/calibration/**` (slippage)
- `services/agent/decisions/**` — anything affecting WHICH parameter to tighten, WHEN to trim, WHAT to halt
- `services/agent/risk_actions/**` — risk-tightening invocations
- `services/agent/parameter_changes/**` — parameter mutation invocations
- `services/agent/prompts/decision/**` — **decision prompts:** any prompt template that influences a decision the agent will make, including: which-parameter-to-tighten templates, when-to-invoke-defensive-trim templates, halt-condition assessment templates, PR drafting templates that reach into strategy logic
- `alembic/**`
- Any file containing constants matching `RISK_RING_*`, `KILL_SWITCH_*`, `MARGIN_*`, `CLUSTER_CAP_*`, `PARAMETER_RANGE_*` (pre-merge linter enforced)

**System-vs-decision boundary examples:**
- `services/agent/prompts/system/morning_briefing.md` ✅ ALLOWED (formatting template)
- `services/agent/prompts/system/error_response_format.md` ✅ ALLOWED (response format)
- `services/agent/prompts/decision/parameter_tighten_choice.md` ❌ FORBIDDEN (decision logic)
- `services/agent/prompts/decision/defensive_trim_trigger_assessment.md` ❌ FORBIDDEN

#### Hot-Fix Auto-Rollback Metrics

Auto-rollback within 30 min if **any**:

| Metric | Threshold |
|---|---|
| Service error rate (5xx + uncaught exceptions) | > 2× 7-day pre-deploy baseline |
| p99 critical-path latency | > 2× 7-day baseline |
| Kill-switch invocation frequency | > 2× 7-day baseline |
| Reconciliation break rate | > 2× 7-day baseline |
| Audit log write failure rate | > 0 (any failure) |

Auto-rollback restores prior commit; alert; deploy disabled for that whitelist subtree for 24h.

### Operator-Friendly PR Review Surface

PRs touching strategy logic or risk parameters surface:
1. Plain-English summary (≤ 200 words; agent-written)
2. Risk impact summary (auto-generated)
3. Backtest delta (LEAN authoritative; pinned `slippage_calibration_version` at PR creation): equity curve, key stats delta, ten worst-divergence trades
4. Test results (unit + integration + linting + type-check)
5. Diff (collapsed)
6. Files affected
7. In-app Approve / Reject / Request Changes (sync to GitHub via backend's GitHub App)

For parameter-only PRs: same git SHA, different `parameter_set_hash`. Backtest delta re-runs LEAN with proposed parameter set against same code.

### Decision Diary

- Operator: mandatory min 10-char reasoning on every signal rejection, defer, or override
- Agent: optional commentary
- Required fields:
  - `entry_class`: `signal_response` | `forward_looking` | `general`
  - `linked_signal_id`: UUIDv7 (REQUIRED if `entry_class = signal_response`; NULL otherwise; schema-enforced)
  - `linked_market_id`: optional (used in `forward_looking`)
  - `tag`: `data_concern` | `regime_concern` | `size_concern` | `manual_judgment` | `other`
  - `timestamp`: UTC + monotonic
  - `author`: `operator` | `agent`
  - `reasoning_text`: free text, min 10 chars when author=operator

### Communications

- Primary: Discord bot via `discord.py`. Channels: `#daily-brief`, `#signals`, `#fills`, `#alerts`, `#critical`, `#ops`, `#ask-agent`, `#audit`
- Backup: email
- Discord-bot-as-service: bot service (gateway WS) + webhook-pusher service on shared internal Docker network with sops-decrypted secrets
- Heartbeat split:
  - Delivery = HTTP 2xx ack from Discord; failure → email backup automatic
  - Engagement = ANY of: Discord reaction/reply on critical alerts; email reply; web app authenticated activity; reply on daily liveness probe
  - No engagement > 24h on critical alert OR no engagement to daily liveness probe → HALT_NEW (defensive_envelope)
- Daily liveness probe: 09:00 ET each CME session; "system is alive — react/reply to acknowledge" to `#daily-brief` + email backup
- Vacation: per Vacation Mode section
- NO SMS, NO voice escalation
- External watchdog: separate region; pings backend `/health` every 5 min; emails on >15 min unreachable during CME session

### Security

- Secrets: sops + age; encrypted files in repo; separate sops files per environment (`secrets/dev.enc.yaml`, `secrets/paper.enc.yaml`, `secrets/live.enc.yaml`)
- Age key backup: printed paper, fireproof safe + safety deposit box; annual rotation
- Rotation: quarterly forced; immediate on compromise
- DB backups: daily encrypted to S3 Object Lock; quarterly restore drill
- Encryption at rest: Hetzner volume + app-level for high-sensitivity columns
- Auth (web): WebAuthn primary + TOTP backup + 8 single-use printed backup codes
- All-factors-lost recovery: dba_breakglass + sops backup restore + manual identity re-establishment
- Auth tokens: opaque session ID in HttpOnly + Secure + SameSite=Strict cookie; CSRF token in non-HttpOnly cookie (double-submit); server-side session row with `last_uv_at`
- Re-auth (WebAuthn UV) within 5 min for risk-loosening (web-only by construction)
- Session lifetime: 30 min idle / 24h absolute / 7d refresh
- Container hardening: non-root, read-only fs where compatible, no privileged, Trivy in CI, distroless where compatible
- Network egress allowlist: IBKR endpoints (Phase 2 only), Anthropic API, S3, NTP, package mirrors, GitHub, QC API endpoints (Phase 1)
- Network ingress: Caddy HTTPS public + SSH (key-only); internal Docker network only otherwise
- Repo / build-chain DR: self-hosted Gitea on VPS (full GitHub mirror, daily sync); weekly encrypted repo archive to S3
- GitHub workflow: branch protection on `main` requires CI pass + ≥1 approval; agent commits to `agent/...` feature branches; operator self-approves agent PRs via in-app review surface (sync via GitHub App install token)

### Time and Clock

- NTP: chrony, primary `pool.ntp.org`, fallback `time.cloudflare.com`
- Clock skew: log warn > 100ms; defensive halt > 1s
- Audit ordering: `timestamp_utc` + `monotonic_ns` (within process); QC events also carry `source_clock_ts` and `ingest_clock_ts`
- All schema timestamps: `TIMESTAMPTZ` UTC, rendered `America/New_York`
- DST: scheduler wall-clock ET (DST-aware); session-counted windows correct across DST transitions

### Idempotency

- All writes: UUIDv7 PKs
- Order placement `client_order_id` (33 chars under IBKR ~50-char limit):
  - Format: `{strategy_short}-{paramset_short}-{signal_short}-{retry_n}`
  - `strategy_short` = first 8 hex chars of `strategy_hash` (8 chars)
  - `paramset_short` = first 8 hex chars of `parameter_set_hash` (8 chars)
  - `signal_short` = last 12 hex chars of `signal_uuid` (12 chars)
  - `retry_n` = 1–2 digits
  - Total: 8 + 1 + 8 + 1 + 12 + 1 + 2 = 33 chars
- Audit writes: UUIDv7
- Webhook re-delivery: dedupe by `event_uuid` for 7-day window via Postgres unique constraint

### `session_evicted` SSE Event (LOCKED)

Server emits `session_evicted` to a session in the following cases:
- **Tab-limit eviction:** N+1 connection from same user; oldest existing connection receives event
- **Explicit logout:** user logs out from another tab/device
- **Breakglass session kill:** dba_breakglass action terminates user sessions
- **Credentials rotation:** WebAuthn or TOTP credentials rotated; existing sessions invalidated

Payload: `{ type: "session_evicted", sequence_no, server_now, data: { reason: "tab_limit"|"explicit_logout"|"breakglass_kill"|"creds_rotated" } }`

Frontend behavior: display banner appropriate to reason; on `tab_limit`, banner indicates which tab was evicted; on others, redirects to `/login`.

### Backtesting Validation

- Walk-forward: rolling 3-year train, 6-month out-of-sample, advance, repeat
- 70/30 in-sample / held-out test split; held-out touched ONCE
- Survivorship-bias / continuity per Data Sources
- Realistic fills via slippage calibration (versioned, per locked procedure)
- Tax modeling post-hoc on trade log
- Capacity analysis at 1×, 5×, 10×, 25× current capital
- 30 CME session paper minimum per strategy version (CI gate)
- vectorbt-vs-LEAN parity (weekly cron) per locked rule above

### Testing Discipline

- **Unit tests required:** risk engine (every state transition, kill-switch trigger, severity branch, cluster-shrink iteration with 10-iter limit), position sizing (full algorithm including Stage 0 universe filter, Stage 2 50%-override, Stage 5 lot rounding), order routing, order rejection taxonomy handling, audit log immutability + hash chain (incl. backfill provenance, TRUNCATE blocking), version governance + composite-hash, reconciliation logic with tolerance bands, capacity calculator, momentum-score auto-trim, decision diary writer (per entry_class), vacation handler (entries blocked, exits continue, queued orders cancelled), capital-event reset asymmetry (deposit YES, withdrawal NO), capital-event mode sessions 6–30 mode-flag persistence, vol-target multiplier composition (MIN), data quality validation, signal storm detector, vol regime detector, daily liveness probe, per-parameter tighten direction enforcement, dividend on-the-fly back-adjustment + accrual, slippage calibration OLS fit, ETF MTM at 17:00 ET = NYSE 16:00 close, intraday MTM 60s cadence
- **Integration tests:** strategy logic against historical data, broker connectivity (mock + live-paper Phase 2; QC ObjectStore mock Phase 1), full kill-switch flow incl. all severity branches, signal-to-fill round trip, order placement at next-session-open delay, QC adapter golden-test parity (weekly), vectorbt-vs-LEAN parity (weekly; trade definition entry-to-exit), per-service degradation matrix scenarios, continuous-vs-physical contract reconciliation at futures rolls, hot-fix auto-rollback simulation, DST transition handling, PDT pre-check edge cases, cluster-shrink convergence + non-convergence handling, universe filter at multiple equity tiers
- CI gates ALL PRs
- Pre-merge gates: tests pass, `ruff`, `mypy --strict`, `gitleaks`, no risk-engine modification without `risk-review-approved` label, hot-fix forbidden-path linter

### Performance Targets

- Phase 1 single strategy: backtest Sharpe ≥ 1.5; live Sharpe ≥ 0.8 over the first 6 months of live trading (cross-phase: spans Phase 1 → Phase 2 cutover; measured against continuous live track record from inception); max DD ≤ 15%; signal acceptance ≥ 90% (per refined denominator)
- Phase 2 portfolio: live Sharpe ≥ 1.2
- Phase 3 portfolio: live Sharpe ≥ 1.5
- Drift alerts: live underperforms backtest by > 1 SD over 30+ days
- Auto-decommission floor (above)

### Operating Cost Envelope (LOCKED)

| Cost Category | Monthly | Notes |
|---|---|---|
| QuantConnect (Phase 1) | $20–80 | $20 default, $80 if backtest queue bottlenecks |
| Polygon.io (Phase 2 contingent) | $0 or $30 | Only if QC has gaps |
| Hetzner VPS primary (CCX13 or similar) | $20–40 | Hosts backend + frontend |
| Hetzner external watchdog (CX11) | $5 | Different region |
| S3 / Backblaze B2 backups | $1–3 | Object Lock |
| Anthropic Claude API | $30–100 | Aggressive prompt caching |
| Domain registration | $1 | Amortized |
| Email service (Resend or SES) | $1–5 | Low volume |
| IBKR market data | $0–30 | Phase 2 only; most free |
| Sentry | $0–26 | Free or upgraded |
| GitHub | $0 | Personal |
| **Total target** | **$80–320/month** | |
| **Soft alert ceiling** | **$200/month** | |
| **Hard alert (cost-review state)** | **$300/month** | |

System tracks via provider billing API or CSV; surfaces in System page.

## YOUR DELIVERABLE

Produce a complete, production-grade backend technical specification covering ALL sections below. Use Mermaid for diagrams. Be specific and concrete. Where genuine implementation choices remain, present 2–3 options with tradeoffs and a recommendation.

**Frontend contract:** the parallel frontend spec (Prompt B) defines six post-auth pages (Today, Trades, Performance, Research, System, Calendar) plus pre-auth (`/login`, `/setup`, `/recover`); a Discord bot with phased commands; a single multiplexed SSE channel `/api/sse/events` with event types (`signal`, `fill`, `position`, `pnl`, `risk_state`, `health`, `alert`, `audit`, `agent`, `vacation`, `watchdog`, `session_evicted`); event format `{ type, sequence_no, server_now, data }`; canonical anomaly reason codes; CSV column lists; agent status state enum. Reference these by name.

### 1. System Architecture Overview
Mermaid diagram of all services, data flow, external integrations, watchdog topology. Service inventory. Phase 1 (no direct IBKR; QC ObjectStore-mediated) vs. Phase 2 (direct IBKR + LEAN Local) architectures explicitly different. Migration path with pre-cutover checklist + abort.

### 2. Component Breakdown
For each (data ingestion, storage, signal engine, risk engine [position sizing including Stage 0 universe filter and Stage 2 50%-override], execution engine, reconciliation [Phase 1 QC-mediated vs. Phase 2 direct], monitoring, agent, scheduler+calendar combined, audit service, QC adapter, watchdog, Gitea mirror, slippage calibration service): purpose, inputs, outputs, dependencies, configuration, failure modes (cross-ref Per-Service Degradation Matrix), implementation notes.

### 3. Data Models and Schemas
Postgres DDL via Alembic migrations for every persistent entity. Include: `audit_log` (with hash-chain fields, source/ingest clocks, partitioned by year from day 1), `trades`, `orders`, `fills`, `positions` (current and historical), `signals`, `strategy_versions`, `parameters` (event-sourced with valid_from/valid_to), `parameter_sets` (with `parameter_set_hash`), `slippage_calibration_versions` (with α_market, β_market per market), `alerts`, `accounts`, `balances` over time, `macro_events`, `reconciliation_breaks`, `data_quality_events`, `decision_diary` (with entry_class), `attribution` (immutable expected_* + nullable realized_*), `agent_actions`, `vacation_mode`, `qc_adapter_cursor`, `capital_events`, `cost_events`, `liveness_probes`, `pdt_day_trade_log`, `dividend_history`, `incident_reviews` (FK to audit_log and alerts), `universe_state` (current active markets + exclusion reasons).

### 4. API Contracts
- REST endpoints (path, method, pydantic schemas, auth)
- SSE channel `/api/sse/events` with all event types specified including `session_evicted` (per locked behavior)
- Discord bot commands and button payloads (per Prompt B phasing table)
- Internal HTTP-IPC payloads (backend → bot)
- Webhook payloads (QC ObjectStore poll, backend → email backup, watchdog ping push, agent → QC algorithm trim instruction Phase 1)
- Idempotency key conventions including `client_order_id` format (33 chars per spec)
- CSV export endpoints (with hash-chain footer)

### 5. Sequence Diagrams (Mermaid)
At minimum:
- Position sizing full algorithm (Stage 0 universe filter → inverse-vol → per-position cap with 50%-override → cluster shrink-iterate → gross/net cap → lot rounding)
- Universe expansion on equity growth (deposit triggers re-evaluation; previously-excluded market enters active universe)
- Signal generation 17:30 ET → per-market settlement wait → emission → order queueing → next-session-open placement → ack (with t_0 marked at placement attempt)
- Slippage recalibration (versioned event, no paper-day reset)
- Daily liveness probe → engagement registration
- Hot-fix auto-deploy → 30-min metric watch → rollback or commit
- HALT_NEW max-dwell escalation at 7 trading days
- HALT_NEW (incident_review) flow with full DB snapshot + write-up to `incident_reviews` before resume
- IBKR margin-call edge case
- Cutover scheduling and abort flow
- Phase 1 → Phase 2 cutover execution
- Vol-target multiplier composition (CONVALESCENT + capital-event sessions 1–5 → MIN = 0.5)
- Capital-event mode sessions 1–5 vs. 6–30 (vol multiplier transition; DD baseline behavior)
- Capital-event deposit (DD reset) vs. withdrawal (no DD reset) asymmetry
- DST transition handling
- PDT pre-check refusal flow (Phase 1 source: QC ObjectStore push)
- Macro event window straddling next-session order placement
- Vacation start (cancel pending working orders), end
- `session_evicted` event emission for all four reasons

### 6. Error Handling Strategy
Categorization (transient / persistent / catastrophic); Per-Service Degradation Matrix realization; Order Rejection Taxonomy implementation; idempotency for order placement and audit writes; specific handling per matrix.

### 7. Observability
- Logging schema (structlog JSON, fields per category; local file via logrotate; daily S3 upload)
- Metrics inventory (Prometheus or equivalent)
- Health check endpoints consumed by external watchdog
- Dashboard recommendation
- Agent telemetry consumption
- Alert routing logic by severity (P0/P1/P2; Defensive Risk Envelope escalated routing)
- Cost tracking integration

### 8. Security
sops + age implementation; per-environment file structure; age key backup; Postgres role hierarchy with break-glass procedure; file permissions; network exposure; API auth; audit log immutability (BEFORE UPDATE/DELETE trigger + EVENT TRIGGER for TRUNCATE + REVOKE TRUNCATE); backup encryption keys; repo / build-chain DR (Gitea + S3); account recovery procedure; GitHub workflow.

### 9. Deployment Topology
VPS specs; external watchdog topology; Docker Compose layout (Caddy reverse proxy routing /api/* + /sse/* to FastAPI, else Next.js; separate Discord-bot service + webhook-pusher service); environment configuration (dev local, paper, live); deployment procedure; rollback; DR runbook.

### 10. Testing Strategy
Unit and integration test inventory (full list above); CI/CD pipeline (GitHub Actions); pre-merge gates; strategy validation pipeline; vectorbt-vs-LEAN parity test; QC adapter golden-test parity; continuous-vs-physical contract reconciliation; slippage calibration verification.

### 11. Phased Build Plan
- **Phase 0 (weeks 0–8):** v1 strategy authored by Claude Code with operator review weeks 0–1; paper begins week 1; sub-universe verified end of week 2; QC adapter coded + golden-tested by week 4; 30 paper sessions complete within weeks 1–7; week 8 buffer
- **Phase 1 (months 2–5):** live track record on QC; custom backend skeleton in parallel; Phase 1 backend has NO direct IBKR; market data + broker state via QC ObjectStore push
- **Phase 2 (months 5–9):** custom infra hardening; LEAN Local; ib-async; direct IBKR; paper validation; cutover
- **Phase 3 (months 9–12):** capital scaling; second-strategy preparation; legal structure
- Each phase: deliverables, success criteria, kill criteria

### 12. Claude Ops Agent Detailed Spec
Trigger model; tool inventory (bounded actions; hot-fix whitelist incl. system-vs-decision prompt boundary; defensive trim invocation); prompt-cache strategy; cost budget; failure mode handling; audit trail; rollback mechanism; Operator-Friendly PR Review Surface — full rendering spec.

## FORMAT REQUIREMENTS

- Markdown with clear section headers
- Mermaid for ALL diagrams
- Concrete library/tool/version recommendations
- Where implementation choices remain, present 2–3 options with tradeoffs and recommendation
- Length will be substantial; favor completeness over brevity
- Never invent strategic decisions; flag missing context with `[QUESTION FOR OPERATOR: ...]`
- Reference Prompt B's IA, SSE event types, Discord command schemas, and canonical vocabulary by name

Begin.
