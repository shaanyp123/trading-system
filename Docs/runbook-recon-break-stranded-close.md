# Runbook — EOD recon break after a venue-side close (stranded close fill)

**Added 2026-07-16** in response to the operator-reported incident "position closed
last night + EOD recon break." Generalizes the night-three (2026-07-12) repair
into a reusable ceremony. Authority for the failure class:
`Docs/decisions-log.md` 2026-07-12 "C1 night three" entry + PR #377.

## What this break class is

The strategy legitimately closes a position (decision-driven exit, client-stop
flatten, or native-stop fill), the **venue fills the close**, but the backend
fill propagation fails or is deferred afterward. Result:

* Venue book: **flat** (the close is a fact at Coinbase).
* Backend book: `positions_current` keeps the stale quantity; the `trades` row
  stays `open_position`.
* The next 00:15 UTC recon **correctly** flags a `position_qty` break
  (backend ≠ venue) and auto-halts (`HALT_NEW` via `RECON_MISMATCH`).
* Every subsequent nightly recon re-observes the same break (grace-period
  continuation — no new alert, which is why a P2 first alert is easy to miss)
  until the book is repaired. The break row itself auto-resolves
  (`auto_rereconciled`, PR #385) on the first cycle after the divergence is
  gone — **only the book repair is manual.**

This is a *data-real, substance-phantom* break: the divergence is real, but the
economic substance (the close) already happened at the venue. The halt + break
are the safety net working as designed; the repair re-records reality.

## Step 0 — Confirm the shape (paste-ready, read-only)

Postgres requires a password even for in-container `psql`
(`--auth-local=scram-sha-256` per the compose file); the superuser password
lives in `deploy/.env` on the VPS — you never type it by hand. Paste this
preamble ONCE per SSH session (same pattern `deploy/day5-bringup.sh` uses),
then use the `psq` helper for every query in this runbook:

```
cd /opt/trading
set -a; source deploy/.env; set +a
psq() { docker compose --env-file deploy/.env exec -T \
  -e PGPASSWORD="${POSTGRES_SUPERUSER_PASSWORD}" \
  postgres psql -U postgres -d trading -c "$1"; }
```

Then:

```
psq "SELECT state, severity, reason, entered_at_utc FROM risk_state WHERE is_current;"
psq "SELECT market, quantity, avg_cost, last_mark_ts FROM positions_current;"
psq "SELECT metric, market, expected, actual, delta, detected_at_utc, resolved_at_utc, resolution_path \
     FROM reconciliation_breaks ORDER BY detected_at_utc DESC LIMIT 5;"
psq "SELECT id, market, direction, state, total_quantity, opened_at_utc, closed_at_utc \
     FROM trades ORDER BY created_at DESC LIMIT 5;"
```

**Shape match:** `risk_state` = `HALT_NEW`; `positions_current` shows a non-zero
row for a market the Coinbase UI shows flat; an unresolved `position_qty` break
whose `expected` (backend) is the stale quantity and `actual` (venue) is 0; an
`open_position` trade for the same market. If the venue is NOT flat (e.g. venue
shows a partial remainder, or a position the backend lacks), **stop here and
report the query output back** — that is a different shape (partial venue fill
or naked-order fill) and blind replay would mis-record it.

## Step 1 — Identify the close leg + how propagation failed

Find the close leg's `client_order_id` and status (the decision outcome JSON
carries the ladder stages; the first stage's cid is the leg cid):

```
psq "SELECT decision_date, status, outcome FROM strategy_decisions ORDER BY decision_date DESC LIMIT 3;"
psq "SELECT client_order_id, market, direction, quantity, status, created_at \
     FROM orders ORDER BY created_at DESC LIMIT 8;"
```

Then pull the worker's own account of the failure (pick the close night's date):

```
journalctl CONTAINER_NAME=trading-strategy_worker-1 --since "2026-07-15 00:00" --until "2026-07-15 00:20" \
  | grep -E "fill_propagation_failed_fallback|fill_scenario_deferred|ladder_incomplete|fill_unknown_order|decision_failed|error" | head -40
```

Interpretation:

| Evidence | Meaning | Repair path |
|---|---|---|
| `orders` row **`pending`**, no `fills` row, decision `failed` | Propagation crashed mid-apply (night-three shape) | Step 2, no `--force` |
| **NO `orders` row at all** for the close leg + decision `failed` ("unhandled exception") | The leg crashed AFTER the venue fill but BEFORE `insert_order_row` (2026-07-16 variant) — only the leg's `signals` row exists | Step 2 with `--create-order-row --signal-id <uuid> --order-direction buy\|sell` (find the signal id via the query below) |
| `orders` row **`filled`** + `fills` row present + `strategy_worker_fill_propagation_failed_fallback` or `..._fill_scenario_deferred` in logs | The #386 minimal fallback recorded orders/fills honestly but deliberately did NOT touch positions/trades | Step 2 with `--force` (expect one duplicate `fills` row — accepted per PR #377 notes) |
| `strategy_worker_ladder_incomplete_halting` | Venue only partially filled the close — venue is NOT flat | **Do not replay.** Report back; the remainder must be flattened first |
| None of the above | Unknown variant | **Do not replay.** Export the journal window (`--output=json`) and report back |

For the no-orders-row variant, the exit signal the worker minted before the
crash (it is written before the venue is touched, so a venue fill proves it
exists):

```
psq "SELECT id, emitted_at_utc, market, direction, signal_type, status, target_contracts \
     FROM signals ORDER BY emitted_at_utc DESC LIMIT 3;"
```

Expect a `direction='flat' / signal_type='exit'` row stamped at the crash
night's 00:05 UTC — its `id` is the `--signal-id` for Step 2.

Also record the real fill economics off the Coinbase web UI fills screen (or
the `order_placed` audit payload's `stages` array): total contracts, average
price, total fees, fill timestamp.

## Step 2 — Repair the book via replay (dry-run first)

> **Deploy prerequisite for the `--create-order-row` mode:** the mode landed
> 2026-07-16 — the running api image must include it (`scripts/` ships inside
> the image). If the merge is newer than the last deploy:
> `cd /opt/trading && git pull --ff-only origin main && bash deploy/day5-bringup.sh --rebuild`
> (no migration; standard timing window applies).

The tool re-feeds the aggregated close fill through the SAME pipeline the
worker uses (`process_fill_event`) — full audit record, no hand SQL. Dry-run
prints what it would do:

```
cd /opt/trading
docker compose --env-file deploy/.env exec -T api python -m scripts.operator_tools.replay_leg_fill \
  --client-order-id <CLOSE_LEG_CID> \
  --fill-quantity <CONTRACTS> \
  --fill-price "<AVG_PRICE>" \
  --commission-usd "<TOTAL_FEES>" \
  --filled-at-utc "<REAL_FILL_TS_ISO>" \
  --env paper
```

For the **no-orders-row variant** (2026-07-16), add the create mode — the
tool first records the missing orders row through the same production path
the worker uses (audit-first ORDER_PLACED carrying an explicit `repair`
marker), then replays:

```
  ... same flags as above ... \
  --create-order-row \
  --signal-id <EXIT_SIGNAL_UUID> \
  --order-direction buy
```

(`--order-direction buy` closes a short; `sell` closes a long. The tool
refuses unless the direction and `--fill-quantity` exactly close the live
`positions_current` row.)

Review the printed order row + payload, then execute by appending
`--no-dry-run --confirm` (add `--force` ONLY for the fallback shape above).
Expected on success: `ORDER_FILLED → POSITION_CLOSED → BALANCE_SNAPSHOT_RECORDED
→ TRADE_CLOSED` audit events; the `positions_current` row deleted; the trade
`closed` with realized PnL. Known accepted artifact: the replay's balance
snapshot transiently double-counts PnL until the next EOD recon re-anchors cash
from the venue (documented `from_fill` convention, PR #377).

## Step 3 — Verify

```
psq "SELECT market, quantity FROM positions_current;"
psq "SELECT id, state, realized_pnl_usd, closed_at_utc FROM trades ORDER BY created_at DESC LIMIT 3;"
```

Expect: no stale row (flat book), trade `closed` with a real
`realized_pnl_usd`. Also eyeball the Coinbase UI **open orders** for the
product — a leftover resting stop on a flat book is a naked order (night-three
lesson); cancel it in the UI if present. Optionally run the audit-chain
verification ceremony (`/verify-chain`).

## Step 4 — Resume + adjudicate

Resume via the dashboard kill-switch panel (`POST /api/system/kill-switch/resume`
behind a fresh Touch ID UV), and record the adjudication with the defect
reference from Step 1. State lands `CONVALESCENT`; three clean UTC days
graduate it back to `NORMAL` automatically (recon-cycle tick).

## What happens automatically afterward

* The open `reconciliation_breaks` row resolves itself on the next 00:15 UTC
  cycle (`resolution_path='auto_rereconciled'`) once backend == venue.
* No re-entry compounding risk while halted (the decision dispatch gate fails
  closed), but repair before the next 00:05 UTC decision keeps the nightly
  digest clean.

## Escalate back to a Claude session when

* Step 0 shows any shape other than "backend stale / venue flat".
* Step 1 shows a failure signature not in the table (new defect class — it
  needs a root-cause fix PR, like #377/#383/#386 before it).
* The same class strands a THIRD time: that is the trigger to design the
  automated stranded-close re-feed (an [A02] `services/signal/**` +
  `services/risk/**` PR — worker detects "orders filled + venue flat +
  positions_current stale" on the risk tick and replays through the same
  pipeline without operator involvement). Deliberately NOT built unilaterally:
  auto-mutating the book from venue truth is a risk-design decision the
  operator must approve.
