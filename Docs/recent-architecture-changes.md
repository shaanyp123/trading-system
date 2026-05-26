# Recent Architecture Changes

Chronological log of architecture pivots that supersede earlier spec text. Read at session start to avoid recommending retired patterns or designing against assumptions that no longer hold.

**Read order:** newest pivot last so each block makes sense in context of what was true at the time it landed. Most recent state is at the bottom (LEAN futures saga 2026-05-22 → 2026-05-24).

See `Docs/decisions-log.md` for the per-day blow-by-blow + the underlying decision-point rationale (DP-NNN identifiers).

---

> **🚀 OPERATIONAL DAY 1 — 2026-05-20.** Paper trading goes formally operational on this date with the data-layer pivot v2 (Option C) landing + the risk-state forced from HALT_NEW → NORMAL after recovering from the 2026-05-19 recon break. Subsequent operational days count from here (Day 2 = 2026-05-21, etc.). The Drill 10 retrospective from 2026-05-19 was the LIVE-validated end-to-end milestone; today's milestone is "v2 architecture lands + paper trading is the canonical operational state going forward." Live-money cutover (`live-small` env tag) remains a future Phase milestone — today is paper-only. See `Docs/decisions-log.md` 2026-05-20 entries for the full chain.

> **🔄 ARCHITECTURE PIVOT 2026-05-12 — read this BEFORE consulting any other foundation doc.**
>
> The original Phase 1 architecture (QC-Cloud-mediated; backend polls QC ObjectStore for events; defensive trims via `/instructions/<n>.json`; cutover to direct-IBKR in Phase 2 ~Month 5) was retired on 2026-05-12 when DP-025 surfaced at Day 28 deploy: QC's `/object/get` REST endpoint is gated behind the Institutional subscription tier ($500+/mo), which is not viable on a solo-operator budget.
>
> **Operator decision Day 28 02:00 UTC: Option 4 — pull the original Phase 2 architecture forward into Phase 1.** Post-pivot operational reality:
> - **Phase 1 starts on direct IBKR** via `ib-async` to a Dockerized `ib_gateway` container
> - **LEAN runs locally** in a Dockerized `lean_local` container on the operator's VPS from Phase 1 onset
> - **LEAN POSTs `signal_emitted` events** to backend at `POST /api/internal/lean/signals` (shared-bearer auth)
> - **There is no Phase 2 cutover event**; the Phase 1 / Phase 2 split collapses
> - **`services/qc_adapter/**` code stays in repo** as dormant under `qc_adapter_backfill` docker-compose profile gate (Pivot-PR-A moves it there)
>
> See `Docs/decisions-log.md` 2026-05-12 entry "Phase-1 architecture pivot — QC ObjectStore → LEAN Local + direct IBKR (DP-025 → Option 4)" for the full rationale, the 4 underlying decision points (DP-023/024/025/026), and the diff manifest across all 6 foundation docs.

> **🔄 DATA-LAYER PIVOT v2 — Option C LANDED 2026-05-21.** The api owns the bar-fetch responsibility via `services/data/bar_sync.py` (`BarSyncWorker` on `clientId=3` per dev-guide §1.5 LOCKED + deploy reality; daily 17:00 ET cycle) that calls IBKR `reqHistoricalData` for all 11 Phase 1 markets + writes the bars to the shared `lean_data` Docker volume in LEAN's expected on-disk format (equity-daily zip + futures-daily zip + per-day universe CSVs + map_files sentinels). `lean_local` reverts to the original `FakeDataQueue` + `SubscriptionDataReaderHistoryProvider` shape and reads on-disk via the api-managed bars (read-only volume mount). No second IBKR session. No QC plugin in the runtime path. $0/yr ongoing. Same data freshness as the v1 ambition would have provided.
>
> **Post-pivot bar_sync hardening (2026-05-21 follow-up batch, PRs #205-#211):** the first 21:00 UTC cycle (2026-05-20) wrote empty `open_interest` columns + blocked LEAN's `DataMappingMode.OPEN_INTEREST` resolver on all 7 futures. Three sequential PRs (#205+#206+#207) landed real front-month OI via `reqMktData` + qualified contracts + `reqMarketDataType(3)` for delayed-tier paper accounts; 6/7 futures resolved cleanly + /MCL persistently returned OI=0 (NYMEX entitlement gap). A second follow-up batch (#209+#210+#211) landed: (a) a positive-sentinel substitution (`SENTINEL_OI_WHEN_FETCH_FAILED = 1`) so /MCL's resolver picks the contract anyway (strategy logic doesn't consume OI numerically); (b) the bar_sync clientId code default sync from 2 → 3 to match deploy reality; (c) a P2 alert seam (`services/data/bar_sync_alerts.py` + `partial_cycle_alert_hook` parameter) with two descriptor flavors (partial-cycle failure + sentinel substitution) firing at `consecutive_count >= 2`. The api lifespan wires this hook at `services/api/main.py:1212` (`partial_cycle_alert_hook=alert_dispatch_hook`, built by `_build_bar_sync_alert_hook`); when the hook returns `None` (dispatcher not configured) the worker logs `bar_sync_alert_dropped_no_hook` with the descriptor fields. The wiring was verified live during the 2026-05-25 drill 6 diagnostic — see `Docs/decisions-log.md` 2026-05-25 entry "ib_gateway stuck-at-login recurrence + recovery (drill 6)". Full bar_sync-hardening saga in `Docs/decisions-log.md` 2026-05-21 entries.
>
> **lean_local data-layer cache fix (2026-05-21 evening):** the first 21:30 UTC LEAN cycle post-OI-saga still emitted 0 futures signals + logged `v1_history_unavailable failed_markets=[…all 7 futures…]` — even though bar_sync wrote correct OHLCV+OI to disk at 21:00:50 UTC for every market (verified: `mes_trade.zip` contains `mes_trade_202606.csv` with bars Sep 2025 → today including today's row at `20260521 00:00,7433.25,7486.75,7407.5,7468.25,1622904`). Root cause: LEAN's `SubscriptionDataReaderHistoryProvider` caches the data layer **at boot** and doesn't re-scan mid-session. The lean_local container last booted at 16:26 UTC; bar_sync wrote fresh files at 21:00:50 UTC (4.5h later); LEAN's `self.history()` returned empty even though `stat()` inside the container saw the fresh mtime. **Fix:** systemd timer on the VPS host (`deploy/lean_local/systemd/lean-local-daily-restart.{service,timer}`) restarts lean_local daily at **21:10 UTC** — 10 minutes after bar_sync's typical end (21:00:08-21:01:30 UTC observed), 20 minutes before the LEAN signal cycle at 21:30 UTC. Installed + armed live on the VPS this same session (`systemctl list-timers | grep lean-local` → next fire 2026-05-22 21:10 UTC). Same-session manual `docker compose restart lean_local` at 21:38:46 UTC unblocks tomorrow's 21:30 UTC cycle. See `Docs/decisions-log.md` 2026-05-21 entry "lean_local data-layer cache fix" for the full diagnostic + the systemic restart-timer rationale.
>
> **v1 attempt postmortem (PRs #195 + #196 + #197 + the 2026-05-20 03:06 UTC `lean_local stop`):** the v1 ambition routed LEAN's market-data + history calls directly through IBKR via the QC `InteractiveBrokersBrokerage` data-queue-handler on `clientId=10`. Blocked on a 3rd architectural failure mode at deploy: the QC plugin's `IBAutomater` component wants to **spawn its own IBKR gateway process** inside the `lean_local` container rather than connect to the existing `ib_gateway` sidecar (the api worker already owns that gateway on `clientId=1`). IBKR's session model enforces single-IP-per-account so a second gateway is infeasible. Full 3-failure-mode timeline + 4-option analysis in `Docs/decisions-log.md` 2026-05-20 evening entry.
>
> **Architecture (post-Option-C):**
> - **api clientId=1** — order-placement worker (long-lived; `services/execution/ibkr_adapter.py`)
> - **api clientId=3** — bar_sync worker (per-cycle connect → fetch → disconnect; `services/data/bar_sync.py`; synced to deploy reality 2026-05-21 per PR #210)
> - **operator probes + recovery tools** — clientId=80-99 (e.g., `scripts/operator_tools/replay_executions.py` uses 99)
> - **reserve 4-7** — future multi-strategy / additional read-only telemetry clients (clientId=2 reserved for the order-worker's deploy override per dev-guide §1.5)
> - **lean_local** — internal-only network; READ-ONLY mount of `lean_data` volume; reads via `FakeDataQueue` + `SubscriptionDataReaderHistoryProvider`; no direct IBKR connection
>
> **Operator-side deploy ceremony (post-merge of THIS PR):**
> 1. SSH to VPS; `git pull --ff-only`
> 2. `docker compose --env-file deploy/.env build api lean_local` (api code changed → bar_sync worker; lean_local Dockerfile reverted to single-stage)
> 3. `docker compose --env-file deploy/.env up -d --force-recreate api lean_local`
> 4. Watch api logs for `bar_sync_worker_spawned` at boot + (at 17:00 ET) `bar_sync_cycle_firing` → `bar_sync_cycle_completed failed_markets=[]`
> 5. Watch lean_local logs for clean `v1_strategy initialized` (no `v1_universe_data_missing` if bar_sync landed) + at 17:30 ET `v1_signals_generated session_date=… signals_emitted_count=…`
> 6. Verify `verify_chain --env paper` still passes
>
> See `Docs/decisions-log.md` 2026-05-21 entry "Data-layer pivot v2 LANDS via Option C" for the full implementation rationale + the 2026-05-20 evening entry for the v1 postmortem.

> **🔧 LEAN futures data-layer saga 2026-05-22 → 2026-05-24 — 8 PRs landed (#220-#232) restoring futures `self.history()` end-to-end.** Pre-saga: all 7 Phase 1 micros returned `hist_len=0`; ETFs returned `hist_len=205` from the same code path. Post-saga: 6 of 7 futures resolve cleanly (/MCL sidelined per IBKR paper-tier NYMEX entitlement gap); SID encoding matches LEAN canonical byte-for-byte against bundled `es.csv` (55/55); historical contracts backfilled in daily zips; bar_sync's per-bar universe write writes the correct historical front-month per `session_date`. PR #227 (diagnostic probe retirement) stays DRAFT pending **2026-05-25 21:30 UTC validation cycle**. See `Docs/decisions-log.md` 2026-05-22/23/24 entries for the full chain.
>
> **Key new patterns introduced:**
> - **`V1_SIDELINED_MARKETS` registry** (`strategies/v1_trend_following/parameters.py`) — operator-reversible sideline mechanism with 5-step re-enable runbook in the docstring + per-market preconditions; invariant test in `tests/unit/test_strategy_v1.py::TestSidelinedMarkets` locks `V1_SIDELINED_MARKETS ∩ set(V1_CANDIDATE_UNIVERSE) == ∅` so a market can't be simultaneously active + sidelined
> - **SID-hash `MappedSymbol` synthesis** (`services/data/map_file_synthesis.py`) — reproduces LEAN's `SecurityIdentifier.GenerateFuture` byte-for-byte for any `(expiry_date, market_dir)` pair via the `oadate` / `encode_base36` / `compute_future_sid_hash` / `compute_future_expiry` public helpers; validated against 55/55 ES contracts in LEAN's bundled `Data/future/cme/map_files/es.csv`
> - **Per-bar front-month write** (`services/data/bar_sync.py::_per_bar_front_month_or_fallback` + `map_file_synthesis.front_month_for_session_date`) — every per-day universe file gets the actual historical front-month for that `session_date`, NOT today's pick. Fixes the pre-PR-#232 `/MES` map_file 9-month gap (2025-06-22 → 2026-03-16) caused by writing today's expiry into every historical universe file
> - **`set_filter(-365, 90)` for futures** (`lean/v1_strategy.py`) — extends LEAN's continuous-future chain backward to include contracts expired up to 365 days ago, so historical contracts on disk (PR #229 backfill) are actually consumed by `self.history()` instead of being filtered out
> - **`/MYM` routes via `cbot` market_dir** (`services/data/bar_sync.py::PHASE1_UNIVERSE_METADATA` + `strategies/v1_trend_following/universe_freshness.py::V1_FUTURES_MARKET_PATHS`) — paired with operator-side data migration on the VPS (`mv future/cme/{daily/mym_*,universes/mym} future/cbot/`); matches LEAN's `FuturesExpiryFunctions.cs::MicroDow30EMini` registration under `Market.CBOT` per PR #226
