# Coinbase market-data worker — operator smoke runbook (C0-B2a)

A27 smoke fixture for `services/data/coinbase_market_data.py` (crypto-pivot
delta spec §3.2). The worker runs **inside the api container** (lifespan
task, same as the retired bar_sync worker) — there is no new container.
No API keys are involved: every endpoint it touches is public.

If anything fails, capture the exact error + step number and stop.
Root-cause discipline per dev-guide §1.3.

## What the worker does

| Job | Cadence | Output |
|---|---|---|
| WS ticker marks | continuous (30 s risk-loop consumer) | in-memory `MarkStore` |
| Funding logger | hourly (top of UTC hour) | `funding_rates` rows |
| Product metadata snapshot | daily 00:00 UTC | `product_metadata` rows |
| Spot daily-bar sample | daily 00:00 UTC | in-memory latest bars (log line) |
| Staleness watchdog | every 30 s tick | P2 `broker_disconnect` alert past 3 min |

## Smoke steps (VPS, after deploying a build containing this worker)

1. **Boot log.** `docker compose --env-file deploy/.env logs api | grep coinbase_market_data_worker_spawned`
   → one line with `ws_url=wss://advanced-trade-ws.coinbase.com` and
   `alert_dispatch_hook_wired=true` (false is acceptable pre-secrets-fill; alerts
   then log-and-drop as `coinbase_market_data_alert_dropped_no_hook`).
2. **WS connect.** `... logs api | grep coinbase_ws_connected` → shows the
   subscribed `product_ids` (must include `BTC-USD`, `ETH-USD`, plus every
   discovered `*-CDE` perp; discovery failure logs
   `coinbase_ws_product_discovery_failed` and subscribes spot only — that
   is a degraded state worth investigating, not a pass).
3. **Public REST reachability** (from the VPS, outside the container is fine):
   `curl -s "https://api.coinbase.com/api/v3/brokerage/market/products?product_type=FUTURE&get_all_products=true" | head -c 400`
   → JSON starting `{"products":[...`.
4. **Funding capture.** After the next top-of-hour:
   `docker compose --env-file deploy/.env exec postgres psql -U app_service -d trading -c "SELECT product_id, observed_at_utc, rate_per_interval FROM funding_rates ORDER BY observed_at_utc DESC LIMIT 5;"`
   → rows for the CDE perps. If instead the api logs
   `coinbase_funding_rate_unavailable`, that is strategy §11 open
   question 4 materializing (public payload carries no funding) —
   escalate to the operator for the data-source decision; do not
   improvise a scraper.
5. **Metadata snapshot.** After the next 00:00 UTC:
   `... psql ... -c "SELECT product_id, captured_at_utc, tick_size, contract_size FROM product_metadata ORDER BY captured_at_utc DESC LIMIT 5;"`
   → one row per perp product for today, plus a
   `coinbase_daily_bar_sampled` log line per spot product.
6. **Staleness watchdog drill (optional but recommended once):** block the
   WS egress (e.g. `iptables` drop to the WS host, or set
   `API_COINBASE_WS_URL=wss://invalid.invalid` and restart) → within
   ~3–6 min the api logs `coinbase_marks_stale` and (hook wired) one P2
   lands in Discord `#alerts`; restore and confirm
   `coinbase_marks_recovered`.

## Config knobs (deploy/.env, all optional)

- `API_COINBASE_MARKET_DATA_ENABLED=false` — kill switch for the worker.
- `API_COINBASE_REST_BASE_URL` / `API_COINBASE_WS_URL` — endpoint overrides.
- `API_COINBASE_MARKET_DATA_STALE_THRESHOLD_SECONDS` — locked at 180 by
  strategy §7; changing it is a strategy amendment, not tuning.
