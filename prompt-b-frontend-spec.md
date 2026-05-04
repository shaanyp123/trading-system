# PROMPT B — FRONTEND TECH SPEC

## ROLE

You are a senior frontend architect and design engineer with experience building production trading dashboards and operator interfaces for systematic CTAs and prop shops. You understand that trading UIs are utilitarian, dense, and fast — not consumer-app pretty. They resemble Bloomberg, Linear, or a small CTA's research environment, not a startup landing page.

You will produce a comprehensive technical specification for the FRONTEND of a single-operator algorithmic trading system. Implementation will be primarily by Claude Code working with a non-technical solo operator.

**The SPEC is comprehensive (full target shape); the BUILD is phased. Phase 1 ships a defined ~30% subset (enumerated per page in §2); the rest follows in Phase 2 and 3. The phased build plan in §11 is binding; do not infer that everything ships in Phase 1.**

## OPERATOR CONTEXT

- Solo operator, finance background, no coding ability, US-based (NJ), trades alone
- Moves around frequently — must operate the system from mobile (signal approval, monitoring, queries via Discord) and from desk (research, deep review, parameter changes via web)
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
- Discord bot service (separate process, internal Docker network) and Discord webhook-pusher service (separate process)
- External watchdog (separate region)

The frontend talks to the backend via FastAPI REST + SSE. The Discord bot ALSO talks to the backend (mostly via REST + receives backend-to-bot HTTP-IPC events). Most user actions (signal approval, decision diary entry, ratification) must be possible from BOTH the web app AND Discord and produce identical outcomes via shared backend endpoints. **Asymmetric exception:** kill-switch INVOKE is available from both surfaces; kill-switch RESUME is web-only (requires WebAuthn UV which Discord cannot perform). This asymmetry is intentional safety design — invoking is risk-tightening (cheap to be wrong about); resuming is risk-loosening (must be supervised with strong auth).

## ARCHITECTURE / TOPOLOGY (LOCKED — explicit)

```
Browser ──── HTTPS ──── Vercel (static SPA bundle + SSG/ISR shell)
   │
   └──── HTTPS / SSE / fetch credentials:include ───── api.<domain> (FastAPI on Hetzner VPS)
                                                              │
                                                              ├── Postgres
                                                              ├── LEAN engine
                                                              ├── Claude ops agent
                                                              ├── Discord bot service
                                                              └── Discord webhook-pusher service

External watchdog (different region — e.g., Hetzner Falkenstein or AWS Lambda)
   │
   ├── pings api.<domain>/health every 5 min
   ├── pushes ping result to api.<domain>/internal/watchdog (backend stores)
   └── emails operator if backend unreachable >15 min during market hours
```

**Rendering model (corrected):**
- **NOT SSR with per-request VPS calls** (defeats uptime independence claim).
- Pre-auth pages (`/login`, `/setup`, `/recover`) and the post-auth shell are **SSG (static generation)** with client-side hydration.
- Authenticated data fetches happen **client-side** via TanStack Query against `api.<domain>`.
- Non-personalized content (marketing-style — none planned) would be ISR; not in scope.
- "Frontend uptime independent of VPS" applies to **shell rendering** only; live data goes stale or shows stale-data indicators when VPS is down.

**Network details:**
- All live data (REST + SSE) flows browser ↔ VPS DIRECT. Vercel does NOT proxy live data.
- Domain layout: `app.<domain>` (Vercel) for the SPA; `api.<domain>` (Hetzner VPS) for REST/SSE. Both subdomains of one parent domain so WebAuthn RP and cookie scoping are clean.
- WebAuthn Relying Party: parent `<domain>` (root). Cookies issued by backend, `Domain=.<domain>; HttpOnly; Secure; SameSite=Strict`. Frontend `fetch` calls use `credentials: 'include'`. CORS allowlist on backend permits Vercel origin.
- WebAuthn ceremonies happen via **full navigation to `api.<domain>`** for register/authenticate endpoints (NOT cross-origin fetch). Reason: WebAuthn ceremonies are sensitive to origin context; full navigation is the simplest correct pattern. Post-ceremony, redirect back to `app.<domain>` with session established.
- **UV freshness mechanism:** server-side session row in Postgres carries `last_uv_at` timestamp; updated on every WebAuthn UV-attested authentication. Sensitive actions check `last_uv_at >= now() - 5 min` server-side. Cookie carries opaque session ID only.

## TECH STACK (LOCKED)

- **Next.js 14+ App Router** + **TypeScript** (strict mode) + **Tailwind CSS** + **shadcn/ui** components
- **TanStack Query** for server-state, **Zustand** for client-state
- **TanStack Table + `@tanstack/react-virtual`** for large tables (Trades, audit explorer)
- **Recharts** for analytics charts; **Lightweight Charts** (TradingView OSS) for price/equity curves; **lazy-loaded per page** (not bundled into `/today`)
- **Server-Sent Events (SSE)** primary real-time mechanism via single multiplexed channel; **REST polling fallback** at degraded rates
- **Sonner** for toasts
- **react-hook-form + zod** for forms
- **Vercel free Hobby tier** for static + SSG hosting; live data direct to VPS
- **Authentication:** WebAuthn (passkey) primary + TOTP backup + 8 single-use printed backup codes generated at enrollment; codes regenerable from authenticated session
- **Authorization (RBAC):** schema present from day 1; "owner" role active initially; "reader" role planned for CPA in year 2; investor role NEVER (PDF reports only)
- **Reader role permission matrix (year-2 deliverable, schema present now):**
  - Reader CAN view: Performance page (all metrics), Trades (read-only including per-trade detail and **decision diary** for tax provenance), Tax export download, **tax widget detail**
  - Reader CANNOT view: System (risk envelope, deployments, agent activity prompts/responses), Research (parameter sandbox, A/B), Calendar ratification controls, account numbers (PII redacted from any view)
  - Reader CANNOT do: any writes, signal approval/reject, kill-switch, parameter changes, deploy actions
- **PDF rendering:** **Typst** for layout/typography on the VPS, with **charts pre-rendered server-side as SVG via headless Recharts** and embedded as images (Typst's native plotting is too sparse for the density we need). Frontend triggers via API; receives signed download URL.
- **Error tracking and RUM:** **Sentry** free tier (errors) + Sentry Performance Monitoring at low-volume tier (~$26/mo if usage warrants; otherwise free tier sufficient for solo). NOT Datadog.
- **Feature flagging:** simple env-var-based flags for Phase 1/2/3 gates (`NEXT_PUBLIC_PHASE=1|2|3`); read at boot; reload required for changes. NO PostHog/LaunchDarkly.
- **Browser support:** latest 2 stable versions of Chrome, Firefox, Safari. Edge implicit (Chromium). Detect WebAuthn support; show explainer if unsupported.

For Discord:
- **`discord.py`** bot (runs as Python service on the VPS, separate from web frontend; communicates with backend via internal HTTP-IPC over Docker network)
- **Slash commands** + **button interactions** + **embeds** + **threads**

## DESIGN PHILOSOPHY (BINDING — DO NOT SOFTEN)

- **Utilitarian, not aesthetic.** Resemble Bloomberg, Linear, professional CTA tools. Dense. Fast. Monospaced numbers. Dark by default. NO marketing-style polish.
- **Animations:** **functional only** (state transitions, modal open/close, drawer slide). Max 150ms duration. NO decorative or attention-grabbing animations. (Single statement; replaces the prior "no animations" + "≤150ms" pair.)
- **Functional and fast over decorated.** Iterate on function and speed; never on decoration.
- **Mobile = Discord, NOT a native app.** Web app is desk-only. NO native mobile build, ever.
- **Tablet policy:** below **1024px viewport width**, render a "use desktop or Discord" notice with a Discord deep-link button. NO responsive optimization for tablet/phone.
- **Simple now, simple later.** Add features only when usage demands.
- **Single density mode** (dense). Not configurable.
- **Single theme** (dark). Not configurable.
- **Numeric formatting:** US locale; tabular figures via `font-feature-settings: 'tnum'`. Negatives = leading minus + red color + small downward arrow icon (color-blind safe). Positives = no sign + green + upward arrow on emphasized values; bare otherwise.
- **Time-zone:** ALL UI in `America/New_York`. Backend stores UTC; presentation always ET. NOT user-configurable.
- **Numeric precision:** read from backend's `instrument_metadata` (tick size, point value, decimals) — never hardcoded.
- **Time source for stale-data calculation:** server-supplied timestamps in every payload (`server_now: timestamp`); frontend computes staleness as `server_now − data.timestamp > threshold`. **Browser clock is never trusted** for stale calculations.

## INFORMATION ARCHITECTURE — 6 POST-AUTH PAGES + 3 PRE-AUTH SURFACES

**Post-auth (the "6 pages"):**
1. **Today** — landing; single-glance dashboard
2. **Trades** — unified signal queue + position monitor + fill history + per-trade journal + attribution; filterable
3. **Performance** — equity curve, drawdown, attribution, tearsheet, PDF export
4. **Research** — backtest viewer, parameter sandbox, regime analysis, A/B
5. **System** — risk envelope, kill-switch UI, deployments, agent activity, audit explorer, reconciliation, watchdog status
6. **Calendar** — events, ratification, holidays, roll schedule

**Pre-auth surfaces (NOT counted as "pages" but must be specced):**
- **`/login`** — WebAuthn login + TOTP fallback + backup-code link
- **`/setup`** — first-run bootstrap: backend prints one-time registration token at first start; operator visits `/setup?token=...` to enroll first WebAuthn credential and TOTP, generate 8 printed backup codes
- **`/recover`** — account recovery with backup code; resets WebAuthn + TOTP enrollment; if all factors lost, requires backend `dba_breakglass` procedure (escalation message + contact path shown)

NO additional post-auth pages. NO investor dashboards. NO mobile-optimized variants. NO "Agent" page.

## LOCKED STRATEGIC AND SYSTEMS DECISIONS — DO NOT REOPEN

### Strategy and Phasing
- Multi-asset systematic trend-following on micro futures + bond ETFs
- Universe: ~8–12 markets (equity index micros, commodity micros, /MBT, bond ETFs)
- Daily bars; signal generation 17:30 ET
- Phase 0 (weeks 0–7): backend foundation; paper begins week 1; **frontend Phase 0 (weeks 0–3 in parallel):** scaffold (Next.js, auth + setup + login + recovery, Discord bot skeleton with `/positions` and `/halt`); reads from backend audit log starting week 3–4 once QC adapter is wired
- Phase 1 (months 2–5): live track record on QC; frontend Phase 1 surface ships before live trading begins (see §11 for per-page enumeration)
- Phase 2 (months 5–9): full Performance + Research + Calendar; six additional features; harden
- Phase 3 (months 9–12): investor PDF generation; CPA reader role; refinements

### Risk Framework (frontend must visualize; numbers locked from Prompt A)
- Vol-targeted sizing, **14% portfolio annualized vol** (default; agent-mutable within Min/Max table in Prompt A)
- **Per-position / gross / net trio:** 25% / 300% / 150% of equity notional (THE three concentric "rings"; reframe — clusters are a separate constraint dimension, not concentric)
- **Cluster caps (separate dimension, not part of rings):** equity-index 60%, commodity 80%, rates/bonds 80%, crypto 40%; realized cross-portfolio correlation alert >0.7, halt >0.85
- Daily loss limit -5% of daily-start MTM (17:00 ET CME settle anchor)
- Trailing DD limit -20% from peak intraday MTM (subject to capital-event reset on deposit ≥5% equity)
- Monthly DD threshold -10% triggers vol-target halving for remainder of month
- Decommission floor: live 30-day Sharpe < 0 OR live max DD breach -25% OR 60-day live underperforms backtest by > 2 SD → HALT_NEW + human review

### Vol-Target Multiplier Composition (locked)
When multiple vol-target reductions are active simultaneously (e.g., CONVALESCENT 50% AND monthly-DD halving 50%), **take the MIN of the multipliers — do NOT compound.** Active multiplier in this example = 0.5 (= 7% portfolio vol). Avoids over-restrictive composition that would suppress legitimate signals.

### Kill-Switch State Machine (frontend must render)
- States: `NORMAL`, `HALT_NEW`, `CONVALESCENT`
- HALT_NEW: cancel working orders, hold positions, no new ENTRIES
- **Manual position close (exit) IS allowed during HALT_NEW.** Only opening a new position is blocked. Frontend allows close-position action even in HALT_NEW (with re-auth as it's a manual order action, even though it's risk-reducing).
- CONVALESCENT: 50% vol target (subject to MIN composition rule above), 5 trading sessions portfolio-wide, auto-transitions to NORMAL
- No HALT_ALL or auto-liquidate state. Margin auto-trim is graduated de-leverage, not panic-flatten (see Prompt A for full algorithm).
- Convalescent mode banner shows: state name, sessions remaining, current effective vol target, link to history

### Audit & Track Record
- Immutable append-only audit log with SHA-256 single-linked hash chain (insertion-order, with `repaired_for_sequence_no` provenance for backfills; gaps remain visible)
- Strategy version + parameter set composite identity (`strategy_hash` + `parameter_set_hash` from Prompt A)
- **Track record portability semantics:** lineage metadata persisted; UI segregates environments via filters and tabs; **never visually splices different environments into one chart or single number** — except the strategy health score (which always scopes to current environment; see Health Score below)
- Environment tagging boundaries: `paper` / `live-small` (real money, equity < $50k) / `live-scale` (real money, equity ≥ $50k); determined at signal-emit time; immutable per trade

### Tax (frontend must render)
- Futures (1256): automatic 60/40 LTCG/STCG, no election
- ETFs: capital gains/losses with wash sale tracking; no 475(f) election by default; system supports both modes
- Tax estimate widget: YTD liability, 1256 60/40 breakdown, wash-sale-flagged trades; nightly update
- **Tax election toggle (specific scope):** toggles ETF treatment between (a) default = no 475(f), wash-sale tracking active, capital gains/losses; vs (b) elected 475(f) = MTM treatment, no wash-sale tracking, ordinary gain/loss. Gated by "I have consulted a CPA" acknowledgment modal. Election is logged to audit_log with full provenance.

### Claude Ops Agent Authority Matrix (frontend must display agent activity correctly)
| Category | Agent Authority |
|---|---|
| Tighten risk via parameter change (next cycle) | AUTO + notify |
| Tighten risk via defensive position trim (mid-session) | AUTO + notify |
| Loosen risk | HUMAN APPROVAL |
| Hot-fix infra | AUTO-DEPLOY + auto-rollback if degraded |
| Strategy logic changes | DRAFTS PR (operator-friendly review surface) |
| Place orders directly as primary action | NEVER (hard-coded) |
| Invoke kill switch | AUTO on threshold |
| Un-invoke kill switch | HUMAN ONLY (re-auth, web-only) |
| Strategy params within pre-approved range | AUTO + auto-revert if degraded (2 SD widened) |
| Reports/alerts/briefings/diagnostics | AUTO |

### Performance Targets
- Phase 1 single strategy: backtest Sharpe ≥ 1.5, live ≥ 0.8, max DD ≤ 15%, signal acceptance ≥ 90%
- Phase 2 portfolio: live ≥ 1.2
- Phase 3 portfolio: live ≥ 1.5

### Severity Model (locked)
- **P0** — kill-switch fired, broker disconnect, reconciliation tolerance breach, margin auto-trim invoked, audit-log write failure, Defensive Risk Envelope. Critical channel + email backup.
- **P1** — slippage drift, model decay, capacity warning, anomalous signal flagged, vol regime transition. Warn channel.
- **P2** — informational (fills, daily summary, agent reports, ratification reminders). Routine.

### Anomaly-Flagged Signals (locked)
A signal is `anomaly_flagged: true` when **any** of:
- Vol regime z-score > 1.5
- Capacity at > 1% ADV (between alert and cap)
- Decision diary previously logged a `data_concern` or `regime_concern` for the same market within 14 days
- Backtest expected slippage exceeded by > 2× in last 5 trades for the same market
- Strategy-version-vs-baseline divergence flagged in last week's golden test

Backend emits `anomaly_reasons: string[]` alongside the boolean (each entry is a short machine-readable reason code; frontend maps to human-readable tooltip text). Frontend renders an anomaly badge on signals where flagged; tooltip lists the specific reasons that fired.

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
| Watchdog last-ping | 10min | 30min |

**Stale-data indicator:** subtle yellow corner badge on the affected widget + tooltip with last-update timestamp.

**Distinguishing PAUSED vs. STALE:** when system state is HALT_NEW or CONVALESCENT (or vacation mode), data flows that are intentionally paused (new signals, new fills) display a "PAUSED — last activity at X" pill **instead of** the stale-data badge. Backend signals state changes via SSE so frontend can switch indicators without false-positive stale flagging.

### Re-Auth Requirements (locked)
WebAuthn UV re-prompt within last **5 minutes** required for **risk-loosening** actions. NOT required for risk-tightening actions.

**Required (all risk-loosening or sensitive):**
- Kill-switch RESUME (un-invoke) — web-only by design (Discord cannot perform UV; resume from Discord is explicitly NOT supported)
- Parameter range change PR submission
- Deploy approval (any)
- Environment tag override
- Backup code regeneration
- Tax election toggle
- Manual position close during HALT_NEW (manual order action)

**NOT required:**
- Kill-switch INVOKE (risk-tightening — friction-light)
- Defensive trim invocation (also risk-tightening)
- Signal approval/reject (in normal flow; protected by session)
- Decision diary entry
- Calendar ratification
- Stress test run

UV freshness mechanism: server-side `last_uv_at` per session row in Postgres; checked server-side on sensitive endpoint calls.

### Strategy Health Score (locked formula)
**Scope:** current environment only (typically `live-scale` if active; otherwise `live-small`; otherwise `paper`). Single number cannot blend environments. Indicator at top-right of every page. Click expands component scores.

| Component | Weight | Window | Score 0–100 |
|---|---|---|---|
| Live Sharpe vs. backtest | 30% | 60-day rolling | 100 if live ≥ backtest; 0 if live < backtest − 2σ; linear |
| Slippage drift | 20% | 30-day rolling | 100 if realized ≤ assumed; 0 if realized ≥ 2× assumed; linear |
| Hit rate vs. expected | 20% | 60-day rolling | 100 if live ≥ expected; 0 if live ≤ expected − 20%; linear |
| Capacity headroom | 15% | current | 100 if avg position < 0.25% ADV; 0 if any > 1% ADV; linear |
| Days since last reconciliation break | 15% | current | 100 if ≥ 30 days; 0 if < 1 day; sqrt-shaped |

Composite = weighted sum.
- **Green:** ≥ 75
- **Yellow:** 50–74
- **Red:** < 50

**Insufficient data handling (first 60 days of live or any window where data isn't available):**
- Each component with insufficient data renders gray "—" pill in the expansion panel; tooltip: "insufficient data (X/N days)"
- Composite degrades gracefully: re-weights available components to total 100% and computes; if composite total weight < 50%, render G/Y/R indicator as gray "—" overall with explainer "insufficient data — track record under construction"
- Indicator never lies; missing data shown as missing, not as zero

### Concurrent-Tab Race Resolution (locked, Phase 1 minimal)
- Each tab opens its own SSE connection
- Optimistic UI shows immediate change on user action (e.g., signal approved)
- If server SSE event contradicts optimistic state (e.g., signal already approved by another tab), show toast "approved by another tab" and revert local state to server truth
- Phase 2 enhancement: `BroadcastChannel` for cross-tab coordination to avoid contradicting optimistic actions

### Bulk-Approve "Standard" Signals (rule clarified)
The button approves **all signals in the queue that are NOT `anomaly_flagged`**. Anomaly-flagged signals remain in the queue for individual review (the whole point — bulk-approve is for the unambiguous tail).
- **Disabled when:** ZERO non-anomaly signals exist in queue (nothing to bulk-approve).
- **Enabled when:** ≥1 non-anomaly signal in queue, regardless of how many anomaly-flagged are also present.

### Toast/Alert Taxonomy (locked)
- **P0:** persistent until manually dismissed; subtle single-chime sound (no loop); top-center; full-width banner-style
- **P1:** 8s auto-dismiss; top-right
- **P2:** 4s auto-dismiss; top-right
- **Stack cap 5 visible;** older toasts pushed off and collapse to "+N more" notification group at top-right; clicking expands a drawer of recent toasts

### Empty-State Inventory (Phase 0/1 day-one states must be specified per page)
Every screen requires explicit empty-state design for:
- No trades yet (Trades, Performance, Today)
- No backtests yet (Research)
- No alerts (alerts panel)
- No agent activity (System / agent feed)
- No reconciliation history (System)
- No calendar events for the day (Calendar)
- Insufficient health-score data (Today)
- No stress test scenarios run yet
- Pre-deployment state (Phase 0 weeks 0–3 when no live data exists at all)

### Strategy Version Badge (specified)
- **Global badge** (top bar, near health score): currently DEPLOYED strategy version (short hash + click for full info)
- **Per-trade pill** (in Trades table rows and detail drawer): version active at signal emit time (different field; smaller; muted color)

### Trade State Enumeration (locked, single source of truth)
`pending` → `approved` | `rejected` | `deferred` | `expired`
`approved` → `working` (broker has order) → `partially_filled` | `filled` (= `executed`)
`filled` / `executed` → `open_position` (held) → `closed`
`partially_filled` → can also enter `capacity_constrained` if cap-limited
`open_position` → on stop hit: `stopped_out`
Any terminal state (`closed`, `stopped_out`, `rejected`, `expired`) is final.

Frontend renders state via badge with consistent color/icon; spec requires explicit state machine.

### Vacation Mode (frontend must render and enforce)
- Server-enforced (backend refuses new entries during vacation; calendar ratification suspended)
- Frontend reads vacation state via SSE; renders persistent banner with end date, "End vacation now" button (re-auth not required to end since ending vacation re-enables strategy entries which is risk-loosening but the operator is the same person who started it; small inconsistency — flag this for review post-implementation if it bothers the operator, can add re-auth)
- **Ending vacation:** allowed from web (with confirmation) and from Discord (`/vacation end`)
- **Queued signals during vacation:** new signals not generated; existing positions can stop out

### PR Rejection Feedback Loop
When operator rejects an agent-drafted PR via the operator-friendly review surface:
- Modal: tag picker (`logic_disagreement` | `risk_concern` | `unclear_rationale` | `bad_test_coverage` | `other`) + free text, min 10 chars
- Reason logged to audit + fed back to agent context for next iteration's prompt cache
- PR closed with "rejected by operator: [reason]" comment
- Agent learns "this kind of change is rejected for [reason]" — reduces wasted PR drafts

## YOUR DELIVERABLE

Produce a complete, production-grade frontend technical specification covering ALL sections below. Use Mermaid for diagrams. Wireframes described in TEXT/ASCII/Mermaid (not image generation). Be specific and concrete.

**Backend API contract:** the parallel backend spec (Prompt A) produces the canonical REST/SSE/Discord schema. Reference these by name where they exist; flag with `[CONTRACT — verify against Prompt A output]` and proceed with your expected contract where they don't. **Specific expected SSE channel architecture (single multiplexed channel, browsers cap ~6 SSE per origin):**

- One SSE endpoint: `GET /api/sse/events`
- Event types multiplexed within: `signal`, `fill`, `position`, `pnl`, `risk_state`, `health`, `alert`, `audit`, `agent`, `vacation`, `watchdog`
- Each event: `{ type, server_now, data }`
- Client filters/dispatches by `type`
- One connection per tab; on browser SSE limit, oldest tab disconnects with banner

### 1. Information Architecture
- Full IA tree (page → sections → components → states)
- Pre-auth surfaces (`/login`, `/setup`, `/recover`) specced in addition to the 6 post-auth pages
- Navigation model (top nav recommended; defend if differing)
- Command palette (cmd-k): pages, plus search corpus = trades by ID/symbol, signals by ID, audit entries by ID/text — extensible per feature
- **Keyboard shortcuts:** `?` opens cheat-sheet modal; document the full shortcut list
- Persistent UI elements (top bar): strategy version badge (global), strategy health score (G/Y/R, current-environment scoped), current portfolio P&L, agent status indicator, environment tag (`paper` / `live-small` / `live-scale`), current state (NORMAL / HALT_NEW / CONVALESCENT / VACATION) with banner if not NORMAL

### 2. Screen-by-Screen Specification

For each of the 6 post-auth pages: layout, component hierarchy, data displayed (with backend source — endpoint or SSE event type; reference Prompt A by name; flag with `[CONTRACT]` where backend contract is unconfirmed), empty/loading/error/partial-data/stale-data/paused states, interactions, real-time update behavior, filter/sort/search controls, accessibility considerations.

**Phase 1 surface enumeration per page (binding):**

| Page | Phase 1 ships | Phase 2 adds |
|---|---|---|
| Today | Health score (with insufficient-data handling), positions table, P&L summary D/W/M/Y, exposure breakdown, queued signals (individual approve/reject; NO bulk-approve, NO anomaly badge yet), recent fills feed, P0/P1 alerts, paused-state distinction | Stress test button, anomalies quick-link list, P2 alerts integration |
| Trades | Filterable table (date/market only filters), per-trade summary row | Per-trade detail drawer, decision diary, attribution view, all filters, bulk-approve, anomaly badges |
| Performance | Equity curve (no benchmark overlay yet), monthly returns table | Drawdown underwater, attribution by market/signal/regime, actual-vs-rule compare, tax estimate widget, PDF export, benchmark overlay |
| Research | (not in Phase 1) | Backtest viewer, parameter sandbox, regime analysis, A/B compare, walk-forward visualizer |
| System | Kill-switch UI + state, audit log basic table (cursor-paginated, basic filter), reconciliation status, watchdog status | Risk envelope view + propose-PR, deployments log + rollback, agent activity feed, full audit explorer with FTS, operator-friendly PR review surface, convalescent mode banner |
| Calendar | Read-only event list (next 30 days) | Tomorrow's ratification flow (web; Discord ratification ships in Phase 1), holidays, contract expiration / roll schedule, manual event log |

#### Today (full target)
- Strategy health score (G/Y/R) prominent + click-expand
- Current positions table (compact, monospace, virtualized if >50 rows)
- P&L summary (D/W/M/Y) with benchmark comparison (Phase 2: configurable; Phase 1: SPY default if shown at all)
- Exposure breakdown (gross / net / per-market / per-cluster) visualized against ring + cluster limits
- Queued signals — quick approve/reject inline; bulk "approve standard" button (rule above)
- Recent fills feed (live via SSE event type `fill`)
- Active alerts (P0 → P1 → P2 sorted)
- Stress test "run now" button (Phase 2; async; opens progress drawer)
- Quick links to anomalies (Phase 2; backend-emitted list of abnormal-but-not-failure conditions; click navigates to relevant page filtered)

#### Trades (full target)
- Unified table (TanStack Table + `@tanstack/react-virtual`): signals, orders, fills, positions
- Filters: date range, market, strategy version, regime (vol + trend), signal type, environment (paper / live-small / live-scale; never blended)
- Per-trade detail drawer: full lifecycle; decision diary; attribution; agent commentary; linked audit entries; stress-test impact
- Signal approval flow with reason capture for rejections
- Bulk approve "standard" — rule above
- Server-side pagination with cursor-based infinite scroll
- Server-side filter pushdown
- Expected scale: ~50–200 trades/month; 5-year accumulation 3k–12k
- **CSV export** of current filter

#### Performance (full target)
- Equity curve with benchmark overlay (SPY default; Phase 2 configurable)
- Drawdown chart (underwater plot)
- Monthly returns calendar heatmap
- Attribution by market, signal type, vol regime, trend regime — switchable views
- Rolling Sharpe, rolling DD, rolling hit rate (60-day default)
- Actual vs. rule-following P&L compare (dual curves; rolling 30-day divergence; alert at 5%)
- Tax estimate widget (click-expand for per-trade breakdown; election toggle gated by CPA acknowledgment)
- **Environment-segregation rule:** charts default to current environment; "Show all environments (segregated)" toggle renders separate stacked panels per environment with clear labeling; NEVER one curve combining environments; NEVER one number combining environments (except health score which is current-env-scoped by definition)
- PDF export: monthly/quarterly tearsheets via Typst (charts pre-rendered server-side as SVG); see §13
- **Print stylesheet:** print-optimized CSS for Performance views (operator wants to print)
- **CSV export** of monthly returns table

#### Research (full target — Phase 2)
- Backtest result loader from CLI-generated artifacts via backend API
- Equity curve, trade list, statistics for a backtest
- Parameter sandbox: propose change → drafts a PR via backend (backend holds GitHub App install token; frontend never touches GitHub)
- Regime analysis: filter strategy performance by regime conditions
- A/B comparison view (strategy version v3 vs. v4 on same dataset)
- Walk-forward visualizer: backend exposes per-window data (`train_start`, `train_end`, `test_start`, `test_end`, `train_metrics`, `test_metrics`); frontend renders as overlapping bars or strip chart

#### System (full target)
- Risk envelope: view current limits with cluster cap visualization; propose changes via PR-drafting workflow (re-auth required)
- Kill switch: status (NORMAL / HALT_NEW / CONVALESCENT), history, manual invoke (with confirmation modal; **NO re-auth** — invoke is risk-tightening), recovery flow (RESUME requires re-auth; web-only)
- Convalescent mode banner: when active, prominent on every page with sessions remaining, current effective vol target, exit countdown
- Vacation mode banner: when active, prominent with end date and end button
- Deployments log: every deploy with diff view and rollback button (re-auth required for rollback)
- Agent activity feed: drafted PRs, hot-fixes deployed, alerts raised, decisions made; expandable to show prompt + response
- **Operator-friendly PR review surface (full rendering spec):**
  1. Plain-English summary (≤200 words)
  2. Risk impact summary (auto-generated)
  3. Backtest delta (LEAN-authoritative; equity curve overlay, key stats delta, ten worst-divergence trades)
  4. Test results (unit + integration + linting + type-check)
  5. Files affected (list with one-line summary)
  6. Diff view (collapsed by default, expandable)
  7. In-app Approve / Reject / Request Changes buttons (sync to GitHub via backend)
  - On Reject: modal with tag picker + free text (min 10 chars); reason fed back to agent context
- Full audit explorer: cursor-paginated, server-side filter pushdown, virtualized list with infinite scroll; full-text search on `reason` field via Postgres FTS; hash-chain integrity badge per record; backfill-provenance indicator (visible gap markers with linked repair records); environment filter
- Reconciliation status: last reconciliation per source (TWS real-time / FlexQuery EOD), tolerance-band check results, any breaks, weekly summary
- **External watchdog status:** last-ping timestamp + status. Data path: watchdog → backend `/internal/watchdog` (push) → backend `system_state` table → frontend reads via `GET /api/system/status`. Stale threshold per table above.
- **Operating cost dashboard:** monthly run-rate per provider, total vs. envelope (soft $200 / hard $300), 90-day trend
- **Operator account management:** regenerate backup codes (re-auth required), revoke all sessions, manage TOTP enrollment

#### Calendar (full target)
- 30-day forward view of macro events (tier 1 / 2 / 3, color-coded with icons — never color alone)
- Tomorrow's events ratification: must be ratified by 23:00 ET nightly; if not, hard halt for next session until ratified; banner shows "Ratification required for [date]" with one-tap ratify (Phase 1 ratification is Discord-primary; web ratification ships Phase 2)
- Contract expiration / roll schedule (futures only; computed from `ROLL_DAYS_BEFORE_EXPIRY` parameter)
- Exchange holidays
- Manual event log (operator-added events; logged to audit)

#### Pre-auth Surfaces

##### `/login`
- WebAuthn login (full-navigation ceremony to api.<domain>; redirect back)
- TOTP fallback link (collapsed by default)
- Backup code link → /recover
- Browser unsupported explainer if no WebAuthn

##### `/setup` (first-run bootstrap)
- Token-protected route (`?token=...` from backend stdout at first boot)
- Wizard:
  1. Enroll WebAuthn passkey
  2. Enroll TOTP (QR code + manual entry)
  3. Generate 8 single-use backup codes; **force download/print acknowledgment** before continuing
  4. Confirm enrollment; redirect to /today

##### `/recover`
- Backup-code entry (single-use; 8 available at enrollment)
- Successful entry → reset WebAuthn enrollment + TOTP enrollment (forces re-setup); regenerate backup codes
- Failed: rate-limited; after 5 fails, lock 1h
- "All factors lost" path: shows escalation message with backend `dba_breakglass` procedure contact (operator's own documented runbook)

### 3. Six Locked Additional Features (each spec'd concretely; all Phase 2 except where noted)
- **Decision diary** (Phase 2 in Trades; Phase 1 minimum capture in Discord rejection flow): structured tag (`data_concern` / `regime_concern` / `size_concern` / `manual_judgment` / `other`) + free text on every signal override; required min 10 chars; queryable; surfaced in trade detail and Performance attribution
- **Actual vs. rule-following P&L compare:** dual equity curves on Performance; rolling 30-day divergence metric; alert threshold at 5% deviation
- **Strategy health score:** composite formula above with insufficient-data handling; persistent top-right; click-expand
- **Benchmark overlay:** SPY default; configurable to 60/40 SPY/AGG or custom symbol via dropdown
- **Tax estimate widget:** YTD liability, 1256 60/40 split, wash sale flagging; nightly backend cron; click-expand for per-trade breakdown; election toggle gated by CPA acknowledgment
- **Stress test:** single button on Today; **async execution** (POST → 202 + jobId, SSE progress events on `agent` channel, terminal payload); progress drawer with cancel; **modal shows ALL six scenarios in tabbed view** (1σ/2σ/3σ down day + 2008/2020/2022 replays) with summary table at top showing P&L impact across all six

### 4. Real-Time Update Mechanism
- Single multiplexed SSE channel `/api/sse/events` with event types listed above
- Per-page update strategy (which fields update via SSE event type, which via polling, which on manual refresh)
- **Polling fallback:** if SSE fails to connect after 3 retries (5s, 15s, 30s backoff), fall back to REST polling at intervals matching stale-data thresholds (5s P&L, 30s positions, 10s fills, etc.); UI shows "DEGRADED — polling mode" indicator; retry SSE every 60s
- Reconnection / resilience: exponential backoff with jitter; resume-from-last-event-id (server tracks per-session)
- Stale-data indicator vs. paused-state indicator (rules above)
- Multi-tab: own SSE per tab; oldest disconnects on browser cap; concurrent-tab race resolution rule above
- **Retry/backoff on 429:** exponential backoff with jitter; max 5 retries; banner "rate-limited, retrying" if persists >10s

### 5. Auth and Session Management
- WebAuthn registration flow (Mermaid sequence) — full navigation to api.<domain>, RP=parent
- TOTP backup flow
- 8 single-use backup codes generated at enrollment; printed by user; hashed in DB
- Session token model: opaque session ID in HttpOnly + Secure + SameSite=Strict cookie; server-side session row with `last_uv_at` for re-auth checks
- Re-auth (WebAuthn UV re-prompt within 5 min) required for risk-loosening actions only (full list above)
- RBAC: owner active; reader planned (full permission matrix above; reader sees decision diary for tax provenance, never sees PII account numbers)
- Account recovery via backup codes; if all factors lost → backend dba_breakglass procedure (escalation message + contact path)
- First-run bootstrap via /setup with token printed to backend stdout

### 6. Discord Bot Specification (CRITICAL — primary mobile surface)

#### Channels
For each (`#daily-brief`, `#signals`, `#fills`, `#alerts`, `#critical`, `#ops`, `#ask-agent`, `#audit`): purpose, message format (full embed schemas with field-by-field), who/what writes, how user interacts.

#### Slash Commands
For each: parameters, response format, permissions, confirmation modals.
- `/positions`, `/exposure`, `/pnl [today|wtd|mtd|ytd]`
- `/halt` (kill-switch INVOKE; confirmation modal; **resume NOT supported via Discord** — print explainer message directing to web)
- `/calendar`, `/last-fills [n]` (default 10, max 50)
- `/report [period]`, `/health` (current health score breakdown), `/ratify` (ratify tomorrow's calendar)
- `/ask <query>` (Claude agent chat)
- `/vacation start [days]`, `/vacation end`

#### Button Interactions
Signal approval/reject/defer:
- Payload format
- State machine (consistent with locked state enumeration above)
- Confirmation modals where required (any kill-switch action invoke; rejection requires decision diary modal)
- Decision diary capture on rejections (modal with tag picker + text field, min 10 chars)
- Bulk approve "standard" button on daily brief (rule above; enabled when ≥1 non-anomaly signal exists)

#### Threads
Per-trade thread for fill updates, agent commentary, operator notes

#### Backend → Bot IPC
Backend posts events to bot's local HTTP listener on Docker internal network. **Replay buffer overflow:** if bot has been disconnected from Discord >1h, on reconnect it fetches missed events from backend buffer (last 24h max). If gap > 24h, drop with notice "Discord catch-up incomplete, see web app." External watchdog independently covers VPS-down case.

#### Bot Architecture
- `discord.py` async event loop
- Connection to backend (REST + receives IPC events)
- Stateless preferred; fetches from backend
- Restart/recovery: idempotent re-subscription to events; replays missed messages from backend buffer (24h limit)

#### Web/Discord Action Parity
For every action surfaced in BOTH (signal approval, kill-switch INVOKE, decision diary entry, ratify, run stress test, query positions/P&L, vacation start/end), spec the shared backend endpoint. Document the **single explicit asymmetry: kill-switch RESUME is web-only** (WebAuthn UV cannot be performed via Discord; design choice for resume = strong-auth; invoking is friction-light).

### 7. Component Library Inventory
Beyond shadcn/ui defaults, spec custom components:
- Trade row (states from locked enumeration: pending / approved / rejected / deferred / expired / working / partially_filled / filled / executed / open_position / closed / stopped_out / capacity_constrained)
- Signal approval card with buttons
- Anomaly badge (icon + tooltip listing `anomaly_reasons`)
- Health score indicator (G/Y/R + expandable; insufficient-data graceful)
- Equity curve chart wrapper (with benchmark overlay support)
- Drawdown chart (underwater plot)
- Attribution treemap or bar
- Stress test result modal (tabbed, six scenarios, summary table)
- Stress test progress drawer (async with cancel)
- Decision diary entry form (tag picker + text, min-length validator)
- PR draft preview (plain-English + risk impact + backtest delta + diff view)
- PR rejection feedback modal (tag picker + min-10 free text)
- Kill-switch INVOKE button (confirmation, no re-auth)
- Kill-switch RESUME button (re-auth required; web-only)
- Audit log row with expansion + hash-chain integrity badge + backfill-provenance indicator
- Convalescent mode banner (sessions remaining + effective vol target + exit countdown)
- Vacation mode banner (end date + end button)
- Reconciliation status indicator
- Stale-data corner badge vs. paused-state pill (distinct UI)
- Environment tag pill (`paper` / `live-small` / `live-scale`)
- Strategy version badge (global) + per-trade version pill (smaller)
- External watchdog status indicator
- Operating cost dashboard tile
- Toast variants (P0/P1/P2 per taxonomy above)
- Empty-state components (full inventory above)
- Browser-unsupported explainer

For each: purpose, props, states, accessibility (keyboard nav, screen reader, never rely on color alone — pair with icon/text), tabular-num CSS application.

### 8. Data Fetching and State Strategy
- TanStack Query patterns (staleness, refetch policies per data type — match stale-data thresholds)
- Zustand store organization (narrow client state)
- Optimistic updates: signal approval, decision diary, ratification, vacation toggle
- Cache invalidation rules
- Error boundary placement
- Loading state strategy (skeleton vs. spinner — when each)
- All metrics computed backend-side (health score, attribution, tax, stress test, walk-forward); frontend renders only

### 9. Design Tokens
- Color palette (dark default; semantic tokens for P&L green/red, severity P0/P1/P2, regime indicators, environment pills, paused vs stale; all paired with icon/text for color-blind safety)
- Typography scale (monospaced for ALL numbers — JetBrains Mono or Inconsolata; sans for prose — Inter)
- `font-feature-settings: 'tnum'` applied to numeric tabular contexts
- Spacing scale (4px base; dense)
- Animation timing (≤150ms; functional only)
- Density mode (single — dense; not configurable)

### 10. Sequence Diagrams (Mermaid)
At minimum:
- WebAuthn first-run /setup (token-gated): registration with backup code generation
- WebAuthn login with re-auth challenge for risk-loosening action
- TOTP backup login flow
- Backup code recovery flow
- Signal arrives in queue → user approves via web → backend executes → fill displays via SSE
- Same signal flow but approved via Discord button (parity)
- User rejects signal with decision diary entry (web AND discord)
- User invokes kill switch from Discord (confirmation; no re-auth)
- User invokes kill switch from web (confirmation; no re-auth)
- User RESUMES from HALT_NEW via web (re-auth required; Discord cannot perform this — sequence shows /halt → resume-not-supported message)
- User manually closes a position during HALT_NEW (re-auth required for manual order action)
- Stress test button → POST 202 + jobId → SSE progress on `agent` channel → terminal payload
- PR draft from parameter sandbox → operator-friendly review surface render → human reviews → merges via backend → deploys
- PR rejection with feedback modal → reason fed to agent context
- Real-time fill update via SSE
- Vacation mode start (incl. ratification gate suspension), end
- VPS outage → external watchdog email → operator manual flow
- Concurrent-tab signal approval conflict → toast revert
- SSE failure → fallback to polling → degraded indicator → SSE retry success

### 11. Phased Build Plan
Aligned to operator's 6–12 month runway, in parallel with backend (Prompt A Phase 0 = weeks 0–7):

- **Phase 0 (frontend weeks 0–3):** scaffold (Next.js, auth + /setup + /login + /recover, basic Today page reading mock data initially); Discord bot skeleton with `/positions` and `/halt`; integrate with backend audit log starting week 3–4 once QC adapter is wired
- **Phase 1 (months 2–5):** ships before live trading begins per the per-page Phase 1 enumeration table in §2
- **Phase 2 (months 5–9):** fills out Phase 2 columns of §2 enumeration; six additional features; PR review surface; full Performance + Research + Calendar
- **Phase 3 (months 9–12):** investor PDF generation via Typst; CPA reader role plumbing; refinements

Each phase: deliverables, success criteria, kill criteria.

### 12. Testing Strategy
- Component tests (Vitest + React Testing Library) — coverage targets per component category
- E2E critical flows (Playwright): WebAuthn registration + login, signal approval (web + discord), kill-switch invoke (both surfaces) + resume (web only), decision diary entry, ratification, stress test async flow, PR review surface render, PR rejection feedback, vacation start/end, manual position close during HALT_NEW, concurrent-tab race
- Visual regression (Chromatic recommended) for design system consistency
- Accessibility audits (axe-core in CI) — WCAG 2.1 AA target
- Discord bot tests: command response correctness, button payload handling, IPC ingestion, replay buffer behavior
- **Cross-environment segregation tests:** assert no UI element ever blends `paper` and `live-*` data in a single number or chart (with explicit health-score current-env-scoping carve-out)

### 13. Investor PDF Report Layout (year-2 deliverable)
Renderer: **Typst** for layout/typography on the VPS; **charts pre-rendered server-side as SVG via headless Recharts** and embedded as images.

Layout:
- Cover page (period, fund/strategy name placeholder, prepared-by, date)
- Performance summary table (returns by period, comparison to benchmark)
- Equity curve and drawdown chart (SVG-embedded)
- Monthly returns table (calendar heatmap)
- Risk metrics (Sharpe, Sortino, max DD, hit rate, vol)
- Attribution summary (by market or strategy)
- Methodology disclosure (one paragraph)
- Risk disclosures (standard CTA-style language placeholder)
- Footer: page numbers, generation timestamp, hash of source data for audit

### 14. SLO / Performance Budgets

| Page | JS bundle (gzipped) | Targets |
|---|---|---|
| /today | ≤ 350KB initial | TTI ≤ 2s, LCP ≤ 2.5s, Lighthouse perf ≥ 90 |
| /trades | ≤ 500KB | (table-heavy, virtualized) |
| /performance | ≤ 600KB | (chart libs lazy-loaded into this page) |
| /research | ≤ 800KB | (Phase 2; heaviest) |
| /system | ≤ 500KB | |
| /calendar | ≤ 350KB | |

- Recharts and Lightweight Charts deferred to /performance and /research; never loaded for /today
- p99 SSE event-to-render: ≤ 500ms
- Aggressive code-splitting per route; dynamic imports for heavy components
- Bundle analyzer run in CI; fail PR if budget exceeded by >10%

### 15. Export Taxonomy
- **Trades CSV** — current filter; includes hash-chain footer for audit
- **Audit CSV** — current filter (chunked for large exports); includes hash-chain footer
- **Performance CSV** — monthly returns table
- **Tax annual export** — Form 6781, Schedule D, Form 8949 CSVs + PDF summary (annual; January)
- **PDF report** — Performance tearsheet (monthly/quarterly)
- **Print stylesheet** — Performance views and Trades filtered views (operator wants to print)

### 16. Observability
- Sentry for error tracking (free tier; upgrade only if usage warrants)
- Sentry Performance Monitoring for RUM at low-volume tier
- Frontend error boundary integration with Sentry
- User feedback via Sentry user-feedback widget on errors

## FORMAT REQUIREMENTS

- Markdown with clear section headers
- Mermaid for ALL diagrams
- Wireframes in text/ASCII/Mermaid (no image generation)
- Concrete library/tool/version recommendations
- Where genuine implementation choices remain, present 2–3 options with tradeoffs and a recommendation
- Length will be substantial; favor completeness over brevity
- Never invent strategic decisions; flag missing context with `[QUESTION FOR OPERATOR: ...]`
- For backend contract dependencies, flag with `[CONTRACT — verify against Prompt A output]` and proceed with expected contract
- This spec must interlock with Prompt A; reference its REST endpoints, SSE event types, and Discord command schemas by name where they exist

Begin.
