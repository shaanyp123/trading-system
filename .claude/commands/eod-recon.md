---
description: Generate observation + diagnostic commands for the daily EOD reconciliation cycle
argument-hint: [date: YYYY-MM-DD or "today"]
allowed-tools: [Bash, Read]
---

# EOD reconciliation observation

Generates the operator-side commands to observe + diagnose the daily EOD reconciliation cycle (22:30 UTC / 18:30 ET). The cycle runs inside the api lifespan via `services/reconciliation/eod_cycle.py::ReconciliationScheduler`. This command outputs the SSH commands to watch the cycle, inspect breaks, and re-run the planner if needed.

## Steps

1. **Resolve the target date.** If arg is "today" or empty: use today's UTC date. Otherwise parse the YYYY-MM-DD arg.

2. **Output the live-watch block** (run BEFORE 22:30 UTC to catch the cycle live):

```bash
# Watch the cycle fire
docker compose logs api --since 30m --follow | grep -E "reconciliation_cycle|reconciliation_break|flex_query"
```

3. **Output the after-the-fact diagnostic block:**

```bash
# Was the cycle attempted?
docker compose logs api --since 24h | grep "reconciliation_cycle_firing\|reconciliation_cycle_completed\|reconciliation_scheduler_skipped"

# Were any breaks detected?
docker compose logs api --since 24h | grep "reconciliation_break_detected\|reconciliation_break_resolved"

# Cross-check the reconciliation_breaks table (last 7 days)
docker compose exec -T -e PGPASSWORD="$APP_SERVICE_PW" postgres psql -U app_service -d trading \
  -c "SELECT id, account_id, env, kind, market, detected_at_utc, resolution_status FROM reconciliation_breaks WHERE detected_at_utc > NOW() - INTERVAL '7 days' ORDER BY detected_at_utc DESC;"
```

4. **Output the "did the scheduler arm" check:**

```bash
# At api boot, the scheduler logs one of:
#   reconciliation_scheduler_started      — sops flex_query_id + token populated, scheduler armed
#   reconciliation_scheduler_skipped      — sops field missing or placeholder; defensive-skip
docker compose logs api --since 24h | grep -m1 "reconciliation_scheduler"
```

5. **Anti-pattern reminders:**
   - `docker compose logs api` may emit secret-laden lines if a log line accidentally formats a credential field. Per memory `feedback_secret_handling.md`, ROUTE TO FILE if you need to grep through it: `docker compose logs api --since 24h > /tmp/api_logs && chmod 600 /tmp/api_logs && grep ... /tmp/api_logs > /tmp/recon_lines && shred -u /tmp/api_logs && wc -l /tmp/recon_lines`.
   - If a break is detected: DO NOT manually edit the `reconciliation_breaks` table or `risk_state` row. Use the operator web `/system` page or escalate per `Docs/decisions-log.md` 2026-05-18 ib_gateway-restart precedent.

## Cross-refs

- Canonical runbook: `deploy/reconciliation/README.md`
- Planner: `services/reconciliation/recon.py::plan_reconciliation_check`
- Orchestrator: `services/reconciliation/apply.py::apply_reconciliation_plan`
- Scheduler: `services/reconciliation/eod_cycle.py::ReconciliationScheduler`
- FlexQuery fetcher: `services/reconciliation/flex_query_fetcher.py`
- Memory: `reference_verification_ceremonies.md`
