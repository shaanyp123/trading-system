# `deploy/lean_local/systemd/` — Daily lean_local restart timer

Operator runbook for installing the systemd timer that restarts the
`lean_local` Docker container daily at **21:10 UTC** (between bar_sync's
21:00 UTC cycle and LEAN's 21:30 UTC signal cycle).

## Why this exists

LEAN's `SubscriptionDataReaderHistoryProvider` reads the on-disk data
layer (the `lean_data` Docker volume) **at boot** and caches the file
tree in-process for the lifetime of the container. The api's
`BarSyncWorker` writes fresh per-day daily-zip + universe files every
day at 21:00 UTC, but LEAN cannot see those updates without a restart.

**The failure mode this prevents** (observed 2026-05-21 21:30 UTC LEAN
cycle, before this timer was installed):

```
v1_universe_data_fresh markets_checked=7 threshold_days=5 fresh_count=7
... (~5 hours pass; bar_sync runs at 21:00 UTC, writes fresh daily zips) ...
v1_history_unavailable session_date=2026-05-21 failed_markets=['/MES','/MNQ','/MYM','/M2K','/MGC','/MCL','/MBT']
v1_signals_generated session_date=2026-05-21 signals_emitted_count=0 rejections_count=4
```

All 7 futures fail history resolution because LEAN's data-layer cache
predates bar_sync's writes. `stat()` inside the container shows the
fresh files exist (mtime = 21:00:50 UTC, content correct including
today's row) — it's purely an in-process cache issue.

See `Docs/decisions-log.md` 2026-05-21 entry "lean_local data-layer
cache fix" for the full root-cause analysis.

## Files

| File | Purpose |
|---|---|
| `lean-local-daily-restart.service` | systemd oneshot: runs `docker compose ... restart lean_local` |
| `lean-local-daily-restart.timer`  | systemd timer: fires the service daily at 21:10 UTC |

## Install (operator runs on the VPS once)

```bash
# SSH to VPS as root
ssh root@<vps-host>

# Copy the unit files into systemd's search path.
# (Assumes the repo is at /opt/trading via `git pull` workflow.)
cp /opt/trading/deploy/lean_local/systemd/lean-local-daily-restart.service \
   /etc/systemd/system/lean-local-daily-restart.service
cp /opt/trading/deploy/lean_local/systemd/lean-local-daily-restart.timer \
   /etc/systemd/system/lean-local-daily-restart.timer

# Reload systemd to pick up the new units.
systemctl daemon-reload

# Enable + start the timer (Persistent=true means it survives reboots).
systemctl enable --now lean-local-daily-restart.timer

# Verify the timer is armed.
systemctl list-timers --all | grep lean-local-daily-restart
# Expected: a row showing the next fire is tomorrow at 21:10 UTC.

# Verify the service unit is valid (won't actually fire until the timer trips).
systemctl status lean-local-daily-restart.service
# Expected: "inactive (dead)" — the service is oneshot, fires when timer triggers.
```

## Test fire (optional — operator runs to validate)

To validate the unit works without waiting for 21:10 UTC, manually start
the service:

```bash
systemctl start lean-local-daily-restart.service

# Tail the service journal:
journalctl -u lean-local-daily-restart.service -n 30 --no-pager

# Expected: docker compose output showing
#   "Container trading-lean_local-1 Restarting" + "Started"
# Service should reach state "inactive (dead)" with exit code 0.

# Verify lean_local actually restarted:
docker compose -f /opt/trading/docker-compose.yml --env-file /opt/trading/deploy/.env ps lean_local
# Expected: STATUS shows recent uptime (seconds-old, not days).
```

## Disable (operator: if rolling back)

```bash
systemctl disable --now lean-local-daily-restart.timer
rm /etc/systemd/system/lean-local-daily-restart.{service,timer}
systemctl daemon-reload
```

Note: disabling re-introduces the data-layer cache issue. The lean_local
container will keep running but its history layer will stay stale
relative to bar_sync's writes — same failure mode as documented above.

## Monitoring

The timer fires silently when successful. To monitor over time:

```bash
# Last 5 fires + their exit codes:
journalctl -u lean-local-daily-restart.service --since "1 week ago" | grep -E "Started|Deactivated|Failed"

# Or watch for the lean_local boot signal in the api logs after a fire:
docker compose -f /opt/trading/docker-compose.yml --env-file /opt/trading/deploy/.env logs lean_local --since 5m 2>&1 | grep "v1_strategy initialized"
```

If `Failed` shows up in the timer journal, investigate via
`systemctl status lean-local-daily-restart.service` for the failure mode
(likely: docker daemon hiccup, lean_local container missing, compose
file moved).

## Reversibility

Trivial — see "Disable" above. The unit files live entirely in
`/etc/systemd/system/`; no other system config is mutated.
