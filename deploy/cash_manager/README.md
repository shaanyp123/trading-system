# Cash-yield worker (delta spec §3.6) — C2 activation runbook

**Status: DORMANT.** `services/risk/cash_manager.py` ships behind
`API_CASH_MANAGER_ENABLED` (pydantic `cash_manager_enabled`, default
`false`). While false the api lifespan starter early-returns: no
scheduler task, no venue client, no sweep path exists at runtime.

**Activation is a C2 operator decision** gated on delta-spec open
question #1 (same-day reclaim verified live) and must be recorded in
`Docs/decisions-log.md` before the flag is flipped.

## What it does when enabled

Daily at **00:25 UTC** (after the 00:15 recon and the 00:20 cash
capture), the worker:

1. Reads the venue balance summary + positions, the latest
   `product_metadata` contract sizes, and the latest
   `cash_balance_snapshots.cbi_usdc` capture.
2. Computes `target = initial_margin + 25% x gross notional +
   operational headroom ($750 default)` and the floor guard
   (`$1,500 halt floor + headroom`). Visible USD equity is **never**
   swept below `max(target, floor guard)` — a sweep cannot manufacture
   the Jul-17 zero-headroom shape.
3. Excess ≥ $50 → `to_yield` (USD→USDC convert). Deficit ≥ $50 →
   `to_margin` (USDC→USD convert + `schedule_futures_sweep`), bounded by
   the known USDC balance.
4. Every sweep: audit event FIRST (existing capital-event audit types
   with the `"kind": "cash_sweep_internal_transfer"` payload
   discriminator — see the module docstring), then a `cash_sweeps` row
   (`requested` → `completed`/`failed`). **Never** a `capital_events`
   row — sweeps are internal transfers and must not trip the 5% capital
   event mode or dd-baseline fields.

Any missing/malformed input (equity, margin, marks, contract sizes,
USDC balance for a reclaim) produces a named no-op — the worker never
sweeps on partial data.

## HARD GATES — sweep-blind consumers (MUST be resolved before first enable)

The sweep is guarded against the *absolute* floor, but three existing
consumers measure *relative* baselines that do NOT read `cash_sweeps`
(risk-review 2026-07-18, blocker 2). Activating without resolving these
produces a spurious P0 halt and permanent recon breakage on day one:

1. **Daily loss limit (`strategy_worker` 5b):** `equity /
   day_start_equity − 1` with the baseline captured at ~00:00 UTC — 25
   minutes BEFORE the 00:25 sweep. A `to_yield` sweep > 8% of day-start
   equity reads as a same-day loss → spurious flatten + HALT_NEW within
   one risk tick (the first-activation sweep at current balances is
   ~40%+). Worse, a `to_margin` reclaim inflates equity vs the stale
   baseline and can MASK a genuine −8% breach (a loosening).
2. **Weekly loss limit (5c):** same distortion via `strategy_decisions`
   equity history → spurious 7-day V_target halving after a large sweep.
3. **EOD recon cash compare:** backend cash is fills-derived; venue cash
   is live. Every completed sweep diverges the two by its amount,
   CUMULATIVELY — the min $50 sweep already exceeds the max($5, 1 bps)
   tolerance, and ≥$1,000 net drift escalates P0.

**Required before the flag is flipped:** a consumer-adjustment PR
([A02]) that nets completed `cash_sweeps` out of the daily/weekly
baselines and the recon backend-cash figure (or explicitly re-bases
them at sweep time), reviewed and merged. Do NOT enable on the strength
of this runbook alone.

Also before C2: make `scripts/operator_tools/reconcile_statement.py`
sweep-aware (its "conversion"/"transfer" keywords currently classify
sweep lines as `capital_event` — cross-reference `cash_sweeps` so daily
conversions don't pollute the A1 statement-match categories).

## A27 fact-check checklist (MUST complete before first enable)

The conversion + sweep SDK wrappers (`SdkCashSweepVenueClient`) are
UNVERIFIED against the live venue. On the activation drill, with a
1-contract-scale test amount:

1. **Convert quote contract:** `create_convert_quote(from_account,
   to_account, amount)` — verify the response carries `trade.id` and
   whether accounts are specified by currency code or account UUID
   (the wrapper currently passes currency codes; fix here if wrong).
2. **Commit contract:** `commit_convert_trade(trade_id, ...)` — verify
   required params and that conversion settles instantly (1:1, no fee).
   **If settlement is asynchronous, the crash-recovery re-plan argument
   weakens:** a restart inside the settlement window sees pre-settlement
   balances and can double-sweep. Verify balance reflection BEFORE
   trusting same-day scheduler re-fires.
3. **Sweep contract:** `schedule_futures_sweep(usd_amount)` — verify
   the param name and that the sweep lands same-day; note the venue's
   processing window.
4. **Same-day reclaim end-to-end (delta-spec open question #1):**
   USDC→USD convert + sweep must be usable for margin the SAME UTC day.
   Time it. If it cannot be verified, the worker stays dormant.
5. **Rewards accrual residual:** confirm Coinbase One USDC rewards
   accrue on the full CBI USDC balance (delta spec §3.6 residual).
6. **Equity-visibility check:** after a small `to_yield` sweep, confirm
   `equity_from_summary` drops by exactly the swept amount and the
   00:20 capture reflects the new USDC balance next day (the Amendment C
   floor basis input).
7. Record every observed contract detail in the decisions-log and fold
   corrections into `SdkCashSweepVenueClient` before scaling amounts.

## Enable / disable

```bash
# VPS: /opt/trading/deploy/.env  (api container env)
API_CASH_MANAGER_ENABLED=true    # C2 decision only
```

Redeploy the api container; look for `cash_manager_ENABLED_spawned`
(WARNING level — deliberately loud) in the logs. Disable by removing
the var (default false) and redeploying; `cash_manager_dormant_via_setting`
confirms dormancy.

## Verification queries

```sql
SELECT * FROM cash_sweeps ORDER BY requested_at_utc DESC LIMIT 5;
-- Stuck rows (crash between INSERT and terminal UPDATE — ledger lies,
-- money is safe; investigate + resolve manually):
SELECT * FROM cash_sweeps WHERE status = 'requested'
  AND requested_at_utc < now() - INTERVAL '1 hour';
-- audit_log stores JCS bytes, not JSONB — decode before filtering:
SELECT event_type,
       convert_from(payload_jcs, 'UTF8')::jsonb->>'direction' AS direction,
       convert_from(payload_jcs, 'UTF8')::jsonb->>'amount_usd' AS amount_usd
FROM audit_log
WHERE convert_from(payload_jcs, 'UTF8')::jsonb->>'kind' = 'cash_sweep_internal_transfer'
ORDER BY sequence_no DESC LIMIT 5;
-- MUST return zero rows (baseline-pollution guard):
SELECT COUNT(*) FROM capital_events WHERE audit_event_uuid IN
  (SELECT event_uuid FROM audit_log
   WHERE convert_from(payload_jcs, 'UTF8')::jsonb->>'kind' = 'cash_sweep_internal_transfer');
```
