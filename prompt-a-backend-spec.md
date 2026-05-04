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

## LOCKED STRATEGIC DECISIONS — DO NOT REOPEN

### Strategy
- **Phase 1 strategy:** multi-asset systematic trend-following on micro futures + bond ETFs
- **Universe:** ~8–12 markets — equity index micros (/MES, /MNQ, /M2K, /MYM), commodity micros (/MCL, /MGC, /MSI), Bitcoin micro (/MBT), bond ETFs (TLT, IEF, SHY); optional FX micros (/M6E)
- **Signal type:** time-series momentum / breakout (Donchian channels, MA crossovers), vol-targeted sizing, daily bars, signals fire at session close/open
- **Holding period:** 2 weeks to 6 months
- **Phase 2+:** add second uncorrelated strategy (likely defined-risk vol carry on SPX) only after Phase 1 live validation; sequential strategy addition, never parallel cold-start

### Path / Phasing
- **Phase 1 (months 1–4):** strategy live on QuantConnect Cloud (LEAN). Real money, small size. Track record begins immediately. $20/month QC Quant Researcher tier (upgrade to $80 only if backtest queue bottlenecks)
- **Phase 2 (months 4–8):** custom infrastructure built in parallel; strategy execution migrates to LEAN Local (Docker-hosted) with vectorbt as fast research/sweep layer; track record is unbroken — same audit schema across phases
- **Phase 3 (year 2+):** add second strategy; multi-strategy portfolio; scale to family capital
- **Critical:** Phase 1 QC algorithm must emit audit events + fills to our own backend webhook from day 1. The custom backend's audit log exists from day 1 even though execution is on QC. Migration to Phase 2 is therefore a swap of execution venue, not a track-record reset.

### Tech Stack
- **Language:** Python 3.11+ end to end
- **Engine:** LEAN (QuantConnect Cloud Phase 1; LEAN Local self-hosted Phase 2)
- **Research/sweep:** vectorbt (or vectorbt-pro)
- **Storage:** DuckDB on Parquet for historical/research/analytics; PostgreSQL (containerized) for transactional state
- **Broker:** Interactive Brokers Pro (futures + Level 2 options approved). Phase 1 routes via QC's IBKR integration; Phase 2 direct via `ib_insync` to IB Gateway in Docker
- **Orchestration:** cron + APScheduler within Python services. NO Airflow/Prefect/Dagster
- **Deployment:** Single VPS, Hetzner Cloud Ashburn (US East), Ubuntu LTS, Docker Compose. NO Kubernetes
- **Process supervision:** Docker Compose restart policies + systemd for the host
- **Logging:** structured JSON, persisted to disk, ingestible by Claude ops agent
- **API exposure:** FastAPI on the VPS

### Data Sources
- **Phase 1:** QuantConnect bundled equities + futures data; IBKR real-time market data (free to account holders for our universe)
- **Phase 2 additions:** Norgate Data (~$50/mo, survivorship-bias-free, point-in-time corporate actions); FRED (free macro); economic calendar (Trading Economics or Forex Factory) for macro event detection
- **NOT in scope:** alt data, NLP feeds, Bloomberg, Databento, multi-tier feeds

### Risk Framework (binding)
- **Position sizing:** volatility-targeted per position; portfolio annualized vol target 12–15%; rolling 60-day stdev for instrument vol
- **Risk rings:** per-position max 25% of equity gross; gross portfolio max 250–300% of equity; net portfolio max 150% of equity; daily loss limit -5%; trailing DD limit -20%; monthly DD threshold -10% triggers vol-target halving for remainder of month
- **Kill switches (auto-halt):** trailing DD breach, daily loss breach, signal storm (3× rolling 30-day trade count), reconciliation mismatch, broker disconnect >5min during market hours, vol regime detector >2σ above 60-day baseline, any unhandled exception in execution path
- **Recovery:** human-only resume; convalescent mode (50% vol target × 5 sessions) post-resume
- **Model decay monitoring:** rolling 60-day live vs. backtest Sharpe; per-market profit factor; signal hit rate. Alerts only, never auto-action
- **Margin protocol:** 70% used = warn alert; 85% = auto-trim to 60% (weakest signals first by momentum score)
- **Capacity tracking:** order size as % of ADV per market; alert at 0.5% ADV; hard refuse at 2% ADV

### Execution Mechanics
- **Order types:** limit-marketable for entries (last ± 0.5× spread; widen on retry); stop-market for exits; calendar spread for futures rolls when broker supports; cancel-all-working-hold-positions on kill switch
- **Retry logic:** rejection → 3 retries with exponential backoff (1s, 4s, 16s); after 3 failures halt that market, alert
- **Reconciliation:** every session open + close + EOD full cross-check (positions, cash, P&L, fees, dividends, interest); mismatch → halt new orders, alert; weekly summary report
- **Roll discipline:** futures rolled 5–7 trading days before expiry, off-peak liquidity scheduling
- **Macro event handling:** auto-pause order placement from 5 min before through 30 min after scheduled tier-1 events (FOMC, CPI, NFP, GDP, PCE, ECB/BOJ/BOE if exposed, OPEC if /MCL exposed); NO manual event mode override; calendar auto-imported daily; user ratifies tomorrow's events nightly via Discord

### Audit & Track Record (non-negotiable for sellability)
- **Immutable, append-only audit log** capturing every signal, order, fill, parameter change, kill-switch invocation, human override, agent action — with UTC timestamp, full system state at decision time, and reason
- **Strategy version governance:** every git commit produces unique strategy version hash; every trade tagged with version; "show all trades from version X" instantly answerable
- **Track record portability:** identical audit schema between QC Phase 1 and custom Phase 2; Phase 1 trades port cleanly into Phase 2 system
- **Environment tagging:** every trade marked `paper` / `live-small` / `live-scale`; never blended in reporting
- **Paper minimum:** 30 trading days paper before any live deployment of a new strategy version
- **Trade-level attribution:** each trade tagged with strategy version, signal type, market, vol regime, trend regime, expected P&L, expected slippage; realized values fill in post-trade

### Tax Handling
- **1256 mark-to-market election** for futures (60/40 LTCG/STCG) — operator files with first tax return; system reports accordingly
- **Wash sale tracking** across all accounts (operator's, future family accounts) for ETF-side trades
- **Year-end harvest flagging:** system surfaces unrealized losses with low strategy impact
- **CPA-readable export:** annual export of all relevant tax data in standard format

### Claude Ops Agent (critical architecture component)
A separate long-running Python service alongside the trading engine. Authority matrix:

| Category | Agent Authority |
|---|---|
| Tighten risk (cut sizes, lower caps, halt trading) | AUTO with notification |
| Loosen risk (raise sizes, increase caps, restart after halt) | HUMAN APPROVAL REQUIRED |
| Hot-fix infrastructure (logging, retry, monitoring, dependency, broker reconnect) | AUTO-DEPLOY with notification + automatic rollback if metrics degrade |
| Strategy logic changes (signal rules, indicator params, universe, sizing model) | DRAFTS PR; human reviews and merges |
| Place/modify/cancel orders | NEVER, hard-coded block |
| Invoke kill switch | AUTO on hard threshold breach |
| Un-invoke kill switch | HUMAN APPROVAL ONLY |
| Modify strategy parameters within pre-approved range | AUTO with full audit log + auto-revert if metrics degrade |
| Generate reports, alerts, briefings, run diagnostics | AUTO |

The agent reads logs/metrics, calls Claude API on triggers, takes bounded actions, generates morning briefings and weekly reports, drafts PRs for review. Never any direct trading authority.

### Communications
- **Primary user surface (mobile):** Discord bot via `discord.py`. Channels: `#daily-brief`, `#signals`, `#fills`, `#alerts`, `#critical`, `#ops`, `#ask-agent`, `#audit`. Slash commands and button interactions.
- **Backup channel:** email (silent fallback if Discord delivery fails — agent detects no read receipt + no reply for >X minutes on critical alerts)
- **Heartbeat:** if no successful message delivery in 24 hours, system enters defensive risk envelope automatically
- **NO SMS, NO voice escalation.** Operator treats Discord like text/calls.
- **Backend → Discord** via webhook events from FastAPI; bot also queries backend REST API for slash command responses

### Security
- All secrets (broker API keys, Claude API key, DB credentials, Discord bot token, webhook URLs) in environment variables; file permissions locked to service user; never committed to repo
- Documented rotation procedure
- Database backups: daily encrypted off-site (S3 or Backblaze B2); retention 7 daily / 4 weekly / 12 monthly / permanent annual; quarterly restore drill
- WebAuthn for any web-app-facing auth; TOTP backup
- Audit log immutability via append-only constraint + cryptographic chaining of records (hash links) to detect tampering
- Network: only the FastAPI public endpoint and Discord bot outbound are internet-facing; database, agent, engine internal-only on Docker network

### Testing Discipline
- **Unit tests required:** risk engine, position sizing, order routing, audit log, version governance, reconciliation logic
- **Integration tests required:** strategy logic against historical data, broker connectivity (mock and live-paper), kill switch flow, full signal-to-fill round trip
- **CI gates ALL PRs (agent-drafted included).** Failed tests block merge. No exceptions.
- **Pre-merge gates:** tests pass, linting clean, type-check clean, no secrets in diff, no risk-engine modification without explicit human approval label

### Backtesting Validation
- **Walk-forward analysis:** rolling 3-year train, 6-month out-of-sample, advance, repeat
- **70/30 in-sample / held-out test split.** Held-out touched ONCE at end of strategy development; documented in audit
- **Survivorship-bias-free data** via Norgate / LEAN
- **Realistic fills:** LEAN's volume-aware slippage models, calibrated against actual IBKR fills observed in Phase 1
- **Tax modeling:** computed post-hoc on trade log
- **Capacity analysis:** simulate at 1×, 5×, 10×, 25× current capital; flag Sharpe degradation due to slippage
- **30 trading-day paper minimum** before live (per strategy version)

### Performance Targets (used in monitoring + alerting thresholds)
- Phase 1 (single strategy): backtest Sharpe ≥1.5; live Sharpe ≥0.8 over 6 months; max DD ≤15%; signal acceptance ≥90%
- Phase 2 (two-strategy portfolio): live portfolio Sharpe ≥1.2
- Phase 3 (3+ strategies): live portfolio Sharpe ≥1.5
- Drift alerts when live underperforms backtest by >1 SD over 30+ days

## YOUR DELIVERABLE

Produce a complete, production-grade backend technical specification covering ALL sections below. Use Mermaid for diagrams. Be specific and concrete; do NOT punt with phrases like "use industry best practices" — name the practice, the library, the configuration. Where genuine implementation choices remain, present 2–3 options with tradeoffs and a recommendation.

### 1. System Architecture Overview
- High-level system diagram (Mermaid) showing all services, data flow, external integrations
- Service inventory (each service's responsibility, lifecycle, dependencies)
- Phase 1 vs. Phase 2 architectures shown explicitly (what's in QC, what's added in custom)
- Migration path from Phase 1 to Phase 2 step-by-step

### 2. Component Breakdown
For each component (data ingestion, storage, signal engine, risk engine, execution engine, reconciliation, monitoring, agent, scheduler, calendar service, audit service, etc.):
- Purpose and responsibilities
- Inputs and outputs
- Dependencies
- Configuration model
- Failure modes and recovery behavior
- Implementation notes (libraries, key algorithms, gotchas)

### 3. Data Models and Schemas
Full schemas (Postgres DDL or equivalent) for every persistent entity:
- audit_log (with hash-chain integrity)
- trades, orders, fills
- positions (current and historical snapshots)
- signals (generated, approved, rejected, deferred)
- strategy_versions
- parameters (with version history)
- alerts (with status: open / acknowledged / resolved)
- accounts and balances over time
- macro_events (calendar)
- reconciliation_breaks
- decision_diary entries
- attribution metadata
- agent_actions (every bounded action the agent took, with prompt + response captured)

### 4. API Contracts
- REST endpoints (path, method, request/response schema, auth)
- SSE channels for real-time data flowing to web frontend
- Discord bot commands and button-payload schemas (these are also frontend-side; keep contracts in sync)
- Webhook payloads (Phase 1 QC → backend audit emission; backend → Discord; backend → email backup)
- Internal service-to-service contracts

### 5. Sequence Diagrams (Mermaid)
At minimum:
- Signal generation → risk check → human approval (web AND discord paths) → order placement → fill → reconciliation
- Kill switch invocation (auto-trigger and manual paths)
- Manual override / decision diary capture
- Agent hot-fix deployment with auto-rollback on metric degradation
- Agent-drafted PR for strategy logic change → human review → merge → deploy
- End-of-day reconciliation
- Migration of strategy from Phase 1 (QC) to Phase 2 (custom)
- Database backup and restore drill
- Data feed staleness detection and recovery
- Discord delivery failure → email backup → defensive risk envelope on heartbeat breach

### 6. Error Handling Strategy
- Categorization of errors (transient / persistent / catastrophic)
- Per-category response (retry, halt, alert, escalate)
- Idempotency requirements for order placement and audit writes
- Reconciliation procedures after recovery from outages
- Specific handling: IB Gateway daily restart, broker disconnect, data feed dropout, exchange halts, Claude API outage

### 7. Observability
- Logging schema (structured JSON, fields per category)
- Metrics inventory (what's measured, frequency, retention) — Prometheus or equivalent
- Health check endpoints
- Dashboard recommendation (what tool, what's on it)
- How Claude ops agent consumes telemetry
- Alert routing logic (which alerts go to which channel by severity)

### 8. Security
- Secrets management (specific tool/approach)
- File permissions / service user model
- Network exposure (which services public, which internal)
- API authentication for the web frontend
- Audit log immutability and tamper detection mechanism
- Backup encryption keys management

### 9. Deployment Topology
- VPS specs (Hetzner Ashburn, sizing recommendation with justification)
- Docker Compose layout (services, networks, volumes)
- Environment configuration (dev local, paper, live)
- Deployment procedure (manual + agent-driven hot-fix paths)
- Rollback procedure
- DR runbook including IBKR phone trading desk path and TWS manual override

### 10. Testing Strategy
- Unit test inventory (what's covered, what's not, why)
- Integration test inventory
- CI/CD pipeline (GitHub Actions recommended)
- Pre-merge gates with specifics
- Strategy validation pipeline (paper-minimum enforcement; how it's mechanically blocked from skipping)

### 11. Phased Build Plan
Aligned to operator's 6–12 month runway:
- **Phase 0 (weeks 0–4):** foundation — operator upskilling, IBKR account, QC setup, audit schema design, repo + CI scaffolding, secrets management, VPS provisioned
- **Phase 1 (months 1–4):** live track record on QC; custom backend skeleton in parallel (audit ingestion from QC webhook, FastAPI scaffold, Postgres schema, Discord bot bootstrap)
- **Phase 2 (months 4–8):** custom infra hardening; LEAN Local deployment; ib_insync integration; paper validation; migrate execution
- **Phase 3 (months 8–12):** capital scaling, second strategy preparation, family-money legal structure
- Each phase: deliverables, success criteria (objective metrics), kill criteria (when to abandon)

### 12. Claude Ops Agent Detailed Spec
- Trigger model (cron, event-driven, on-demand from Discord)
- Tool inventory (specific bounded actions agent can take, with parameters)
- Prompt-cache strategy (system prompt, codebase context, market state — what's cached, TTLs)
- Cost budget and monitoring (~$30–100/mo target; alert if exceeded)
- Failure mode handling (Claude API outage, hallucination detection via constrained outputs, rate limits)
- Audit trail of every agent decision with prompt + response captured
- Rollback mechanism for agent-deployed hot-fixes if metrics degrade

## FORMAT REQUIREMENTS

- Markdown with clear section headers
- Mermaid for ALL diagrams
- Concrete library/tool/version recommendations (e.g., "PostgreSQL 16, asyncpg 0.29.x" not "a database")
- Where genuine implementation choices remain, present 2–3 options with tradeoffs and a recommendation
- Length will be substantial; favor completeness over brevity
- Never invent strategic decisions; if context is missing, flag with `[QUESTION FOR OPERATOR: ...]`
- This spec must interlock with the frontend spec (separate session); reference shared decisions explicitly so contracts align

Begin.
