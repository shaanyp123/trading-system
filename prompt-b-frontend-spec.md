# PROMPT B — FRONTEND TECH SPEC

## ROLE

You are a senior frontend architect and design engineer with experience building production trading dashboards and operator interfaces for systematic CTAs and prop shops. You understand that trading UIs are utilitarian, dense, and fast — not consumer-app pretty. They resemble Bloomberg, Linear, or a small CTA's research environment, not a startup landing page.

You will produce a comprehensive technical specification for the FRONTEND of a single-operator algorithmic trading system. Implementation will be primarily by Claude Code working with a non-technical solo operator.

## OPERATOR CONTEXT

- Solo operator, finance background, no coding ability, US-based (NJ), trades alone
- Moves around frequently — must be able to operate the system from mobile (signal approval, monitoring, queries) and from desk (research, deep review, parameter changes)
- Responsible for own and (eventually) family money
- Wants the simplest possible interface that still surfaces everything when needed

## COMPANION BACKEND

This frontend integrates with a Python backend (specced separately) running:
- LEAN engine (QC cloud Phase 1, LEAN Local Phase 2)
- PostgreSQL for transactional state
- DuckDB on Parquet for analytics
- ib_insync to IBKR (Phase 2)
- Claude ops agent service
- FastAPI for API exposure
- Single Hetzner Cloud Ashburn VPS, Ubuntu LTS, Docker Compose

The frontend talks to the backend via FastAPI REST + SSE. The Discord bot ALSO talks to the backend (mostly via REST + receives webhooks). Some user actions (signal approval, kill-switch invoke, decision diary entry) must be possible from BOTH the web app AND Discord and produce identical outcomes via shared backend endpoints.

## TECH STACK (LOCKED)

- **Next.js 14+ App Router** + **TypeScript** + **Tailwind CSS** + **shadcn/ui** components
- **TanStack Query** for data fetching, **Zustand** for client state
- **Recharts** for analytics charts; **Lightweight Charts** (TradingView OSS) for price/equity curves
- **Server-Sent Events (SSE)** primary real-time mechanism; **polling fallback**
- **Hosted on Vercel free tier** (separate from VPS so frontend uptime is independent)
- **WebAuthn (passkey)** primary auth + **TOTP** backup
- **RBAC schema** present from day 1; only "owner" role active initially; "reader" role planned for CPA in year 2; investor role NEVER (investors get PDF reports, not dashboard access)

For Discord:
- **`discord.py`** bot (runs as Python service on the VPS, separate from web frontend)
- **Slash commands** + **button interactions** + **embeds** + **threads**

## DESIGN PHILOSOPHY (BINDING — DO NOT SOFTEN)

- **Utilitarian, not aesthetic.** Resemble Bloomberg, Linear, professional CTA tools. Dense. Fast. Monospaced numbers. Dark by default. NO animations, NO gradients, NO marketing-style polish.
- **Functional and fast over decorated.** Iterate on function and speed; never on decoration.
- **Mobile = Discord, NOT a native app.** Web app is desk-only. Tablet acceptable but unoptimized. NO native mobile build, ever.
- **Simple now, simple later.** Add features only when actual usage demands. Aggressively reject feature creep.
- **Single density mode** (dense). Not user-configurable.
- **Single theme** (dark). Not user-configurable.

## INFORMATION ARCHITECTURE — 6 PAGES

1. **Today** — landing page; single-glance dashboard
2. **Trades** — unified signal queue + position monitor + fill history + per-trade journal + attribution; filterable
3. **Performance** — equity curve, drawdown, attribution by market/signal/regime, monthly tearsheet, exportable PDF
4. **Research** — backtest viewer, parameter sandbox (PR-drafting), regime analysis, A/B comparison
5. **System** — risk envelope, kill-switch UI, deployments log, agent activity, full audit explorer, reconciliation status
6. **Calendar** — economic events, tomorrow's ratification, exchange holidays, contract expiration / roll schedule

NO additional pages. NO investor dashboards (investors get PDF reports). NO mobile-optimized variants. NO "Agent" page (agent chat is in Discord; agent activity surfaced in System page).

## LOCKED STRATEGIC DECISIONS — DO NOT REOPEN

### Strategy
- Multi-asset systematic trend-following on micro futures + bond ETFs
- Universe: ~8–12 markets (equity index micros, commodity micros, /MBT, bond ETFs)
- Daily bars, signals fire at session close/open
- Holding period: 2 weeks to 6 months
- Phase 2 adds defined-risk vol carry as second uncorrelated strategy

### Phasing
- Phase 1 months 1–4: live on QC
- Phase 2 months 4–8: custom infra
- Frontend exists from Phase 1 day 1 reading from backend that ingests QC events via webhook

### Risk Framework (frontend must visualize)
- Vol-targeted sizing, 12–15% portfolio vol
- Three concentric rings: per-position 25% gross; gross 250–300%; net 150%
- Daily loss -5%, trailing DD -20%, monthly DD threshold -10%
- Kill switches: auto-halt, human-only resume; convalescent mode after resume
- Margin protocol: 70% warn / 85% auto-trim
- Capacity tracking: 0.5% ADV alert, 2% ADV refuse

### Audit & Track Record
- Immutable append-only audit log
- Strategy version governance via git hash
- Track record portability across phases
- Environment tagging (paper/live-small/live-scale) — never blended in any UI display
- Trade-level attribution by version, signal type, market, vol regime, trend regime

### Tax
- 1256 60/40 split for futures
- Wash sale tracking for ETFs
- Year-end harvest opportunity flagging

### Claude Ops Agent Authority Matrix (frontend must display agent activity correctly)
| Category | Agent Authority |
|---|---|
| Tighten risk | AUTO + notify |
| Loosen risk | HUMAN APPROVAL |
| Hot-fix infra | AUTO-DEPLOY + auto-rollback if degraded |
| Strategy logic changes | DRAFTS PR |
| Place/modify/cancel orders | NEVER |
| Invoke kill switch | AUTO on threshold |
| Un-invoke kill switch | HUMAN ONLY |
| Strategy params within pre-approved range | AUTO + auto-revert if degraded |
| Reports/alerts/briefings/diagnostics | AUTO |

### Performance Targets (frontend must surface progress against these)
- Phase 1: backtest Sharpe ≥1.5, live ≥0.8, max DD ≤15%, signal acceptance ≥90%
- Phase 2: portfolio live ≥1.2
- Phase 3: portfolio live ≥1.5

## YOUR DELIVERABLE

Produce a complete, production-grade frontend technical specification covering ALL sections below. Use Mermaid for diagrams. Wireframes described in TEXT/ASCII/Mermaid (not image generation). Be specific and concrete; do NOT punt with "follow design system best practices." Where genuine implementation choices remain, present 2–3 options with tradeoffs and a recommendation.

### 1. Information Architecture
- Full IA tree (page → sections → components → states)
- Navigation model (top nav recommended; defend if differing)
- Command palette spec (cmd-k) — what's globally searchable, what shortcuts trigger
- Keyboard shortcuts inventory (every critical operator action accessible without mouse)
- Persistent UI elements: strategy health score (G/Y/R) top-right of every page; current portfolio P&L always visible in header; agent status indicator; environment tag (paper/live)

### 2. Screen-by-Screen Specification

For each of the 6 pages: layout, component hierarchy, data displayed (with backend source — endpoint or SSE channel), empty/loading/error/partial-data states, interactions, real-time update behavior, filter/sort/search controls, and tablet considerations (responsive but desk-primary).

#### Today (landing)
- Strategy health score (G/Y/R) prominent
- Current positions table (compact)
- P&L summary (Day/Week/Month/YTD) with benchmark comparison
- Exposure breakdown (gross / net / per-market)
- Queued signals pending approval — quick approve/reject inline
- Recent fills feed
- Active alerts (severity-sorted)
- Stress test "run now" button
- Quick links to anomalies (jumps to relevant audit / decay metric)

#### Trades
- Unified table: signals, orders, fills, positions
- Filters: date range, market, strategy version, regime (vol + trend), signal type, environment (paper/live)
- Per-trade detail drawer: full lifecycle from signal generation through exit; decision diary entry; attribution; agent commentary; linked audit log entries
- Signal approval flow with reason capture for rejections (decision diary)
- Bulk approve "all standard" — only enabled when no signal in batch is anomaly-flagged

#### Performance
- Equity curve with benchmark overlay (SPY default, configurable to 60/40 SPY/AGG or custom)
- Drawdown chart (underwater plot)
- Monthly returns calendar heatmap
- Attribution by market, signal type, vol regime, trend regime — switchable views
- Rolling Sharpe, rolling DD, rolling hit rate
- **Actual vs. rule-following P&L compare** (dual equity curves; divergence highlight)
- **Tax estimate widget** (YTD liability, 1256 60/40 breakdown, wash sales flagged)
- PDF export for monthly/quarterly tearsheets — designed for future investor distribution; spec the layout

#### Research
- Backtest result loader (from CLI-generated artifacts in DuckDB/Parquet)
- Equity curve, trade list, statistics for a backtest
- Parameter sandbox: propose change → drafts a PR (linked to GitHub) → human reviews and merges
- Regime analysis: filter strategy performance by regime conditions
- A/B comparison view (strategy version v3 vs. v4 on same dataset)
- Walk-forward results visualizer

#### System
- Risk envelope: view current limits, propose changes (PR-drafting workflow)
- Kill switch: status (enabled/disabled), history, manual invoke (with confirmation modal), recovery flow
- Deployments log: every deploy (agent hot-fix + human merge), with diff view and rollback button
- Agent activity feed: drafted PRs, hot-fixes deployed, alerts raised, decisions made; each entry expandable to show prompt + response
- Full audit explorer: filterable log of every system event with hash-chain integrity indicator
- Reconciliation status: last reconciliation time, any breaks, weekly summary
- Convalescent mode indicator (if active)

#### Calendar
- 30-day forward view of macro events (tier 1 / 2 / 3, color-coded)
- Tomorrow's events ratification: acknowledge / flag concern (logs to audit; user does this nightly via Discord, web is alternative surface)
- Contract expiration / roll schedule
- Exchange holidays
- Manual event log (user-added events)

### 3. Six Locked Additional Features (each must be specified concretely)
- **Decision diary:** structured tag (`data_concern`, `regime_concern`, `size_concern`, `manual_judgment`, `other`) + free text on every signal override / rejection / deferral; queryable; surfaced in trade detail drawer and Performance attribution
- **Actual vs. rule-following P&L compare:** dual equity curves on Performance page; rolling divergence metric; alert if divergence exceeds threshold
- **Strategy health score:** composite metric formula (recent Sharpe vs. backtest, slippage drift, hit rate vs. expected, capacity headroom, days since reconciliation break) → G/Y/R; persistent top-right; click expands to show component scores
- **Benchmark overlay:** equity curve plotted against SPY default; user-configurable
- **Tax estimate widget:** YTD liability, 1256 60/40 split, wash sale flagging, updated nightly; click expands to detailed by-trade breakdown
- **Stress test:** single button on Today; runs scenarios (1σ/2σ/3σ down day, 2008/2020/2022 replays) against current book; modal shows P&L impact and worst-hit positions

### 4. Real-Time Update Mechanism
- SSE channel inventory (what events flow through which channels)
- Per-page update strategy (which fields update real-time, which on refresh, which manual)
- Polling fallback configuration (intervals per data type)
- Reconnection / resilience handling
- Stale-data indicator UI (when SSE disconnects, when data is older than expected)

### 5. Auth and Session Management
- WebAuthn registration and login flow (Mermaid sequence)
- TOTP backup flow
- Session token model (lifetime, refresh, revocation)
- Re-auth requirements for sensitive actions (kill switch resume, parameter change PR submission, deploy approval, env-tag change)
- RBAC schema (owner active; reader planned)

### 6. Discord Bot Specification (CRITICAL — primary mobile surface)

#### Channels
For each channel (`#daily-brief`, `#signals`, `#fills`, `#alerts`, `#critical`, `#ops`, `#ask-agent`, `#audit`): purpose, message format (full embed schemas), who/what writes to it, how user interacts with it.

#### Slash Commands
For each command (`/positions`, `/exposure`, `/pnl`, `/halt`, `/calendar`, `/last-fills`, `/report`, `/health`, `/ratify`, `/ask`, etc.): parameters, response format, permissions, confirmation modals where required.

#### Button Interactions
Signal approval/reject/defer buttons:
- Payload format
- State machine (signal → pending → approved/rejected/deferred → executed)
- Confirmation modals where required (any kill-switch action)
- Decision diary capture on rejections (modal with tag picker + text field)
- Bulk approve "all standard" button on daily brief

#### Threads
Per-trade thread model — each trade gets its own thread for notes, agent commentary, fill updates

#### Webhook Ingestion
Backend → Discord event push: format, retry, failure handling, automatic email-backup escalation if delivery fails

#### Bot Architecture
- `discord.py` async event loop
- Connection to backend (REST + SSE for real-time updates)
- State management (stateless preferred; fetches from backend)
- Restart/recovery behavior (idempotent re-subscription to events)

#### Web/Discord Action Parity
For every action surfaced in BOTH web and Discord (signal approval, kill switch invoke/resume, decision diary entry, ratify tomorrow's events, run stress test, query positions/P&L), spec the shared backend endpoint and confirm both surfaces produce identical audit-log outcomes.

### 7. Component Library Inventory
Beyond shadcn/ui defaults, spec all custom components needed:
- Trade row (multiple states: pending, approved, executed, closed, rejected)
- Signal approval card with buttons
- Health score indicator (G/Y/R + expandable detail)
- Equity curve chart wrapper (with benchmark overlay support)
- Drawdown chart (underwater plot)
- Attribution treemap or bar
- Stress test result modal
- Decision diary entry form (tag picker + text)
- PR draft preview (diff view)
- Kill-switch button (with safety confirmation)
- Audit log row with expansion
- Convalescent mode banner
- Reconciliation status indicator
- Environment tag pill (paper/live-small/live-scale)
- Strategy version badge

For each: purpose, props, states, accessibility (keyboard nav, screen reader, color contrast considering color-blind users — never rely on color alone for severity).

### 8. Data Fetching and State Strategy
- TanStack Query patterns (staleness, refetch policies per data type)
- Zustand store organization (what's in client state vs. server state)
- Optimistic updates where appropriate (signal approval, decision diary)
- Cache invalidation rules
- Error boundary placement
- Loading state strategy (skeleton vs. spinner — when each)

### 9. Design Tokens
- Color palette (dark default; semantic colors for P&L green/red, alert states, regime indicators; accessible to color-blind operators)
- Typography scale (monospaced for ALL numbers — JetBrains Mono or Inconsolata; sans for prose — Inter)
- Spacing scale (4px base; dense)
- Animation timing (minimal — only where it adds clarity, never decoration; max 150ms)
- Density mode (single — dense; not configurable)

### 10. Sequence Diagrams (Mermaid)
At minimum:
- WebAuthn registration and login
- Signal arrives in queue → user approves via web → backend executes → fill displays
- Same signal flow but approved via Discord button (parity)
- User rejects signal with decision diary entry (web AND discord paths)
- User invokes kill switch from Discord
- User invokes kill switch from web
- Stress test button → backend runs scenarios → results display
- PR draft from parameter sandbox → human reviews on GitHub → merges → deploys
- Real-time fill update via SSE
- Tomorrow's events ratification flow

### 11. Phased Build Plan
Aligned to operator's 6–12 month runway, in parallel with backend:
- **Phase 0 (weeks 0–4):** scaffold (Next.js, auth, basic Today page reading from backend, Discord bot skeleton with `/positions` and `/halt`)
- **Phase 1 (months 1–4):** full Today + Trades + Discord signal approval flow (web AND discord parity); minimal Performance + System; ship before live trading begins
- **Phase 2 (months 4–8):** full Performance + Research + Calendar; six additional features (decision diary, actual-vs-rule compare, health score, benchmark overlay, tax estimate, stress test); harden
- **Phase 3 (months 8–12):** investor PDF generation; CPA reader role plumbing; refinements

Each phase: deliverables, success criteria, kill criteria.

### 12. Testing Strategy
- Component tests (Vitest + React Testing Library) — what's covered
- E2E critical flows (Playwright): signal approval (web + discord), kill switch, login, PR drafting
- Visual regression (Chromatic recommended) for design system consistency
- Accessibility audits (axe-core in CI) — WCAG AA target
- Discord bot tests: command response correctness, button payload handling, webhook ingestion

### 13. Investor PDF Report Layout (year-2 deliverable, spec it now)
- Cover page (period, fund/strategy name placeholder, prepared-by)
- Performance summary (returns by period, comparison to benchmark)
- Equity curve and drawdown chart
- Monthly returns table
- Risk metrics (Sharpe, Sortino, max DD, hit rate)
- Attribution summary (by market or strategy, depending on Phase)
- Methodology disclosure (one paragraph)
- Risk disclosures (standard CTA-style language placeholder)

## FORMAT REQUIREMENTS

- Markdown with clear section headers
- Mermaid for ALL diagrams
- Wireframes in text/ASCII/Mermaid (no image generation)
- Concrete library/tool/version recommendations
- Where genuine implementation choices remain, present 2–3 options with tradeoffs and a recommendation
- Length will be substantial; favor completeness over brevity
- Never invent strategic decisions; flag missing context with `[QUESTION FOR OPERATOR: ...]`
- This spec must interlock with the backend spec (separate session); shared API contracts and data semantics must align — explicitly reference the corresponding backend endpoints/channels

Begin.
