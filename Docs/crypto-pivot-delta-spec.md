# Crypto-Perps Pivot — Backend/Frontend Delta Spec

**Date:** 2026-07-08 · **Status:** DRAFT for operator review · **Authority chain:** `Docs/crypto-perps-strategy.md` (incl. Amendments A/B) → `research/crypto_perps/REPORT.md` (validation PASS) → `Docs/decisions-log.md` 2026-07-08 entries.

This is a **delta** against `Docs/backend-spec.md` / `Docs/frontend-spec.md`, not a rewrite. Anything not named here is **unchanged**. It supersedes the entire 2026-05-12 → 2026-05-24 IBKR/LEAN Phase-1 architecture chain in `Docs/recent-architecture-changes.md`.

---

## 0. The pivot in one paragraph

The system stops trading CME micro-futures via IBKR/LEAN and starts trading **Coinbase CFM US perpetual-style futures (nano BTC, nano ETH)** via the **Coinbase Advanced Trade API**, running the validated vol-targeted trend strategy at the Amendment B profile, fully automated (no per-trade approval), with Discord visibility and the existing frontend. The platform chassis — FastAPI api, hash-chained audit log, SSE, kill-switch FSM, Discord bot + webhook pusher, reconciliation engine, auth, frontend shell — is retained. The broker layer, data layer, signal engine, and daily-cycle scheduling are replaced.

## 1. RETIRE (delete or decommission; concrete paths)

| Surface | Paths | Disposition |
|---|---|---|
| IBKR client/adapter | `services/execution/ibkr_client.py`, `services/execution/ibkr_adapter.py` | Replace (§3.1). `[A02]` label required — these are forbidden paths. |
| IBKR Gateway | `docker-compose.yml` `ib_gateway`, `docker-compose.live.yml` `ib_gateway`, `deploy/ibkr/` | Delete service + docs. |
| IBKR recon fetchers | `services/reconciliation/flex_query_fetcher.py`, `services/reconciliation/ibkr_intraday.py` | Replace (§3.5). Diff/apply/scheduler (`recon.py`, `apply.py`, `eod_cycle.py`, `scheduler.py`) are KEPT. |
| LEAN | `lean/`, `infrastructure/lean_local/`, `deploy/lean_local/systemd/*` (universe-synthesis 21:00, restart 21:10 timers), `docker-compose.yml` `lean_local` | Delete. |
| LEAN signals ingress | `services/api/routes/internal/lean.py` (`POST /api/internal/lean/signals`) | Delete route + its shared-bearer locked decision. Signals are now generated in-process (§3.3), not POSTed. |
| bar_sync + CME data | `services/data/bar_sync.py`, `bar_sync_alerts.py`, `services/api/bar_sync_status.py`, `services/data/map_file_synthesis.py` | Delete. Replaced by §3.2. |
| QC adapter | `services/qc_adapter/` (dormant since 2026-05-12), `qc_adapter_cursor` table | Delete code; table dropped in a later cleanup migration (keep data until then). |
| CME strategy + calendar | `strategies/v1_trend_following/`, `services/scheduler/calendar_import.py` | Delete. Crypto trades 24/7; the only calendar inputs are the CDE Friday close + quarterly maintenance windows (§3.4). `vacation.py` KEPT. |
| IBKR operator tools | `scripts/operator_tools/{trigger_v1_cycle,recon_positions_probe,replace_protective_stop,replay_executions,master_client_id_probe,recovery_agent}.py` | Delete/replace with Coinbase equivalents as needed in Phase C1. |
| Locked decisions (dev-guide §1.5) | IBKR clientId allocations; "LEAN authoritative for backtest delta"; bar_sync block; Phase-1 IBKR architecture block | Marked RETIRED; replacements in §6. |
| Skills/runbooks | `eod-recon`, `trigger-v1-cycle`, FlexQuery runbook doc | Retire/rewrite in Phase C2. |

**Decommission timing:** at Phase C0 start, stop the `lean_local`/`bar_sync` systemd timers and `ib_gateway` container and cancel any open paper orders — the CME paper system is retired *before* the new build, not run in parallel (operator decision 2026-07-08). The IBKR paper account's final state is archived via one last EOD recon + audit event `strategy_retired` (existing free-text payload; no new enum).

## 2. KEEP UNCHANGED (the chassis)

`services/audit/**` (writer, chain, verify, decision diary — §5.1 pattern), `services/api/**` core + SSE (`services/api/sse.py`, envelope + 14 event types — **no new SSE types needed**, see §3.7), auth stack, `services/webhook_pusher/**`, `services/discord_bot/**` shell, `services/risk/state_machine.py` FSM shell (one change, §3.4), `services/reconciliation/{scheduler,recon,apply,eod_cycle}.py`, `services/calibration/**`, `services/{monitoring,observability,version}/**`, `watchdog/**`, alembic history, and all frontend generic surfaces (`/system` tiles, auth pages, `/performance`, decision diary, `components/ui/*`). All dev-guide §11 anti-patterns stand except `[A13]` (revised, §6).

## 3. ADD / REPLACE

### 3.1 Broker adapter — `services/execution/coinbase_client.py` + `coinbase_adapter.py`
- `coinbase-advanced-py` SDK; REST `/api/v3/brokerage` for orders (`product_type=FUTURE`, CDE perp-style symbols discovered at runtime — never hardcoded, product IDs encode expiry), WS `user` channel for fills, `futures_balance_summary` for equity/margin.
- Execution ladder per strategy §5: post-only limit at touch → 10 min → cross at mid±5bps IOC → market. `client_order_id` = deterministic hash (date, asset, decision-seq) — idempotent crash recovery.
- Native stop-limit management: place/replace 3×ATR backstop within 10 s of any position-opening/expanding fill; verified via `list_orders` (small-live gate A2).
- **Do not opt in** to intraday margin (strategy §7) — one overnight margin regime, 24/7.
- Same file-tree position as the IBKR adapter ⇒ inherits `[A02]` forbidden-path protection. `execution/types.py` DTOs reused where broker-agnostic.

### 3.2 Market data + funding telemetry — `services/data/coinbase_market_data.py`
- WS `ticker` (mark for the 30 s risk loop) + REST candles for daily bars sampled 00:00 UTC (signal input = spot `BTC-USD`/`ETH-USD` per strategy §4; execution references live perp quotes).
- **Funding logger from day one** (REPORT F-3 rider): persist hourly CDE funding per product to a new `funding_rates` table; nightly comparison vs the parametric model feeds gate B3.
- Data-staleness watchdog: mark stale > 3 min ⇒ strategy §7 outage policy (protected-hold if native stop confirmed resting, else flatten).

### 3.3 Signal engine — `services/signal/crypto_trend.py`
- Ports S1–S4 + Amendment B semantics (hysteresis-**hold**, dd-tiers removed, band-edge rebalancing) from `research/crypto_perps/backtest.py`, which is the **reference implementation**; a parity test (same input bars ⇒ identical integer-contract targets) is a Phase C0 exit gate — the same trust-bridge discipline LEAN had.
- Runs in-process in a `strategy_worker` container: daily decision at **00:05 UTC**; risk loop every 30 s (client 2×ATR stops, liquidation-buffer check, halt check).
- `services/signal/**` is a forbidden path ⇒ all strategy-logic PRs need `risk-review-approved`. Correct and intended.
- Sizing precedence codified per REPORT F-4: §7 per-trade risk cap **overrides** §6 vol-target output.

### 3.4 Risk engine deltas — `services/risk/**`
- New `parameter_sets` seed with Amendment B values: `V_TARGET="0.80"`, `PER_TRADE_RISK_FRAC="0.05"`, `DAILY_LOSS_LIMIT="-0.08"`, `WEEKLY_LOSS_LIMIT="-0.16"`, `GROSS_CAP="2.0"`→**"3.0"**, `HALT_EQUITY_USD="1500"`, `HYSTERESIS_HOLD="true"`, `BAND_EDGE="true"`, ETH min-price `"2000"`, lockout/stop/ATR constants per strategy §4–§5. All Decimal-as-string (`[A05]`).
- Kill-switch FSM: states/severities unchanged; the CONVALESCENT auto-graduation criterion "5 clean **CME sessions**" becomes "5 clean **UTC calendar days**" (crypto has no sessions). One-line policy change + test, inside `[A02]`.
- Sizing module: CME contract math → nano-contract math (`contract_size × mark`), integer rounding, per-asset 1.4×E / gross 3.0×E caps, §5.6/§5.7 canonical patterns preserved in shape.
- Halt at $1,500 sets the existing persistent HALTED flag; restart remains manual-flag-removal.

### 3.5 Reconciliation — fetcher swap only
- New `services/reconciliation/coinbase_fetcher.py`: `list_positions` + fills + `futures_balance_summary` (+ funding settlements). EOD cycle moves to **00:15 UTC** (after the daily decision); intraday probe reuses the REST position endpoint. Diff tolerances re-based in Phase C1 config. FlexQuery XML path gone.

### 3.6 Cash-yield worker — `services/risk/cash_manager.py` (new; Amendment B build requirement)
- Daily target: futures margin + buffer (25% of gross notional) stays at CFM; excess swept to the yield instrument; reclaim same-day on margin calls via `schedule_futures_sweep`. Instrument + realized rate are **open question #1** (§7) — ships OFF by default, enabled once verified. Every sweep → audit event (existing `capital_event` taxonomy) + `cash_sweeps` table row.

### 3.7 API / SSE / DB deltas
- **No new SSE event types** (avoids `[A03]` migration): funding accruals and sweeps surface through existing `pnl`/`position`/`audit` invalidations.
- New tables (one migration, with `downgrade()` per `[A16]`): `funding_rates`, `cash_sweeps`, `product_metadata` (CDE product snapshot: tick size, margin %, contract size — refreshed daily; REPORT rider: auto-demotion on fee/margin change detection). `contracts`/`orders`/`fills`/`positions_*` reused as-is (venue-agnostic columns).
- New routes: `GET /api/system/funding` (telemetry), `GET /api/system/cycle` (replaces bar-sync status: last decision, last risk-loop heartbeat, next Friday close). Hot-fix whitelist (`services/api/**`) applies.

### 3.8 Discord deltas — `services/discord_bot/`, `services/webhook_pusher/`
- Retire `/barsync`; **retire `/approve`** — no per-trade approval (operator mandate). Trades are *announced*: `#fills` embed gains signal rationale (trend score, target vs prior, stop level, funding est). Keep `/positions`, `/status`, `/halt`, `/capital-*`.
- New `/cycle` (daily-decision outcome digest at 00:10 UTC: per-asset score → target → action → costs) and monthly + quarterly governance reports (decisions-log 2026-07-08 refinement loops) pushed to `#reports`.
- `AlertCategory` remaps (free-text payloads, no enum migration): `broker_disconnect`→Coinbase WS/REST outage, `margin_warn`→liquidation-buffer breach, retire `qc_objectstore_degraded`; add funding-divergence (B3) and slippage-gate (B1) alerts under existing `slippage_drift`/`data_quality_*` categories.

### 3.9 Frontend deltas — `apps/web`
- `components/today/pipeline-freshness-strip.tsx`: bar_sync→LEAN→recon triad becomes **market-data → 00:05 decision → risk-loop heartbeat → 00:15 recon** (UTC-anchored; `formatET()` per `[A07]` still renders ET).
- `positions-table`, `recent-fills`, `exposure-breakdown`, `lib/api/types.ts`: CME contract fields → CDE perp fields (product_id, contracts, entry VWAP, client-stop, native-stop, liq-distance, funding-to-date). `WatchingSection`/`ExitWatchingSection`: proximity = distance to hysteresis flip / stop levels (same visual pattern, new inputs).
- New Today tile: **funding + cash-yield strip** (accrued funding today, swept cash, yield rate).
- `/signals` approval affordances removed (announce-only). Everything else per frontend-spec §2 unchanged; `routes.config.ts` phase gates honored (`[A23]`).

## 4. Backtest authority (replaces LEAN authority)
`research/crypto_perps/backtest.py` (frozen Amendment B params) is authoritative for PR-review backtest deltas (§5.8 artifact generator points at it). Weekly live-vs-sim parity job (trust bridge): live fills/costs/P&L vs simulated same-window; tolerances re-based during Phase A (slippage gate B1, fee gate B2, funding gate B3 run **permanently**). Quarterly: extend data (FMP or venue candles), re-run the §9 falsification suite, log verdict.

## 5. Phased build plan (each phase gate mechanically testable)

| Phase | Scope | Exit gate |
|---|---|---|
| **C0 — scaffold + shadow** (~1 wk) | Decommission old stack (§1); coinbase_client read-only (products, candles, WS, balance); funding logger; signal engine + parity test vs `research/crypto_perps`; decisions logged + Discord `/cycle` digest, **no orders** | Parity: 30 consecutive days of recorded bars ⇒ identical targets to reference impl; `/cycle` digest fires 3 days running; funding rows accruing |
| **C1 — execution + small-live** (~1-2 wks build, then 45+ days live) | Order ladder, stops, recon fetcher, risk loop, halt wiring; live at `E_effective = min(equity, $1,500)`, max 2 BTC / 4 ETH contracts | Strategy §10 gates A1–A4 + B1–B3, all green, ≥45 days |
| **C2 — full size + polish** | Scale to 50% then 100% equity per §10; cash-yield worker ON (post open-question #1); frontend deltas; governance reports automation | §10 scale-up criteria + demotion triggers armed; quarterly report generated once |
| **Later** | 2.5× scale decision at the 6-month live review; Bermuda-perps comparison memo if API-tradable | Live-vs-sim tracking within tolerance over 6 months |

Build order note: C0/C1 backend work rides the PR-review flow; `services/{signal,risk,execution,reconciliation}/**` and `alembic/**` changes need `risk-review-approved` (`[A02]`); api/discord/frontend surfaces ride the §2.3 hot-fix whitelist where applicable.

## 6. Locked-decision + anti-pattern deltas (for dev-guide §1.5/§11 PR)
- RETIRE: IBKR clientId table, bar_sync block, LEAN signal ingress + backtest authority, Phase-1 IBKR architecture block, `[A13]` as written.
- NEW LOCKED: venue = Coinbase CFM CDE perp-style via Advanced Trade API (no offshore venues); **no per-trade approval** (announce-only Discord); strategy profile = Amendment B (signal params frozen; risk-preference knobs changeable only via amendment + decisions-log); backtest authority = `research/crypto_perps/backtest.py`; intraday-margin opt-in = NEVER; halt = $1,500 manual-clear; cash sweeps must be same-day reclaimable.
- `[A13]` (revised): DO use `coinbase-advanced-py` against CDE products from `services/execution/coinbase_*.py`; DO NOT re-introduce IBKR/LEAN/QC paths (RETIRED 2026-07-08); DO NOT hardcode CDE product IDs.

## 7. Open questions (carry into C0/C1; from strategy §11 + build)
1. Cash-yield instrument: what does CFM/CBI actually offer for swept USD, at what rate, with same-day reclaim? (Gates §3.6 ON.)
2. Strategy §11 items 1–9 verbatim (product metadata, live fee minimum, stop-limit support on `*-CDE` via API, programmatic funding retrieval, sandbox coverage, Bermuda-perps status, WS fill schema, overnight-margin-only confirmation, maintenance calendar).
3. FMP vs venue candles as the permanent quarterly-revalidation data source.
4. Tax note (§1256 60/40) → operator's CPA; frontend Tax Estimate tile inputs change.

---
*Review flow: operator reads this doc; approval = a reply in-session logged to decisions-log; C0 begins on approval. Every §1 deletion and §3 addition lands as reviewable PRs per dev-guide §2.*
