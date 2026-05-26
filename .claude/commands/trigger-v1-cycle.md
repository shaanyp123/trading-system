---
description: Generate the operator-side SSH ceremony for an on-demand V1 strategy cycle trigger (mirrors LEAN's 21:30 UTC cycle)
argument-hint: <env: paper|live-small|live-scale> [--session-date YYYY-MM-DD]
allowed-tools: [Bash, Read]
---

# Trigger V1 strategy cycle on demand

Generates the exact operator-side SSH ceremony for invoking `scripts/operator_tools/trigger_v1_cycle.py` against the specified env. The tool runs INSIDE the api container ON THE VPS — Claude Code locally cannot execute it. This command outputs the canonical commands per `scripts/operator_tools/README.md` so the operator can copy-paste into their SSH session.

**Default flow is two-step:** dry-run first → operator reviews output → wet run with `--no-dry-run`. The tool's `--dry-run` default is True, so the dry-run command is literally what runs without the operator opting in to the wet path.

## Steps

1. **Validate env arg.** Must be one of: `paper`, `live-small`, `live-scale`. (No `dev` — the audit_log.env CHECK constraint rejects it; the tool's argparse mirrors the constraint.) If missing or invalid: output usage error referencing the canonical envs from dev-guide §1.5.

2. **Parse optional `--session-date YYYY-MM-DD`.** If provided, pass through. If omitted, the tool defaults to today's ET calendar date.

3. **Output the SSH context block:**

```bash
ssh trading@178.156.239.84
cd /opt/trading
```

4. **Output the pre-flight check block** (operator runs ONCE before the dry-run):

```bash
# Pre-flight 1: api container healthy
docker compose --env-file deploy/.env exec -T api \
  /opt/venv/bin/python -c "print('api importable')" || \
  { echo "FAIL: api container not healthy"; exit 1; }

# Pre-flight 2: lean_data volume mounted in api
docker compose --env-file deploy/.env exec -T api \
  ls /Lean/Data/equity/usa/daily/ 2>&1 | head -5 || \
  { echo "FAIL: /Lean/Data not mounted in api"; exit 1; }

# Pre-flight 3: sops decrypts cleanly
export SOPS_AGE_KEY_FILE=/etc/credstore.encrypted/age_key
sops --decrypt secrets/<env>.enc.yaml > /dev/null || \
  { echo "FAIL: sops can't decrypt secrets/<env>.enc.yaml"; exit 1; }
```

5. **Output the DRY-RUN command** (operator runs this FIRST; bearer NOT required for dry-run):

```bash
ssh trading@178.156.239.84 -- 'set -euo pipefail
  cd /opt/trading
  (
    export SOPS_AGE_KEY_FILE=/etc/credstore.encrypted/age_key
    PG_PASS=$(sops -d secrets/<env>.enc.yaml | yq -r .postgres.app_service_password)
    docker compose --env-file deploy/.env exec -T \
      -w /app \
      -e PYTHONPATH=/app \
      -e DATABASE_URL="postgresql+asyncpg://app_service:${PG_PASS}@postgres:5432/trading" \
      api \
      /opt/venv/bin/python -m scripts.operator_tools.trigger_v1_cycle \
        --env <env> \
        <session-date-flag-if-set>
  )
'
```

6. **Output the structured-log inspection guidance** (what the operator should look for in the dry-run output):

```
Expected dry-run output keys (each is a structlog line):

  trigger_v1_cycle_started        — invocation metadata; confirms dry_run=True
  trigger_v1_cycle_context_loaded — account_id, risk_state (MUST be NORMAL),
                                     active_universe, dedup set, equity
  trigger_v1_cycle_futures_member_selected — per-futures-market: which
                                              <lower>_trade_<YYYYMM>.csv was
                                              picked as the continuous series
  trigger_v1_cycle_bars_stale     — WARNING per market if newest bar > 5 days
                                     old; investigate bar_sync if unexpected
  trigger_v1_cycle_rejection      — one per rejected market (no_breakout,
                                     trend_filter_failed, hurst_below_threshold,
                                     etc.) — forensic visibility
  trigger_v1_cycle_dedup_skip     — one per market that already has a signal
                                     row for the session_date
  trigger_v1_cycle_dry_run_would_post — one per signal the strategy emitted
                                         (would POST in the wet run)
  trigger_v1_cycle_completed      — summary: signals_generated, dedup_skipped,
                                     dry_run_skipped, rejections by reason

If risk_state != NORMAL → tool exits with code 1 (EXIT_RISK_STATE_BLOCKED).
This is a HARD ABORT; resume the kill-switch state machine via /system before retrying.
```

7. **Output the WET RUN command** (operator runs ONLY after dry-run looks correct):

```bash
ssh trading@178.156.239.84 -- 'set -euo pipefail
  cd /opt/trading
  (
    export SOPS_AGE_KEY_FILE=/etc/credstore.encrypted/age_key
    PG_PASS=$(sops -d secrets/<env>.enc.yaml | yq -r .postgres.app_service_password)
    LEAN_BEARER=$(sops -d secrets/<env>.enc.yaml | yq -r .lean.api_bearer_token)
    docker compose --env-file deploy/.env exec -T \
      -w /app \
      -e PYTHONPATH=/app \
      -e DATABASE_URL="postgresql+asyncpg://app_service:${PG_PASS}@postgres:5432/trading" \
      -e LEAN_LOCAL_BEARER_TOKEN="${LEAN_BEARER}" \
      api \
      /opt/venv/bin/python -m scripts.operator_tools.trigger_v1_cycle \
        --env <env> \
        <session-date-flag-if-set> \
        --no-dry-run<allow-non-paper-if-live>
  )
'
```

Notes (emit verbatim with the command):

- Both `PG_PASS` and `LEAN_BEARER` are subshell-scoped. They do NOT persist outside the parenthesized block.
- `LEAN_LOCAL_BEARER_TOKEN` is the same bearer `lean_local` uses (`secrets/<env>.enc.yaml::lean.api_bearer_token`).
- For `live-small` / `live-scale`: append `--allow-non-paper` (the tool fails closed without it).

8. **Substitute `<env>`** with the actual env arg in all occurrences. If `--session-date YYYY-MM-DD` was provided, substitute `<session-date-flag-if-set>` with `--session-date YYYY-MM-DD`; otherwise leave it empty. If env is `live-*`, substitute `<allow-non-paper-if-live>` with ` --allow-non-paper`; otherwise empty.

9. **Output post-success verification block:**

```bash
# Verify the audit chain extends + still passes
docker compose --env-file deploy/.env exec -T \
  -e DATABASE_URL="$DATABASE_URL" \
  api \
  /opt/venv/bin/python -m services.audit.verify_chain --env <env>
# Expected: CHAIN OK: <N> rows verified (N larger by however many signals POSTed)

# Verify the signals table has new rows
APP_SERVICE_PW=$(sops -d secrets/<env>.enc.yaml | yq -r .postgres.app_service_password)
docker compose --env-file deploy/.env exec -T \
  -e PGPASSWORD="$APP_SERVICE_PW" \
  postgres \
  psql -U app_service -d trading -h postgres -c "
SELECT id, market, direction, status, session_date, emitted_at_utc
FROM signals
WHERE emitted_at_utc > NOW() - INTERVAL '1 hour'
ORDER BY emitted_at_utc DESC
LIMIT 10;
"
unset APP_SERVICE_PW
```

10. **Anti-pattern reminders:**

- Per memory `feedback_secret_handling.md`: NEVER `cat` the sops decrypt output. NEVER `echo $LEAN_BEARER` or `echo $PG_PASS`. NEVER print the decrypted YAML to stdout. The `yq -r` extraction is the only consumer.
- Per memory `feedback_no_destructive_shortcuts.md`: if the tool exits 1 (risk_state blocked), DO NOT try to "fix" the risk_state by direct UPDATE. Investigate the kill-switch state machine via `/system` and resume properly.
- Per `Docs/claude-dev-guide.md` §1.5: this tool POSTs to `/api/internal/lean/signals` which writes to `audit_log` via the canonical `ingest_signal_emitted` path. Audit-first ordering is the api's responsibility — the tool stays out of the audit-write path.

11. **Output expected timing:** with all 9 active markets (V1_CANDIDATE_UNIVERSE minus /MCL) and ~250 bars each on disk, the tool completes in <5s. Dry-run is faster (~2s; no HTTP roundtrips). If the tool hangs beyond 30s, the api / DB / disk read is wedged — check `docker compose ps` + `docker compose logs api --tail 20`.

## Exit-code crib sheet

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Risk state blocked dispatch (not NORMAL) — DO NOT bypass; resume via /system |
| 3 | ≥1 signal POST returned non-2xx (other signals may have landed) |
| 5 | DB init failure (DATABASE_URL missing / Postgres unreachable) |
| 6 | Invalid CLI args (live-env requires --allow-non-paper) |
| 7 | LEAN_LOCAL_BEARER_TOKEN unset (only in --no-dry-run) |
| 99 | Unexpected exception (traceback on stderr) |

## Cross-refs

- Canonical runbook: `scripts/operator_tools/README.md` (search for "trigger_v1_cycle.py")
- The tool itself: `scripts/operator_tools/trigger_v1_cycle.py`
- LEAN's natural cycle (which this tool mirrors): `lean/v1_strategy.py::on_daily_signal_cycle`
- The api endpoint: `services/api/routes/internal/lean.py::post_lean_signal`
- Strategy logic: `strategies/v1_trend_following/strategy.py::V1TrendFollowing.generate_signals`
- Memory: `project_phase_status_operational`, `project_clientid_allocation`, `feedback_secret_handling`
