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

## HARD GATES — sweep-blind consumers (RESOLVED 2026-07-19; activation still gated)

**The consumer-adjustment PR has LANDED** (C1→C2 follow-up PR 2,
decisions-log 2026-07-19 — the [A02] prerequisite this section used to
demand). All three formerly sweep-blind consumers now net completed
`cash_sweeps` (`status='completed'`, `Σ to_yield − Σ to_margin`,
windowed by `completed_at_utc`):

1. **Daily loss limit (`strategy_worker` 5b):** measures
   `(equity + net_swept_out_since_baseline) / day_start_equity − 1`,
   windowed from the persisted baseline-capture instant
   (`strategy_worker_status.day_start_captured_at_utc`). A `to_yield`
   sweep no longer reads as a loss; a `to_margin` reclaim no longer
   masks a genuine −8% breach. Test-pinned in both directions.
2. **Weekly loss limit (5c):** same netting, windowed from the week-ago
   decision row's write instant.
3. **EOD recon cash compare:** the backend cash figure subtracts net
   sweeps completed after the latest venue-sourced (`coinbase_eod`)
   balances row — the venue row re-bases to truth daily, so the netting
   window never double-counts. Test-pinned in both directions.

Fail-safe on every consumer: if the sweeps read fails (or a netting
window has no anchor), the consumer computes the UNADJUSTED pre-PR
value and logs `*_sweep_netting_*_unadjusted` at WARNING. With zero
`cash_sweeps` rows — today's dormant reality — all three consumers are
bit-identical to pre-PR behavior (test-pinned).

**Activation remains gated** on the [A27] venue drill below (the
conversion/sweep SDK wrappers are still unverified live, incl.
delta-spec open question #1: same-day reclaim) **and the C2 operator
decision recorded in the decisions-log.** Do NOT flip the flag on the
strength of the consumer PR alone.

`scripts/operator_tools/reconcile_statement.py` sweep-awareness LANDED
(2026-07-20, decisions-log entry of the same date): conversion/transfer/
sweep-shaped statement lines matching a completed `cash_sweeps` row
(same |amount|, completed within ±1 UTC day) classify as `sweep`, not
`capital_event`; a completed sweep with no statement line is reported
loudly and flips the verdict to REVIEW. With zero `cash_sweeps` rows the
tool is bit-identical to its sweep-blind behavior (test-pinned).

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
