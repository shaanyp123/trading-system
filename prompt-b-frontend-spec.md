# PROMPT B — FRONTEND TECH SPEC

## ROLE

You are a senior frontend architect and design engineer with experience building production trading dashboards and operator interfaces for systematic CTAs and prop shops. You understand that trading UIs are utilitarian, dense, and fast — not consumer-app pretty. They resemble Bloomberg, Linear, or a small CTA's research environment, not a startup landing page.

You will produce a comprehensive technical specification for the FRONTEND of a single-operator algorithmic trading system. Implementation will be primarily by Claude Code working with a non-technical solo operator.

**The SPEC is comprehensive (full target shape); the BUILD is phased (Phase 1 ships ~30% of it; rest follows in Phase 2 and 3). The phased build plan in §11 is binding; do not infer that everything ships in Phase 1.**

## OPERATOR CONTEXT

- Solo operator, finance background, no coding ability, US-based (NJ), trades alone
- Moves around frequently — must be able to operate the system from mobile (signal approval, monitoring, queries) and from desk (research, deep review, parameter changes)
- Responsible for own and (eventually) family money
- Wants the simplest possible interface that still surfaces everything when needed

## COMPANION BACKEND

This frontend integrates with a Python backend (specced in a parallel session — Prompt A) running:
- LEAN engine (QC cloud Phase 1, LEAN Local Phase 2)
- PostgreSQL 16 for transactional state (asyncpg + SQLAlchemy 2.x async + Alembic)
- DuckDB on Parquet for analytics
- `ib-async` to IBKR (Phase 2)
- Claude ops agent service
- FastAPI for API exposure
- Single Hetzner Cloud Ashburn VPS, Ubuntu LTS, Docker Compose
- structlog JSON logging

The frontend talks to the backend via FastAPI REST + SSE. The Discord bot ALSO talks to the backend (mostly via REST + receives backend-to-bot HTTP-IPC events for forwarding to Discord — see Architecture / Topology below). Some user actions (signal approval, kill-switch invoke, decision diary entry) must be possible from BOTH the web app AND Discord and produce identical outcomes via shared backend endpoints.

## ARCHITECTURE / TOPOLOGY (LOCKED — explicit)

```
Browser ──── HTTPS ──── Vercel (static + SSR)
   │
   └──── HTTPS / SSE / fetch credentials:include ───── api.<domain> (FastAPI on Hetzner VPS)
                                                              │
                                                              ├── Postgres
                                                              ├── LEAN engine
                                                              ├── Claude ops agent
                                                              └── Discord bot (separate process)

External watchdog (different region, e.g., Hetzner Falkenstein or AWS Lambda)
   │
   └── pings api.<domain> /health every 5 min; emails operator if unreachable >15 min during market hours
```

- **Vercel hosts the Next.js app** (static + SSR pages). Free Hobby tier sufficient for our solo-operator load.
- **All live data (REST + SSE) flows browser ↔ VPS DIRECT.** Vercel does NOT proxy live data (Hobby function timeouts and SSE incompatibility make proxying unworkable). Frontend uses `process.env.NEXT_PUBLIC_API_BASE` to reach the VPS.
- **Domain layout:** `app.<domain>` (Vercel) for the SPA; `api.<domain>` (Hetzner VPS) for REST/SSE. Both are subdomains of one parent domain so WebAuthn RP and cookie scoping are clean.
- **WebAuthn Relying Party:** `<domain>` (parent). Cookies issued by backend, `Domain=.<domain>; HttpOnly; Secure; SameSite=Strict`. Frontend `fetch` calls use `credentials: 'include'`. CORS allowlist on backend permits the Vercel origin.
- **"Frontend uptime independent of VPS" applies to static page rendering only.** When the VPS is down, static pages still load but show stale-data indicators on every live element. This is acceptable; Discord is the operational channel and the frontend isn't a critical-path tool during outages.
- **External watchdog (mandatory):** a tiny separate-region service pings the VPS health endpoint every 5 minutes. If unreachable for >15 min during market hours, sends email to operator. ~$5/month.

## TECH STACK (LOCKED)

- **Next.js 14+ App Router** + **TypeScript** (strict mode) + **Tailwind CSS** + **shadcn/ui** components
- **TanStack Query** for server-state, **Zustand** for client-state
- **TanStack Table + `@tanstack/react-virtual`** for the Trades page and audit explorer (shadcn alone is insufficient for virtualized tables; this fills the gap)
- **Recharts** for analytics charts; **Lightweight Charts** (TradingView OSS) for price/equity curves
- **Server-Sent Events (SSE)** primary real-time mechanism; **polling fallback** at degraded refresh rate
- **Hosted on Vercel free Hobby tier** for static + SSR; live data direct to VPS (see Topology above)
- **Authentication:** WebAuthn (passkey) primary + TOTP backup + 8 single-use printed backup codes generated at enrollment; codes regenerable from authenticated session
- **Authorization (RBAC):** schema present from day 1; "owner" role active initially; "reader" role planned for CPA in year 2; investor role NEVER (investors get PDF reports, not dashboard access)
- **Reader role permission matrix (year-2 deliverable, design in schema now):**
  - Reader CAN view: Performance page (all metrics), Trades (read-only, no detail drawer write), Tax export download
  - Reader CANNOT view: System (risk envelope, deployments, audit), Research (parameter sandbox, A/B comparisons), Calendar ratification controls, agent activity prompts/responses
  - Reader CANNOT do: any writes, signal approval/reject, kill-switch, parameter changes, deploy actions
- **PDF rendering:** **Typst** (modern, deterministic, fast) running in a container on the VPS. Frontend triggers via API; receives signed download URL or streamed binary.
- **Toast/in-app notifications:** **`sonner`** library
- **Form handling:** **`react-hook-form`** with **`zod`** validators (zod schemas shared with backend pydantic models via codegen if feasible, else manually mirrored)

For Discord:
- **`discord.py`** bot (runs as Python service on the VPS, separate from web frontend)
- **Slash commands** + **button interactions** + **embeds** + **threads**
- **Backend → Bot communication:** internal HTTP-IPC (FastAPI POST to bot's local HTTP listener on Docker network), NOT public webhooks. The "webhook" terminology in the original spec was misleading; correct as IPC.

## DESIGN PHILOSOPHY (BINDING — DO NOT SOFTEN)

- **Utilitarian, not aesthetic.** Resemble Bloomberg, Linear, professional CTA tools. Dense. Fast. Monospaced numbers. Dark by default. NO animations, NO gradients, NO marketing-style polish. Animation budget: ≤150ms, only where it adds clarity (state transitions, modal open/close).
- **Functional and fast over decorated.** Iterate on function and speed; never on decoration.
- **Mobile = Discord, NOT a native app.** Web app is desk-only. NO native mobile build, ever.
- **Tablet policy:** below **1024px viewport width**, render a "use desktop or Discord" notice with a Discord deep-link button. NO responsive optimization for tablet/phone. Operator should not be using the web app on a small screen for trading-critical actions.
- **Simple now, simple later.** Add features only when actual usage demands. Aggressively reject feature creep within scope. The spec is comprehensive because the system has a comprehensive shape; the build is phased to keep day-1 surface narrow.
- **Single density mode** (dense). Not user-configurable.
- **Single theme** (dark). Not user-configurable.
- **Numeric formatting:** US locale, tabular figures via `font-feature-settings: 'tnum'`. Negatives = leading minus + red color + small downward arrow icon (color-blind safe — never rely on color alone). Positives = no sign + green + upward arrow on emphasized values; bare number elsewhere.
- **Time-zone:** ALL UI in `America/New_York`. Backend stores UTC; presentation always ET. NOT user-configurable. Calendar events also rendered in ET.
- **Numeric precision:** read from backend's `instrument_metadata` (tick size, point value, price decimals) — never hardcoded in frontend.

## INFORMATION ARCHITECTURE — 6 PAGES

1. **Today** — landing page; single-glance dashboard
2. **Trades** — unified signal queue + position monitor + fill history + per-trade journal + attribution; filterable
3. **Performance** — equity curve, drawdown, attribution by market/signal/regime, monthly tearsheet, exportable PDF
4. **Research** — backtest viewer, parameter sandbox (PR-drafting), regime analysis, A/B comparison
5. **System** — risk envelope, kill-switch UI, deployments log, agent activity, full audit explorer, reconciliation status
6. **Calendar** — economic events, tomorrow's ratification, exchange holidays, contract expiration / roll schedule

NO additional pages. NO investor dashboards (investors get PDF reports). NO mobile-optimized variants. NO "Agent" page (agent chat is in Discord; agent activity surfaced in System page).

## LOCKED STRATEGIC AND SYSTEMS DECISIONS — DO NOT REOPEN

### Strategy
- Multi-asset systematic trend-following on micro futures + bond ETFs
- Universe: ~8–12 markets (equity index micros, commodity micros, /MBT, bond ETFs)
- Daily bars; signals fire at session close (preferred) or open (fallback)
- Holding period: 2 weeks to 6 months
- Phase 2 adds defined-risk vol carry as second uncorrelated strategy (sequential addition)

### Phasing
- Phase 0 (weeks 0–6): paper trading begins on QC week 1; QC adapter coded by week 4; 30 paper days complete
- Phase 1 (months 2–5): live on QC; custom backend skeleton parallel; **frontend Phase 1 surface ships before live trading begins** (Today + Trades + Discord signal-approval flow + minimal System and Performance)
- Phase 2 (months 5–9): full Performance + Research + Calendar; six additional features; harden
- Phase 3 (months 9–12): investor PDF generation; CPA reader role; refinements

### Risk Framework (frontend must visualize; numbers locked)
- Vol-targeted sizing, **14% portfolio annualized vol** (locked single value)
- Three concentric rings, all measured in **gross/net notional vs. equity**:
  - Per-position max **25%** of equity notional
  - Gross portfolio max **300%** of equity notional (locked, not range)
  - Net portfolio max **150%** of equity notional
- Cluster caps: equity-index micros combined **60%**, commodity (combined) **80%**, rates/bonds **80%**, crypto **40%**
- Daily loss limit **-5%** of daily-start MTM equity
- Trailing DD limit **-20%** from peak intraday MTM equity since inception
- Monthly DD threshold **-10%** triggers vol-target halving for remainder of month
- Decommission floor: live 30-day Sharpe < 0 OR live max DD breach -25% OR 60-day live underperforms backtest by > 2 SD → HALT_NEW + human review

### Kill-Switch State Machine (frontend must render)
- States: `NORMAL`, `HALT_NEW` (cancel working orders, hold positions, no new entries, manual resume), `CONVALESCENT` (50% vol target, 5 trading sessions portfolio-wide, auto-transition to NORMAL)
- Convalescent mode banner shows: state name, sessions remaining, current vol target (7%), and link to history
- No HALT_ALL or auto-liquidate state

### Audit & Track Record
- Immutable append-only audit log with SHA-256 single-linked hash chain
- Strategy version governance via git hash
- **Track record portability semantics:** lineage metadata persisted across phases; UI segregates environments (`paper` / `live-small` / `live-scale`) via filters and tabs; **never visually splices different environments into one chart or single number.** Default views filter to current environment; toggle to multi-environment requires explicit "show all (segregated)" mode that renders separate panels per environment.
- **Environment tagging boundary:**
  - `paper` = any non-real-money trade, regardless of capital
  - `live-small` = real money, account equity < $50k at signal time
  - `live-scale` = real money, account equity ≥ $50k at signal time
  - Boundary applied at signal-emit time; trades carry their tag immutably
- Trade-level attribution by version, signal type, market, vol regime, trend regime

### Tax (frontend must render)
- Futures (1256): automatic 60/40 LTCG/STCG, no election, MTM mandatory; widget shows YTD breakdown
- ETFs: capital gains/losses with wash sale tracking; no 475(f) election by default
- Tax estimate widget surfaces YTD liability, 1256 60/40 breakdown, wash sale flagged trades
- Election toggle in UI must be gated by "I have consulted a CPA" acknowledgment

### Claude Ops Agent Authority Matrix (frontend must display agent activity correctly)
| Category | Agent Authority |
|---|---|
| Tighten risk | AUTO + notify |
| Loosen risk | HUMAN APPROVAL |
| Hot-fix infra | AUTO-DEPLOY + auto-rollback if degraded |
| Strategy logic changes | DRAFTS PR (operator-friendly review surface) |
| Place/modify/cancel orders | NEVER (hard-coded; agent has no broker creds) |
| Invoke kill switch | AUTO on threshold |
| Un-invoke kill switch | HUMAN ONLY (re-auth required) |
| Strategy params within pre-approved range | AUTO + auto-revert if degraded |
| Reports/alerts/briefings/diagnostics | AUTO |

### Performance Targets (frontend must surface progress against these)
- Phase 1: backtest Sharpe ≥ 1.5, live ≥ 0.8, max DD ≤ 15%, signal acceptance ≥ 90%
- Phase 2: portfolio live ≥ 1.2
- Phase 3: portfolio live ≥ 1.5

### Severity Model (locked)
Backend assigns one of three severity levels to every alert; frontend renders accordingly:
- **P0** — kill-switch fired, broker disconnect, reconciliation break, margin auto-trim invoked. Critical channel + email backup.
- **P1** — slippage drift, model decay, capacity warning, anomalous signal flagged. Warn channel.
- **P2** — informational (fills, daily summary, agent reports). Routine.

### Anomaly-Flagged Signals (locked definition)
A signal is `anomaly_flagged` when **any** of the following at signal-emit time:
- Vol regime z-score > 1.5
- Capacity at > 1% ADV (between alert and cap)
- Decision diary previously logged a `data_concern` or `regime_concern` for the same market within 14 days
- Backtest expected slippage exceeded by > 2× in last 5 trades for the same market
- Strategy-version-vs-baseline divergence flagged in last week's golden test

`anomaly_flagged` is a backend-emitted boolean on each signal; frontend reads it. Bulk-approve "all standard" disables when ANY signal in the queue is anomaly-flagged.

### Stale-Data Thresholds (locked, per data type)

| Data Type | Stale during market hours | Stale outside market hours |
|---|---|---|
| P&L (live) | 5s | 60s |
| Positions | 30s | 5min |
| Open orders | 10s | 60s |
| Recent fills | 10s | 60s |
| Health score | 60s | 5min |
| Calendar | 24h | 24h |
| Backtest results | never (immutable artifacts) | — |
| Audit log entries (trailing edge) | 5min | 30min |

Stale-data indicator: subtle yellow corner badge on the affected widget + tooltip with last-update timestamp.

### Re-Auth Requirements (locked)
- WebAuthn user-verification re-prompt (NOT TOTP re-entry) within last **5 minutes** required for:
  - Kill-switch invoke or resume
  - Parameter range change PR submission
  - Deploy approval (any)
  - Environment tag override
  - Backup code regeneration
  - Tax election toggle
- Session age check is the gate; UV re-prompt is the proof.

### Strategy Health Score (locked formula)
Composite metric, top-right of every page, click-to-expand for component scores.

| Component | Weight | Window | Score 0–100 |
|---|---|---|---|
| Live Sharpe vs. backtest | 30% | 60-day rolling | 100 if live ≥ backtest; 0 if live < backtest − 2σ; linear in between |
| Slippage drift | 20% | 30-day rolling | 100 if realized ≤ assumed; 0 if realized ≥ 2× assumed; linear |
| Hit rate vs. expected | 20% | 60-day rolling | 100 if live ≥ expected; 0 if live ≤ expected − 20%; linear |
| Capacity headroom | 15% | current | 100 if avg position < 0.25% ADV; 0 if any > 1% ADV; linear |
| Days since last reconciliation break | 15% | current | 100 if ≥ 30 days; 0 if < 1 day; sqrt-shaped |

Composite = weighted sum.
- **Green:** ≥ 75
- **Yellow:** 50–74
- **Red:** < 50

## YOUR DELIVERABLE

Produce a complete, production-grade frontend technical specification covering ALL sections below. Use Mermaid for diagrams. Wireframes described in TEXT/ASCII/Mermaid (not image generation). Be specific and concrete; do NOT punt with "follow design system best practices." Where genuine implementation choices remain, present 2–3 options with tradeoffs and a recommendation.

**Backend API contract:** the parallel backend spec produces the canonical REST/SSE/Discord schema. Your frontend spec must reference these by name (path, channel, event type) — for every screen, list the specific endpoints/channels it consumes. If the backend session has not yet produced the contract, declare your expected contract and flag with `[CONTRACT — verify against Prompt A output]` so cross-checking at integration is mechanical.

### 1. Information Architecture
- Full IA tree (page → sections → components → states)
- Navigation model (top nav recommended; defend if differing)
- Command palette (cmd-k) — globally searchable surfaces, shortcuts triggered, action set
- Keyboard shortcuts inventory (every critical operator action accessible without mouse)
- Persistent UI elements (top bar): strategy health score (G/Y/R) on right; current portfolio P&L; agent status indicator; environment tag (`paper` / `live-small` / `live-scale`); current state (NORMAL / HALT_NEW / CONVALESCENT) with banner if not NORMAL

### 2. Screen-by-Screen Specification

For each of the 6 pages: layout, component hierarchy, data displayed (with backend source — endpoint or SSE channel; reference Prompt A contract by name), empty/loading/error/partial-data/stale-data states, interactions, real-time update behavior, filter/sort/search controls, accessibility considerations.

#### Today (landing)
- Strategy health score (G/Y/R) prominent + click-expand to component scores
- Current positions table (compact, monospace numbers, virtualized if >50 rows)
- P&L summary (D/W/M/Y) with benchmark comparison (default SPY)
- Exposure breakdown (gross / net / per-market / per-cluster) — visualized against ring limits
- Queued signals pending approval — quick approve/reject inline; bulk "approve all standard" disabled when any anomaly-flagged
- Recent fills feed (live via SSE)
- Active alerts (severity-sorted P0 → P1 → P2)
- Stress test "run now" button (async; opens progress drawer)
- Quick links to anomalies (backend-emitted list of abnormal-but-not-failure conditions; each navigates to relevant page filtered to relevant period/market)

#### Trades
- Unified table (TanStack Table + `@tanstack/react-virtual`): signals, orders, fills, positions
- Filters: date range, market, strategy version, regime (vol + trend), signal type, environment (paper/live-small/live-scale; never blended)
- Per-trade detail drawer: full lifecycle from signal generation through exit; decision diary entry; attribution; agent commentary; linked audit log entries; stress-test impact
- Signal approval flow with reason capture for rejections (decision diary modal)
- Bulk approve "all standard" — only enabled when no signal in batch is `anomaly_flagged`
- Server-side pagination with cursor-based infinite scroll
- Server-side filter pushdown (no client-side filtering of large result sets)
- Expected scale: ~50–200 trades/month at full operation; 5-year accumulation ~3k–12k trades

#### Performance
- Equity curve with benchmark overlay (SPY default, configurable to 60/40 SPY/AGG or custom symbol)
- Drawdown chart (underwater plot)
- Monthly returns calendar heatmap
- Attribution by market, signal type, vol regime, trend regime — switchable views
- Rolling Sharpe, rolling DD, rolling hit rate (60-day windows by default; toggleable)
- **Actual vs. rule-following P&L compare:** dual equity curves; rolling divergence metric; alert at threshold
- **Tax estimate widget:** YTD liability, 1256 60/40 split, wash sales flagged; click-expand for per-trade breakdown; election toggle gated by CPA acknowledgment
- **Environment-segregation rule:** by default, charts filter to a single environment (current). "Show all environments (segregated)" toggle renders separate stacked panels per environment with clear labeling. NEVER one curve combining live-small + live-scale. NEVER one number combining paper + live.
- PDF export: monthly/quarterly tearsheets via Typst (server-side render); see §13 for layout

#### Research
- Backtest result loader (from CLI-generated artifacts in DuckDB/Parquet via backend API)
- Equity curve, trade list, statistics for a backtest
- Parameter sandbox: propose change → drafts a PR via backend (backend holds GitHub App install token; frontend never touches GitHub)
- Regime analysis: filter strategy performance by regime conditions
- A/B comparison view (strategy version v3 vs. v4 on same dataset)
- Walk-forward results visualizer

#### System
- Risk envelope: view current limits with cluster cap visualization; propose changes via PR-drafting workflow (re-auth required)
- Kill switch: status (NORMAL / HALT_NEW / CONVALESCENT), history, manual invoke (with confirmation modal + re-auth), recovery flow
- Convalescent mode banner: when active, prominent on every page with sessions remaining, current vol target (7%), exit countdown
- Deployments log: every deploy (agent hot-fix + human merge), with diff view and rollback button (re-auth required)
- Agent activity feed: drafted PRs, hot-fixes deployed, alerts raised, decisions made; each entry expandable to show prompt + response
- **Operator-friendly PR review surface (critical for non-coding operator):**
  1. Plain-English summary (≤200 words) — written by agent: what changed, why, what behavior changes
  2. Risk impact summary (auto-generated): which risk metrics affected, by how much, in plain numbers
  3. Backtest delta: equity curve overlay, key statistics delta table, ten worst-divergence trades
  4. Test results: unit + integration + linting + type-check, all visible
  5. Files affected: list with one-line summary per file
  6. Diff view: collapsed by default, expandable
  7. In-app Approve / Reject / Request Changes buttons (sync to GitHub via backend)
- Full audit explorer: cursor-paginated, server-side filter pushdown, virtualized list with infinite scroll; full-text search on `reason` field via Postgres FTS; hash-chain integrity badge per record (boolean from backend); environment filter
- Reconciliation status: last reconciliation time per source (TWS API real-time / FlexQuery EOD), any breaks, weekly summary
- External watchdog status (last successful ping)

#### Calendar
- 30-day forward view of macro events (tier 1 / 2 / 3, color-coded)
- Tomorrow's events ratification: must be ratified by 23:00 ET nightly; if not, hard halt for next session until ratified (frontend mirrors this state — banner shows "Ratification required for [date]" with one-tap ratify)
- Contract expiration / roll schedule (futures only)
- Exchange holidays
- Manual event log (operator-added events; logged to audit)

### 3. Six Locked Additional Features (each spec'd concretely)
- **Decision diary:** structured tag (`data_concern` / `regime_concern` / `size_concern` / `manual_judgment` / `other`) + free text on every signal override / rejection / deferral; required minimum 10 chars on operator-authored reasoning; queryable via Trades page filters + Postgres FTS on text; surfaced in trade detail drawer and Performance attribution
- **Actual vs. rule-following P&L compare:** dual equity curves on Performance page; rolling 30-day divergence metric; alert threshold at 5% deviation
- **Strategy health score:** composite formula (locked above with weights, windows, thresholds); persistent top-right; click-expand
- **Benchmark overlay:** equity curve plotted against SPY default; configurable to 60/40 SPY/AGG or custom symbol via dropdown
- **Tax estimate widget:** YTD liability, 1256 60/40 split, wash sale flagging; updated nightly via backend cron; click-expand for per-trade breakdown
- **Stress test:** single button on Today; **async execution** (POST → 202 + jobId, SSE progress events, terminal payload); progress drawer with cancel option; modal shows P&L impact, worst-hit positions, ring-cap proximity; scenarios = 1σ / 2σ / 3σ down day, 2008 / 2020 / 2022 replays

### 4. Real-Time Update Mechanism
- SSE channel inventory (each channel name + event types + payload schema)
- Per-page update strategy (which fields update via SSE, which via polling, which on manual refresh)
- Polling fallback configuration (intervals per data type — match stale-data thresholds above)
- Reconnection / resilience handling (exponential backoff; resume-from-last-event-id)
- Stale-data indicator UI (yellow corner badge + tooltip with last-update timestamp)
- **Multi-tab behavior:** each tab opens its own SSE connection; if browser/server limit reached, oldest tab disconnects with a banner notice. (Phase 2+: consider `BroadcastChannel` or shared worker; not Phase 1.)

### 5. Auth and Session Management
- WebAuthn registration flow (Mermaid sequence) — RP = parent domain, ceremony hosted on backend, frontend redirects via Vercel rewrite or full navigation
- TOTP backup flow
- 8 single-use backup codes generated at WebAuthn enrollment; printed by user; hashed in DB; regenerable from authenticated session
- Session token model: JWT access (15-min lifetime) + refresh token (7-day), HttpOnly + Secure + SameSite=Strict cookies, server-side session records for revocation
- Re-auth (WebAuthn UV re-prompt within last 5 minutes) required for sensitive actions (kill-switch resume, parameter range change PR, deploy approval, env tag override, backup code regen, tax election toggle)
- RBAC schema (owner active; reader planned with full permission matrix above)
- Account recovery: backup code → reset WebAuthn + TOTP enrollment via backend admin endpoint; document the recovery procedure

### 6. Discord Bot Specification (CRITICAL — primary mobile surface)

#### Channels
For each channel (`#daily-brief`, `#signals`, `#fills`, `#alerts`, `#critical`, `#ops`, `#ask-agent`, `#audit`): purpose, message format (full embed schemas with field-by-field), who/what writes to it, how user interacts with it.

#### Slash Commands
For each command (`/positions`, `/exposure`, `/pnl`, `/halt`, `/calendar`, `/last-fills`, `/report`, `/health`, `/ratify`, `/ask`, `/vacation start`, `/vacation end`, etc.): parameters, response format, permissions, confirmation modals where required.

#### Button Interactions
Signal approval/reject/defer buttons:
- Payload format
- State machine (signal → pending → approved/rejected/deferred → executed/expired)
- Confirmation modals where required (any kill-switch action, any rejection requires decision diary modal)
- Decision diary capture on rejections (modal with tag picker + text field, min 10 chars)
- Bulk approve "all standard" button on daily brief (disabled if any signal anomaly_flagged)

#### Threads
Per-trade thread model — each trade gets its own thread for fill updates, agent commentary, operator notes

#### Backend → Bot IPC (corrected from "webhook")
Backend posts events to bot's local HTTP listener on Docker internal network: format, retry, failure handling. If bot delivery fails (HTTP 5xx or connection refused) AND Discord delivery via bot itself fails, automatic email-backup escalation. **External watchdog** also covers VPS-down case independently.

#### Bot Architecture
- `discord.py` async event loop
- Connection to backend (REST + SSE for real-time updates from VPS-internal endpoints)
- State management: stateless preferred; fetches from backend
- Restart/recovery: idempotent re-subscription to events; replays missed messages from backend buffer (last 1h)

#### Web/Discord Action Parity
For every action surfaced in BOTH web and Discord (signal approval, kill switch invoke/resume, decision diary entry, ratify tomorrow's events, run stress test, query positions/P&L), spec the shared backend endpoint and confirm both surfaces produce identical audit-log outcomes. Reference the backend endpoint by name from Prompt A contract.

### 7. Component Library Inventory
Beyond shadcn/ui defaults, spec all custom components needed:
- Trade row (states: pending, approved, executed, closed, rejected, capacity-constrained)
- Signal approval card with buttons
- Anomaly badge (icon + tooltip with reason)
- Health score indicator (G/Y/R + expandable component panel)
- Equity curve chart wrapper (with benchmark overlay support)
- Drawdown chart (underwater plot)
- Attribution treemap or bar
- Stress test result modal
- Stress test progress drawer (async with cancel)
- Decision diary entry form (tag picker + text, min-length validator)
- PR draft preview (plain-English + risk impact + backtest delta + diff view)
- Kill-switch button (with safety confirmation + re-auth)
- Audit log row with expansion + hash-chain integrity badge
- Convalescent mode banner (sessions remaining + vol target + exit countdown)
- Reconciliation status indicator
- Stale-data corner badge (yellow + tooltip)
- Environment tag pill (`paper` / `live-small` / `live-scale`)
- Strategy version badge
- External watchdog status indicator
- Vacation mode banner

For each: purpose, props, states, accessibility (keyboard nav, screen reader, never rely on color alone — pair with icon/text), tabular-num CSS application.

### 8. Data Fetching and State Strategy
- TanStack Query patterns (staleness, refetch policies per data type — match stale-data thresholds)
- Zustand store organization (what's client-state vs. server-state — keep narrow)
- Optimistic updates: signal approval, decision diary, ratification
- Cache invalidation rules
- Error boundary placement
- Loading state strategy (skeleton vs. spinner — when each)
- All metrics computed backend-side (health score, attribution, tax, stress test); frontend is a renderer

### 9. Design Tokens
- Color palette (dark default; semantic tokens for P&L green/red, severity P0/P1/P2, regime indicators, environment pills; all paired with icon/text for color-blind safety)
- Typography scale (monospaced for ALL numbers — JetBrains Mono or Inconsolata; sans for prose — Inter)
- `font-feature-settings: 'tnum'` applied globally to numeric tabular contexts
- Spacing scale (4px base; dense)
- Animation timing (≤150ms; only state transitions and modal open/close; never decoration)
- Density mode (single — dense; not configurable)

### 10. Sequence Diagrams (Mermaid)
At minimum:
- WebAuthn registration with backup code generation
- WebAuthn login with re-auth challenge for sensitive action
- TOTP backup login flow
- Backup code recovery flow
- Signal arrives in queue → user approves via web → backend executes → fill displays via SSE
- Same signal flow but approved via Discord button (parity)
- User rejects signal with decision diary entry (web AND discord paths)
- User invokes kill switch from Discord (with confirmation)
- User invokes kill switch from web (with re-auth)
- User resumes from HALT_NEW → CONVALESCENT (re-auth required)
- Stress test button → POST 202 + jobId → SSE progress → terminal payload
- PR draft from parameter sandbox → operator-friendly review surface render → human reviews → merges via backend → deploys
- Real-time fill update via SSE
- Tomorrow's events ratification flow (web and discord)
- Vacation mode start, daily-summary-only operation, end
- VPS outage → external watchdog email → operator manual flow

### 11. Phased Build Plan
Aligned to operator's 6–12 month runway, in parallel with backend:
- **Phase 0 (weeks 0–6):** scaffold (Next.js, auth + WebAuthn enrollment, basic Today page reading from backend, Discord bot skeleton with `/positions` and `/halt`); ships before paper trading begins
- **Phase 1 (months 2–5):** full Today + Trades + Discord signal approval flow (web AND discord parity); minimal Performance + System + Calendar; ships before live trading begins
- **Phase 2 (months 5–9):** full Performance + Research + Calendar; six additional features (decision diary, actual-vs-rule compare, health score, benchmark overlay, tax estimate, stress test); harden
- **Phase 3 (months 9–12):** investor PDF generation via Typst; CPA reader role plumbing; refinements

Each phase: deliverables, success criteria, kill criteria.

### 12. Testing Strategy
- Component tests (Vitest + React Testing Library) — coverage targets per component category
- E2E critical flows (Playwright): WebAuthn registration + login, signal approval (web + discord), kill switch invoke + resume, decision diary entry, ratification, stress test async flow, PR review surface render
- Visual regression (Chromatic recommended) for design system consistency
- Accessibility audits (axe-core in CI) — WCAG 2.1 AA target
- Discord bot tests: command response correctness, button payload handling, IPC ingestion
- Cross-environment segregation tests: assert no UI element ever blends `paper` and `live-*` data in a single number or chart

### 13. Investor PDF Report Layout (year-2 deliverable, spec it now)
Renderer: **Typst** running in a container on the VPS; frontend triggers via API.

Layout:
- Cover page (period, fund/strategy name placeholder, prepared-by, date)
- Performance summary table (returns by period, comparison to benchmark)
- Equity curve and drawdown chart (rendered via Typst's plotting)
- Monthly returns table (calendar heatmap)
- Risk metrics (Sharpe, Sortino, max DD, hit rate, vol)
- Attribution summary (by market or strategy, depending on Phase)
- Methodology disclosure (one paragraph)
- Risk disclosures (standard CTA-style language placeholder)
- Footer: page numbers, generation timestamp, hash of source data for audit

### 14. SLO / Performance Budgets
- TTI: ≤ 2s on cable connection
- LCP: ≤ 2.5s on /today
- p99 SSE event-to-render: ≤ 500ms
- Initial JS bundle: ≤ 250KB gzipped for /today; ≤ 500KB for heavier pages (/performance, /system audit)
- Lighthouse performance score ≥ 90 on /today

## FORMAT REQUIREMENTS

- Markdown with clear section headers
- Mermaid for ALL diagrams
- Wireframes in text/ASCII/Mermaid (no image generation)
- Concrete library/tool/version recommendations
- Where genuine implementation choices remain, present 2–3 options with tradeoffs and a recommendation
- Length will be substantial; favor completeness over brevity
- Never invent strategic decisions; flag missing context with `[QUESTION FOR OPERATOR: ...]`
- For backend contract dependencies, flag with `[CONTRACT — verify against Prompt A output]` and proceed with your expected contract
- This spec must interlock with the backend spec; reference Prompt A's REST endpoints, SSE channels, and Discord command schemas by name where they exist

Begin.
