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

### Strategy

- **Phase 1 strategy:** multi-asset systematic trend-following on micro futures + bond ETFs
- **Universe:** ~8–12 markets — equity index micros (/MES, /MNQ, /M2K, /MYM), commodity micros (/MCL, /MGC, /MSI), Bitcoin micro (/MBT), bond ETFs (TLT, IEF, SHY); optional FX micros (/M6E)
- **Signal type:** time-series momentum / breakout (Donchian channels, MA crossovers); vol-targeted sizing; daily bars
- **Daily bar definition (locked, per asset class):**
  - Futures (CME-listed: equity index micros, commodity micros, /MBT, /M6E): close = **CME daily settlement, 17:00 ET**
  - ETFs (TLT, IEF, SHY): close = **NYSE close, 16:00 ET**
- **Signal generation cadence:** runs once daily at **17:30 ET** (after both close anchors); processes all markets together using that day's settles/closes
- **Holding period:** 2 weeks to 6 months
- **Phase 2+:** add second uncorrelated strategy (likely defined-risk vol carry on SPX) only after Phase 1 live validation; sequential strategy addition, never parallel cold-start
- **Base currency:** USD only. No FX hedging. Foreign instruments (if any FX micros are added) settle and convert at IBKR's standard rate.
- **Account model:** single live IBKR Pro account in operator's name. No sub-accounts, no prop-firm splits in Phase 1. Schema must support multi-account in future without migration (use `account_id` foreign key throughout).
- **PDT / Reg T constraints (locked):**
  - Futures use SPAN margin; PDT does not apply.
  - ETFs use Reg T (50% initial, 25% maintenance). PDT rule (no more than 3 day-trades in 5 rolling days) applies while account equity < $25k.
  - **Risk engine MUST refuse any ETF order that would create a 4th day-trade within a 5-day rolling window if account equity < $25k.** This is a hard pre-trade check.
  - Portfolio Margin not in scope (requires $125k+; revisit only when family capital lands).

### Path / Phasing

- **Phase 0 (weeks 0–7, extended again):** foundation — operator upskilling, IBKR Pro account opening, QC subscription, repo + CI scaffolding, secrets management (sops), Hetzner VPS provisioned, audit schema designed and migrated, **paper trading begins on QuantConnect in week 1 with the v1 strategy**, **QC ObjectStore audit adapter coded and golden-tested against custom-format target by week 4**, **30 NYSE trading days of paper completed by end of week 6** (week 7 is buffer + Phase 1 handover).
- **Phase 1 (months 2–5):** live trading on QuantConnect Cloud (LEAN). Real money, small size (`live-small`). Track record begins immediately. $20/month QC Quant Researcher tier (upgrade to $80 only if backtest queue bottlenecks). Custom backend skeleton runs in parallel, ingesting QC audit events.
- **Phase 2 (months 5–9):** custom infrastructure built and hardened; strategy execution migrates to LEAN Local (Docker-hosted) with vectorbt as fast research/sweep layer; track record is unbroken via continuous audit log schema.
- **Phase 3 (months 9–12):** capital scaling, second-strategy preparation, family-money legal structure work.
- **Phase 1 → Phase 2 cutover for open positions:** flatten all open positions on QC at end of cutover session; reconcile audit log to terminal state; restart fresh on LEAN Local the following morning. **No position transfer across execution venues.** Audit log remains continuous; physical positions reset to flat.

### Tech Stack (locked)

- **Language:** Python 3.11+ end to end
- **Engine:** LEAN (QuantConnect Cloud Phase 1; LEAN Local self-hosted via Docker Phase 2). **LEAN is the AUTHORITATIVE backtest engine for the PR review surface.** vectorbt is research-only for parameter sweeps.
- **Research/sweep:** vectorbt (or vectorbt-pro)
- **Storage:** DuckDB on Parquet for historical/research/analytics; PostgreSQL 16 (containerized) for transactional state
- **Postgres driver:** asyncpg with SQLAlchemy 2.x async
- **Migrations:** Alembic
- **Broker library:** `ib-async` (community-maintained fork of the now-unmaintained `ib_insync`; same API surface). Phase 1 routes via QC's IBKR integration; Phase 2 direct via `ib-async` to IB Gateway in Docker.
- **Margin model:** SPAN for futures (broker-computed); Reg T for ETFs.
- **Orchestration:** cron + APScheduler within Python services. **Scheduler and calendar service are co-located in a single APScheduler-backed Python process.** Job store is **persistent (Postgres-backed)** so scheduled jobs survive restarts. NO Airflow/Prefect/Dagster.
- **Real-time push:** SSE for browser one-way push; REST for everything else; **NO WebSocket.**
- **Deployment:** Single VPS, Hetzner Cloud Ashburn (US East), Ubuntu LTS, Docker Compose. NO Kubernetes. **Single-host accepted; see RPO/RTO below.**
- **Process supervision:** Docker Compose restart policies + systemd for the host; chrony for NTP
- **Logging:** `structlog` with JSON renderer. OpenTelemetry tracing optional Phase 2+, not required Phase 1.
- **Validation:** pydantic v2 for all schema-bound data models
- **API exposure:** FastAPI on the VPS

### Data Sources (locked)

- **Phase 1:** QuantConnect bundled equities + futures data (sufficient for backtest + live signal). IBKR real-time market data (free to account holders for our universe).
- **Phase 2 additions (Polygon.io is CONTINGENT, not committed):** Polygon.io Stocks Starter ($30/mo) **only if** QC bundled equity data has gaps the strategy notices in Phase 1 live. Default Phase 2 = no Polygon. **FRED** (free) for macro context. Economic calendar via **Forex Factory or Trading Economics** (free tier or low-cost).
- **NOT in scope:** Norgate Data (Linux Docker friction), alt data, NLP feeds, Bloomberg, Databento, multi-tier feeds.
- **Survivorship-bias / continuity claims (precise):** QC's bundled data is survivorship-bias-handled for the equity/ETF leg (delisted instruments included). For the futures leg, the relevant correctness criterion is **roll methodology**, not survivorship; QC uses continuous-contract construction with documented roll rules (typically Panama-method / open-interest-based). LEAN execution uses physical contracts, so backtest continuous-contract returns must reconcile to physical-fill returns at cutover dates — this reconciliation is part of testing.

### Risk Framework (concrete math; locked)

#### Position sizing
- Volatility-targeted per position
- Portfolio annualized vol target: **14%** (locked single value)
- Instrument vol estimate: rolling 60-day standard deviation of daily log returns
- Position size formula: `position_notional_i = (per_position_vol_target × equity) / (instrument_daily_vol_pct × sqrt(252))`
- `per_position_vol_target` allocates the portfolio target across active markets via inverse-vol weighting, capped by per-position and per-cluster ceilings

#### Equity and DD anchors (locked)
- **Daily-start MTM anchor:** **17:00 ET** (CME daily settlement boundary), portfolio-wide. Daily P&L = `MTM(t) − MTM(prior 17:00 ET snapshot)`. NOT NYSE 09:30; CME settle is the relevant boundary for our predominantly-futures book.
- **Trailing DD reference:** peak intraday MTM equity since system inception, including unrealized.
- **Capital-event handling (deposit / withdrawal):** on any deposit ≥ 5% of current equity (specifically including the eventual $250k family deposit):
  - Trailing DD reference (peak MTM) **resets to current equity at deposit time**
  - System enters "capital event mode" for **30 sessions**: peak MTM tracked from deposit date forward; convalescent-style 50% vol target for first 5 sessions to dampen sizing whiplash
  - Audit log entry for capital event with full provenance (timestamp, amount, source, new peak MTM baseline)
  - All historical track record preserved; environment tag transitions according to new equity (see Environment Tagging)
- **Withdrawals:** symmetric; peak MTM does NOT reset on withdrawal (would otherwise create perverse incentive to withdraw to reset DD).

#### Risk rings (all units explicit)
All rings measured against **mark-to-market equity**, **gross/net in instrument notional terms** (not margin), unless stated otherwise.

| Ring | Limit | Measurement Basis |
|---|---|---|
| Per-position max | 25% of equity notional | Sum of \|notional\| for that single market |
| Gross portfolio max | 300% of equity notional | Sum of \|notional\| across all positions |
| Net portfolio max | 150% of equity notional | Signed sum of notional across all positions |
| Equity-index cluster max | 60% of equity notional gross | Combined /MES, /MNQ, /M2K, /MYM |
| Commodity cluster max | 80% of equity notional gross | Combined /MCL, /MGC, /MSI |
| Rates/bonds cluster max | 80% of equity notional gross | Combined TLT, IEF, SHY |
| Crypto cluster max | 40% of equity notional gross | /MBT |
| Realized cross-portfolio correlation | Alert at avg pairwise > 0.7; HALT_NEW at > 0.85 | 60-day rolling realized correlation matrix across open positions |
| Daily loss limit | -5% of daily-start MTM (17:00 ET anchor) | Portfolio-wide |
| Trailing drawdown limit | -20% from peak intraday MTM equity | Subject to capital-event reset rule |
| Monthly DD threshold | -10% in calendar month | Triggers vol-target halving (to 7%) for remainder of month |
| Strategy decommission floor | Auto-halt + human review required | (a) live 30-day Sharpe < 0, OR (b) live max DD breach -25%, OR (c) 60-day live Sharpe underperforms backtest by > 2 SD |

#### Vol regime detector
- Metric: 60-day rolling realized volatility of portfolio daily returns
- Z-score: current value vs. its own 60-day historical distribution
- Trigger: z-score > 2 → kill-switch fires (HALT_NEW)

#### Signal storm detector
- Metric: portfolio total trade count in current session vs. rolling 30-day mean daily trade count
- Trigger: current session count > 3× the mean → kill-switch fires

#### Margin protocol — graduated defensive de-leverage (NOT panic-flatten)

Margin auto-trim is a graduated reduction of weakest positions to maintain margin headroom. **It is explicitly carved out from the "no auto-flatten" principle because it is bounded, ordered, and capped — not a full unwind.**

- 70% of available margin used → warn alert (no action)
- 85% of available margin used → auto-trim sequence:
  1. Compute **momentum score** for each open position: rolling 60-day z-score of price returns (relative to instrument's own distribution). Lower z-score = weaker.
  2. Rank by momentum score ascending (weakest first); tie-break by largest absolute margin contribution.
  3. Cut positions in rank order via **marketable-limit orders** (1× spread, escalating to 2× spread on retry; NOT pure market orders — avoids panic-execution dynamics).
  4. **Hard cap per session: -30% of gross exposure across the entire sweep.** No full unwind in one shot.
  5. Cut until used margin < 60% OR session cap reached, whichever comes first.
  6. If margin still > 80% after one full sweep, escalate to HALT_NEW (no further trims; force human review).
  7. Log every cut to audit_log with `reason: margin_auto_trim` and full provenance.

#### Capacity tracking
- Rolling 30-day average daily volume (ADV) computed per market
- Order size as % of ADV computed at signal-emit time
- Alert at 0.5% ADV; partial-fill cap at 2% ADV
- **Capacity refusal at 2% ADV → partial fill at the cap, not refuse.** If signal demands more, fill what's possible; tag remainder as `capacity_constrained` in attribution; position then sized at actual filled fraction of intended.

### Kill-Switch State Machine (explicit)

States:
- **NORMAL** — full operation; all entry and exit signals processed
- **HALT_NEW** — cancel all working orders; hold all existing positions (no liquidation); no new entries; only stop-out exits permitted; alerts to all channels; **manual human resume only**
- **CONVALESCENT** — 50% vol target (7% portfolio vol); entries permitted; remains for 5 trading sessions portfolio-wide (counted by NYSE session days); auto-transitions to NORMAL on completion

Transitions:
- `NORMAL → HALT_NEW`: any kill-switch trigger fires
- `HALT_NEW → CONVALESCENT`: human invokes resume via web app or Discord (with re-auth)
- `CONVALESCENT → NORMAL`: 5 trading sessions complete without breach
- `CONVALESCENT → HALT_NEW`: any kill-switch trigger fires; 5-session counter resets on next resume
- **No HALT_ALL or auto-liquidate state.** Carve-out: graduated margin auto-trim (above) is NOT a panic-flatten and is the sole sanctioned auto-de-leverage path.

#### CONVALESCENT counter reset events (locked)

| Event | Resets 5-session counter? |
|---|---|
| Kill-switch trigger fires while in CONVALESCENT | YES (returns to HALT_NEW; counter reset on next resume) |
| Reconciliation false-positive resolved within tolerance | NO |
| Calendar ratification grace window | NO |
| Heartbeat engagement timeout | NO (this triggers Defensive Risk Envelope path; see Communications) |
| Capital event (deposit triggering capital-event mode) | YES |

#### Kill-switch triggers (any of the following → `→ HALT_NEW`)
- Trailing DD breach (-20% from peak MTM equity, subject to capital-event reset)
- Daily loss breach (-5% of daily-start MTM, 17:00 ET anchor)
- Signal storm (above)
- Reconciliation mismatch (delta exceeds tolerance band — see Reconciliation Tolerances table)
- Broker disconnect persisting > 5 minutes during market hours
- Vol regime detector trip (above)
- Realized cross-portfolio correlation > 0.85 (above)
- Decommission floor trigger (above)
- Audit log write failure (audit integrity is non-negotiable)
- Any unhandled exception in execution path
- **Heartbeat engagement failure (= Defensive Risk Envelope path; identical state, distinct alert routing — see below)**

### Defensive Risk Envelope (clarified — same state, different label and routing)

Defensive Risk Envelope is the term used when the kill-switch trigger is **comms breakdown** specifically (heartbeat engagement failure). Behavior is identical to other HALT_NEW transitions; only the trigger name and **alert routing differ** (Defensive Envelope escalates more aggressively via email backup and external watchdog).

The earlier "Defensive Envelope is NOT a kill-switch" framing was wrong; corrected here to: comms-breakdown trigger fires the standard kill-switch flow, but with the Defensive Envelope label and routing.

### Risk-Tightening Boundary (parameter changes vs. position trims)

The agent's "tighten risk" authority is implemented via TWO distinct paths to avoid the parameter-change-vs-mid-session collision:

1. **Parameter changes** (e.g., reducing vol target multiplier within range): take effect at NEXT signal cycle, never mid-session. Used for sustained tightening.
2. **Defensive position trims**: direct order action mid-session that reduces exposure without touching parameters. Uses the same auto-trim algorithm as margin auto-trim (momentum-ranked, marketable-limit, capped at -30% of gross per session). Used for immediate tightening.

Agent authority covers both paths under "tighten risk = AUTO with notification." Audit log distinguishes the path used.

### Auto-Revert Thresholds (parameter changes — widened to be robust against noise)

A parameter change made by the agent (within pre-approved code-defined ranges) auto-reverts when **any** of the following:
- 30-day rolling live Sharpe drops > **2 SD** (not 1 SD — was a hair trigger) from pre-change 30-day baseline within 30 sessions of the change, AND minimum 30 trades on changed market(s) in window
- Max DD breaches -10% within 5 sessions of the change
- 5+ consecutive losing trades attributable to the changed parameter (deterministic attribution: trades where the changed parameter directly affected signal generation for that market — i.e., the signal computation reads the changed parameter)

Auto-revert action: parameter restored to pre-change value; full audit entry; alert to operator; no further auto-changes to that parameter for 14 days.

### Logic-Change vs. Parameter-Change Boundary (clarified)

- **Logic change** (requires PR + human merge): changes to *which signals fire* — rule logic, indicator selection, market universe, strategy structure, sizing model, risk-ring values, cluster definitions, parameter ranges themselves.
- **Parameter change** (auto with audit, within pre-approved range): changes to *parameters governing existing signals* — values within the ranges defined in the Parameter Ranges table below.
- **Pre-approved range itself is logic.** Changing a range requires a PR.
- **Parameter changes take effect at next signal cycle (next session), never mid-session.** Hard-coded.

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

**These are agent-mutable within Min/Max.** Any change outside Min/Max requires a PR.

**Risk-ring values (per-position 25%, gross 300%, net 150%, cluster caps) are NOT in this table.** They are constants in code; agent CANNOT modify them; PR required.

### Reconciliation Tolerances Table (LOCKED)

A delta exceeding the tolerance band → kill-switch trigger. Within tolerance → benign, logged, no action.

| Metric | Tolerance | Grace Period |
|---|---|---|
| Position quantities (per instrument) | 0 (exact) | None |
| Cash balance (USD) | greater of $5 absolute or 1 bps of equity | T+1 grace for fees, dividends, interest postings |
| Margin balance | $10 absolute | None |
| FX-denominated cash (e.g., for /M6E) | $1 absolute | T+1 for FX rounding |
| Realized P&L (cumulative) | $1 absolute | T+1 |
| Unrealized P&L | $5 absolute | None |

**Tolerances widen by 2× during dividend ex-dates for +24h** (auto-detected from instrument calendar).

### Per-Service Degradation Matrix (LOCKED)

| Failure | System Response |
|---|---|
| Risk engine down | Signal engine halts; no signals can pass risk check; HALT_NEW |
| Reconciliation stale > 60s during market hours | HALT_NEW (kill-switch trigger) |
| Calendar service can't reach FRED/Forex Factory | Use last successful import; alert; if last successful > 48h, hard halt new orders next session until manual ratification |
| QC ObjectStore poll fails 5–9 min | Alert only; transient is expected |
| QC ObjectStore poll fails > 10 min | Defensive Risk Envelope (HALT_NEW + escalated routing) |
| Backend can't reach IBKR > 5 min during market | HALT_NEW (kill-switch trigger) |
| Discord delivery fails | Email backup automatic; external watchdog covers VPS-down case |
| Database write fails (non-audit) | Retry 3× with backoff; on persistent failure, HALT_NEW |
| Database write fails (audit_log) | HARD HALT immediately; audit integrity non-negotiable |
| Anthropic Claude API down | Agent service degrades to read-only mode; no agent-driven actions; alert; trading continues unaffected (agent is supervisory, not in critical path) |
| External watchdog unreachable | Alert; system continues; if + Discord delivery also failing, defensive risk envelope |

### Data Quality Handling (LOCKED)

Per-bar validation at ingestion (data feed boundary):

**Reject (do not act on, log + alert) if any:**
- Close price = 0 or negative
- OHLC contains NaN
- High < Low
- Volume = 0 for futures during the relevant session window
- Bar timestamp is outside expected range (more than 2× normal bar period late)

**Quarantine (don't act on, log, no alert escalation) if any:**
- |close − prev_close| > 10× rolling 30-day daily range (price spike)
- Volume < 10% of rolling 30-day average for that market
- Bar arrived > 30 min late but within 2× period

**On rejected or quarantined bar:** skip signal generation for that market that day; log to `audit_log` and to a `data_quality_events` table; continue normal processing for other markets.

### Execution Mechanics

- **Order types:**
  - Entries: limit-marketable (last ± 0.5× spread); on retry, widen to 1× spread, then 1.5× spread
  - Exits (stop): stop-market for execution certainty
  - Profit-target exits: limit at target
  - Futures rolls: calendar spread orders when broker supports; otherwise leg with 60s stagger
  - Kill-switch action: cancel all working orders; **hold positions** (no liquidation, no flatten)
  - Margin auto-trim and defensive position trims: marketable-limit (1× spread, escalating to 2× on retry); never pure market
- **Retry logic:** order rejection → 3 retries with exponential backoff (1s, 4s, 16s); after 3 failures, halt that market only, alert
- **Reconciliation source-of-truth (locked):**
  - Intraday risk decisions: TWS API real-time portfolio snapshot
  - End-of-day reconciliation and tax: IBKR FlexQuery (XML)
  - Both reconciled at session close; FlexQuery is authoritative for tax + audit reporting
  - Tolerance bands per Reconciliation Tolerances Table above
- **Reconciliation cadence:** every session open + close + EOD full cross-check; weekly summary report
- **Roll discipline:** futures rolled per `ROLL_DAYS_BEFORE_EXPIRY` parameter, off-peak liquidity scheduling
- **Macro event handling:**
  - Auto-pause order placement from 5 min before through 30 min after scheduled tier-1 events (FOMC, CPI, NFP, GDP, PCE, ECB/BOJ/BOE if exposed, OPEC if /MCL exposed)
  - **NO manual event mode override.** Rules-based only.
  - Calendar auto-imported nightly
  - User ratifies tomorrow's events via Discord by **23:00 ET nightly**
  - **Default if no ratification by 23:00 ET:** hard halt new orders for next session until ratified (forces engagement)
  - **EXCEPTION during vacation mode:** ratification gate is suspended (vacation already disables new entries; no entries to halt). Calendar still imported nightly; ratification not required.
  - **Macro window vs. session boundary collision:** pause wins. New signals deferred to next session if pause window straddles close.

### Audit & Track Record

- **Immutability mechanism (explicit):**
  - `audit_log` table protected by Postgres triggers blocking `UPDATE`, `DELETE`, `TRUNCATE` operations
  - Service role granted only `INSERT, SELECT` on `audit_log`; `REVOKE UPDATE, DELETE, TRUNCATE`
  - **Hash chain:** SHA-256 single-linked list **ordered by INSERTION sequence, NOT by event time**. Each record's `prev_hash` references prior record's full hash; `record_hash` is SHA-256 over `(prev_hash || record_payload)`. Genesis record has `prev_hash` = 32 zero bytes.
  - **Backfill / repair handling (e.g., from QC log gap-fill):** backfilled records APPEND at chain tail (current sequence number), carrying `repaired_for_sequence_no` and `repaired_for_event_timestamp` provenance. **The original gap remains visible.** No retro-insertion. The hash chain integrity is preserved because the chain is over insertion order, not event order. Audit explorer renders the relationship visually.
  - Backups: written to S3 with **Object Lock (Compliance mode)**; retention 7 daily / 4 weekly / 12 monthly / permanent annual; quarterly restore drill mandatory. Backups themselves immutable for the lock period.

- **Postgres role hierarchy (locked):**
  - `app_service` — used by all application services. Grants: `INSERT, SELECT` on `audit_log`; `SELECT, INSERT, UPDATE, DELETE` on non-audit tables. Cannot bypass audit triggers.
  - `app_owner` — schema owner; runs Alembic migrations. Cannot bypass audit triggers (triggers use SECURITY DEFINER and check role; even owner blocked from `UPDATE`/`DELETE` on `audit_log`).
  - `dba_breakglass` — separate credential, kept offline, sops-encrypted. Superuser. **Use only via documented break-glass procedure**: login generates a high-severity audit entry (out-of-band, written before any other action), requires post-event review write-up, all session activity logged via session_replication_role for full audit trail.

- **Strategy version + parameter set composite identity (locked):**
  - `strategy_hash` = git commit SHA of strategy code at signal time
  - `parameter_set_hash` = SHA-256 over canonical-serialized active parameter values at signal time
  - `parameters` table is event-sourced with `valid_from`/`valid_to` per-row; queryable by timestamp
  - Every trade tagged with **both** `strategy_hash` AND `parameter_set_hash`; queries can filter by either or by composite

- **Track record portability:** identical audit schema between QC Phase 1 and custom Phase 2; QC adapter must emit audit records in the custom-target schema with byte-for-byte identical structure (golden-test parity required, see Testing)

- **Environment tagging (locked transition rules):**
  - `paper` = any non-real-money trade, regardless of capital
  - `live-small` = real money, account equity < $50k at signal time
  - `live-scale` = real money, account equity ≥ $50k at signal time
  - **Transition is determined at signal-emit time** based on account equity at that instant (read from latest reconciliation snapshot)
  - Tag is immutable per trade; never re-stamped later
  - **Never blended in any reporting** — see frontend spec

- **Paper minimum:** 30 NYSE trading days paper before any live deployment of a new strategy version (per strategy version)

- **Trade-level attribution:** computed at signal-emit time by strategy code (vol regime, trend regime, expected P&L, expected slippage); realized values fill in post-trade. Captured fields are immutable post-emit.

### QuantConnect Audit Adapter (Phase 1 critical path)

The Phase 1 audit log lives in our backend, not QC. The adapter must be loss-tolerant.

- **Mechanism:** QC algorithm writes audit events to **QC ObjectStore** (durable, project-scoped storage) as JSONL with monotonic sequence numbers per session. NOT QC's `Notify.Web` (rate-limited, no retry, lossy).
- **Backend ingestion:** custom service polls QC ObjectStore via QC API every 60s during market hours; reads incrementally with cursor (last sequence number persisted to Postgres); resumes from last cursor on backend restart
- **Schema:** identical to custom-emitted audit records. Golden-test parity verified weekly: same input event produces byte-for-byte identical record from QC adapter and from native custom emitter.
- **Loss handling:** if cursor gap detected (sequence number jump):
  - Alert; pull from QC's own logs to fill gap
  - Backfilled records APPENDED at current chain tail with `repaired_for_sequence_no` provenance (per hash-chain repair procedure above)
  - Affected trades flagged `audit_repaired` in attribution
- **Failure mode:** if QC ObjectStore unavailable for > 10 min, Defensive Risk Envelope (HALT_NEW) — audit integrity is non-negotiable
- **Clock skew handling:** every ingested event carries both `source_clock_ts` (QC-side timestamp) and `ingest_clock_ts` (backend ingestion timestamp); monotonic ordering preserved via `ingest_clock_ts` for chain hashing

### Tax Handling (corrected)

- **Futures (Section 1256 contracts):** treatment is automatic 60% LTCG / 40% STCG with mandatory year-end mark-to-market. **No election required.** System reports Form 6781 data.
- **ETFs (securities):** standard capital gains/losses with wash sale tracking. **No 475(f) trader-status election by default**; system supports both modes for future flexibility but defaults to non-elected.
- **CPA consultation REQUIRED** before any tax election; system documentation and onboarding flow must explicitly surface this. No election toggle in the UI without an "I have consulted a CPA" acknowledgment.
- **Wash sale tracking** across all accounts via `account_id` linkage in trade records
- **Year-end harvest flagging:** system surfaces unrealized losses with low-strategy-impact harvest opportunities
- **Tax export:** CSVs structured for Form 6781, Schedule D, Form 8949; PDF summary; importable by Drake / ProSeries / TurboTax. Annual export triggered Jan 31 each year.

### Claude Ops Agent — Authority Matrix and Boundaries

A separate long-running Python service alongside the trading engine.

| Category | Agent Authority | Implementation Note |
|---|---|---|
| Tighten risk via parameter change (within range, next-cycle) | AUTO with notification | Goes through risk engine API; parameter store update; effective next signal cycle |
| Tighten risk via defensive position trim (mid-session) | AUTO with notification | Direct order action via momentum-ranked auto-trim path; capped at -30% gross per session |
| Loosen risk (raise sizes, increase caps, restart after halt) | HUMAN APPROVAL REQUIRED | Hard-coded as denied capability; only operator-authenticated requests can loosen |
| Hot-fix infrastructure (logging, retry, monitoring, dependency, broker reconnect) | AUTO-DEPLOY with notification + automatic rollback if metrics degrade within 30 min | Whitelist of allowed file paths; any file outside whitelist requires PR |
| Strategy logic changes (signal rules, indicator selection, universe, sizing model, risk-ring values, parameter ranges themselves) | DRAFTS PR; human reviews and merges | See Operator-Friendly PR Review Surface |
| Place / modify / cancel orders directly (i.e., order placement as a primary action) | NEVER, hard-coded block | Agent service has no broker API credentials; physically cannot place orders. *Note:* defensive position trims are placed by the **risk engine** in response to agent's "tighten risk" call, not by the agent itself. |
| Invoke kill switch | AUTO on hard threshold breach | |
| Un-invoke kill switch | HUMAN APPROVAL ONLY (with re-auth) | |
| Modify strategy parameters within pre-approved range | AUTO with full audit log + auto-revert per thresholds (above) | Effective only at next signal cycle |
| Generate reports, alerts, briefings, run diagnostics | AUTO | |

The agent reads logs/metrics, calls Claude API on triggers, takes bounded actions, generates morning briefings and weekly reports, drafts PRs for review. **Never any direct trading authority.** Agent service has zero broker credentials.

### Operator-Friendly PR Review Surface (critical — operator is non-coding)

Every PR (agent-drafted or human-drafted) that touches strategy logic or risk parameters must surface the following review artifacts to the operator. The diff is reference; the actionable artifact is the first three items.

1. **Plain-English summary** (max 200 words) — written by agent: what changed, why, what behavior changes. Required.
2. **Risk impact summary** (auto-generated) — which risk metrics affected, by how much, in plain numbers (e.g., "expected daily P&L variance increases from $180 to $220 at current capital").
3. **Backtest delta** — produced by **LEAN (authoritative)**; current strategy version vs. proposed: equity curve overlay, key statistics delta table, ten worst-divergence trades highlighted.
4. **Test results** — unit + integration + linting + type-check, all visible with pass/fail.
5. **Diff view** — collapsed by default, expandable on click.
6. **Files affected** — list with one-line summary per file.
7. **In-app Approve / Reject / Request Changes buttons** — sync to GitHub via API.

The operator's review competence is on the plain-English + risk impact + backtest delta. Spec the rendering of these clearly.

### Decision Diary

- **Operator writes:** mandatory minimum-10-character reasoning on every signal rejection, defer, or override
- **Agent writes:** suggestions / commentary, optional
- **Required fields per entry:**
  - `tag`: enum of `data_concern` | `regime_concern` | `size_concern` | `manual_judgment` | `other`
  - `timestamp`: UTC + monotonic
  - `author`: `operator` | `agent`
  - `reasoning_text`: free text, min 10 chars when author = operator
  - `linked_signal_id`: UUIDv7 reference

### Communications

- **Primary user surface (mobile):** Discord bot via `discord.py`. Channels: `#daily-brief`, `#signals`, `#fills`, `#alerts`, `#critical`, `#ops`, `#ask-agent`, `#audit`. Slash commands and button interactions.
- **Backup channel:** email (silent fallback if Discord delivery fails)
- **Discord-bot-as-service architecture:** the Discord bot inbound (gateway WS) and the backend's outbound event push to Discord run as **two services on the same Docker network sharing a sops-decrypted secret bundle**, NOT in one container. Backend posts events to bot's local HTTP listener (internal IPC, not public webhook).
- **Heartbeat — split semantics (locked, accounting for backup channel):**
  - **Delivery** = HTTP 2xx ack from Discord webhook on push. Failure → automatic email backup; alert operator out-of-band.
  - **Engagement** = ANY of:
    - Discord reaction or reply on critical alerts
    - **Email reply** to email-backup notifications
    - Web app authenticated activity within session
  - No engagement for > 24h on any critical alert → Defensive Risk Envelope (HALT_NEW with escalated routing). Email reply DOES count as engagement; system does not halt purely because Discord is down if operator is engaging via email.
- **Vacation mode:** operator runs `/vacation start [days]` in Discord
  - Engagement timeout extends to 7 days
  - New strategy entries auto-disabled (only stop-out exits permitted)
  - Daily summary still posts
  - **Macro-event ratification gate suspended** (no entries to halt; calendar still imported nightly)
  - On `/vacation end` or expiry, normal operation resumes
- **NO SMS, NO voice escalation.** Operator treats Discord like text/calls.
- **External watchdog (mandatory):** separate-region tiny VPS or AWS Lambda pings backend `/health` every 5 min. Unreachable > 15 min during market hours → email to operator. ~$5/month.

### Security

- **Secrets management:** Mozilla **sops** with **age** encryption. Encrypted secret files committed to repo; decrypted at deploy time on the VPS. Avoids running a Vault server. **Age key backup: printed paper copy in offline cold storage** (operator's safe).
- **Secret rotation cadence:** quarterly forced; immediate on compromise
- **Database backups:** daily encrypted to S3 with Object Lock (Compliance mode); retention tier above; quarterly restore drill mandatory
- **Encryption at rest:** Hetzner volume encryption for live DB; application-level encryption for high-sensitivity columns
- **Auth (web frontend):** WebAuthn (passkey) primary + TOTP backup + 8 single-use printed backup codes generated at enrollment (recovery path; codes regenerable from authenticated session). **If both passkey device AND TOTP device AND backup codes are lost: full system reset required via dba_breakglass break-glass procedure + sops-encrypted backup restore + manual identity re-establishment. Document this procedure explicitly.**
- **Auth tokens:** JWT access (15-min lifetime) + refresh token (7-day lifetime). HttpOnly + Secure + SameSite=Strict cookies. Server-side session records in Postgres for revocation. Sensitive actions require re-auth (WebAuthn UV re-prompt) within last 5 minutes.
- **Container hardening:** non-root users, read-only filesystem where compatible, no privileged containers, **Trivy** image scanning in CI, distroless base images where compatible
- **Network egress allowlist on host:** IBKR endpoints, Anthropic API, AWS S3 (or Backblaze B2), NTP pool, Ubuntu/Debian package mirrors, GitHub. Everything else denied via UFW/iptables.
- **Network ingress:** only FastAPI public endpoint (HTTPS via Caddy or Traefik with Let's Encrypt) and SSH (key-only). Internal services on internal Docker network only.
- **Repo / build-chain DR:** mirror to **self-hosted Gitea on the VPS** (full repo mirror, daily sync from GitHub); weekly encrypted repo archive to S3. If GitHub unreachable during a Hetzner-rebuild scenario, restoration possible from Gitea + sops + S3.

### Time and Clock

- **NTP:** chrony daemon, primary `pool.ntp.org`, fallback `time.cloudflare.com`
- **Clock skew tolerance:** log warn at > 100ms; defensive halt at > 1s
- **Audit ordering:** records carry `timestamp_utc` AND `monotonic_ns` (`time.monotonic_ns()`) within process; monotonic for relative ordering within process; UTC for cross-service ordering with skew tolerance; QC-ingested events additionally carry `source_clock_ts` and `ingest_clock_ts`
- **All schema timestamps:** `TIMESTAMPTZ` in Postgres, stored as UTC, rendered in `America/New_York` at presentation layer

### Idempotency

- **All writes:** UUIDv7 primary keys (time-ordered, sortable, unique)
- **Order placement:** `client_order_id = "{strategy_short_hash}-{paramset_short_hash}-{signal_uuid}-{retry_n}"` — IBKR-side dedup
- **Audit writes:** UUIDv7
- **Webhook re-delivery:** receiver dedupes by `event_uuid` for 7-day window via Postgres unique constraint

### SLO Budgets

- **Signal-to-order latency:** p50 ≤ 60s, p99 ≤ 5min
- **Kill-switch invocation latency:** ≤ 5s from trigger to broker order cancellation request
- **Reconciliation freshness during market hours:** ≤ 60s from broker state to internal mirror
- **Discord webhook delivery:** ≤ 10s p99
- **Backtest queue:** acceptable p99 ≤ 30 min on QC tier; if exceeded persistently, upgrade tier

### RPO / RTO

- **RPO:** 15 minutes (Postgres WAL ship to S3 every 15 min via `wal-g` or equivalent)
- **RTO during market hours:** 4 hours
- **RTO outside market hours:** 24 hours
- **Single VPS accepted; no warm standby.**
- **DR runbook (mandatory deliverable):** Hetzner-Ashburn outage:
  1. External watchdog email triggers within 15 min
  2. Operator opens TWS desktop on personal laptop
  3. Manually closes risky positions per documented playbook
  4. Calls IBKR phone trading desk if web/desktop also unavailable
  5. Restores VPS from latest backup on recovery
  6. Reconciles audit log against IBKR FlexQuery for outage period; flag affected trades as `outage_period`

### Backtesting Validation

- **Walk-forward analysis:** rolling 3-year train, 6-month out-of-sample, advance, repeat
- **70/30 in-sample / held-out test split.** Held-out touched ONCE at end of strategy development; documented in audit
- **Survivorship-bias / continuity:** see Data Sources section for precise per-leg claims
- **Realistic fills (calibration procedure, locked):**
  - For each Phase 1 live fill: log `expected_price` (from LEAN's slippage model) and `actual_price`
  - Compute `realized_slippage = (actual − expected) / expected` per fill
  - Aggregate by market and order type
  - Update LEAN slippage model parameters (in vectorbt and LEAN Local) using Phase 1's empirical distribution
  - **Recalibration cadence:** monthly during Phase 1; quarterly during Phase 2+
  - **Alert if** realized > 2× modeled for any single market for 3 consecutive months → strategy review
- **Tax modeling:** computed post-hoc on trade log
- **Capacity analysis:** simulate at 1×, 5×, 10×, 25× current capital; flag Sharpe degradation due to slippage; refuse migration to higher capital tier if degradation > 30%
- **30 trading-day paper minimum** before live (per strategy version) — gate enforced mechanically: deploy-to-live blocked by CI if `paper_days_for_version < 30`

### Testing Discipline

- **Unit tests required:** risk engine (every state transition, every kill-switch trigger), position sizing, order routing, audit log immutability + hash-chain integrity (including backfill provenance), version governance + parameter_set_hash composition, reconciliation logic with tolerance bands, capacity calculator, momentum-score auto-trim ranking, decision diary writer, vacation mode handler, capital-event reset logic, data quality validation
- **Integration tests required:** strategy logic against historical data, broker connectivity (mock and live-paper), full kill-switch flow including state transitions, full signal-to-fill round trip, QC adapter golden-test parity (weekly cron), vectorbt-vs-LEAN parity (weekly cron — flag P&L divergence > 0.1% or trade count mismatch as P0 bug), per-service degradation matrix scenarios
- **CI gates ALL PRs (agent-drafted included).** Failed tests block merge.
- **Pre-merge gates:** tests pass, linting clean (`ruff`), type-check clean (`mypy --strict`), no secrets in diff (`gitleaks`), no risk-engine modification without explicit `risk-review-approved` label set by operator

### Performance Targets

- Phase 1 single strategy: backtest Sharpe ≥ 1.5; live Sharpe ≥ 0.8 over 6 months; max DD ≤ 15%; signal acceptance ≥ 90%
- Phase 2 portfolio: live Sharpe ≥ 1.2
- Phase 3 portfolio: live Sharpe ≥ 1.5
- Drift alerts when live underperforms backtest by > 1 SD over 30+ days
- Auto-decommission floor: live 30-day Sharpe < 0 OR live max DD breach -25% OR 60-day live Sharpe < backtest by > 2 SD → HALT_NEW

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
| **Soft alert ceiling** | **$200/month** | Alert operator if 30-day rolling > $200 |
| **Hard alert (operator review required)** | **$300/month** | Alert + system enters review state if 30-day rolling > $300 |

System tracks actual spend monthly via integration with each provider's billing API or CSV export; surfaces in System page.

## YOUR DELIVERABLE

Produce a complete, production-grade backend technical specification covering ALL sections below. Use Mermaid for diagrams. Be specific and concrete; do NOT punt with phrases like "use industry best practices" — name the practice, the library, the configuration, the file path. Where genuine implementation choices remain, present 2–3 options with tradeoffs and a recommendation.

### 1. System Architecture Overview
- High-level system diagram (Mermaid) showing all services, data flow, external integrations
- Service inventory (each service's responsibility, lifecycle, dependencies)
- Phase 1 vs. Phase 2 architectures shown explicitly
- Migration path from Phase 1 to Phase 2 step-by-step with explicit position-flatten cutover
- External watchdog topology shown explicitly

### 2. Component Breakdown
For each component (data ingestion, storage, signal engine, risk engine, execution engine, reconciliation, monitoring, agent, scheduler+calendar combined service, audit service, QC adapter, watchdog, Gitea mirror, etc.):
- Purpose and responsibilities
- Inputs and outputs
- Dependencies
- Configuration model
- Failure modes and recovery behavior (cross-reference Per-Service Degradation Matrix)
- Implementation notes (libraries, key algorithms, gotchas)

### 3. Data Models and Schemas
Full schemas (Postgres DDL via Alembic migration scripts) for every persistent entity:
- `audit_log` (with hash-chain fields: `prev_hash`, `record_hash`, `sequence_no`, `repaired_for_sequence_no`, `repaired_for_event_timestamp`, `source_clock_ts`, `ingest_clock_ts`)
- `trades`, `orders`, `fills`
- `positions` (current and historical snapshots)
- `signals` (generated, approved, rejected, deferred; with `anomaly_flagged` boolean and reasons)
- `strategy_versions` (git hash, deployed_at, retired_at)
- `parameters` (event-sourced with `valid_from`/`valid_to`; pre-approved range constraints)
- `parameter_sets` (computed `parameter_set_hash` per active set)
- `alerts` (status: open / acknowledged / resolved; severity P0/P1/P2)
- `accounts` and `balances` over time
- `macro_events` (calendar with ratification status)
- `reconciliation_breaks` (with tolerance band reference)
- `data_quality_events`
- `decision_diary`
- `attribution` metadata
- `agent_actions` (every bounded action; prompt + response captured)
- `vacation_mode` state
- `qc_adapter_cursor` (last sequence number ingested)
- `capital_events` (deposits/withdrawals with peak-MTM-reset records)
- `cost_events` (monthly cost tracking per provider)

### 4. API Contracts
- REST endpoints (path, method, request/response schema with pydantic models, auth)
- SSE channels for real-time data flowing to web frontend
- Discord bot commands and button-payload schemas
- Internal HTTP-IPC payloads (backend → bot service)
- Webhook payloads (QC ObjectStore poll → backend ingestion; backend → email backup; external watchdog ping)
- Internal service-to-service contracts
- Idempotency key conventions (including the composite `client_order_id`)

### 5. Sequence Diagrams (Mermaid)
At minimum:
- Signal generation → risk check → human approval (web AND Discord paths) → order placement → fill → reconciliation
- Kill-switch state transitions: NORMAL → HALT_NEW → CONVALESCENT → NORMAL (including counter-reset events)
- Defensive Risk Envelope path (heartbeat engagement failure → HALT_NEW with escalated routing)
- Capital event handling (deposit → peak MTM reset → capital-event mode 30 sessions)
- Manual override / decision diary capture (web AND Discord)
- Agent hot-fix deployment with auto-rollback
- Agent-drafted PR for strategy logic change → operator-friendly review surface render → human review → merge → deploy
- End-of-day reconciliation (TWS real-time vs. FlexQuery, with tolerance-band check)
- Phase 1 → Phase 2 cutover (position flatten, audit continuation)
- Database backup and restore drill
- Data feed staleness detection and recovery
- Data-quality reject / quarantine handling
- Discord delivery failure → email backup → email-reply engagement → no halt
- QC ObjectStore audit ingestion with cursor advance and gap repair (showing append-only chain with provenance)
- Vacation mode start (incl. ratification gate suspension), end
- Macro event auto-pause straddling session boundary
- Margin auto-trim graduated de-leverage (NOT panic-flatten)
- Defensive position trim mid-session (agent-initiated)
- VPS outage → external watchdog email → operator manual recovery via TWS / IBKR phone

### 6. Error Handling Strategy
- Categorization (transient / persistent / catastrophic)
- Per-category response (retry, halt, alert, escalate)
- Per-Service Degradation Matrix realization (above)
- Idempotency requirements for order placement and audit writes
- Reconciliation procedures after recovery from outages
- Specific handling: IB Gateway daily restart, broker disconnect, data feed dropout, exchange halts, Claude API outage, QC ObjectStore unavailability, Hetzner outage, dividend ex-date tolerance widening

### 7. Observability
- Logging schema (structlog JSON, fields per category)
- Metrics inventory (Prometheus or equivalent; what's measured, frequency, retention)
- Health check endpoints (consumed by external watchdog)
- Dashboard recommendation (specific tool, what's on it)
- How Claude ops agent consumes telemetry
- Alert routing logic by severity (including escalated routing for Defensive Risk Envelope)
- Cost tracking integration (provider billing APIs / CSV ingestion)

### 8. Security
- Secrets management implementation (sops + age, file layout, age key backup procedure, rotation procedure)
- Postgres role hierarchy (app_service / app_owner / dba_breakglass) with break-glass procedure
- File permissions / service user model
- Network exposure (public vs. internal services explicitly)
- API authentication for the web frontend (JWT + cookie scheme)
- Audit log immutability mechanism (Postgres triggers + role grants + hash chain + backfill provenance)
- Backup encryption keys management
- Repo / build-chain DR (Gitea mirror, S3 archive)
- Account recovery procedure when all factors lost (break-glass + restore + identity re-establishment)

### 9. Deployment Topology
- VPS specs (Hetzner Ashburn — recommend size with justification)
- External watchdog topology
- Docker Compose layout (services, networks, volumes, including separate Discord-bot service and webhook-pusher service on shared internal network)
- Environment configuration (dev local, paper, live)
- Deployment procedure (manual + agent-driven hot-fix paths; whitelist enforcement)
- Rollback procedure
- DR runbook including IBKR phone trading desk path, TWS manual override, Gitea-based rebuild

### 10. Testing Strategy
- Unit test inventory (what's covered, what's not, why)
- Integration test inventory
- CI/CD pipeline (GitHub Actions recommended)
- Pre-merge gates with specifics
- Strategy validation pipeline (paper-minimum mechanical enforcement)
- vectorbt-vs-LEAN parity test design
- QC adapter golden-test parity design
- Per-service degradation matrix scenario tests
- Slippage calibration verification

### 11. Phased Build Plan
Aligned to operator's 6–12 month runway:
- **Phase 0 (weeks 0–7):** foundation; paper trading begins week 1; QC adapter coded + golden-tested by week 4; 30 paper days complete by end of week 6; week 7 buffer
- **Phase 1 (months 2–5):** live track record on QC; custom backend skeleton in parallel
- **Phase 2 (months 5–9):** custom infra hardening, LEAN Local deployment, ib-async integration, paper validation, migrate execution
- **Phase 3 (months 9–12):** capital scaling, second-strategy preparation, family-money legal structure
- Each phase: deliverables, success criteria (objective metrics), kill criteria (when to abandon)

### 12. Claude Ops Agent Detailed Spec
- Trigger model (cron, event-driven, on-demand from Discord)
- Tool inventory (specific bounded actions with parameters; whitelist of file paths for hot-fix; defensive trim invocation)
- Prompt-cache strategy (system prompt, codebase context, market state — what's cached, TTLs)
- Cost budget and monitoring (~$30–100/mo target; alert if exceeded; tied into Operating Cost Envelope monitoring)
- Failure mode handling (Claude API outage degrades agent to read-only, trading continues; hallucination detection via constrained outputs; rate limits)
- Audit trail of every agent decision with prompt + response captured
- Rollback mechanism for agent-deployed hot-fixes (auto-revert at 30-min metric check)
- Operator-friendly PR review surface — full rendering spec for the seven review artifacts

## FORMAT REQUIREMENTS

- Markdown with clear section headers
- Mermaid for ALL diagrams
- Concrete library/tool/version recommendations
- Where genuine implementation choices remain, present 2–3 options with tradeoffs and a recommendation
- Length will be substantial; favor completeness over brevity
- Never invent strategic decisions; if context is missing, flag with `[QUESTION FOR OPERATOR: ...]`
- This spec must interlock with the frontend spec; reference shared decisions explicitly so contracts align — name the REST endpoints, SSE channels, and Discord command schemas that the frontend will consume

Begin.
