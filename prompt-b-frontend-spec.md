# PROMPT B — FRONTEND TECH SPEC

## ROLE

You are a senior frontend architect and design engineer with experience building production trading dashboards and operator interfaces for systematic CTAs and prop shops. You understand that trading UIs are utilitarian, dense, and fast — not consumer-app pretty. They resemble Bloomberg, Linear, or a small CTA's research environment, not a startup landing page.

You will produce a comprehensive technical specification for the FRONTEND of a single-operator algorithmic trading system. Implementation will be primarily by Claude Code working with a non-technical solo operator.

**The SPEC is comprehensive (full target shape); the BUILD is phased. Phase 1 ships a defined ~30% subset (enumerated per page in §2 and per Discord surface in §6); the rest follows in Phase 2 and 3. The phased build plans are binding; do not infer that everything ships in Phase 1.**

**Workflow note:** this prompt is intended to run AFTER Prompt A (backend spec) has produced its API contract. The implementer should paste Prompt A's `§4 API Contracts` section into this prompt as additional context where indicated. Where backend contract is genuinely missing, flag with `[CONTRACT — verify against Prompt A]` and propose your expected contract; do NOT invent from scratch where reasonable defaults are listed below.

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

The frontend talks to the backend via FastAPI REST + SSE (same origin — see Architecture). The Discord bot ALSO talks to the backend (mostly via REST + receives backend-to-bot HTTP-IPC events). Most user actions (signal approval, decision diary entry, ratification) must be possible from BOTH the web app AND Discord and produce identical outcomes via shared backend endpoints. **Asymmetric exception:** kill-switch INVOKE is available from both surfaces; kill-switch RESUME is web-only (requires WebAuthn UV which Discord cannot perform).

## ARCHITECTURE / TOPOLOGY (LOCKED — REVISED: SELF-HOSTED ON VPS)

```
Browser ──── HTTPS ──── api.<domain> + app.<domain> (single VPS, Hetzner Ashburn)
                           │
                           ├── Caddy or Traefik (reverse proxy, Let's Encrypt)
                           │     ├── routes /api/* → FastAPI
                           │     ├── routes /sse/* → FastAPI (long-lived SSE)
                           │     └── routes /* → Next.js production server (or static SPA)
                           ├── Next.js Node server (or pre-built static SPA + nginx)
                           ├── FastAPI
                           ├── Postgres
                           ├── LEAN engine
                           ├── Claude ops agent
                           ├── Discord bot service
                           └── Discord webhook-pusher service

External watchdog (different region — Hetzner Falkenstein or AWS Lambda)
   ├── pings api.<domain>/health every 5 min
   ├── pushes ping result to api.<domain>/internal/watchdog
   └── emails operator if backend unreachable >15 min during CME RTH
```

**Why self-hosted (not Vercel):**
- Vercel Hobby tier ToS prohibits commercial/trading use; Vercel Pro is $20/mo + adds cross-origin SSE+cookie complexity
- Self-hosting on the same VPS = single origin (no CORS); same uptime story we already accept (no warm standby per RPO/RTO)
- The earlier "frontend uptime independent of VPS" claim was nearly meaningless once live data went browser↔VPS direct — abandon it cleanly
- Saves $20/mo

**Single-origin model:**
- Both web app and API served from `<domain>` (no `app.` vs. `api.` split required, but allowed if preferred for routing clarity; if split, `app.<domain>` and `api.<domain>` share parent for cookie scoping)
- Recommended: single origin `<domain>` with reverse proxy routing `/api/*` and `/sse/*` to FastAPI, everything else to Next.js
- Cookies issued by backend, `HttpOnly; Secure; SameSite=Strict; Path=/`
- WebAuthn Relying Party = `<domain>`
- No cross-origin fetch issues; `credentials: 'same-origin'` (default) suffices

**Rendering model:**
- Next.js App Router; **SSG (static generation) for the SPA shell + client-side hydration + client-side data fetching for authenticated content**
- Pre-auth pages (`/login`, `/setup`, `/recover`) and post-auth shell are static
- Authenticated data fetches happen client-side via TanStack Query against `/api/*`
- No SSR with per-request VPS calls (would just add overhead since we're on the same host)

**Network details:**
- All live data (REST + SSE) flows browser ↔ VPS over HTTPS via reverse proxy
- HSTS enabled; security headers configured (see Security section)

**SSE transport:**
- Use **`@microsoft/fetch-event-source`** library, NOT native `EventSource`
- Reasons: supports POST + custom headers, better error handling, proper abort signal support, consistent cookie behavior. Future-proof if we ever need cross-origin.
- Single multiplexed channel `/api/sse/events`
- Client filters/dispatches by event `type`
- One connection per tab; **server-side enforces N-connection limit per user session**; on connection N+1, server closes oldest with a control event (`{"type": "session_evicted", "reason": "tab_limit"}`); browser displays banner. **No client-side cross-tab coordination needed.** (BroadcastChannel for cross-tab coordination is a Phase 2+ enhancement, not required for eviction.)

## TECH STACK (LOCKED)

- **Next.js 14+ App Router** + **TypeScript** (strict mode) + **Tailwind CSS** + **shadcn/ui** components
- **Hosted on the same Hetzner VPS** as backend; served via Caddy or Traefik reverse proxy with Let's Encrypt
- **TanStack Query** for server-state, **Zustand** for client-state
- **TanStack Table + `@tanstack/react-virtual`** for large tables (Trades, audit explorer)
- **Recharts** for analytics charts; **Lightweight Charts** (TradingView OSS) for price/equity curves; **lazy-loaded per page** (not bundled into `/today`)
- **`@microsoft/fetch-event-source`** for SSE (NOT native EventSource)
- **Sonner** for toasts
- **react-hook-form + zod** for forms
- **Authentication:** WebAuthn (passkey) primary + TOTP backup + 8 single-use printed backup codes generated at enrollment
- **Authorization (RBAC):** schema present from day 1; "owner" role active initially; "reader" role planned for CPA in year 2; investor role NEVER (PDF reports only)
- **Reader role permission matrix (year-2 deliverable, schema present now):**
  - Reader CAN view: Performance page (all metrics in % terms; **no absolute dollar amounts** — see redaction rule), Trades read-only including per-trade detail and decision diary for tax provenance, Tax export download, tax widget detail
  - Reader CANNOT view: System (risk envelope, deployments, agent activity prompts/responses), Research, Calendar ratification controls, account numbers
  - **Reader redaction rules (locked):** account numbers fully redacted; absolute dollar P&L converted to percentage of starting NAV; strategy code/PR contents fully hidden; agent prompts/responses fully hidden; trade timing displayed but trade rationale (decision diary author=agent entries) hidden
  - Reader CANNOT do: any writes
- **PDF rendering:** **Typst** for layout/typography on the VPS, with **charts pre-rendered server-side as SVG via headless Recharts** and embedded as images. Frontend triggers via API: **async** (POST → 202 + jobId + SSE progress events on `agent` channel → terminal payload with signed download URL). **Signed URL: 1-hour TTL; one-time use; download logged to audit.**
- **Error tracking and RUM:** **Sentry** free tier (errors) + Sentry Performance Monitoring at low-volume tier (~$26/mo if usage warrants). NOT Datadog.
- **Feature flagging:** simple env-var-based flags for Phase 1/2/3 gates (`NEXT_PUBLIC_PHASE=1|2|3`); read at boot. NO PostHog/LaunchDarkly.
- **Browser support:** latest 2 stable versions of Chrome, Firefox, Safari. Edge implicit (Chromium). Detect WebAuthn support; show explainer if unsupported.

For Discord:
- **`discord.py`** bot (runs as Python service on the VPS, separate from web frontend; communicates with backend via internal HTTP-IPC over Docker network)
- **Slash commands** + **button interactions** + **embeds** + **threads**

## DESIGN PHILOSOPHY (BINDING — DO NOT SOFTEN)

- **Utilitarian, not aesthetic.** Resemble Bloomberg, Linear, professional CTA tools. Dense. Fast. Monospaced numbers. Dark by default. NO marketing-style polish.
- **Animations:** functional only (state transitions, modal open/close, drawer slide). Max 150ms duration. NO decorative or attention-grabbing animations.
- **Functional and fast over decorated.**
- **Mobile = Discord, NOT a native app.** Web app is desk-only. NO native mobile build.
- **Tablet/mobile policy:** below **1024px viewport**, render "use desktop or Discord" notice with Discord deep-link button. **EXCEPTION: `/login` is accessible at all viewport sizes** (operator must authenticate from any device to start the day; can use a phone if laptop is dead). All other pre- and post-auth pages enforce the 1024px block.
- **Simple now, simple later.**
- **Single density mode** (dense). Not configurable.
- **Single theme** (dark). Not configurable.
- **Numeric formatting:** US locale; tabular figures via `font-feature-settings: 'tnum'`. Negatives = leading minus + red color + small downward arrow icon (color-blind safe). Positives = no sign + green + upward arrow on emphasized values; bare otherwise.
- **Time-zone:** ALL UI in `America/New_York`. NOT user-configurable.
- **Numeric precision:** read from backend's `instrument_metadata` — never hardcoded.
- **Time source for stale-data:** server-supplied timestamps in every payload (`server_now`); browser clock never trusted.
- **Empty-state visual language:** muted text-only with single optional CTA button; no illustrations; austere; consistent with utilitarian tone. Pattern: "No <noun> yet" + one short explainer + optional CTA.

## INFORMATION ARCHITECTURE — 6 POST-AUTH PAGES + 3 PRE-AUTH SURFACES

**Post-auth ("6 pages"):**
1. **Today** — landing; single-glance dashboard (`/`)
2. **Trades** — unified signal queue + position monitor + fill history + per-trade journal + attribution; filterable (`/trades`, `/trades/:id`)
3. **Performance** — equity curve, drawdown, attribution, tearsheet, PDF export (`/performance`)
4. **Research** — backtest viewer, parameter sandbox, regime analysis, A/B (`/research`, `/research/backtest/:id`)
5. **System** — risk envelope, kill-switch UI, deployments, agent activity, audit explorer, reconciliation, watchdog status (`/system`, `/system/audit/:id`, `/system/pr/:id`)
6. **Calendar** — events, ratification, holidays, roll schedule (`/calendar`)

**Pre-auth surfaces:**
- **`/login`** — WebAuthn login + TOTP fallback + backup-code link
- **`/setup`** — first-run bootstrap: backend prints one-time registration token at first start; operator visits `/setup?token=...`
- **`/recover`** — account recovery with backup code

**Auth callback:** `/auth/callback` — handles post-WebAuthn-redirect from backend; reads session cookie; navigates to original target URL (preserved in `state` parameter through ceremony).

NO additional post-auth pages. NO investor dashboards. NO mobile-optimized variants. NO "Agent" page.

**Deep-link conventions:**
- Discord-to-web: every Discord embed includes a deep link to the relevant detail (`/trades/:signal_uuid`, `/system/audit/:event_uuid`, `/system/pr/:pr_id`)
- Trade rows in Trades table link to `/trades/:id` (drawer mode preferred; full-page on direct nav)

## LOCKED STRATEGIC AND SYSTEMS DECISIONS — DO NOT REOPEN

### Strategy and Phasing
- Multi-asset systematic trend-following on micro futures + bond ETFs
- Universe: ~8–12 markets (equity index micros, commodity micros, /MBT, bond ETFs)
- Daily bars; signal generation 17:30 ET
- **Phase 0 (frontend weeks 0–3, parallel with backend Phase 0 weeks 0–8):**
  - Weeks 0–2: scaffold Next.js + auth + /setup + /login + /recover; basic /today rendering against MOCK DATA (typed mock fixtures matching expected backend schemas); other post-auth routes (`/trades`, `/performance`, `/research`, `/system`, `/calendar`) are **hidden from navigation and return 404 for direct URLs** during this window
  - Weeks 2–3: backend audit log integration begins (week 3–4); /today switches to live data; other routes unhide progressively as their backend endpoints come online
- **Phase 1 (months 2–5):** ships before live trading begins per per-page enumeration (§2) and Discord enumeration (§6)
- **Phase 2 (months 5–9):** fills out Phase 2 columns; six additional features; full Performance + Research + Calendar
- **Phase 3 (months 9–12):** investor PDF generation; CPA reader role plumbing

### Risk Framework (numbers locked from Prompt A; frontend renders)
- Vol-targeted sizing, **14% portfolio annualized vol**
- Per-position / gross / net trio: 25% / 300% / 150% of equity notional
- Cluster caps: equity-index 60%, commodity 80%, rates/bonds 80%, crypto 40%, FX 30%
- Realized cross-portfolio correlation alert >0.7, halt >0.85
- Daily loss limit -5% of daily-start MTM (17:00 ET)
- Trailing DD -20% (capital-event reset)
- Monthly DD threshold -10% triggers vol halving
- Decommission floor: live 30-day Sharpe < 0 OR live max DD breach -25% OR 60-day live underperforms backtest by > 2 SD → HALT_NEW

### Vol-Target Multiplier Composition
When multiple reductions are active, take MIN of multipliers (do NOT compound).

### Kill-Switch State Machine
- States: `NORMAL`, `HALT_NEW` (cancel working orders, hold positions, no new entries; manual close ALLOWED with re-auth), `CONVALESCENT` (50% vol target, 5 CME RTH sessions)
- HALT_NEW max dwell: 7 trading days → operator escalation
- IBKR margin-call residual risk explicitly possible at HALT_NEW with high used margin (system-initiated panic-flatten not done; broker-mandated liquidation outside scope; alert language at HALT_NEW entry due to margin must call this out)
- Convalescent banner shows: state, sessions remaining, current effective vol target, exit countdown

### Audit & Track Record
- Immutable append-only audit log; SHA-256 single-linked hash chain by insertion order; backfills append at tail with `repaired_for_sequence_no` provenance; gaps remain visible
- Composite identity: `strategy_hash` + `parameter_set_hash` + `slippage_calibration_version`
- Track record portability: lineage metadata persisted; UI segregates environments via filters and tabs; **never visually splices `paper` / `live-small` / `live-scale` into one chart or one number** — except the strategy health score (current-environment-scoped by definition)
- Environment tags: `paper` / `live-small` (real money, equity < $50k at signal time) / `live-scale` (real money, equity ≥ $50k at signal time); immutable per trade

### Tax
- Futures (1256): automatic 60/40, no election
- ETFs: capital gains/losses with wash sale tracking; no 475(f) election by default
- Tax estimate widget: YTD liability, 1256 60/40 breakdown, wash-sale-flagged trades; nightly update
- Tax election toggle (475(f) for ETFs); CPA-acknowledgment-gated; logged to audit

### Claude Ops Agent Authority Matrix
| Category | Authority |
|---|---|
| Tighten risk via parameter change (next cycle) | AUTO + notify |
| Tighten risk via defensive position trim (mid-session) | AUTO + notify (causally agent-initiated; mechanically risk-engine-placed) |
| Loosen risk | HUMAN APPROVAL |
| Hot-fix infra (within whitelist) | AUTO-DEPLOY + auto-rollback if degraded |
| Strategy logic changes | DRAFTS PR (operator-friendly review surface) |
| Place orders directly (primary action) | NEVER (no broker creds) |
| Invoke kill switch | AUTO on threshold |
| Un-invoke kill switch | HUMAN ONLY (re-auth, web-only) |
| Strategy params within pre-approved range | AUTO + auto-revert (2 SD widened) |
| Reports/alerts/briefings/diagnostics | AUTO |

### Performance Targets
- Phase 1: backtest Sharpe ≥ 1.5, live ≥ 0.8, max DD ≤ 15%, signal acceptance ≥ 90%
- Phase 2: portfolio live ≥ 1.2
- Phase 3: portfolio live ≥ 1.5

### Severity Model
- **P0:** kill-switch fired, broker disconnect, reconciliation tolerance breach, margin auto-trim invoked, audit-log write failure, Defensive Risk Envelope. Critical channel + email backup.
- **P1:** slippage drift, model decay, capacity warning, anomalous signal flagged, vol regime transition. Warn channel.
- **P2:** informational (fills, daily summary, agent reports, ratification reminders, daily liveness probe). Routine.

### Anomaly-Flagged Signals — Reason Code Vocabulary (LOCKED)

Backend emits `anomaly_reasons: AnomalyReasonCode[]` alongside the `anomaly_flagged: boolean`.

| Code | Meaning |
|---|---|
| `vol_regime_z_high` | Vol regime z-score > 1.5 |
| `capacity_above_alert` | Capacity at 0.5%–2% ADV (between alert and cap) |
| `recent_decision_diary_concern` | Decision diary `data_concern` or `regime_concern` for same market within 14 days |
| `slippage_outlier_recent` | Backtest expected slippage exceeded by > 2× in last 5 trades for same market |
| `version_baseline_divergence` | Strategy-version-vs-baseline divergence flagged in last week's golden test |

Frontend maps codes to human-readable tooltip text per a localized string table.

### Stale-Data Thresholds (locked, per data type)

| Data Type | Stale during CME RTH | Stale outside CME RTH |
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

**Stale indicator:** subtle yellow corner badge + tooltip with last-update timestamp.

**PAUSED vs. STALE distinction:** when system state is HALT_NEW, CONVALESCENT, or vacation, intentionally-paused flows display a "PAUSED — last activity at X" pill instead of stale badge. Backend signals state via SSE so frontend switches indicators without false stale flags.

### Re-Auth Requirements (locked — principle restated for consistency)

**Principle:** WebAuthn UV re-prompt within last 5 minutes is required for **(a) risk-loosening actions**, OR **(b) direct manual order actions while system is in a halt state**. NOT required for risk-tightening or rule-defined signal flow actions.

**Required (re-auth):**
- Kill-switch RESUME (un-invoke) — risk-loosening; web-only (Discord cannot perform UV)
- Parameter range change PR submission — risk-loosening
- Deploy approval (any) — material change
- Environment tag override — material change
- Backup code regeneration — sensitive
- Tax election toggle — material change
- **Vacation END** — risk-loosening (re-enables strategy entries) — **CORRECTED from prior inconsistency**; required from web; from Discord, requires confirmation modal (no UV available there) plus reason capture for audit
- **Manual position close during HALT_NEW** — direct manual order action in halt state

**NOT required (no re-auth):**
- Kill-switch INVOKE — risk-tightening; friction-light
- Defensive trim invocation — risk-tightening
- Signal approval / reject (in NORMAL state) — rule-defined flow, not ad-hoc; protected by session
- Decision diary entry — supporting metadata, not action
- Calendar ratification — required engagement, not order action
- Stress test run — read-only computation
- Vacation START — risk-reducing (disables new entries)

**UV freshness mechanism:** server-side `last_uv_at` per session row in Postgres; checked server-side on sensitive endpoint calls.

### CSRF Strategy (locked)

`SameSite=Strict` cookies + **double-submit cookie pattern for sensitive endpoints**: backend issues `csrf_token` cookie at session start; frontend reads via JS (cookie is NOT HttpOnly for this token) and sends as `X-CSRF-Token` header on POST/PUT/DELETE requests. Backend validates header equals cookie value on every state-changing request. Belt-and-suspenders against CSRF.

### Session Lifetime (locked)

- **Idle timeout:** 30 min (sliding; resets on any authenticated request)
- **Absolute maximum:** 24 hours from login
- **Refresh token:** 7 days (used to obtain new access token without re-auth, when within absolute max)
- **Cookie max-age:** matches absolute max
- After absolute max, full re-login required

### Strategy Health Score (locked formula)

Scope: current environment only. Single number cannot blend environments.

| Component | Weight | Window | Score 0–100 |
|---|---|---|---|
| Live Sharpe vs. backtest | 30% | 60-day rolling | 100 if live ≥ backtest; 0 if live < backtest − 2σ; linear |
| Slippage drift | 20% | 30-day rolling | 100 if realized ≤ assumed; 0 if realized ≥ 2× assumed; linear |
| Hit rate vs. expected | 20% | 60-day rolling | 100 if live ≥ expected; 0 if live ≤ expected − 20%; linear |
| Capacity headroom | 15% | current | 100 if avg position < 0.25% ADV; 0 if any > 1% ADV; linear |
| Days since last reconciliation break | 15% | current | 100 if ≥ 30 days; 0 if < 1 day; sqrt-shaped |

Composite = weighted sum. Green ≥ 75, Yellow 50–74, Red < 50.

**Insufficient data:** missing components show gray "—"; composite re-weights available components; if total weight < 50%, render G/Y/R as gray "—" with explainer.

**Click-expand cache:** server returns full component breakdown in same payload as composite (single fetch); click-expand uses cached data, no refetch.

### Concurrent-Tab / SSE Eviction (locked)

- Server enforces N-connection-per-user limit (default N=4)
- On connection N+1, server closes oldest with control event `{type: "session_evicted", reason: "tab_limit"}`
- Browser receives event, displays banner, stops further updates
- **Phase 1: server-driven eviction only** (no client-side cross-tab coordination required)
- Phase 2 enhancement: `BroadcastChannel` for cross-tab optimistic-update reconciliation

### Optimistic Update Failure UX

On user action (e.g., signal approval):
1. Apply optimistic state immediately
2. Send to backend
3. On 5xx or network failure: queue locally, retry up to 3× with exponential backoff (1s, 4s, 16s)
4. After 3 failures: surface as toast with manual "Retry" button + "Cancel" button; revert optimistic state until user acts
5. On contradicting SSE event during retry: revert optimistic state, show toast "approved by another tab" or similar

### Bulk-Approve "Standard" Signals
The button approves **all signals NOT `anomaly_flagged`**. Disabled when zero non-anomaly signals exist. Enabled otherwise (regardless of how many anomaly-flagged also present).

### Toast/Alert Taxonomy
- **P0:** persistent until manually dismissed; subtle single-chime sound; top-center; full-width banner-style; **ARIA `role="alert"`**, `aria-live="assertive"`
- **P1:** 8s auto-dismiss; top-right; **ARIA `role="status"`**, `aria-live="polite"`
- **P2:** 4s auto-dismiss; top-right; same ARIA as P1
- Stack cap 5 visible; older collapse to "+N more" group

### Live Region / Accessibility for SSE Updates

Incoming SSE events that affect on-screen UI must update via React state and be announced where appropriate via ARIA live regions:
- New signal in queue → `aria-live="polite"` region announces "New signal: {market}, {direction}"
- Fill arrives → `aria-live="polite"` region announces "Fill: {market} {qty} @ {price}"
- P0 alert fires → `role="alert"` region announces full alert text
- Health score changes → no announcement (visual only)
- Position updates → no announcement (visual only; high-frequency)

### SSE Event Ordering

- Each event carries `sequence_no` (monotonic per-channel)
- Client tracks last-received `sequence_no`; on gap detected, requests replay from `last_event_id`
- Out-of-order events buffered up to 5s; if older arrives, applied in order

### Empty-State Inventory (Phase 0/1 day-one states)

Every screen requires explicit empty-state design. Pattern: muted heading + one short explainer + optional CTA.

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

### Strategy Version Badge

- **Global badge** (top bar): currently DEPLOYED strategy version (short hash + click for full info)
- **Updates via SSE** on `agent` channel deploy events; no full reload required
- **Per-trade pill** (Trades rows + detail drawer): version active at signal emit time; smaller; muted color

### Trade State Enumeration (locked)

`pending` → `approved` | `rejected` | `deferred` | `expired`
`approved` → `working` → `partially_filled` | `filled` (= `executed`)
`filled` / `executed` → `open_position` → `closed`
`partially_filled` can also enter `capacity_constrained`
`open_position` → on stop hit: `stopped_out`
Terminal: `closed`, `stopped_out`, `rejected`, `expired`.

### Vacation Mode

- Server-enforced (backend refuses new entries during vacation; calendar ratification suspended)
- Frontend reads vacation state via SSE; renders persistent banner with end date and "End vacation now" button
- **Ending vacation requires re-auth on web (consistent with re-auth principle); from Discord, requires confirmation modal + reason capture (since UV unavailable)**
- New signals not generated during vacation; existing positions exit normally (stops, profit-targets, manual close)

### PR Rejection Feedback Loop

- Modal: tag picker (`logic_disagreement` | `risk_concern` | `unclear_rationale` | `bad_test_coverage` | `other`) + free text, min 10 chars
- Reason logged to audit + fed back to agent context
- PR closed with comment via backend GitHub App

### Agent Status Indicator States (locked)

Top-bar element shows one of:
- `idle` — agent ready, no recent activity
- `working` — agent currently processing a request or generating a report
- `degraded` — Claude API rate-limited or partial outage; agent in read-only mode
- `disabled` — vacation mode (agent not generating reports during vacation per locked policy) or operator-toggled off
- `errored` — agent encountered unrecoverable error in last hour; auto-recovers; alert raised

### CSV Export Schemas (locked column lists)

**Trades CSV:**
`signal_uuid, signal_emit_time_utc, signal_emit_time_et, market, direction, signal_type, strategy_hash, parameter_set_hash, slippage_calibration_version, environment_tag, anomaly_flagged, anomaly_reasons, status, approved_by, approved_at, expected_pnl, expected_slippage, vol_regime_at_emit, trend_regime_at_emit, fill_qty, fill_avg_price, realized_pnl, realized_slippage, holding_days, decision_diary_tag, decision_diary_text, capacity_constrained, hash_chain_index`
Footer row: `chain_start_hash, chain_end_hash, record_count, exported_at`

**Audit CSV:**
`sequence_no, event_uuid, timestamp_utc, monotonic_ns, event_type, actor, environment_tag, payload_json, prev_hash, record_hash, repaired_for_sequence_no, source_clock_ts, ingest_clock_ts`
Footer row: same hash-chain footer.

**Performance CSV:**
`month, return_pct, drawdown_pct, sharpe_60d, hit_rate, trade_count, environment_tag`

Tax exports: defer column lists to spec output; reference IRS Form 6781, Schedule D, Form 8949 layouts.

### Operating Cost Dashboard Provider List (locked)

Tile per provider with monthly run-rate and delta vs. envelope. Providers (matching Prompt A's Operating Cost Envelope):
- Hetzner VPS primary
- Hetzner external watchdog
- QuantConnect (Phase 1)
- Polygon.io (Phase 2 contingent — show $0 if not active)
- Anthropic API
- S3 / Backblaze B2 backups
- Sentry
- Email service (Resend or SES)
- Domain registrar
- IBKR market data
- GitHub (typically $0)

Aggregate: total monthly + delta vs. soft ceiling ($200) and hard ceiling ($300); 90-day trend chart.

## YOUR DELIVERABLE

Produce a complete, production-grade frontend technical specification covering ALL sections below. Use Mermaid for diagrams. Wireframes described in TEXT/ASCII/Mermaid (not image generation). Be specific and concrete.

**Backend API contract:** the parallel backend spec (Prompt A) produces the canonical REST/SSE/Discord schema. Where Prompt A's output is available, reference endpoints/channels/commands by name. Where unconfirmed, flag with `[CONTRACT — verify against Prompt A]` and propose your expected contract using the canonical event/code vocabulary defined above. Specifically:

- One SSE endpoint: `GET /api/sse/events` (delivered via `@microsoft/fetch-event-source`)
- Event types: `signal`, `fill`, `position`, `pnl`, `risk_state`, `health`, `alert`, `audit`, `agent`, `vacation`, `watchdog`, `session_evicted`
- Each event: `{ type, sequence_no, server_now, data }`
- Resume on reconnect via `last-event-id` header

### 1. Information Architecture
- Full IA tree (page → sections → components → states)
- Pre-auth surfaces (`/login`, `/setup`, `/recover`) + `/auth/callback`
- Navigation model (top nav recommended; defend if differing)
- Command palette (cmd-k): pages, plus search corpus = trades by ID/symbol, signals by ID, audit entries by ID/text
- **Keyboard shortcuts:** `?` opens cheat-sheet modal; document the full shortcut list
- Persistent UI elements (top bar): strategy version badge (global), strategy health score (G/Y/R, current-environment scoped), current portfolio P&L, agent status indicator (state enum above), environment tag, current state (NORMAL / HALT_NEW / CONVALESCENT / VACATION) with banner if not NORMAL
- Deep-link conventions for Discord-to-web jumps

### 2. Screen-by-Screen Specification

For each of the 6 post-auth pages: layout, component hierarchy, data displayed (with backend source — endpoint or SSE event type; reference Prompt A by name; flag with `[CONTRACT]` where unconfirmed), all states (empty/loading/error/partial-data/stale-data/paused), interactions, real-time update behavior, filter/sort/search controls, accessibility (ARIA live regions where applicable).

**Phase 1 surface enumeration per page (binding):**

| Page | Phase 1 ships | Phase 2 adds |
|---|---|---|
| Today | Health score (with insufficient-data handling), positions table, P&L summary D/W/M/Y, exposure breakdown, queued signals (individual approve/reject WITH decision diary modal on rejection; NO bulk-approve, NO anomaly badge), recent fills feed, P0/P1 alerts, paused-state distinction | Stress test button, anomalies quick-link list, P2 alerts integration, bulk-approve "standard", anomaly badges |
| Trades | Filterable summary table (date/market filters); CSV export | Per-trade detail drawer/page, decision-diary view in Trades, attribution view, all filters, advanced search |
| Performance | Equity curve (no benchmark overlay), monthly returns table; CSV export | Drawdown underwater, attribution by market/signal/regime, actual-vs-rule compare, tax estimate widget, PDF export, benchmark overlay, print stylesheet |
| Research | (not in Phase 1) | Backtest viewer, parameter sandbox, regime analysis, A/B compare, walk-forward visualizer |
| System | Kill-switch UI + state, audit log basic table (cursor-paginated; **filter = date range + event type + environment**), reconciliation status, watchdog status | Risk envelope view + propose-PR, deployments log + rollback, agent activity feed, full audit explorer with FTS + actor + hash-validity + repaired-events filters, operator-friendly PR review surface, convalescent banner, operating cost dashboard, operator account management |
| Calendar | Read-only event list (next 30 days) | Tomorrow's ratification flow on web (Discord ratification ships Phase 1), holidays, contract expiration / roll schedule, manual event log |

#### Today (full target)
- Strategy health score (G/Y/R) prominent + click-expand using cached payload
- Current positions table (compact, monospace, virtualized if >50 rows)
- P&L summary (D/W/M/Y) with benchmark comparison
- Exposure breakdown (gross / net / per-market / per-cluster) visualized against ring + cluster limits
- Queued signals — quick approve/reject inline; rejection opens decision diary modal (ships Phase 1)
- Recent fills feed (live via SSE event type `fill`; ARIA-announced)
- Active alerts (P0 → P1 → P2 sorted)
- Stress test "run now" button (Phase 2; async)
- Quick links to anomalies (Phase 2)

#### Trades (full target)
- Unified table (TanStack Table + `@tanstack/react-virtual`)
- Filters: date range, market, strategy version, regime (vol + trend), signal type, environment (never blended)
- Per-trade detail drawer (Phase 2): full lifecycle; decision diary; attribution; agent commentary; linked audit entries; stress-test impact
- Server-side pagination (cursor-based infinite scroll); server-side filter pushdown
- Expected scale: ~50–200 trades/month; 5-year accumulation 3k–12k
- CSV export per locked schema

#### Performance (full target)
- Equity curve with benchmark overlay (SPY default; configurable Phase 2)
- Drawdown chart (underwater plot)
- Monthly returns calendar heatmap
- Attribution by market, signal type, vol regime, trend regime
- Rolling Sharpe, rolling DD, rolling hit rate (60-day default)
- Actual vs. rule-following P&L compare (dual curves; rolling 30-day divergence; alert at 5%)
- Tax estimate widget (click-expand; election toggle CPA-acknowledgment-gated)
- Environment-segregation rule: charts default to current environment; "Show all environments (segregated)" renders separate stacked panels per environment; never blended
- PDF export (async; Typst)
- Print stylesheet for printable views (Performance, Trades filtered): page-break-inside: avoid for cards; header includes period and prepared-by; footer includes generation timestamp; A4 portrait by default
- CSV export per locked schema

#### Research (Phase 2)
- Backtest result loader from CLI-generated artifacts via backend API
- Equity curve, trade list, statistics for a backtest
- Parameter sandbox: propose change → drafts a PR via backend (backend holds GitHub App install token)
- Regime analysis
- A/B comparison view
- Walk-forward visualizer: backend exposes per-window data; render as overlapping bars or strip chart

#### System (full target)
- Risk envelope: view current limits with cluster cap visualization; propose changes via PR-drafting workflow (re-auth required)
- Kill switch: status, history, manual invoke (confirmation modal; **NO re-auth** — invoke is risk-tightening), recovery flow (RESUME requires re-auth; web-only)
- Convalescent mode banner (sessions remaining + effective vol target + exit countdown)
- Vacation mode banner (end date + end button; end requires re-auth)
- Deployments log: every deploy with diff view and rollback button (re-auth for rollback)
- Agent activity feed (expandable to show prompt + response)
- Operator-friendly PR review surface (full rendering spec):
  1. Plain-English summary (≤200 words)
  2. Risk impact summary (auto-generated)
  3. Backtest delta (LEAN-authoritative; equity curve overlay, key stats delta, ten worst-divergence trades)
  4. Test results (unit + integration + linting + type-check)
  5. Files affected
  6. Diff view (collapsed by default)
  7. In-app Approve / Reject / Request Changes buttons (sync to GitHub via backend)
  - On Reject: PR rejection feedback modal (locked above)
- Audit explorer: cursor-paginated, server-side filter pushdown, virtualized, infinite scroll; Postgres FTS on `reason`; hash-chain integrity badge per record; backfill-provenance indicator (visible gap markers + linked repair records); environment filter; **Phase 1 filters = date + event type + environment; Phase 2 adds actor, FTS, hash-validity, repaired-events**
- Reconciliation status: last reconciliation per source (TWS real-time / FlexQuery EOD), tolerance-band check results, breaks, weekly summary
- External watchdog status: data path watchdog → backend `/internal/watchdog` → backend `system_state` → frontend reads `GET /api/system/status`
- Operating cost dashboard (provider list locked above)
- Operator account management: regenerate backup codes (re-auth), revoke all sessions, manage TOTP enrollment

#### Calendar (full target)
- 30-day forward view of macro events (tier 1/2/3, color + icon)
- Tomorrow's events ratification: must be ratified by 23:00 ET; if not, hard halt next session until ratified; banner shows requirement (Phase 1: Discord-primary; Phase 2: web-primary)
- Contract expiration / roll schedule (futures only; per `ROLL_DAYS_BEFORE_EXPIRY`)
- Exchange holidays
- Manual event log (operator-added; logged to audit)

#### Pre-auth Surfaces

##### `/login`
- WebAuthn login (full-navigation ceremony to backend; redirect back to `/auth/callback` then to original target)
- TOTP fallback (collapsed by default)
- Backup code link → `/recover`
- Browser unsupported explainer if no WebAuthn
- Accessible at all viewport sizes (1024px block exempted)

##### `/setup` (first-run bootstrap)
- Token-protected route (`?token=...` from backend stdout at first boot)
- Wizard:
  1. Enroll WebAuthn passkey (if browser supports; if not, allow TOTP-only enrollment with prominent warning that WebAuthn must be added on first compatible browser; reduced session privileges until WebAuthn enrolled)
  2. Enroll TOTP (QR code + manual entry)
  3. Generate 8 single-use backup codes; **force download/print acknowledgment** before continuing
  4. Confirm enrollment; redirect to /today

##### `/recover`
- Backup-code entry (single-use; 8 available at enrollment)
- Successful entry → reset WebAuthn + TOTP enrollment; regenerate backup codes
- Failed: rate-limited; after 5 fails, lock 1h
- "All factors lost" path: shows escalation message with `dba_breakglass` procedure contact

### 3. Six Locked Additional Features (each spec'd concretely)
- **Decision diary** (Phase 1: rejection-flow modal in /today AND in Discord; Phase 2: Trades page queryable surface): structured tag + free text; min 10 chars; queryable
- **Actual vs. rule-following P&L compare:** dual equity curves on Performance; rolling 30-day divergence; alert at 5%
- **Strategy health score:** locked formula above
- **Benchmark overlay:** SPY default; configurable Phase 2
- **Tax estimate widget:** YTD liability, 1256 60/40, wash sale flagging; nightly cron; click-expand
- **Stress test:** async on Today (POST → 202 + jobId, SSE on `agent` channel, terminal payload); modal shows ALL six scenarios in tabbed view; **summary table columns: scenario name, total P&L impact ($), max position-level loss ($), DD impact (%), worst-hit market**

### 4. Real-Time Update Mechanism
- Single multiplexed SSE channel `/api/sse/events` via `@microsoft/fetch-event-source`
- Per-page update strategy
- Polling fallback: if SSE fails after 3 retries (5s, 15s, 30s backoff), REST polling at intervals matching stale-data thresholds; UI shows "DEGRADED — polling mode" indicator; retry SSE every 60s
- Reconnection: exponential backoff with jitter; resume via `last-event-id` header
- Stale-data vs. paused-state indicators
- Multi-tab: server-side eviction (locked above)
- Retry/backoff on 429: exponential with jitter; max 5 retries; banner if persists >10s

### 5. Auth and Session Management
- WebAuthn registration flow (Mermaid sequence) — full navigation to backend; RP=`<domain>`; post-redirect handler `/auth/callback` reads session and navigates to `state.targetUrl`
- TOTP backup flow
- 8 single-use backup codes generated at enrollment; printed by user; hashed in DB
- WebAuthn-unsupported bootstrap: TOTP-only enrollment allowed with prominent warning; session has reduced privileges until WebAuthn added
- Session token model: opaque session ID in HttpOnly + Secure + SameSite=Strict cookie; CSRF token in non-HttpOnly cookie; double-submit pattern on state-changing requests; server-side session row with `last_uv_at` for re-auth checks
- Session lifetime: 30 min idle / 24h absolute / 7d refresh
- Re-auth (WebAuthn UV within 5 min) per locked Re-Auth principle and list
- RBAC: owner active; reader planned (full permission matrix above; redaction rules locked)
- Account recovery via backup codes; if all factors lost → `dba_breakglass`

### 6. Discord Bot Specification

#### Discord Surface Phasing (binding)

| Surface | Phase 0 | Phase 1 | Phase 2 |
|---|---|---|---|
| `/positions` | ✓ | full | refinements |
| `/halt` (kill-switch INVOKE; resume not supported via Discord — explainer message) | ✓ | full | — |
| `/pnl [today|wtd|mtd|ytd]` | — | ✓ | — |
| `/exposure` | — | ✓ | — |
| `/calendar` | — | ✓ | — |
| `/last-fills [n]` (default 10, max 50) | — | ✓ | — |
| `/ratify` (ratify tomorrow's calendar) | — | ✓ | — |
| `/health` (current health score breakdown) | — | ✓ | — |
| `/vacation start [days]`, `/vacation end` | — | ✓ (end requires confirmation modal + reason) | — |
| `/report [period]` | — | — | ✓ |
| `/ask <query>` (Claude agent chat) | — | — | ✓ |
| Channels: `#daily-brief`, `#signals`, `#fills`, `#alerts`, `#critical`, `#ops`, `#audit` | — | ✓ | — |
| Channel: `#ask-agent` | — | — | ✓ |
| Signal approve/reject/defer buttons | — | ✓ (rejection requires decision diary modal: tag + min 10 chars) | — |
| Bulk approve "standard" button on daily brief | — | — | ✓ |
| Per-trade threads (fill updates, agent commentary, operator notes) | — | — | ✓ |
| P0/P1 alert delivery | — | ✓ | — |
| P2 alert delivery (informational) | — | — | ✓ |
| Replay buffer (24h) on reconnect | — | ✓ | — |

#### Channels (Phase 1+ unless noted)
For each (`#daily-brief`, `#signals`, `#fills`, `#alerts`, `#critical`, `#ops`, `#audit`, plus Phase 2 `#ask-agent`): purpose, message format (full embed schemas with field-by-field), who/what writes, how user interacts.

#### Slash Commands
For each command per phasing table: parameters, response format, permissions, confirmation modals.

#### Button Interactions (Phase 1+)
Signal approval/reject/defer:
- Payload format
- State machine consistent with locked state enumeration
- Confirmation modals on kill-switch invoke
- Decision diary capture on rejections (tag picker + text, min 10 chars)
- Bulk approve "standard" Phase 2

#### Threads (Phase 2)
Per-trade thread for fill updates, agent commentary, operator notes

#### Backend → Bot IPC
Backend posts events to bot's local HTTP listener on Docker internal network. Replay buffer overflow: if bot disconnected from Discord >1h, on reconnect fetches missed events from backend buffer (24h max). If gap > 24h, drop with notice "Discord catch-up incomplete, see web app." External watchdog independently covers VPS-down case.

#### Bot Architecture
- `discord.py` async event loop
- Connection to backend (REST + receives IPC events)
- Stateless preferred; fetches from backend
- Restart/recovery: idempotent re-subscription; replays missed messages from backend buffer (24h limit)

#### Web/Discord Action Parity
For every action surfaced in BOTH (signal approval, kill-switch INVOKE, decision diary entry, ratify, run stress test, query positions/P&L, vacation start/end), spec the shared backend endpoint. Document the **single explicit asymmetry: kill-switch RESUME is web-only**.

### 7. Component Library Inventory
Beyond shadcn/ui defaults, spec custom components:
- Trade row (states from locked enumeration)
- Signal approval card with buttons
- Anomaly badge (icon + tooltip listing anomaly_reasons via locked vocabulary)
- Health score indicator (G/Y/R + expandable; insufficient-data graceful)
- Equity curve chart wrapper (with benchmark overlay support)
- Drawdown chart (underwater plot)
- Attribution treemap or bar
- Stress test result modal (tabbed, six scenarios, summary table per locked columns)
- Stress test progress drawer (async with cancel)
- Decision diary entry form (tag picker + text, min-length validator)
- PR draft preview
- PR rejection feedback modal
- Kill-switch INVOKE button (confirmation, no re-auth)
- Kill-switch RESUME button (re-auth required; web-only)
- Audit log row with expansion + hash-chain integrity badge + backfill-provenance indicator
- Convalescent mode banner
- Vacation mode banner
- Reconciliation status indicator
- Stale-data corner badge vs. paused-state pill
- Environment tag pill (`paper` / `live-small` / `live-scale`)
- Strategy version badge (global, SSE-updated) + per-trade version pill
- External watchdog status indicator
- Operating cost dashboard tile
- Toast variants (P0/P1/P2 per taxonomy with ARIA)
- Empty-state components (full inventory above)
- Browser-unsupported explainer
- Agent status indicator (state enum locked)
- ARIA live region wrapper component (for SSE-driven announcements)

For each: purpose, props, states, accessibility (keyboard nav, screen reader, never rely on color alone — pair with icon/text), tabular-num CSS application.

### 8. Data Fetching and State Strategy
- TanStack Query patterns (staleness, refetch policies per data type — match stale-data thresholds)
- Zustand store organization (narrow client state)
- Optimistic updates: signal approval, decision diary, ratification, vacation toggle (failure UX locked above)
- Cache invalidation rules
- Error boundary placement
- Loading state strategy (skeleton vs. spinner)
- All metrics computed backend-side (health score, attribution, tax, stress test, walk-forward); frontend renders only

### 9. Design Tokens
- Color palette (dark default; semantic tokens; all paired with icon/text)
- Typography scale (monospaced for ALL numbers — JetBrains Mono or Inconsolata; sans for prose — Inter)
- `font-feature-settings: 'tnum'` applied to numeric tabular contexts
- Spacing scale (4px base; dense)
- Animation timing (≤150ms; functional only)
- Density mode (single — dense; not configurable)

### 10. Sequence Diagrams (Mermaid)
At minimum:
- WebAuthn first-run /setup (token-gated): registration with backup code generation
- WebAuthn-unsupported bootstrap fallback (TOTP-only)
- WebAuthn login with re-auth challenge for risk-loosening action
- TOTP backup login flow
- Backup code recovery flow
- Signal arrives → approve via web → backend executes → fill displays via SSE
- Same flow via Discord button (parity)
- Reject signal with decision diary entry (web AND discord)
- Invoke kill switch from Discord (confirmation; no re-auth)
- Invoke kill switch from web (confirmation; no re-auth)
- RESUME from HALT_NEW via web (re-auth required); Discord `/halt` resume attempt → explainer message redirecting to web
- Manual close during HALT_NEW (re-auth required)
- Vacation END via web (re-auth) and via Discord (confirmation modal + reason capture)
- Stress test button → POST 202 + jobId → SSE progress on `agent` channel → terminal payload
- PDF export → POST 202 + jobId → SSE progress → signed download URL (1h TTL, one-time-use)
- PR draft from parameter sandbox → operator-friendly review surface → human reviews → merges via backend → deploys
- PR rejection with feedback modal → reason fed to agent context
- Real-time fill update via SSE (with ARIA announcement)
- Tab eviction: server closes oldest tab on N+1 connection → control event → banner
- VPS outage → external watchdog email → operator manual flow
- Concurrent-tab signal approval conflict → toast revert
- SSE failure → fallback to polling → degraded indicator → SSE retry success
- Optimistic-update network failure → 3 retries → manual retry toast

### 11. Phased Build Plan
- **Phase 0 (frontend weeks 0–3):** scaffold (Next.js, auth + /setup + /login + /recover, basic Today against mock data; other routes 404 with explainer); Discord bot skeleton with `/positions` and `/halt`; integrate live data starting week 3–4
- **Phase 1 (months 2–5):** ships before live trading; per per-page (§2) and Discord-surface (§6) phasing tables
- **Phase 2 (months 5–9):** fills out Phase 2 columns; six additional features (full versions); PR review surface; full Performance + Research + Calendar
- **Phase 3 (months 9–12):** investor PDF generation; CPA reader role plumbing; refinements

Each phase: deliverables, success criteria, kill criteria.

### 12. Testing Strategy
- Component tests (Vitest + React Testing Library) — coverage targets per component category
- E2E critical flows (Playwright): WebAuthn registration + login (with WebAuthn virtual authenticator), signal approval (web + discord), kill-switch invoke (both surfaces) + resume (web only), decision diary entry, ratification, stress test async flow, PR review surface render, PR rejection feedback, vacation start/end (with re-auth on end), manual position close during HALT_NEW (with re-auth), concurrent-tab race / server eviction, optimistic-update failure paths
- Visual regression (Chromatic recommended) for design system consistency
- Accessibility audits (axe-core in CI) — WCAG 2.1 AA target; ARIA live region behavior tested
- Discord bot tests: command response correctness, button payload handling, IPC ingestion, replay buffer behavior
- Cross-environment segregation tests: assert no UI element ever blends `paper` and `live-*` data in a single number or chart (with explicit health-score current-env-scoping carve-out)
- **CI tooling: GitHub Actions; bundle analyzer via `@next/bundle-analyzer` enforced in workflow; PR fails if any bundle exceeds budget by >10%**

### 13. Investor PDF Report Layout (year-2 deliverable)
Renderer: Typst on VPS; charts pre-rendered server-side as SVG via headless Recharts; embedded as images.

Layout:
- Cover page (period, fund/strategy name placeholder, prepared-by, date)
- Performance summary table
- Equity curve and drawdown chart (SVG-embedded)
- Monthly returns table (calendar heatmap)
- Risk metrics (Sharpe, Sortino, max DD, hit rate, vol)
- Attribution summary (by market or strategy)
- Methodology disclosure (one paragraph)
- Risk disclosures (standard CTA-style language placeholder)
- Footer: page numbers, generation timestamp, hash of source data for audit

PDF delivery: async (POST → 202 + jobId; SSE progress on `agent` channel; terminal payload with signed download URL; 1h TTL; one-time use; download logged to audit).

### 14. SLO / Performance Budgets

| Page | JS bundle (gzipped) | Targets |
|---|---|---|
| / (Today) | ≤ 350KB initial | TTI ≤ 2s, LCP ≤ 2.5s, Lighthouse perf ≥ 90 |
| /trades | ≤ 500KB | (table-heavy, virtualized) |
| /performance | ≤ 600KB | (chart libs lazy-loaded) |
| /research | ≤ 800KB | (Phase 2; heaviest) |
| /system | ≤ 500KB | |
| /calendar | ≤ 350KB | |

- Recharts and Lightweight Charts deferred to /performance and /research; never loaded on /today
- p99 SSE event-to-render: ≤ 500ms
- Aggressive code-splitting per route; dynamic imports for heavy components
- Bundle analyzer run in CI; PR fails if budget exceeded by >10%

### 15. Export Taxonomy
- **Trades CSV** — locked schema above
- **Audit CSV** — locked schema above
- **Performance CSV** — locked schema above
- **Tax annual export** — Form 6781, Schedule D, Form 8949 CSVs + PDF summary (annual; January)
- **PDF report** — Performance tearsheet (monthly/quarterly); async delivery
- **Print stylesheet** — Performance and Trades filtered views (locked properties above)

### 16. Observability
- Sentry for error tracking (free tier; upgrade if usage warrants)
- Sentry Performance Monitoring for RUM at low-volume tier
- Frontend error boundary integration with Sentry
- User feedback via Sentry user-feedback widget on errors
- ARIA live region announcements logged via Sentry breadcrumbs (auditability of "what was announced when")

### 17. Security Headers and Browser Hardening (locked)

Backend serves frontend with these headers (Caddy/Traefik config):
- `Content-Security-Policy`: strict; `default-src 'self'`; `script-src 'self' 'wasm-unsafe-eval'`; `style-src 'self' 'unsafe-inline'`; `connect-src 'self' https://sentry.io`; `img-src 'self' data:`; `frame-ancestors 'none'`
- `Strict-Transport-Security: max-age=31536000; includeSubDomains; preload`
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Permissions-Policy: camera=(), microphone=(), geolocation=(), publickey-credentials-get=(self)`

### 18. Staging Environment

- `paper.<domain>` for paper environment (separate hostname; same VPS or staging VPS)
- Production deploys go to `<domain>`; staging deploys go to `paper.<domain>`
- Staging API at `paper.<domain>/api/*` reads from a paper-environment Postgres (separate from live)
- All `staging` deploys auto-tagged in audit; no live broker integration on paper.<domain>

## FORMAT REQUIREMENTS

- Markdown with clear section headers
- Mermaid for ALL diagrams
- Wireframes in text/ASCII/Mermaid (no image generation)
- Concrete library/tool/version recommendations
- Where genuine implementation choices remain, present 2–3 options with tradeoffs and a recommendation
- Length will be substantial; favor completeness over brevity
- Never invent strategic decisions; flag missing context with `[QUESTION FOR OPERATOR: ...]`
- For backend contract dependencies, flag with `[CONTRACT — verify against Prompt A output]` and propose your expected contract using the canonical event/code vocabulary defined in this prompt
- This spec must interlock with Prompt A; reference its REST endpoints, SSE event types, and Discord command schemas by name where Prompt A's output is supplied

Begin.
