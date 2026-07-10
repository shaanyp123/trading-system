# USDC rewards + cash-balance capture — operator smoke runbook

A27 smoke fixture for `services/data/usdc_rewards.py` (USDC-interest
capture, decisions-log 2026-07-10). The job runs **inside the api
container** (lifespan task, same as the recon scheduler) — there is no
new container. It authenticates with the SAME CDP key pair the
execution/recon surfaces use (`coinbase.api_key_name` +
`coinbase.api_private_key` in the host secrets file); no new secrets.

**This is the codebase's first integration with the Coinbase v2 ledger
API** (`GET /v2/accounts/{currency}/transactions` — a different API
family from the Advanced Trade v3 endpoints the other runbooks cover).
The fact-checks below pin that platform contract. If anything fails,
capture the exact error + step number and stop; root-cause discipline
per dev-guide §1.3.

## What the job does

| Capture | Cadence | Output |
|---|---|---|
| v2 USDC ledger poll (candidate reward transactions) | daily 00:20 UTC (+ once per restart, idempotently) | `usdc_reward_transactions` rows |
| `get_accounts` cash snapshot (spot USD + CBI USDC) | daily 00:20 UTC (first capture of the UTC day wins) | `cash_balance_snapshots` row |

The persistence filter is DELIBERATELY LOOSE until the first Friday
reward payout is observed: everything that isn't `buy`/`sell`/`send`
persists, and unfamiliar types log `usdc_ledger_unknown_transaction_type`.
The follow-up filter-lock PR is triggered by observing the real payout
type in that warning / the table.

## Platform-contract fact-checks (the §6.8 pins)

- **FC-1 — v2 ledger is readable with the CDP key and returns the
  `data` + `pagination` envelope.** Verified live 2026-07-09 (operator
  session probe: `RESTClient.get('/v2/accounts/USDC/transactions')`
  returned the account's buy/sell conversion rows). Re-verify any time
  with smoke step 3 below. On mismatch (401/404 or a different
  envelope): the v2 family may have changed — check
  https://docs.cdp.coinbase.com/coinbase-app/track/api/transactions and
  stop the job via `API_USDC_REWARDS_CAPTURE_ENABLED=false` until the
  parser is re-pinned.
- **FC-2 — v2 list pagination**: `limit` (max 100), `order=desc`
  (explicitly passed — newest-first is load-bearing for the 20-page
  cap), cursor via `pagination.next_starting_after` → `starting_after`.
  On mismatch: `coinbase_cash_ledger_page_cap_hit` warnings with
  missing recent rows are the symptom.
- **FC-3 — `get_accounts` (Advanced Trade v3) returns per-account
  `available_balance` as a `{value, currency}` money object**, paginated
  via `has_next`/`cursor`. Same contract the execution transport's
  balance parsing relies on.

## Smoke steps (VPS, after deploying a build containing this job)

1. **Boot log.**
   `docker compose --env-file deploy/.env logs api | grep usdc_rewards_capture_spawned`
   → one line with `fire_time_utc=00:20:00`. If instead you see
   `usdc_rewards_capture_coinbase_credentials_missing`, fill the
   `coinbase.*` secrets and restart (same contract as the recon
   scheduler). `usdc_rewards_capture_disabled_via_setting` means the
   kill switch is on.
2. **First capture fires on the restart re-fire** (any boot after 00:20
   UTC fires the same day's capture within ~60 s — idempotent, safe):
   `... logs api | grep usdc_rewards_capture_completed`
   → carries `ledger_rows_seen` / `ledger_rows_persisted` /
   `ledger_rows_new` / `snapshot_written=true`. Conversion-only history
   (pre-first-payout) legitimately shows `ledger_rows_persisted=0`.
3. **v2 ledger reachability (FC-1), in-container** (credentials never
   render on screen — count keys, print lengths only):
   ```
   docker compose --env-file deploy/.env exec api python -c "
   import json, yaml
   s = yaml.safe_load(open('/run/secrets/secrets.yaml'))
   from coinbase.rest import RESTClient
   c = RESTClient(api_key=s['coinbase']['api_key_name'], api_secret=s['coinbase']['api_private_key'])
   body = c.get('/v2/accounts/USDC/transactions', params={'limit': 3, 'order': 'desc'})
   print('keys:', sorted(body.keys()))
   print('rows:', len(body.get('data', [])))
   print('types:', [r.get('type') for r in body.get('data', [])])"
   ```
   → `keys:` must include `data` and `pagination`; `types:` shows the
   ledger's transaction types (this is also how the operator reads the
   REAL reward type after the first Friday payout, to feed the
   filter-lock follow-up).
4. **Rows landed.**
   `docker compose --env-file deploy/.env exec postgres psql -U app_service -d trading -c "SELECT snapshot_date_utc, spot_usd, cbi_usdc FROM cash_balance_snapshots ORDER BY snapshot_date_utc DESC LIMIT 3;"`
   → today's row with plausible balances (spot USD ≈ the venue app's
   spot cash; cbi_usdc ≈ the USDC balance). And after the first payout:
   `... -c "SELECT venue_created_at_utc, transaction_type, amount FROM usdc_reward_transactions ORDER BY venue_created_at_utc DESC LIMIT 5;"`
5. **Strip render.** Dashboard Today page → the Funding & yield strip
   shows Spot USD / CBI USDC (with as-of tooltip) and Last reward /
   Lifetime rewards ("—" until the first payout). Equivalent curl:
   `curl -su "<basic-auth>" https://<apex>/api/system/funding | python3 -m json.tool | grep -A1 usdc`
6. **Unknown-type watch (the observe-then-lock loop).**
   `... logs api | grep usdc_ledger_unknown_transaction_type`
   → expected to fire once per novel type. When the Friday payout's
   type appears here, open the filter-lock follow-up PR.

## Config knobs (deploy/.env)

- `API_USDC_REWARDS_CAPTURE_ENABLED=false` — kill switch for the job
  (the api and every other worker are unaffected).

## Failure semantics

Both capture halves are independently best-effort: a venue or DB error
logs (`usdc_rewards_ledger_capture_failed` /
`usdc_rewards_balance_snapshot_failed`) and the other half still runs;
the scheduler survives and retries at the next daily fire (or the next
restart), idempotently. The job's death is visible in
`async_task_died` logs but deliberately does NOT page — telemetry only,
no risk surface depends on it.
