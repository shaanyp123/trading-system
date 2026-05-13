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

## Discord push on recon breaks

The api lifespan constructs an `alert_dispatch_hook` closure that fires
once per ACTIONABLE break (grace-period continuations are skipped):

1. INSERT one row into the `alerts` table with
   `severity` (P2 default; P0 when cash > $1000 or position > 5
   contracts), `category='reconciliation_break'`, message rendering
   the operator-readable title + body, payload JSONB carrying the
   quantitative split, and `triggering_audit_event_uuid` for cross-
   reference with the audit page.
2. Invoke `services.webhook_pusher.dispatcher.dispatch_alert` which
   fans out to:
   - **P2:** Discord `#alerts` channel only.
   - **P0:** `#alerts` + `#critical` + Resend email.

### Sops fields the hook consumes

```yaml
discord:
  webhook_urls:
    alerts: https://discord.com/api/webhooks/<id>/<token>     # required
    critical: https://discord.com/api/webhooks/<id>/<token>   # required for P0
resend:
  api_key: re_xxxxxxxxxxxxxxxxxxxx           # required for P0 (email)
  from_address: ops@yourdomain.com           # required for P0
  to_address: ops@yourdomain.com             # required for P0
```

`webhook_pusher` already consumes these (see
`deploy/webhook_pusher/README.md`); the api now reads the SAME sops
fields. No new sops material if you've already deployed `webhook_pusher`.

### Wiring states

- `discord.webhook_urls.alerts` **unset** → api logs
  `alert_dispatch_hook_skipped_no_webhook_url` at boot. Recon cycle
  still runs end-to-end; alerts are dropped on the floor with an
  apply-layer WARNING (`reconciliation_alerts_dropped_no_hook`).
- `discord.webhook_urls.alerts` **set**, `#critical` **unset** → P2
  alerts deliver successfully; P0 alerts trip the dispatcher's planner
  validation (SEVERITY_TO_CHANNELS includes #critical for P0). The
  alert row's `delivery_status` JSONB shows the per-channel failure.
- All five fields **set** → P2 + P0 paths both work. P0 includes the
  Resend email leg.

### Manual verification

After deploying:

```bash
# 1. Inspect api boot logs for hook construction:
ssh root@<vps> 'docker compose logs api 2>&1 | grep -E "alert_dispatch_hook_(constructed|skipped)" | tail -3'
#   expect:  alert_dispatch_hook_constructed channels=['discord_alerts', 'discord_critical'] email_wired=true
#   OR:      alert_dispatch_hook_skipped_no_webhook_url

# 2. Force a manual recon cycle break (test path; NOT for live):
#    Use psql to insert a fake position into positions_current that
#    diverges from FlexQuery, then wait for the 22:30 UTC cycle OR
#    restart the api with API_RECONCILIATION_SCHEDULER_TEST_MODE if
#    that escape hatch is wired (Phase 1+ follow-up; today the
#    operator waits for natural cycle).

# 3. After cycle fires, check #alerts channel for the embed:
#    Title: "Reconciliation break: <market> position divergence <N> contracts"
#    Color: yellow (P2) or red (P0)
```

## Phase 1+ follow-ups (not in this PR)

- **Kill-switch hook:** when `should_invoke_kill_switch` is True (actionable
  break outside grace period), the api today logs but does NOT auto-halt.
  Operator manually halts via the `/system` page. Auto-halt lands when the
  risk-dispatch state-transition contract is exercised end-to-end.
- **NAV-relative cash threshold:** today's $1000 absolute P0 threshold
  may prove too noisy at scale; switching to bp-of-NAV (e.g., 5bp of
  equity baseline) is a single planner edit when the absolute number
  proves operationally insufficient.
- **Notional-based position threshold:** today's 5-contract threshold
  is asymmetric between high-multiplier markets (/MES 50x SPX → ~$130k)
  and low-multiplier markets (/MBT 0.1x BTC → ~$35k). Switch to
  contract_multiplier-based notional once the planner has the contracts
  table available.
- **Friday-weekend grace window:** today's 36h prior-breaks lookup
  doesn't span the weekend (Friday → Monday is ~72h). Switch to
  business-days math or extend window unconditionally if a real
  Friday-detected break trips this.
