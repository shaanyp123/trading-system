#!/bin/bash
# synthesize_today_universe.sh
#
# Daily synthesis of "today's" per-day universe files for the LEAN futures
# data layout. Addresses the chronic v1_history_unavailable failure mode
# documented in Docs/decisions-log.md 2026-05-17 and 2026-05-19 entries.
#
# Background: LEAN's continuous-symbol resolution for futures
# (DataMappingMode.OpenInterest + add_future) requires a per-day universe
# file matching the cycle's session_date. DataBento doesn't publish
# today's daily bar until ~1h after the futures close (21:00 UTC), but the
# LEAN cycle fires at 21:30 UTC — too tight for a full re-seed.
#
# Workaround: when today's universe file is missing, synthesize it by
# copying the latest available file forward. The expiry structure
# (which contracts are tradeable today) is unchanged from yesterday for
# all 7 micros (quarterly + monthly expiries roll only at month-end).
# OI/price values are 1 day stale — acceptable for trend-following's
# 200-day MA-based logic.
#
# Idempotent: skips markets where today's file already exists.
# Read-only against everything except the trading_lean_data volume's
# universes/ directories. Safe to run multiple times per day.
#
# Cost: $0. Wall-clock: <5s (file copies via transient alpine container).
#
# ---------------------------------------------------------------------------
# OWNERSHIP (2026-06-04 fix): the transient alpine containers run with
# `--user 1000:1000` so synthesized files are owned by the `trading` uid
# (1000) — the SAME uid the api container's BarSyncWorker writes as. Without
# this, the containers ran as root and left root-owned (`0:0`) per-day files;
# bar_sync (uid 1000, firing ~46s later at 21:00:46) then hit `EACCES`
# rewriting them and logged `bar_sync_universe_write_permission_skipped` for
# every market, every day. The whole volume is uid-1000-owned by convention
# (api writes it); synthesis must match. See memory bar-sync-universe-permission
# and Docs/decisions-log.md 2026-06-04.
#
# MARKET LIST (2026-06-04 fix): the canonical V1 futures universe per
# strategies/v1_trend_following/universe_freshness.py is
# `/MES /MNQ /MYM /M2K /MGC /MCL /MBT`. /MYM lives on **CBOT** (cbot/mym),
# not CME — the prior hardcoded `cme/mym` never matched a directory, counted
# as a per-run failure, and made this oneshot unit exit non-zero EVERY day
# (systemd marked it failed 06-02, 06-03, ...). Corrected to `cbot/mym`.
# ---------------------------------------------------------------------------
#
# Usage:
#   /opt/trading/scripts/operator_tools/synthesize_today_universe.sh
#
# Exit codes:
#   0 — success (synthesized or already-present)
#   1 — docker run failed
#   2 — verification failed (today's file still missing post-synthesis)

set -euo pipefail

# Canonical V1 futures universe: <exchange_dir>/<market_dir>. Keep in sync with
# strategies/v1_trend_following/universe_freshness.py (_DEFAULT_UNIVERSE_DIRS).
# /MCL is V1-sidelined (parameters.py V1_SIDELINED_MARKETS) but its data layer
# is still maintained for one-step re-enable, so it stays in the list.
UNIVERSE_SPECS="cme/mes cme/mnq cbot/mym cme/m2k cme/mbt comex/mgc nymex/mcl"

TODAY="${SYNTH_DATE_OVERRIDE:-$(date -u +%Y%m%d)}"
LOG_TAG="[synthesize_today_universe TODAY=$TODAY]"

echo "$LOG_TAG starting"

docker run --rm \
  --user 1000:1000 \
  -v trading_lean_data:/Lean/Data \
  -e TODAY="$TODAY" \
  -e UNIVERSE_SPECS="$UNIVERSE_SPECS" \
  alpine:3 \
  sh -c '
    set -e
    synthesized=0
    skipped=0
    failed=0
    for spec in $UNIVERSE_SPECS; do
      exch=$(echo "$spec" | cut -d/ -f1)
      mkt=$(echo "$spec" | cut -d/ -f2)
      dir=/Lean/Data/future/$exch/universes/$mkt
      if [ ! -d "$dir" ]; then
        echo "  $spec: FAIL (no directory)"
        failed=$((failed+1))
        continue
      fi
      target="$dir/${TODAY}.csv"
      if [ -f "$target" ]; then
        echo "  $spec: skipped (${TODAY}.csv already exists)"
        skipped=$((skipped+1))
        continue
      fi
      latest=$(ls "$dir" 2>/dev/null | sort | tail -1)
      if [ -z "$latest" ]; then
        echo "  $spec: FAIL (empty directory)"
        failed=$((failed+1))
        continue
      fi
      cp "$dir/$latest" "$target"
      echo "  $spec: synthesized ${TODAY}.csv from $latest"
      synthesized=$((synthesized+1))
    done
    echo ""
    echo "summary: synthesized=$synthesized skipped=$skipped failed=$failed"
    # Exit nonzero if any canonical market could not be synthesized.
    if [ "$failed" -gt 0 ]; then exit 2; fi
  ' || {
    echo "$LOG_TAG ERROR: docker run failed"
    exit 1
  }

echo "$LOG_TAG verifying..."
docker run --rm \
  --user 1000:1000 \
  -v trading_lean_data:/Lean/Data \
  -e TODAY="$TODAY" \
  -e UNIVERSE_SPECS="$UNIVERSE_SPECS" \
  alpine:3 \
  sh -c '
    set -e
    missing=0
    for spec in $UNIVERSE_SPECS; do
      exch=$(echo "$spec" | cut -d/ -f1)
      mkt=$(echo "$spec" | cut -d/ -f2)
      target=/Lean/Data/future/$exch/universes/$mkt/${TODAY}.csv
      if [ ! -f "$target" ]; then
        echo "  MISSING: $spec/${TODAY}.csv"
        missing=$((missing+1))
      fi
    done
    if [ "$missing" -gt 0 ]; then exit 2; fi
    echo "  all 7 markets have ${TODAY}.csv"
  ' || {
    echo "$LOG_TAG ERROR: verification failed"
    exit 2
  }

echo "$LOG_TAG done (exit 0)"
