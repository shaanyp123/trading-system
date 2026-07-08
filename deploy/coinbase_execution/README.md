# Coinbase execution layer — operator canary runbook (C0-B2b)

A27 smoke fixture for `services/execution/coinbase_client.py` +
`coinbase_adapter.py` (crypto-pivot delta spec §3.1). These drills ARE
the C0 offline exit gates for the execution surface (delta spec §5:
"order-ladder + stop + restart-recovery integration tests against live
API in read-only/1-contract-canary form") — operator-run against the
real venue before C1 small-live starts.

If anything fails, capture the exact error + step number and stop.
Root-cause discipline per dev-guide §1.3. Coinbase API surprises that
contradict the strategy doc (e.g. stop-limit not accepted on `*-CDE`
products — strategy §11 open question 3) are OPERATOR ESCALATIONS, not
things to code around.

## Prerequisites

- CDP API key created (portal.cdp.coinbase.com → API keys, trade
  permission) and populated in sops: `coinbase.api_key_name` +
  `coinbase.api_private_key` (see `deploy/sops/secret_schemas/paper.template.yaml`).
- CFM futures account approved + funded with the C1 float.
- **Do NOT enable intraday margin anywhere in the Coinbase UI** — the
  system assumes the overnight margin regime 24/7 (locked; strategy §7).

## Phase 0 — read-only checks (no orders)

1. **Auth + discovery.** From the repo venv:
   `python3 -c "import asyncio; from services.execution.coinbase_client import SdkCoinbaseBrokerClient; import os; c = SdkCoinbaseBrokerClient(api_key_name=os.environ['CB_KEY'], api_private_key=os.environ['CB_PEM']); print(asyncio.run(c.list_perp_products()))"`
   → one `PerpProductRef` per CDE perp product with non-None
   `contract_size`/`tick_size`. Record the discovered product IDs —
   never hardcode them anywhere.
2. **Balance summary.** Same pattern with `get_futures_balance_summary()`
   → Decimals for total/cfm balances; note which margin fields the venue
   actually populates (strategy §11 open question 1).
3. **Top of book.** `get_best_bid_ask(<BTC perp product_id>)` → sane
   two-sided book.

## Phase 1 — 1-contract canary drills (real fills, nano size)

4. **Ladder drill.** Execute a +1 nano-BTC delta through
   `CoinbaseExecutionAdapter.execute_target_delta`. Verify in the log:
   `coinbase_ladder_completed` with `fully_filled=true`; check the fill
   on the venue matches `LadderExecutionResult.avg_fill_price`; record
   realized slippage vs the touch (gate B1's first data point).
5. **Stop drill (gate A2).** With the canary position open, call
   `ensure_native_stop(...)` with the current ATR inputs → expect
   `verified_resting=true` in <10 s and the stop-limit visible in the
   venue UI. If the venue REJECTS stop_limit on the CDE product —
   escalate to operator (strategy §11 open question 3).
6. **Restart drill (gate A3).** Kill the process; run
   `startup_reconcile()` → positions + resting stop reported,
   `unprotected_product_ids` empty. Re-run the SAME
   `execute_target_delta` inputs → the deterministic client_order_id
   must recover the existing order, NOT double-order (check
   `coinbase_order_recovered_by_client_id` in logs and exactly one
   position on the venue).
7. **Flatten.** `execute_target_delta` with the opposite delta →
   position closed; `cancel_all_orders()` → zero working orders.

## Config

- sops `coinbase.api_key_name` / `coinbase.api_private_key` →
  `API_COINBASE_API_KEY_NAME` / `API_COINBASE_API_PRIVATE_KEY`
  (mapped by `services/api/entrypoint.py`).
- Locked constants (strategy §5 — amendment required to change):
  post-only wait 600 s, IOC cross ±5 bps, native stop 3×ATR with limit
  1% through trigger, stop verify ≤10 s.
