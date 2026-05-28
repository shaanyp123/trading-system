# Runbook — FlexQuery Template 1505530: Add Futures Open-Positions Section

**Status:** OPEN — operator action required in IBKR Account Management portal.
**Priority:** P2 (non-blocking; PR #275 already prevents auto-halt on this break, but the recurring break + P2 alert noise costs operator attention every 22:30 UTC EOD recon cycle).
**Estimated wall-clock:** 5-10 minutes in the IBKR portal + 5 minutes verification at the next 22:30 UTC EOD recon.

## What's broken

FlexQuery template ID `1505530` (the one wired into `sops` as `ibkr.flex_query_id`) was created with only the equity/option Open-Positions section enabled. Futures Open-Positions are NOT included. Result: the 22:30 UTC EOD reconciliation pulls a FlexQuery XML where `<OpenPositions>` is missing all `<OpenPosition assetCategory="FUT" .../>` rows, so the recon planner sees `broker_view={no futures}` while the backend's signal/position tracker correctly has `expected_view={M2KM6 long 1, …}`. Every futures position the operator holds at 21:30 UTC settlement produces a false `position_qty` break.

**Symptom:** 2026-05-27 22:30 UTC recon flagged `M2KM6 expected=1 actual=0`, auto-transitioned NORMAL → HALT_NEW with severity=routine. Position was real at IBKR (verified independently via `reqExecutionsAsync`). See `Docs/decisions-log.md` 2026-05-27 (late evening) entry for the full incident.

**Mitigation already shipped (PR #275, deployed):** the recon planner now downgrades a `position_qty` break to non-actionable (`kill_switch_invoked=false`) when the missing-FUT-template warning fires the same cycle. So the system no longer halts on this — but the recon_break row + P2 alert still recurs every 22:30 UTC until the template is fixed at IBKR.

## Why the operator has to do this

The FlexQuery template configuration is an IBKR-account-side artifact. There's no IBKR API to mutate the template — it must be edited via the Account Management web UI. Backend cannot do this. The operator is the only path.

## Timing constraint — important

**Do NOT do this during 20:55-22:35 UTC.** That window covers:
- 21:00 UTC — `bar_sync` worker fires on clientId=3, fetches 11-market historical bars
- 21:10 UTC — `lean_local` systemd timer restart (data-layer cache flush)
- 21:30 UTC — LEAN V1 signal cycle on clientId=3 (post-restart)
- 22:30 UTC — EOD reconciliation pulls FlexQuery on clientId=1

**Best window:** **02:00-04:00 UTC** (CME settlement trough; minimal trading activity; if `ib_gateway` is evicted it'll auto-reconnect via the autoheal sidecar within ~5 minutes without disrupting any scheduled cycle).

**Eviction caveat:** logging into IBKR's Account Management UI triggers a single-IP-per-account check. If you're logged in from a browser and the `ib_gateway` container is also logged in from the VPS IP, IBKR may evict the gateway session. The autoheal sidecar (PR shipped earlier this week) restarts `ib_gateway` automatically on health-check failure; recovery is 5-6 minutes. During the 02:00-04:00 UTC window this is invisible — no consumer cares about a 5-minute gateway gap.

## The runbook

### Phase 1 — Pre-flight (~30 seconds)

1. Confirm current time is within 02:00-04:00 UTC. Convert: `date -u`. If you're outside the window, schedule for tonight and bail here.
2. Sanity-check the system is healthy before you start:
   ```
   ssh root@178.156.239.84 'docker ps --format "table {{.Names}}\t{{.Status}}" | head'
   ```
   Expect all 9 containers `Up (healthy)` or `Up`. If anything is unhealthy, fix that first — don't compound problems.

### Phase 2 — Edit the template (~5 minutes)

3. Open https://www.interactivebrokers.com/sso/Login in a fresh browser tab or private window.
4. Log in with the operator account (U25655583). 2FA prompt if not whitelisted.
5. Top navigation: **Performance & Reports** → **Flex Queries**.
6. Find the **Activity Flex Query** named for paper trading. The Query ID column should show `1505530`. Click the **edit/pencil** icon on its row.
7. In the template editor's **Sections** panel:
   - Find **Open Positions** in the section list (left side).
   - Click the **gear/configure** icon next to it.
   - In the **Asset Class** sub-filter, ensure all of these are checked:
     - ☑ Stocks (already on)
     - ☑ Options (already on)
     - ☑ **Futures** ← the missing one
     - ☑ Futures Options (defensive — paper account doesn't trade these but harmless)
     - ☑ Forex / Cash (defensive)
   - In the **Options** sub-filter on the same panel, ensure these columns are enabled (they should already be from the equity setup, but verify):
     - Symbol
     - Conid (CRITICAL — contract id)
     - Description
     - Quantity
     - **underlyingSymbol** (CRITICAL — recon planner uses this for FUT symbol normalization per PR #275)
     - Asset Category
     - currency
     - position value (mark-to-market)
   - Click **Save** on the section configuration.
8. In the template's top-level page, click **Continue** then **Save** to commit the template edit.

### Phase 3 — Verify the template (~2 minutes)

9. Still in the IBKR portal: on the Flex Queries page, find query 1505530 and click the **Run** icon (manually trigger a fetch).
10. Wait 30-60 seconds. The portal will offer a downloadable XML once generation completes.
11. Download the XML. Open it in a text editor. Search for `<OpenPosition`. You should see rows with `assetCategory="FUT"` for any futures positions currently open at IBKR. If you only see `STK`/`OPT` rows, the template edit didn't take — repeat Phase 2 carefully.
12. Specifically grep for the current /M2K position (as of this runbook authoring: M2KM6 long 1). Confirm the row has both `symbol="M2KM6"` AND `underlyingSymbol="M2K"`.

### Phase 4 — Log out + watch the next EOD recon (passive)

13. Log out of the IBKR portal cleanly via the avatar menu → **Log Out**. (Closing the tab alone leaves the session alive longer.)
14. Within 5 minutes, the autoheal sidecar should report `ib_gateway` back to `(healthy)` if it was evicted. Verify:
    ```
    ssh root@178.156.239.84 'docker inspect ib_gateway --format "{{.State.Health.Status}}"'
    ```
    If `unhealthy` for more than 10 minutes after logout, manually restart:
    ```
    ssh root@178.156.239.84 'docker compose --env-file deploy/.env restart ib_gateway'
    ```
15. Wait for the next 22:30 UTC EOD reconciliation cycle (could be the same day if you did Phase 2-3 in the morning, or the next day if you did it overnight).
16. Confirm the cycle is clean. Two ways to verify:
    - **Logs:** `ssh root@178.156.239.84 'docker logs api --since 1h 2>&1 | grep -E "reconciliation_eod|broker_view_missing_futures"'` — you should see the cycle's success log and NO `broker_view_missing_futures` warning.
    - **DB:** `ssh root@178.156.239.84 'docker compose --env-file deploy/.env exec postgres psql -U trading -d trading -c "SELECT id, break_type, expected_qty, actual_qty, created_at_utc, resolved_at_utc FROM reconciliation_breaks ORDER BY created_at_utc DESC LIMIT 5"'` — the stale unresolved rows (`019e6b8f-…` and `019e6c83-…` from the 2026-05-27 + earlier cycles) should have `resolved_at_utc IS NOT NULL` set by the post-fix cycle's `_resolve_prior_breaks` call.

### Phase 5 — Document outcome

17. Append a line to `Docs/decisions-log.md` (the 2026-05-28 entry, follow-up subsection):
    ```
    * 2026-05-XX HH:MM UTC — FlexQuery template 1505530 updated to include Futures
      Open-Positions per Docs/flexquery-template-1505530-add-fut-section-runbook.md.
      First post-fix 22:30 UTC EOD recon at 2026-05-XX 22:30 UTC was clean
      (no broker_view_missing_futures warning; stale recon_breaks 019e6b8f-… +
      019e6c83-… auto-resolved). Closes Task #9 from the 2026-05-28 morning
      handoff.
    ```

## Rollback / fallback

If the template edit produces unexpected XML changes that break parsing in `services/reconciliation/flex_query_fetcher.py::_parse_open_positions` (defensive check: parsing errors would surface as `FlexQueryFetchError` with `error_code=PARSE_FAILED`):

1. Revert the template by editing back through the IBKR portal — remove the FUT asset class from the Open Positions section.
2. The system reverts to the pre-fix behavior: PR #275 still downgrades the false-positive break, the only loss is the P2 alert noise stays.
3. Open a follow-up Claude Code session to investigate the parse failure and either patch the parser or fine-tune the template's FUT column set.

## Why not just add a smaller XML field set?

You could theoretically add only `Symbol` + `Quantity` to the FUT rows and skip Conid / underlyingSymbol / currency. **Don't.** The PR #275 recon planner normalization keys off `underlyingSymbol` to map IBKR's contract-month symbol (`M2KM6`) back to the strategy's root ticker (`M2K`). Without it the planner can't reconcile and will treat the FUT row as a phantom position, re-triggering breaks. Match the equity column set as much as possible.

## See also

- `Docs/decisions-log.md` 2026-05-27 (late evening) — original incident, root-cause confirmation
- `services/reconciliation/flex_query_fetcher.py` — parsing contract + sample XML element shapes
- `services/reconciliation/planner.py` — PR #275 FUT-symbol normalization via `underlyingSymbol`
- PR #275 — `fix(reconciliation): normalize FUT symbols via underlyingSymbol + warn on missing-FUT template`
