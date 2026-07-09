# Reconciliation EOD scheduler — operator runbook

> Crypto-pivot C0 §3.5 (2026-07-09): the IBKR FlexQuery fetch path is
> DELETED. EOD reconciliation now pulls positions + equity/margin from
> the Coinbase Advanced Trade API and fires at **00:15 UTC** (ten
> minutes after the 00:05 UTC daily strategy decision). The diff/apply
> engine and the alert push pipeline are unchanged.

The api process runs a daily 00:15 UTC reconciliation cycle that:

1. Fetches a Coinbase snapshot via the CDP API key: CFM futures
   positions (`list_positions`), equity/margin
   (`get_futures_balance_summary`), and the last ~26h of fills
   (informational, best-effort).
2. Compares against the backend's `positions_current` + `balances` rows
   (position markets are venue `product_id`s, e.g. the runtime-discovered
   nano BTC/ETH CDE ids — never hardcoded).
3. Writes `reconciliation_check_passed` audit on a clean match OR one
   `reconciliation_break_detected` audit + one `reconciliation_breaks` row
   per metric that doesn't match within tolerance. Every cycle also writes
   one `balances` row with `source='coinbase_eod'` (the /system tile's
   "last check" freshness signal).

**The scheduler does not start until the Coinbase CDP credentials are
populated in the host secrets file.** Until then, the api logs a startup
warning + keeps serving requests normally; the cycle simply doesn't run.

## Step 1 — CDP API key

The recon fetcher uses the SAME CDP key pair as the execution layer —
there is no separate recon credential. If you completed the Coinbase
key ceremony from `deploy/crypto-vps-bringup.md` Step 3, there is
nothing new to create. Otherwise: portal.cdp.coinbase.com → API keys →
ECDSA (ES256) → **View + Trade only, no Transfer** → IP-allowlist the
VPS.

## Step 2 — Populate the secrets file

On the VPS, edit `/opt/trading-secrets/secrets.yaml` (schema:
`deploy/secrets.template.yaml`):

```yaml
coinbase:
  api_key_name: "organizations/{org}/apiKeys/{key}"
  api_private_key: |
    -----BEGIN EC PRIVATE KEY-----
    ...
    -----END EC PRIVATE KEY-----
```

## Step 3 — Re-deploy the api

```bash
ssh root@<vps>
cd /opt/trading
git pull --ff-only
docker compose up -d api
```

Watch the boot log:

```bash
docker logs --tail 60 trading-api-1 | grep -E "reconciliation_scheduler"
```

Expected:

- `reconciliation_scheduler_spawned account_id=... env=paper`
- Within 60s of 00:15 UTC: `reconciliation_scheduler_firing session_date_utc=...`
- Followed by either `reconciliation_eod_cycle_completed breaks_detected=0 ...` (clean) or `breaks_detected>=1 actionable_break_count=N` (operator triage needed)

## Step 4 — Verify cycle output

After the first cycle fires, verify on the VPS:

```bash
# Inspect the audit row landed:
docker exec -i trading-postgres-1 psql -U app_owner -d trading -c "SELECT sequence_no, event_type, env, ingest_clock_ts FROM audit_log WHERE event_type LIKE 'reconciliation_%' ORDER BY sequence_no DESC LIMIT 5;"

# Inspect any new break rows:
docker exec -i trading-postgres-1 psql -U app_owner -d trading -c "SELECT id, metric, market, expected, actual, delta, resolution_path FROM reconciliation_breaks ORDER BY detected_at_utc DESC LIMIT 10;"

# Confirm the per-cycle balance snapshot landed:
docker exec -i trading-postgres-1 psql -U app_owner -d trading -c "SELECT snapshot_ts, net_liquidation, cash_usd, source FROM balances WHERE source = 'coinbase_eod' ORDER BY snapshot_ts DESC LIMIT 3;"

# Confirm audit chain integrity:
docker exec -i trading-api-1 /opt/venv/bin/python -m services.audit.verify_chain --env paper
```

The chain should report `CHAIN OK: <N> rows verified`.

## Troubleshooting

| Symptom | Likely cause | Remedy |
| ------- | ------------ | ------ |
| `reconciliation_scheduler_coinbase_credentials_missing` at boot | secrets fields unset or still placeholders | Re-run Step 2 + Step 3 |
| `reconciliation_scheduler_no_active_account` at boot | `accounts` table empty | Complete the bootstrap (`bootstrap_live_account --mint-from-defaults`) first |
| `reconciliation_eod_cycle_coinbase_fetch_failed operation=list_positions` (or `get_futures_balance_summary`) | venue outage, revoked/expired CDP key, IP allowlist miss | Check Coinbase status; verify the key in the CDP portal; one-shot retry is automatic next session day |
| `reconciliation_eod_cycle_coinbase_fetch_failed ... total_usd_balance` | venue omitted the equity anchor | Transient venue payload issue — recurs ⇒ escalate; the cycle refuses to reconcile against a fabricated zero |
| `coinbase_recon_fills_fetch_degraded` | fills endpoint degraded | Non-blocking (fills are informational); recurs ⇒ investigate key permissions |
| `breaks_detected >= 1` on first cycle | expected pre-population; the backend has zero positions/cash and Coinbase shows real balances | Manual triage — accept the first cycle's break as the initialization marker, or pre-populate `balances` before the first cycle |
| Scheduler logs no `firing` event after 00:15 UTC | clock skew on VPS, or process restarted after 00:15 with the fired marker reset | Verify VPS clock (`timedatectl`); restart the api to re-init the scheduler |

## Disable / pause

To pause the scheduler without removing the secrets fields:

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

### The secrets file fields the hook consumes

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
`deploy/webhook_pusher/README.md`); the api reads the SAME secrets
fields. No new secrets material if you've already deployed `webhook_pusher`.

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

# 2. After the 00:15 UTC cycle fires, check #alerts for any break embed:
#    Title: "Reconciliation break: <market> position divergence <N> contracts"
#    Color: yellow (P2) or red (P0)
```

## Phase C1 follow-ups (not in this PR)

- **Diff tolerances:** carried over from the CME era unchanged;
  re-based in Phase C1 config per `Docs/crypto-pivot-delta-spec.md` §3.5.
- **Intraday probe:** §3.5 names an intraday position probe reusing the
  REST position endpoint (the `positions_override` seam in
  `eod_cycle.build_broker_view` is its entry point). Lands in C1.
- **Kill-switch hook:** when `should_invoke_kill_switch` is True (actionable
  break outside grace period), the api today logs but does NOT auto-halt.
  Operator manually halts via the `/system` page. Auto-halt lands when the
  risk-dispatch state-transition contract is exercised end-to-end.
- **NAV-relative cash threshold:** today's $1000 absolute P0 threshold
  may prove too noisy at scale; switching to bp-of-NAV (e.g., 5bp of
  equity baseline) is a single planner edit when the absolute number
  proves operationally insufficient.
- **Notional-based position threshold:** today's 5-contract threshold
  predates nano contracts (nano BTC ≈ $1.1k notional); switch to
  contract_size × mark notional once `product_metadata` is wired into
  the planner inputs.
- **Per-position uPnL fallback:** when the venue omits a position's
  `unrealized_pnl`, the mark refresh skips the row (no
  contract-multiplier-safe local formula in C0); computing from
  `product_metadata.contract_size` is the C1 fix.
