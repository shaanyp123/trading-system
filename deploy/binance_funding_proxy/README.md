# Binance funding-rate proxy logger — operator smoke runbook

A27 smoke fixture for `services/data/binance_funding_proxy.py` (gate-B3
proxy funding series, C1→C2 build) and its one-shot companion
`scripts/operator_tools/backfill_binance_funding.py`. The job runs
**inside the api container** (lifespan task, same shape as the USDC
rewards capture) — no new container, **no credentials** (the endpoint is
public). Kill switch: `API_BINANCE_FUNDING_PROXY_ENABLED=false`.

**This is the codebase's first integration with a Binance API.** The
fact-checks below pin the platform contract. If anything fails, capture
the exact error + step number and stop; root-cause discipline per
dev-guide §1.3.

## What the job does

| Capture | Cadence | Output |
|---|---|---|
| `GET /fapi/v1/fundingRate` per symbol (BTCUSDT, ETHUSDT — derived from the spot signal universe), trailing 35 days | daily 00:40 UTC (+ once per restart, idempotently) | `funding_rates` rows, `source='binance_proxy'`, `interval_hours=8` |

Gate B3 (`GET /api/system/gates`) arms automatically once proxy rows
exist in the trailing 30-day window — no gate-code change shipped with
this job, by design (any non-`coinbase_advanced` source with a positive
mean counts).

## Proxy honesty + terms (strategy §9 ⚠️)

- Binance funding is **8-hourly with ±0.75% clamps** vs CDE's hourly
  smoothed mechanism; Binance books are ~100x deeper; USDT-margin basis
  differs from USD by bps. The series validates funding **magnitude**
  (B3's 2x band) — nothing finer. Rows are labeled `binance_proxy` so
  no consumer can mistake them for venue truth; nothing in the
  decision/risk path reads them.
- **Rate limits:** `/fapi/v1/fundingRate` is IP-weighted on the public
  fapi limit pool (weight "share 500/5min/IP" per the published
  changelog — see FC-3). This job makes **2 requests/day** (one per
  symbol; the backfill a handful more) — orders of magnitude below any
  limit; a 429/418 response would surface as
  `binance_funding_symbol_capture_failed` with the HTTP status.
- **Terms:** public market-data endpoints require no account or key;
  strategy §9 flags "verify current rate limits/terms of each at
  implementation time" — re-check
  https://www.binance.com/en/binance-api-terms if usage ever grows
  beyond telemetry polling. A US-IP note: `fapi.binance.com` serves
  market data globally, but if the VPS ever relocates somewhere Binance
  geo-blocks (it currently answers from US IPs for public data),
  the symptom is the same capture-failed log + gate B3 falling back to
  `insufficient_data` — the trading path is unaffected by construction.

## Platform-contract fact-checks (the §6.8 pins)

- **FC-1 — response shape:** `GET /fapi/v1/fundingRate?symbol=BTCUSDT&limit=5`
  returns a JSON **array** of objects with `symbol`, `fundingTime`
  (epoch **milliseconds**, int), `fundingRate` (decimal string), and
  `markPrice` (decimal string, may be empty on old rows). Verify with
  smoke step 3. On mismatch: the parser degrades rows to None and logs
  `binance_funding_rows_unparseable` — stop the job via the kill switch
  until re-pinned.
- **FC-2 — settlement cadence is 8-hourly** (00:00/08:00/16:00 UTC
  fundingTimes). `interval_hours=8` is persisted per row; if Binance
  ever changes cadence (they have — some symbols settle 4-hourly in
  extreme funding regimes), the stored `interval_hours` would be wrong
  for those rows. Check step 4's timestamps; a non-8h gap means the
  constant needs to become per-row derived.
- **FC-3 — rate-limit headers:** the response carries
  `X-MBX-USED-WEIGHT-1M`; at 2 calls/day this is noise, but a 429
  means the shared IP pool is exhausted by something else on the host.
- **FC-4 — `startTime` pagination:** rows return ascending from
  `startTime`, max `limit=1000`; the backfill advances `startTime` past
  the last row per page. Verified by the backfill's per-symbol count
  matching `(range days) × 3` (±1 at the edges).

## Smoke steps (VPS, after deploying a build containing this job)

1. **Boot log.**
   `docker compose --env-file deploy/.env logs api | grep binance_funding_proxy_spawned`
   → one line with `fire_time_utc=00:40:00` and
   `symbols=['BTCUSDT', 'ETHUSDT']`.
   `binance_funding_proxy_disabled_via_setting` means the kill switch is on.
2. **First capture fires on the restart re-fire** (any boot after 00:40
   UTC re-fires the day's job idempotently):
   `docker compose --env-file deploy/.env logs api | grep binance_funding_proxy_capture_completed`
   → `symbols_failed=0`, `rows_seen≈105` per symbol on first run,
   `rows_new>0` on the first run and `rows_new=0..3` on subsequent days.
3. **Contract probe (FC-1), from any shell:**
   `curl -s 'https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&limit=3' | python3 -m json.tool`
   → array of `{symbol, fundingTime, fundingRate, markPrice}` objects.
4. **Rows landed (+ FC-2 cadence check):**
   ```sql
   SELECT product_id, observed_at_utc, rate_per_interval, interval_hours
   FROM funding_rates WHERE source = 'binance_proxy'
   ORDER BY observed_at_utc DESC LIMIT 6;
   ```
   → alternating BTCUSDT/ETHUSDT rows at 8-hour spacings, `interval_hours=8`.
5. **Gate B3 armed:** `/gates` in Discord (or `GET /api/system/gates`) —
   B3 now reads `ratio …` green/red instead of
   `no proxy-source rows recorded`.
6. **(Optional) deep backfill:**
   ```bash
   docker exec -i trading-api-1 /opt/venv/bin/python -m \
     scripts.operator_tools.backfill_binance_funding \
     --start 2026-06-01 --end 2026-07-19          # dry run first
   ```
   then re-run with `--execute`; re-runs are idempotent.
