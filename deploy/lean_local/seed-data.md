# `deploy/lean_local/` — seed-data operator runbook

Canonical procedure for populating the `trading_lean_data` Docker volume with
daily bars for the Phase 1 universe. A27 satisfier per dev-guide §6.8
alternative (b) — operator-runbook with concrete fact-checks against the
real `lean_local` container and the `trading_lean_data` volume.

This runbook canonicalizes the 2026-05-12 seed ceremony (see
`Docs/decisions-log.md` entry "Phase 1 data-seed ceremony — ETF universe
seeded to `trading_lean_data`"). Re-run any time the operator needs to
extend the data window, refresh the seed against fresh yfinance data, or
add a new ticker to the universe.

**Scope (Phase 1, current state):** the 4 Phase 1 bond ETFs (`TLT IEF SHY TIP`)
are sourced from Yahoo Finance via the `yfinance` package — free, no API key.
The 7 Phase 1 micro futures (`/MES /MNQ /MYM /M2K /MGC /MCL /MBT`) are NOT
covered — those require a paid data subscription (QC AlgoSeek US Futures
Daily History, DataBento, or equivalent) and are tracked as a separate
cost-impact decision in the decisions-log.

## Prerequisites

* Ashburn VPS reachable via SSH (`ssh root@178.156.239.84`).
* `/opt/trading` checked out at `ebc625b` or later (post-PR #122).
* Operator's local workstation has Python 3.11+ installed.
* `lean_local` container running cleanly per `deploy/lean_local/README.md`.
* Audit chain CLEAN before the ceremony (`verify_chain --env paper` exit 0).
  Capture row count as baseline — it grows by N (= signals emitted at next
  21:30 UTC cycle) after the seed lands and the strategy generates signals.

---

## Step 1 — Stage the bundle on the operator's workstation

The `scripts/seed_lean_data.py` script fetches daily bars via yfinance and
emits a LEAN-format equity-daily bundle. Install `yfinance` in a venv to
avoid polluting the system Python:

```bash
cd /Users/<operator>/path/to/trading-system   # local repo

python3 -m venv /tmp/lean_seed_venv
/tmp/lean_seed_venv/bin/pip install --quiet --no-input yfinance

/tmp/lean_seed_venv/bin/python scripts/seed_lean_data.py
```

**Expected output:**

```
Tickers:   ['TLT', 'IEF', 'SHY', 'TIP']
Window:    2025-05-17 -> 2026-05-12 (exclusive end)
Exchange:  P (applied to all tickers)
Min bars:  225
Out:       /tmp/lean_seed/staged
---
  TLT: 250 rows  2025-05-19..2026-05-09  last_close=$85.42  zip=4011b
  IEF: 250 rows  2025-05-19..2026-05-09  last_close=$94.61  zip=3705b
  SHY: 250 rows  2025-05-19..2026-05-09  last_close=$82.20  zip=3128b
  TIP: 250 rows  2025-05-19..2026-05-09  last_close=$111.27 zip=3637b
---
Staged: 4 ticker(s) under /tmp/lean_seed/staged/equity/usa/
Tarball: /tmp/lean_seed/staged.tar.gz  (22531 bytes)
```

(Exact row counts + dates will differ; the structure is what matters.)

**Sanity-check the last-close against a public reference.** Pick one ticker
and compare its last-day close against Google Finance, Yahoo Finance's
website, or Schwab's quote page. The 2026-05-12 ceremony cross-checked TLT
$85.56 against the public $84.95-$85.46 range and accepted within tolerance.

**On mismatch:** if any ticker's row count is < 225, yfinance may be rate-
limited or have a data outage. Wait 5 minutes and retry, or extend the
window with `--start 2024-06-01` to capture more history.

### Customization

```bash
# Extend the window:
/tmp/lean_seed_venv/bin/python scripts/seed_lean_data.py --start 2024-01-01

# Add new tickers (e.g., add SPY for cross-check):
/tmp/lean_seed_venv/bin/python scripts/seed_lean_data.py --tickers TLT,IEF,SHY,TIP,SPY

# Use a different exchange code (Q for Nasdaq, N for NYSE):
/tmp/lean_seed_venv/bin/python scripts/seed_lean_data.py --tickers QQQ --exchange Q

# Skip the tarball (just emit the directory):
/tmp/lean_seed_venv/bin/python scripts/seed_lean_data.py --no-tar
```

---

## Step 2 — Stage onto the VPS

The tarball is the cleanest transfer unit (single file scp, single
`tar -xzf` extract):

```bash
scp /tmp/lean_seed/staged.tar.gz root@178.156.239.84:/tmp/lean_seed_etf.tar.gz

ssh root@178.156.239.84
mkdir -p /tmp/lean_seed_stage
tar -xzf /tmp/lean_seed_etf.tar.gz -C /tmp/lean_seed_stage

# Verify the 12 files extracted cleanly (4 zips + 4 map_files + 4 factor_files
# for the default 4-ticker run; scales linearly with --tickers count):
find /tmp/lean_seed_stage -type f | sort
```

**On mismatch:** if you see `._<filename>` AppleDouble sidecars in the
listing, your local tar shipped macOS metadata. Re-run with the canonical
flags: `COPYFILE_DISABLE=1 tar --no-mac-metadata --no-xattrs -czf …`. The
`scripts/seed_lean_data.py` tarball emission uses Python's `tarfile` module
which doesn't have this issue — only manually-built tars do.

---

## Step 3 — Copy into the `trading_lean_data` volume

The compose volume is named `trading_lean_data` (project-prefix `trading_`),
NOT the literal `lean_data`. Use a transient `alpine` container with both
the volume and the staging directory mounted:

```bash
docker run --rm \
  -v trading_lean_data:/Lean/Data \
  -v /tmp/lean_seed_stage:/seed:ro \
  alpine \
  cp -rv /seed/equity/. /Lean/Data/equity/
```

The trailing `/.` on the source is intentional — it merges directory
contents without overwriting the existing tutorial bundle (SPY, AAPL, QQQ,
etc.). The `:ro` flag on the seed mount is defense-in-depth — the cp is
write-only against the destination.

**Verify the files landed:**

```bash
docker run --rm -v trading_lean_data:/Lean/Data alpine sh -c '
  ls -la /Lean/Data/equity/usa/daily/ | grep -iE "tlt|ief|shy|tip"
  ls -la /Lean/Data/equity/usa/map_files/ | grep -iE "tlt|ief|shy|tip"
  ls -la /Lean/Data/equity/usa/factor_files/ | grep -iE "tlt|ief|shy|tip"
'
```

You should see 12 files total (4 each in daily/, map_files/, factor_files/).

---

## Step 4 — Restart `lean_local`

The container picks up the new data on restart — LEAN's
`SubscriptionDataReaderHistoryProvider` reads `/Lean/Data/` lazily on each
`self.history(symbol, count, Resolution.DAILY)` call, but the algorithm's
warmup happens at boot.

```bash
docker compose --env-file deploy/.env restart lean_local

# Wait ~10-30 seconds for boot. Watch for the initialize heartbeat:
docker logs trading-lean_local-1 --since 90s 2>&1 | \
  grep -E "lean_strategy_initialized|lean_signal_post_succeeded|Sequence contains no matching|exception"
```

**Expected (literal lines from 2026-05-12 ceremony):**

```
20260512 19:14:10.480 TRACE:: Log: 2026-05-12 15:14:09 v1_strategy initialized (post-pivot 2026-05-12, Pivot-PR-D) live_mode=True ...
20260512 19:14:10.480 TRACE:: Log: 2026-05-12 15:14:09 lean_signal_post_succeeded status=202 event_type=lean_strategy_initialized
```

**On mismatch:** any `Sequence contains no matching element` error means the
LEAN-side broker config is misaligned with the image. The post-ceremony
2026-05-12 PR #120 swapped LEAN-side broker to `PaperBrokerage`; if this
runbook fires on a container that still has `InteractiveBrokersBrokerage`
in `lean/lean.json`'s `paper-internal` env, it crash-loops. See
`Docs/decisions-log.md` 2026-05-12 entry "Post-ceremony session — LEAN
container's IBKR DLL gap" for full context.

**api side:**

```bash
docker logs trading-api-1 --since 90s 2>&1 | \
  grep -E "lean_event_received|lean_strategy_initialized"
```

Expected: 202 Accepted in <5ms for the `lean_strategy_initialized` event.

---

## Step 5 — Wait for the 21:30 UTC cycle + verify

The strategy's daily cycle fires at 17:30 ET = 21:30 UTC (after CME
settlement). After the cycle:

```bash
# LEAN side — at least one signal emitted:
docker logs trading-lean_local-1 --since 24h 2>&1 | grep "v1_signals_generated"

# api side — at least one signal_emitted POST received:
docker logs trading-api-1 --since 24h 2>&1 | grep "lean_event_received.*signal_emitted"

# signals table — at least one new row:
ssh root@178.156.239.84 'cd /opt/trading && export SOPS_AGE_KEY_FILE=/etc/credstore.encrypted/age_key && \
  sops --decrypt secrets/paper.enc.yaml > /dev/shm/paper.decrypted.yaml && \
  chmod 600 /dev/shm/paper.decrypted.yaml && \
  APP_SERVICE_PWD=$(awk "\$1 == \"app_service_password:\" {print \$2; exit}" /dev/shm/paper.decrypted.yaml) && \
  docker compose --env-file deploy/.env exec -T \
    -e PGPASSWORD="$APP_SERVICE_PWD" postgres \
    psql -U app_service -d trading -c "SELECT COUNT(*) FROM signals;" && \
  shred -u /dev/shm/paper.decrypted.yaml'

# audit chain — grew by N (where N = signals emitted), still CLEAN:
ssh root@178.156.239.84 # see deploy/audit/README.md Step 1-3 for verify_chain
```

If `signals_emitted_count=0` on the heartbeat, the strategy ran but no
Donchian breakouts qualified. That's a valid outcome — the strategy is
conservative by design. Watch for the next cycle (T+1 day at 21:30 UTC).

---

## Step 6 — Cleanup

```bash
ssh root@178.156.239.84 '
  rm -rf /tmp/lean_seed_stage
  rm -f /tmp/lean_seed_etf.tar.gz
'
```

The volume keeps the seeded data across docker compose down/up — named
volumes are persistent.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ModuleNotFoundError: No module named 'yfinance'` | venv not activated, or pip install failed | Re-run `/tmp/lean_seed_venv/bin/pip install yfinance` |
| `OperationalError: database is locked` from yfinance | yfinance's local SQLite cache contention | The script fetches per-ticker (not batched) to avoid this; if it persists, `rm -rf ~/.cache/py-yfinance` and retry |
| `Exit code 2: only N rows < 225` | yfinance returned insufficient data | Extend window: `--start 2024-01-01`. Or the ticker is fresh-listed (< 1 year history) — pick a different ticker |
| `Exit code 3: last_close <= 0` | Bad data from yfinance (rare) | Retry. If persistent, switch to a different data provider (Stooq, Tiingo) |
| LEAN `v1_history_unavailable` for ETF tickers post-seed | Map files or factor files missing | Step 3 missed map_files or factor_files; re-run `cp -rv /seed/equity/. /Lean/Data/equity/` ensuring all 3 subdirs land |
| 12 files staged but only 4 land in volume | Source path mismatch; `cp -rv /seed/. /Lean/Data/equity/` (note: source `/seed/.` instead of `/seed/equity/.`) | Re-run with the canonical `/seed/equity/.` source path |
| `_` AppleDouble files in volume | macOS tar leaked `._<file>` sidecars | scripts/seed_lean_data.py uses Python `tarfile` which avoids this — re-run via the script, NOT manual `tar -czf` |
| Boot crash with `Sequence contains no matching element` | LEAN-side broker mismatch (pre-PR #120 state) | Confirm `lean/lean.json::live-mode-brokerage` is `PaperBrokerage` per PR #120 |
| `MIN_HOLDING_DAYS` filter never blocks anything | LEAN's `self.portfolio[symbol]` returns flat under PaperBrokerage | Expected post-pivot — the api owns positions, not LEAN; tracked as Phase 1+ follow-up |

---

## Cross-references

* Build script: `scripts/seed_lean_data.py`
* 2026-05-12 ceremony record: `Docs/decisions-log.md` entry "Phase 1 data-seed ceremony"
* LEAN data-volume design: `lean/README.md` file-index `lean_data` row
* LEAN local container runbook: `deploy/lean_local/README.md`
* Audit chain verifier: `deploy/audit/README.md`
* Strategy parameter source-of-truth: `strategies/v1_trend_following/parameters.py`
