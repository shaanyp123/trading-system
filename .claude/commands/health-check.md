---
description: Generate the operator-side SSH ceremony for a quick system health snapshot (containers, last cycles, risk state, audit chain)
argument-hint: (no args)
allowed-tools: [Bash, Read]
---

# System health check

Generates the paste-ready SSH ceremony for a fast "is everything OK right now?" snapshot. Output covers: container state, last operational cycles (Coinbase market-data worker, recon), risk state, audit chain tail. (Crypto-pivot C0-B4: bar_sync/LEAN/IBKR sections retired with the CME stack.)

The actual checks run on the VPS. Claude Code locally can't reach the postgres DB or docker without SSH. This command codifies the canonical commands per the dev-guide § so the operator doesn't have to remember them or grep runbooks.

## Steps

1. **Output the SSH context block:**

```bash
ssh trading@178.156.239.84
cd /opt/trading
```

2. **Output the health-check commands** in copy-paste-ready blocks. The operator runs them sequentially in the VPS shell; each one is independently safe (read-only).

### Container state (instant)

```bash
docker compose ps
# Expected: all of api, postgres, caddy, nextjs, webhook_pusher,
#           discord_bot showing
#           (Up + healthy). Any "(unhealthy)" or "Exit N" warrants
#           inspection.
```

### Coinbase market-data worker (today)

```bash
docker compose logs api --since 24h | grep -E "coinbase_(ws_connected|funding_snapshot_completed|metadata_snapshot_completed|marks_stale)" | tail -6
# expect: coinbase_ws_connected + one funding_snapshot_completed per hour
#         + one metadata_snapshot_completed after 00:00 UTC; NO marks_stale
```

### Last EOD reconciliation (today)

```bash
docker compose logs api --since 24h | grep -E "reconciliation_(cycle_completed|break_detected)" | tail -4
# Expected (around 22:30 UTC daily):
#   reconciliation_cycle_completed snapshot_at_utc=...
# break_detected lines = INCIDENT; see deploy/reconciliation/README.md.
```

### Risk state + last audit row

```bash
# Stage DATABASE_URL inline (NEVER echo $APP_SERVICE_PW)
APP_SERVICE_PW=$(yq '.postgres.app_service_password' -r /opt/trading-secrets/secrets.yaml)

docker compose exec -T -e PGPASSWORD="$APP_SERVICE_PW" postgres \
  psql -U app_service -d trading \
  -c "SELECT env, state, transitioned_at_utc FROM risk_state WHERE env = 'paper' ORDER BY transitioned_at_utc DESC LIMIT 1;" \
  -c "SELECT MAX(sequence_no) AS last_seq, COUNT(*) AS rows FROM audit_log WHERE env = 'paper';"

unset APP_SERVICE_PW
# Expected:
#   risk_state: state = NORMAL (or whatever your last known good state is)
#   audit_log: rows = monotonic count; last_seq matches
```

### IBKR connection status

```bash
docker compose logs ib_gateway --since 10m | tail -10
docker compose logs api --since 5m | grep -E "ibkr_connection|reqHistoricalData_completed|orderStatus" | tail -5
# Expected: no `IBKR Error 1100` (lost connection) in the last 10 min.
# autoheal will restart ib_gateway if it sees (unhealthy) — check:
docker compose logs autoheal --tail 5
# Expected: only the initial "Container ib_gateway is being monitored" — no
# recent restart events. Frequent restarts = healthcheck flapping; investigate.
```

### Last verify_chain result (from cron — `#audit` Discord channel)

The systemd timer at `deploy/audit/systemd/verify-chain-daily.timer` posts daily at 02:00 ET (06:00 UTC). Check Discord `#audit` for the most recent post — should be a one-liner "OK <timestamp> — env=paper, CHAIN OK: N rows verified". If no post in 24h, the timer failed; check:

```bash
systemctl list-timers verify-chain-daily.timer
systemctl status verify-chain-daily.service
journalctl -u verify-chain-daily.service -n 30 --no-pager
```

3. **Output anti-pattern reminders** (since some commands touch the secrets file + docker logs):
   - Per memory `feedback_secret_handling.md`: NEVER echo `$APP_SERVICE_PW`. NEVER `cat` the secrets file. The `unset` at the end of the risk-state block is required hygiene.
   - The risk-path guard hook won't fire on operator-side SSH (it only watches in-session Edit/Write/MultiEdit on the local machine). On the VPS, the operator's discipline IS the guard.

4. **Summary line**: emit a final block the operator can use as a mental checklist:

```
Health snapshot — paste-ready commands above. After running, expect:
- ☑ all containers Up + healthy
- ☑ bar_sync_cycle_completed failed_markets=[] within last 24h
- ☑ v1_signals_generated session_date=<today> (count optional)
- ☑ no reconciliation_break_detected in last 24h
- ☑ risk_state = NORMAL
- ☑ audit chain monotonic + last verify_chain OK
- ☑ no IBKR Error 1100 in last 10 min; autoheal idle
```

## Cross-refs

- Canonical daily ceremony: `Docs/recent-architecture-changes.md` Option C block
- Memory: `project_phase_status_operational`, `project_clientid_allocation`, `project_autoheal_sidecar`
- Verify_chain: `deploy/audit/README.md` (manual + automated sections)
- Recon: `deploy/reconciliation/README.md`
- Autoheal: `deploy/autoheal/README.md`
