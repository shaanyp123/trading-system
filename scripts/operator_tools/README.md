# Operator recovery tooling

Committed operator tools for production-state recovery. Distinct from
`scripts/` (laptop-side ceremony scripts) — anything here runs inside
the api container against production state via the canonical
service-side write paths (never raw SQL).

## Contents

| Script | Purpose | Mutates state? |
|---|---|---|
| `replay_executions.py` | Pull executions back from IBKR and feed them through `process_fill_event` to recover from backend-blind fills | Yes — writes audit chain + fills / positions_current / balances / trades |
| `recovery_agent.py` | Poll `alerts` for `worker_failure` events and decide whether to invoke `replay_executions.py` (transient) or alert-only (hard crash) | Yes — writes `RECOVERY_ACTION_TAKEN` audit + UPDATEs alerts row + may invoke replay subprocess |
| `trigger_v1_cycle.py` | On-demand V1 strategy cycle trigger (mirrors what LEAN does at 21:30 UTC); reads bars from disk + POSTs to `/api/internal/lean/signals` | Yes (when `--no-dry-run`) — POSTs signal_emitted events which become audit rows + signals INSERTs via the api endpoint. **--dry-run default = ON.** |
| `replace_protective_stop.py` | POSITION_UNPROTECTED recovery: places a fresh bracket-stop for a position whose protective stop was cancelled but never replaced (PR-C exit-pipeline failure mode) | Yes (when `--no-dry-run --confirm`) — writes ORDER_PLACED audit + INSERTs orders row + places stop_market at IBKR. **--dry-run default = ON; two-flag gate.** |
| `master_client_id_probe.py` | Empirical validation of the IBKR Master Client ID configuration (`TWS_MASTER_CLIENT_ID`). Three sequential stages on distinct clientIds: place a safe stop at `$1` on /MES from clientId=86, cancel via the master clientId, `reqGlobalCancel` cleanup from clientId=87. | Yes — places a single safe (stop=$1, DAY TIF, /MES) order at IBKR. Cancels itself end-to-end on success. No DB writes; no audit chain. **Operator-coordinated; runs post `docker compose up -d --force-recreate ib_gateway`.** |
| `bootstrap_live_account.py` | Idempotent DB bootstrap: `accounts` + `risk_state` (live cutover) and — via `--mint-from-defaults` — seeds the baseline `parameter_sets` head row from the canonical V1 defaults, minting the `parameter_set_hash` (PR #294). **For the paper seed (already-bootstrapped env) ALWAYS add `--seed-params-only`** to skip the account/risk_state inserts (without it the full bootstrap creates a duplicate account because paper's account id is `operator`, not the IBKR number — the 2026-05-30 incident; full path now refuses on mismatch). See the **parameter-set seeding** + **decommission ceremony** sections below. | Yes (when `--no-dry-run --confirm`) — parameterized INSERTs via `ON CONFLICT DO NOTHING`. **--dry-run default = ON; two-flag gate; idempotent re-runs are no-ops.** |
| `apply_parameter_change.py` | Operator-driven **audited** change to an operator-only flag (`STRATEGY_DECOMMISSIONED` / `EXIT_AUTO_APPROVE`): audit-first `parameter_change_applied`/`_reverted` event → `parameters` history row (`prev == hash`, hash-stable) → in-place `parameter_sets` flip (PR-D, design §13). Replaces the raw decommission UPDATE. | Yes (when `--no-dry-run --confirm`) — writes the audit chain + `parameters` + `parameter_sets`. **--dry-run default = ON; two-flag gate; no-op when already at value.** |
| `cancel_orphan_order.py` | Cancel a stuck/orphan `orders` row left at `status='pending'` with `broker_order_id IS NULL` — pre-inserted but never placed at IBKR (e.g. the 2026-06-04 /MYM Error-200 failure; exchange bug fixed in #327). Reuses the audit-first `process_terminal_status_event`. **Refuses** if `broker_order_id` is set (may be live at IBKR — use an IBKR cancel instead). | Yes (when `--execute`) — writes `ORDER_CANCELLED`/`ORDER_REJECTED` audit + UPDATEs the orders row via the tested terminal-status processor (no hand-rolled SQL). **--dry-run default = ON.** |

---

## `replay_executions.py` — backend-blind fills recovery

### When to use

**Canonical scenario:** IBKR has fills, but the backend missed the
`orderStatus` events. The audit chain stays clean (no `ORDER_FILLED`
rows for the orders in question), but `fills` / `positions_current` /
`balances` / `trades` rows are NEVER written for the affected orders.

This usually happens after an IBKR Error 1100 connection loss between
`ib_gateway` and IBKR — the worker's `subscribe_order_status` callback
chain stops receiving updates, IBKR fills the order anyway, and when
the gateway reconnects IBKR does NOT re-fire historical `orderStatus`
events. The events are gone.

**Concrete detection:**

1. `api` logs show one or more `ibkr_error_1100` entries (or any of the
   downstream silent-worker fingerprints from PRs #168 / #169).
2. `psql -c "SELECT COUNT(*) FROM fills WHERE created_at::date = CURRENT_DATE"`
   returns 0 (or fewer than expected) for today's drill / live signals
   that you have evidence filled at IBKR.
3. TWS Desktop or the IBKR portal shows the fills with timestamps from
   today, but the backend's `signals` / `orders` rows are still
   `status='pending'` or `status='partially_filled'` with no
   downstream propagation.

### When NOT to use

- **The fills haven't happened yet.** If TWS shows the order still
  working at IBKR, there is nothing to replay. Wait or cancel.
- **CIDs are from a different process.** If the `orderRef` in IBKR
  doesn't match the `orders.client_order_id` you're trying to recover,
  the script won't find a match (exit code 1). Verify CIDs first via
  `SELECT id, client_order_id, broker_order_id FROM orders WHERE signal_id = '...'`.
- **The worker is currently picking up the fills.** If `api` logs show
  `order_placement_fill_propagated` events for the same CIDs, the
  worker is alive and the dedupe set will let the script run safely
  (the SQL-layer `UNIQUE(broker_fill_id, created_at)` constraint
  catches dupes), but it's strictly unnecessary. Wait for the worker
  to drain.
- **env is `live-small` or `live-scale` and you don't have authorization.**
  The script requires `--allow-non-paper` for any non-paper env. This
  is a deliberate fail-closed default because the script mutates audit
  chain rows and live-env recovery should always be operator-approved.

### Pre-flight checks

```bash
ssh root@178.156.239.84
cd /opt/trading

# 1. api container healthy
docker compose --env-file deploy/.env exec -T api \
  /opt/venv/bin/python -c "print('api importable')" || \
  { echo "FAIL: api container not healthy"; exit 1; }

# 2. ib_gateway reachable on the docker network (paper port 4004)
docker compose --env-file deploy/.env exec -T api \
  /opt/venv/bin/python -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(3)
result = s.connect_ex(('ib_gateway', 4004))
s.close()
print('ib_gateway port 4004 reachable:', result == 0)
" || \
  { echo "FAIL: ib_gateway not reachable"; exit 1; }

# 3. sops decrypts cleanly
export SOPS_AGE_KEY_FILE=/etc/credstore.encrypted/age_key
sops --decrypt secrets/paper.enc.yaml > /dev/null || \
  { echo "FAIL: sops can't decrypt secrets/paper.enc.yaml"; exit 1; }
```

If any check fails, stop and resolve before continuing. Root-cause
discipline per `Docs/claude-dev-guide.md` §1.3.

### Run command (paper env)

```bash
# Replace --client-order-ids with your actual CIDs (comma-separated).
ssh root@178.156.239.84 -- 'set -euo pipefail
  cd /opt/trading
  (
    export SOPS_AGE_KEY_FILE=/etc/credstore.encrypted/age_key
    PG_PASS=$(sops -d secrets/paper.enc.yaml | yq -r .postgres.app_service_password)
    docker compose --env-file deploy/.env exec -T \
      -w /app \
      -e PYTHONPATH=/app \
      -e DATABASE_URL="postgresql+asyncpg://app_service:${PG_PASS}@postgres:5432/trading" \
      api \
      /opt/venv/bin/python -m scripts.operator_tools.replay_executions \
        --client-order-ids c951158b-aaaa-...,c951158c-bbbb-... \
        --env paper
  )
'
```

Notes:

- `DATABASE_URL` is set on the docker-exec command line via `-e` so it
  is scoped to the python invocation only — it does NOT persist in the
  api container's environment after the script exits.
- The script uses `DATABASE_URL` (bare, not `API_DATABASE_URL`) so the
  variable name matches the operator-runbook convention from
  `deploy/audit/README.md`.
- `PYTHONPATH=/app` ensures the `services.*` imports resolve inside
  the api container's working dir.
- The subshell parens isolate `PG_PASS` and `SOPS_AGE_KEY_FILE` from
  the parent shell.

### Run command (live env — requires authorization)

```bash
# Same command + --allow-non-paper. Operator must have explicit
# authorization to mutate live audit chain rows.
ssh root@178.156.239.84 -- 'set -euo pipefail
  cd /opt/trading
  (
    export SOPS_AGE_KEY_FILE=/etc/credstore.encrypted/age_key
    PG_PASS=$(sops -d secrets/live-small.enc.yaml | yq -r .postgres.app_service_password)
    docker compose --env-file deploy/.env exec -T \
      -w /app \
      -e PYTHONPATH=/app \
      -e DATABASE_URL="postgresql+asyncpg://app_service:${PG_PASS}@postgres:5432/trading" \
      api \
      /opt/venv/bin/python -m scripts.operator_tools.replay_executions \
        --client-order-ids ... \
        --env live-small \
        --allow-non-paper
  )
'
```

### Dry-run mode

Always recommended before the wet run. Same command, add `--dry-run`:

```bash
docker compose ... \
  /opt/venv/bin/python -m scripts.operator_tools.replay_executions \
    --client-order-ids ... \
    --env paper \
    --dry-run
```

Dry-run connects to IBKR, fetches executions, aggregates per-CID, and
logs the would-be `FillIngestPayload` values to stdout — but does NOT
call `process_fill_event`. Useful for:

- Verifying the CIDs match before committing
- Confirming the VWAP / commission / timestamp roll-up looks right
- Sanity-checking the IBKR connection works on the chosen clientId

If dry-run looks correct, re-run without `--dry-run` to commit.

### Exit codes

| Code | Meaning | Operator action |
|---|---|---|
| 0 | Success — every CID matched + replayed cleanly | Verify per next section, optionally Discord-POST |
| 1 | At least one CID had no matching execution | Investigate: wrong CID? wrong date? fill actually didn't happen? |
| 2 | At least one CID is a partial fill or reversal | Manual reconciliation; PR-G doesn't handle these (Phase 2+) |
| 3 | At least one CID raised `FillProcessingError` | Escalate; check api logs for the specific `error_code` |
| 4 | IBKR connection failed | Check `ib_gateway` health; verify clientId isn't wedged |
| 5 | DB pool init failed | Verify `DATABASE_URL` is set + the password is correct |
| 6 | Invalid CLI args | Check the `--env=live-*` requires `--allow-non-paper` gate |
| 99 | Unexpected exception (traceback on stderr) | Escalate; capture stderr + the input CIDs |

### Verification (post-success)

```bash
# 1. Audit chain extends + still passes verification.
docker compose --env-file deploy/.env exec -T \
  -e DATABASE_URL="$DATABASE_URL" \
  api \
  /opt/venv/bin/python -m services.audit.verify_chain --env paper
# Expected: CHAIN OK: <N> rows verified  (N is now larger than before)

# 2. fills table has new rows.
docker compose --env-file deploy/.env exec -T \
  -e PGPASSWORD="$PG_PASS" \
  postgres \
  psql -U app_service -d trading -h postgres -c "
SELECT id, order_id, broker_fill_id, fill_price, fill_quantity, commission_usd, filled_at_utc
FROM fills
WHERE broker_fill_id LIKE 'replay:%'
ORDER BY created_at DESC
LIMIT 5;
"

# 3. trades table reflects the lifecycle (open → closed) where applicable.
docker compose --env-file deploy/.env exec -T \
  -e PGPASSWORD="$PG_PASS" \
  postgres \
  psql -U app_service -d trading -h postgres -c "
SELECT id, state, total_quantity, avg_entry_price, avg_exit_price, realized_pnl_usd, closed_at_utc
FROM trades
WHERE entry_signal_id IN (SELECT signal_id FROM orders WHERE client_order_id IN ('...'))
ORDER BY created_at DESC;
"
```

If the audit chain still verifies + the fills / trades rows look
consistent with what IBKR shows, the recovery is complete.

### Discord #fills manual POST (operator-driven, optional)

The script does NOT emit SSE — the multiplexer is intra-process to the
api container and this script is a separate process. Downstream
subscribers (`#fills` Discord channel, `/signals` page) are NOT
notified automatically.

If you want to backfill the Discord notification for visibility, POST
to the `#fills` webhook directly. Sample embed JSON (from the 2026-05-18
drill 5 recovery — adapt the values to your CIDs):

```bash
WEBHOOK_URL=$(sops -d secrets/paper.enc.yaml | yq -r .discord.fills_webhook_url)
curl -sS -X POST -H 'Content-Type: application/json' "$WEBHOOK_URL" -d '{
  "embeds": [{
    "title": "Fill replayed (backend recovery)",
    "description": "IBKR fills replayed via scripts/operator_tools/replay_executions.py after backend-blind incident.",
    "color": 16753920,
    "fields": [
      {"name": "client_order_id", "value": "c951158b-...", "inline": false},
      {"name": "fill_price", "value": "85.62", "inline": true},
      {"name": "fill_quantity", "value": "1", "inline": true},
      {"name": "commission_usd", "value": "1.00", "inline": true},
      {"name": "audit_event_uuid", "value": "<uuid from script stdout>", "inline": false}
    ],
    "footer": {"text": "Replay tool — backend was disconnected at fill time"}
  }]
}'
```

`color: 16753920` is `#ffa500` orange — distinct from the normal
green-fill color so the operator can spot the backfilled events at a
glance.

### Cleanup

No state to clean. The script:

- Reads from IBKR (no write to IBKR).
- Writes to audit_log + fills + positions_current + balances + trades
  via the canonical `process_fill_event` path. These rows are durable
  by design — they're now part of the audit chain.

If you needed to abort mid-run (Ctrl+C or the docker exec exited), some
CIDs may have been replayed and others not. Re-run with the unprocessed
CIDs only; the `UNIQUE(broker_fill_id, created_at)` constraint on the
`fills` table catches dupes if you accidentally include an already-
replayed CID.

### Architecture note

The script lives in `scripts/operator_tools/` (NEW path) which is on the
dev-guide hot-fix scope but NOT on the §11 anti-pattern [A02]
forbidden-modification whitelist (services/risk + services/audit +
services/execution etc.). The script CALLS those modules but does not
modify them — regular PR review applies.

The audit + table writes go through
`services.risk.fill_processor.process_fill_event` which is the same
entry point the worker's `_on_order_status` callback uses in
production. There is no parallel code path; the script is literally
"what the worker would have done if it had been listening."

### Lineage

This is the committable, parameterized version of the transient
`/tmp/drill5_recovery.py` script that closed the 2026-05-18 drill 5
backend-blind-fills incident. See `Docs/decisions-log.md` 2026-05-18
entry for the full retrospective.

---

## `trigger_v1_cycle.py` — on-demand V1 strategy cycle trigger

### When to use

**Canonical scenarios:**

- Test a strategy logic change without waiting for tomorrow's 21:30 UTC cycle.
- Replay a missed cycle (e.g., `lean_local` was down at 21:30 UTC).
- Manual signal probe for forensic / debugging purposes.
- Diagnostic question: "what would the strategy say RIGHT NOW with the current data on disk?"

**Concrete origin:** PR #247 (2026-05-26) fixed a Donchian inclusive-window bug that suppressed signal firing for 7 days. The fix is deployed; tomorrow's natural cycle will fire — but this tool is the future answer when an operator wants to fire a cycle off-schedule.

### What it does

For a given `--session-date` (default = today ET):

1. Verifies `risk_state.state == 'NORMAL'`. Aborts on `HALT_NEW` or `CONVALESCENT` (this tool is **not** a backdoor around the kill-switch).
2. Loads `V1Parameters` from the `parameter_sets` head pointer.
3. Loads current positions from `positions_current` (joined with open `trades` rows for the `opened_at_session_date` field).
4. For each market in `V1_CANDIDATE_UNIVERSE \ V1_SIDELINED_MARKETS`:
   - Reads bars from the shared `lean_data` Docker volume (`bar_sync` writes here daily at 17:00 ET on `clientId=3`).
   - For futures: picks the latest `<lower>_trade_<YYYYMM>.csv` member in the zip (= today's front-month per bar_sync).
   - For ETFs: reads `<lower>.csv` and decodes the deci-cent integer scaling back to `Decimal`.
5. Calls `V1TrendFollowing.generate_signals(...)` against the universe + position snapshot.
6. For each emitted signal: dedups against the `signals` table by `(account_id, env, market, session_date)`; if a row already exists today, logs `trigger_v1_cycle_dedup_skip` and skips. Otherwise POSTs to `/api/internal/lean/signals` (or logs the would-POST payload in `--dry-run`).
7. Logs each rejection (`market`, `reason`) for forensic visibility.
8. Emits a summary line: `trigger_v1_cycle_completed session_date=YYYY-MM-DD signals_emitted=N rejections=M dedup_skipped=K reasons={...}`.

### When NOT to use

- **Risk state is not NORMAL.** The tool fails closed; the operator's recourse is to resume from `HALT_NEW` via the web UI.
- **Today's natural 21:30 UTC LEAN cycle already ran AND emitted what you expected.** Dedup will skip everything; the tool's effective output is the rejection log + summary, no new signals.
- **Bar staleness > 5 days.** The tool logs a `trigger_v1_cycle_bars_stale` warning per market but still continues — operator may want to trigger against last-known data while bar_sync recovers. If you see this warning unexpectedly, investigate bar_sync first.
- **env is `live-small` or `live-scale` and you don't have authorization.** The tool requires `--allow-non-paper` for any non-paper env (fail-closed default for tooling that touches production signal flow).

### Pre-flight checks

```bash
ssh root@178.156.239.84
cd /opt/trading

# 1. api container healthy
docker compose --env-file deploy/.env exec -T api \
  /opt/venv/bin/python -c "print('api importable')" || \
  { echo "FAIL: api container not healthy"; exit 1; }

# 2. lean_data volume mount visible from api container
docker compose --env-file deploy/.env exec -T api \
  ls /Lean/Data/equity/usa/daily/ 2>&1 | head -5 || \
  { echo "FAIL: /Lean/Data not mounted in api"; exit 1; }

# 3. sops decrypts cleanly
export SOPS_AGE_KEY_FILE=/etc/credstore.encrypted/age_key
sops --decrypt secrets/paper.enc.yaml > /dev/null || \
  { echo "FAIL: sops can't decrypt secrets/paper.enc.yaml"; exit 1; }
```

If any check fails, stop and resolve before continuing. Root-cause discipline per `Docs/claude-dev-guide.md` §1.3.

### Run command (paper env, dry-run — REQUIRED FIRST)

The tool defaults to `--dry-run=True`; you have to pass `--no-dry-run` to actually POST. **Always do a dry-run first** to confirm the strategy + dedup output is what you expect.

```bash
ssh root@178.156.239.84 -- 'set -euo pipefail
  cd /opt/trading
  (
    export SOPS_AGE_KEY_FILE=/etc/credstore.encrypted/age_key
    PG_PASS=$(sops -d secrets/paper.enc.yaml | yq -r .postgres.app_service_password)
    docker compose --env-file deploy/.env exec -T \
      -w /app \
      -e PYTHONPATH=/app \
      -e DATABASE_URL="postgresql+asyncpg://app_service:${PG_PASS}@postgres:5432/trading" \
      api \
      /opt/venv/bin/python -m scripts.operator_tools.trigger_v1_cycle \
        --env paper
  )
'
```

Notes:

- `DATABASE_URL` is scoped to the docker-exec invocation only (subshell env), per the same pattern as `replay_executions.py`. Never displayed; never persisted in the api container's environment.
- `LEAN_LOCAL_BEARER_TOKEN` is NOT required for dry-run (the bearer check fires only when `--no-dry-run` is set).
- The tool defaults `--session-date` to today's ET calendar date.

Inspect the structured log output for:
- `trigger_v1_cycle_context_loaded` — confirms account, risk_state, active universe, dedup set.
- `trigger_v1_cycle_dry_run_would_post` — one per signal the strategy emitted (or zero if rejections dominate).
- `trigger_v1_cycle_rejection` — one per rejected market with the reason.
- `trigger_v1_cycle_completed` — summary line.

### Run command (paper env, wet — committing)

Once the dry-run looks correct, re-run with `--no-dry-run` and the bearer staged.

```bash
ssh root@178.156.239.84 -- 'set -euo pipefail
  cd /opt/trading
  (
    export SOPS_AGE_KEY_FILE=/etc/credstore.encrypted/age_key
    PG_PASS=$(sops -d secrets/paper.enc.yaml | yq -r .postgres.app_service_password)
    LEAN_BEARER=$(sops -d secrets/paper.enc.yaml | yq -r .lean.api_bearer_token)
    docker compose --env-file deploy/.env exec -T \
      -w /app \
      -e PYTHONPATH=/app \
      -e DATABASE_URL="postgresql+asyncpg://app_service:${PG_PASS}@postgres:5432/trading" \
      -e LEAN_LOCAL_BEARER_TOKEN="${LEAN_BEARER}" \
      api \
      /opt/venv/bin/python -m scripts.operator_tools.trigger_v1_cycle \
        --env paper \
        --no-dry-run
  )
'
```

Notes:

- Both `PG_PASS` and `LEAN_BEARER` are subshell-scoped and never echoed. Per `feedback_secret_handling.md`: do NOT `cat` the sops output; do NOT `echo $LEAN_BEARER`; do NOT print the decrypted YAML to stdout. The `yq -r` extraction is the only consumer.
- The api endpoint (`/api/internal/lean/signals`) writes the audit row + INSERTs the `signals` row via the canonical `ingest_signal_emitted` pipeline — same path LEAN's natural cycle uses. Audit-first ordering is the api's responsibility.

### Run command (live env — requires authorization)

```bash
# Same as above, swap `paper` → `live-small` and add --allow-non-paper.
# Operator must have explicit authorization to drive production signal flow.
ssh root@178.156.239.84 -- 'set -euo pipefail
  cd /opt/trading
  (
    export SOPS_AGE_KEY_FILE=/etc/credstore.encrypted/age_key
    PG_PASS=$(sops -d secrets/live-small.enc.yaml | yq -r .postgres.app_service_password)
    LEAN_BEARER=$(sops -d secrets/live-small.enc.yaml | yq -r .lean.api_bearer_token)
    docker compose --env-file deploy/.env exec -T \
      -w /app \
      -e PYTHONPATH=/app \
      -e DATABASE_URL="postgresql+asyncpg://app_service:${PG_PASS}@postgres:5432/trading" \
      -e LEAN_LOCAL_BEARER_TOKEN="${LEAN_BEARER}" \
      api \
      /opt/venv/bin/python -m scripts.operator_tools.trigger_v1_cycle \
        --env live-small \
        --no-dry-run \
        --allow-non-paper
  )
'
```

### Replay a missed cycle

If LEAN was down on a past day, pass `--session-date YYYY-MM-DD`. The tool will:

- Use that date as the `as_of_session_date` for the strategy.
- Compute the dedup set against `signals` rows for that specific session_date.
- Use the bars currently on disk (which represent bar_sync's most-recent successful sync; you cannot replay against a historical disk state without restoring it first).

```bash
# Example: replay 2026-05-24's cycle (a Sunday is fine — strategy uses bars only)
docker compose ... \
  /opt/venv/bin/python -m scripts.operator_tools.trigger_v1_cycle \
    --env paper \
    --session-date 2026-05-24
```

### Exit codes

| Code | Meaning | Operator action |
|---|---|---|
| 0 | Success — cycle ran cleanly; all eligible signals POSTed (or marked would-POST in `--dry-run`) | Verify per next section; optionally Discord-POST per the `replay_executions.py` template |
| 1 | Risk state blocked dispatch (not NORMAL) | Investigate why kill-switch is engaged; resume manually via `/system` if appropriate |
| 3 | At least one signal POST returned non-2xx | Check the per-signal `trigger_v1_cycle_post_rejected` log lines; the audit_log will show the ones that succeeded |
| 5 | DB init failure | Verify `DATABASE_URL` env var + Postgres connectivity |
| 6 | Invalid CLI args | Check the `--env=live-*` requires `--allow-non-paper` gate + `--session-date` format |
| 7 | `LEAN_LOCAL_BEARER_TOKEN` env var unset (and not `--dry-run`) | Stage via sops per the run command above |
| 99 | Unexpected exception (traceback on stderr) | Escalate; capture stderr + the input args |

### Verification (post-success)

```bash
# 1. Audit chain extends + still passes verification.
docker compose --env-file deploy/.env exec -T \
  -e DATABASE_URL="$DATABASE_URL" \
  api \
  /opt/venv/bin/python -m services.audit.verify_chain --env paper
# Expected: CHAIN OK: <N> rows verified  (N now larger by however many signals POSTed)

# 2. signals table has new rows tagged with the operator-trigger strategy_version.
docker compose --env-file deploy/.env exec -T \
  -e PGPASSWORD="$PG_PASS" \
  postgres \
  psql -U app_service -d trading -h postgres -c "
SELECT id, market, direction, status, session_date, emitted_at_utc
FROM signals
WHERE emitted_at_utc > NOW() - INTERVAL '1 hour'
ORDER BY emitted_at_utc DESC
LIMIT 10;
"

# 3. audit_log shows the SIGNAL_EMITTED rows.
docker compose --env-file deploy/.env exec -T \
  -e PGPASSWORD="$PG_PASS" \
  postgres \
  psql -U app_service -d trading -h postgres -c "
SELECT sequence_no, event_type, ingest_clock_ts
FROM audit_log
WHERE event_type = 'signal_emitted'
  AND ingest_clock_ts > NOW() - INTERVAL '1 hour'
ORDER BY sequence_no DESC
LIMIT 10;
"
```

### Will this mess anything up? (operator concern)

Built-in mitigations for each failure mode:

| Risk | Mitigation |
|---|---|
| Double-emit (today's natural cycle + this tool) | Dedup against `signals.(account_id, env, market, session_date)`; markets already emitted today are skipped with `trigger_v1_cycle_dedup_skip` |
| Bar staleness | Logged as `trigger_v1_cycle_bars_stale` warning per market; tool continues (operator-trigger may want last-known data) |
| Kill-switch bypass | Hard-fail on non-NORMAL risk_state; manual resume via web UI required |
| /MCL re-emission | `V1_SIDELINED_MARKETS` honored via set difference; /MCL never appears in the active universe |
| Audit chain integrity | The api endpoint handles audit-first ordering per backend-spec §2.10.1; this tool stays out of the audit-write path |
| Live-env unauthorized run | `--allow-non-paper` gate; argparse error code 6 if missing |
| Bearer leak | Bearer staged via sops to a subshell-scoped env var; never echoed; the tool reads via `os.environ` |

### Architecture note

`scripts/operator_tools/**` is NEW path on the dev-guide hot-fix scope but NOT on the §11 anti-pattern [A02] forbidden-modification whitelist. The tool CALLS `services/api/routes/internal/lean.py` (via HTTP POST) + `strategies/v1_trend_following/strategy.py` (via direct import) but does not modify them — regular PR review applies.

The tool's strategy invocation is the same `V1TrendFollowing.generate_signals(...)` call that `lean/v1_strategy.py::on_daily_signal_cycle` makes at 21:30 UTC. The POST payload shape matches what LEAN emits. The only divergences are deliberate:

1. **`strategy_version`** is `v1_trend_following@operator-trigger` (LEAN uses `v1_trend_following@phase1-pivot-d`). Forensic-visibility distinction so the operator can filter audit_log + `/signals` to see which were tool-triggered.
2. **`target_contracts`** is hard-coded to 1 — same conservative single-lot allocation LEAN uses (`_naive_target_contracts`). The full Stage 0-5 server-side sizing runs when the operator approves the signal.
3. **`opened_at_session_date`** on positions is sourced from the open `trades` row (when one exists) rather than LEAN's `holding.invested_since`. When no open trade is recorded, the strategy's MIN_HOLDING_DAYS check is conservatively skipped — same behavior LEAN gets when `invested_since` is missing.

### Lineage

Born from the operator brief after PR #247 (Donchian fix) revealed a gap: the operator wanted to trigger a cycle off-schedule to test the fix without waiting overnight. Built to be reusable for any future "I want to run a cycle right now" scenario. Designed to match LEAN's emission contract bit-for-bit so the api endpoint can't distinguish operator-triggered from LEAN-triggered signals (same `ingest_signal_emitted` pipeline; same audit-chain payload shape).

---

## `replace_protective_stop.py` — POSITION_UNPROTECTED recovery

### When to use

Run this tool when a `POSITION_UNPROTECTED` P0 alert fires in Discord `#critical`. That alert is paired 1:1 with the `POSITION_UNPROTECTED` audit row emitted by `services/risk/order_placement_worker.py::apply_exit_close_placement` when the bracket-stop CANCEL step succeeded but the subsequent close-order PLACE failed. The position is now NAKED (no protective stop) until either:

- The next strategy cycle re-emits the exit (which re-runs the full cancel→place pipeline and will re-cancel whatever this tool places), OR
- The market moves enough to fill the original entry's intent without further intervention (rare).

Either way, the immediate bridge is to restore a protective stop. That's what this tool does.

### When NOT to use

- The position is NOT naked (a working bracket-stop already exists). The tool's idempotency guard surfaces this with `EXIT_BRACKET_ALREADY_PROTECTED` (code 2); pass `--force` only if you intentionally want to add a second protective stop (e.g., widening the level while leaving the original in place until you manually cancel it).
- The position has been closed (positions_current row missing). Surfaces as `EXIT_NO_POSITION` (code 1); investigate whether the bracket fired or the operator flattened manually.
- You want to CHANGE the stop level on a working bracket: cancel the existing bracket in TWS first, then run this tool to place a fresh one at the new level.

### Pre-flight checks

1. Confirm the POSITION_UNPROTECTED audit row + alert row in the DB:
   ```
   docker compose --env-file deploy/.env exec -T -e PGPASSWORD="$PG_PASS" \
     postgres psql -U app_service -d trading -h postgres -c "
   SELECT sequence_no, ingest_clock_ts FROM audit_log
   WHERE event_type = 'position_unprotected'
   ORDER BY sequence_no DESC LIMIT 5;
   "
   ```
2. Confirm the position exists + note its `(account_id, market, quantity, avg_cost)`:
   ```
   docker compose ... psql ... -c "
   SELECT market, quantity, avg_cost FROM positions_current;
   "
   ```
3. Confirm no working bracket-stop already exists for the entry signal (the tool checks this too, but eyeball first):
   ```
   docker compose ... psql ... -c "
   SELECT client_order_id, stop_price, status FROM orders
   WHERE order_type = 'stop_market' AND status = 'working';
   "
   ```

### Run command (paper env)

```
docker compose --env-file deploy/.env exec -T \
  -e DATABASE_URL="$DATABASE_URL" \
  api \
  /opt/venv/bin/python -m scripts.operator_tools.replace_protective_stop \
    --market /M2K \
    --env paper \
    --no-dry-run \
    --confirm
```

Stop price defaults to the ATR-derived level from the original entry's `sizing_trace`. Override with `--stop-price 2750.20` if the original level is no longer protective (market moved past it) or if you want to widen/tighten.

### Dry-run mode (default)

Without `--no-dry-run`, the tool builds + logs the plan without touching IBKR or the DB:

```
docker compose ... -- \
  /opt/venv/bin/python -m scripts.operator_tools.replace_protective_stop \
    --market /M2K --env paper
```

Use this to preview the would-do plan: stop price, side, quantity, client_order_id, source (`sizing_trace` vs `operator_override`). Exits 0 cleanly.

### Exit codes

| Code | Meaning | Operator action |
|---|---|---|
| 0 | Success (placed OR dry-run plan printed) | None |
| 1 | No open position for this market | Investigate whether position closed out-of-band |
| 2 | Working bracket-stop already exists | Cancel it first OR pass `--force` |
| 3 | Stop-price direction sanity violated | Pass `--stop-price` strictly below avg_cost (long) or above (short) |
| 4 | IBKR connection failure | Verify ib_gateway healthy; retry |
| 5 | DB init failure | Verify `DATABASE_URL` env var staged |
| 6 | Invalid CLI args | Check args |
| 7 | `--confirm` missing for `--no-dry-run` | Pass both flags |
| 8 | Broker rejected the placement | Escalate; check margin / halted market / instrument permission |
| 99 | Unexpected exception | Capture traceback + escalate |

### Architecture note

`scripts/operator_tools/**` is NOT on the dev-guide §11 [A02] forbidden whitelist. The tool CALLS `services/audit` (writer), `services/execution` (IBKR client), and the orders/positions_current/trades tables (read-only SELECTs + a single INSERT/UPDATE on orders), but does NOT modify those modules. Regular PR review applies — no `risk-review-approved` label.

Two-flag gate (`--no-dry-run` AND `--confirm`) is DELIBERATELY stricter than `trigger_v1_cycle`'s one-flag gate because this tool bypasses the risk-state check (recovery must work during HALT_NEW) and writes to the live IBKR account.

### Lineage

Designed in `Docs/exit-pipeline-design.md` §Q5 as the operator-side bridge for the POSITION_UNPROTECTED failure mode introduced by exit-pipeline PR-C (services/risk/order_placement_worker.py's exit-close path). Shape mirrors `replay_executions.py` (one-shot IBKR tool with audit-first writes) rather than `trigger_v1_cycle.py` (HTTP POST to api).

---

## `bootstrap_live_account.py` — parameter-set seeding (paper)

> Seeds paper's **empty** `parameter_sets` head row from the canonical V1
> defaults via `--mint-from-defaults` (PR-A, #294). This is distinct from the
> tool's live-cutover role (the `accounts` + `risk_state` rows + the
> `--parameter-set-json` "copy paper's head" path); see the module docstring
> for cutover usage.

### When to use

- The `parameter_sets` table is empty (0 rows) and you need a head row so that
  (a) the `/system` risk-envelope UI shows real values instead of spec defaults,
  and (b) the **decommission ceremony** below has a row to flip.
- One-time per environment. Idempotent — safe to re-run
  (`ON CONFLICT (parameter_set_hash) DO NOTHING`).

### What `--mint-from-defaults --seed-params-only` does

> **⚠️ ALWAYS pass `--seed-params-only` for the paper seed.** Without it the
> tool ALSO runs the full `accounts` + `risk_state` bootstrap, and on paper that
> is **NOT** a no-op: the live paper account's `external_account_id` is
> **`operator`**, not the IBKR number `U25655583`, so the `ON CONFLICT
> (external_account_id)` guard does NOT match — the tool inserts a **duplicate
> account** + a **second `is_current=TRUE` risk_state** row (the 2026-05-30
> incident; cleaned up manually). `--seed-params-only` skips those inserts
> entirely. (Belt-and-suspenders: the full path now also REFUSES with
> `EXIT_ACCOUNT_MISMATCH=4` when an active owner account with a different
> `external_account_id` already exists.)

1. Builds the baseline row from `default_v1_parameters().to_canonical_dict()` —
   all **12** canonical UPPER_CASE keys, decimals-as-strings, both operator-only
   flags (`STRATEGY_DECOMMISSIONED`, `EXIT_AUTO_APPROVE`) stored as the string
   `"False"`.
2. Mints `parameter_set_hash` via
   `services/version/composite_hash.py::compute_parameter_set_hash`, which hashes
   only the **10** Parameter-Ranges-Table params — the two flags are EXCLUDED
   (backend-spec §3.11 / design Q1-A). That exclusion is what makes the later
   decommission flip PK-stable (see the ceremony).
3. INSERTs the `parameter_sets` row idempotently.
4. With `--seed-params-only`, the `accounts` + `risk_state` INSERTs are
   **SKIPPED** (you'll see `bootstrap_live_account_seed_params_only` in the log).
   The `parameter_sets` table is global/content-addressable (no `account_id`
   column), so the seeded row is account-independent.

### Pre-flight (Q6 — confirm 0 rows first)

```bash
ssh root@178.156.239.84 -- 'set -euo pipefail
  cd /opt/trading
  (
    export SOPS_AGE_KEY_FILE=/etc/credstore.encrypted/age_key
    PG_PASS=$(sops -d secrets/paper.enc.yaml | yq -r .postgres.app_service_password)
    docker compose --env-file deploy/.env exec -T \
      -e PGPASSWORD="$PG_PASS" \
      postgres \
      psql -U app_service -d trading -h postgres -c "
SELECT count(*) AS total,
       count(*) FILTER (WHERE last_active_at IS NULL) AS active
FROM parameter_sets;
"
  )
'
# Expected: total=0, active=0. If a row already exists, SKIP the INSERT below
# (it would no-op anyway) and go straight to the ceremony's UPDATE.
```

### Run command (paper env, dry-run — REQUIRED FIRST)

Default is `--dry-run`; the minted hash is logged without any write.

```bash
ssh root@178.156.239.84 -- 'set -euo pipefail
  cd /opt/trading
  (
    export SOPS_AGE_KEY_FILE=/etc/credstore.encrypted/age_key
    PG_PASS=$(sops -d secrets/paper.enc.yaml | yq -r .postgres.app_service_password)
    docker compose --env-file deploy/.env exec -T \
      -w /app \
      -e PYTHONPATH=/app \
      -e DATABASE_URL="postgresql+asyncpg://app_service:${PG_PASS}@postgres:5432/trading" \
      api \
      /opt/venv/bin/python -m scripts.operator_tools.bootstrap_live_account \
        --env paper \
        --mint-from-defaults \
        --seed-params-only
  )
'
```

Inspect the log for `bootstrap_live_account_minted_baseline_parameter_set` —
note the `parameter_set_hash` (`stored_key_count=12`, `hashed_key_count=10`).
**Record the hash**; the wet run must produce the **same** hash (determinism).

### Run command (paper env, wet — committing)

Same invocation plus the two-flag gate (`--no-dry-run --confirm`). `--env paper`
needs **no** `--allow-non-paper`.

```bash
ssh root@178.156.239.84 -- 'set -euo pipefail
  cd /opt/trading
  (
    export SOPS_AGE_KEY_FILE=/etc/credstore.encrypted/age_key
    PG_PASS=$(sops -d secrets/paper.enc.yaml | yq -r .postgres.app_service_password)
    docker compose --env-file deploy/.env exec -T \
      -w /app \
      -e PYTHONPATH=/app \
      -e DATABASE_URL="postgresql+asyncpg://app_service:${PG_PASS}@postgres:5432/trading" \
      api \
      /opt/venv/bin/python -m scripts.operator_tools.bootstrap_live_account \
        --env paper \
        --mint-from-defaults \
        --seed-params-only \
        --no-dry-run --confirm
  )
'
```

### Verification (post-success)

```bash
# Head row exists; flags stored as "False"; hash matches the dry-run.
... (same ssh + subshell + PG_PASS wrapper) ...
      psql -U app_service -d trading -h postgres -c "
SELECT parameter_set_hash,
       parameters->>'STRATEGY_DECOMMISSIONED' AS decom,
       parameters->>'EXIT_AUTO_APPROVE'        AS auto_approve,
       last_active_at
FROM parameter_sets
ORDER BY first_active_at DESC LIMIT 1;
"
# Expected: 1 row; decom='False'; auto_approve='False'; last_active_at=NULL (head).
```

Also load `/system` in the web UI — the risk-envelope tile now reflects the
seeded values rather than spec defaults.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success (row inserted, idempotent no-op, or dry-run plan printed) |
| 1 | Invalid `--parameter-set-json` (N/A for `--mint-from-defaults`) |
| 4 | Existing-account mismatch — an active `owner` account already exists with a different `external_account_id` than `--external-account-id`; the full bootstrap refuses. Use `--seed-params-only` (already-bootstrapped env) or pass a matching `--external-account-id`. |
| 5 | DB init failure — verify `DATABASE_URL` staged |
| 6 | Invalid CLI args (e.g. `--mint-from-defaults` + `--parameter-set-json` together; or `--seed-params-only` without a param-set source) |
| 99 | Unexpected exception |

### Architecture note

The seed INSERT is **parameterized SQL** (`ON CONFLICT (parameter_set_hash) DO
NOTHING`), not a service-side write path — an intentional bootstrap exception to
the "never raw SQL" norm at the top of this file. `parameter_sets` is
content-addressed and has no operator-facing write endpoint (design F10/L1); no
audit row is written (A01 N/A). `services/version/**` and
`scripts/operator_tools/**` are NOT on the [A02] forbidden whitelist — regular
PR review, no `risk-review-approved` label (PR #294).

---

## Decommission ceremony (exit-pipeline §10.3 smoke)

Proves the `STRATEGY_DECOMMISSIONED` kill-switch end-to-end on paper: flip the
flag → `trigger_v1_cycle` emits an `exit_reason='decommission'` CLOSE for every
held position → operator approves → bracket-stop cancel + close place →
`TRADE_CLOSED`. This is `Docs/exit-pipeline-design.md` §10.3 step 3, unblocked by
the seed tooling above.

> **⚠️ SCOPE — this stops the manual `trigger_v1_cycle` path ONLY, NOT the live
> nightly cycle.** The 21:30 UTC LEAN cycle reads its parameters from
> `lean/lean.json` (NOT the DB), and `lean.json` does not even carry the
> `STRATEGY_DECOMMISSIONED` key — so flipping the DB flag does **nothing** to the
> nightly strategy. **A green smoke here is NOT evidence that the kill-switch
> stops live trading.** Wiring the DB flag (or `lean.json`) into the nightly
> cycle is **parameter-sets-bootstrap-design PR-C (Q3-C)** — not yet shipped.
> (Distinct from exit-pipeline-design's own PR-C #253, which is done.)

> **✅ AUDIT — PR-D shipped the audited ceremony.** Steps 2 + 5 below now use
> `apply_parameter_change.py`, which writes a `parameter_change_applied` /
> `parameter_change_reverted` audit event + a `parameters`-table row (audit-first)
> before flipping the head. The legacy **raw `UPDATE`** (Q4-A smoke mechanism) is
> retained ONLY as a fallback and writes **no audit trail** — do NOT use it for a
> *live* decommission. (Note: making the flip stop the live *nightly* cycle is
> PR-C, which must also be deployed — see the SCOPE caveat above.)

### Pre-conditions

- A seeded `parameter_sets` head row (run the seeding section above first).
- At least one held position (e.g. tonight's /M2K LONG).
- `risk_state.state == 'NORMAL'` (`trigger_v1_cycle` fails closed otherwise).

All `psql` / tool steps below run inside the same `ssh … 'set -euo pipefail; cd
/opt/trading; ( export SOPS_AGE_KEY_FILE=…; PG_PASS=$(sops …); … )'` wrapper
shown in the seeding section — only the `-c "…"` SQL / the module invocation
changes. Secrets stay subshell-scoped and are never echoed
(`feedback_secret_handling.md`).

### Sequence

**1 — Capture the head hash (it must NOT change across the flip).**

```sql
SELECT parameter_set_hash, parameters->>'STRATEGY_DECOMMISSIONED' AS decom
FROM parameter_sets WHERE last_active_at IS NULL;
-- Record parameter_set_hash. Expect decom='False'.
```

**2 — Flip the flag to True (AUDITED ceremony — PR-D, preferred).**

Use the audited tool (PR #—, design §13): it writes a `parameter_change_applied`
audit event FIRST, then a `parameters`-table history row + the in-place
`parameter_sets` flip — all in one ceremony. Run inside the same `ssh + subshell`
wrapper, with `DATABASE_URL` exported as in the seeding section (the tool reads
`os.environ['DATABASE_URL']`):

```bash
/opt/venv/bin/python -m scripts.operator_tools.apply_parameter_change \
  --env paper --parameter STRATEGY_DECOMMISSIONED --value true \
  --reason "decommission smoke <date>" --no-dry-run --confirm
```

`--dry-run` is the default (prints the plan, no writes). The flip is PK-stable —
`parameter_set_hash` is **unchanged** (the hash excludes the flag, design Q1-A),
so the new `parameters` row carries `prev_parameter_set_hash == parameter_set_hash`.
Re-run the step-1 SELECT to confirm `decom='True'` + the hash is unchanged.

> **Fallback (only if PR-D's tool is unavailable):** the legacy raw UPDATE — which
> writes **NO audit trail** and must NOT be used for a *live* decommission:
> ```sql
> UPDATE parameter_sets
> SET parameters = jsonb_set(parameters, '{STRATEGY_DECOMMISSIONED}', to_jsonb('True'::text))
> WHERE last_active_at IS NULL;
> ```

**3 — Run the trigger (exits only, decommission reason only).**

```bash
/opt/venv/bin/python -m scripts.operator_tools.trigger_v1_cycle \
  --env paper \
  --exits-only \
  --reason-filter=decommission \
  --no-dry-run
```

Wet run, so stage `LEAN_LOCAL_BEARER_TOKEN` in the subshell exactly as the
`trigger_v1_cycle` wet-run command does above. Expect one `signal_emitted`
(`signal_type='exit'`, `exit_reason='decommission'`) per held position; the tool
logs each emitted market + reason and a `trigger_v1_cycle_completed` summary.
`--exits-only` keeps the run surgical (the entry pipeline is short-circuited by
the flag anyway).

**4 — Approve + observe.** At `/signals`, approve the decommission exit(s).
Observe bracket-stop cancel + close placement; confirm `TRADE_CLOSED` (the trade
closes in the `/positions` + `/signals` surfaces and the audit chain extends).

**5 — REVERT (HARD GATE — do not skip).**

Same audited tool with `--value false` — emits `parameter_change_reverted`:

```bash
/opt/venv/bin/python -m scripts.operator_tools.apply_parameter_change \
  --env paper --parameter STRATEGY_DECOMMISSIONED --value false \
  --reason "revert decommission smoke <date>" --no-dry-run --confirm
```

Verify `decom='False'` again; `parameter_set_hash` still unchanged.

> **Fallback (only if PR-D's tool is unavailable; no audit trail):**
> ```sql
> UPDATE parameter_sets
> SET parameters = jsonb_set(parameters, '{STRATEGY_DECOMMISSIONED}', to_jsonb('False'::text))
> WHERE last_active_at IS NULL;
> ```

> **If you skip the revert, the flag stays `True` and the *next*
> `trigger_v1_cycle` run will re-emit decommission exits for everything held.**
> (The nightly cycle is unaffected either way — it never reads this flag.) The
> revert is a checklist gate, not a suggestion.

### Verification

Both run inside the same `ssh + subshell + PG_PASS` wrapper as the seeding
section (only the container + command change):

```bash
# 1. Audit chain still verifies after the ceremony (api container):
/opt/venv/bin/python -m services.audit.verify_chain --env paper
# Expected: CHAIN OK: <N> rows verified

# 2. The decommission exit signals landed (postgres container). exit_reason is
#    visible in the trigger tool's per-signal log + the /signals UI; it is NOT a
#    top-level signals column, so filter on signal_type here:
psql -U app_service -d trading -h postgres -c "
SELECT id, market, direction, signal_type, status, emitted_at_utc
FROM signals
WHERE signal_type = 'exit'
  AND emitted_at_utc > NOW() - INTERVAL '1 hour'
ORDER BY emitted_at_utc DESC;
"
```

Plus confirm: `TRADE_CLOSED` for each approved exit; the flag is reverted to
`'False'`; and `parameter_set_hash` was stable throughout (steps 1, 2, 5).

### If something goes wrong

- **Accidental / premature decommission:** run step 5 (flip to `False`)
  immediately, and **reject** any pending decommission exits at `/signals`
  **before** approving them — nothing flattens until you approve. The nightly
  cycle will not flatten the book on its own (it never reads the flag).
- **Hash changed across the flip:** STOP. That means something hashed the flag
  (a regression against design Q1-A / PR #294). Do not proceed; investigate
  `services/version/composite_hash.py`.

### Architecture note

The flag-flip is a raw `UPDATE` (design Q4-A) — the one deliberate exception to
this file's "service-side write paths only" norm, scoped to the paper smoke and
gated behind the two ⚠️ caveats above. No audit event is written for the flip
until PR-D. `Docs/parameter-sets-bootstrap-design.md` is on the `Docs/**` path
(not forbidden); `scripts/operator_tools/**` is hot-fix scope — no
`risk-review-approved` label.

### Lineage

`Docs/parameter-sets-bootstrap-design.md` PR-B. Unblocked by PR-A (#294), which
built the hash minter + `--mint-from-defaults`. The L3/§2 caveat (trigger path
only) and Q4-A (raw UPDATE, audit deferred to PR-D) are signed off in that
design's §11.
