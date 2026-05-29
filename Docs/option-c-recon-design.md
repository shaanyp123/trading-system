# Implementation Guide — Option C: replace FlexQuery position check with `reqPositions`

**Author:** Claude (session 2026-05-28)
**Scope:** EOD reconciliation false-positive halts (two-night recurrence 2026-05-27 + 2026-05-28).
**Status:** Living design record (originally drafted at `/tmp/option-c-implementation-guide.md`; persisted here for durability). **PR-A MERGED** 2026-05-28 ([#288](https://github.com/shaanyp123/trading-system/pull/288), squash `fbbe77b`) — `services/reconciliation/ibkr_intraday.py` (`fetch_recon_positions`, `ReconPosition`, `ReconPositionsFetchError`, `DEFAULT_RECON_CLIENT_ID=4`). **PR-B** (wire into `run_eod_cycle`, flag-gated, default `flexquery`) + **PR-C** (flip default to `reqpositions` after ≥1 clean cycle) PENDING. See §3 for per-PR scope.
**Forbidden-path posture:** Every PR touches `services/reconciliation/**` (dev-guide §11 [A02] forbidden whitelist) → every PR ships with the `risk-review-approved` label. PR-A took **Path A** (see §4 Q4): it only IMPORTS from `services/execution/**` and does NOT modify it, so the execution layer stays untouched across all three PRs.

---

## 1. Executive summary

The 22:30 UTC EOD reconciliation halts the system every time a new position is opened, because IBKR FlexQuery only reports settlement-cleared positions and our trade fills land after the daily clearing cutoff. Option C swaps the position-quantity check from FlexQuery to `IB.reqPositions()` via the existing `IbAsyncIbkrClient` adapter (real-time TWS API view, no clearing lag). FlexQuery stays as the source-of-truth for the equity / cash / NAV reconciliation columns where settlement-day timing doesn't matter at the granularity we care about.

**Risk impact:** isolated to `services.reconciliation.eod_cycle.run_eod_cycle`. The pure-policy planner (`services.reconciliation.recon.plan_reconciliation_check`) is unchanged — only the data source feeding `BrokerView.positions` changes. Audit-first ordering is preserved (the `apply.py` orchestrator is unchanged). The current 24h auto-clear via T+1 grace continues to work as a backstop if the new path ever silently regresses.

**Verifiable outcome:** the next 22:30 UTC cycle after deploy should land 0 actionable position-qty breaks for the open /M2K position (verified empirically), and the existing FlexQuery-based cash/NAV check continues to pass.

---

## 2. Architecture before / after

### Before (today — broken)

```
┌──────────────────────────────────┐
│   ReconciliationScheduler        │  18:30 ET / 22:30 UTC tick
│   (services/reconciliation/      │
│    scheduler.py)                 │
└────────────────┬─────────────────┘
                 │ fires CycleCallback
                 ▼
┌──────────────────────────────────┐
│   run_eod_cycle                  │
│   (services/reconciliation/      │
│    eod_cycle.py)                 │
└────────────────┬─────────────────┘
                 │
                 ▼
┌──────────────────────────────────┐
│   IbkrFlexQueryClient            │  HTTPS XML pull from IBKR
│   .fetch_snapshot()              │  ⚠️ ONLY settlement-cleared positions
│   (services/reconciliation/      │     (today's fills are not in this XML)
│    flex_query_fetcher.py)        │
└────────────────┬─────────────────┘
                 │ ReconciliationSnapshot
                 ▼
┌──────────────────────────────────┐
│   build_broker_view              │
│   - normalize FUT symbols        │
│     (M2KM6 → /M2K via            │
│      underlyingSymbol; PR #275)  │
│   - sum USD cash                 │
└────────────────┬─────────────────┘
                 │ BrokerView (positions+cash)
                 ▼
┌──────────────────────────────────┐
│   build_backend_view             │  reads positions_current + balances
└────────────────┬─────────────────┘
                 │
                 ▼
┌──────────────────────────────────┐
│   plan_reconciliation_check      │  pure policy
└────────────────┬─────────────────┘
                 │ ReconciliationPlan
                 ▼
┌──────────────────────────────────┐
│   apply_reconciliation_plan      │  audit-first writes
└──────────────────────────────────┘
```

### After (Option C — proposed)

```
┌──────────────────────────────────┐
│   ReconciliationScheduler        │  18:30 ET / 22:30 UTC tick
└────────────────┬─────────────────┘
                 │ fires CycleCallback
                 ▼
┌──────────────────────────────────┐
│   run_eod_cycle                  │
└────────────────┬─────────────────┘
                 │
                 ├──► IbkrFlexQueryClient.fetch_snapshot()   ── EOD cash/NAV/MTM/attribution
                 │      (unchanged — feeds equity baseline,
                 │       cash, balance-snapshot refresh,
                 │       position MTM)
                 │
                 └──► IbAsyncIbkrClient.get_positions()      ── EOD position view
                        (NEW for recon — clientId=4,
                         per-cycle connect/disconnect,
                         pattern mirrors bar_sync clientId=3)
                 │
                 ▼
┌──────────────────────────────────┐
│   build_broker_view              │
│   - positions: from reqPositions │
│   - cash: from FlexQuery         │
│   - source: REQ_POSITIONS_HYBRID │  (NEW enum value; alembic delta — see §3 PR-B)
└────────────────┬─────────────────┘
                 │ BrokerView (positions+cash)
                 ▼
┌──────────────────────────────────┐
│   plan_reconciliation_check      │  pure policy — unchanged
│   apply_reconciliation_plan      │  audit-first — unchanged
└──────────────────────────────────┘
```

**One-line summary of the data-flow change:** position list comes from real-time TWS API (`reqPositions` via clientId=4); everything else still comes from FlexQuery XML. Both are stitched together into the same `BrokerView` the pure-policy planner already consumes.

---

## 3. PR plan

The work is split into **3 PRs in dependency order**. PR-A is the smallest possible "make the new data path available" change; PR-B wires the recon orchestrator to call it; PR-C is the feature-flag flip after empirical observation. If the operator wants to compress this into 2 PRs (skip the flag), the recommendation is still to land PR-A separately so the new adapter capability is reviewable in isolation.

> **All three PRs require `risk-review-approved` label** because every code change lands under `services/reconciliation/**` or `services/execution/**`. The PR description for each must call this out so the merge linter doesn't block.

### PR-A — `feat(execution): per-cycle reqPositions client for recon`

**Scope statement.**
* IN: a new tiny module `services/reconciliation/ibkr_intraday.py` (pre-named in backend-spec §2.6 — the spec already reserves this filename) that owns a per-cycle `IbAsyncIbkrClient` connect → `get_positions()` → disconnect, plus a `fetch_recon_positions(...)` async function that returns `tuple[ReconPosition, ...]` in a small dataclass shape the planner can ingest. Mirrors the bar_sync per-cycle connect pattern (`services/data/bar_sync.py::run_cycle`).
* IN: unit tests for the new module covering: connect succeeds + positions returned; connect fails → raises `IbkrPlacementError`; `reqPositions` returns empty list → returns `()` cleanly; symbol normalization (futures get leading `/`, ETFs don't); structlog field discipline.
* OUT: any changes to `eod_cycle.py` (that's PR-B).
* OUT: any new audit event types or new SSE event types.

**Files touched (full paths).**
* `services/reconciliation/ibkr_intraday.py` — NEW (~120 lines + module docstring).
* `services/reconciliation/__init__.py` — add to `__all__` if the file has one; otherwise no-op.
* `tests/unit/test_reconciliation_ibkr_intraday.py` — NEW (~250 lines).

**Forbidden-path trip status.** YES — both `services/reconciliation/**` AND `services/execution/**` (indirect, via reading `services.execution.ibkr_client.IbkrClient` Protocol + `services.execution.ibkr_adapter.IbAsyncIbkrClient` + `services.execution.types.IbkrPosition`). `risk-review-approved` required. Note: the module IMPORTS from `services.execution` but doesn't modify it — the imports don't trip the linter, but the new file under `services/reconciliation/**` does.

**Test plan.**
* New `tests/unit/test_reconciliation_ibkr_intraday.py`:
  * `TestFetchReconPositions::test_returns_positions_on_happy_path` — fake `IbkrClient` returns 2 `IbkrPosition` rows; assert correct dataclass mapping.
  * `TestFetchReconPositions::test_returns_empty_tuple_when_no_positions` — fake returns `[]`; assert `()`.
  * `TestFetchReconPositions::test_connect_failure_raises_ibkr_placement_error` — fake `connect()` raises; assert propagates as `IbkrPlacementError`.
  * `TestFetchReconPositions::test_get_positions_failure_raises_then_disconnects` — fake `get_positions()` raises; assert `disconnect()` was still called in finally.
  * `TestFetchReconPositions::test_disconnect_failure_is_swallowed_and_logged` — fake `disconnect()` raises after a successful path; assert function still returns happily and a warning is logged.
  * `TestSymbolNormalization::test_futures_get_leading_slash` — `IbkrPosition` with `contract.market='/MES'` (the adapter already prefixes `/` per `_contract_from_ib`) round-trips as `/MES`.
  * `TestSymbolNormalization::test_etfs_stay_unprefixed` — `IbkrPosition` with `contract.market='TLT'` round-trips as `TLT`.
  * `TestSymbolNormalization::test_zero_quantity_positions_dropped` — fake returns a zero-qty position; assert it's filtered out (matches `build_backend_view`/`build_broker_view` semantics).
* Run via: `pytest tests/unit/test_reconciliation_ibkr_intraday.py -v` from the repo root.
* No integration tests in PR-A (the testcontainers boilerplate isn't needed — this is pure adapter-mocking).

**Operator review surface (plain English).**
"This PR adds a tiny module that knows how to ask IBKR for our current open positions in real time. It connects to the broker, asks for the position list, and disconnects — the same pattern our bar-sync worker uses today on a different clientId. It doesn't change anything about how the recon cycle works yet — that's the next PR. Reviewing this PR means checking that the new code can't accidentally hang an IBKR session (always disconnects, even on errors) and that it returns the same `/MES` / `TLT` symbol convention the rest of the recon code already uses."

**Dependencies.** None. Lands first.

**Rollback procedure.** Revert the PR. Nothing else in the system imports the new module yet (PR-B is the wiring), so reverting PR-A is a no-op as long as PR-B hasn't merged.

---

### PR-B — `feat(reconciliation): switch eod_cycle position source to reqPositions (flag-gated)`

**Scope statement.**
* IN: modify `services/reconciliation/eod_cycle.py::run_eod_cycle` to call `fetch_recon_positions` from PR-A in addition to the existing FlexQuery fetch. The new positions feed `build_broker_view`'s positions dict; FlexQuery's `OpenPositions` rows are still parsed (they continue to feed `refresh_backend_from_broker_snapshot` for the per-position MTM path), but NO LONGER feed the planner's position-qty check.
* IN: a new `EodCycleConfig` field `position_source: Literal["flexquery", "reqpositions"] = "flexquery"` so the operator can flip the active source via environment variable (defaults to OLD behavior).
* IN: a structlog log line `eod_cycle_position_source_selected source=<...>` at cycle start so observability is unambiguous.
* IN: a new structlog log line `eod_cycle_reqpositions_failed reason=<...>` when `fetch_recon_positions` raises, plus the failure-mode-resolution chosen (see §4 question 3).
* IN: tests covering both flag values + the failure mode.
* IN: a new `BrokerSource` enum value (or reuse `BrokerSource.TWS_API` — already exists in `services/reconciliation/recon.py:154-162`!). The existing `TWS_API = "tws_api"` value is exactly what we want; **no enum addition needed and therefore no alembic migration**. This is a load-bearing detail — see §4 question 9 + footnote.
* OUT: changes to `recon.py` (planner stays pure).
* OUT: changes to `apply.py` (audit-first orchestrator stays unchanged).
* OUT: removing or modifying `refresh_backend_from_broker_snapshot` — it still uses the FlexQuery position list for MTM since FlexQuery carries `markPrice` + `fifoPnlUnrealized`. (Positions appearing in `reqPositions` but NOT in FlexQuery don't get an MTM row written; the planner's diff is what catches them. Documented as a known limitation in the module docstring.)

**Files touched (full paths).**
* `services/reconciliation/eod_cycle.py` — modify `EodCycleConfig`, modify `run_eod_cycle` (~50 lines net add); add a tiny helper `_select_position_source(config) → str` for log clarity.
* `services/api/config.py` — add `eod_recon_position_source: Literal["flexquery", "reqpositions"] = "flexquery"` setting (env var `API_EOD_RECON_POSITION_SOURCE`). Hot-fix scope (`services/api/**`).
* `services/api/main.py` — pass the setting into `EodCycleConfig` construction at `_start_reconciliation_scheduler` (~5 lines). Hot-fix scope.
* `tests/unit/test_reconciliation_eod_cycle.py` — extend `TestRunEodCycleOrchestrator` with: `test_happy_path_with_reqpositions_source`, `test_reqpositions_failure_logs_and_falls_back_to_flexquery`, `test_position_source_selected_log_line_present`. Reuse the existing `_patch_refresh` auto-fixture and `_build_snapshot` helper.
* `tests/integration/test_reconciliation_apply.py` — NO change (apply is unchanged).

**Forbidden-path trip status.** YES — `services/reconciliation/eod_cycle.py` is the primary change. `risk-review-approved` required. `services/api/**` portions are hot-fix scope and do NOT require the label, but since the PR is a unit they all land together under the label.

**Test plan.**
* `pytest tests/unit/test_reconciliation_eod_cycle.py -v` — runs the new + existing orchestrator tests.
* `pytest tests/unit/test_reconciliation.py tests/unit/test_reconciliation_apply.py tests/unit/test_reconciliation_flex_query_fetcher.py -v` — sanity check that nothing regressed (none of these should need touching).
* `pytest tests/integration/test_reconciliation_apply.py -v` — verifies schema correctness end-to-end. Tests are unchanged; they pass against unchanged `apply.py` + `recon.py`.
* No new integration test in PR-B. The PR-C empirical validation IS the production-like integration test (a real cycle running against ib_gateway).

**Operator review surface (plain English).**
"This PR teaches the EOD reconciliation cycle to optionally ask IBKR's real-time API for positions instead of waiting for IBKR's overnight settlement XML. The cycle reads which source to use from a new environment variable (`API_EOD_RECON_POSITION_SOURCE`); default is the OLD source (FlexQuery), so merging this PR alone changes nothing in production. Reviewing means: (1) confirm the position-source choice doesn't leak into the FlexQuery cash/NAV path; (2) confirm a `reqPositions` failure handles cleanly (see §4 question 3 below for the policy); (3) confirm the audit-first ordering in `apply.py` is untouched."

**Dependencies.** Blocked by PR-A.

**Rollback procedure.** Two-stage. **Fast rollback (preferred):** set `API_EOD_RECON_POSITION_SOURCE=flexquery` on VPS `.env`, `docker compose up -d --force-recreate api`, next cycle reverts to old behavior. **Code rollback:** revert PR-B (and optionally PR-A if you want a clean state). Existing `reconciliation_breaks` rows from the rollback transition window stay; the existing 36h grace window auto-resolves them next cycle.

---

### PR-C — `chore(deploy): flip EOD recon position source to reqPositions`

**Scope statement.**
* IN: change the default of `eod_recon_position_source` in `services/api/config.py` from `"flexquery"` to `"reqpositions"`. OR (equivalently) set `API_EOD_RECON_POSITION_SOURCE=reqpositions` in the operator's VPS `deploy/.env`. Recommendation: do BOTH — set the env var explicitly so the deploy reality is visible in `.env`, AND flip the code default so a future operator who clones the repo fresh gets the modern behavior by default.
* IN: a `decisions-log.md` entry recording the cutover + the first clean cycle observation.
* OUT: any code changes to `eod_cycle.py`, `recon.py`, `apply.py` (PR-A and PR-B did the work).

**Files touched (full paths).**
* `services/api/config.py` — one-line default flip.
* `deploy/.env.example` — document the new default + env var.
* `Docs/decisions-log.md` — append entry.
* `Docs/backend-spec.md` §2.6 — update the "Code path" row (see §4 question 9).

**Forbidden-path trip status.** NO — config default flip + docs + spec text are all hot-fix scope. **However** the deploy ceremony's risk profile is the same as a forbidden-path change (it's flipping how recon decides whether to halt the system), so the operator should treat the actual flip as gated on having observed at least one clean 22:30 UTC cycle with the env-var override active. The PR is small but the deploy moment is the load-bearing event.

**Test plan.**
* `pytest tests/unit/test_reconciliation_eod_cycle.py -v` — confirms the new default reads through cleanly. The existing tests should pass without modification if they construct `EodCycleConfig` explicitly (the default change is what matters for production).
* No new tests.

**Operator review surface (plain English).**
"This PR makes the new position source the default. Before merging, the operator has observed at least one (recommend two) consecutive clean 22:30 UTC cycles with `API_EOD_RECON_POSITION_SOURCE=reqpositions` set explicitly in VPS `.env`. Reviewing means: confirm the empirical observation rows are recorded in the decisions-log entry."

**Dependencies.** Blocked by PR-B + N≥1 clean cycles observed in production with the env override active.

**Rollback procedure.** Single env-var flip back to `flexquery` + `docker compose up -d --force-recreate api`. Same as PR-B's fast rollback.

---

## 4. Each open question's resolution

### Q1. Which clientId for the recon's `reqPositions` call?

**Recommendation: claim reserved `clientId=4` with a per-cycle connect.**

| Option | Pros | Cons |
|---|---|---|
| Reuse worker's clientId=1 | No new clientId; worker already connected | Couples recon to worker availability; mixing read-only + write traffic on the same socket increases blast radius if recon misbehaves |
| **Claim reserved clientId=4 (RECOMMENDED)** | Matches the existing `clientId=4-7 reserved` slot in dev-guide §1.5 LOCKED; clean isolation from worker (clientId=1) and bar_sync (clientId=3); per-cycle connect mirrors bar_sync's proven pattern | Uses one of the four reserved slots; needs one new code default constant + one entry in dev-guide §1.5 |
| New persistent recon clientId | Always-on connection = lower per-cycle latency | Adds a 4th persistent IBKR socket (worker, bar_sync, recon = 3); more state to monitor; daily cycle doesn't need always-on |

**Reasoning.** Dev-guide §1.5 LOCKED already says "reserve 4-7" for cases exactly like this. Bar_sync's per-cycle pattern is proven across ~9 daily cycles since 2026-05-20 with no clientId-collision incidents. Recon runs once per day so the connect cost is negligible. Isolation from the worker means a recon code bug can't wedge order placement.

**Operator input required.** Confirm `clientId=4` is acceptable (and whether the operator wants to keep slots 5/6/7 reserved for future workers). If the operator wants to track the deploy-reality footgun (`API_IBKR_CLIENT_ID=2` override on VPS for the worker), they should also pick a deploy-override slot for recon — recommendation: keep `clientId=4` as both code default AND deploy reality (no override needed; recon hasn't been on any clientId before).

---

### Q2. Connection model — per-cycle or long-lived?

**Recommendation: per-cycle connect/disconnect (mirrors bar_sync).**

| Option | Pros | Cons |
|---|---|---|
| **Per-cycle (RECOMMENDED)** | Already proven by bar_sync; clean isolation; if ib_gateway is down at cycle time, the failure is localized to that cycle and the next day's cycle gets a fresh attempt; no risk of the socket going stale between cycles | One-cycle-per-day connect cost (~1-2s typical) |
| Long-lived shared connection | Lower latency per cycle | Daily 22:30 UTC cadence makes latency irrelevant; long-lived sockets are the source of half the production incidents (Error 326 wedges, drill-6 stuck-at-login); state-of-the-system question becomes "is the recon socket still alive?" which adds an observability burden |

**Reasoning.** Recon fires once per ET calendar day. The cost of a clean per-cycle connect is tens of milliseconds of code complexity vs. a multi-incident history of long-lived sockets going pear-shaped (2026-05-25 drill 6, 2026-05-17/18 Error 326 wedges).

**Operator input required.** Confirmation only. No alternatives worth presenting.

---

### Q3. Failure mode when `reqPositions` times out / errors?

**Recommendation: emit `recon_reqpositions_failed` alert at P1, fall back to FlexQuery position list, log the fallback, and continue the cycle.**

| Option | Pros | Cons |
|---|---|---|
| Halt the cycle (no recon ran) | Forces operator attention immediately | Skipping recon entirely is worse than running recon with degraded data — a real cash mismatch wouldn't be flagged; recurring transient broker failures would create a noisy halt loop |
| **Fall back to FlexQuery + P1 alert (RECOMMENDED)** | Cycle still runs; cash/NAV checks still execute; if FlexQuery also lacks the position (the settlement-lag case), the existing PR #275 `broker_view_missing_futures` warning + non-actionable downgrade still kicks in; operator sees the P1 alert and knows reqPositions failed so future debugging is anchored | Some failure modes silently degrade to the old broken behavior — but the alert makes it visible |
| Skip position checks; still run cash/NAV; emit `recon_data_source_unavailable` alert | Cleaner conceptually | If `reqPositions` fails AND a real position-qty break exists, the cycle silently misses it. The fallback option above gets the same outcome with one extra layer of defense |

**Reasoning.** `reqPositions` failures should be rare (the adapter `connect()` already raises a structured `IbkrPlacementError`; gateway downtime is the most likely cause). The recurring false-positive problem we're solving is the FlexQuery settlement-lag, so falling back to FlexQuery on a reqPositions failure puts us in "the same broken state we were in yesterday" — which is fine for a one-off and the P1 alert ensures the operator notices.

**Operator input required.** Two sub-decisions:
1. Confirm P1 alert severity (P1 → `#alerts` **only** per `services.webhook_pusher.payloads.SEVERITY_TO_CHANNELS` — **only P0 fans out to email**; the earlier draft of this line incorrectly said "P1 → #alerts + email"). Alternative: P2 (also `#alerts` only). Recommendation: P1 because a recon-source failure is operationally important.
2. Confirm the fallback policy (use FlexQuery as the safety net). Alternative: hard-skip position checks entirely. Recommendation: FlexQuery fallback because it preserves the existing post-PR #275 behavior (false-positive may re-appear, but operator already has the manual-override runbook for it).

> **RESOLVED (2026-05-29, PR-B follow-up).** Operator confirmed **P1** (`#alerts` only, no email). Implemented in `services/reconciliation/eod_cycle.py::run_eod_cycle`: on a terminal `ReconPositionsFetchError` the cycle writes a `RECONCILIATION_DATA_SOURCE_DEGRADED` audit row (audit-first, §2.10.1) then dispatches a P1 `reconciliation_data_source_degraded` alert via the existing `alert_dispatch_hook` seam, then falls back to FlexQuery. The emit is fully defensive — audit-write or dispatch failure is swallowed (logged ERROR) so the FlexQuery fallback always runs. New `alert_category` value added via `alembic/versions/2026-05-29_recon_src_degraded.py`; the audit `event_type` column is free-text TEXT so it needed no migration.

---

### Q4. Atomicity of `reqPositions` — is the "all positions received" sentinel robust?

**Status: needs one concrete adapter check before PR-A merges.**

The existing `IbAsyncIbkrClient.get_positions()` at `services/execution/ibkr_adapter.py:897-922` calls `ib.positions(account=self._account_id or "")` — this is the ib-async **synchronous local-cache read**, not a fresh `reqPositionsAsync()` call. The cache is populated by ib-async at `connectAsync()` time via the underlying `reqPositions` + `positionEnd` event handshake.

**Failure mode to verify.** When we do per-cycle connect → immediately call `get_positions()`, is the cache guaranteed populated before the read?

* ib-async's `connectAsync()` awaits `apiStart`, which fires AFTER the initial position sync. **Probably safe**, but the assumption isn't documented in the adapter and a future ib-async upgrade could change it.
* Empirical evidence from existing code: `get_positions()` is already called on the order-placement worker's long-lived clientId=1 connection (the worker uses it as a margin pre-check). That path has worked for ~6 weeks. Per-cycle connect introduces a new timing pattern.

**Recommendation.** Inside PR-A's `fetch_recon_positions`:
1. Call the adapter's existing `await client.connect()`.
2. After connect, **call `await ib.reqPositionsAsync()` explicitly** to force a fresh request rather than relying on the cache. The adapter currently doesn't expose this — see "operator input required" below.
3. Then read `ib.positions(account=...)` (or refactor to call the new method).

**Operator input required.** Two paths:
* **Path A (smaller change):** PR-A trusts the existing `get_positions()` behavior and ships an integration smoke test that asserts the positions list is non-empty after connect IF the IBKR account has positions. Document the cache-timing assumption.
* **Path B (more defensive):** PR-A extends the adapter to add a new method `get_positions_fresh()` that explicitly calls `reqPositionsAsync()` before reading the cache. This is a `services/execution/**` change which is on the forbidden whitelist — same label requirement, same blast radius.

**Recommendation: Path A**, because (a) the adapter's existing behavior has worked for 6 weeks on the worker, (b) Path B adds API surface that may not be needed, (c) we can land Path B as a follow-up if Path A surfaces a timing issue.

---

### Q5. Migration path — feature flag or hard cutover?

**Recommendation: feature-flag gated (PR-B ships flag default `"flexquery"`; PR-C flips default to `"reqpositions"` after empirical observation).**

| Option | Pros | Cons |
|---|---|---|
| **Feature-flag gated (RECOMMENDED)** | Operator can flip in production without code redeploy; A/B against one or two cycles before committing; rollback is single env-var change | Requires +1 PR (the flip) |
| Hard cutover with PR #275 as safety net | One PR fewer | Production observation happens AFTER the change is live; if the new path has a subtle bug it ships to production unsupervised. PR #275's safety net works for the "100% missing" case but not the "partial missing" case |

**Reasoning.** Production observation post-deploy is the only way to know if `reqPositions` behaves as expected in a real-world IBKR cycle. The feature flag costs one tiny PR + a few hours of operator vigilance and gives a clean rollback story. Compared to the current "two nights of manual overrides" baseline, this is the conservative path.

**Operator input required.** Confirm. Implicit in the 3-PR plan above.

---

### Q6. Does the planner need changes?

**No.** The planner (`services/reconciliation/recon.py::plan_reconciliation_check`) takes a `BackendView` and a `BrokerView` plus prior breaks; it doesn't care how the views were materialized. `BrokerView` has fields `positions: Mapping[str, Decimal]`, `cash_usd: Decimal`, and `source: BrokerSource`. PR-B changes `build_broker_view` to consume positions from `fetch_recon_positions` instead of from `snapshot.positions`, but the resulting `BrokerView` dataclass shape is byte-identical and the planner's tolerances + grace-period logic are unchanged.

**Notable detail.** `BrokerSource` enum already has `TWS_API = "tws_api"` (`recon.py:154-162`). PR-B should set `BrokerView.source = BrokerSource.TWS_API` when the position source is `reqpositions` (cash still comes from FlexQuery so we have a "hybrid" reality, but the planner's `source` field is per-view, not per-field). The audit row payload's `source` field then says `tws_api` for the cycle. This is a behavioral change in audit semantics — the EOD audit `reconciliation_break_detected` payloads will now say `source=tws_api` instead of `source=flexquery_eod` for position-qty breaks. **Operator input required:** acceptable? Alternative: introduce a NEW `BrokerSource` value (`HYBRID_TWS_API_POSITIONS = "hybrid_tws_api_positions"`) which would force an alembic migration (the `BrokerSource` literal is reflected in the `reconciliation_breaks.source` TEXT column; schema is free-form per `recon.py:131-141`, so technically no migration needed — but if you want the new value visible to the file-index it's worth documenting). Recommendation: **reuse `TWS_API`** for simplicity; doc-string note explains the hybrid reality.

---

### Q7. Symbol normalization

**Carries over cleanly. No new normalization code needed.**

Today's path (PR #275):
* FlexQuery XML reports `symbol="M2KM6"` + `underlyingSymbol="M2K"` for futures.
* `eod_cycle.py::_market_from_flex_symbol` prefers `underlying_symbol` for FUT positions → returns `"/M2K"`.

New path (PR-B):
* `IbAsyncIbkrClient.get_positions()` returns `IbkrPosition` objects whose `.contract: IbkrContractRef` already carries `market` in the canonical form. The adapter's `_contract_from_ib` at `ibkr_adapter.py:1229-1258` does:
  ```python
  sec_type = getattr(ib_contract, "secType", "")
  if sec_type == "FUT":
      market = f"/{symbol}"  # symbol is the IBKR root, e.g. "M2K"
  else:
      market = symbol
  ```
* For futures, `ib.positions()` returns a Contract object with `symbol="M2K"` (the root) and `localSymbol="M2KM6"` (the contract-month form). The adapter's mapping picks `symbol` (the root) and prefixes `/`. Result: `"/M2K"` matches the backend's `positions_current.market` convention end-to-end.
* For ETFs, `symbol="TLT"`, `sec_type="STK"`, no prefix → `"TLT"`. Matches today's behavior.

**Verification once PR-A's smoke test runs.** Print the first `IbkrPosition` returned during the first paper run and confirm `market` field matches `/M2K` exactly (not `M2KM6`, not `/M2KM6`). Document the observed values in the PR-A description as an empirical pin.

---

### Q8. What stays on FlexQuery?

**Three things:**
1. **`account_summary` (equity + cash + NAV + futures P&L).** Feeds `BrokerView.cash_usd` + the `equity_baseline` for cash tolerance bps math.
2. **`cash_balances` (per-currency USD cash).** Feeds the same.
3. **The position list — but only for the MTM refresh path in `refresh_backend_from_broker_snapshot`** (`eod_cycle.py:479-687`). That function uses `pos.market_price_usd` + `pos.unrealized_pnl_usd` to UPDATE `positions_current.unrealized_pnl`. `reqPositions` does NOT include `markPrice` / `fifoPnlUnrealized` (the adapter's `IbkrPosition.market_price_usd` is hard-coded to `None` per `ibkr_adapter.py:909`). FlexQuery is the only source for those, so the MTM refresh stays on FlexQuery.

**Implication for `build_broker_view`.** When `position_source="reqpositions"`, the `BrokerView.positions` dict is sourced from the new path. When `position_source="flexquery"`, the existing path (which includes the `underlyingSymbol` normalization) runs.

**FlexQuery parser survives empty `OpenPositions`?** Yes — `parse_flex_xml` at `flex_query_fetcher.py:282` iterates `<OpenPosition>` rows; an empty list produces an empty `positions` tuple. The existing `account_summary` parsing (`EquitySummaryByReportDateInBase`) is independent. **However:** today's FlexQuery template per the operator's runbook (PR #279) does include `OpenPositions` for FUT — the template wasn't changed. So the parser doesn't need to handle a truly-empty-positions case differently; it's an "ignore positions field" decision at `build_broker_view` time.

**Operator input required.** None — this is a code-level integration detail PR-B handles.

---

### Q9. Spec amendment required?

**Yes — `Docs/backend-spec.md` §2.6 row "Code path" needs an update in PR-C.**

Today:
> Code path | `services/reconciliation/recon.py` (Day 9 PR #42 pure-policy planner) + `services/reconciliation/flex_query_fetcher.py` (Pivot-PR-C) + `services/reconciliation/ibkr_intraday.py` (Pivot-PR-C)

After PR-C:
> Code path | `services/reconciliation/recon.py` (Day 9 PR #42 pure-policy planner) + `services/reconciliation/flex_query_fetcher.py` (cash/NAV/MTM only post-Option-C) + `services/reconciliation/ibkr_intraday.py` (positions via reqPositions per Option-C 2026-05-28)

**Other spec touches.**
* §2.6 row "EOD cadence" already mentions "IBKR FlexQuery XML pulled directly by backend" — add a clarifying parenthetical "(cash + NAV + position MTM only; position-qty check uses ib-async `reqPositions` post-Option-C 2026-05-28)".
* §1.4 service inventory row 5: the existing text "✅ ib-async intraday + IBKR FlexQuery EOD" is now also true at EOD post-Option-C. No change needed.

**Backend-spec §2.6 is on the docs hot-fix whitelist** (the `Docs/**` prefix is implicit; `services/reconciliation/**` is the code path, not the doc text). Spec amendments don't require `risk-review-approved` per the dev-guide hot-fix paths.

---

### Q10. Existing tests — what needs updating, what's new?

**No existing tests need deletion.** Three categories of test work:

**(a) Extend existing test files.**

| File | Change |
|---|---|
| `tests/unit/test_reconciliation_eod_cycle.py` | Add `TestRunEodCycleOrchestrator::test_happy_path_with_reqpositions_source` + `test_reqpositions_failure_falls_back_to_flexquery` + `test_position_source_selected_log_line_present`. Reuse the existing `_patch_refresh` autouse fixture, `_build_snapshot`, `_flex_pos` helpers. |
| `tests/unit/test_reconciliation_eod_cycle.py::TestEodCycleConfig` | Add `test_position_source_default_flexquery` + `test_position_source_can_be_set_reqpositions`. |
| `tests/unit/test_api_main_order_worker.py` (or similar lifespan test file) | If there's a `TestStartReconciliationScheduler` class, add a test that asserts `EodCycleConfig.position_source` is plumbed from `APISettings.eod_recon_position_source`. |

**(b) New test files.**

| File | Coverage |
|---|---|
| `tests/unit/test_reconciliation_ibkr_intraday.py` | PR-A's full unit-test surface (see PR-A test plan above). |

**(c) Integration tests — NO new file needed.**

The existing `tests/integration/test_reconciliation_apply.py` exercises `apply_reconciliation_plan` against real Postgres + alembic head. Since neither `recon.py` nor `apply.py` change, the existing integration tests continue to pass. The new `fetch_recon_positions` path can be smoke-tested with a fake `IbkrClient` in unit tests (mocking the `IB.positions()` cache is standard pattern; see `tests/integration/test_replace_protective_stop_end_to_end.py::_fake_ibkr_client` for the canonical mock shape).

The **real integration test for PR-A + PR-B is the empirical production cycle observation in PR-C's deploy ceremony** — a 22:30 UTC cycle running against a live ib_gateway with a real position. This is the A27 smoke-test satisfier per dev-guide §6.8 (the PR-A docstring must include "Smoke-tested via: deploy/reconciliation/README.md Step <N>" with N = the cutover ceremony step).

---

## 5. Test strategy

### Unit tests (PR-A)

Fully mockable; no IBKR / no Postgres. Pattern:

```python
# tests/unit/test_reconciliation_ibkr_intraday.py
from unittest.mock import AsyncMock, MagicMock

from services.execution.types import IbkrContractRef, IbkrPosition
from services.reconciliation.ibkr_intraday import fetch_recon_positions, ReconPosition


@pytest.mark.asyncio
async def test_fetch_returns_positions_on_happy_path():
    fake_client = MagicMock()
    fake_client.connect = AsyncMock()
    fake_client.disconnect = AsyncMock()
    fake_client.get_positions = AsyncMock(return_value=[
        IbkrPosition(
            contract=IbkrContractRef(market="/M2K", ...),
            quantity=Decimal("1"),
            ...
        )
    ])

    result = await fetch_recon_positions(client_factory=lambda: fake_client)

    assert len(result) == 1
    assert result[0].market == "/M2K"
    assert result[0].quantity == Decimal("1")
    fake_client.connect.assert_called_once()
    fake_client.disconnect.assert_called_once()
```

Coverage targets: 95% for the new module (per dev-guide §3.1 — `services/execution/**` and `services/reconciliation/**` both at the 90% audit/risk/execution floor).

### Unit tests (PR-B)

Extend the existing `test_reconciliation_eod_cycle.py` patterns. The `_stub_session_factory` + `_patch_refresh` autouse + monkeypatched `apply_reconciliation_plan` pattern already exists and supports both source modes with minor extension:

```python
# Inside TestRunEodCycleOrchestrator
async def test_happy_path_with_reqpositions_source(self, monkeypatch):
    """When position_source='reqpositions', positions come from fetch_recon_positions
    and FlexQuery's OpenPositions are not consulted for the broker view."""
    config = EodCycleConfig(
        account_id=uuid4(),
        env="paper",
        flex_query_id=1,
        flex_query_token="t",
        position_source="reqpositions",  # NEW field
    )

    snap = _build_snapshot(
        positions=(),  # FlexQuery says no FUT positions (the settlement-lag case)
        cash_balances=(FlexCashBalance(currency="USD", balance=Decimal("100000")),),
    )
    flex_client = MagicMock()
    flex_client.fetch_snapshot = AsyncMock(return_value=snap)

    # NEW: stub the reqPositions fetcher to return the real position
    async def fake_fetch_recon_positions(**kwargs):
        return (ReconPosition(market="/M2K", quantity=Decimal("1")),)

    monkeypatch.setattr(
        "services.reconciliation.eod_cycle.fetch_recon_positions",
        fake_fetch_recon_positions,
    )

    factory = _stub_session_factory(
        positions=[{"market": "/M2K", "qty": 1}],
        balance={"cash_usd": Decimal("100000"), "net_liquidation": Decimal("105000")},
    )

    captured = {}
    async def fake_apply(plan, **kwargs):
        captured["plan"] = plan
        return MagicMock(kill_switch_invoked=False, audit_event_uuids=(),
                        inserted_break_ids=(), resolved_break_count=0,
                        alerts_dispatched_count=0)
    monkeypatch.setattr(
        "services.reconciliation.eod_cycle.apply_reconciliation_plan", fake_apply
    )

    await run_eod_cycle(
        config=config, session_factory=factory,
        flex_client_factory=lambda: flex_client,
    )

    # Backend + broker agree on /M2K=1 → no position break.
    pos_breaks = [b for b in captured["plan"].breaks_detected
                  if b.metric == ReconciliationMetric.POSITION_QTY]
    assert pos_breaks == []
```

### Mock IBKR responses

The canonical pattern for mocking IBKR without a real gateway is `services/execution/types.py`'s `IbkrPosition` dataclass + a `MagicMock` `IbkrClient`. The existing `tests/integration/test_replace_protective_stop_end_to_end.py::_fake_ibkr_client` is the canonical shape (uses `AsyncMock` for async methods, captures callbacks for ack-event simulation). PR-A's tests should mirror this.

Reference patterns to follow:
* `tests/integration/test_replace_protective_stop_end_to_end.py` lines 1-160 (testcontainers pg fixture; module-scoped `pg_url` with `alembic upgrade head`; `_require_docker()` skip; `fresh_account_id` per-test fixture).
* `tests/integration/test_reconciliation_apply.py` lines 1-180 (same testcontainers shape; `_make_break` + `_pending` builder helpers).
* `tests/integration/test_replace_protective_stop_end_to_end.py::_fake_ibkr_client` (lines 379-454) for the IBKR-client mock pattern.

PR-A does not need a testcontainers integration test (the orchestrator wiring is unit-test-able). The empirical production cycle in PR-C is the production-shape verification.

### Local make targets

```bash
# After PR-A
make lint
make typecheck
pytest tests/unit/test_reconciliation_ibkr_intraday.py -v

# After PR-B
pytest tests/unit/test_reconciliation_eod_cycle.py -v
pytest tests/integration/test_reconciliation_apply.py -v  # no change, just sanity
pytest tests/unit/test_reconciliation.py tests/unit/test_reconciliation_apply.py -v  # regression

# After PR-C
pytest tests/unit/test_reconciliation_eod_cycle.py -v  # default-value sanity
```

---

## 6. Deploy ceremony

This is the operator-facing runbook for landing Option C in production. Each step has a verification gate; if a gate fails, the operator stops and triages before continuing.

### Step 0 — Pre-flight (operator side, no merges yet)

* Confirm dev-guide §1.5 LOCKED reserves `clientId=4-7` for additional read-only telemetry clients.
* Confirm the current production VPS `.env` does NOT have `API_EOD_RECON_POSITION_SOURCE` set (env var is new in PR-B).
* Confirm `IBKR_MASTER_CLIENT_ID` value (still TBD per the 2026-05-27 + 2026-05-28 morning entries). Recon's clientId=4 is OUTSIDE the master-client mitigation scope — recon only reads positions, never cancels orders cross-client — so master-clientId reconciliation is orthogonal to this work.
* Confirm an open /M2K (or other futures) position exists at IBKR. If no open position, this whole exercise is theoretical; reproduce the false-positive condition by opening a small paper position first.

**Verification gate:** Operator says "clientId=4 free, env var unset, position exists, master-clientId is independent."

### Step 1 — Merge PR-A (add `fetch_recon_positions` adapter)

```bash
# Operator-side ceremony
git checkout main
git pull
gh pr review <PR-A-number> --approve  # after Claude's PR description + risk-review-approved label applied
gh pr merge <PR-A-number> --squash --delete-branch
```

Deploy (Step 5 below) — but recommendation: hold the VPS deploy until PR-B is also merged. PR-A by itself adds dead code (nothing imports the new module yet), so deploying it solo wastes a docker recreate.

**Verification gate:** `gh pr list --state merged` shows PR-A; `git log --oneline -1` on main is the PR-A squash; CI passed all required checks.

### Step 2 — Merge PR-B (recon orchestrator wiring + feature flag default `flexquery`)

```bash
gh pr review <PR-B-number> --approve
gh pr merge <PR-B-number> --squash --delete-branch
```

**Verification gate:** as Step 1.

### Step 3 — VPS deploy (PR-A + PR-B; flag still defaults to `flexquery`)

```bash
# SSH to VPS
ssh root@<vps-host>
cd /opt/trading
git pull --ff-only
# Confirm NO env-var override yet; flag default is still "flexquery"
grep API_EOD_RECON_POSITION_SOURCE deploy/.env  # should return nothing
docker compose --env-file deploy/.env up -d --build --force-recreate api
# Verify the new module imports cleanly + api came up:
docker compose logs --since 2m api | grep -E "(reconciliation_scheduler_spawned|reconciliation_scheduler_disabled|eod_cycle_position_source_selected|api_lifespan_ready|ERROR)"
```

**Verification gate:** logs show `reconciliation_scheduler_spawned`; no `ERROR` lines on the new module path; `eod_cycle_position_source_selected source=flexquery` (or similar, if PR-B logs at boot).

### Step 4 — Wait for the next 22:30 UTC EOD cycle

This first post-deploy cycle still uses FlexQuery (flag default unchanged). It MAY or MAY NOT halt — depends on whether IBKR's overnight clearing is current. The point of this step is to verify the new code path didn't accidentally break the existing one.

**Verification gate:** logs show `reconciliation_eod_cycle_completed` with the same breaks-detected / actionable-break-count fields as the prior days' cycles. If the cycle halts again on the same /M2K break, that's expected — we haven't flipped the flag yet.

### Step 5 — Flip the env var (still without merging PR-C)

> **Prerequisite (added 2026-05-29):** the api service in `docker-compose.yml`
> must pass `API_EOD_RECON_POSITION_SOURCE` through to the container
> (`API_EOD_RECON_POSITION_SOURCE: ${API_EOD_RECON_POSITION_SOURCE:-flexquery}`
> in its `environment:` block). `config.py` uses `env_file=None`, so a value
> in `deploy/.env` reaches the container ONLY via that mapping — `--env-file`
> alone feeds compose interpolation, not the container env. PR-B (#290)
> shipped the config field but not the passthrough; it was added in a
> follow-up. Without it, the `echo >> deploy/.env` below silently no-ops and
> the cycle stays on FlexQuery.

```bash
# On VPS
echo "API_EOD_RECON_POSITION_SOURCE=reqpositions" >> deploy/.env
docker compose --env-file deploy/.env up -d --force-recreate api
docker compose logs --since 2m api | grep -E "(eod_cycle_position_source_selected|reconciliation_scheduler_spawned|ERROR)"
```

**Verification gate:** logs show `eod_cycle_position_source_selected source=reqpositions`. If the recon cycle isn't scheduled to fire imminently, you may need to wait until the next 22:30 UTC tick.

### Step 6 — Observe N≥1 (recommend 2) clean 22:30 UTC cycles

Watch the next one or two cycles for:
* `reconciliation_eod_cycle_starting` log line.
* `eod_cycle_position_source_selected source=reqpositions` (proves the new path is active).
* New ib_gateway connect from clientId=4 in `docker compose logs ib_gateway` (or `ibkr_connected client_id=4` from the api side).
* `reconciliation_eod_cycle_completed actionable_break_count=0` — the success criterion.
* SELECT from `reconciliation_breaks` post-cycle returns no new rows for /M2K.
* Audit chain still clean: `verify_chain --env paper` (operator's standard ceremony).

If the cycle still halts, the most likely failure modes are:
1. `reqPositions` returned a position whose `market` field doesn't match backend's `positions_current.market` (symbol normalization bug — check the actual values via `docker compose exec api python -c "..."` quick probe).
2. The cycle hit the FlexQuery fallback path (Q3 failure mode) — check for `eod_cycle_reqpositions_failed` log line.
3. The /M2K position is closed since the prior cycle (= legitimate change; no longer a recon issue).

**Verification gate:** ≥1 clean cycle in production with `source=reqpositions`. Recommendation: wait for 2 cycles before merging PR-C so the empirical baseline is wider.

### Step 7 — Merge PR-C (flip default)

```bash
gh pr review <PR-C-number> --approve
gh pr merge <PR-C-number> --squash --delete-branch
```

VPS already has the env var set, so the next deploy after PR-C merges doesn't change runtime behavior — the env var override and the new code default produce the same result. The env var can stay in `.env` for explicitness (recommend keeping it; deploy reality is visible in one place).

### Step 8 — Decisions-log entry

Add a 2026-05-XX entry to `Docs/decisions-log.md` summarizing:
* The recurring false-positive incident (3+ nights now: 2026-05-27, 2026-05-28, plus any that landed during the cutover ceremony).
* The Option-C decision + the three PRs.
* The empirical cycle observations (which dates were clean, which weren't, any operator-side manual overrides during the cutover).
* The deploy ceremony's actual timeline.

### Step 9 — Stale `reconciliation_breaks` cleanup (optional)

The 2026-05-27 + 2026-05-28 stale rows mentioned in the morning entries (`019e6b8f-...` + `019e6c83-...`) will auto-resolve via the 36h grace window on the first clean post-Option-C cycle. If the operator wants them gone sooner, manually UPDATE `resolved_at_utc` + `resolution_path='manual'` — but the audit row stays put (audit_log immutability per backend-spec §2.10.2).

---

## 7. Open risks + monitoring

### Pre-deploy risks

**R1. `reqPositions` cache-population timing (Q4).** If the adapter's `connect()` returns before `positionEnd` fires, `get_positions()` could return a stale or empty list on the first call after connect. **Mitigation:** PR-A smoke test inspects the result of the first paper run and pins the observed values. If empty, fall back to Path B (explicit `reqPositionsAsync`).

**R2. clientId=4 collision with future work.** If a later session claims clientId=4 for something else without re-reading dev-guide §1.5, two recon-shape sockets could collide. **Mitigation:** PR-A updates dev-guide §1.5 LOCKED to mark clientId=4 as taken by recon. (This is a dev-guide doc change; hot-fix scope.)

**R3. Symbol mismatch on edge-case markets.** /MYM routes via `cbot` per 2026-05-23 entry; `_contract_from_ib` maps `sec_type == "FUT"` → `f"/{symbol}"` uniformly. If ib-async returns `symbol="MYM"` rather than `symbol="YM"` for /MYM, the prefix is correct. **Mitigation:** PR-A smoke test includes /MYM if any position exists; first paper-trial verification.

### Post-deploy risks

**R4. reqPositions reports stale data mid-cycle.** If ib_gateway disconnects + reconnects mid-cycle, IBKR re-syncs positions but the timing is opaque. **Mitigation:** the 60s scheduler tick has a single fire-then-mark-fired loop; the cycle's `reqPositions` call runs once. If the gateway is mid-reconnect at fire time, the cycle fails with `IbkrPlacementError` and falls back to FlexQuery per Q3 policy.

**R5. False negatives — a real position-qty mismatch goes undetected.** If `reqPositions` shows the same (wrong) qty as backend, the planner won't flag it. **Mitigation:** this is identical to FlexQuery's failure mode (FlexQuery could also report wrong); the only defense is the operator's daily review of fills + Discord #fills feed. Phase-1+ enhancement: cross-check `reqPositions` vs FlexQuery once both arrive and emit a P3 "broker view disagreement" warning when they diverge. NOT in scope for Option C.

**R6. `reqPositions` returns positions for accounts other than ours.** The adapter passes `account=self._account_id or ""`; the empty-string fallback could include all subaccounts on a multi-account login. **Mitigation:** the operator's IBKR account is single-account (verified `U25655583`). PR-A's smoke test pins the account-id field on the first returned position.

### Abort criteria

If during Step 6 observation:
* `reqPositions` consistently returns empty when positions are known to exist → abort, revert env var, re-investigate cache-timing (Q4 Path B).
* New audit events have payload `source=tws_api` but backend logs disagree on counts → abort, revert env var, dump the recon cycle's full trace.
* IBKR gateway logs show repeated cross-clientId errors (Error 162, Error 10147) → abort, revert env var. Confirm clientId=4 isn't colliding with another process.

If during Step 6 a halt fires:
* If the halt is on `position_qty` for /M2K still — the new path didn't help; revert + investigate.
* If the halt is on `cash_usd` (a real cash mismatch) — that's a legitimate halt; investigate normally, this PR plan doesn't change cash-recon semantics.

### Monitoring after PR-C merges

Daily ad-hoc check:
* `docker compose logs --since 24h api | grep -E "(eod_cycle_position_source_selected|reconciliation_eod_cycle_completed|eod_cycle_reqpositions_failed)"` — should show `source=reqpositions` + `actionable_break_count=0` (or a legitimate non-zero with operator triage).
* `SELECT COUNT(*) FROM reconciliation_breaks WHERE detected_at_utc > NOW() - INTERVAL '24 hours' AND resolution_path IS NULL;` — should be 0 or small.

Weekly check:
* Confirm `verify_chain --env paper` still passes (the audit chain is untouched by this work, but the ceremony catches any unrelated regression).

---

## Appendix A — Files-touched summary across all three PRs

| File | PR | Forbidden-path | Net lines |
|---|---|---|---|
| `services/reconciliation/ibkr_intraday.py` | A | YES (services/reconciliation/**) | +120 |
| `tests/unit/test_reconciliation_ibkr_intraday.py` | A | NO (tests/**) | +250 |
| `services/reconciliation/eod_cycle.py` | B | YES (services/reconciliation/**) | +50 |
| `services/api/config.py` | B | NO (services/api/** hot-fix) | +15 |
| `services/api/main.py` | B | NO (services/api/** hot-fix) | +5 |
| `tests/unit/test_reconciliation_eod_cycle.py` | B | NO | +180 |
| `services/api/config.py` (default flip) | C | NO | ±1 |
| `deploy/.env.example` | C | NO | +3 |
| `Docs/decisions-log.md` | C | NO | +60 |
| `Docs/backend-spec.md` §2.6 | C | NO | ±5 |
| `Docs/claude-dev-guide.md` §1.5 (clientId=4 claim) | A | NO | +2 |
| `Docs/file-index.md` | All | NO | +10 |

**Total net code:** ~440 lines (+ tests + docs).

## Appendix B — Open items requiring operator input before kickoff

1. **clientId choice** — confirm clientId=4 (vs. reuse worker's 1, vs. new persistent). Recommendation: clientId=4.
2. **Failure-mode policy** — confirm "P1 alert + fall back to FlexQuery". Alternative: hard-halt or skip-positions.
3. **`get_positions()` vs. explicit `reqPositionsAsync()`** — Path A (trust existing adapter behavior) or Path B (extend adapter). Recommendation: Path A; promote to B if PR-A smoke surfaces a timing issue.
4. **`BrokerSource` enum value** — reuse `TWS_API` vs. add `HYBRID_TWS_API_POSITIONS`. Recommendation: reuse `TWS_API` for simplicity.
5. **Cycle-observation count** — N=1 or N=2 clean cycles before PR-C merges. Recommendation: N=2 for confidence.
6. **Master Client ID interaction** — recon's clientId=4 doesn't issue cancels, so master-clientId mitigation is orthogonal. Confirm.

---

## Appendix C — How this differs from previously-considered options

* **Option A (move EOD recon to 07:30 UTC post-clearing).** Not chosen: shifts the failure mode rather than fixing it; couples our cycle timing to IBKR's overnight clearing infra; doesn't help intraday recon if it's ever added.
* **Option B (extend PR #275 to downgrade "partial missing" too).** Not chosen: silently masks real breaks during the 24h grace window. Sensitivity loss.
* **Option C (this plan).** Chosen: addresses root cause (FlexQuery's settlement-day semantics), keeps FlexQuery for the columns where it's still right, uses an adapter we already trust on clientId=1, isolates per-cycle on clientId=4 to avoid wedging the worker.

End of guide.
