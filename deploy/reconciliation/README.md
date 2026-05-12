# Reconciliation EOD scheduler — operator runbook

The api process runs a daily 18:30 ET reconciliation cycle that:

1. Fetches an IBKR FlexQuery snapshot (positions + cash balances + NAV)
2. Compares against the backend's `positions_current` + `balances` rows
3. Writes `reconciliation_check_passed` audit on a clean match OR one
   `reconciliation_break_detected` audit + one `reconciliation_breaks` row
   per metric that doesn't match within tolerance.

**The scheduler does not start until the operator populates two sops fields.**
Until then, the api logs a startup warning + keeps serving requests
normally; the cycle simply doesn't run.

## Step 1 — Create the FlexQuery template in IBKR's portal

1. Sign in to **Account Management** (the portal — separate from TWS).
2. Navigate **Reports → Flex Queries → New Flex Query**.
3. Configure (per backend-spec §2.6 reconciliation contract):
   - **Sections to include:** `Open Positions`, `Cash Report`, `Equity Summary in Base`, `Account Information`. Trades + Dividends are useful future enhancements but not required Phase 1.
   - **Format:** XML (`v=3`).
   - **Period:** "Last Business Day" — runs end-of-day after CME's 17:00 ET settlement.
   - **Account selection:** the operator's paper account (`DUQ...`) or live (`U...`) per env.
4. Save. IBKR assigns a numeric **Query ID** + an auto-generated **Flex Web Service Token**. **Record both.**

## Step 2 — Populate sops

```bash
cd ~/Documents/GitHub/Trading
sops secrets/paper.enc.yaml
```

Add under the existing `ibkr:` block (alongside `account_number`, etc.):

```yaml
ibkr:
  ...
  flex_query_id: 991122        # the numeric ID from Step 1
  flex_query_token: <paste>    # the auto-generated token from Step 1
```

Re-encrypt + commit + push:

```bash
git add secrets/paper.enc.yaml
git commit -m "chore(sops): populate ibkr.flex_query_id + flex_query_token"
git push
```

## Step 3 — Re-deploy the api

SSH to the VPS + pull + recreate the api container:

```bash
ssh root@178.156.239.84
cd /opt/trading
git pull --ff-only
docker compose up -d api
```

Watch the boot log:

```bash
docker logs --tail 60 trading-api-1 | grep -E "reconciliation_scheduler"
```

Expected:

- `reconciliation_scheduler_spawned account_id=... env=paper flex_query_id=991122`
- Within 60s of 18:30 ET on the next business day: `reconciliation_scheduler_firing session_date_et=...`
- Followed by either `reconciliation_eod_cycle_completed breaks_detected=0 ...` (clean) or `breaks_detected>=1 actionable_break_count=N` (operator triage needed)

## Step 4 — Verify cycle output

After the first cycle fires, verify on the VPS:

```bash
# Inspect the audit row landed:
docker exec -i trading-postgres-1 psql -U app_owner -d trading -c "SELECT sequence_no, event_type, env, ingest_clock_ts FROM audit_log WHERE event_type LIKE 'reconciliation_%' ORDER BY sequence_no DESC LIMIT 5;"

# Inspect any new break rows:
docker exec -i trading-postgres-1 psql -U app_owner -d trading -c "SELECT id, metric, market, expected, actual, delta, resolution_path FROM reconciliation_breaks ORDER BY detected_at_utc DESC LIMIT 10;"

# Confirm audit chain integrity:
docker exec -i trading-api-1 /opt/venv/bin/python -m services.audit.verify_chain --env paper
```

The chain should report `CHAIN OK: <N> rows verified`.

## Troubleshooting

| Symptom | Likely cause | Remedy |
| ------- | ------------ | ------ |
| `reconciliation_scheduler_flex_credentials_missing` at boot | sops fields unset or still placeholders | Re-run Step 2 + Step 3 |
| `reconciliation_scheduler_no_active_account` at boot | `accounts` table empty | Complete the `/setup` flow first |
| `reconciliation_eod_cycle_flex_fetch_failed error_code=AUTH_INVALID` | wrong token or template ID; expired token | Regenerate the token in IBKR portal + re-populate sops |
| `reconciliation_eod_cycle_flex_fetch_failed error_code=MAX_ATTEMPTS_EXHAUSTED` | template generation taking >60s | One-shot retry next session day; if persistent, simplify the template (fewer sections) |
| `breaks_detected >= 1` on first cycle | expected pre-population; the backend has zero positions/cash and FlexQuery shows real balances | Manual triage — the operator pre-populates `balances` + `positions_current` from the IBKR statement before the first cycle, OR accepts the first cycle's break as the initialization marker |
| Scheduler logs no `firing` event after 18:30 ET | clock skew on VPS, or process restarted after 18:30 with `last_fired_session_date_et` reset | Verify VPS clock; restart the api to re-init the scheduler |

## Disable / pause

To pause the scheduler without removing the sops fields:

```bash
# in deploy/.env on the VPS:
API_RECONCILIATION_SCHEDULER_ENABLED=false
```

Restart the api. The api keeps running; the recon cycle does not fire
until the toggle is flipped back to `true`.

## Phase 1+ follow-ups (not in this PR)

- **Kill-switch hook:** when `should_invoke_kill_switch` is True (actionable
  break outside grace period), the api today logs but does NOT auto-halt.
  Operator manually halts via the `/system` page. Auto-halt lands when the
  risk-dispatch state-transition contract is exercised end-to-end.
- **Prior-breaks lookup:** today the planner's `prior_breaks` is empty so
  every detected break is actionable. The follow-up adds a query against
  `reconciliation_breaks WHERE resolved_at_utc IS NULL AND detected_at_utc > now() - INTERVAL '24 hours'`
  so the T+1 grace window classification works.
- **Discord push:** detected breaks should produce a `#alerts` embed via
  the existing `dispatch_alert` path. The wiring lands alongside the broader
  P0/P1 alert routing follow-up.
