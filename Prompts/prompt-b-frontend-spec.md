# PROMPT B — FRONTEND TECH SPEC

## ROLE

You are a senior frontend architect and design engineer with experience building production trading dashboards and operator interfaces for systematic CTAs and prop shops. You understand that trading UIs are utilitarian, dense, and fast — not consumer-app pretty. They resemble Bloomberg, Linear, or a small CTA's research environment, not a startup landing page.

You will produce a comprehensive technical specification for the FRONTEND of a single-operator algorithmic trading system. Implementation will be primarily by Claude Code working with a non-technical solo operator.

**The SPEC is comprehensive (full target shape). The BUILD is phased; the per-page table in §2 and the Discord-surface table in §6 are the binding phase contracts. Phase columns supersede prose where they differ.**

**Workflow note:** this prompt is intended to run AFTER Prompt A (backend spec) has produced its API contract. Ideally the implementer pastes Prompt A's `§4 API Contracts` section into this prompt as additional context where indicated. Where backend contract is genuinely missing, reference the **Expected Backend Contract Defaults** section below — it provides canonical REST paths, SSE payload shapes, and Discord IPC formats so the receiver has defaults rather than inventing from scratch. Flag any divergence with `[CONTRACT — verify against Prompt A]`.

## OPERATOR CONTEXT

- Solo operator, finance background, no coding ability, US-based (NJ), trades alone
- Moves around frequently — must operate from mobile (signal approval, monitoring, queries via Discord) and from desk (research, deep review, parameter changes via web)
- Responsible for own and (eventually) family money
- Wants the simplest possible interface that still surfaces everything when needed

## DOMAIN PLACEHOLDER

Throughout this spec, `<domain>` is a placeholder for the **registrable apex domain** (e.g., `mytrading.com`) — NOT a subdomain. This constraint is binding: WebAuthn `rpID` must equal `<domain>`, and the suffix-matching guarantee (production at `<domain>`, staging at `paper.<domain>`) only holds when `<domain>` is the registrable apex. If operator at Phase 0 wants to host the app at `app.mytrading.com`, then `<domain>` = `mytrading.com` and the public hostname is decoupled from the rpID; production is then served at `app.mytrading.com` and staging at `paper.mytrading.com`, with both sharing rpID = `mytrading.com`. Substitute at deployment.

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

**Surface parity (clarified — asymmetric by design):** parity is partial, not blanket. The asymmetric rule:
- **Risk-tightening AND routine actions:** BOTH surfaces; no re-auth. Includes signal approval, decision diary entry, query commands (positions/exposure/P&L), kill-switch INVOKE, vacation START, manual close during NORMAL.
- **Risk-loosening OR exceptional manual order action:** WEB-ONLY by construction (requires WebAuthn UV which Discord cannot perform). Includes kill-switch RESUME, parameter PR submission, deploy approval, env tag override, backup code regen, tax election toggle, vacation END, manual close during HALT_NEW.

Calendar ratification: Discord in Phase 1; web in Phase 2.

## ARCHITECTURE / TOPOLOGY (LOCKED — SELF-HOSTED)

```
Browser ──── HTTPS ──── <domain> (single VPS, Hetzner Ashburn)
                           │
                           ├── Caddy (reverse proxy, auto Let's Encrypt)
                           │     ├── /api/sse/events → FastAPI (long-lived; flush_interval -1; transport.read_timeout 24h)
                           │     ├── /api/* → FastAPI (127.0.0.1:8000)
                           │     ├── /maintenance → static maintenance page (504 grace fallback)
                           │     └── /* → Next.js Node server (127.0.0.1:3000)
                           ├── Next.js Node server (App Router; SSG shell + CSR data)
                           ├── FastAPI
                           ├── Postgres
                           ├── LEAN engine
                           ├── Claude ops agent
                           ├── Discord bot service
                           └── Discord webhook-pusher service

External watchdog (separate region — Hetzner Helsinki or Falkenstein, NOT Ashburn)
   ├── pings <domain>/health every 5 min
   ├── pushes ping result to <domain>/internal/watchdog (Authorization: Bearer <shared-secret>; secret in sops; rotated quarterly via `services/security/rotate-secrets.sh`; 1h overlap window where both old and new tokens accepted before old invalidated, for graceful handover)
   └── emails operator if unreachable >15 min during CME session
```

**Locked:**
- **Reverse proxy: Caddy** (auto-cert; sufficient feature set)
- **Caddy backend ports:** Next.js on `127.0.0.1:3000`; FastAPI on `127.0.0.1:8000`
- **Caddy SSE config (actual Caddyfile syntax, locked):**
  ```
  handle /api/sse/events {
    reverse_proxy 127.0.0.1:8000 {
      flush_interval -1
      transport http {
        read_timeout 24h
        write_timeout 24h
      }
    }
  }
  ```
- **Caddy `/internal/watchdog` exposure:** IP-allowlisted to watchdog VPS static IP (Caddy matcher `@watchdog remote_ip <static-ip>`) AND Bearer token auth at FastAPI layer. Defense-in-depth: both required.
- **Rendering: Next.js Node server** (App Router; SSG shell + client-side hydration + client-side data fetching). NOT static SPA + nginx.
- **Single origin:** bare `<domain>`. Same-origin cookies. `credentials: 'same-origin'` (default) suffices.
- **WebAuthn `rpID`: registrable apex domain** (= `<domain>`, e.g., `mytrading.com`). Credentials registered at `rpID = <domain>` work at `<domain>`, `app.<domain>`, `paper.<domain>`, etc., via WebAuthn registrable-domain suffix matching. **Single enrollment, both environments — by design** (solo operator simplification). If `<domain>` placeholder is set to a subdomain (e.g., `app.mytrading.com`), the rpID must still be the apex (`mytrading.com`) — see Domain Placeholder section.
- **SSE endpoint canonical path: `GET /api/sse/events`**
- **Maintenance / deploy-time UX:** during planned deploys, Caddy serves `/maintenance` static page (~5KB; generic "back shortly" message); 502 from upstream during unplanned outage triggers `/maintenance` fallback via Caddy `handle_errors`; SSE reconnect storm on recovery handled by client-side jittered backoff (5s + random 0–10s).
- **Frontend ↔ backend version skew:** `GET /api/version` returns `{ backend_version, expected_frontend_version }`; frontend polls on tab focus + every 60s; on mismatch, displays "New version available — refresh" banner with reload button.
- HSTS enabled; security headers per §17.

**SSE transport:**
- Use **`@microsoft/fetch-event-source`** library, NOT native `EventSource`
- Single multiplexed channel; client filters/dispatches by event `type`
- Server enforces N-connection limit per user (default N=4, per-user across devices); on connection N+1, server closes oldest with control event (canonical envelope shape — see SSE Event Format below); browser displays banner. **No client-side cross-tab coordination needed.** Brief auth-only connections (e.g., `/login` from phone) don't usually evict — they auth then disconnect.
- **Web SSE replay buffer: 24h backend retention** (aligned with Discord IPC buffer; both consume from same backend store). Beyond 24h gap, client falls back to full re-fetch of canonical state per page.

## TECH STACK (LOCKED)

- **Next.js 14+ App Router (Node server)** + **TypeScript** strict + **Tailwind CSS** + **shadcn/ui**
- **Caddy** reverse proxy + Let's Encrypt
- **TanStack Query** (server-state) + **Zustand** (client-state)
- **TanStack Table + `@tanstack/react-virtual`** for large tables
- **Date handling: `date-fns` + `date-fns-tz`**; single `formatET(date, format)` helper used everywhere; backend supplies UTC, frontend always converts to `America/New_York` via this helper
- **Chart library assignment (locked, per surface):** see table below
- **`@microsoft/fetch-event-source`** for SSE
- **Sonner** for toasts
- **react-hook-form + zod** for forms
- **Auth:** WebAuthn (passkey) primary + TOTP backup + 8 single-use printed backup codes (10-char base32 in 2 groups of 5; e.g., `ABCDE-FGHIJ`; server-stored as Argon2id hashes)
- **Authorization (RBAC):** schema present from day 1; "owner" role active initially; "reader" role planned for CPA in year 2; investor role NEVER (PDF reports only)
- **Reader role permission matrix (LOCKED — dollar-redaction applied consistently):**
  - **Performance page:** all metrics in **% of starting NAV** (no absolute dollar amounts)
  - **Trades read-only:** per-trade detail visible, but **dollar fields (`realized_pnl`, `expected_pnl`) redacted to % of starting NAV**; `fill_price` and `fill_qty` preserved (needed for tax provenance); decision diary entries authored by **operator only** (agent-authored entries hidden — rationale leak prevention)
  - **Tax widget + Tax CSV exports:** **absolute dollars preserved** (locked exception — tax outputs inherently dollar-denominated; CPA needs them to do tax work)
  - **Stress test:** OWNER-ONLY (locked — risk-strategic content reader doesn't need)
  - **System, Research, Calendar ratification controls, account numbers, strategy code/PR contents, agent prompts/responses:** reader CANNOT view
  - **Reader CANNOT do** any writes
  - **Reader-forbidden routes return 403** with explainer ("Your role does not permit access to this page; contact owner if needed"). NOT 404 — distinguishes "you can't see this" (403) from "this doesn't exist yet" (Phase 0 hidden routes = 404).
- **PDF rendering:** Typst on VPS (layout/typography); charts pre-rendered SVG via headless Recharts. Async (POST → 202 + jobId + SSE progress on `job` channel — see SSE event types below — → terminal payload with signed download URL; 1h TTL; one-time use; download logged to audit).
- **Error tracking + RUM:** **Sentry free tier** (5k errors/month, 10k performance units, 50 replays — sufficient for solo-operator low-volume). Upgrade to Sentry **Team plan ($26/mo)** when monthly volume exceeds free-tier quotas (driven by error rate in production; trip wire = 30-day rolling > 4k errors → upgrade signal).
- **Feature flagging — Phase 1/2/3 gates (LOCKED):**
  - Coarse phase env var: `NEXT_PUBLIC_PHASE=1|2|3` (read at boot)
  - **Per-route availability** via `routes.config.ts`: typed array of `{ path: string, available_from: 0|1|2|3, hidden_in_nav: boolean }`
  - **Consulted by BOTH server (Next.js middleware) AND client (nav component).** Server: route returns 404 if `current_phase < available_from`. Client: nav menu omits routes where `hidden_in_nav: true`.
  - Independent semantics: a route can be `available_from: 1, hidden_in_nav: true` → deep-linkable but not in menu (e.g., `/trades/:id` post-Phase-1 — accessible from Discord deep-link, not in main nav)
  - **Phase transitions: deployment-controlled.** Operator (or Claude Code) edits `routes.config.ts` + `NEXT_PUBLIC_PHASE` env var, deploys via standard procedure. Logged to `audit_log` as `phase_transition_deployed`. NO runtime phase flipping.
  - NO PostHog/LaunchDarkly.
- **Browser support:** latest 2 stable Chrome, Firefox, Safari. Edge implicit (Chromium). WebAuthn detection with explainer.
- **Project layout (locked):** pnpm workspace.
  - `apps/web/` — Next.js + TypeScript
  - `services/discord-bot/` — Python + `discord.py` (separate Dockerfile)
  - `packages/api-types/` — TypeScript types codegen'd from FastAPI's OpenAPI; consumed by `apps/web/`
  - `packages/discord-types/` — Python pydantic schemas mirroring `apps/web` types where shared (manual mirror; minimal surface — IPC payloads only)

For Discord:
- **`discord.py`** bot in `services/discord-bot/`; HTTP-IPC with backend over internal Docker network; auth via shared sops-decrypted secret
- Slash commands + button interactions + embeds + threads (per Phase 2)

### Chart Library Assignment (locked, per surface)

| Surface | Library | Notes |
|---|---|---|
| /performance equity curve | **Lightweight Charts** (TradingView OSS) | Time-series-optimized; lazy-loaded |
| /performance drawdown | Recharts | Underwater plot |
| /performance attribution | Recharts | Bars / treemap |
| /performance monthly heatmap | Recharts | Calendar heatmap |
| /performance actual-vs-rule compare | Lightweight Charts | Dual time-series |
| /research backtest equity | Lightweight Charts | |
| /research walk-forward visualizer | Recharts | **Strip chart** (locked) |
| /research A/B compare | Lightweight Charts | |
| /today exposure rings | Recharts | Bars or radial |
| /today health-score expand | Recharts | Component bars |
| /system operating cost dashboard | Recharts | Provider tiles + 90-day trend |
| /system audit chain integrity | none / inline | Status badge |
| Per-trade detail (Phase 2) | Lightweight Charts | Price + entry/exit markers |
| **PDF exports** | Recharts SVG (server-side via headless renderer) | PDF and UI render the equity curve from different libs (Recharts vs. Lightweight Charts); visual style nearly identical; trade-off documented |

## DESIGN PHILOSOPHY (BINDING)

- Utilitarian, not aesthetic. Bloomberg/Linear/CTA-tool feel. Dense, fast, monospaced numbers, dark default.
- Animations: functional only (state transitions, modal/drawer); ≤150ms; no decorative.
- Mobile = Discord. NO native mobile build, ever.
- **Tablet/mobile policy:** below **1024px viewport**, render "use desktop or Discord" notice with Discord deep-link button.
  - **EXCEPTIONS — accessible at all viewport sizes:** `/login`, `/setup`, `/recover` (operator must be able to authenticate, bootstrap, or recover from any device — phone if laptop is dead).
- Single density (dense). Single theme (dark). Not configurable.
- Numeric formatting: US locale; tabular figures via `font-feature-settings: 'tnum'` (also applied to Inter for prose-numeric contexts; for monospaced fonts already tabular this is a no-op but harmless). Negatives: leading minus + red color + small downward arrow icon (color-blind safe). Positives: green + upward arrow on emphasized values; bare otherwise.
- **Time-zone:** ALL UI in `America/New_York` via `formatET()` helper. Backend stores UTC.
- Numeric precision: read from backend's `instrument_metadata` (see Loading Model below) — never hardcoded.
- **`server_now` format:** RFC 3339 UTC with `Z` suffix and millisecond precision (e.g., `2026-05-04T17:30:00.123Z`). Browser absolute clock NEVER trusted for stale calculations.
- **Stale-data math (locked):** each cached payload stores `received_at_monotonic_ms = performance.now()` at receive time. Stale check uses `performance.now() - received_at_monotonic_ms > threshold_ms`. Browser monotonic clock (`performance.now()`) is trusted for ELAPSED time only, not absolute time. The `server_now` field is used for cross-payload reasoning and for rendering "last update" timestamps in UI.
- **No-events-arriving fallback:** if no SSE event of any type arrives within 60s during CME session, TanStack Query's own staleness detection fires; UI shows degraded indicator; client triggers polling fallback.
- Empty-state visual language: muted text-only with single optional CTA button; no illustrations; austere; "No <noun> yet" + short explainer + optional CTA.

### Design Tokens (LOCKED — reference palette)

Implementer may refine but should anchor to these values:

**Colors (dark default):**
- Background base: `#0a0a0a` (Tailwind `neutral-950`)
- Surface: `#171717` (Tailwind `neutral-900`)
- Surface-elevated: `#262626` (Tailwind `neutral-800`)
- Border: `#404040` (Tailwind `neutral-700`)
- Text primary: `#fafafa` (Tailwind `neutral-50`)
- Text secondary: `#a3a3a3` (Tailwind `neutral-400`)
- Text muted: `#737373` (Tailwind `neutral-500`)
- P&L positive: `#10b981` (Tailwind `emerald-500`)
- P&L negative: `#f43f5e` (Tailwind `rose-500`)
- Severity P0 / critical: `#ef4444` (Tailwind `red-500`)
- Severity P1 / warn: `#f59e0b` (Tailwind `amber-500`)
- Severity P2 / info: `#0ea5e9` (Tailwind `sky-500`)
- Stale-data badge: `#eab308` (Tailwind `yellow-500`)
- Paused-state pill: `#6366f1` (Tailwind `indigo-500`)
- Health green ≥75: `emerald-500`; yellow 50–74: `amber-500`; red <50: `red-500`
- Environment pills: `paper` `sky-500`, `live-small` `amber-500`, `live-scale` `emerald-500`

**Typography:**
- Numbers: **JetBrains Mono** (or Inconsolata fallback)
- Prose: **Inter**
- Sizes: text-xs (0.75rem), text-sm (0.875rem), text-base (1rem), text-lg (1.125rem), text-xl (1.25rem), text-2xl (1.5rem)

**Spacing:** 4px base; Tailwind default scale (0.5/1/1.5/2/3/4/6/8/12/16)

**Animation:** ≤150ms; `cubic-bezier(0.4, 0, 0.2, 1)` (Tailwind `ease-in-out`)

### `instrument_metadata` Loading Model (locked)

- Boot-time bulk fetch: `GET /api/metadata/instruments` returns full table
- Cached in TanStack Query with **24h stale-while-revalidate**
- Updates picked up on next revalidation
- If boot fetch fails: render error banner + block trading-related actions; read-only views still render with "—" for precision-sensitive fields

**Schema (locked):**
```typescript
type InstrumentMetadata = {
  symbol: string;                       // e.g., "/MES", "TLT"
  kind: 'future' | 'etf';
  active_in_universe: boolean;          // per current equity tier
  exclusion_reason: string | null;      // e.g., "single_contract_notional_exceeds_50pct_equity"
  tick_size: string;                    // Decimal-string for precision (e.g., "0.25")
  point_value: string;                  // Decimal-string (e.g., "5.00" for /MES = $5/point)
  multiplier: number;                   // Contract multiplier
  decimals_price: number;               // Display decimals for price
  decimals_qty: number;                 // Display decimals for quantity (typically 0 for futures, 0 for whole shares)
  contract_month?: string;              // e.g., "2026-03"; futures only
  cluster: 'equity_index' | 'commodity' | 'rates_bonds' | 'crypto' | 'fx' | null;
};
```

### Print Paper Size (locked)
**US Letter** (8.5" × 11"), portrait. Operator is US-based.

## INFORMATION ARCHITECTURE — 6 POST-AUTH + 3 PRE-AUTH SURFACES

**Post-auth ("6 pages"):**
1. **Today** (`/`) — landing dashboard
2. **Trades** (`/trades`, `/trades/:id`)
3. **Performance** (`/performance`)
4. **Research** (`/research`, `/research/backtest/:id`)
5. **System** (`/system`, `/system/audit/:id`, `/system/pr/:id`) — includes **Agent Activity section** (NOT a separate top-level page)
6. **Calendar** (`/calendar`)

**Pre-auth surfaces:**
- **`/login`** — WebAuthn login + TOTP fallback + backup-code link (mobile-accessible)
- **`/setup`** — first-run bootstrap with token (mobile-accessible)
- **`/recover`** — backup-code recovery (mobile-accessible)

**Auth callback:** N/A — WebAuthn does NOT use OAuth-style callback. See WebAuthn Ceremony.

NO additional top-level pages. NO investor dashboards. NO mobile-optimized variants. NO separate "Agent" page (agent activity is a section under `/system`).

**Deep-link conventions:**
- Discord-to-web: every Discord embed includes deep link to relevant detail
- Trade rows in Trades table → `/trades/:id` (drawer mode preferred; full-page on direct nav)

## LOCKED STRATEGIC AND SYSTEMS DECISIONS — DO NOT REOPEN

### Strategy and Phasing
- Multi-asset systematic trend-following on micro futures + bond ETFs
- Universe: ~8–12 markets (canonical full target); active universe at any given equity is filtered per Prompt A's per-position-cap-feasibility rule
- Daily bars; signal generation 17:30 ET
- Frontend phasing per per-page table (§2)

### Risk Framework (numbers from Prompt A; frontend renders)
- Vol-targeted sizing, 14% portfolio annualized vol
- Per-position 25% target / 50% hard floor for single-contract override; gross 300%; net 150% (deliberately conservative)
- Cluster caps: equity-index 60%, commodity 80%, rates/bonds 80%, crypto 40%, FX 30%
- Realized cross-portfolio correlation: alert >0.7, halt >0.85
- Daily loss limit -5% of daily-start MTM (17:00 ET)
- Trailing DD -20% (capital-event reset on deposit only)
- Monthly DD -10% triggers vol halving
- Decommission floor per Prompt A (severity=incident_review)

### Vol-Target Multiplier Composition
MIN of multipliers (do NOT compound). Per Prompt A.

### Kill-Switch State Machine
- States: `NORMAL`, `HALT_NEW`, `CONVALESCENT`
- HALT_NEW severity flag: `routine` / `defensive_envelope` / `incident_review`. Frontend renders severity-specific banner text.
- HALT_NEW max dwell: 7 trading days → operator escalation
- IBKR margin-call residual risk possible at HALT_NEW (high used margin)
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
- **Tax election toggle (475(f) for ETFs):** CPA-acknowledgment-gated (in-app modal; operator types verbatim "I have consulted a CPA regarding 475(f) election"); session-credentialed; logged to audit. NO file upload, NO email confirmation.

### Six Stress Test Scenarios

| Scenario | Definition |
|---|---|
| `1σ_down` | Single-day return = -1 × 60-day rolling realized portfolio σ |
| `2σ_down` | -2σ |
| `3σ_down` | -3σ |
| `gfc_2008` | Sep 1, 2008 – Dec 31, 2008 daily returns replayed |
| `covid_2020` | Mar 1, 2020 – Mar 31, 2020 daily returns replayed |
| `crossasset_2022` | Jan 1, 2022 – Dec 31, 2022 daily returns replayed |

Backend computes; frontend renders summary table (locked columns: scenario, P&L impact $, max position-level loss $, DD %, worst-hit market) + per-scenario tab.

### Decision Diary

**Tag vocabulary:** `data_concern` | `regime_concern` | `size_concern` | `manual_judgment` | `other`

**Author enum:** `operator` | `agent` (reader cannot author; reader-mode UI hides authoring controls)

**Input sanitization (locked):**
- Min: 10 characters; Max: 2000 characters
- Allowed character set: printable Unicode (`\p{L}`, `\p{N}`, `\p{P}`, `\p{S}`, `\p{Z}`); control characters disallowed
- XSS strategy: render via React (auto-escapes); store as plaintext UTF-8 in Postgres TEXT column
- Backend validates length + character set on ingestion; rejects with 400 + reason if violated

### Trade State Enumeration

```
pending → approved | rejected | deferred | expired
deferred → pending (next session)
approved → working | expired (if approval window lapses)
working → partially_filled | filled | cancelled
partially_filled → working | filled | cancelled
filled = executed → open_position
partially_filled (cap binding) → capacity_constrained → open_position
open_position → closed (manual or profit-target) | stopped_out (stop hit)
Terminal: closed, stopped_out, rejected, expired, cancelled
```

### Claude Ops Agent Authority Matrix
| Category | Authority |
|---|---|
| Tighten risk via parameter change (next cycle) | AUTO + notify |
| Tighten risk via defensive position trim (mid-session) | AUTO + notify |
| Loosen risk | HUMAN APPROVAL |
| Hot-fix infra (within whitelist per Prompt A) | AUTO-DEPLOY + auto-rollback |
| Strategy logic changes | DRAFTS PR |
| Place orders directly (primary action) | NEVER |
| Invoke kill switch | AUTO on threshold |
| Un-invoke kill switch | HUMAN ONLY (re-auth, web-only) |
| Strategy params within range AND tighten direction | AUTO + auto-revert |
| Reports/alerts/briefings/diagnostics | AUTO |

### Performance Targets
- Phase 1: backtest Sharpe ≥ 1.5, live ≥ 0.8 over first 6 months (cross-phase), max DD ≤ 15%, signal acceptance ≥ 90% (per Prompt A's refined denominator)
- Phase 2: portfolio live ≥ 1.2
- Phase 3: portfolio live ≥ 1.5

### Severity Model
- **P0:** kill-switch fired, broker disconnect, reconciliation tolerance breach, margin auto-trim, audit-log write failure, Defensive Risk Envelope, incident_review HALT_NEW
- **P1:** slippage drift, model decay, capacity warning, anomalous signal flagged, vol regime transition
- **P2:** informational (fills, daily summary, agent reports, ratification reminders, daily liveness probe)

### Anomaly-Flagged Signals — Reason Code Vocabulary

| Code | Meaning |
|---|---|
| `vol_regime_z_high` | Vol regime z-score > 1.5 |
| `capacity_above_alert` | Capacity at 0.5%–2% ADV |
| `recent_decision_diary_concern` | Decision diary concern within 14 days same market |
| `slippage_outlier_recent` | Backtest expected slippage exceeded by > 2× in last 5 trades same market |
| `version_baseline_divergence` | Strategy-version-vs-baseline divergence in last week's golden test |

Backend emits `anomaly_reasons: AnomalyReasonCode[]`. Frontend maps to human-readable tooltip text.

**Phase 1 anomaly handling (revised):** anomaly badge ships in Phase 1 web (Today queued signals AND Discord `#signals` embed) — minor UI element; cheap to add; ensures consistent decision quality across surfaces. Bulk-approve "standard" remains Phase 2.

### Stale-Data Thresholds (locked, per data type)

| Data Type | Stale during CME session | Stale outside CME session |
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
| Operating cost data | 24h | 24h |

Stale indicator: yellow corner badge + tooltip with last-update timestamp.

PAUSED vs. STALE: when state is HALT_NEW, CONVALESCENT, or vacation, intentionally-paused flows display "PAUSED — last activity at X" pill instead of stale badge.

### Re-Auth Requirements (LOCKED — single principle)

**Principle:** WebAuthn UV re-prompt within last 5 minutes is required for **(a) risk-loosening actions**, OR **(b) direct manual order actions while system is in a halt state (HALT_NEW)**. Such actions are **WEB-ONLY by construction** — Discord cannot perform UV.

**Web-only (re-auth required):**
- Kill-switch RESUME (un-invoke)
- Parameter range change PR submission
- Deploy approval
- Environment tag override
- Backup code regeneration
- Tax election toggle
- **Vacation END**
- **Manual position close during HALT_NEW**

**Available from both surfaces (no re-auth):**
- Kill-switch INVOKE — risk-tightening
- Defensive trim invocation — risk-tightening
- Signal approval / reject in NORMAL state — rule-defined flow
- **Manual position close during NORMAL** — risk-reducing routine action; both surfaces; no re-auth (mirrors signal flow)
- Decision diary entry — supporting metadata
- Calendar ratification (Phase 1: Discord; Phase 2: both)
- Stress test run — read-only
- Vacation START — risk-reducing

UV freshness: server-side `last_uv_at` per session; checked on sensitive endpoints.

### CSRF Strategy
SameSite=Strict cookies + double-submit cookie pattern: backend issues `csrf_token` cookie at session start; frontend reads via JS (not HttpOnly for this token) and sends as `X-CSRF-Token` header on POST/PUT/DELETE; backend validates header equals cookie value.

### Session Lifetime
- Idle timeout: 30 min sliding
- Absolute max: 24h from login
- Refresh token: 7 days (within absolute max)
- Cookie max-age = absolute max
- After absolute max: full re-login

### TOTP-Only Bootstrap — Reduced Privileges
If WebAuthn unavailable at `/setup`, TOTP-only enrollment allowed but session has `auth_strength: weak`:
- Cannot perform any re-auth-required action
- Effectively read-only; can view but cannot mutate
- Operator forced to add WebAuthn on first compatible browser to unlock full privileges
- Session badge shows "Reduced — add WebAuthn"
- **Upgrade path (locked):** when operator adds WebAuthn credential while signed in with TOTP-only weak session, the existing session UPGRADES IN PLACE. `auth_strength` flips from `weak` to `strong` server-side; UI reflects on next render. **No re-login required.** Session row's `auth_strength` is mutated atomically alongside the credential registration.

### Strategy Health Score (formula inlined from Prompt A for completeness)

**Scope:** current environment only. Single number cannot blend environments.

| Component | Weight | Window | Score 0–100 |
|---|---|---|---|
| Live Sharpe vs. backtest | 30% | 60-day rolling | 100 if live ≥ backtest; 0 if live < backtest − 2σ; linear |
| Slippage drift | 20% | 30-day rolling | 100 if realized ≤ assumed; 0 if realized ≥ 2× assumed; linear |
| Hit rate vs. expected | 20% | 60-day rolling | 100 if live ≥ expected; 0 if live ≤ expected − 20%; linear |
| Capacity headroom | 15% | current | 100 if avg position < 0.25% ADV; 0 if any > 1% ADV; linear |
| Days since last reconciliation break | 15% | current | 100 if ≥ 30 days; 0 if < 1 day; sqrt-shaped |

Composite = weighted sum. Green ≥ 75, Yellow 50–74, Red < 50.

**Insufficient-data cutoff (locked):**
- Per component: if rolling window has < 50% of expected data points (e.g., < 30 trading days for the 60-day Sharpe component), component renders gray "—" with tooltip showing days available
- Composite: if components representing < 50% of total weight are available, composite renders gray "—" with explainer "insufficient data — track record under construction"
- Otherwise composite re-weights available components to total 100%

Click-expand uses cached payload (server returns components in same payload as composite).

### Concurrent-Tab / SSE Eviction
- Server enforces N=4 connections per user (across all devices/browsers)
- Phone `/login` connects briefly then disconnects after auth — typically doesn't evict desktop tabs
- On connection N+1, server closes oldest with `session_evicted` event (canonical envelope below)
- Phase 1: server-driven eviction only
- Phase 2 enhancement: BroadcastChannel for cross-tab optimistic-update reconciliation

### Optimistic Update Failure UX
1. Apply optimistic state immediately
2. Send to backend
3. On 5xx or network failure: queue locally, retry up to 3× with exponential backoff (1s, 4s, 16s)
4. After 3 failures: toast with manual "Retry" + "Cancel"; revert until user acts
5. On contradicting SSE event during retry: revert + toast

### Bulk-Approve "Standard" (clarified)
Approves all signals in queue that are NOT `anomaly_flagged`. Anomaly-flagged signals remain for individual review. **Disabled when ZERO non-anomaly signals exist; enabled when ≥1 non-anomaly exists, regardless of how many anomaly-flagged also present.** Phase 2 only.

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

### SSE Event Format (canonical envelope, locked)

**Every SSE event:**
```json
{
  "type": "<event_type>",
  "sequence_no": <global_monotonic_int>,
  "server_now": "<RFC 3339 UTC ms-precision>",
  "data": { ...type-specific payload... }
}
```

**Event types (locked):** `signal`, `fill`, `position`, `pnl`, `risk_state`, `health`, `alert`, `audit`, `agent`, `vacation`, `watchdog`, `session_evicted`, **`job`** (long-running job progress — stress tests, PDF exports), **`version`** (backend/frontend skew detection)

**`session_evicted` example (canonical shape):**
```json
{
  "type": "session_evicted",
  "sequence_no": 12345,
  "server_now": "2026-05-04T17:30:00.123Z",
  "data": { "reason": "tab_limit" | "explicit_logout" | "breakglass_kill" | "creds_rotated" }
}
```

**`job` event payload:**
```json
{
  "type": "job",
  "sequence_no": ...,
  "server_now": "...",
  "data": {
    "job_id": "<uuid>",
    "job_kind": "stress_test" | "pdf_export" | "backtest_replay",
    "status": "queued" | "running" | "complete" | "failed",
    "progress_pct": 0-100,
    "result_url": "<signed url, only on complete>",
    "error_message": "<only on failed>"
  }
}
```

### SSE Event Ordering & Replay (LOCKED)
- `sequence_no` GLOBAL monotonic across the multiplexed channel (single sequence space)
- Client tracks last-received `sequence_no` via `last-event-id` header on reconnect
- **Replay semantics:** server replays ALL events since `last-event-id`, NOT filtered. Client filters by `type` after delivery.
- Out-of-order events buffered up to 5s; older arrivals applied in order
- **Backend SSE replay buffer: 24h** (aligned with Discord IPC replay buffer; both consume from the same backend buffer). Beyond 24h gap → full re-fetch of canonical state per page. Resolves the prior 1h-vs-24h asymmetry.

### Polling Fallback (intervals clarified)
- If SSE fails to connect after 3 retries (5s, 15s, 30s backoff), client falls back to **per-resource REST polling** at intervals matching the corresponding stale-data threshold (5s for P&L, 30s for positions, etc.). Backend exposes a `is_session_active` flag in poll responses so client knows whether to use the "during session" or "outside session" threshold.
- UI shows "DEGRADED — polling mode" indicator
- Retry SSE every 60s while in polling mode

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
- Hidden Phase 0/1 routes (404 with explainer + Discord deep-link)
- `/performance` with zero trades: austere ("No trades yet — equity curve will appear after first fill"); NOT a flat NAV line

### Strategy Version Object (locked schema)

Top-bar global badge displays `short_hash` (7-char git SHA prefix) + click opens detail popover.

```typescript
type StrategyVersion = {
  short_hash: string;           // 7-char git short SHA
  full_sha: string;             // 40-char git SHA
  branch: string;               // e.g., "main", "agent/parameter-tighten-vol-target"
  deployed_at: string;          // RFC 3339 UTC
  deployed_by: 'operator' | 'agent';
  deploy_method: 'pr_merge' | 'agent_hot_fix';
  parent_version_short_hash: string | null;
  backtest_baseline_id: string | null;
  parameter_set_hash: string;
  slippage_calibration_version: string;
  decommissioned: boolean;
  decommissioned_reason: string | null;
};
```

Updates via SSE `agent` channel deploy events. Per-trade pill (Trades rows + detail drawer): version active at signal emit time; smaller, muted color.

### Vacation Mode
- Server-enforced (backend refuses new entries; calendar ratification suspended)
- Frontend reads vacation state via SSE; persistent banner with end date and "End vacation" button (re-auth required; web-only)
- Pending working orders cancelled at vacation start; existing positions exit normally

### PR Rejection Feedback Loop
Modal: tag picker (`logic_disagreement` | `risk_concern` | `unclear_rationale` | `bad_test_coverage` | `other`) + free text, min 10 chars. Logged to audit + fed to agent context. PR closed via backend GitHub App.

### Agent Status Indicator States
- `idle` — agent ready
- `working` — agent processing
- `degraded` — Claude API rate-limited or partial outage; read-only mode
- `disabled` — vacation mode or operator-toggled off
- `errored` — unrecoverable error in last hour; auto-recovers; alert raised

### CPA Enrollment Flow (year-2 deliverable, schema present now)

When operator decides to add a CPA reader:
1. Operator (in /system Account Management Phase 2) clicks "Invite Reader"
2. Backend generates one-time setup token for `role: reader`
3. Operator delivers token to CPA out-of-band (e.g., signed encrypted email)
4. CPA visits `/setup` and enters token
5. CPA completes WebAuthn enrollment + TOTP + backup codes (same flow as operator)
6. Reader role active immediately; redaction rules enforced server-side per RBAC

Schema present from day 1 (`role` column in users/sessions tables); functional flow is Phase 3.

## EXPECTED BACKEND CONTRACT DEFAULTS

When Prompt A's `§4 API Contracts` is supplied to the implementer, those values supersede this section. Otherwise, propose the following as defaults and flag with `[CONTRACT — verify against Prompt A]`:

### REST Endpoints (canonical paths)

**Auth:**
- `POST /api/auth/webauthn/challenge` — start login ceremony
- `POST /api/auth/webauthn/verify` — complete login
- `POST /api/auth/webauthn/register/challenge` — start registration (within `/setup`)
- `POST /api/auth/webauthn/register/verify` — complete registration
- `POST /api/auth/totp/verify` — TOTP fallback login
- `POST /api/auth/recover` — backup code recovery
- `POST /api/auth/logout` — invalidate session
- `POST /api/auth/backup-codes/regenerate` — re-auth required

**Signals & trades:**
- `GET /api/signals?status=pending&...` — list signals
- `POST /api/signals/:id/approve` — approve
- `POST /api/signals/:id/reject` — reject (body: decision diary entry)
- `POST /api/signals/:id/defer` — defer
- `POST /api/signals/bulk-approve-standard` — Phase 2; approves all non-anomaly in queue
- `GET /api/trades?...` — list with filters
- `GET /api/trades/:id` — detail
- `POST /api/trades/:id/close` — manual close (re-auth required if HALT_NEW)
- `GET /api/trades/export.csv?...` — CSV export

**System:**
- `GET /api/system/status` — kill-switch state, vacation, watchdog, reconciliation summary
- `GET /api/system/risk-envelope` — current limits (Phase 1 read-only)
- `POST /api/system/risk-envelope/propose` — Phase 2; drafts PR (re-auth)
- `POST /api/system/kill-switch/invoke` — both surfaces; no re-auth
- `POST /api/system/kill-switch/resume` — web-only; re-auth required
- `POST /api/system/vacation/start` — both surfaces
- `POST /api/system/vacation/end` — web-only; re-auth required
- `GET /api/system/audit?...` — audit explorer
- `GET /api/system/audit/export.csv?...`
- `GET /api/system/deployments` — Phase 2
- `POST /api/system/deployments/:id/rollback` — Phase 2; re-auth required
- `GET /api/system/agent-activity` — Phase 2
- `GET /api/system/costs?days=N` — operating cost dashboard
- `GET /api/system/watchdog` — last ping
- `POST /api/internal/watchdog` — watchdog push (Bearer auth, internal)

**Performance / Research / Calendar:**
- `GET /api/performance/equity?env=current|all&from=&to=`
- `GET /api/performance/attribution?...`
- `GET /api/performance/tax-estimate`
- `POST /api/performance/tax-election` — re-auth + CPA acknowledgment
- `POST /api/performance/pdf-export` — async; returns `{ job_id }`
- `GET /api/research/backtests` — Phase 2
- `GET /api/research/walk-forward/:strategy_version` — Phase 2
- `POST /api/research/parameters/propose` — Phase 2; drafts PR
- `GET /api/calendar/events?from=&to=`
- `POST /api/calendar/ratify` — both surfaces

**Health & metadata:**
- `GET /api/health` — for external watchdog
- `GET /api/version` — `{ backend_version, expected_frontend_version }` for skew detection
- `GET /api/metadata/instruments` — instrument metadata bulk

**Stress test & jobs:**
- `POST /api/stress-test/run` — async; returns `{ job_id }`; SSE progress on `job` channel
- `GET /api/jobs/:job_id` — fallback poll if SSE fails

### REST conventions
- All errors: `{ error_code, message, details? }`
- Auth: opaque session ID in HttpOnly cookie + CSRF token in non-HttpOnly cookie + `X-CSRF-Token` header on state-changing requests
- Pagination: cursor-based (`?cursor=...&limit=...`); response includes `next_cursor` and `has_more`
- All timestamps in payloads: RFC 3339 UTC with `Z` suffix and ms precision

## YOUR DELIVERABLE

Produce a complete, production-grade frontend technical specification covering ALL sections below. Use Mermaid for diagrams. Wireframes in TEXT/ASCII/Mermaid (no image generation). Be specific and concrete.

### 1. Information Architecture
- Full IA tree (page → sections → components → states)
- Pre-auth surfaces (`/login`, `/setup`, `/recover`)
- Top nav recommended; defend if differing
- Command palette (cmd-k): pages + corpus = trades by ID/symbol, signals by ID, audit by ID/text
- Keyboard shortcuts: `?` opens cheat-sheet modal; full list documented
- Persistent UI elements (top bar): strategy version badge, health score, current portfolio P&L, agent status indicator, environment tag, current state banner if not NORMAL
- Deep-link conventions

### 2. Screen-by-Screen Specification

For each of 6 post-auth pages: layout, component hierarchy, data displayed (with backend source — endpoint or SSE event type per Expected Backend Contract Defaults), all states, interactions, real-time update behavior, filter/sort/search controls, accessibility.

**Phase 1 surface enumeration per page (binding):**

| Page | Phase 1 ships | Phase 2 adds |
|---|---|---|
| Today | Health score (insufficient-data graceful), positions table, P&L summary D/W/M/Y, exposure breakdown vs. ring + cluster limits, queued signals (individual approve/reject WITH decision diary modal on rejection; **anomaly badge present in Phase 1 — revised**; NO bulk-approve), recent fills feed, P0/P1 alerts, paused-state distinction | Stress test button (six scenarios), anomalies quick-link list, P2 alerts integration, bulk-approve "standard" |
| Trades | Filterable summary table (date/market filters); CSV export; **minimal per-trade detail PAGE at `/trades/:id`** (full-page; basic info: signal, market, direction, status, fill_price, fill_qty, P&L; ensures Discord deep-links don't 404) | Per-trade detail DRAWER (in-table preview), full decision-diary view in Trades, full attribution view, all filters, advanced search |
| Performance | Equity curve (no benchmark overlay yet), monthly returns table; CSV export | Drawdown underwater, attribution, actual-vs-rule compare, tax estimate widget, PDF export, benchmark overlay, print stylesheet, environment-segregation toggle |
| Research | (not in Phase 1) | Backtest viewer, parameter sandbox, regime analysis, A/B compare, walk-forward visualizer (strip chart) |
| System | Kill-switch UI + state, **read-only Risk Envelope tile**, audit log basic table (date + event type + environment filter), reconciliation status (Phase 1 source: QC; Phase 2: TWS + FlexQuery), watchdog status, **minimal Account section: regenerate backup codes (re-auth)** | Risk envelope + propose-PR, deployments log + rollback, agent activity feed, full audit explorer with FTS + actor + hash-validity + repaired-events filters, operator-friendly PR review surface, convalescent banner refinements, operating cost dashboard, full operator account management |
| Calendar | Read-only event list (next 30 days) | Tomorrow's ratification flow on web (Phase 1 ratification is Discord-only), holidays, contract expiration / roll schedule, manual event log |

#### Today (full target)
- Health score (G/Y/R) prominent + click-expand using cached payload
- Current positions table (compact, monospace, virtualized if >50 rows)
- P&L summary (D/W/M/Y) with benchmark comparison
- Exposure breakdown (gross / net / per-market / per-cluster) visualized against ring + cluster limits
- Queued signals — quick approve/reject inline; **anomaly badge in Phase 1**; rejection opens decision diary modal
- Recent fills feed (live via SSE event type `fill`; ARIA-announced)
- Active alerts (P0 → P1 → P2 sorted)
- Stress test "run now" button (Phase 2; async via `job` SSE channel)
- Quick links to anomalies (Phase 2)

#### Trades (full target)
- Unified table (TanStack Table + `@tanstack/react-virtual`)
- Filters: date range, market, strategy version, regime, signal type, environment (never blended)
- Per-trade detail drawer (Phase 2): full lifecycle, decision diary (operator-authored visible to reader; agent-authored visible to owner only), attribution, agent commentary, linked audit entries, stress-test impact
- Server-side pagination + filter pushdown
- Expected scale: ~50–200 trades/month; 5-year ~3k–12k
- CSV export per locked schema (§15)

#### Performance (full target)
- Equity curve with benchmark overlay (SPY default; configurable Phase 2) — Lightweight Charts
- Drawdown chart (underwater plot) — Recharts
- Monthly returns calendar heatmap — Recharts
- Attribution by market, signal type, vol regime, trend regime — Recharts
- Rolling Sharpe, rolling DD, rolling hit rate (60-day default)
- Actual vs. rule-following P&L compare — Lightweight Charts
- Tax estimate widget (click-expand; election toggle CPA-acknowledgment-gated; reader sees full dollar detail)
- Environment-segregation rule: charts default to current environment; "Show all environments (segregated)" toggle renders separate stacked panels per environment; never blended
- PDF export (async via `job` SSE channel; Typst + Recharts SVG)
- Print stylesheet (US Letter portrait): page-break-inside: avoid; header includes period + prepared-by; footer includes generation timestamp. **Trigger: explicit "Prepare for print" button.**
- CSV export per locked schema

#### Research (Phase 2)
- Backtest result loader from CLI-generated artifacts
- Equity curve, trade list, statistics
- Parameter sandbox: propose change → drafts PR via backend; ranges from Prompt A's Parameter Ranges Table; in-range / out-of-range visual indicator
- Regime analysis
- A/B comparison
- Walk-forward visualizer: **strip chart** (Recharts)

#### System (full target)
- **Risk Envelope tile (Phase 1 read-only; Phase 2 add propose-PR):** displays all numeric limits from Prompt A's Risk Rings + Cluster Caps + Parameter Ranges. Phase 1 read-only.
- Kill switch: status, history, manual invoke (confirmation modal; NO re-auth — risk-tightening), recovery flow (RESUME requires re-auth, web-only); incident_review HALT_NEW: red banner + resume disabled until post-incident review write-up
- Convalescent mode banner (sessions remaining + effective vol target + countdown)
- Vacation mode banner (end date + end button; web-only end with re-auth)
- Deployments log (Phase 2): every deploy with diff view + rollback (re-auth)
- **Agent Activity section** (NOT a separate page; lives under /system Phase 2): drafted PRs, hot-fixes, alerts, decisions; expandable to show prompt + response
- **Operator-friendly PR review surface (Phase 2):**
  1. Plain-English summary (≤200 words; backend computes; cached on PR record)
  2. Risk impact summary (auto-generated; backend computes)
  3. Backtest delta (LEAN-authoritative; backend runs LEAN; cached on PR record; stale handling: if underlying calibration changes, banner shows "stale; recompute" button which re-runs LEAN)
  4. Test results
  5. Files affected
  6. Diff (collapsed; sourced from GitHub via backend's GitHub App)
  7. In-app Approve / Reject / Request Changes (sync via backend GitHub App)
  - On Reject: feedback modal
- Audit explorer:
  - Phase 1: cursor-paginated table; filters = date + event type + environment
  - Phase 2: add FTS on `reason`, actor filter, hash-validity filter, repaired-events filter; virtualized infinite scroll; hash-chain integrity badge; backfill-provenance indicator
- Reconciliation status:
  - Phase 1: source = QC brokerage state (via QC API + audit ingestion)
  - Phase 2: source = TWS API real-time + FlexQuery EOD
- External watchdog status: data path watchdog → backend `/internal/watchdog` (Bearer auth) → `system_state` → frontend reads `GET /api/system/watchdog`
- Operating cost dashboard (Phase 2): provider tiles
- Account management:
  - Phase 1 minimal: regenerate backup codes (re-auth required)
  - Phase 2 full: revoke all sessions, manage TOTP enrollment, view auth audit log, **invite reader** (year-2 use)

#### Calendar (full target)
- 30-day forward view (tier 1/2/3, color + icon)
- Tomorrow's ratification: must be ratified by 23:00 ET; if not, hard halt next session until ratified (Phase 1: Discord-primary; Phase 2: web-primary)
- Contract expiration / roll schedule (futures only)
- Exchange holidays
- Manual event log

#### Pre-auth Surfaces

##### `/login`
- WebAuthn login (Ceremony below)
- TOTP fallback (collapsed by default)
- Backup code link → `/recover`
- Browser unsupported explainer
- Mobile-accessible

##### `/setup` (first-run bootstrap, security-hardened)
- **Token NOT in URL.** Backend prints one-time token to stdout at first boot via structured log line: `[SETUP_TOKEN] <token>` (greppable). Token persisted in Postgres `setup_tokens` table with `consumed_at` field. **Token regenerated on every boot if previous token is unconsumed**, invalidating the old one (limits exposure window). Operator visits `/setup` (no query params); enters token in **password-style form field** (NOT visible in plaintext, NOT in browser history, NOT in Caddy access logs).
- Backend `/api/setup/verify-token` validates; rate-limited (5 attempts then 1h lock); on success marks `consumed_at`
- On success, wizard:
  1. Enroll WebAuthn passkey (or TOTP-only with prominent warning if WebAuthn unsupported; reduced session privileges)
  2. Enroll TOTP (QR + manual entry)
  3. Generate 8 single-use backup codes (10-char base32 in 2 groups of 5; format `ABCDE-FGHIJ`); force download/print acknowledgment (operator types "I have saved my backup codes" verbatim)
  4. Confirm; redirect to `/`
- Mobile-accessible

##### `/recover`
- Backup-code entry (single-use)
- Successful → reset WebAuthn + TOTP enrollment; regenerate backup codes
- Failed: rate-limited; lock 1h after 5 fails
- "All factors lost" path: escalation message with `dba_breakglass` procedure contact
- Mobile-accessible

#### WebAuthn Ceremony

WebAuthn is a JS-driven `navigator.credentials.*` flow. NO OAuth-style redirect with `state`.

**Login flow:**
1. User clicks "Sign in with WebAuthn" on `/login`
2. Frontend captures intended `targetUrl` (default `/`)
3. Frontend POSTs `{ targetUrl }` to `/api/auth/webauthn/challenge`
4. Backend generates challenge; stores `(challenge, targetUrl)` in server-side session row keyed by transient ceremony ID; returns `{ ceremonyId, challengeBase64, allowedCredentials }`
5. Frontend calls `navigator.credentials.get({ publicKey: { challenge, allowCredentials, rpId: <domain> } })`
6. Browser prompts user for passkey
7. Frontend POSTs assertion to `/api/auth/webauthn/verify` with `{ ceremonyId, assertion }`
8. Backend verifies; sets session cookie; returns `{ targetUrl }` from server-side session
9. Frontend client-side `router.push(targetUrl)`

**Registration flow:** analogous with `navigator.credentials.create()`.

`rpID = <domain>` (parent registrable domain). Same credentials work at `<domain>` and `paper.<domain>` via WebAuthn registrable-domain suffix matching.

### 3. Six Locked Additional Features
- **Decision diary** (Phase 1: rejection-flow modal in /today AND in Discord; Phase 2: Trades queryable surface). Tag vocabulary per locked enum. Min 10 chars.
- **Actual vs. rule-following P&L compare:** dual equity curves; rolling 30-day divergence; alert at 5%
- **Strategy health score:** locked formula
- **Benchmark overlay:** SPY default; configurable Phase 2
- **Tax estimate widget:** YTD, 1256 60/40, wash sale flagging; nightly cron; click-expand. CPA-acknowledgment-gated election toggle.
- **Stress test:** async on Today via `job` SSE channel; six scenarios; modal with tabbed view + summary table

### 4. Real-Time Update Mechanism
- Single multiplexed `/api/sse/events` via `@microsoft/fetch-event-source`
- Per-page update strategy
- **Polling fallback:** SSE fails after 3 retries → REST polling per-resource at intervals matching stale-data thresholds (using backend-supplied `is_session_active` flag — locked: backend computes from CME futures session hours Sun 18:00 ET → Fri 17:00 ET with daily 17:00–18:00 maintenance break; clients consume the boolean, never compute it); "DEGRADED — polling mode" indicator; retry SSE every 60s
- Reconnection: exponential backoff with jitter (5s + random 0–10s); resume via `last-event-id` header
- Multi-tab: server-side eviction
- Retry/backoff on 429: exponential with jitter; max 5 retries

### 5. Auth and Session Management
- WebAuthn ceremony (Mermaid)
- TOTP backup flow
- 8 single-use backup codes (locked format: 10-char base32, 2 groups of 5; Argon2id-hashed server-side)
- TOTP-only bootstrap reduced privileges
- Session: opaque session ID in HttpOnly + Secure + SameSite=Strict cookie; CSRF token in non-HttpOnly cookie; double-submit; server-side `last_uv_at`
- Lifetime: 30 min idle / 24h absolute / 7d refresh
- Re-auth (WebAuthn UV within 5 min) per principle (web-only by construction)
- RBAC: owner active; reader planned (matrix above; redaction enforced server-side)
- Account recovery via backup codes
- CPA enrollment flow (year-2)
- `/setup` token entered in form field (NOT in URL)

### 6. Discord Bot Specification

#### Surface Phasing (binding)

| Surface | Phase 0 | Phase 1 | Phase 2 |
|---|---|---|---|
| `/positions` | ✓ | full | refinements |
| `/halt` (kill-switch INVOKE; resume not supported via Discord) | ✓ | full | — |
| `/pnl [today\|wtd\|mtd\|ytd]` | — | ✓ | — |
| `/exposure` | — | ✓ | — |
| `/calendar` | — | ✓ | — |
| `/last-fills [n]` (default 10, max 50) | — | ✓ | — |
| `/ratify` | — | ✓ | — |
| `/health` | — | ✓ | — |
| `/vacation start [days]` | — | ✓ | — |
| `/vacation end` | — | **NOT supported via Discord** (web-only) | — |
| `/close [trade_id]` (manual close during NORMAL only; HALT_NEW close is web-only) | — | ✓ | — |
| `/report [period]` | — | — | ✓ |
| `/ask <query>` | — | — | ✓ |
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

#### Web/Discord Action Parity
**Risk-tightening or rule-defined-flow actions: BOTH surfaces.** Risk-loosening or manual-order-during-halt: WEB-ONLY by construction. The web-only set: kill-switch RESUME, parameter PR submission, deploy approval, env tag override, backup code regen, tax election toggle, vacation END, manual close during HALT_NEW.

### 7. Component Library Inventory
Beyond shadcn/ui defaults, spec custom components:
- Trade row (states from locked enumeration including `cancelled`)
- Signal approval card with buttons
- Anomaly badge (ships Phase 1; persists thereafter; tooltip listing `anomaly_reasons`)
- Health score indicator (G/Y/R + expandable; insufficient-data graceful)
- Equity curve chart wrapper (Lightweight Charts; benchmark overlay support)
- Drawdown chart (Recharts; underwater plot)
- Attribution treemap or bar (Recharts)
- Stress test result modal (tabbed, six scenarios, summary table)
- Stress test progress drawer (async with cancel; `job` SSE channel)
- PDF export progress drawer (same `job` channel, different `job_kind`)
- Decision diary entry form (tag picker + text, min-length validator)
- PR draft preview
- PR rejection feedback modal
- Kill-switch INVOKE button (confirmation, no re-auth)
- Kill-switch RESUME button (re-auth required; web-only)
- Audit log row with expansion + hash-chain integrity badge + backfill-provenance indicator
- Convalescent mode banner (severity-aware; `incident_review` red variant)
- Vacation mode banner (end date + web-only end button)
- Reconciliation status indicator (Phase 1 QC source / Phase 2 TWS+FlexQuery source)
- Risk envelope read-only tile (Phase 1) / propose-PR (Phase 2)
- Stale-data corner badge vs. paused-state pill
- Environment tag pill
- Strategy version badge (global, SSE-updated) + per-trade version pill
- External watchdog status indicator
- Operating cost dashboard tile (Phase 2)
- Toast variants (P0/P1/P2 per taxonomy with ARIA)
- Empty-state components
- Browser-unsupported explainer
- Agent status indicator (state enum locked)
- ARIA live region wrapper
- "Prepare for print" button
- CPA acknowledgment modal (verbatim text capture)
- TOTP-only weak-session badge
- Maintenance page (static; served by Caddy on 502)
- Version-skew banner ("New version available — refresh")

For each: purpose, props, states, accessibility, tabular-num CSS application.

### 8. Data Fetching and State Strategy
- TanStack Query patterns (staleness, refetch policies per stale-data thresholds)
- **Cache layer (locked): in-memory only Phase 1.** TanStack Query default in-memory cache; cold reload re-fetches everything including `instrument_metadata`. Phase 2+ may add IndexedDB persistence (`@tanstack/query-async-storage-persister`) for offline tolerance; not Phase 1.
- `instrument_metadata` boot-time bulk fetch with 24h SWR caching (in-memory; re-fetched on cold load)
- Zustand store organization
- Optimistic updates per failure UX rule
- Cache invalidation rules
- Error boundary placement
- Loading state strategy
- All metrics computed backend-side; frontend renders only

### 9. Design Tokens
Per the locked palette and scales above. Spec tailwind.config.ts with token mappings.

### 10. Sequence Diagrams (Mermaid)
At minimum:
- WebAuthn registration on /setup (token entered in form field; with backup code generation)
- WebAuthn-unsupported bootstrap fallback (TOTP-only with reduced privileges)
- WebAuthn login (corrected ceremony — JS API, NO `/auth/callback`)
- TOTP backup login flow
- Backup code recovery flow
- Signal arrives → approve via web → backend executes → fill displays via SSE (with ARIA announcement)
- Same flow via Discord button (parity)
- Reject signal with decision diary entry (web AND discord)
- Invoke kill switch from Discord (confirmation; no re-auth)
- Invoke kill switch from web (confirmation; no re-auth)
- RESUME from HALT_NEW via web (re-auth required); Discord `/halt` resume attempt → explainer redirect
- Manual close during NORMAL (both surfaces; no re-auth)
- Manual close during HALT_NEW (web-only; re-auth required)
- Vacation END (web-only, re-auth); Discord `/vacation end` → explainer
- Stress test → POST 202 + jobId → SSE on `job` channel → terminal payload
- PDF export → POST 202 + jobId → SSE on `job` channel → signed download URL
- PR draft → review surface → human reviews → merges via backend → deploys
- PR rejection with feedback modal → reason fed to agent context
- Real-time fill update via SSE (with ARIA)
- Tab eviction: server closes oldest tab → `session_evicted` control event → banner
- VPS outage → external watchdog email → operator manual flow
- Concurrent-tab signal approval conflict → toast revert
- SSE failure → fallback to polling → degraded indicator → SSE retry success
- Optimistic-update network failure → 3 retries → manual retry toast
- Phase 1 reconciliation status using QC brokerage source
- HALT_NEW (incident_review): red banner + resume disabled until post-incident write-up
- Frontend ↔ backend version skew detection: `/api/version` mismatch → "New version available" banner
- Maintenance page during planned deploy: Caddy serves `/maintenance` while upstream cycles
- CPA enrollment flow (year-2)

### 11. Phased Build Plan
- **Phase 0 (frontend weeks 0–3):** scaffold (Next.js, auth + /setup + /login + /recover, basic Today against mock data; other routes 404 via `routes.config.ts`); Discord bot skeleton with `/positions` + `/halt`; live data integration starting week 3–4
- **Phase 1 (months 2–5):** ships before live trading; per per-page (§2) and Discord-surface (§6) phasing tables. Includes Phase 1 read-only Risk Envelope tile, Phase 1 minimal Account section, **anomaly badge in Phase 1 web**, manual close via `/close` Discord command (NORMAL only).
- **Phase 2 (months 5–9):** fills out Phase 2 columns; six additional features (full versions); PR review surface; full Performance + Research + Calendar; bulk-approve "standard"; CPA enrollment plumbing
- **Phase 3 (months 9–12):** investor PDF generation refinements; CPA reader role activated; refinements

Each phase: deliverables, success criteria, kill criteria.

### 12. Testing Strategy
- Component tests (Vitest + React Testing Library)
- E2E (Playwright with WebAuthn virtual authenticator): full critical-flow inventory above
- Visual regression (Chromatic)
- Accessibility (axe-core in CI; WCAG 2.1 AA; ARIA live region behavior)
- Discord bot tests (command response, button payloads, IPC ingestion, replay buffer)
- Cross-environment segregation tests (with health-score current-env-scoping and tax-artifact reader-redaction-bypass carve-outs)
- **PDF-vs-UI equity curve parity test:** at fixed sample data, render PDF (Recharts SVG) and UI (Lightweight Charts) equity curves; visual regression tolerance ≤ 5% pixel difference (allows legitimate library differences; catches major divergence). Run weekly in CI.
- **Reader role redaction tests:** assert dollar fields converted to %-of-NAV in Trades/Performance for reader; assert tax artifacts retain $; assert stress test returns 403 to reader; assert decision diary agent-authored entries hidden from reader
- CI: GitHub Actions; bundle analyzer via `@next/bundle-analyzer`; PR fails if bundle exceeds budget by >10%

### 13. Investor PDF Report Layout (year-2)
Renderer: Typst on VPS; charts pre-rendered as SVG via headless Recharts.

Layout per spec above; async delivery via `job` SSE channel.

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
- Aggressive code-splitting per route
- Bundle analyzer in CI; PR fails if >10% over budget

### 15. Export Taxonomy

**Trades CSV columns (corrected — no audit-chain footer; trades are not the audit chain):**
`signal_uuid, signal_emit_time_utc, signal_emit_time_et, market, direction, signal_type, strategy_hash, parameter_set_hash, slippage_calibration_version, environment_tag, anomaly_flagged, anomaly_reasons, status, approved_by, approved_at, expected_pnl, expected_slippage, vol_regime_at_emit, trend_regime_at_emit, fill_qty, fill_avg_price, realized_pnl, realized_slippage, holding_days, decision_diary_tag, decision_diary_text, decision_diary_author, capacity_constrained, audit_chain_anchor_hash`
Footer: `record_count, exported_at, audit_chain_anchor_at_export` (single hash anchoring this export to a known audit-chain state at export time, NOT chain start/end of the trades themselves).

**Audit CSV columns (chain footer correct here):**
`sequence_no, event_uuid, timestamp_utc, monotonic_ns, event_type, actor, environment_tag, payload_json, prev_hash, record_hash, repaired_for_sequence_no, source_clock_ts, ingest_clock_ts`
Footer: `chain_start_hash, chain_end_hash, record_count, exported_at` (correct here — audit IS the chain).

**Performance CSV columns:**
`month, return_pct, drawdown_pct, sharpe_60d, hit_rate, trade_count, environment_tag`

**Tax annual export:** Form 6781, Schedule D, Form 8949 CSVs + PDF summary; column lists deferred to spec output

**PDF report:** Performance tearsheet (monthly/quarterly); async via `job` channel

**Print stylesheet:** Performance and Trades filtered views; explicit "Prepare for print" button trigger; US Letter portrait

### 16. Observability
- Sentry free tier for errors; Performance Monitoring upgrade at >100k events/month
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

- `paper.<domain>` for paper environment
- Production deploys → `<domain>`; staging → `paper.<domain>`
- Same WebAuthn `rpID = <domain>`; credentials work at both via registrable-domain suffix matching
- Staging API at `paper.<domain>/api/*` reads paper-environment Postgres
- All staging deploys auto-tagged in audit; no live broker integration on `paper.<domain>`

## FORMAT REQUIREMENTS

- Markdown with clear section headers
- Mermaid for ALL diagrams
- Wireframes in text/ASCII/Mermaid (no image generation)
- Concrete library/tool/version recommendations
- Where genuine implementation choices remain, present 2–3 options with tradeoffs and a recommendation
- Length will be substantial; favor completeness over brevity
- Never invent strategic decisions; flag missing context with `[QUESTION FOR OPERATOR: ...]`
- For backend contract dependencies, reference the Expected Backend Contract Defaults section above; if Prompt A's `§4` is supplied, that supersedes; otherwise flag divergence with `[CONTRACT — verify against Prompt A]`

Begin.
