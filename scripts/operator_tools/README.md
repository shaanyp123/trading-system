# Operator recovery tooling

Committed operator tools for production-state recovery. Distinct from
`scripts/` (laptop-side ceremony scripts) — anything here runs inside
the api container against production state via the canonical
service-side write paths (never raw SQL).

## Contents

| Script | Purpose | Mutates state? |
|---|---|---|
| `replay_executions.py` | Pull executions back from IBKR and feed them through `process_fill_event` to recover from backend-blind fills | Yes — writes audit chain + fills / positions_current / balances / trades |

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
