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

**The canonical session calendar is CME RTH** (Sun 18:00 ET → Fri 17:00 ET, with daily 17:00–18:00 ET maintenance pause). Used for:
- 17:00 ET daily MTM anchor
- CONVALESCENT 5-session counter
- Capital-event mode 30-session counter
- **Paper-day counter (30-session paper minimum) — CME RTH sessions, NOT NYSE** (consistency with our predominantly-futures book)
- Trading-day counts in general

Where a behavior is specifically ETF-related (PDT rule, ETF order placement, NYSE exchange holiday), the **NYSE calendar** is used.

Both calendars read from `pandas_market_calendars` (or equivalent maintained source).

### Strategy

- **Phase 1 strategy:** multi-asset systematic trend-following on micro futures + bond ETFs
- **Universe (canonical full target):** ~8–12 markets — equity index micros (`/MES`, `/MNQ`, `/M2K`, `/MYM`), commodity micros (`/MCL`, `/MGC`, `/SIL` — CME micro silver; **NOT `/MSI`**), Bitcoin micro (`/MBT`), bond ETFs (TLT, IEF, SHY); optional FX micros (`/M6E`)
- **Phase 1 sub-universe (executable on QC bundled data):** verified at Phase 0 weeks 0–1. Markets that fail QC bundled-data verification (likely candidates: `/M6E`) are EXCLUDED from Phase 1 and added Phase 2 with Polygon or external data. **`/M6E` excluded from Phase 1 by default** unless QC verification succeeds.
- **Signal type:** time-series momentum / breakout (Donchian channels, MA crossovers); vol-targeted sizing; daily bars
- **Daily bar definition (locked, per asset class):**
  - Futures (CME-listed): close = **CME daily settlement, 17:00 ET**
  - ETFs (TLT, IEF, SHY): close = **NYSE close, 16:00 ET** with **dividend back-adjustment** (total-return series for signal computation; dividends accrued into MTM at ex-date and reinvested into the bar series)
- **Signal generation cadence and per-market wait policy:**
  - Scheduler runs at **17:30 ET** after both close anchors
  - For each market independently: if settlement available, generate signal immediately; otherwise retry every 5 min until 18:00 ET (30 min tolerance)
  - At 18:00 ET (30 min late): use last available bid/ask midpoint with `unsettled` flag and proceed for that market
  - At 18:30 ET (60 min late): drop signal generation for that market that day (`market_drop_settlement_unavailable`); other markets unaffected
  - **Partial signal generation is normal**: 8 markets generate signals at 17:30, 2 wait or drop independently
- **Order placement timing (locked):**
  - Futures: orders queued at 17:30 ET signal cycle; **placed at next CME RTH session start** (typically 18:00 ET same evening after maintenance pause). Macro pause windows apply to PLACEMENT (delays the place); if pause + 60-min staleness exceeds session, signal dropped (`macro_window_drop`).
  - ETFs: orders queued at 17:30 ET signal cycle; **placed at next NYSE open (09:30 ET next trading day)**. Macro pauses apply.
  - Both: limit-marketable at the open with widening retry per Execution Mechanics.
- **Holding period:** **minimum** 10 days (per `MIN_HOLDING_DAYS` default); **typical realized** 2 weeks to 6 months; max bound by stop-out, signal reversal, or strategy decommission. (Earlier "2 weeks to 6 months" was the typical, not the minimum.)
- **Phase 2+:** add second uncorrelated strategy only after Phase 1 live validation; sequential strategy addition
- **Base currency:** USD only. No FX hedging. `/M6E` (if active) settles in USD via IBKR auto-conversion.
- **Account model:** single live IBKR Pro account in operator's name. Schema includes `account_id` foreign key throughout from day 1; Phase 3 multi-account addition is INSERT to accounts + new sops file + service config update (no migration).

#### Position Granularity (locked)
- **Futures: contract-level** (per expiration month, e.g., `/MES Mar26`); risk rings combine notional across active expirations of the same root for the per-position cap (so during a roll window, old + new contract count together against per-position limit)
- **ETFs: symbol-level**
- Roll-window double-exposure: brief overlap when rolling (e.g., 2 days holding both Mar and Jun /MES); combined notional respects per-position cap; if cap binding, roll completes via momentum-ranked partial reduction of front-month before adding back-month.

#### PDT / Reg T (locked, refined)
- Futures use SPAN; PDT does not apply.
- ETFs use Reg T (50% initial, 25% maintenance). PDT rule applies while account equity < $25k.
- **PDT pre-check (refined):** on any new ETF entry, if `account_equity < $25,000 AND rolling_5_session_day_trade_count >= 3` (NYSE sessions), refuse entry. Conservative — under-trades rather than risk a violation. Even though our `MIN_HOLDING_DAYS≥10` makes intra-day same-day round-trips structurally rare, a stop-out on day-of-open is possible during volatile sessions; the pre-check protects that edge case.
- Day-trade count source: FlexQuery EOD + intraday TWS state.
- Portfolio Margin not in scope (requires $125k+).

#### Sharpe Definition (canonical)
- Annualization factor: 252
- Risk-free rate: 0
- Returns: daily close-to-close based on 17:00 ET MTM
- `Sharpe = mean(daily_returns) × sqrt(252) / stdev(daily_returns)`
- "X-day rolling Sharpe" uses last X CME RTH sessions

#### Signal Acceptance Rate Definition (locked)
`signal_acceptance_rate = orders_placed / signals_emitted_post_data_quality_filter`
- **Numerator:** signals that resulted in actual broker order placement (NOT capacity-constrained-to-zero, NOT operator-rejected, NOT refused by PDT/risk pre-check, NOT macro-window-dropped, NOT sub-minimum-size-after-rounding)
- **Denominator:** signals emitted by strategy after data-quality validation (rejected/quarantined bars produce no signal and don't count); includes `unsettled`-flag signals
- Target: ≥ 90% (Phase 1)

### Path / Phasing

- **Phase 0 (weeks 0–8, holiday-buffered):** foundation — operator upskilling, IBKR Pro account opening, QC subscription, repo + CI scaffolding (incl. **v1 strategy code authored in weeks 0–1 by operator + Claude Code together** as part of "upskilling"), secrets management (sops), Hetzner VPS provisioned, audit schema designed and migrated. **Paper trading begins on QC week 1 with the v1 strategy. Phase 1 sub-universe verified by week 2 (drop markets that fail QC bundled-data verification). QC ObjectStore audit adapter coded and golden-tested by week 4. 30 CME RTH paper sessions completed within weeks 1–7 (calendar buffer absorbs typical 1–2 holidays). Week 8 buffer + Phase 1 handover.** If 30-session paper minimum slips past week 7 due to holiday cluster, Phase 0 extends.
- **Phase 1 (months 2–5):** live trading on QuantConnect Cloud (LEAN). Real money, small size (`live-small`). Track record begins immediately.
- **Phase 2 (months 5–9):** custom infrastructure built and hardened; strategy migrates to LEAN Local (Docker) + vectorbt research layer; track record continuous via QC adapter audit ingestion.
- **Phase 3 (months 9–12):** capital scaling, second-strategy preparation, family-money legal structure.
- **Phase 1 → Phase 2 cutover:** operator selects date ≥5 trading days in advance; pre-cutover automated checklist (positions reconciled in last 24h, no working stops >5σ from current price, parameter sets canonicalized, custom backend integration tests pass); abort on any check fail OR HALT_NEW state in 24h prior; cutover at session close → flatten on QC → restart fresh on LEAN Local next morning. **No position transfer.** Audit log continuous.

### Tech Stack (locked)

- **Language:** Python 3.11+ end to end
- **Engine:** LEAN (QC Cloud Phase 1; LEAN Local Phase 2). **LEAN authoritative for backtest PR review surface.** vectorbt research-only.
- **Storage:** DuckDB on Parquet (analytics); PostgreSQL 16 (transactional; asyncpg + SQLAlchemy 2.x async; Alembic migrations)
- **Broker library:** **`ib-async >= 0.9.x, < 2.0`** (community fork of `ib_insync`, same API surface; latest stable in 0.x line at implementation time). Phase 1 routes via QC's IBKR integration; Phase 2 direct via `ib-async` to IB Gateway in Docker.
- **Margin model:** SPAN for futures (broker-computed); Reg T for ETFs.
- **"Used margin" canonical:** `used_margin_pct = 1 − (ExcessLiquidity / NetLiquidation)`, both pulled from IBKR `accountSummary`.
- **Orchestration:** cron + APScheduler in single co-located process (scheduler + calendar service); persistent Postgres-backed job store. **DST-aware**: scheduler is wall-clock ET via `zoneinfo.ZoneInfo("America/New_York")`; session-counted windows use canonical session calendar (correct across DST transitions).
- **Real-time push:** SSE for browser one-way push (single multiplexed `/api/sse/events` channel; no WebSocket); REST otherwise
- **Deployment:** Single VPS, Hetzner Cloud Ashburn, Ubuntu LTS, Docker Compose. NO Kubernetes.
- **Frontend co-located on same VPS** (no Vercel) — simplifies origin/CORS/SSE
- **Process supervision:** Docker Compose restart policies + systemd; chrony for NTP
- **Logging:** `structlog` JSON renderer
- **Log destination:** local file via logrotate (daily rotation, 30-day local retention); compressed daily copy uploaded to S3 (90-day S3 retention). **No central log aggregator Phase 1.** Phase 2+ may add Loki.
- **Validation:** pydantic v2
- **API exposure:** FastAPI on the VPS

### Data Sources (locked, with criticality flags)

- **Phase 1:** QuantConnect bundled equities + futures data (Phase 1 sub-universe verified at Phase 0 weeks 0–2); IBKR real-time market data
- **Phase 2:** Polygon.io Stocks Starter ($30/mo) **only if** QC bundled equity data has notable gaps in Phase 1 live (else $0); FRED (free) for **macro-context display only — NOT critical-path**; **economic calendar via Forex Factory or Trading Economics — IS critical-path** for tier-1 macro pause auto-detection
- **CRITICALITY (locked, fixes prior FRED-vs-calendar contradiction):**
  - FRED: NICE-TO-HAVE (regime indicators, charts). Outage → degraded macro context display only. No halt.
  - Economic calendar (Forex Factory/Trading Economics): CRITICAL. Outage > 48h → hard halt new orders next session until manual ratification (per Per-Service Degradation Matrix).
- **Tier-1 macro event taxonomy (source-agnostic):** system maintains its own tier-1 list (FOMC, CPI, NFP, GDP, PCE, ECB/BOJ/BOE rate decisions if exposed, OPEC if `/MCL` exposed); matches against incoming calendar feeds by event-name pattern. Source-specific severity codes (Forex Factory red/orange, Trading Economics 1–3) are translated to our internal tier.
- **NOT in scope:** Norgate, alt data, NLP feeds, Bloomberg, Databento, multi-tier feeds.
- **Data correctness claims (per leg):**
  - **ETFs/equities:** QC bundled is survivorship-bias-free
  - **Futures:** roll methodology (Panama / open-interest); LEAN execution uses physical contracts; backtest continuous-vs-physical reconciliation at roll dates is mandatory test
- **ETF dividend handling:** dividends back-adjusted into bar series for signal computation; reinvested into MTM at ex-date; tracked separately as `dividend_pnl` in attribution; reconciliation tolerance widens 2× during ex-dates for +24h.

### Risk Framework (concrete math; locked)

#### Position sizing — full algorithm (locked)

Stage 1 — Inverse-vol weighting (unconstrained):
```
For each active market i:
  σ_i = rolling 60-day stdev of daily log returns
  raw_weight_i = 1 / σ_i
  total = Σ raw_weight_j (over all active j)
  unconstrained_weight_i = raw_weight_i / total
  unconstrained_notional_i = unconstrained_weight_i × (effective_vol_target / portfolio_realized_vol_at_unconstrained_weights) × equity
```
Where `effective_vol_target = m_combined × VOL_TARGET_PCT_ANNUAL / sqrt(252)` (daily target). See Vol-Target Multiplier Composition below.

Stage 2 — Per-position cap: `capped_notional_i = min(unconstrained_notional_i, 0.25 × equity)`.

Stage 3 — Per-cluster cap (iterative shrink-to-fit):
```
For each cluster c with cap C_c:
  cluster_total = Σ |capped_notional_i| for i in cluster c
  if cluster_total > C_c × equity:
    scale = (C_c × equity) / cluster_total
    for i in cluster c: capped_notional_i *= scale
Re-apply per-position cap after cluster scaling.
Iterate until all caps satisfied.
If still infeasible: drop the lowest-momentum signal in the binding cluster; restart.
```

Stage 4 — Gross/net caps:
```
gross = Σ |capped_notional_i|
if gross > 3.0 × equity: uniform shrink × (3.0 / (gross/equity))
net = Σ capped_notional_i
if |net| > 1.5 × equity: uniform shrink × (1.5 / (|net|/equity))
Re-apply per-position and per-cluster caps.
```
**Net cap rationale (locked):** in synchronized trend regimes, 12-market same-direction can want >150% net. The 150% cap is **deliberately conservative** — accepts under-realized payoffs in exchange for bounded directional exposure. Phase 3+ may revisit at scale.

Stage 5 — Lot-size rounding:
```
contract_count_i = capped_notional_i / (point_value_i × multiplier_i)
rounded_i = round_to_nearest_integer(contract_count_i, banker's_rounding)
if rounded_i == 0: drop signal; tag 'sub_minimum_size'
realized_notional_i = rounded_i × point_value_i × multiplier_i
rounding_deviation_i = (realized_notional_i - capped_notional_i) / capped_notional_i
Track in attribution.
```

#### Vol-Target Multiplier Composition (LOCKED — explicit formulas)

Each reduction has a multiplier `m ∈ (0, 1]` such that `effective_vol_target = m_combined × VOL_TARGET_PCT_ANNUAL`.

| Reduction | Multiplier | Active when |
|---|---|---|
| `m_capital_event` | 0.5 | First 5 sessions of capital-event mode (sessions 1–5); 1.0 thereafter (sessions 6–30) |
| `m_convalescent` | 0.5 | During CONVALESCENT state (5 CME RTH sessions); 1.0 otherwise |
| `m_monthly_dd` | 0.5 | Remainder of calendar month after monthly DD threshold (-10%) breached; 1.0 otherwise (resets at start of next calendar month) |

**Combined:** `m_combined = min(m_capital_event, m_convalescent, m_monthly_dd, 1.0)`. Each inactive multiplier contributes 1.0; the MIN of all multipliers (including the implicit 1.0 floor) is taken. **NOT compounded.**

Examples:
- All inactive → `m_combined = 1.0`, full vol target
- CONVALESCENT only → 0.5
- CONVALESCENT + monthly DD → 0.5 (not 0.25)
- Capital-event sessions 1–5 + CONVALESCENT → 0.5
- Capital-event sessions 6–30 only → 1.0 (capital-event multiplier inactive after session 5; mode flag still set for trailing-DD reset and audit tagging)

#### Capital-Event Mode (locked, sessions 6–30 clarified)

On deposit/withdrawal ≥ 5% of current equity:
- Trailing DD reference resets to current equity at deposit time
- Capital-event mode flag set for **30 CME RTH sessions**
- Sessions 1–5: `m_capital_event = 0.5`
- Sessions 6–30: `m_capital_event = 1.0`; mode flag still set; mode-active flag means: trailing-DD tracked from deposit baseline forward; trades during this window tagged `capital_event_mode_session=N` in attribution
- Session 30+: mode auto-deactivates; no further effect

Withdrawals: peak MTM does NOT reset on withdrawal (no perverse incentive to withdraw and reset DD).

#### Equity and DD Anchors

- **Daily-start MTM anchor:** 17:00 ET CME settlement, portfolio-wide. Daily P&L = `MTM(t) − MTM(prior 17:00 ET)`.
- **Trailing DD reference:** peak intraday MTM since system inception, subject to capital-event reset.

#### Risk Rings

| Ring | Limit | Measurement Basis |
|---|---|---|
| Per-position max | 25% equity notional | Sum of \|notional\| for that single market (combined active expirations for futures) |
| Gross portfolio max | 300% equity notional | Sum of \|notional\| across all positions |
| Net portfolio max | 150% equity notional (deliberately conservative) | Signed sum across positions |
| Equity-index cluster max | 60% gross | Combined `/MES`, `/MNQ`, `/M2K`, `/MYM` |
| Commodity cluster max | 80% gross | Combined `/MCL`, `/MGC`, `/SIL` |
| Rates/bonds cluster max | 80% gross | Combined TLT, IEF, SHY |
| Crypto cluster max | 40% gross | `/MBT` |
| FX cluster max | 30% gross | `/M6E` and any future FX micros |
| Realized cross-portfolio correlation | Alert at avg pairwise > 0.7; HALT_NEW at > 0.85 | 60-day rolling realized correlation matrix |
| Daily loss limit | -5% of daily-start MTM | 17:00 ET anchor, portfolio-wide |
| Trailing drawdown limit | -20% from peak intraday MTM | Capital-event reset applies |
| Monthly DD threshold | -10% in calendar month | Activates `m_monthly_dd = 0.5` for remainder of month |
| Strategy decommission floor | HALT_NEW + human review | (a) live 30-day Sharpe < 0, OR (b) live max DD ≤ -25%, OR (c) 60-day live Sharpe underperforms backtest by > 2 SD |

**Decommission floor SD baseline (locked, fills Phase-1 live <6-month gap):**
- Pre-Phase-1 live: SD = empirical SD of 30-day rolling Sharpes from walk-forward folds during backtest
- Phase-1 live, days 1–179: same as pre-Phase-1 (walk-forward fold SD); conservative
- Phase-1+ live, days 180+: SD = empirical SD of rolling 30-day windows from live track record
- Same baseline used for auto-revert thresholds

**Decommission workflow:**
1. State → HALT_NEW with `severity=incident_review` (see HARD HALT below)
2. Strategy version flagged `decommissioned` in `strategy_versions`
3. Audit entry with provenance
4. Resume: explicit operator override (re-auth + audit justification) OR new strategy version deployment (resets `paper_days_for_version` counter; 30 new CME RTH paper sessions required)

#### Vol Regime Detector
- Metric: 60-day rolling realized vol of portfolio daily returns
- Z-score: vs. own 60-day historical distribution (250 samples of 60-day windows)
- Trigger: z > 2 → HALT_NEW

#### Signal Storm Detector
- `session_count > max(5, 3 × rolling_90_day_mean_daily_trade_count)`
- Floor of 5 prevents low-baseline trip; 3× catches genuine storms once activity scales

#### Margin Protocol — Graduated De-leverage

- 70% used → warn alert (no action)
- 85% used → auto-trim sequence:
  1. Compute momentum score per open position (rolling 60-day z-score of returns); rank ascending (weakest first)
  2. Tie-break: largest absolute margin contribution
  3. Cut via marketable-limit (1× spread, escalating to 2× on retry)
  4. **Hard cap: -30% of gross exposure across the entire sweep**
  5. Cut until used margin < 60% OR session cap reached
  6. If used margin still > 80% after one full sweep → escalate to HALT_NEW; no further trims

**Acknowledged residual risk:** if HALT_NEW reached with used margin still > 80%, **IBKR may force-liquidate outside system control.** Alert text at HALT_NEW-due-to-margin must call this out explicitly (different from other HALT_NEW alerts).

#### Capacity Tracking
- Rolling 30-day ADV per market
- Order size as % of ADV at signal-emit time
- Alert at 0.5%; partial-fill cap at 2% (size to 2% of ADV; remainder tagged `capacity_constrained`)

### Kill-Switch State Machine

States:
- **NORMAL** — full operation
- **HALT_NEW** — cancel all working orders; hold positions (no system-initiated liquidation); no new entries; **all exits continue** (stops, profit-targets, manual close); manual human resume
- **CONVALESCENT** — `m_convalescent = 0.5`; entries permitted; 5 CME RTH sessions; auto → NORMAL on completion

**HALT_NEW severity flag (locked, replaces "HARD HALT" terminology):**

HALT_NEW carries a `severity` enum:
- `severity=routine` — standard HALT_NEW (kill-switch trigger fires; standard alert routing)
- `severity=defensive_envelope` — comms breakdown trigger; escalated alert routing (email backup priority + external watchdog notify + Discord retry cadence increased)
- `severity=incident_review` — formerly "HARD HALT". Triggers:
  - Audit log write failure
  - Postgres data corruption
  - Hash chain integrity break detected
  - Decommission floor trigger
  
  Behavior: HALT_NEW state PLUS:
  - Immediate full database snapshot to S3 (not just WAL)
  - Page operator via all channels
  - Auto-resume permanently disabled; resume requires post-incident review write-up logged to audit before re-auth permitted
  - All-channel alert with explicit "incident review required" language

**HALT_NEW max dwell:** 7 trading days → daily reminder escalation; never auto-flatten.

Transitions:
- `NORMAL → HALT_NEW`: any trigger fires (severity per trigger taxonomy)
- `HALT_NEW (routine|defensive_envelope) → CONVALESCENT`: human resume (re-auth, web-only)
- `HALT_NEW (incident_review) → CONVALESCENT`: human resume PLUS audit-logged review write-up (re-auth, web-only)
- `CONVALESCENT → NORMAL`: 5 CME RTH sessions complete without breach
- `CONVALESCENT → HALT_NEW`: any trigger fires; counter resets on next resume

#### CONVALESCENT Counter — Reset/Independence Rules (corrected for prior phrasing)

| Event | Effect on CONVALESCENT 5-session counter |
|---|---|
| Any kill-switch trigger fires while in CONVALESCENT (returns to HALT_NEW) | RESET; new 5-session counter starts on next resume |
| Heartbeat engagement timeout (= kill-switch trigger, severity=defensive_envelope) | RESET (consistent with above) |
| Reconciliation false-positive resolved within tolerance (no state transition) | NO CHANGE |
| Calendar ratification grace (no state transition) | NO CHANGE |
| Capital event (deposit ≥ 5% equity) | **NO RESET to CONVALESCENT counter**; capital event starts ITS OWN INDEPENDENT 30-session capital-event-mode timer; both timers run independently; vol multipliers compose via MIN |

(Earlier phrasing said capital event "resets" CONVALESCENT — that was wrong. Corrected: capital event is independent.)

#### Kill-Switch Triggers (any → HALT_NEW)
- Trailing DD breach (-20% from peak MTM, capital-event-reset-aware) [routine]
- Daily loss breach (-5% of daily-start MTM) [routine]
- Signal storm (formula above) [routine]
- Reconciliation mismatch (delta exceeds Reconciliation Tolerances Table) [routine]
- Broker disconnect > 5 min during CME RTH [routine]
- Vol regime z > 2 [routine]
- Realized cross-portfolio correlation > 0.85 [routine]
- Decommission floor [incident_review]
- Audit log write failure [incident_review]
- Postgres data corruption / hash chain break [incident_review]
- Any unhandled exception in execution path [routine]
- Heartbeat engagement failure [defensive_envelope]

### Vacation Mode

- `/vacation start [days]` in Discord
- Engagement timeout extends to 7 days
- **NEW position entries auto-disabled.** All EXIT logic continues.
- **Pending limit-orders queued before vacation that haven't filled: CANCELLED at vacation start** (consistent with "no new entries"); existing positions hold; stops + profit-targets remain active
- Daily summary + liveness probe still post
- Macro-event ratification gate suspended
- `/vacation end` or expiry → normal operation

### Risk-Tightening Boundary (parameter changes vs. position trims)

Two paths:
1. **Parameter changes** (within range): take effect at NEXT signal cycle, never mid-session
2. **Defensive position trims:** mid-session direct order action via momentum-ranked auto-trim path; capped at -30% gross per session; **causally agent-initiated, mechanically placed by risk engine** (which holds broker creds; agent has zero broker creds). Audit records both.

#### Per-Parameter "Tighten" Direction (LOCKED)

Agent's "tighten via parameter change" authority is restricted to parameters with a defined direction. Other parameters are NOT agent-mutable for tightening (PR required to change).

| Parameter | "Tighten" Direction | Rationale |
|---|---|---|
| `LOOKBACK_DAYS_DONCHIAN` | Increase | Longer lookback = stronger breakout required |
| `INSTRUMENT_VOL_LOOKBACK_DAYS` | n/a | Statistical estimation parameter — not a tightening lever |
| `VOL_TARGET_PCT_ANNUAL` | Decrease | Lower vol = less risk |
| `MA_FAST_DAYS` | Increase | Slower fast MA = fewer false signals |
| `MA_SLOW_DAYS` | Increase | Slower slow MA = stronger trend required |
| `STOP_DISTANCE_ATR_MULT` | Decrease | Tighter stop = less risk per trade |
| `HURST_THRESHOLD` | Increase | Higher threshold = stronger trend evidence |
| `ROLL_DAYS_BEFORE_EXPIRY` | Increase | Earlier roll = less expiry risk |
| `MIN_HOLDING_DAYS` | n/a | Risk-neutral parameter |

Agent moves WITHIN the parameter's Min/Max range AND in the "tighten" direction. Loosening direction (or n/a parameters) requires HUMAN (PR or explicit operator authorization).

### Auto-Revert Thresholds (parameter changes)

Auto-reverts when **any**:
- 30-day rolling live Sharpe drops > 2 SD from pre-change baseline within 30 sessions, AND minimum 30 trades on changed market(s) in window. (SD baseline per Decommission Floor SD Baseline rule above.)
- Max DD breaches -10% within 5 CME RTH sessions of the change
- Consecutive losing trades:
  - Globally-applicable params (`VOL_TARGET_PCT_ANNUAL`, `INSTRUMENT_VOL_LOOKBACK_DAYS`): 5+ consecutive losing trades portfolio-wide
  - Market-specific params: 5+ consecutive losing trades on affected market within window; if window doesn't yield 5 trades for that market, condition cannot fire (other revert conditions still apply)

Auto-revert: parameter restored; full audit; alert; no further auto-changes to that parameter for 14 days.

### Logic-Change vs. Parameter-Change Boundary

- **Logic change** (PR + human merge): rule logic, indicator selection, market universe, strategy structure, sizing model, risk-ring values, cluster definitions, parameter ranges themselves, hot-fix-whitelist itself, "tighten" direction table itself
- **Parameter change** (auto with audit, within range AND in tighten direction): values within Parameter Ranges Table
- Pre-approved range itself is logic. Changing range = PR.
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
| `MIN_HOLDING_DAYS` | 5 | 21 | **14** (was 10; aligned to "2 weeks" typical-holding prose) | Minimum holding period before exit eligible |

**Agent-mutable within Min/Max AND in tighten direction (above table).** Changes outside range OR in loosening direction require PR.

`parameter_set_hash` SCOPE: hash computed over **only the parameters in this table**. Risk-ring values, cluster caps, hot-fix whitelist ride `strategy_hash`.

### Slippage Calibration as Versioned Artifact

- Versioned `slippage_calibration` table with `slippage_calibration_version`
- Recalibration logged as audit event but does NOT reset paper-day counter (doesn't change live execution)
- **Live execution: uses CURRENT HEAD `slippage_calibration_version`** (locked)
- **Backtest at PR creation: pins to current version at PR creation time** (re-used if PR re-run)
- Trade records carry `slippage_calibration_version` alongside `strategy_hash` and `parameter_set_hash`
- Recalibration cadence: monthly Phase 1; quarterly Phase 2+
- Alert if realized > 2× modeled for any single market for 3 consecutive months → strategy review

### Reconciliation Tolerances Table

A delta exceeding tolerance → kill-switch trigger.

| Metric | Tolerance | Grace Period |
|---|---|---|
| Position quantities (per instrument-contract) | 0 (exact) | None |
| Cash balance (USD) | greater of $5 absolute or 1 bps of equity | T+1 grace for fees, dividends, interest |
| Margin balance | $10 absolute | None |
| FX-denominated cash (intraday `/M6E` if active) | $1 absolute | T+1 for FX rounding |
| Realized P&L (cumulative) | $1 absolute | T+1 |
| Unrealized P&L | $5 absolute | None |

Tolerances widen 2× during dividend ex-dates for +24h.

### Per-Service Degradation Matrix

| Failure | System Response |
|---|---|
| Risk engine down | Signal engine halts; HALT_NEW (severity=routine) |
| Reconciliation stale > 60s during CME RTH | HALT_NEW (routine) |
| Calendar service can't reach Forex Factory/Trading Economics | Use last successful import; alert; if last successful > 48h, hard halt new orders next session until manual ratification |
| FRED unreachable | Degraded macro-context display only; no halt (FRED is non-critical) |
| QC ObjectStore poll fails 5–9 min | Alert only |
| QC ObjectStore poll fails > 10 min | HALT_NEW (defensive_envelope) |
| Backend can't reach IBKR > 5 min during CME RTH | HALT_NEW (routine) |
| Discord delivery fails | Email backup automatic; external watchdog covers VPS-down |
| Database write fails (non-audit) | Retry 3× with backoff; on persistent failure, HALT_NEW (routine) |
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
| Generic rejection (transient — connection blip, race condition) | Retry per existing logic (3× exponential backoff: 1s, 4s, 16s) |
| Pre-trade risk check rejection (internal, before broker submission) | Log + alert; do NOT retry; never bypass |
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
  - Intraday risk: TWS API real-time portfolio snapshot
  - EOD/tax/audit: IBKR FlexQuery (XML; authoritative)
  - Tolerance bands per Reconciliation Tolerances Table
- **Cadence:** session open + close + EOD; weekly summary
- **Roll discipline:** futures rolled per `ROLL_DAYS_BEFORE_EXPIRY`; off-peak liquidity
- **Macro event handling (clarified for placement-vs-generation):**
  - **Auto-pause applies to ORDER PLACEMENT only.** Signal generation at 17:30 ET runs regardless. Orders queued from a 17:30 cycle are delayed if next-session open intersects pause window.
  - Pause window: 5 min before through 30 min after scheduled tier-1 events
  - If pause + 60-min staleness exceeds session, signal is dropped (`macro_window_drop`)
  - Calendar imported nightly; user ratifies via Discord by 23:00 ET; default if no ratification: hard halt new orders next session until ratified
  - Vacation mode exception: ratification gate suspended
  - Macro window vs. session boundary: pause wins

### HALT_NEW → CONVALESCENT Resume — Re-Signal Behavior (locked)

On resume from HALT_NEW to CONVALESCENT:
- **System waits for next 17:30 ET scheduled cycle**; does NOT regenerate signals immediately
- **Cancelled working orders are NOT re-instantiated**; they're gone
- New signals generated at next cycle reflect current state under `m_convalescent = 0.5` (composed via MIN with other active multipliers)

### SLO Budgets (timing definitions clarified)

`t_0` = signal emission timestamp (after data quality + risk check pass; clock starts here)
`t_1` = order placement timestamp (broker order acknowledged)

- Signal-to-order latency: p50 ≤ 60s, p99 ≤ 5 min (`t_1 − t_0`)
- Kill-switch invocation: ≤ 5s from trigger to broker cancellation request
- Reconciliation freshness during CME RTH: ≤ 60s
- Discord webhook delivery: ≤ 10s p99
- Backtest queue: p99 ≤ 30 min on QC tier

The 30-min CME settlement wait is BEFORE `t_0`, not part of latency budget.

### Audit & Track Record

- **Immutability mechanism (corrected phrasing):**
  - Postgres `BEFORE UPDATE OR DELETE on audit_log` trigger raises exception. **INSERT is permitted** (the chain is append-only). The original phrasing "block INSERT modifications" was a phrasing bug; the correct rule is "block UPDATE and DELETE; INSERT permitted; UPDATE/DELETE blocked even by app_owner."
  - **TRUNCATE blocking:** Postgres EVENT TRIGGER on `ddl_command_start` aborts any TRUNCATE targeting `audit_log` AND `REVOKE TRUNCATE` from all roles except `dba_breakglass`. (Row triggers do NOT fire on TRUNCATE — must use event trigger.)
  - Service role: `INSERT, SELECT` on `audit_log`; `REVOKE UPDATE, DELETE, TRUNCATE`
  - Hash chain: SHA-256 single-linked, ordered by INSERTION sequence; `prev_hash`, `record_hash = SHA-256(prev_hash || record_payload)`. Genesis `prev_hash` = 32 zero bytes.
  - Backfill/repair: APPEND at chain tail with `repaired_for_sequence_no` and `repaired_for_event_timestamp` provenance. Original gap visible.
  - Backups: S3 Object Lock (Compliance mode); retention 7 daily / 4 weekly / 12 monthly / permanent annual; quarterly restore drill mandatory.

- **Postgres role hierarchy:**
  - `app_service` — `INSERT, SELECT` on `audit_log`; `SELECT, INSERT, UPDATE, DELETE` on non-audit
  - `app_owner` — schema owner; runs Alembic; cannot bypass audit triggers (BEFORE trigger raises regardless of role; EVENT TRIGGER blocks TRUNCATE; explicit REVOKE TRUNCATE)
  - `dba_breakglass` — superuser; offline; only role with TRUNCATE on `audit_log`
  - **dba_breakglass physical storage (locked):** age key (for sops) + dba_breakglass DB password printed on paper; sealed in fireproof envelope in operator's home safe; **secondary copy in safety deposit box at bank**. Annual rotation: regenerate, re-seal, destroy old prints.

- **Composite identity (every trade tagged):**
  - `strategy_hash` = git commit SHA at signal time
  - `parameter_set_hash` = SHA-256 over canonical-serialized active values from Parameter Ranges Table at signal time
  - `slippage_calibration_version` = current head version at signal time (live) or pinned-at-PR-creation (backtest)

- **Track record portability:** identical schema between QC Phase 1 and custom Phase 2; QC adapter golden-test parity verified weekly (byte-for-byte)

- **Environment tagging:**
  - `paper` = any non-real-money trade
  - `live-small` = real money, equity < $50k at signal time
  - `live-scale` = real money, equity ≥ $50k at signal time
  - Decommission floor and signal-storm thresholds applied **per strategy version** (not per account)

- **Paper minimum:** 30 CME RTH sessions paper before live deployment of new strategy version (CI gate)

- **Trade-level attribution schema:**
  - `attribution` table: same row, two field groups
    - `expected_*` (`expected_pnl`, `expected_slippage`, `vol_regime_at_emit`, `trend_regime_at_emit`): computed at signal-emit time; **immutable post-emit, enforced by Postgres BEFORE UPDATE trigger that allows updates only to `realized_*` columns**
    - `realized_*` (`realized_pnl`, `realized_slippage`, `realized_holding_days`, `dividend_pnl`): nullable until trade closes; filled in post-trade
  - Audit log captures both stages with separate event types (`signal_emitted`, `trade_realized`)

- **audit_log local retention (locked):** Phase 1: forever in Postgres (size manageable); Year 2+: partition by year; Year 5+: cold tier (S3 only, dropped from live DB).

### QuantConnect Audit Adapter (Phase 1 critical path)

- QC algorithm writes audit events to QC ObjectStore as JSONL with monotonic sequence numbers per session
- Backend polls QC ObjectStore via QC API every 60s during CME RTH; cursor-based; resumes from last cursor on restart
- Schema identical to custom-emitted records; weekly golden-test parity (byte-for-byte)
- Loss handling: gap detected → alert + pull from QC's logs; backfilled records APPENDED at current tail with provenance
- Failure mode: unavailable > 10 min → HALT_NEW (defensive_envelope)
- Clock skew: every event carries `source_clock_ts` (QC) and `ingest_clock_ts` (backend); chain hashed by `ingest_clock_ts`

### Tax Handling

- Futures (1256): automatic 60/40 LTCG/STCG, no election; system reports Form 6781
- ETFs: capital gains/losses with wash sale tracking; no 475(f) by default; system supports both modes
- CPA consultation required before any election; UI gate
- Wash sale tracking across all `account_id`s
- Year-end harvest flagging
- Tax export: CSVs for Form 6781, Schedule D, Form 8949; PDF summary; Drake/ProSeries/TurboTax importable; annual Jan 31

### Claude Ops Agent — Authority Matrix

| Category | Authority | Note |
|---|---|---|
| Tighten risk via parameter change (within range, tighten-direction, next-cycle) | AUTO + notify | Per Parameter Ranges Table + Tighten Direction Table |
| Tighten risk via defensive position trim (mid-session) | AUTO + notify | Causally agent-initiated; mechanically risk-engine-placed |
| Loosen risk (raise sizes, increase caps, restart after halt, parameter loosening) | HUMAN APPROVAL | Hard-coded denial |
| Hot-fix infrastructure (within whitelist) | AUTO-DEPLOY + auto-rollback if degraded | |
| Strategy logic changes | DRAFTS PR | Operator-Friendly PR Review Surface |
| Place orders directly (primary action) | NEVER | No broker creds physically |
| Invoke kill switch | AUTO on threshold | |
| Un-invoke kill switch (resume) | HUMAN ONLY (re-auth, web-only) | |
| Modify strategy params within pre-approved range AND tighten direction | AUTO + audit + auto-revert | Effective next signal cycle |
| Generate reports, alerts, briefings, diagnostics | AUTO | |

#### Hot-Fix Whitelist (LOCKED, with decision-path carve-out specified)

**ALLOWED for agent auto-deploy:**
- `services/observability/**`
- `services/monitoring/**`
- `services/agent/reporting/**`
- `services/agent/monitoring/**`
- `services/agent/integrations/**` (external API clients)
- `services/agent/prompts/system/**` (system prompts only — NOT decision prompts)
- `infrastructure/retry/**`
- `infrastructure/broker_reconnect/**`
- `infrastructure/logging/**`
- `Dockerfile`, `docker-compose.yml`
- `requirements*.txt`, `pyproject.toml`
- `deploy/**`

**FORBIDDEN — PR required (decision-path code):**
- `services/risk/**`, `services/signal/**`, `services/audit/**`, `services/execution/**`, `services/reconciliation/**`
- `services/calibration/**` (slippage)
- `services/agent/decisions/**`, `services/agent/risk_actions/**`, `services/agent/parameter_changes/**` (decision paths)
- `services/agent/prompts/decision/**` (decision prompts)
- `alembic/**` (DB migrations)
- Any file containing `RISK_RING_*`, `KILL_SWITCH_*`, `MARGIN_*`, `CLUSTER_CAP_*`, `PARAMETER_RANGE_*` (pre-merge linter enforced)

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
1. Plain-English summary (≤ 200 words; agent-written): what changed, why, behavior changes
2. Risk impact summary (auto-generated): metrics affected, by how much, plain numbers
3. Backtest delta (LEAN authoritative; pinned `slippage_calibration_version` at PR creation): equity curve overlay, key stats delta, ten worst-divergence trades
4. Test results: unit + integration + linting + type-check
5. Diff (collapsed)
6. Files affected
7. In-app Approve / Reject / Request Changes (sync to GitHub via backend's GitHub App)

For parameter-only PRs (same git SHA, different `parameter_set_hash`): backtest delta re-runs LEAN with proposed parameter set against same code at same `slippage_calibration_version`.

### Decision Diary (schema refined)

- Operator: mandatory min 10-char reasoning on every signal rejection, defer, or override
- Agent: optional commentary
- Required fields:
  - `entry_class`: `signal_response` | `forward_looking` | `general` (locked enum)
  - `linked_signal_id`: UUIDv7 reference (REQUIRED if `entry_class = signal_response`; NULL otherwise; schema-enforced)
  - `linked_market_id`: optional reference to market (used in `forward_looking` for market-keyed entries like "skip /MCL tomorrow due to OPEC")
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
  - No engagement > 24h on any critical alert OR no engagement to daily liveness probe → HALT_NEW (defensive_envelope)
- Daily liveness probe: 09:00 ET each CME RTH session; "system is alive — react/reply to acknowledge" to `#daily-brief` + email backup
- Vacation: `/vacation start [days]`; engagement timeout 7 days; new entries disabled; pending working orders cancelled at start; exits continue; daily summary + liveness probe still post; ratification gate suspended; `/vacation end` or expiry resumes
- NO SMS, NO voice escalation
- External watchdog: separate region; pings backend `/health` every 5 min; emails on >15 min unreachable during CME RTH; ~$5/mo

### Security

- Secrets: sops + age; encrypted files in repo; **separate sops files per environment**: `secrets/dev.enc.yaml`, `secrets/paper.enc.yaml`, `secrets/live.enc.yaml`. Live and paper IBKR credentials in different files; environment selects.
- Age key backup: printed paper, fireproof safe + safety deposit box (above)
- Rotation: quarterly forced; immediate on compromise
- DB backups: daily encrypted to S3 Object Lock; quarterly restore drill
- Encryption at rest: Hetzner volume encryption + app-level for high-sensitivity columns
- Auth (web): WebAuthn primary + TOTP backup + 8 single-use printed backup codes
- All-factors-lost recovery: dba_breakglass + sops backup restore + manual identity re-establishment
- Auth tokens: opaque session ID in HttpOnly + Secure + SameSite=Strict cookie; CSRF token in non-HttpOnly cookie (double-submit pattern); server-side session row with `last_uv_at`
- Re-auth (WebAuthn UV) within 5 min for risk-loosening actions only (full list in frontend spec)
- Session lifetime: 30 min idle / 24h absolute / 7d refresh
- Container hardening: non-root, read-only fs where compatible, no privileged, Trivy in CI, distroless where compatible
- Network egress allowlist: IBKR endpoints, Anthropic API, S3, NTP, package mirrors, GitHub
- Network ingress: Caddy/Traefik HTTPS public + SSH (key-only); internal Docker network only otherwise
- Repo / build-chain DR: self-hosted Gitea on VPS (full GitHub mirror, daily sync); weekly encrypted repo archive to S3
- GitHub workflow: branch protection on `main` requires CI pass + at least one approval (operator self-approves agent PRs via in-app review surface, sync'd via backend's GitHub App install token); agent commits to feature branches `agent/...`

### Time and Clock

- NTP: chrony, primary `pool.ntp.org`, fallback `time.cloudflare.com`
- Clock skew: log warn > 100ms; defensive halt > 1s
- Audit ordering: `timestamp_utc` + `monotonic_ns` (within process); QC events also carry `source_clock_ts` and `ingest_clock_ts`
- All schema timestamps: `TIMESTAMPTZ` UTC, rendered `America/New_York`
- DST: scheduler is wall-clock ET (DST-aware); session-counted windows correct across DST transitions because they count sessions, not hours

### Idempotency

- All writes: UUIDv7 PKs
- Order placement `client_order_id`: `{strategy_short}-{paramset_short}-{signal_short}-{retry_n}` (33 chars, under IBKR ~50-char limit)
- Audit writes: UUIDv7
- Webhook re-delivery: dedupe by `event_uuid` for 7-day window via Postgres unique constraint

### Backtesting Validation

- Walk-forward: rolling 3-year train, 6-month out-of-sample, advance, repeat
- 70/30 in-sample / held-out test split; held-out touched ONCE
- Survivorship-bias / continuity per Data Sources
- Realistic fills via slippage calibration (versioned)
- Tax modeling post-hoc on trade log
- Capacity analysis at 1×, 5×, 10×, 25× current capital
- 30 CME RTH session paper minimum per strategy version (CI gate)
- vectorbt-vs-LEAN parity (weekly cron): **threshold = cumulative-since-inception P&L divergence > 0.1% of starting equity at end of run, OR any single trade-count mismatch → P0**

### Testing Discipline

- Unit tests required: risk engine (every state transition, kill-switch trigger, severity branch, cluster-shrink iteration), position sizing (full algorithm + lot rounding), order routing, **order rejection taxonomy handling**, audit log immutability + hash chain (incl. backfill provenance, TRUNCATE blocking), version governance + composite-hash, reconciliation logic with tolerance bands, capacity calculator, momentum-score auto-trim, decision diary writer (per entry_class), vacation handler (entries blocked, exits continue, queued orders cancelled), capital-event reset + sessions 6–30 mode-flag persistence, vol-target multiplier composition (MIN), data quality validation, signal storm detector, vol regime detector, daily liveness probe, **per-parameter tighten direction enforcement**, dividend back-adjustment + accrual
- Integration tests: strategy logic against historical data, broker connectivity (mock + live-paper), full kill-switch flow incl. all severity branches, signal-to-fill round trip, **order placement at next-session-open delay**, QC adapter golden-test parity (weekly), vectorbt-vs-LEAN parity (weekly), per-service degradation matrix scenarios, continuous-vs-physical contract reconciliation at futures rolls, hot-fix auto-rollback simulation, **DST transition handling**, **PDT pre-check edge cases**
- CI gates ALL PRs
- Pre-merge gates: tests pass, `ruff`, `mypy --strict`, `gitleaks`, no risk-engine modification without `risk-review-approved` label, hot-fix forbidden-path linter

### Performance Targets

- Phase 1 single strategy: backtest Sharpe ≥ 1.5; **live Sharpe ≥ 0.8 over the first 6 months of live trading (CROSS-PHASE: spans Phase 1 → Phase 2 cutover; measured against continuous live track record from inception)**; max DD ≤ 15%; signal acceptance ≥ 90%
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
| Anthropic Claude API (agent) | $30–100 | Aggressive prompt caching |
| Domain registration | $1 | Amortized |
| Email service (Resend or SES) | $1–5 | Low volume |
| IBKR market data | $0–30 | Most free |
| Sentry (frontend errors) | $0 (free) or $26 (Performance Monitoring) | |
| GitHub | $0 | Personal |
| **Total target** | **$80–320/month** | |
| **Soft alert ceiling** | **$200/month** | |
| **Hard alert (cost-review state)** | **$300/month** | |

System tracks via provider billing API or CSV; surfaces in System page.

## YOUR DELIVERABLE

Produce a complete, production-grade backend technical specification covering ALL sections below. Use Mermaid for diagrams. Be specific and concrete; do NOT punt with phrases like "use industry best practices" — name the practice, the library, the configuration. Where genuine implementation choices remain, present 2–3 options with tradeoffs and a recommendation.

**Frontend contract:** the parallel frontend spec (Prompt B) defines six post-auth pages (Today, Trades, Performance, Research, System, Calendar) plus pre-auth (`/login`, `/setup`, `/recover`, `/auth/callback`); a Discord bot with phased commands; a single multiplexed SSE channel `/api/sse/events` with event types (`signal`, `fill`, `position`, `pnl`, `risk_state`, `health`, `alert`, `audit`, `agent`, `vacation`, `watchdog`, `session_evicted`); event format `{ type, sequence_no, server_now, data }`; canonical anomaly reason codes; CSV column lists; agent status state enum. Reference these by name.

### 1. System Architecture Overview
Mermaid diagram of all services, data flow, external integrations, watchdog topology. Service inventory. Phase 1 vs. Phase 2 architectures. Migration path with pre-cutover checklist + abort.

### 2. Component Breakdown
For each (data ingestion, storage, signal engine, risk engine [position sizing algorithm], execution engine, reconciliation, monitoring, agent, scheduler+calendar combined, audit service, QC adapter, watchdog, Gitea mirror, slippage calibration service): purpose, inputs, outputs, dependencies, configuration, failure modes (cross-ref Per-Service Degradation Matrix), implementation notes.

### 3. Data Models and Schemas
Postgres DDL via Alembic migrations for every persistent entity. Include: `audit_log` (with hash-chain fields, source/ingest clocks), `trades`, `orders`, `fills`, `positions` (current and historical), `signals` (with `anomaly_flagged` + `anomaly_reasons`), `strategy_versions`, `parameters` (event-sourced with valid_from/valid_to), `parameter_sets` (with `parameter_set_hash`), `slippage_calibration_versions`, `alerts` (severity P0/P1/P2; status open/ack/resolved), `accounts`, `balances` over time, `macro_events`, `reconciliation_breaks`, `data_quality_events`, `decision_diary` (with entry_class), `attribution` (immutable expected_* + nullable realized_*), `agent_actions`, `vacation_mode`, `qc_adapter_cursor`, `capital_events`, `cost_events`, `liveness_probes`, `pdt_day_trade_log`.

### 4. API Contracts
- REST endpoints (path, method, pydantic schemas, auth)
- SSE channel `/api/sse/events` with all event types specified including `session_evicted`
- Discord bot commands and button payloads (per Prompt B phasing table)
- Internal HTTP-IPC payloads (backend → bot)
- Webhook payloads (QC ObjectStore poll, backend → email backup, watchdog ping push)
- Idempotency key conventions including `client_order_id` format
- CSV export endpoints (with hash-chain footer)

### 5. Sequence Diagrams (Mermaid)
At minimum:
- Position sizing full algorithm (inverse-vol → per-position cap → cluster shrink-iterate → gross/net cap → lot rounding)
- Signal generation 17:30 ET → per-market settlement wait → emission → order queueing → next-session-open placement
- Slippage recalibration (versioned event, no paper-day reset)
- Daily liveness probe → engagement registration
- Hot-fix auto-deploy → 30-min metric watch → rollback or commit
- HALT_NEW max-dwell escalation at 7 trading days
- HALT_NEW (incident_review) severity flow with full DB snapshot + post-incident review write-up requirement
- IBKR margin-call edge case (system at HALT_NEW, broker force-liquidates outside system control)
- Cutover scheduling and abort flow
- Phase 1 → Phase 2 cutover execution
- Vol-target multiplier composition example (CONVALESCENT + capital-event sessions 1–5 → MIN = 0.5)
- Capital-event mode sessions 1–5 vs. 6–30 (vol multiplier transition)
- DST transition handling
- PDT pre-check refusal flow
- Macro event window straddling next-session order placement
- Vacation start (cancel pending working orders), end

### 6. Error Handling Strategy
- Categorization (transient / persistent / catastrophic)
- Per-Service Degradation Matrix realization
- Order Rejection Taxonomy implementation
- Idempotency for order placement and audit writes
- Specific handling per matrix

### 7. Observability
- Logging schema (structlog JSON, fields per category; local file via logrotate; daily S3 upload)
- Metrics inventory (Prometheus or equivalent; what's measured, frequency, retention)
- Health check endpoints consumed by external watchdog
- Dashboard recommendation
- Agent telemetry consumption
- Alert routing logic (P0/P1/P2; severity flags `routine` / `defensive_envelope` / `incident_review`)
- Cost tracking integration

### 8. Security
- sops + age implementation; per-environment file structure; age key backup procedure
- Postgres role hierarchy with break-glass procedure (incl. paper key storage)
- File permissions / service user model
- Network exposure
- API auth for frontend (opaque session ID + CSRF double-submit)
- Audit log immutability (BEFORE UPDATE/DELETE trigger + EVENT TRIGGER for TRUNCATE + REVOKE TRUNCATE; explicit row-trigger TRUNCATE limitation acknowledgment)
- Backup encryption keys
- Repo / build-chain DR (Gitea + S3)
- Account recovery procedure
- GitHub workflow

### 9. Deployment Topology
- VPS specs (Hetzner Ashburn — recommend size with justification; backend + frontend co-located)
- External watchdog topology
- Docker Compose layout (Caddy/Traefik reverse proxy routing /api/* + /sse/* to FastAPI, else Next.js; separate Discord-bot service + webhook-pusher service on shared internal network)
- Environment configuration (dev local, paper, live)
- Deployment procedure (manual + agent-driven hot-fix; whitelist enforcement; pre-merge linter)
- Rollback procedure
- DR runbook (TWS, IBKR phone, Gitea-based rebuild)

### 10. Testing Strategy
- Unit and integration test inventory (full list above)
- CI/CD pipeline (GitHub Actions)
- Pre-merge gates including hot-fix forbidden-path linter
- Strategy validation pipeline (paper-minimum mechanical enforcement; CI blocks deploy-to-live on `paper_days_for_version < 30`)
- vectorbt-vs-LEAN parity test design (cumulative P&L divergence > 0.1% threshold)
- QC adapter golden-test parity design
- Continuous-vs-physical contract reconciliation at futures rolls
- Slippage calibration verification

### 11. Phased Build Plan
- **Phase 0 (weeks 0–8):** foundation; v1 strategy authored weeks 0–1; paper begins week 1; QC adapter coded + golden-tested by week 4; 30 paper sessions complete within weeks 1–7; week 8 buffer; extends if 30-session minimum slips
- **Phase 1 (months 2–5):** live track record on QC; custom backend skeleton in parallel
- **Phase 2 (months 5–9):** custom infra hardening; LEAN Local; ib-async; paper validation; cutover with pre-cutover checklist
- **Phase 3 (months 9–12):** capital scaling; second-strategy preparation; legal structure
- Each phase: deliverables, success criteria, kill criteria

### 12. Claude Ops Agent Detailed Spec
- Trigger model
- Tool inventory (bounded actions; hot-fix whitelist; defensive trim invocation; tighten-direction enforcement)
- Prompt-cache strategy
- Cost budget
- Failure mode handling (Claude API outage, hallucination detection, rate limits)
- Audit trail (every decision with prompt + response)
- Rollback mechanism (auto-rollback metrics)
- Operator-Friendly PR Review Surface — full rendering spec

## FORMAT REQUIREMENTS

- Markdown with clear section headers
- Mermaid for ALL diagrams
- Concrete library/tool/version recommendations
- Where implementation choices remain, present 2–3 options with tradeoffs and recommendation
- Length will be substantial; favor completeness over brevity
- Never invent strategic decisions; flag missing context with `[QUESTION FOR OPERATOR: ...]`
- Reference Prompt B's IA, SSE event types, Discord command schemas, and canonical vocabulary by name

Begin.
