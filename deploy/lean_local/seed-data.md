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

## Step 7 — Cross-check against an independent data source

After a new seed lands, validate it against a SECOND independent data
provider to catch a "garbage-in-garbage-out" failure mode (yfinance data
quality regression, accidental wrong-window seed, decoder off-by-10000,
etc.) BEFORE approving any live signal that consumes the bars.

The validation script `scripts/verify_seed_data.py` reads the on-disk
LEAN zips (read-only against the volume) and diffs the close prices
against either Stooq or Tiingo. Threshold: 1bp on the close-price
divergence (= 0.01% = ~$0.01 on a $85 ETF). Volume divergence is
informational only.

### Option A — Single-bar spot-check via Google Finance (zero infrastructure)

For a quick sanity check after a re-seed, browse to `https://www.google.com/finance/quote/<TICKER>:<EXCHANGE>` for each ticker (TLT:NASDAQ, IEF:NASDAQ, SHY:NASDAQ, TIP:NYSEARCA) and confirm the "Previous close" field matches the LEAN-decoded last bar.

The 2026-05-12 ceremony validated:

| Ticker | LEAN last close (2026-05-11) | Google Finance previous close | Divergence |
|---|---|---|---|
| TLT | $85.56 | $85.56 | 0bp |
| IEF | $94.64 | $94.64 | 0bp |
| SHY | $82.22 | $82.22 | 0bp |
| TIP | $111.31 | $111.31 | 0bp |

This is a single-bar check; it catches catastrophic garbage modes (wrong
magnitude, decoder bug, wrong-window seed) but does NOT catch an isolated
mid-history single-bar divergence.

### Option B — Full historical 360-row cross-check via Tiingo (recommended pre-live gate)

For the canonical pre-live gate (Week 8 pre-live checklist), use Tiingo
as the independent source (free tier: 1000 req/day on daily-OHLCV, well
above our 4-ticker scope). One-time setup:

1. Sign up at `https://www.tiingo.com/account/api` — free; gives the
   account-level API key.
2. Add to sops: `sops secrets/paper.enc.yaml` → append the key under a
   new `tiingo:` block:
   ```yaml
   tiingo:
     api_key: <token>
   ```
3. Commit + push the sops-encrypted secrets file.

Then run the script against the live volume:

```bash
# Stage the script on the VPS
scp scripts/verify_seed_data.py root@178.156.239.84:/tmp/verify_seed_data.py

# Decrypt sops + extract key + run cross-check (file-only secret handling)
ssh root@178.156.239.84 'bash -s' <<'REMOTE_EOF'
set -e
export SOPS_AGE_KEY_FILE=/etc/credstore.encrypted/age_key
sops --decrypt /opt/trading/secrets/paper.enc.yaml > /dev/shm/paper.decrypted.yaml
chmod 600 /dev/shm/paper.decrypted.yaml

# Extract the Tiingo key via python yaml.safe_load. Prior versions of this
# runbook used `awk "/^tiingo:/,/^[a-z]/" | awk "/api_key:/ {print $2; exit}"`
# which failed under bash-`-s` heredoc invocation against sops files using
# 4-space indentation (2026-05-12 session captured the bug). yaml.safe_load
# is the canonical extraction pattern for sops-decrypted nested keys.
eval "$(python3 << 'PYEOF'
import yaml
d = yaml.safe_load(open("/dev/shm/paper.decrypted.yaml"))
print(f'export TIINGO_KEY="{d.get("tiingo", {}).get("api_key", "")}"')
PYEOF
)"
shred -u /dev/shm/paper.decrypted.yaml

# Sanity-check key was captured (LENGTH only; NEVER echo the value itself)
echo "tiingo key length: ${#TIINGO_KEY}"
[ -z "$TIINGO_KEY" ] && { echo "ERROR: tiingo.api_key empty"; exit 3; }

docker run --rm \
  -v trading_lean_data:/Lean/Data:ro \
  -v /tmp/verify_seed_data.py:/verify.py:ro \
  -e TIINGO_KEY="$TIINGO_KEY" \
  python:3.11-slim \
  sh -c 'python /verify.py --data-dir /Lean/Data --source tiingo --tiingo-api-key "$TIINGO_KEY"'
unset TIINGO_KEY
REMOTE_EOF
```

**Expected output (clean case):**

```
LEAN seed verification vs 'tiingo'
Data dir:   /Lean/Data
Window:     2024-12-02 -> 2026-05-11 (inclusive on both ends)
Threshold:  1.0000bp on close-price divergence

Ticker     LEAN rows   Cross rows   Shared   Only LEAN   Only cross      Max bp   Breaches  Result
--------------------------------------------------------------------------------------------------
TLT              360          360      360           0            0    0.0000bp          0  PASS
IEF              360          360      360           0            0    0.0000bp          0  PASS
SHY              360          360      360           0            0    0.0000bp          0  PASS
TIP              360          360      360           0            0    0.0000bp          0  PASS
```

Exit code 0. **On exit 1** (any row > threshold), the per-ticker
"Worst-divergence rows" section lists the dates + LEAN close vs cross
close + signed bp divergence. Operator inspects, decides whether to:
(a) accept (e.g., a known dividend ex-date that yfinance handles
differently from Tiingo); (b) reject + re-seed from a different source.

**2026-05-12 actual run (Option B executed for the first time):** 4-of-4
PASS. Max divergence 0.6070bp on SHY (sub-cent on $82 ETF; well under
the 1bp threshold). All 360 dates match perfectly with 0 missing on
either side. ETF pre-live gate LOCKED. See `Docs/decisions-log.md`
2026-05-12 entry "Post-PR-#130 session continued" for the full result
table.

**On exit 2** (cross-source fetch failed): check the per-ticker
`FETCH FAILED — ...` line. The most common causes are rate-limiting
(Tiingo's free tier: 1000 req/day; we use ~4) or an invalid/expired key.

### Option C — Stooq (DEFERRED — API-gated as of 2026)

The script supports `--source stooq` but Stooq's public CSV endpoint
moved behind an API-key gate in mid-2024. Calls return a
"Get your apikey: ..." prompt body instead of CSV. To use Stooq, sign
up at `https://stooq.com/q/d/?s=tlt.us&get_apikey` (free + captcha),
then pass the key as a `&apikey=` URL parameter — current script
doesn't wire this; treat Stooq as TODO if/when operator prefers it
over Tiingo. Documented for institutional memory.

---

## Step 8 — Cross-check cleanup

After Step 7 completes (regardless of outcome), remove the transient
script staged on the VPS:

```bash
ssh root@178.156.239.84 'rm -f /tmp/verify_seed_data.py'
```

The cross-check is read-only against the volume; nothing in
`/Lean/Data/` is mutated. Re-running the cross-check at any later time
is a single `scp` + `docker run` cycle away.

---

## Futures seed via DataBento direct SDK

Companion to Steps 1-6 above. Where `scripts/seed_lean_data.py` covers
the 4 Phase 1 bond ETFs from Yahoo Finance, `scripts/seed_lean_futures_databento.py`
covers the 7 Phase 1 CME micro futures (`/MES /MNQ /MYM /M2K /MGC /MCL /MBT`)
from DataBento. The 11-market universe is fully covered once both seed
scripts have landed.

**Why DataBento direct SDK and not the lean CLI:** the lean CLI's
DataBento integration only supports `--data-type Trade` or `--data-type Quote`
(see `Docs/decisions-log.md` 2026-05-12 entry "Post-PR-#130 session
continued — Tiingo cross-check PASS; LEAN CLI's DataBento integration NOT
viable"). Our strategy needs Trade + Open Interest + per-day Universe data
because `lean/v1_strategy.py` uses `data_mapping_mode=DataMappingMode.OPEN_INTEREST`
to pick the front-month contract by highest OI. The DataBento Python SDK
exposes all 3 schemas (`ohlcv-1d`, `statistics`, `definition`); the custom
converter writes them in the LEAN on-disk format LEAN expects.

**Cost expectation:** ~$0.96 total for the 7-micro × 23-month default
fetch, well within DataBento's $125 free credit (130x headroom).

### Prerequisites for the futures seed

* `databento.api_key` is populated in `secrets/paper.enc.yaml` (signup
  at databento.com; the key is 32 chars; PR #131).
* The 4 ETFs from Steps 1-5 are already seeded into `trading_lean_data`
  (the strategy initializes both equity + future subscriptions at boot;
  if the ETF subscriptions fail at warmup, the cycle won't reach the
  futures path).

### Step F1 — Stage the bundle on the operator's workstation

```bash
cd /Users/<operator>/path/to/trading-system   # local repo

# Decrypt sops to surface the DataBento key (file-only, never to stdout)
export SOPS_AGE_KEY_FILE="$HOME/.config/sops/age/keys.txt"
sops --decrypt secrets/paper.enc.yaml > /tmp/paper.dec.yaml
chmod 600 /tmp/paper.dec.yaml

# Install databento SDK in a venv (NOT a pyproject.toml dep — operator-side
# ceremony script, same precedent as yfinance for the ETF seed).
python3 -m venv /tmp/db_venv
/tmp/db_venv/bin/pip install --quiet --no-input "databento>=0.78" pyyaml

# Resolve the API key via Python + yaml.safe_load (the awk pattern that
# was buggy in PR #132 is now standardized on this idiom).
export DATABENTO_API_KEY=$(/tmp/db_venv/bin/python -c "import yaml; print(yaml.safe_load(open('/tmp/paper.dec.yaml'))['databento']['api_key'])")
echo "Key length: ${#DATABENTO_API_KEY}"  # expect 32

# Run the converter (default: all 7 micros, 23-month window, --yes skips prompt)
/tmp/db_venv/bin/python scripts/seed_lean_futures_databento.py --yes
```

**Expected output (≈70 minutes wall-clock for the full 23-month fetch
because the statistics schema returns ~8.1M records):**

```
Tickers:   ['MES', 'MNQ', 'MYM', 'M2K', 'MBT', 'MGC', 'MCL']
Window:    2024-06-01 -> 2026-05-12 (exclusive end)
Dataset:   GLBX.MDP3
Out:       /tmp/lean_seed_futures

Quoting cost (3 schemas, parent stype)...
  ohlcv-1d   : $ 0.3495
  statistics : $ 0.4853
  definition : $ 0.1234
  TOTAL      : $ 0.9582

---
  MES  (cme   ):  XX expiries  XXXXX trade rows  XXXXX OI rows  XXX universe days  ...
  ...
---
Staged: 7 ticker(s) under /tmp/lean_seed_futures/future/
Tarball: /tmp/lean_seed_futures.tar.gz  (XXXX bytes)
```

Cost-quote tripwire: the script aborts with exit 7 if the quoted cost
exceeds 5x the pre-quote (~$4.80 ceiling). If you see this, something
is wrong with the symbology or window — investigate before re-running.

The script removes any pre-existing `--out` directory at start
(`shutil.rmtree` then `mkdir`), so re-running cleanly overwrites prior
output.

**Single-ticker smoke test (~$0.003 cost, 30s wall-clock):**

```bash
/tmp/db_venv/bin/python scripts/seed_lean_futures_databento.py \
  --tickers MES --start 2026-04-01 --end 2026-05-01 --out /tmp/test_mes --no-tar --yes
```

Use this before the full fetch to confirm the converter is producing
valid output (5-6 expiries, ~90 trade rows, ~25 universe days).

### Step F2 — Stage onto the VPS

```bash
scp /tmp/lean_seed_futures.tar.gz root@178.156.239.84:/tmp/lean_seed_futures.tar.gz

ssh root@178.156.239.84
mkdir -p /tmp/lean_seed_futures_stage
tar -xzf /tmp/lean_seed_futures.tar.gz -C /tmp/lean_seed_futures_stage

# Verify the futures directories landed (3 markets — cme, comex, nymex):
find /tmp/lean_seed_futures_stage -maxdepth 3 -type d | sort
```

### Step F3 — Copy into the `trading_lean_data` volume

```bash
docker run --rm \
  -v trading_lean_data:/Lean/Data \
  -v /tmp/lean_seed_futures_stage:/seed:ro \
  alpine \
  cp -rv /seed/future/. /Lean/Data/future/
```

The trailing `/.` on the source merges into the existing future/ tree
(which contains the bundled ES + HSI tutorial bundles + cme/comex/nymex
market subdirs from the base image) without overwriting them.

**Verify the files landed:**

```bash
docker run --rm -v trading_lean_data:/Lean/Data alpine sh -c '
  for t in mes mnq mym m2k mbt; do
    echo "--- $t (cme) ---"
    ls -la /Lean/Data/future/cme/daily/${t}_trade.zip /Lean/Data/future/cme/daily/${t}_openinterest.zip 2>/dev/null
    ls /Lean/Data/future/cme/universes/$t/ 2>/dev/null | wc -l
    cat /Lean/Data/future/cme/map_files/$t.csv 2>/dev/null
  done
  echo "--- mgc (comex) ---"
  ls -la /Lean/Data/future/comex/daily/mgc_*.zip 2>/dev/null
  echo "--- mcl (nymex) ---"
  ls -la /Lean/Data/future/nymex/daily/mcl_*.zip 2>/dev/null
'
```

### Step F4 — Restart `lean_local` and verify

```bash
docker compose --env-file deploy/.env restart lean_local

# Watch the boot logs — expect lean_strategy_initialized 202 (no "factor_files
# not found" or "Map file not found" errors):
docker logs trading-lean_local-1 --since 90s 2>&1 | \
  grep -E "lean_strategy_initialized|lean_signal_post_succeeded|factor_files|Map file|Sequence contains"
```

**Note: Raw mode is already explicit in `lean/v1_strategy.py`.** The
strategy's `add_future()` calls hard-code `data_normalization_mode=DataNormalizationMode.RAW`
(this PR added it explicitly because `add_future()`'s implicit default
falls back to `BackwardsRatio` per the QC forum staff response on
discussion 17093, NOT to `Raw` as we initially assumed). If you still
see "factor_files not found" at boot after this seed lands, the
implicit-default path was somehow restored — check the strategy code
hasn't drifted from the explicit-Raw configuration.

**If LEAN errors with "Map file not found" or universe-related errors:**
the 2-row sentinel map_file may not be sufficient for LEAN's continuous-
contract construction. Fallback options:
1. Emit explicit per-roll map_file entries with synthesized QC-internal
   contract hashes (complex; involves reverse-engineering LEAN's
   continuous-contract math).
2. Escape hatch: continuous-front-month via `add_equity` instead of
   `add_future`. This requires modifying `strategies/v1_trend_following/parameters.py`
   (`risk-review-approved` PR) but removes LEAN's continuous-contract
   dependency entirely. See `Docs/decisions-log.md` 2026-05-12 entry
   "Phase 1 futures-data path memo" Option C.

### Step F5 — Wait for 21:30 UTC cycle + verify

Same as Step 5 for the ETF seed. Expected at the FIRST cycle after the
futures seed lands:

```
v1_history_unavailable session_date=YYYY-MM-DD failed_markets=[]
v1_signals_generated session_date=YYYY-MM-DD signals_emitted_count=N rejections_count=M
```

Where N+M=11 (all 11 markets parse cleanly). The strategy's
`MIN_HOLDING_DAYS=14` filter and conservative Donchian-breakout logic
will probably reject most candidates — Phase 1 day-one signal count of
0-2 is realistic + expected if the universe is sideways.

### Step F6 — Cleanup

```bash
ssh root@178.156.239.84 '
  rm -rf /tmp/lean_seed_futures_stage
  rm -f /tmp/lean_seed_futures.tar.gz
'

# Local workstation cleanup:
shred -u /tmp/paper.dec.yaml
rm -rf /tmp/lean_seed_futures /tmp/test_mes
```

The volume keeps the seeded futures data across docker compose down/up —
named volumes are persistent.

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
| Step 7 cross-check: all 4 tickers `FETCH FAILED — ... 'Get your apikey:'` | Stooq's public CSV endpoint is now API-gated (mid-2024) | Switch to `--source tiingo` after operator-side signup at `tiingo.com`; see Step 7 Option B |
| Step 7 cross-check: `cross-source fetch failed: ValueError: tiingo: --tiingo-api-key required` | Forgot to pass `--tiingo-api-key` (or env var blank) | Pass `--tiingo-api-key "$TIINGO_API_KEY"` (env var captured from sops) |
| Step 7 cross-check: 1 or more tickers `FAIL` with worst-row divergence > 1bp | yfinance data quality regression OR a known dividend ex-date OR factor-file mismatch | Inspect the worst-rows table for which dates flag; cross-reference against `dividend_history` for known ex-dates; decide accept (corner case) vs re-seed |
| `ModuleNotFoundError: No module named 'databento'` (futures seed) | venv not activated, or pip install failed | Re-run `/tmp/db_venv/bin/pip install "databento>=0.78"` |
| Futures `Exit code 5: DataBento API key not provided` | `DATABENTO_API_KEY` env var blank | Re-run sops decrypt + yaml.safe_load extraction; verify `${#DATABENTO_API_KEY}` is 32 chars |
| Futures `Exit code 7: cost quote ... exceeds ...` | Symbology error in `--tickers` or window way larger than expected | Re-quote a single ticker first; check `DEFAULT_UNIVERSE` parent symbol mapping |
| Futures `BentoWarning: reduced quality: <date> (degraded)` | DataBento marked a single trading day as degraded quality on the upstream venue | Informational; data still ingests. Cross-check the bar on that date against an independent source before approving signals derived from it |
| LEAN boot crash `factor_files not found` (futures) | The strategy's explicit `data_normalization_mode=DataNormalizationMode.RAW` (added in this-PR) was reverted or the implicit BackwardsRatio default is back | Re-check `lean/v1_strategy.py` `add_future()` calls — each must include `data_normalization_mode=DataNormalizationMode.RAW` per QC forum discussion 17093 (the implicit default is BackwardsRatio, which DOES need factor_files we don't have) |
| LEAN boot crash `Map file not found` (futures) | 2-row sentinel map_file insufficient for LEAN's continuous-contract construction | Consider escape-hatch: continuous-front-month via `add_equity` (Path C in 2026-05-12 futures-data memo; requires `risk-review-approved` PR) |
| Cycle still shows `failed_markets=['/MES', ...]` after futures seed | Path filter — staging didn't merge into volume correctly | Re-run Step F3 with explicit volume path; verify via the post-Step-F3 cross-check loop |

---

## Cross-references

* Build script: `scripts/seed_lean_data.py`
* Cross-check script: `scripts/verify_seed_data.py`
* 2026-05-12 ceremony record: `Docs/decisions-log.md` entry "Phase 1 data-seed ceremony"
* 2026-05-12 cross-check record: `Docs/decisions-log.md` entry "ETF data-quality cross-check"
* LEAN data-volume design: `lean/README.md` file-index `lean_data` row
* LEAN local container runbook: `deploy/lean_local/README.md`
* Audit chain verifier: `deploy/audit/README.md`
* Strategy parameter source-of-truth: `strategies/v1_trend_following/parameters.py`
