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
- **Signal type:** time-series momentum / breakout (Donchian channels, MA crossovers); vol-targeted sizing; daily bars; signals fire at session close (preferred) or open (fallback for markets without clean close timestamp)
- **Holding period:** 2 weeks to 6 months
- **Phase 2+:** add second uncorrelated strategy (likely defined-risk vol carry on SPX) only after Phase 1 live validation; sequential strategy addition, never parallel cold-start
- **Base currency:** USD only. No FX hedging. Foreign instruments (if any FX micros are added) settle and convert at IBKR's standard rate.
- **Account model:** single live IBKR Pro account in operator's name. No sub-accounts, no prop-firm splits in Phase 1. Schema must support multi-account in future without migration (use `account_id` foreign key throughout).

### Path / Phasing

- **Phase 0 (weeks 0–6, extended from 4):** foundation — operator upskilling, IBKR Pro account opening (futures + Level 2 options approval), QC subscription, repo + CI scaffolding, secrets management (sops), Hetzner VPS provisioned, audit schema designed and migrated, **paper trading begins on QuantConnect in week 1 with the v1 strategy**, **QC ObjectStore audit adapter coded and golden-tested against custom-format target by week 4**, 30 trading days of paper completed by end of Phase 0.
- **Phase 1 (months 2–5):** live trading on QuantConnect Cloud (LEAN). Real money, small size. Track record begins immediately. $20/month QC Quant Researcher tier (upgrade to $80 only if backtest queue bottlenecks). Custom backend skeleton runs in parallel, ingesting QC audit events.
- **Phase 2 (months 5–9):** custom infrastructure built and hardened; strategy execution migrates to LEAN Local (Docker-hosted) with vectorbt as fast research/sweep layer; track record is unbroken via continuous audit log schema.
- **Phase 3 (months 9–12):** capital scaling, second-strategy preparation, family-money legal structure work.
- **Phase 1 → Phase 2 cutover for open positions:** flatten all open positions on QC at end of cutover session; reconcile audit log to terminal state; restart fresh on LEAN Local the following morning. **No position transfer across execution venues.** Audit log remains continuous; physical positions reset to flat.

### Tech Stack (locked)

- **Language:** Python 3.11+ end to end
- **Engine:** LEAN (QuantConnect Cloud Phase 1; LEAN Local self-hosted via Docker Phase 2)
- **Research/sweep:** vectorbt (or vectorbt-pro)
- **Storage:** DuckDB on Parquet for historical/research/analytics; PostgreSQL 16 (containerized) for transactional state
- **Postgres driver:** asyncpg with SQLAlchemy 2.x async
- **Migrations:** Alembic
- **Broker library:** `ib-async` (community-maintained fork of the now-unmaintained `ib_insync`; same API surface). Phase 1 routes via QC's IBKR integration; Phase 2 direct via `ib-async` to IB Gateway in Docker.
- **Orchestration:** cron + APScheduler within Python services (single-process, persistent job store backed by Postgres). NO Airflow/Prefect/Dagster.
- **Deployment:** Single VPS, Hetzner Cloud Ashburn (US East), Ubuntu LTS, Docker Compose. NO Kubernetes. **Single-host accepted; see RPO/RTO below.**
- **Process supervision:** Docker Compose restart policies + systemd for the host; chrony for NTP
- **Logging:** `structlog` with JSON renderer. OpenTelemetry tracing optional Phase 2+, not required Phase 1.
- **Validation:** pydantic v2 for all schema-bound data models
- **API exposure:** FastAPI on the VPS

### Data Sources (locked)

- **Phase 1:** QuantConnect bundled equities + futures data (sufficient for backtest + live signal). IBKR real-time market data (free to account holders for our universe).
- **Phase 2 additions:** **Polygon.io Stocks Starter ($30/mo)** for any equity-side enrichment if needed. **FRED** (free) for macro context. Economic calendar via **Forex Factory or Trading Economics** (free tier or low-cost).
- **NOT in scope:** Norgate Data (Windows-only NDU friction makes Linux Docker integration painful; QC bundled data + Polygon.io covers our needs at lower cost), alt data, NLP feeds, Bloomberg, Databento, multi-tier feeds.

### Risk Framework (concrete math; locked)

#### Position sizing
- Volatility-targeted per position
- Portfolio annualized vol target: **14%** (midpoint of prior 12–15% range, locked single value)
- Instrument vol estimate: rolling 60-day standard deviation of daily log returns
- Position size formula: `position_notional_i = (per_position_vol_target × equity) / (instrument_daily_vol_pct × sqrt(252))`
- Where `per_position_vol_target` allocates the portfolio target across active markets via inverse-vol weighting, capped by per-position and per-cluster ceilings

#### Risk rings (all units explicit)
All rings measured against **mark-to-market equity**, **gross/net in instrument notional terms** (not margin), unless stated otherwise.

| Ring | Limit | Measurement Basis |
|---|---|---|
| Per-position max | 25% of equity notional | Sum of \|notional\| for that single market |
| Gross portfolio max | **300% of equity notional** (locked single value, not range) | Sum of \|notional\| across all positions |
| Net portfolio max | 150% of equity notional | Signed sum of notional across all positions |
| Equity-index cluster max (combined /MES, /MNQ, /M2K, /MYM) | 60% of equity notional gross | |
| Commodity cluster max (combined /MCL, /MGC, /MSI) | 80% of equity notional gross | |
| Rates/bonds cluster max (combined TLT, IEF, SHY) | 80% of equity notional gross | |
| Crypto cluster max (/MBT) | 40% of equity notional gross | |
| Daily loss limit | -5% of daily-start MTM equity | Daily start = MTM at session open |
| Trailing drawdown limit | -20% from peak intraday MTM equity since system inception | Includes unrealized; does not reset at year boundary |
| Monthly DD threshold | -10% in calendar month | Triggers vol-target halving (to 7%) for remainder of month |
| Strategy decommission floor | Auto-halt + human review required | (a) live 30-day Sharpe < 0, OR (b) live max DD breach -25%, OR (c) 60-day live Sharpe underperforms backtest by > 2 SD |

#### Vol regime detector
- Metric: 60-day rolling realized volatility of portfolio daily returns
- Z-score: current value vs. its own 60-day historical distribution
- Trigger: z-score > 2 → kill-switch fires (HALT_NEW)

#### Signal storm detector
- Metric: portfolio total trade count in current session vs. rolling 30-day mean daily trade count
- Trigger: current session count > 3× the mean → kill-switch fires

#### Margin protocol (auto-trim algorithm specified)
- 70% of available margin used → warn alert
- 85% of available margin used → auto-trim sequence:
  1. Rank all open positions by momentum score (lowest first = weakest)
  2. Tie-break by largest individual margin contribution
  3. Cut positions in rank order via market orders (slippage acceptable in defensive scenario; speed prioritized)
  4. Single sweep; cut until used margin < 60%
  5. Log every cut to audit_log with `reason: margin_auto_trim`

#### Capacity tracking
- Rolling 30-day average daily volume (ADV) computed per market
- Order size as % of ADV computed at signal-emit time
- Alert at 0.5% ADV; partial-fill cap at 2% ADV
- **Capacity refusal at 2% ADV → partial fill, not refuse.** If signal demands more, fill what's possible at the cap; tag remainder as `capacity_constrained` in attribution; position then sized at actual filled fraction of intended

### Kill-Switch State Machine (explicit)

States:
- **NORMAL** — full operation; all entry and exit signals processed
- **HALT_NEW** — cancel all working orders; hold all existing positions (no liquidation); no new entries; only stop-out exits permitted; alerts to all channels; **manual human resume only**
- **CONVALESCENT** — 50% vol target (7% portfolio vol); entries permitted; remains for 5 trading sessions portfolio-wide (counted by NYSE session days); auto-transitions to NORMAL on completion

Transitions:
- `NORMAL → HALT_NEW`: any kill-switch trigger fires (list below)
- `HALT_NEW → CONVALESCENT`: human invokes resume via web app or Discord (with re-auth)
- `CONVALESCENT → NORMAL`: 5 trading sessions complete without breach
- `CONVALESCENT → HALT_NEW`: any kill-switch trigger fires; 5-session counter resets on next resume
- **No HALT_ALL or auto-liquidate state exists.** Flatten-into-panic is the failure mode kill switches must not enable.

Kill-switch triggers (any of the following → `→ HALT_NEW`):
- Trailing DD breach (-20% from peak MTM equity since inception)
- Daily loss breach (-5% of daily-start MTM)
- Signal storm (above)
- Reconciliation mismatch (any nonzero delta on positions or cash vs. broker source-of-truth)
- Broker disconnect persisting > 5 minutes during market hours
- Vol regime detector trip (above)
- Decommission floor trigger (above)
- Any unhandled exception in execution path
- Heartbeat engagement failure (see Communications)

### Defensive Risk Envelope (defined)

When invoked: enter `HALT_NEW` state + escalate alerts on all available channels. Used during heartbeat engagement failure or comms breakdown — i.e., when the system cannot confirm operator awareness of state. NOT the same as a kill-switch trigger; this is a precautionary halt due to comms uncertainty.

### Auto-Revert Thresholds (parameter changes)

A parameter change made by the agent (within pre-approved code-defined ranges) auto-reverts when **any** of the following:
- 30-day rolling live Sharpe drops > 1 SD from pre-change 30-day baseline within 30 sessions of the change
- Max DD breaches -10% within 5 sessions of the change
- 5+ consecutive losing trades attributable to a market affected by the change

Auto-revert action: parameter restored to pre-change value; full audit entry; alert to operator; no further auto-changes to that parameter for 14 days.

### Logic-Change vs. Parameter-Change Boundary (clarified)

- **Logic change** (requires PR + human merge): changes to *which signals fire* — rule logic, indicator selection, market universe, strategy structure, sizing model.
- **Parameter change** (auto with audit, within pre-approved range): changes to *parameters governing existing signals* — lookback period, vol target multiplier, position cap multiplier, etc., **only within ranges defined in code** (e.g., `LOOKBACK_RANGE = (40, 80)`).
- **Pre-approved range itself is logic.** Changing a range requires a PR.
- **Parameter changes take effect at next signal cycle (next session), never mid-session.** Hard-coded constraint.

### Execution Mechanics

- **Order types:**
  - Entries: limit-marketable (last ± 0.5× spread); on retry, widen to 1× spread, then 1.5× spread
  - Exits (stop): stop-market for execution certainty
  - Profit-target exits: limit at target
  - Futures rolls: calendar spread orders when broker supports; otherwise leg with 60s stagger
  - Kill-switch action: cancel all working orders; **hold positions** (no liquidation, no flatten)
- **Retry logic:** order rejection → 3 retries with exponential backoff (1s, 4s, 16s); after 3 failures, halt that market only, alert
- **Reconciliation source-of-truth (locked):**
  - Intraday risk decisions: TWS API real-time portfolio snapshot
  - End-of-day reconciliation and tax: IBKR FlexQuery (XML)
  - Both reconciled at session close; FlexQuery is authoritative for tax + audit reporting
  - Mismatch (any nonzero delta) → kill-switch trigger
- **Reconciliation cadence:** every session open + close + EOD full cross-check (positions, cash, P&L, fees, dividends, interest); weekly summary report
- **Roll discipline:** futures rolled 5–7 trading days before expiry, off-peak liquidity scheduling
- **Macro event handling:**
  - Auto-pause order placement from 5 min before through 30 min after scheduled tier-1 events (FOMC, CPI, NFP, GDP, PCE, ECB/BOJ/BOE if exposed, OPEC if /MCL exposed)
  - **NO manual event mode override.** Rules-based only.
  - Calendar auto-imported nightly
  - User ratifies tomorrow's events via Discord by **23:00 ET nightly**
  - **Default if no ratification by 23:00 ET:** hard halt new orders for next session until ratified (forces engagement)
  - **Macro window vs. session boundary collision:** pause wins. New signals deferred to next session if pause window straddles close.

### Audit & Track Record

- **Immutability mechanism (explicit):**
  - `audit_log` table protected by Postgres triggers blocking `UPDATE`, `DELETE`, `TRUNCATE` operations
  - Service role granted only `INSERT, SELECT` on `audit_log`; `REVOKE UPDATE, DELETE, TRUNCATE`
  - **Hash chain:** SHA-256 single-linked list. Each record's `prev_hash` field references prior record's full hash; `record_hash` is SHA-256 over (prev_hash || record_payload). Genesis record has prev_hash = 32 zero bytes. Tamper-detection only; tamper-prevention via DB constraints.
  - Backups: written to S3 with **Object Lock (Compliance mode)**; retention 7 daily / 4 weekly / 12 monthly / permanent annual; quarterly restore drill mandatory. Backups themselves immutable for the lock period.
- **Strategy version governance:** every git commit produces unique strategy version hash; every trade tagged with version
- **Track record portability:** identical audit schema between QC Phase 1 and custom Phase 2; QC adapter must emit audit records in the custom-target schema with byte-for-byte identical structure (golden-test parity required, see Testing)
- **Environment tagging:** every trade marked `paper` / `live-small` / `live-scale`; never blended in any reporting
- **Paper minimum:** 30 trading days paper before any live deployment of a new strategy version (per strategy version)
- **Trade-level attribution:** computed at signal-emit time by strategy code (vol regime, trend regime, expected P&L, expected slippage); realized values fill in post-trade. Captured fields are immutable post-emit.

### QuantConnect Audit Adapter (Phase 1 critical path)

The Phase 1 audit log lives in our backend, not QC. The adapter must be loss-tolerant.

- **Mechanism:** QC algorithm writes audit events to **QC ObjectStore** (durable, project-scoped storage) as JSONL with monotonic sequence numbers per session. NOT QC's `Notify.Web` (rate-limited, no retry, lossy).
- **Backend ingestion:** custom service polls QC ObjectStore via QC API every 60s during market hours; reads incrementally with cursor (last sequence number persisted to Postgres); resumes from last cursor on backend restart
- **Schema:** identical to custom-emitted audit records. Golden-test parity verified weekly: same input event produces byte-for-byte identical record from QC adapter and from native custom emitter.
- **Loss handling:** if cursor gap detected (sequence number jump), alert + auto-pull from QC's own logs to fill gap; flag affected trades as `audit_repaired` with reconciliation reference
- **Failure mode:** if QC ObjectStore unavailable for > 10 min, defensive risk envelope triggers (HALT_NEW) — audit integrity is non-negotiable

### Tax Handling (corrected)

- **Futures (Section 1256 contracts):** treatment is automatic 60% LTCG / 40% STCG with mandatory year-end mark-to-market. **No election required.** System reports Form 6781 data.
- **ETFs (securities):** standard capital gains/losses with wash sale tracking. **No 475(f) trader-status election by default** (operator's ETF activity is too marginal to confidently qualify; IRS-audit risk if challenged outweighs the wash-sale relief). System supports both modes (elected and non-elected) for future flexibility but defaults to non-elected.
- **CPA consultation REQUIRED** before any tax election; system documentation and onboarding flow must explicitly surface this. No election toggle in the UI without an "I have consulted a CPA" acknowledgment.
- **Wash sale tracking** across all accounts (operator's, future family accounts) via `account_id` linkage in trade records
- **Year-end harvest flagging:** system surfaces unrealized losses with low-strategy-impact harvest opportunities
- **Tax export:** CSVs structured for Form 6781 (1256 contracts), Schedule D (capital gains/losses summary), Form 8949 (per-lot detail); plus PDF summary; importable by Drake / ProSeries / TurboTax. Annual export triggered Jan 31 each year.

### Claude Ops Agent — Authority Matrix and Boundaries

A separate long-running Python service alongside the trading engine.

| Category | Agent Authority | Implementation Note |
|---|---|---|
| Tighten risk (cut sizes, lower caps, halt trading) | AUTO with notification | All risk-tightening writes go through risk engine API; never direct DB writes |
| Loosen risk (raise sizes, increase caps, restart after halt) | HUMAN APPROVAL REQUIRED | Hard-coded as denied capability; only operator-authenticated requests can loosen |
| Hot-fix infrastructure (logging, retry, monitoring, dependency, broker reconnect) | AUTO-DEPLOY with notification + automatic rollback if metrics degrade within 30 min | Whitelist of allowed file paths; any file outside whitelist requires PR |
| Strategy logic changes (signal rules, indicator params, universe, sizing model) | DRAFTS PR; human reviews and merges | See Operator-Friendly PR Review Surface below |
| Place / modify / cancel orders | NEVER, hard-coded block | Agent service has no broker API credentials; physically cannot place orders |
| Invoke kill switch | AUTO on hard threshold breach | |
| Un-invoke kill switch | HUMAN APPROVAL ONLY (with re-auth) | |
| Modify strategy parameters within pre-approved code-defined range | AUTO with full audit log + auto-revert per thresholds above | Effective only at next signal cycle, never mid-session |
| Generate reports, alerts, briefings, run diagnostics | AUTO | |

The agent reads logs/metrics, calls Claude API on triggers, takes bounded actions, generates morning briefings and weekly reports, drafts PRs for review. **Never any direct trading authority.** Agent service has zero broker credentials.

### Operator-Friendly PR Review Surface (critical — operator is non-coding)

Every PR (agent-drafted or human-drafted) that touches strategy logic or risk parameters must surface the following review artifacts to the operator. The diff is reference; the actionable artifact is the first three items.

1. **Plain-English summary** (max 200 words) — written by agent: what changed, why, what behavior changes. Required.
2. **Risk impact summary** (auto-generated) — which risk metrics are affected, by how much, in plain numbers (e.g., "expected daily P&L variance increases from $180 to $220 at current capital").
3. **Backtest delta** — current strategy version vs. proposed version: equity curve overlay, key statistics delta table, ten worst-divergence trades highlighted.
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
- **Heartbeat — split semantics:**
  - **Delivery:** HTTP 2xx ack from Discord webhook on push. Failure → automatic email backup; alert operator out-of-band.
  - **Engagement:** user reply or reaction on critical alerts. No engagement for > 24h on a critical alert → defensive risk envelope (HALT_NEW + alerts).
- **Vacation mode:** operator runs `/vacation start [days]` in Discord
  - Engagement timeout extends to 7 days
  - New strategy entries auto-disabled (only stop-out exits permitted)
  - Daily summary still posts
  - On `/vacation end` or expiry, normal operation resumes
- **NO SMS, NO voice escalation.** Operator treats Discord like text/calls.
- **Backend → Discord:** webhook events from FastAPI; bot also queries backend REST API for slash command responses. Discord webhook events ≠ Discord bot service; implement as separate concerns sharing the bot's Discord credentials.

### Security

- **Secrets management:** Mozilla **sops** with **age** encryption. Encrypted secret files committed to repo; decrypted at deploy time on the VPS. Avoids running a Vault server.
- **Secret rotation cadence:** quarterly forced; immediate on compromise indication. Triggered rotations: every quarter, on any departed maintainer (not applicable in solo Phase 1), on any suspected compromise, on any Hetzner-or-S3 credential exposure.
- **Database backups:** daily encrypted to S3 with Object Lock (Compliance mode); retention 7 daily / 4 weekly / 12 monthly / permanent annual; quarterly restore drill mandatory
- **Encryption at rest:** Hetzner volume encryption for live DB; application-level encryption (using `cryptography` library + key from env) for any high-sensitivity columns (e.g., stored API tokens if applicable)
- **Auth (web frontend):** WebAuthn (passkey) primary + TOTP backup
- **Auth tokens:** JWT access (15-min lifetime) + refresh token (7-day lifetime). HttpOnly + Secure + SameSite=Strict cookies. Server-side session records in Postgres for revocation. Sensitive actions (kill-switch resume, parameter range change PR submission, deploy approval, env tag change) require re-auth within last 5 minutes.
- **Container hardening:** non-root users, read-only filesystem where compatible, no privileged containers, **Trivy** image scanning in CI, distroless base images where compatible
- **Network egress allowlist on host:** only IBKR endpoints (TWS Gateway destinations), Anthropic API, AWS S3 (or Backblaze B2) for backups, NTP pool, Ubuntu/Debian package mirrors, GitHub. Everything else denied by default via UFW or iptables rules.
- **Network ingress:** only FastAPI public endpoint (HTTPS via Caddy or Traefik with Let's Encrypt) and SSH (key-only, port-knocking optional). Postgres, Redis, agent, engine all on internal Docker network only.

### Time and Clock

- **NTP:** chrony daemon, primary source `pool.ntp.org`, fallback `time.cloudflare.com`
- **Clock skew tolerance:** log warn at > 100ms; defensive halt at > 1s
- **Audit ordering:** records carry both UTC wall-clock timestamp (`timestamp_utc`) AND `monotonic_ns` (Python `time.monotonic_ns()`) within process; monotonic used for relative ordering within a process; UTC used for cross-service ordering with skew tolerance noted
- **All schema timestamps:** `TIMESTAMPTZ` in Postgres, stored as UTC, rendered with operator's local time (America/New_York) at presentation layer

### Idempotency

- **All writes:** UUIDv7 primary keys (time-ordered, sortable, unique)
- **Order placement:** `client_order_id = "{strategy_version_short_hash}-{signal_uuid}-{retry_n}"` — IBKR-side dedup
- **Audit writes:** UUIDv7
- **Webhook re-delivery:** receiver dedupes by `event_uuid` for 7-day window via Postgres unique constraint

### SLO Budgets

- **Signal-to-order latency:** p50 ≤ 60s, p99 ≤ 5min (daily-bar trend-following; not HFT)
- **Kill-switch invocation latency:** ≤ 5s from trigger to broker order cancellation request
- **Reconciliation freshness during market hours:** ≤ 60s from broker state to internal mirror
- **Discord webhook delivery:** ≤ 10s p99
- **Backtest queue:** acceptable p99 ≤ 30 min on QC tier; if exceeded persistently, upgrade tier

### RPO / RTO

- **RPO:** 15 minutes (Postgres WAL ship to S3 every 15 min via `wal-g` or equivalent)
- **RTO during market hours:** 4 hours
- **RTO outside market hours:** 24 hours
- **Single VPS accepted; no warm standby.** Cost-benefit doesn't justify standby at our scale (<$1M AUM); revisit at $250k+ AUM with family money.
- **DR runbook (mandatory deliverable):** in case of Hetzner-Ashburn outage:
  1. Operator opens TWS desktop on personal laptop
  2. Manually closes risky positions per documented playbook
  3. Calls IBKR phone trading desk if web/desktop also unavailable
  4. Restores VPS from latest backup on recovery (Hetzner same region or alt region)
  5. Reconciles audit log against IBKR FlexQuery for outage period; flag affected trades as `outage_period`

### Backtesting Validation

- **Walk-forward analysis:** rolling 3-year train, 6-month out-of-sample, advance, repeat
- **70/30 in-sample / held-out test split.** Held-out touched ONCE at end of strategy development; documented in audit
- **Survivorship-bias-free data** via QC bundled feeds (which include delisted instruments and corporate-action-adjusted history)
- **Realistic fills:** LEAN's volume-aware slippage models; calibrated against actual IBKR fills observed in Phase 1 (calibration mandatory before Phase 2 backtests are believed)
- **Tax modeling:** computed post-hoc on trade log
- **Capacity analysis:** simulate at 1×, 5×, 10×, 25× current capital; flag Sharpe degradation due to slippage; refuse migration to higher capital tier if degradation > 30%
- **30 trading-day paper minimum** before live (per strategy version) — gate enforced mechanically: deploy-to-live blocked by CI if `paper_days_for_version < 30`

### Testing Discipline

- **Unit tests required:** risk engine (every state transition), position sizing, order routing, audit log immutability + hash chain integrity, version governance, reconciliation logic, capacity calculator, margin auto-trim, decision diary writer, vacation mode handler
- **Integration tests required:** strategy logic against historical data, broker connectivity (mock and live-paper), full kill-switch flow including state transitions, full signal-to-fill round trip, QC adapter golden-test parity (weekly cron), vectorbt-vs-LEAN parity (weekly cron — flag P&L divergence > 0.1% or trade count mismatch as P0 bug)
- **CI gates ALL PRs (agent-drafted included).** Failed tests block merge. No exceptions.
- **Pre-merge gates:** tests pass, linting clean (`ruff`), type-check clean (`mypy --strict`), no secrets in diff (`gitleaks`), no risk-engine modification without explicit `risk-review-approved` label set by operator

### Performance Targets

- Phase 1 single strategy: backtest Sharpe ≥ 1.5; live Sharpe ≥ 0.8 over 6 months; max DD ≤ 15%; signal acceptance ≥ 90%
- Phase 2 portfolio: live Sharpe ≥ 1.2
- Phase 3 portfolio: live Sharpe ≥ 1.5
- Drift alerts when live underperforms backtest by > 1 SD over 30+ days
- Auto-decommission floor (above): live 30-day Sharpe < 0 OR live max DD breach -25% OR 60-day live Sharpe < backtest by > 2 SD → HALT_NEW + human review required

## YOUR DELIVERABLE

Produce a complete, production-grade backend technical specification covering ALL sections below. Use Mermaid for diagrams. Be specific and concrete; do NOT punt with phrases like "use industry best practices" — name the practice, the library, the configuration, the file path. Where genuine implementation choices remain, present 2–3 options with tradeoffs and a recommendation.

### 1. System Architecture Overview
- High-level system diagram (Mermaid) showing all services, data flow, external integrations
- Service inventory (each service's responsibility, lifecycle, dependencies)
- Phase 1 vs. Phase 2 architectures shown explicitly (what's in QC, what's added in custom; what changes at cutover)
- Migration path from Phase 1 to Phase 2 step-by-step with explicit position-flatten cutover

### 2. Component Breakdown
For each component (data ingestion, storage, signal engine, risk engine, execution engine, reconciliation, monitoring, agent, scheduler, calendar service, audit service, QC adapter, etc.):
- Purpose and responsibilities
- Inputs and outputs
- Dependencies
- Configuration model
- Failure modes and recovery behavior
- Implementation notes (libraries, key algorithms, gotchas)
- Specify whether scheduler and calendar service are co-located in one process or separate (recommend with justification)

### 3. Data Models and Schemas
Full schemas (Postgres DDL via Alembic migration scripts) for every persistent entity:
- `audit_log` (with hash-chain fields: `prev_hash`, `record_hash`, `sequence_no`)
- `trades`, `orders`, `fills`
- `positions` (current and historical snapshots)
- `signals` (generated, approved, rejected, deferred)
- `strategy_versions`
- `parameters` (with version history; pre-approved range constraints)
- `alerts` (status: open / acknowledged / resolved)
- `accounts` and `balances` over time
- `macro_events` (calendar with ratification status)
- `reconciliation_breaks`
- `decision_diary`
- `attribution` metadata
- `agent_actions` (every bounded action; prompt + response captured)
- `vacation_mode` state
- `qc_adapter_cursor` (last sequence number ingested)

### 4. API Contracts
- REST endpoints (path, method, request/response schema with pydantic models, auth)
- SSE channels for real-time data flowing to web frontend
- Discord bot commands and button-payload schemas
- Webhook payloads (QC ObjectStore poll → backend ingestion; backend → Discord; backend → email backup)
- Internal service-to-service contracts
- Idempotency key conventions

### 5. Sequence Diagrams (Mermaid)
At minimum:
- Signal generation → risk check → human approval (web AND Discord paths) → order placement → fill → reconciliation
- Kill switch state transitions: NORMAL → HALT_NEW → CONVALESCENT → NORMAL
- Manual override / decision diary capture (web AND Discord)
- Agent hot-fix deployment with auto-rollback on metric degradation
- Agent-drafted PR for strategy logic change → operator-friendly review surface render → human review → merge → deploy
- End-of-day reconciliation (TWS real-time vs. FlexQuery)
- Phase 1 → Phase 2 cutover (position flatten, audit continuation)
- Database backup and restore drill
- Data feed staleness detection and recovery
- Discord delivery failure → email backup → defensive risk envelope on heartbeat engagement breach
- QC ObjectStore audit ingestion with cursor advance and gap repair
- Vacation mode start, daily-summary-only operation, end
- Macro event auto-pause straddling session boundary

### 6. Error Handling Strategy
- Categorization (transient / persistent / catastrophic)
- Per-category response (retry, halt, alert, escalate)
- Idempotency requirements for order placement and audit writes
- Reconciliation procedures after recovery from outages
- Specific handling: IB Gateway daily restart, broker disconnect, data feed dropout, exchange halts, Claude API outage, QC ObjectStore unavailability, Hetzner outage

### 7. Observability
- Logging schema (structlog JSON, fields per category)
- Metrics inventory (Prometheus or equivalent; what's measured, frequency, retention)
- Health check endpoints
- Dashboard recommendation (specific tool, what's on it)
- How Claude ops agent consumes telemetry (read-only access, log aggregation pattern)
- Alert routing logic by severity

### 8. Security
- Secrets management implementation (sops + age, file layout, rotation procedure)
- File permissions / service user model
- Network exposure (public vs. internal services explicitly)
- API authentication for the web frontend (JWT + cookie scheme)
- Audit log immutability mechanism (Postgres triggers + role grants + hash chain)
- Backup encryption keys management

### 9. Deployment Topology
- VPS specs (Hetzner Ashburn — recommend size with justification given workload)
- Docker Compose layout (services, networks, volumes)
- Environment configuration (dev local, paper, live)
- Deployment procedure (manual + agent-driven hot-fix paths; whitelist enforcement)
- Rollback procedure
- DR runbook including IBKR phone trading desk path and TWS manual override

### 10. Testing Strategy
- Unit test inventory (what's covered, what's not, why)
- Integration test inventory
- CI/CD pipeline (GitHub Actions recommended)
- Pre-merge gates with specifics
- Strategy validation pipeline (paper-minimum mechanical enforcement)
- vectorbt-vs-LEAN parity test design
- QC adapter golden-test parity design

### 11. Phased Build Plan
Aligned to operator's 6–12 month runway:
- **Phase 0 (weeks 0–6):** foundation; paper trading begins week 1; QC adapter coded + golden-tested by week 4; 30 paper days complete by end of phase
- **Phase 1 (months 2–5):** live track record on QC; custom backend skeleton in parallel
- **Phase 2 (months 5–9):** custom infra hardening, LEAN Local deployment, ib-async integration, paper validation, migrate execution
- **Phase 3 (months 9–12):** capital scaling, second-strategy preparation, family-money legal structure
- Each phase: deliverables, success criteria (objective metrics), kill criteria (when to abandon)

### 12. Claude Ops Agent Detailed Spec
- Trigger model (cron, event-driven, on-demand from Discord)
- Tool inventory (specific bounded actions with parameters; whitelist of file paths for hot-fix)
- Prompt-cache strategy (system prompt, codebase context, market state — what's cached, TTLs)
- Cost budget and monitoring (~$30–100/mo target; alert if exceeded)
- Failure mode handling (Claude API outage, hallucination detection via constrained outputs, rate limits)
- Audit trail of every agent decision with prompt + response captured
- Rollback mechanism for agent-deployed hot-fixes (auto-revert at 30-min metric check)
- Operator-friendly PR review surface — full rendering spec for the six review artifacts (plain-English summary, risk impact, backtest delta, test results, files affected, diff)

## FORMAT REQUIREMENTS

- Markdown with clear section headers
- Mermaid for ALL diagrams
- Concrete library/tool/version recommendations (e.g., "PostgreSQL 16, asyncpg 0.29.x" not "a database")
- Where genuine implementation choices remain, present 2–3 options with tradeoffs and a recommendation
- Length will be substantial; favor completeness over brevity
- Never invent strategic decisions; if context is missing, flag with `[QUESTION FOR OPERATOR: ...]`
- This spec must interlock with the frontend spec (separate session); reference shared decisions explicitly so contracts align — name the REST endpoints, SSE channels, and Discord command schemas that the frontend will consume

Begin.
