# PROMPT B — FRONTEND TECH SPEC

## ROLE

You are a senior frontend architect and design engineer with experience building production trading dashboards and operator interfaces for systematic CTAs and prop shops. You understand that trading UIs are utilitarian, dense, and fast — not consumer-app pretty. They resemble Bloomberg, Linear, or a small CTA's research environment, not a startup landing page.

You will produce a comprehensive technical specification for the FRONTEND of a single-operator algorithmic trading system. Implementation will be primarily by Claude Code working with a non-technical solo operator.

**The SPEC is comprehensive (full target shape); the BUILD is phased. Phase 1 ships a defined ~30% subset (enumerated per page in §2 and per Discord surface in §6); the rest follows in Phase 2 and 3. The phased build plans are binding; do not infer that everything ships in Phase 1.**

**Workflow note:** this prompt is intended to run AFTER Prompt A (backend spec) has produced its API contract. The implementer should paste Prompt A's `§4 API Contracts` section into this prompt as additional context where indicated. Where backend contract is genuinely missing, flag with `[CONTRACT — verify against Prompt A]` and propose your expected contract; do NOT invent from scratch where reasonable defaults are listed below.

## OPERATOR CONTEXT

- Solo operator, finance background, no coding ability, US-based (NJ), trades alone
- Moves around frequently — must operate from mobile (signal approval, monitoring, queries via Discord) and from desk (research, deep review, parameter changes via web)
- Responsible for own and (eventually) family money
- Wants the simplest possible interface that still surfaces everything when needed

## COMPANION BACKEND

This frontend integrates with a Python backend (specced in a parallel session — Prompt A) running:
- LEAN engine (QC cloud Phase 1, LEAN Local Phase 2)
- PostgreSQL 16 for transactional state
- DuckDB on Parquet for analytics
- `ib-async` to IBKR (Phase 2 only)
- Claude ops agent service
- FastAPI for API exposure
- Single Hetzner Cloud Ashburn VPS, Ubuntu LTS, Docker Compose
- Discord bot service + Discord webhook-pusher service (separate processes, internal Docker network)
- External watchdog (separate region)

**Surface parity (clarified):** signal approval, decision diary entry, query commands (positions/exposure/P&L), kill-switch INVOKE, vacation START — all available from BOTH web and Discord per phasing tables. Risk-loosening actions (RESUME, parameter PR submission, deploy approval, env tag override, backup code regen, tax election toggle, **vacation END**, manual close during HALT_NEW) are **WEB-ONLY** because they require WebAuthn UV which Discord cannot perform. Single principle: **risk-loosening = web-only by construction; risk-tightening or rule-defined-flow = both surfaces.** Calendar ratification is on Discord in Phase 1; web in Phase 2.

## ARCHITECTURE / TOPOLOGY (LOCKED — SELF-HOSTED)

```
Browser ──── HTTPS ──── <domain> (single VPS, Hetzner Ashburn)
                           │
                           ├── Caddy (reverse proxy, auto Let's Encrypt)
                           │     ├── /api/sse/events → FastAPI (long-lived SSE)
                           │     ├── /api/* → FastAPI
                           │     └── /* → Next.js Node server
                           ├── Next.js Node server (App Router; SSG shell + CSR data)
                           ├── FastAPI
                           ├── Postgres
                           ├── LEAN engine
                           ├── Claude ops agent
                           ├── Discord bot service
                           └── Discord webhook-pusher service

External watchdog (separate region — Hetzner Falkenstein)
   ├── pings <domain>/health every 5 min
   ├── pushes ping result to <domain>/internal/watchdog
   └── emails operator if unreachable >15 min during CME RTH
```

**Locked:**
- **Reverse proxy: Caddy** (auto-cert simpler than Traefik on single host; sufficient feature set)
- **Rendering: Next.js Node server** (App Router; SSG shell + client-side hydration + client-side data fetching). NOT static SPA + nginx — keeps SSR door open for future server-rendered report pages without rewriting.
- **Single origin:** bare `<domain>`. WebAuthn Relying Party = `<domain>`. NO `app.` / `api.` split. Same-origin cookies; `credentials: 'same-origin'` (default) suffices.
- **SSE endpoint canonical path: `GET /api/sse/events`** (Caddy routes this specifically as long-lived, with appropriate buffer/timeout settings). Other `/api/*` paths are routed normally.
- HSTS enabled; security headers per §17.

**SSE transport:**
- Use **`@microsoft/fetch-event-source`** library, NOT native `EventSource`
- Single multiplexed channel; client filters/dispatches by event `type`
- Server enforces N-connection limit per user (default N=4, per-user across devices); on connection N+1, server closes oldest with control event `{"type":"session_evicted","reason":"tab_limit"}`; browser displays banner. **No client-side cross-tab coordination needed.** Brief auth-only connections (e.g., `/login` from phone) don't usually evict — they auth then disconnect.
- **Web SSE replay buffer: 1h backend retention.** Beyond 1h gap, client falls back to full re-fetch of canonical state per page.

## TECH STACK (LOCKED)

- **Next.js 14+ App Router (Node server)** + **TypeScript** strict + **Tailwind CSS** + **shadcn/ui**
- **Caddy** reverse proxy + Let's Encrypt
- **TanStack Query** (server-state) + **Zustand** (client-state)
- **TanStack Table + `@tanstack/react-virtual`** for large tables
- **Chart library assignment (locked, per surface):**

| Surface | Library | Notes |
|---|---|---|
| /performance equity curve | **Lightweight Charts** (TradingView OSS) | Time-series-optimized; lazy-loaded |
| /performance drawdown | Recharts | Underwater plot |
| /performance attribution | Recharts | Bars / treemap |
| /performance monthly heatmap | Recharts | Calendar heatmap |
| /performance actual-vs-rule compare | Lightweight Charts | Dual time-series |
| /research backtest equity | Lightweight Charts | Same lib as live equity for visual continuity |
| /research walk-forward visualizer | Recharts | **Strip chart** (locked) |
| /research A/B compare | Lightweight Charts | |
| /today exposure rings | Recharts | Bars or radial |
| /today health-score expand | Recharts | Component bars |
| /system operating cost dashboard | Recharts | Provider tiles + 90-day trend |
| /system audit chain integrity | none / inline | Status badge |
| Per-trade detail (Phase 2) | Lightweight Charts | Price + entry/exit markers |
| **PDF exports** | Recharts SVG (server-side via headless renderer) | Acceptable trade-off: PDF and UI render the equity curve from different libs; visual style nearly identical; flag in spec |

- **`@microsoft/fetch-event-source`** for SSE
- **Sonner** for toasts
- **react-hook-form + zod** for forms
- **Auth:** WebAuthn (passkey) primary + TOTP backup + 8 single-use printed backup codes
- **Authorization (RBAC):** schema present from day 1; "owner" role active initially; "reader" role planned for CPA in year 2; investor role NEVER (PDF reports only)
- **Reader role permission matrix:**
  - Reader CAN view: Performance metrics in % of starting NAV (no absolute dollar amounts), Trades read-only with per-trade detail and decision diary for tax provenance, **tax artifacts and tax widget detail in absolute dollars** (locked exception — tax outputs are inherently dollar-denominated; redaction would defeat the CPA's job)
  - Reader CANNOT view: System (risk envelope, deployments, agent activity prompts/responses), Research, Calendar ratification controls, account numbers (PII redacted), strategy code/PR contents, agent prompts/responses, decision diary entries authored by agent (rationale leak)
  - Reader CANNOT do: any writes
- **PDF rendering:** Typst on VPS (layout/typography); charts pre-rendered SVG via headless Recharts; embedded as images. Async (POST → 202 + jobId + SSE progress on `agent` channel → terminal payload with signed download URL; 1h TTL; one-time use; download logged to audit).
- **Error tracking + RUM:** **Sentry** free tier for errors; Sentry Performance Monitoring upgraded only when 30-day event volume exceeds **100k events/month** (locked threshold).
- **Feature flagging:** env-var (`NEXT_PUBLIC_PHASE=1|2|3`); read at boot. NO PostHog/LaunchDarkly.
- **Browser support:** latest 2 stable Chrome, Firefox, Safari. Edge implicit (Chromium). WebAuthn detection with explainer.

For Discord:
- **`discord.py`** bot on VPS, separate from web; HTTP-IPC with backend over internal Docker network
- Slash commands + button interactions + embeds + threads (per Phase 2)

## DESIGN PHILOSOPHY (BINDING)

- Utilitarian, not aesthetic. Bloomberg/Linear/CTA-tool feel. Dense, fast, monospaced numbers, dark default.
- Animations: functional only (state transitions, modal/drawer); ≤150ms; no decorative.
- Mobile = Discord. NO native mobile build, ever.
- **Tablet/mobile policy:** below **1024px viewport**, render "use desktop or Discord" notice with Discord deep-link button.
  - **EXCEPTIONS — accessible at all viewport sizes:** `/login`, `/setup`, `/recover` (operator must be able to authenticate, bootstrap, or recover from any device — phone if laptop is dead).
  - All other pre- and post-auth pages enforce the 1024px block.
- Single density (dense). Single theme (dark). Not configurable.
- Numeric formatting: US locale; tabular figures (`font-feature-settings: 'tnum'`). Negatives: leading minus + red + small downward arrow icon (color-blind safe). Positives: green + upward arrow on emphasized values; bare otherwise.
- **Time-zone:** ALL UI in `America/New_York`. Backend stores UTC; presentation always ET.
- Numeric precision: read from backend's `instrument_metadata` (see Loading Model below) — never hardcoded.
- **`server_now` format (locked):** RFC 3339 UTC with `Z` suffix and millisecond precision (e.g., `2026-05-04T17:30:00.123Z`). Browser clock NEVER trusted for stale calculations.
- Empty-state visual language: muted text-only with single optional CTA button; no illustrations; austere; "No <noun> yet" + short explainer + optional CTA.

### `instrument_metadata` Loading Model (locked)

- Boot-time bulk fetch: `GET /api/metadata/instruments` returns full table (all active instruments with tick size, point value, decimals, multiplier)
- Cached in TanStack Query with **24h stale-while-revalidate**
- Instrument metadata changes are rare; updates picked up on next revalidation
- If boot fetch fails: render error banner + block trading-related actions (signal approval, manual close); read-only views still render with "—" for precision-sensitive fields

### Print Paper Size (locked)
**US Letter** (8.5" × 11"), portrait. Operator is US-based. Was incorrectly stated as A4 prior.

## INFORMATION ARCHITECTURE — 6 POST-AUTH + 3 PRE-AUTH SURFACES

**Post-auth ("6 pages"):**
1. **Today** (`/`) — landing dashboard
2. **Trades** (`/trades`, `/trades/:id`) — signal queue + positions + fills + journal + attribution
3. **Performance** (`/performance`) — equity, drawdown, attribution, tearsheet, PDF export
4. **Research** (`/research`, `/research/backtest/:id`) — backtest viewer, parameter sandbox, A/B
5. **System** (`/system`, `/system/audit/:id`, `/system/pr/:id`) — risk envelope, kill-switch, deployments, agent activity, audit, reconciliation, watchdog, costs, account
6. **Calendar** (`/calendar`)

**Pre-auth surfaces:**
- **`/login`** — WebAuthn login + TOTP fallback + backup-code link (mobile-accessible)
- **`/setup`** — first-run bootstrap with token (mobile-accessible)
- **`/recover`** — backup-code recovery (mobile-accessible)

**Auth callback:** N/A — WebAuthn does NOT use OAuth-style callback; see WebAuthn Ceremony below for actual flow.

NO additional post-auth pages. NO investor dashboards. NO mobile-optimized variants. NO "Agent" page.

**Deep-link conventions:**
- Discord-to-web: every Discord embed includes deep link to relevant detail (`/trades/:signal_uuid`, `/system/audit/:event_uuid`, `/system/pr/:pr_id`)
- Trade rows in Trades table → `/trades/:id` (drawer mode preferred; full-page on direct nav)

## LOCKED STRATEGIC AND SYSTEMS DECISIONS — DO NOT REOPEN

### Strategy and Phasing
- Multi-asset systematic trend-following on micro futures + bond ETFs
- Universe: ~8–12 markets
- Daily bars; signal generation 17:30 ET
- **Frontend Phase 0 (weeks 0–3, parallel with backend Phase 0 weeks 0–8):**
  - Weeks 0–2: scaffold Next.js + auth + /setup + /login + /recover; basic /today against MOCK DATA (typed mock fixtures matching expected backend schemas); other post-auth routes return 404 with explainer + Discord deep-link
  - Weeks 2–3: backend audit log integration begins (week 3–4); /today switches to live data; other routes unhide progressively as endpoints come online
- Phase 1 (months 2–5): per per-page enumeration (§2) and Discord enumeration (§6)
- Phase 2 (months 5–9): fills out Phase 2 columns; six additional features; full Performance + Research + Calendar
- Phase 3 (months 9–12): investor PDF generation; CPA reader role plumbing

### Risk Framework (numbers from Prompt A; frontend renders)
- Vol-targeted sizing, 14% portfolio annualized vol
- Per-position / gross / net trio: 25% / 300% / 150% of equity notional
- Cluster caps: equity-index 60%, commodity 80%, rates/bonds 80%, crypto 40%, FX 30%
- Realized cross-portfolio correlation: alert >0.7, halt >0.85
- Daily loss limit -5% of daily-start MTM (17:00 ET)
- Trailing DD -20% (capital-event reset)
- Monthly DD -10% triggers vol halving
- Decommission floor per Prompt A

### Vol-Target Multiplier Composition
MIN of multipliers (do NOT compound). Per Prompt A.

### Kill-Switch State Machine
- States: `NORMAL`, `HALT_NEW`, `CONVALESCENT`
- HALT_NEW severity flag: `routine` / `defensive_envelope` / `incident_review`. Frontend renders severity-specific banner text.
- HALT_NEW max dwell: 7 trading days → operator escalation
- IBKR margin-call residual risk possible at HALT_NEW (high used margin); alert language at HALT_NEW-due-to-margin must call this out
- Convalescent banner: state, sessions remaining, current effective vol target, exit countdown
- `incident_review` HALT_NEW banner: bright red, "Incident review required before resume"; resume button disabled until post-incident review write-up logged

### Audit & Track Record
- Append-only log; SHA-256 single-linked hash chain by insertion order; backfills append at tail with `repaired_for_sequence_no`; gaps visible
- Composite identity: `strategy_hash` + `parameter_set_hash` + `slippage_calibration_version`
- Track record portability: lineage metadata persisted; UI segregates environments via filters/tabs; **never visually splices `paper`/`live-small`/`live-scale` into one chart or one number** — except strategy health score (current-environment-scoped)
- Environment tags: `paper` / `live-small` (real money, equity < $50k) / `live-scale` (real money, equity ≥ $50k); immutable per trade

### Tax
- Futures (1256): automatic 60/40
- ETFs: capital gains/losses with wash sale tracking; no 475(f) by default
- Tax estimate widget: YTD liability, 1256 60/40, wash sales; nightly update
- **Tax election toggle (475(f) for ETFs):** CPA-acknowledgment-gated (in-app modal where operator types verbatim "I have consulted a CPA regarding 475(f) election"); session-credentialed; logged to audit with full text + session ID + timestamp. NO file upload, NO email confirmation.

### Six Stress Test Scenarios (LOCKED)

| Scenario | Definition |
|---|---|
| `1σ_down` | Single-day return = -1 × 60-day rolling realized portfolio σ, applied to current book |
| `2σ_down` | Single-day return = -2σ |
| `3σ_down` | Single-day return = -3σ |
| `gfc_2008` | Sep 1, 2008 – Dec 31, 2008 daily returns replayed against current book (equity-index components especially) |
| `covid_2020` | Mar 1, 2020 – Mar 31, 2020 daily returns replayed |
| `crossasset_2022` | Jan 1, 2022 – Dec 31, 2022 daily returns replayed (cross-asset stress: rates + commodities + crypto all moved hard) |

Backend computes; frontend renders summary table (columns locked: scenario name, total P&L impact ($), max position-level loss ($), DD impact (%), worst-hit market) + per-scenario tab with detail.

### Decision Diary Tag Vocabulary (LOCKED)

`data_concern` | `regime_concern` | `size_concern` | `manual_judgment` | `other`

### Trade State Enumeration (LOCKED — gaps fixed)

```
pending → approved | rejected | deferred | expired
deferred → pending (next session)
approved → working | expired (if approval window lapses)
working → partially_filled | filled | cancelled
partially_filled → working (continues filling) | filled (remaining fills) | cancelled (manual cancel of remaining)
filled = executed → open_position
partially_filled with cap binding → capacity_constrained → open_position
open_position → closed (manual or profit-target) | stopped_out (stop hit)
Terminal: closed, stopped_out, rejected, expired, cancelled
```

Frontend renders state via badge with consistent color/icon.

### Claude Ops Agent Authority Matrix (frontend renders)
| Category | Authority |
|---|---|
| Tighten risk via parameter change (next cycle) | AUTO + notify |
| Tighten risk via defensive position trim (mid-session) | AUTO + notify (causally agent-initiated, mechanically risk-engine-placed) |
| Loosen risk | HUMAN APPROVAL |
| Hot-fix infra (within whitelist per Prompt A) | AUTO-DEPLOY + auto-rollback if degraded |
| Strategy logic changes | DRAFTS PR |
| Place orders directly (primary action) | NEVER |
| Invoke kill switch | AUTO on threshold |
| Un-invoke kill switch | HUMAN ONLY (re-auth, web-only) |
| Strategy params within pre-approved range AND tighten direction (per Prompt A) | AUTO + auto-revert |
| Reports/alerts/briefings/diagnostics | AUTO |

### Performance Targets
- Phase 1: backtest Sharpe ≥ 1.5, live ≥ 0.8, max DD ≤ 15%, signal acceptance ≥ 90%
- Phase 2: portfolio live ≥ 1.2
- Phase 3: portfolio live ≥ 1.5

### Severity Model
- **P0:** kill-switch fired, broker disconnect, reconciliation tolerance breach, margin auto-trim, audit-log write failure, Defensive Risk Envelope, incident_review HALT_NEW. Critical channel + email backup.
- **P1:** slippage drift, model decay, capacity warning, anomalous signal flagged, vol regime transition. Warn channel.
- **P2:** informational (fills, daily summary, agent reports, ratification reminders, daily liveness probe). Routine.

### Anomaly-Flagged Signals — Reason Code Vocabulary (LOCKED)

| Code | Meaning |
|---|---|
| `vol_regime_z_high` | Vol regime z-score > 1.5 |
| `capacity_above_alert` | Capacity at 0.5%–2% ADV |
| `recent_decision_diary_concern` | Decision diary concern within 14 days same market |
| `slippage_outlier_recent` | Backtest expected slippage exceeded by > 2× in last 5 trades same market |
| `version_baseline_divergence` | Strategy-version-vs-baseline divergence in last week's golden test |

Backend emits `anomaly_reasons: AnomalyReasonCode[]`. Frontend maps to human-readable tooltip text.

**Phase 1 anomaly handling (locked):** backend emits `anomaly_reasons` from day one. Frontend stores and includes in CSV exports. **Anomaly badge on signal cards is Phase 2 (web).** Phase 1 web operator approves without anomaly context in UI; **Discord `#signals` embed in Phase 1 DOES include anomaly reason text** (operator gets context via Discord while approving from desk-or-mobile). Conscious trade-off: minimizes Phase 1 web complexity while preserving operator awareness via Discord.

### Stale-Data Thresholds (locked, per data type)

| Data Type | Stale during CME RTH | Stale outside CME RTH |
|---|---|---|
| P&L (live) | 5s | 60s |
| Positions | 30s | 5min |
| Open orders | 10s | 60s |
| Recent fills | 10s | 60s |
| Health score | 60s | 5min |
| Calendar | 24h | 24h |
| Backtest results | never (immutable) | — |
| Audit log entries (trailing edge) | 5min | 30min |
| Watchdog last-ping | 10min | 30min |

Stale indicator: yellow corner badge + tooltip with last-update timestamp.

PAUSED vs. STALE: when state is HALT_NEW, CONVALESCENT, or vacation, intentionally-paused flows display "PAUSED — last activity at X" pill instead of stale badge.

### Re-Auth Requirements (LOCKED — single principle, no Discord bypass)

**Principle:** WebAuthn UV re-prompt within last 5 minutes is required for **(a) risk-loosening actions**, OR **(b) direct manual order actions while system is in a halt state**. Such actions are **WEB-ONLY by construction** — Discord cannot perform WebAuthn UV, so any action requiring re-auth cannot be initiated from Discord.

**Web-only (re-auth required):**
- Kill-switch RESUME (un-invoke)
- Parameter range change PR submission
- Deploy approval (any)
- Environment tag override
- Backup code regeneration
- Tax election toggle
- **Vacation END** (risk-loosening — re-enables strategy entries; web-only, no Discord bypass)
- Manual position close during HALT_NEW

**Available from both surfaces (no re-auth):**
- Kill-switch INVOKE — risk-tightening
- Defensive trim invocation — risk-tightening
- Signal approval / reject in NORMAL state — rule-defined flow
- Decision diary entry — supporting metadata
- Calendar ratification (Phase 1: Discord; Phase 2: both)
- Stress test run — read-only
- Vacation START — risk-reducing

UV freshness: server-side `last_uv_at` per session row; checked on sensitive endpoints.

### CSRF Strategy
SameSite=Strict cookies + double-submit cookie pattern: backend issues `csrf_token` cookie at session start; frontend reads via JS (not HttpOnly for this token) and sends as `X-CSRF-Token` header on POST/PUT/DELETE; backend validates header equals cookie value.

### Session Lifetime
- Idle timeout: 30 min sliding
- Absolute max: 24h from login
- Refresh token: 7 days (within absolute max)
- Cookie max-age = absolute max
- After absolute max: full re-login

### TOTP-Only Bootstrap — Reduced Privileges (locked)
If WebAuthn unavailable at `/setup`, TOTP-only enrollment allowed but session has `auth_strength: weak`:
- Cannot perform any re-auth-required action (full list above)
- Effectively read-only; can view but cannot mutate
- Operator forced to add WebAuthn on first compatible browser to unlock full privileges
- Session badge shows "Reduced — add WebAuthn"

### Strategy Health Score (locked formula per Prompt A)

Scope: current environment only. Insufficient-data graceful degradation. Click-expand uses cached payload (server returns components in same payload as composite).

### Concurrent-Tab / SSE Eviction
- Server enforces N=4 connections per user (across all devices/browsers)
- Phone `/login` connects briefly then disconnects after auth — typically doesn't evict desktop tabs
- On connection N+1, server closes oldest with `{type:"session_evicted",reason:"tab_limit"}`
- Phase 1: server-driven eviction only
- Phase 2 enhancement: BroadcastChannel for cross-tab optimistic-update reconciliation

### Optimistic Update Failure UX
1. Apply optimistic state immediately
2. Send to backend
3. On 5xx or network failure: queue locally, retry up to 3× with exponential backoff (1s, 4s, 16s)
4. After 3 failures: toast with manual "Retry" + "Cancel"; revert until user acts
5. On contradicting SSE event during retry: revert + toast

### Bulk-Approve "Standard"
Approves all signals NOT `anomaly_flagged`. Disabled when zero non-anomaly signals exist. Phase 2 only (per Discord and web phasing tables).

### Toast/Alert Taxonomy
- **P0:** persistent until manually dismissed; subtle single-chime; top-center; full-width banner; ARIA `role="alert"`, `aria-live="assertive"`
- **P1:** 8s auto-dismiss; top-right; ARIA `role="status"`, `aria-live="polite"`
- **P2:** 4s auto-dismiss; top-right; same ARIA as P1
- Stack cap 5 visible; older collapse to "+N more"

### Live Region / Accessibility for SSE Updates
- New signal in queue → `aria-live="polite"` announces "New signal: {market}, {direction}"
- Fill arrives → polite announces "Fill: {market} {qty} @ {price}"
- P0 alert fires → `role="alert"` announces full alert text
- Health score / position updates → no announcement (visual only)

### SSE Event Ordering
- Each event: `{ type, sequence_no, server_now, data }`
- **`sequence_no` scope: GLOBAL monotonic across the multiplexed channel** (single sequence space; client tracks one number)
- Client tracks last-received `sequence_no`; on gap, requests replay from `last-event-id` header
- Out-of-order events buffered up to 5s; older arrivals applied in order
- **Backend SSE replay buffer: 1h.** Beyond 1h gap → client falls back to full re-fetch of canonical state per page

### Empty-State Inventory
Pattern: muted heading + short explainer + optional CTA.
- No trades yet (Trades, Performance, Today)
- No backtests yet (Research)
- No alerts (alerts panel)
- No agent activity (System / agent feed)
- No reconciliation history (System)
- No calendar events for the day (Calendar)
- Insufficient health-score data (Today)
- No stress test scenarios run yet
- Pre-deployment state (Phase 0 weeks 0–2 when backend not connected)
- Hidden Phase 0 routes (404 with explainer + Discord deep-link)
- **`/performance` with zero trades:** austere empty state ("No trades yet — equity curve will appear after first fill"); NOT a flat NAV line (misleading; implies a real trace)

### Strategy Version Badge
- Global badge (top bar): currently DEPLOYED version (short hash + click for full info)
- Updates via SSE on `agent` deploy events
- Per-trade pill (Trades rows + detail drawer): version active at signal emit time

### Vacation Mode
- Server-enforced (backend refuses new entries; calendar ratification suspended)
- Frontend reads vacation state via SSE; persistent banner with end date and **"End vacation"** button (re-auth required; web-only — Discord cannot end vacation in this revised design)
- Pending working orders cancelled at vacation start
- Existing positions exit normally (stops, profit-targets, manual close)

### PR Rejection Feedback Loop
Modal: tag picker (`logic_disagreement` | `risk_concern` | `unclear_rationale` | `bad_test_coverage` | `other`) + free text, min 10 chars. Logged to audit + fed to agent context. PR closed via backend GitHub App.

### Agent Status Indicator States (locked)
- `idle` — agent ready
- `working` — agent processing
- `degraded` — Claude API rate-limited or partial outage; read-only mode
- `disabled` — vacation mode or operator-toggled off
- `errored` — unrecoverable error in last hour; auto-recovers; alert raised

### CSV Export Schemas (locked column lists)

Per Prompt A canonical vocabulary. See §15.

### Operating Cost Dashboard (locked data source)

`GET /api/system/costs?days=N` returns provider tiles. Updated nightly by backend cron (provider billing APIs). NO SSE for cost events (slow-update; refresh on page load + manual refresh button). Tiles per Prompt A's Operating Cost Envelope: Hetzner VPS primary, Hetzner watchdog, QuantConnect, Polygon (or $0), Anthropic, S3/B2, Sentry, Email, Domain, IBKR data, GitHub. Aggregate: total + delta vs. soft ($200) and hard ($300) ceilings; 90-day trend chart.

## YOUR DELIVERABLE

Produce a complete, production-grade frontend technical specification covering ALL sections below. Use Mermaid for diagrams. Wireframes in TEXT/ASCII/Mermaid (no image generation). Be specific and concrete.

**Backend API contract:** parallel backend spec (Prompt A) produces canonical REST/SSE/Discord schema. Reference by name where Prompt A's output is supplied; flag with `[CONTRACT — verify against Prompt A]` and propose expected contract using canonical vocabulary defined above. Specifically:

- One SSE endpoint: `GET /api/sse/events` (delivered via `@microsoft/fetch-event-source`)
- Event types: `signal`, `fill`, `position`, `pnl`, `risk_state`, `health`, `alert`, `audit`, `agent`, `vacation`, `watchdog`, `session_evicted`
- Each event: `{ type, sequence_no, server_now, data }` where `sequence_no` is global monotonic and `server_now` is RFC 3339 UTC ms-precision
- Resume on reconnect via `last-event-id` header

### 1. Information Architecture
- Full IA tree (page → sections → components → states)
- Pre-auth surfaces (`/login`, `/setup`, `/recover`) — all mobile-accessible
- Top nav recommended; defend if differing
- Command palette (cmd-k): pages + corpus = trades by ID/symbol, signals by ID, audit by ID/text
- Keyboard shortcuts: `?` opens cheat-sheet modal; full list documented
- Persistent UI elements (top bar): strategy version badge, health score, current portfolio P&L, agent status indicator, environment tag, current state (NORMAL/HALT_NEW/CONVALESCENT/VACATION) banner if not NORMAL
- Deep-link conventions

### 2. Screen-by-Screen Specification

For each of 6 post-auth pages: layout, component hierarchy, data displayed (with backend source — endpoint or SSE event type; reference Prompt A by name; flag `[CONTRACT]` where unconfirmed), all states (empty/loading/error/partial-data/stale-data/paused), interactions, real-time update behavior, filter/sort/search controls, accessibility (ARIA live regions where applicable).

**Phase 1 surface enumeration per page (binding):**

| Page | Phase 1 ships | Phase 2 adds |
|---|---|---|
| Today | Health score (insufficient-data graceful), positions table, P&L summary D/W/M/Y, exposure breakdown visualized against ring + cluster limits, queued signals (individual approve/reject WITH decision diary modal on rejection; NO bulk-approve, NO anomaly badge in web — Discord embed has reason text), recent fills feed, P0/P1 alerts, paused-state distinction | Stress test button (six scenarios), anomalies quick-link list, P2 alerts integration, bulk-approve "standard", anomaly badges in web |
| Trades | Filterable summary table (date/market filters); CSV export | Per-trade detail drawer/page, decision-diary view in Trades, attribution view, all filters, advanced search |
| Performance | Equity curve (no benchmark overlay yet), monthly returns table; CSV export | Drawdown underwater, attribution by market/signal/regime, actual-vs-rule compare, tax estimate widget, PDF export, benchmark overlay, print stylesheet, environment-segregation toggle |
| Research | (not in Phase 1) | Backtest viewer, parameter sandbox, regime analysis, A/B compare, walk-forward visualizer (strip chart) |
| System | Kill-switch UI + state, **read-only Risk Envelope tile** (locked numeric limits visible), audit log basic table (cursor-paginated; filter = date + event type + environment), reconciliation status (Phase 1 source: QC brokerage state via QC API + audit ingestion; Phase 2: TWS + FlexQuery), watchdog status, **minimal Account section: regenerate backup codes (re-auth)** | Risk envelope + propose-PR, deployments log + rollback, agent activity feed, full audit explorer with FTS + actor + hash-validity + repaired-events filters, operator-friendly PR review surface, convalescent banner, operating cost dashboard, full operator account management |
| Calendar | Read-only event list (next 30 days) | Tomorrow's ratification flow on web (Phase 1 ratification is Discord-only), holidays, contract expiration / roll schedule, manual event log |

#### Today (full target)
- Health score (G/Y/R) prominent + click-expand using cached payload
- Current positions table (compact, monospace, virtualized if >50 rows)
- P&L summary (D/W/M/Y) with benchmark comparison
- Exposure breakdown (gross / net / per-market / per-cluster) visualized against ring + cluster limits
- Queued signals — quick approve/reject inline; rejection opens decision diary modal (Phase 1)
- Recent fills feed (live via SSE event type `fill`; ARIA-announced)
- Active alerts (P0 → P1 → P2 sorted)
- Stress test "run now" button (Phase 2; async; six scenarios; modal with tabbed view + summary table per locked columns)
- Quick links to anomalies (Phase 2)

#### Trades (full target)
- Unified table (TanStack Table + `@tanstack/react-virtual`)
- Filters: date range, market, strategy version, regime, signal type, environment (never blended)
- Per-trade detail drawer (Phase 2): full lifecycle, decision diary, attribution, agent commentary, linked audit entries, stress-test impact
- Server-side pagination + filter pushdown
- Expected scale: ~50–200 trades/month; 5-year ~3k–12k
- CSV export per locked schema

#### Performance (full target)
- Equity curve with benchmark overlay (SPY default; configurable Phase 2) — Lightweight Charts
- Drawdown chart (underwater plot) — Recharts
- Monthly returns calendar heatmap — Recharts
- Attribution by market, signal type, vol regime, trend regime — Recharts
- Rolling Sharpe, rolling DD, rolling hit rate (60-day default)
- Actual vs. rule-following P&L compare (dual curves; rolling 30-day divergence; alert at 5%) — Lightweight Charts
- Tax estimate widget (click-expand; election toggle CPA-acknowledgment-gated; reader sees full dollar detail)
- Environment-segregation rule: charts default to current environment; "Show all environments (segregated)" toggle renders separate stacked panels per environment; never blended
- PDF export (async; Typst + Recharts SVG)
- Print stylesheet (US Letter portrait): page-break-inside: avoid; header includes period + prepared-by; footer includes generation timestamp. **Trigger: explicit "Prepare for print" button** (not auto on filter change).
- CSV export per locked schema

#### Research (Phase 2)
- Backtest result loader from CLI-generated artifacts
- Equity curve, trade list, statistics
- Parameter sandbox: propose change → drafts PR via backend; ranges from Prompt A's Parameter Ranges Table; in-range / out-of-range visual indicator
- Regime analysis
- A/B comparison
- Walk-forward visualizer: **strip chart** (Recharts) with rolling windows (`train_start`, `train_end`, `test_start`, `test_end`, train/test metrics)

#### System (full target)
- **Risk Envelope tile (Phase 1 read-only; Phase 2 add propose-PR):** displays all numeric limits from Prompt A's Risk Rings table + Cluster Caps + Parameter Ranges. Phase 1 ships read-only — operator can SEE the limits centrally without yet being able to propose changes
- Kill switch: status, history, manual invoke (confirmation modal; NO re-auth — invoke is risk-tightening), recovery flow (RESUME requires re-auth, web-only); incident_review HALT_NEW: red banner + resume disabled until post-incident review write-up
- Convalescent mode banner (sessions remaining + effective vol target + countdown)
- Vacation mode banner (end date + end button; web-only end with re-auth)
- Deployments log (Phase 2): every deploy with diff view + rollback (re-auth)
- Agent activity feed (Phase 2): drafted PRs, hot-fixes, alerts, decisions; expandable to show prompt + response
- **Operator-friendly PR review surface (Phase 2):**
  1. Plain-English summary (≤200 words)
  2. Risk impact summary (auto-generated)
  3. Backtest delta (LEAN-authoritative)
  4. Test results
  5. Files affected
  6. Diff (collapsed)
  7. In-app Approve / Reject / Request Changes (sync to GitHub via backend)
  - On Reject: feedback modal
- Audit explorer:
  - **Phase 1:** cursor-paginated table; filters = date + event type + environment
  - **Phase 2:** add FTS on `reason` (Postgres FTS), actor filter, hash-validity filter, repaired-events filter; virtualized infinite scroll; hash-chain integrity badge; backfill-provenance indicator
- Reconciliation status:
  - **Phase 1:** source = QC brokerage state (via QC API + audit ingestion); same tolerance bands; produces `reconciliation_breaks` records
  - **Phase 2:** source = TWS API real-time + FlexQuery EOD per Prompt A
- External watchdog status (Phase 1): last-ping; data path watchdog → backend `/internal/watchdog` → `system_state` → frontend reads `GET /api/system/status`
- Operating cost dashboard (Phase 2): provider tiles per locked list
- Account management:
  - **Phase 1 minimal:** regenerate backup codes (re-auth required)
  - **Phase 2 full:** revoke all sessions, manage TOTP enrollment, view auth audit log

#### Calendar (full target)
- 30-day forward view (tier 1/2/3, color + icon — never color alone)
- Tomorrow's ratification: must be ratified by 23:00 ET; if not, hard halt next session until ratified; banner shows requirement (Phase 1: Discord-primary; Phase 2: web-primary)
- Contract expiration / roll schedule (futures only)
- Exchange holidays
- Manual event log (operator-added; logged to audit)

#### Pre-auth Surfaces

##### `/login`
- WebAuthn login (see Ceremony below)
- TOTP fallback (collapsed by default)
- Backup code link → `/recover`
- Browser unsupported explainer
- Mobile-accessible (1024px block exempted)

##### `/setup` (first-run bootstrap)
- Token-protected (`?token=...` from backend stdout at first boot)
- Wizard:
  1. Enroll WebAuthn passkey (or TOTP-only with prominent warning if WebAuthn unsupported; reduced session privileges per locked rule)
  2. Enroll TOTP (QR + manual entry)
  3. Generate 8 single-use backup codes; force download/print acknowledgment
  4. Confirm; redirect to `/`
- Mobile-accessible

##### `/recover`
- Backup-code entry (single-use; 8 at enrollment)
- Successful → reset WebAuthn + TOTP enrollment; regenerate backup codes
- Failed: rate-limited; lock 1h after 5 fails
- "All factors lost" path: escalation message with `dba_breakglass` procedure contact
- Mobile-accessible

#### WebAuthn Ceremony (corrected — NOT OAuth-style)

WebAuthn is a JS-driven `navigator.credentials.*` flow against backend endpoints. NO OAuth-style redirect with `state`.

**Login flow:**
1. User clicks "Sign in with WebAuthn" on `/login`
2. Frontend captures intended `targetUrl` (default `/`)
3. Frontend POSTs `{ targetUrl }` to `/api/auth/webauthn/challenge`
4. Backend generates challenge; stores `(challenge, targetUrl)` in server-side session row keyed by transient ceremony ID; returns `{ ceremonyId, challengeBase64, allowedCredentials }`
5. Frontend calls `navigator.credentials.get({ publicKey: { challenge, allowCredentials } })`
6. Browser prompts user for passkey (Touch ID, security key, etc.)
7. Frontend POSTs assertion to `/api/auth/webauthn/verify` with `{ ceremonyId, assertion }`
8. Backend verifies assertion; sets session cookie; returns `{ targetUrl }` from server-side session
9. Frontend client-side `router.push(targetUrl)`

**Registration flow:** analogous with `navigator.credentials.create()` and `/api/auth/webauthn/register/*`.

There is NO `/auth/callback` page (was incorrect in earlier draft). All ceremony endpoints are JSON APIs called via fetch; navigation happens client-side after success.

### 3. Six Locked Additional Features (each spec'd)
- **Decision diary** (Phase 1: rejection-flow modal in /today AND in Discord; Phase 2: Trades queryable surface). Tag vocabulary per locked enum. Min 10 chars.
- **Actual vs. rule-following P&L compare:** dual equity curves; rolling 30-day divergence; alert at 5%
- **Strategy health score:** locked formula
- **Benchmark overlay:** SPY default; configurable Phase 2
- **Tax estimate widget:** YTD, 1256 60/40, wash sale flagging; nightly cron; click-expand. CPA-acknowledgment-gated election toggle (verbatim text capture).
- **Stress test:** async on Today; six scenarios per locked table; modal with tabbed view + summary table (columns: scenario, P&L impact $, max position-level loss $, DD %, worst-hit market)

### 4. Real-Time Update Mechanism
- Single multiplexed `/api/sse/events` via `@microsoft/fetch-event-source`
- Per-page update strategy
- Polling fallback: SSE fails after 3 retries (5s, 15s, 30s) → REST polling at intervals matching stale-data thresholds; "DEGRADED — polling mode" indicator; retry SSE every 60s
- Reconnection: exponential backoff with jitter; resume via `last-event-id` header (server tracks per session; 1h replay buffer; beyond 1h → full re-fetch)
- Stale-data vs. paused-state indicators
- Multi-tab: server-side eviction; N=4 per user across devices
- Retry/backoff on 429: exponential with jitter; max 5 retries; banner if persists >10s

### 5. Auth and Session Management
- WebAuthn ceremony (Mermaid sequence) per spec above
- TOTP backup flow
- 8 single-use backup codes (printed by user; hashed in DB)
- TOTP-only bootstrap (locked reduced-privileges rule)
- Session: opaque session ID in HttpOnly + Secure + SameSite=Strict cookie; CSRF token in non-HttpOnly cookie; double-submit pattern; server-side session row with `last_uv_at`
- Lifetime: 30 min idle / 24h absolute / 7d refresh
- Re-auth (WebAuthn UV within 5 min) per principle (web-only by construction)
- RBAC: owner active; reader planned (matrix above; redaction rules locked; **tax artifacts bypass dollar-redaction**)
- Account recovery via backup codes; all-factors-lost → `dba_breakglass`

### 6. Discord Bot Specification

#### Surface Phasing (binding)

| Surface | Phase 0 | Phase 1 | Phase 2 |
|---|---|---|---|
| `/positions` | ✓ | full | refinements |
| `/halt` (kill-switch INVOKE; resume not supported via Discord — explainer message) | ✓ | full | — |
| `/pnl [today\|wtd\|mtd\|ytd]` | — | ✓ | — |
| `/exposure` | — | ✓ | — |
| `/calendar` | — | ✓ | — |
| `/last-fills [n]` (default 10, max 50) | — | ✓ | — |
| `/ratify` (ratify tomorrow's calendar) | — | ✓ | — |
| `/health` (current health score breakdown) | — | ✓ | — |
| `/vacation start [days]` | — | ✓ | — |
| `/vacation end` | — | **NOT supported via Discord** (web-only per re-auth principle) | — |
| `/report [period]` | — | — | ✓ |
| `/ask <query>` (Claude agent chat) | — | — | ✓ |
| Channels: `#daily-brief`, `#signals`, `#fills`, `#alerts`, `#critical`, `#ops`, `#audit` | — | ✓ | — |
| Channel: `#ask-agent` | — | — | ✓ |
| Signal approve/reject/defer buttons | — | ✓ (rejection requires decision diary modal) | — |
| `#signals` embed includes `anomaly_reasons` text | — | ✓ | — |
| Bulk approve "standard" button on daily brief | — | — | ✓ |
| Per-trade threads | — | — | ✓ |
| P0/P1 alert delivery | — | ✓ | — |
| P2 alert delivery | — | — | ✓ |
| Replay buffer (24h) on reconnect | — | ✓ | — |

#### Channels (Phase 1+ unless noted)
For each: purpose, message format (full embed schemas with field-by-field), who/what writes, how user interacts.

`#signals` Phase 1 embed: market, direction, intended size, **anomaly_reasons text** (if any), approve/reject/defer buttons, deep link to `/trades/:signal_uuid`.

#### Slash Commands
For each per phasing table: parameters, response format, permissions, confirmation modals.

#### Button Interactions (Phase 1+)
Signal approve/reject/defer: payload format, state machine consistent with locked enumeration, confirmation modals on kill-switch invoke, decision diary capture on rejections (tag picker + min 10 chars). Bulk approve "standard" Phase 2.

#### Threads (Phase 2)
Per-trade thread for fill updates, agent commentary, operator notes.

#### Backend → Bot IPC
Backend posts events to bot's local HTTP listener on Docker internal network. Replay buffer: bot disconnect >1h → on reconnect fetches missed events from backend buffer (24h max). Gap >24h → drop with notice. External watchdog independently covers VPS-down.

#### Bot Architecture
- `discord.py` async event loop
- Connection to backend (REST + receives IPC events)
- Stateless preferred; fetches from backend
- Restart/recovery: idempotent re-subscription; 24h replay

#### Web/Discord Action Parity (revised principle)
**Risk-tightening or rule-defined-flow actions are available from BOTH surfaces.** Risk-loosening actions are **WEB-ONLY by construction** because they require WebAuthn UV. Single principle, multiple actions web-only — not a "single asymmetry" anymore. The web-only set: kill-switch RESUME, parameter PR submission, deploy approval, env tag override, backup code regen, tax election toggle, vacation END, manual close during HALT_NEW.

### 7. Component Library Inventory
Beyond shadcn/ui defaults, spec custom components:
- Trade row (states from locked enumeration including `cancelled`)
- Signal approval card with buttons
- Anomaly badge (Phase 2 web; tooltip listing `anomaly_reasons`)
- Health score indicator (G/Y/R + expandable; insufficient-data graceful)
- Equity curve chart wrapper (Lightweight Charts; benchmark overlay support)
- Drawdown chart (Recharts; underwater plot)
- Attribution treemap or bar (Recharts)
- Stress test result modal (tabbed, six scenarios per locked list, summary table per locked columns)
- Stress test progress drawer (async with cancel)
- Decision diary entry form (tag picker per locked vocab + text, min-length validator)
- PR draft preview
- PR rejection feedback modal
- Kill-switch INVOKE button (confirmation, no re-auth)
- Kill-switch RESUME button (re-auth required; web-only)
- Audit log row with expansion + hash-chain integrity badge + backfill-provenance indicator
- Convalescent mode banner (severity-aware; `incident_review` red variant)
- Vacation mode banner (end date + web-only end button)
- Reconciliation status indicator (Phase 1 QC source / Phase 2 TWS+FlexQuery source)
- Risk envelope read-only tile (Phase 1) / risk envelope propose-PR (Phase 2)
- Stale-data corner badge vs. paused-state pill
- Environment tag pill (`paper` / `live-small` / `live-scale`)
- Strategy version badge (global, SSE-updated) + per-trade version pill
- External watchdog status indicator
- Operating cost dashboard tile (Phase 2)
- Toast variants (P0/P1/P2 per taxonomy with ARIA)
- Empty-state components (full inventory above)
- Browser-unsupported explainer
- Agent status indicator (state enum locked)
- ARIA live region wrapper
- "Prepare for print" button (Performance, Trades filtered)
- CPA acknowledgment modal (verbatim text capture)
- TOTP-only weak-session badge ("Reduced — add WebAuthn")

For each: purpose, props, states, accessibility (keyboard nav, screen reader, never rely on color alone), tabular-num CSS application.

### 8. Data Fetching and State Strategy
- TanStack Query patterns (staleness, refetch policies per stale-data thresholds)
- `instrument_metadata` boot-time bulk fetch with 24h SWR caching
- Zustand store organization (narrow client state)
- Optimistic updates per failure UX rule
- Cache invalidation rules
- Error boundary placement
- Loading state strategy (skeleton vs. spinner — when each)
- All metrics computed backend-side; frontend renders only

### 9. Design Tokens
- Color palette (dark default; semantic tokens; all paired with icon/text)
- Typography (monospaced for ALL numbers — JetBrains Mono or Inconsolata; sans for prose — Inter)
- `font-feature-settings: 'tnum'` for tabular contexts
- Spacing (4px base; dense)
- Animation (≤150ms; functional only)
- Single density (dense). Single theme (dark).

### 10. Sequence Diagrams (Mermaid)
At minimum:
- WebAuthn registration on /setup (token-gated; with backup code generation)
- WebAuthn-unsupported bootstrap fallback (TOTP-only with reduced privileges)
- WebAuthn login (corrected ceremony — JS API, NO `/auth/callback`)
- TOTP backup login flow
- Backup code recovery flow
- Signal arrives → approve via web → backend executes → fill displays via SSE (with ARIA announcement)
- Same flow via Discord button (parity)
- Reject signal with decision diary entry (web AND discord)
- Invoke kill switch from Discord (confirmation; no re-auth)
- Invoke kill switch from web (confirmation; no re-auth)
- RESUME from HALT_NEW via web (re-auth required); Discord `/halt` resume attempt → explainer message redirecting to web
- Manual close during HALT_NEW (re-auth required)
- Vacation END (web-only, re-auth required); Discord `/vacation end` attempt → explainer message redirecting to web
- Stress test → POST 202 + jobId → SSE progress → terminal payload
- PDF export → POST 202 + jobId → SSE progress → signed download URL (1h, one-time)
- PR draft from parameter sandbox → review surface → human reviews → merges via backend → deploys
- PR rejection with feedback modal → reason fed to agent context
- Real-time fill update via SSE (with ARIA announcement)
- Tab eviction: server closes oldest tab on N+1 → control event → banner
- VPS outage → external watchdog email → operator manual flow
- Concurrent-tab signal approval conflict → toast revert
- SSE failure → fallback to polling → degraded indicator → SSE retry success
- Optimistic-update network failure → 3 retries → manual retry toast
- Phase 1 reconciliation status using QC brokerage source
- HALT_NEW (incident_review) entry: red banner + resume disabled until post-incident write-up

### 11. Phased Build Plan
- **Phase 0 (frontend weeks 0–3):** scaffold (Next.js, auth + /setup + /login + /recover, basic Today against mock data; other routes 404 with explainer); Discord bot skeleton with `/positions` + `/halt`; live data integration starting week 3–4
- **Phase 1 (months 2–5):** ships before live trading; per per-page (§2) and Discord-surface (§6) phasing tables. Includes Phase 1 read-only Risk Envelope tile, Phase 1 minimal Account section.
- **Phase 2 (months 5–9):** fills out Phase 2 columns; six additional features; PR review surface; full Performance + Research + Calendar
- **Phase 3 (months 9–12):** investor PDF generation; CPA reader role plumbing; refinements

Each phase: deliverables, success criteria, kill criteria.

### 12. Testing Strategy
- Component tests (Vitest + React Testing Library)
- E2E critical flows (Playwright; WebAuthn virtual authenticator): registration + login (+ TOTP-only fallback), signal approval (web + discord), kill-switch invoke (both surfaces) + resume (web only), decision diary entry, ratification (Phase 1 Discord; Phase 2 web), stress test async flow, PR review surface render + rejection feedback, vacation start (both) / end (web-only), manual position close during HALT_NEW (re-auth), concurrent-tab race / server eviction, optimistic-update failure paths, mobile-accessible pre-auth surfaces (login/setup/recover) at <1024px
- Visual regression (Chromatic) for design system consistency
- Accessibility audits (axe-core in CI) — WCAG 2.1 AA; ARIA live region behavior
- Discord bot tests: command response, button payloads, IPC ingestion, replay buffer
- **Cross-environment segregation tests:** assert no UI element ever blends `paper` and `live-*` data in single number or chart (with explicit health-score current-env-scoping carve-out and tax-artifact reader-redaction-bypass carve-out)
- CI: GitHub Actions; bundle analyzer via `@next/bundle-analyzer`; PR fails if bundle exceeds budget by >10%

### 13. Investor PDF Report Layout (year-2)
Renderer: Typst on VPS; charts pre-rendered as SVG via headless Recharts (PDF and UI render the equity curve from different libs — Recharts for PDF, Lightweight Charts for UI; visual style nearly identical; acceptable trade-off documented).

Layout:
- Cover (period, fund/strategy name placeholder, prepared-by, date)
- Performance summary table
- Equity curve and drawdown chart
- Monthly returns table (calendar heatmap)
- Risk metrics (Sharpe, Sortino, max DD, hit rate, vol)
- Attribution summary
- Methodology disclosure (one paragraph)
- Risk disclosures (CTA-style placeholder)
- Footer: page numbers, generation timestamp, hash of source data

Async delivery: POST → 202 + jobId; SSE progress on `agent`; terminal payload with signed URL (1h TTL, one-time, audit-logged).

### 14. SLO / Performance Budgets

| Page | JS bundle (gzipped) | Targets |
|---|---|---|
| / (Today) | ≤ 350KB initial | TTI ≤ 2s, LCP ≤ 2.5s, Lighthouse perf ≥ 90 |
| /trades | ≤ 500KB | (table-heavy, virtualized) |
| /performance | ≤ 600KB | (chart libs lazy-loaded) |
| /research | ≤ 800KB | (Phase 2; heaviest) |
| /system | ≤ 500KB | |
| /calendar | ≤ 350KB | |

- Recharts and Lightweight Charts deferred to /performance and /research; never on /today
- p99 SSE event-to-render: ≤ 500ms
- Aggressive code-splitting per route; dynamic imports
- Bundle analyzer in CI; PR fails if >10% over budget

### 15. Export Taxonomy

**Trades CSV columns:**
`signal_uuid, signal_emit_time_utc, signal_emit_time_et, market, direction, signal_type, strategy_hash, parameter_set_hash, slippage_calibration_version, environment_tag, anomaly_flagged, anomaly_reasons, status, approved_by, approved_at, expected_pnl, expected_slippage, vol_regime_at_emit, trend_regime_at_emit, fill_qty, fill_avg_price, realized_pnl, realized_slippage, holding_days, decision_diary_tag, decision_diary_text, capacity_constrained, hash_chain_index`
Footer: `chain_start_hash, chain_end_hash, record_count, exported_at`

**Audit CSV columns:**
`sequence_no, event_uuid, timestamp_utc, monotonic_ns, event_type, actor, environment_tag, payload_json, prev_hash, record_hash, repaired_for_sequence_no, source_clock_ts, ingest_clock_ts`
Footer: hash-chain footer.

**Performance CSV columns:**
`month, return_pct, drawdown_pct, sharpe_60d, hit_rate, trade_count, environment_tag`

**Tax annual export:** Form 6781, Schedule D, Form 8949 CSVs + PDF summary; column lists deferred to spec output (reference IRS layouts)

**PDF report:** Performance tearsheet (monthly/quarterly); async

**Print stylesheet:** Performance and Trades filtered views; explicit "Prepare for print" button trigger; US Letter portrait

### 16. Observability
- Sentry free tier for errors; Sentry Performance Monitoring upgrade at >100k events/month
- Frontend error boundary integration with Sentry
- User feedback via Sentry on errors
- ARIA live region announcements logged via Sentry breadcrumbs

### 17. Security Headers and Browser Hardening (locked)

Caddy config:
- `Content-Security-Policy`: `default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; connect-src 'self' https://sentry.io; img-src 'self' data:; frame-ancestors 'none'`
- `Strict-Transport-Security: max-age=31536000; includeSubDomains; preload`
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Permissions-Policy: camera=(), microphone=(), geolocation=(), publickey-credentials-get=(self)`

### 18. Staging Environment

- `paper.<domain>` for paper environment (separate hostname; same VPS or staging VPS)
- Production deploys → `<domain>`; staging → `paper.<domain>`
- Staging API at `paper.<domain>/api/*` reads paper-environment Postgres (separate from live)
- All staging deploys auto-tagged in audit; no live broker integration on `paper.<domain>`

## FORMAT REQUIREMENTS

- Markdown with clear section headers
- Mermaid for ALL diagrams
- Wireframes in text/ASCII/Mermaid (no image generation)
- Concrete library/tool/version recommendations
- Where genuine implementation choices remain, present 2–3 options with tradeoffs and a recommendation
- Length will be substantial; favor completeness over brevity
- Never invent strategic decisions; flag missing context with `[QUESTION FOR OPERATOR: ...]`
- For backend contract dependencies, flag with `[CONTRACT — verify against Prompt A output]` and propose your expected contract using canonical event/code vocabulary defined here
- This spec must interlock with Prompt A; reference its REST endpoints, SSE event types, and Discord command schemas by name where Prompt A's output is supplied

Begin.
