# Strategy worker — operator runbook (crypto-pivot §3.3, C1)

[A27] runbook for the `strategy_worker` compose service — the container
that runs the 00:05 UTC daily decision and the 30 s risk loop, and
**places real orders autonomously** (announce-only mandate; there is no
per-trade approval anywhere). Code: `services/signal/strategy_worker.py`
(+ `worker_main.py` / `worker_entrypoint.py`).

If anything fails, capture the exact error + step number and stop.
Root-cause discipline per dev-guide §1.3.

## What the worker does

| Loop | Cadence | Output |
|---|---|---|
| Daily decision | 00:05 UTC (dedupe by UTC date; late start still runs once) | `strategy_decisions` row + signals/orders/fills rows + orders on the venue |
| Risk loop | every 30 s | heartbeat (`strategy_worker_status` row + `/tmp/strategy-worker.heartbeat`), 2xATR client-stop checks, native-stop fill detection, daily/weekly loss limits, $1,500 hard-halt floor, liquidation-buffer check, §7 outage policy |

Phase-A small-live clamps (strategy §10) default ON:
`E_effective = min(equity, $1,500)`, max 2 BTC / 4 ETH contracts.
Scale-up = `deploy/.env` change (`STRATEGY_WORKER_E_EFFECTIVE_CAP_USD=none`,
raised `STRATEGY_WORKER_MAX_*_CONTRACTS`) — never a code edit.

## Prerequisites (fail-closed if missing)

1. **Secrets file** (`/opt/trading-secrets/secrets.yaml`):
   `postgres.app_service_password` + `coinbase.api_key_name` +
   `coinbase.api_private_key` (CDP key: ECDSA, View+Trade, NO Transfer).
   Missing/placeholder → container exits 2 at boot (by design).
2. **Bootstrap rows**: `python -m scripts.operator_tools.bootstrap_live_account
   --env paper --mint-from-defaults --no-dry-run` must have run (accounts +
   risk_state + Amendment B `parameter_sets` head). Missing → the worker
   refuses to start (`no active accounts row`) or skips decisions
   (`strategy_worker_no_parameter_head`).
3. **Slippage-calibration head row**: signals rows FK into
   `slippage_calibration_versions`. On a fresh DB no head exists and the
   worker logs `strategy_worker_no_slippage_head_fail_closed` and will
   NOT dispatch. Seed the bootstrap calibration first (operator ceremony;
   the calibration module's BOOTSTRAP trigger — see
   `services/calibration/calibration.py`). **This is a deliberate
   fail-closed gate, not a bug.**
4. Migrations at head (`deploy/day5-bringup.sh` Step 4 /
   `alembic upgrade head`) — this PR adds `strategy_decisions` +
   `strategy_worker_status`.

## Smoke steps (VPS, after deploying a build containing this worker)

1. **Boot.** `docker compose --env-file deploy/.env logs strategy_worker | grep strategy_worker_started`
   → shows `e_effective_cap_usd=1500` and `max_contracts={'BTC': 2, 'ETH': 4}`.
2. **Startup reconcile.** `... | grep strategy_worker_startup_recovery_completed`
   → positions map (all zeros on a fresh account), `unprotected=[]`.
3. **Marks feed.** `... | grep marks_feed_connected` → subscribed
   `BTC-USD`/`ETH-USD` + discovered `*-CDE` perp ids (never hardcoded).
4. **Heartbeat.** After ~1 min:
   `docker compose --env-file deploy/.env exec postgres psql -U app_service -d trading -c "SELECT risk_loop_heartbeat_utc, risk_loop_tick_count, marks_stale FROM strategy_worker_status;"`
   → heartbeat within the last minute, tick_count increasing. The
   container healthcheck itself watches `/tmp/strategy-worker.heartbeat`
   (stale > 5 min ⇒ restart).
5. **First decision.** After the next 00:05 UTC:
   `... psql ... -c "SELECT decision_date, status, equity_usd, outcome->'assets' FROM strategy_decisions ORDER BY decision_date DESC LIMIT 1;"`
   → one row, status `completed` (or a `skipped_*` status with a
   readable `skip_reason`). Per-asset score → target → action → costs
   live in `outcome`.
6. **Halt gate drill.** `/halt` from Discord (or the web kill switch),
   wait for the next 00:05 UTC (or restart the worker and watch the
   decision path): decision row lands as `skipped_risk_state`, zero
   orders on the venue (`list_orders` empty of new entries). Resume via
   the web `/system` resume flow.
7. **Restart idempotency (gate A3 rehearsal).** `docker compose restart
   strategy_worker` mid-day: logs show `strategy_worker_decision_already_done`
   (no double-run) and `coinbase_startup_reconcile`; no duplicate orders
   on the venue (deterministic client_order_ids).
8. **Stop protection (gate A2).** With any open position:
   `... psql ... -c "SELECT market, status, order_type, stop_price FROM orders WHERE order_type='stop_market' ORDER BY placed_at_utc DESC LIMIT 2;"`
   → a venue-resting stop-limit row per position; the api-side
   `list_orders` canary drill in `deploy/coinbase_execution/README.md`
   verifies the venue side.

## Config knobs (deploy/.env, all optional)

- `STRATEGY_WORKER_LOG_LEVEL` — default INFO.
- `STRATEGY_WORKER_E_EFFECTIVE_CAP_USD` — default `1500` (Phase A);
  `none` disables the cap (C2).
- `STRATEGY_WORKER_MAX_BTC_CONTRACTS` / `STRATEGY_WORKER_MAX_ETH_CONTRACTS`
  — default 2 / 4 (Phase A).
- `STRATEGY_WORKER_RISK_TICK_SECONDS` — locked at 30 by strategy §5;
  changing it is a strategy amendment, not tuning.

## Known deferrals (operator awareness)

- **Quarterly-maintenance de-risking (§7: gross ≤ 1.0x before the CDE
  3-hour maintenance window) is NOT implemented yet.** It needs the
  venue's maintenance calendar (strategy §11 open question 9); it lands
  as a C1 follow-up once the calendar source is confirmed. Until then,
  de-risk manually before a published window (`/halt` + resume after),
  or accept the exposure at the ≤2/≤4 nano-contract Phase-A book.
- **Friday-close no-entry rule (§7: no entries 60 min pre-halt)** is
  satisfied structurally: the only entry path is the 00:05 UTC daily
  decision, ~21 h from the Friday 21:00-22:00 UTC close; the risk loop
  places exits only. No code enforces it separately — revisit if an
  intraday entry path is ever added.
- **Capital-event vol multiplier** is unevaluated until the §3.5 recon
  PR wires the UTC-day session counter (worker logs when an active
  window is skipped).

## Alerting (risk-review F1a)

Every worker-driven protective action lands an `alerts` row (existing
categories only; specifics ride the `detail` JSONB):

| Event | Category | Severity |
|---|---|---|
| Daily-loss halt / floor halt / repeated-loop-failure halt / venue-failed ladder halt | `kill_switch_invoked` | P1 (floor: P0) |
| Client 2xATR stop flatten | `margin_auto_trim` | P2 |
| §7 outage flatten (no resting stop) | `position_unprotected` | P1 |
| Liquidation-buffer force-reduce | `margin_warn` | P1 |

Alert writes are after-action and never block the protective action; a
failed insert logs `strategy_worker_alert_insert_failed`.

## Failure modes worth knowing

- **`strategy_worker_no_slippage_head_fail_closed`** — prerequisite 3.
- **`strategy_worker_decision_bars_not_ready`** — venue candles lagging;
  the worker retries every 30 s until 01:00 UTC, then records a
  terminal `skipped_stale_bars` decision (never trades a stale bar).
- **`strategy_worker_native_stop_unverified`** — gate A2 verification
  failed; re-arm retries every tick; if marks also go stale the §7
  outage policy flattens the unprotected position.
- **`strategy_worker_kill_switch_invoked`** — the FSM was driven
  (daily loss → `daily_loss_breach`; equity ≤ $1,500 →
  `decommission_floor`; repeated loop failures / venue-failed market
  order → `unhandled_exception`). Positions are flattened first, then
  the transition is written. Restart requires the human resume flow
  (HALT_NEW → CONVALESCENT) — including after a daily-loss breach
  (stricter than the backtest's automatic 24 h pause; see the PR's
  escalation note).
