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

---

# Companion unit: `lean-universe-synthesis` (21:00 UTC)

A second oneshot+timer pair, firing at **21:00 UTC** (just before the
21:10 restart above), that **synthesizes today's per-day universe file**
for each V1 futures market by copying the latest available file forward.

## Why this exists

LEAN's continuous-symbol resolution (`DataMappingMode.OpenInterest`)
needs a universe file matching the cycle's `session_date`. DataBento
doesn't publish today's daily bar until ~1h after the 21:00 UTC futures
close, but the LEAN cycle fires at 21:30 UTC — too tight for a full
re-seed. The expiry structure for the micros only rolls at month-end, so
copying yesterday's universe forward (OI/price 1 day stale) is safe for
the 200-day-MA trend logic. See `Docs/decisions-log.md` 2026-05-17 /
2026-05-19.

## Files

| File | Purpose |
|---|---|
| `lean-universe-synthesis.service` | systemd oneshot: runs `scripts/operator_tools/synthesize_today_universe.sh` |
| `lean-universe-synthesis.timer`   | systemd timer: fires the service daily at 21:00 UTC |
| `scripts/operator_tools/synthesize_today_universe.sh` | the synthesis script (transient `alpine:3` containers) |

## 2026-06-04 fix (why this is now in the repo)

Both units + the script were originally **hand-installed on the host and
never tracked in git** (config drift). Importing them here fixes that, and
ships two corrections:

1. **Ownership** — the script's `docker run` calls now pass
   `--user 1000:1000`. Previously they ran as **root**, so synthesized
   files were `0:0`-owned; bar_sync (uid 1000, firing ~46s later) then hit
   `EACCES` rewriting them → a daily `bar_sync_universe_write_permission_skipped`
   warning for every market. (Non-fatal — bar_sync reused the file — but
   noisy and accumulating root-owned files.)
2. **`/MYM` exchange** — the market list had `cme/mym`, but `/MYM` is a
   **CBOT** contract (`cbot/mym`, per `universe_freshness.py`). The wrong
   path counted as a per-run failure → the oneshot **exited non-zero every
   day** (systemd marked it failed 06-02, 06-03, …). Corrected to `cbot/mym`;
   the unit now exits 0.

## Install (operator runs on the VPS once)

```bash
ssh root@<vps-host>
cp /opt/trading/deploy/lean_local/systemd/lean-universe-synthesis.service \
   /etc/systemd/system/lean-universe-synthesis.service
cp /opt/trading/deploy/lean_local/systemd/lean-universe-synthesis.timer \
   /etc/systemd/system/lean-universe-synthesis.timer
# (the script ships in the repo at /opt/trading/scripts/operator_tools/)
chmod +x /opt/trading/scripts/operator_tools/synthesize_today_universe.sh
systemctl daemon-reload
systemctl enable --now lean-universe-synthesis.timer
systemctl list-timers --all | grep lean-universe-synthesis   # next fire = 21:00 UTC
```

## Test fire (operator — validates without waiting for 21:00)

```bash
# Dry-ish run against a throwaway date, then clean it up:
SYNTH_DATE_OVERRIDE=20990101 /opt/trading/scripts/operator_tools/synthesize_today_universe.sh
# Expected tail: "synthesized=7 skipped=0 failed=0" + "done (exit 0)".

# Confirm the synthesized files are uid-1000-owned (the fix):
docker run --rm -v trading_lean_data:/d alpine sh -c \
  'ls -lan /d/future/cbot/universes/mym/20990101.csv'   # expect "1000 1000"

# Clean up the throwaway date:
docker run --rm --user 1000:1000 -v trading_lean_data:/d alpine sh -c \
  'find /d -name 20990101.csv -delete'
```

## Reversibility

```bash
systemctl disable --now lean-universe-synthesis.timer
rm /etc/systemd/system/lean-universe-synthesis.{service,timer}
systemctl daemon-reload
```
Disabling re-introduces the `v1_history_unavailable` risk when today's
real daily bar lands after the 21:30 cycle.

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
