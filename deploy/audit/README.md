# Day 12 — `services/audit/verify_chain.py` operator runbook

End-to-end smoke for the audit-chain integrity CLI. This is the A27
smoke fixture per `Docs/claude-dev-guide.md` §6.8 alternative (b)
(operator-runbook checklist) — same shape as `lean/README.md`
Steps 4–7, `watchdog/README.md` Steps 6–7, `deploy/api/README.md`
Steps 1–5, and `deploy/webhook_pusher/README.md` Steps 1–8.

**Closes Week 4 verification gate box 2** (per
`implementation-guide.md` §3 Week 4 Tue):
"`python3 services/audit/verify_chain.py --env paper` returns
`CHAIN OK: N rows verified`."

If anything fails, capture the exact error + the step number and stop.
Root-cause discipline per dev-guide §1.3 — we debug rather than blow
past it.

## Prerequisites

- Ashburn VPS reachable via SSH (`ssh root@178.156.239.84`).
- `/opt/trading` checked out at the Day 12 PR commit (or later).
  `git pull origin main` if the VPS is behind. Day 11 carryover
  resolved the VPS's stale-HEAD `secrets/paper.enc.yaml` issue;
  if that recurs, see `Docs/decisions-log.md` 2026-05-12 Day 11
  carryover entry for the empirical-diff procedure.
- The `api` image must contain `services/audit/verify_chain.py`.
  PR #50 keeps the `api` container on `trading_internal` (no external
  internet by design) — the chain CLI does not need egress, so it is
  invoked from inside the `api` container directly. If the api image
  is older than the Day 12 PR merge, rebuild:

  ```bash
  docker compose --env-file deploy/.env build api && \
  docker compose --env-file deploy/.env up -d --force-recreate api
  ```

- `secrets/paper.enc.yaml` decryption working. The age key lives at
  `/etc/credstore.encrypted/age_key` per `deploy/.env`'s
  `SOPS_AGE_KEY_FILE`; every `sops --decrypt` invocation here needs
  that env var exported in the current shell. Day 6 carryover
  verified `wc -c == 64` on `app_service_password`; if `sops -d`
  errors, fix that before continuing.

### Architecture note (where this CLI runs)

The walker reads `audit_log` only — no outbound HTTP, no third-party
APIs. The CLI runs from inside the **`api` container** because that
container already has the `services/` package baked in and has the
correct `app_service` Postgres role available on `trading_internal`.
We do NOT use the `webhook_pusher` container for this — it is on
`trading_egress` for outbound delivery and has no audit reason to
read `audit_log`. We do NOT run from the host because the `services`
Python package lives inside the image, not on the VPS host filesystem.

## Step 1 — Decrypt sops + extract `app_service` password

On the VPS:

```bash
ssh root@178.156.239.84
cd /opt/trading

# Required: point sops at the age key (per deploy/.env). Without this,
# sops --decrypt errors with "failed to load age identities".
export SOPS_AGE_KEY_FILE=/etc/credstore.encrypted/age_key

# Confirm the api container has the new CLI module.
docker compose --env-file deploy/.env exec -T api \
  test -f /app/services/audit/verify_chain.py || \
  { echo "MISSING: api image is older than Day 12 — rebuild via 'docker compose --env-file deploy/.env build api && docker compose --env-file deploy/.env up -d --force-recreate api'"; exit 1; }

# Decrypt to a tmpfs path; never to disk.
sops --decrypt secrets/paper.enc.yaml > /dev/shm/paper.decrypted.yaml
chmod 600 /dev/shm/paper.decrypted.yaml

# Sanity-check app_service_password is non-empty.
APP_SERVICE_PWD=$(awk '$1 == "app_service_password:" {print $2; exit}' \
  /dev/shm/paper.decrypted.yaml)
test -n "$APP_SERVICE_PWD" || \
  { echo "MISSING: postgres.app_service_password not in /dev/shm/paper.decrypted.yaml"; exit 1; }
echo "app_service_password length: ${#APP_SERVICE_PWD}"
# Expected: 64 (32-byte hex string from openssl rand -hex 32 at Day 5).
```

**On mismatch:** if `app_service_password` is empty or shorter than
64 chars, the secret was never filled in `secrets/paper.enc.yaml`.
Run `sops secrets/paper.enc.yaml`, locate `postgres.app_service_password`,
and check whether it is still a `<TODO_FROM_DAY_3_POSTGRES_BOOTSTRAP>`
placeholder. See `deploy/api/README.md` Step 4a for the canonical fill
procedure.

## Step 2 — Stage `DATABASE_URL` for the CLI

The CLI reads its connection string from the bare `DATABASE_URL` env var
(NOT the api process's `API_DATABASE_URL` — the CLI is independent of
the api process's prefixed config). On the VPS, still in the same shell
that sourced `APP_SERVICE_PWD` from Step 1:

```bash
# SQLAlchemy + asyncpg URL shape; postgres host is the docker-compose
# service name (resolves over trading_internal).
export DATABASE_URL="postgresql+asyncpg://app_service:${APP_SERVICE_PWD}@postgres:5432/trading"

# Sanity print (host/role only; never echo the full URL with the password).
echo "DATABASE_URL host: $(echo $DATABASE_URL | sed 's|.*@\([^/]*\)/.*|\1|')"
echo "DATABASE_URL role: $(echo $DATABASE_URL | sed 's|postgresql+asyncpg://\([^:]*\):.*|\1|')"
# Expected:
#   DATABASE_URL host: postgres:5432
#   DATABASE_URL role: app_service
```

**On mismatch:** if either echo prints an empty field, the
`APP_SERVICE_PWD` value was lost between shells. Re-source from
Step 1 in the current terminal — env vars are subshell-local.

## Step 3 — Run the verifier

The CLI runs inside the `api` container with `DATABASE_URL` injected
explicitly via `docker compose exec env`:

```bash
docker compose --env-file deploy/.env exec -T \
  -e DATABASE_URL="$DATABASE_URL" \
  api \
  /opt/venv/bin/python -m services.audit.verify_chain --env paper
```

**Expected (Phase 0, before any audit events have been written):**

```
CHAIN OK: 0 rows verified
```

The exit code is `0`. The 0-row case is vacuously intact and is the
correct state for a fresh paper environment whose `audit_log` table
has been migrated but never written to. The verification gate considers
this a PASS — the CLI's contract is "the chain is intact," not
"the chain has rows."

**Expected (Phase 0+, after `services/api` or other writers have
emitted audit events):**

```
CHAIN OK: <N> rows verified
```

…where `<N>` matches `SELECT COUNT(*) FROM audit_log` (you can
double-check via Step 4 below if you want belt-and-suspenders).

**On mismatch:**

- `CHAIN BREAK at sequence_no=<X> (after <K> verified rows)` —
  rows 1 through `K` are vouched-for; row `X` is the offending row.
  This is a P0 incident: per backend-spec §2.10.1 + §11.1, a hash-chain
  break triggers HALT_NEW with severity `incident_review`. **Stop the
  CLI loop, leave the chain alone, and escalate immediately.** Do NOT
  attempt to delete or repair the offending row — `audit_log` is
  append-only by trigger (`alembic/versions/0005_immutability.py`),
  and any "fix" would falsify the audit trail. Recovery path: capture
  the row's payload via the read-only export tool (Phase 1), run the
  forensic recompute against the canonical source (QC adapter + paper
  trading audit upstream), and append a `repaired_for_sequence_no`
  audit event per backend-spec §2.11 "loss handling" — that work is
  out of scope for this runbook.

- `ERROR: DATABASE_URL is not set.` — Step 2 didn't propagate.
  Re-export `APP_SERVICE_PWD` (Step 1) and `DATABASE_URL` (Step 2) in
  the current shell, then retry Step 3.

- `argparse error: invalid choice: 'production'` — `--env` accepts
  only `dev`, `paper`, `live-small`, `live-scale`. Use the value
  matching `audit_log.env`'s CHECK constraint for the current
  environment.

- `OperationalError: ... password authentication failed for user
  "app_service"` — `app_service_password` in sops doesn't match the
  Postgres role's actual password. Recovery: run `deploy/day5-bringup.sh`
  Step 6 (`ALTER ROLE app_service WITH PASSWORD ...`) to resync from
  sops; or, if sops is the truth, run `sops secrets/paper.enc.yaml`
  and confirm the password matches what Postgres has.

- `OperationalError: ... could not translate host name "postgres" to
  address` — the api container is not on `trading_internal`. Run
  `docker inspect trading-api-1 --format '{{json .NetworkSettings.Networks}}'`
  and verify `trading_internal` is in the list. If not, restart via
  `docker compose --env-file deploy/.env up -d --force-recreate api`.

- `ImportError: No module named services.audit.verify_chain` — the api
  image is older than the Day 12 PR. Rebuild per Step 1's prerequisite
  block.

## Step 4 — Cross-check the count (optional but cheap)

Independent count via psql to confirm the CLI's `<N>` matches the table:

```bash
APP_SERVICE_PWD=$(awk '$1 == "app_service_password:" {print $2; exit}' \
  /dev/shm/paper.decrypted.yaml)

docker compose --env-file deploy/.env exec -T \
  -e PGPASSWORD="$APP_SERVICE_PWD" \
  postgres \
  psql -U app_service -d trading -h postgres -c \
    "SELECT COUNT(*) AS rows, COALESCE(MAX(sequence_no), 0) AS max_seq FROM audit_log;"
```

**Expected:** `rows` matches the `<N>` from Step 3's `CHAIN OK: <N>
rows verified` line. `max_seq` may be greater than `rows` if any prior
SERIALIZABLE retry consumed `BIGSERIAL` ticks without committing — that
is by design (writer.py docstring + Day 8 PR #39 close-out) and not a
chain break.

**On mismatch:** if `rows < N`, you are looking at a different
database than the CLI ran against (check `DATABASE_URL`'s host); if
`rows > N`, the chain is GROWING during the walk (a writer is active),
which is fine — the next run will report the new tail.

## Step 5 — Cleanup

```bash
# Wipe the decrypted secrets from tmpfs.
shred -u /dev/shm/paper.decrypted.yaml

# Logout of the SSH session (env vars are subshell-local; closing the
# shell discards them). Belt-and-suspenders:
unset DATABASE_URL APP_SERVICE_PWD SOPS_AGE_KEY_FILE
exit
```

## Closure of the Week 4 verification gate box 2

Once Step 3 prints `CHAIN OK: <N> rows verified` and exits 0, the
second box of the Week 4 gate
(`implementation-guide.md` §3 Week 4 Tue:
"`python3 services/audit/verify_chain.py --env paper` returns
`CHAIN OK: N rows verified`") flips to `[x]`.

Capture for the Day 12 close-out in `Docs/decisions-log.md`:

- The exact stdout line from Step 3 (`CHAIN OK: <N> rows verified`).
- The `<N>` value (so we have a chain-length anchor for future runs).
- The cross-check from Step 4 (count + max_seq) if you ran it.

Box 3 (concurrency test under 10 concurrent writes) is Day 14 work in
a separate PR — not part of this runbook.

## Caveats

- **DOES NOT mutate state.** The CLI is read-only; it issues a single
  `SELECT * FROM audit_log ORDER BY sequence_no ASC`. There is no risk
  of accidentally writing to `audit_log` even if you run it on a live
  paper or live-scale environment. The `app_service` role does not have
  TRUNCATE on `audit_log` per `alembic/versions/0006_roles.py`.
- **NOT a periodic job.** This runbook is for the verification gate
  + ad-hoc operator runs. The Phase 1 periodic-integrity-check job
  (every 24h via cron, also calls `verify_chain`) lives in a separate
  service and is wired up in Week 5+.
- **A02 binding.** `services/audit/**` is on the dev-guide §2.2
  forbidden whitelist. Future edits to `services/audit/verify_chain.py`
  require a `risk-review-approved` PR label; the pre-merge linter
  blocks otherwise. Operator runbook edits (this file) are off the
  forbidden whitelist; regular PR review applies.

## Module surface (for next agent)

| Function | File | Purpose |
|---|---|---|
| `verify_chain` | `services/audit/chain.py` | Async walker; returns `(ok, broken_seq_no, rows_walked)` |
| `main` | `services/audit/verify_chain.py` | argparse + asyncio.run + DATABASE_URL CLI shell |
| `python -m services.audit.verify_chain --env paper` | (entry point) | What this runbook executes |

For test coverage see `tests/unit/test_audit_chain.py::TestVerifyChainWalker`
(5 fake-session tests covering all four return branches),
`tests/unit/test_verify_chain_cli.py` (CLI argparse + env + stdout
contract; 14 tests across 5 `Test*` classes), and
`tests/integration/test_audit_writer.py::test_verify_chain_count_matches_select_count`
(testcontainers; locks the SQL contract for the `<N>` value above).

---

## Daily automated verification (systemd timer)

The runbook above is operator-driven (manual). For 24/7 safety-net
coverage independent of operator presence, a systemd timer runs the
verification daily at 02:00 ET (06:00 UTC) and posts the result to
the Discord `#audit` channel.

**Files (in repo):**
- `scripts/operator_tools/verify_chain_to_discord.sh` — the script
- `deploy/audit/systemd/verify-chain-daily.service` — what runs
- `deploy/audit/systemd/verify-chain-daily.timer` — when (02:00 ET)

**Why a dedicated Discord webhook (not webhook_pusher):**
Independent of the api service. If the audit chain is broken AND api
is down, this script's curl path still works as long as docker + the
postgres container are running. Avoids compounded-failure silence.

### Install ceremony (operator-side, run once on the VPS)

**Step 1 — Create the Discord webhook.**

In Discord: `#audit` channel → settings (gear icon) → Integrations →
Webhooks → New Webhook → Name it "verify-chain cron" → Copy Webhook
URL.

**Step 2 — Save the webhook URL on the VPS (per `feedback_secret_handling.md`).**

```bash
# SSH to VPS as operator/trading user. Paste the URL via a here-doc to
# avoid it appearing in your shell history.
sudo mkdir -p /etc/trading
sudo tee /etc/trading/audit-webhook.url > /dev/null <<'EOF'
PASTE_WEBHOOK_URL_ON_THIS_LINE
EOF
sudo chmod 600 /etc/trading/audit-webhook.url
sudo chown trading:trading /etc/trading/audit-webhook.url

# Verify file size only — never display content
wc -c /etc/trading/audit-webhook.url   # Discord webhook URLs are ~120-150 bytes
```

**Step 3 — Install the systemd units.**

```bash
sudo cp /opt/trading/deploy/audit/systemd/verify-chain-daily.service \
        /etc/systemd/system/verify-chain-daily.service
sudo cp /opt/trading/deploy/audit/systemd/verify-chain-daily.timer \
        /etc/systemd/system/verify-chain-daily.timer

sudo systemctl daemon-reload
sudo systemctl enable --now verify-chain-daily.timer
```

**Step 4 — Verify the timer is armed.**

```bash
systemctl list-timers verify-chain-daily.timer
# Expected: NEXT column shows tomorrow 06:00 UTC
```

**Step 5 — Smoke test (manual fire).**

```bash
sudo systemctl start verify-chain-daily.service
journalctl -u verify-chain-daily.service --since '1 min ago'
# Expected: clean exit; no errors. Check the #audit Discord channel
# for a message like "OK 2026-05-26T... — env=paper, CHAIN OK: N rows verified"
```

**Step 6 — Rotation: rotating the Discord webhook URL.**

If you ever need to rotate (e.g., the URL leaked), regenerate in
Discord → repeat Step 2 → no restart of the timer needed (the script
re-reads the file on every run).

### Failure modes

| Symptom | Diagnosis | Fix |
|---|---|---|
| No Discord post for >24h | Timer not armed | Re-run Step 4; if NEXT is empty, re-run Step 3 |
| Discord posts `verify-chain cron: cannot read /etc/...webhook.url` | Step 2 not done or wrong perms | Re-run Step 2 |
| Discord posts `AUDIT CHAIN BREAK at sequence_no=...` | Real chain break — incident-level | Stop. Run the manual ceremony from §3 above. Escalate per `Docs/decisions-log.md` |
| Discord posts `unexpected exit=N` | api container down or docker daemon issue | Check `docker compose ps`; restart api if needed; manual ceremony to re-verify chain afterward |
| Posts stop coming silently | The webhook URL was revoked at Discord side | Step 6 (re-create + replace file) |

---

## Recovery agent — install ceremony

A second systemd timer (landed alongside the verify-chain timer in
2026-05-26) polls the `alerts` table every 60s for unhandled
`worker_failure` events — INSERTed by the new task-death hook in
`services/api/async_task_monitor.py` when a tracked lifespan task
(today: `order_placement_worker.run_forever`) transitions to `.done()`
unexpectedly. For each unhandled alert the agent classifies the
failure (transient vs hard crash), invokes
`scripts/operator_tools/replay_executions.py` for transient failures,
audit-first emits `RECOVERY_ACTION_TAKEN`, UPDATEs the alert row, and
posts a recovery summary to Discord `#critical`. Closes the manual
operator step from drill 5 (2026-05-18) + drill 7 (2026-05-18) where
the operator hand-ran `replay_executions.py` to recover backend-blind
fills.

**Files (in repo):**
- `scripts/operator_tools/recovery_agent.py` — the agent
- `scripts/operator_tools/recovery_agent_tick.sh` — bash wrapper
- `deploy/audit/systemd/recovery-agent-poll.service` — what runs
- `deploy/audit/systemd/recovery-agent-poll.timer` — every 60s

**Why a dedicated Discord webhook (not webhook_pusher):**
Independent of the api service — same reasoning as the audit cron. If
the api is down, the recovery agent's direct Postgres + Discord access
still fires. Pairs with autoheal (PR #240, handles gateway stuck-state)
and the verify-chain cron above as the third pillar of the 24/7 safety
net.

### Install ceremony (operator-side, run once on the VPS)

**Step 1 — Create the Discord webhook for `#critical`.**

If you don't already have one: in Discord, `#critical` channel →
settings (gear icon) → Integrations → Webhooks → New Webhook → Name
it "recovery-agent" → Copy Webhook URL.

If you have an existing `#critical` webhook (e.g., from
`secrets/paper.enc.yaml::discord.webhook_urls.critical`), reuse that
URL — the recovery agent and webhook_pusher can share the same
target. The file is the single point of truth for the systemd path.

**Step 2 — Save the webhook URL on the VPS (per `feedback_secret_handling.md`).**

```bash
# SSH to VPS as operator/trading user. Paste the URL via here-doc.
sudo mkdir -p /etc/trading
sudo tee /etc/trading/critical-webhook.url > /dev/null <<'EOF'
PASTE_CRITICAL_WEBHOOK_URL_ON_THIS_LINE
EOF
sudo chmod 600 /etc/trading/critical-webhook.url
sudo chown trading:trading /etc/trading/critical-webhook.url

# Verify file size only — never display content
wc -c /etc/trading/critical-webhook.url   # Discord webhook URLs are ~120-150 bytes
```

**Step 3 — Apply the alembic migration (adds `worker_failure` to alert_category enum).**

```bash
cd /opt/trading
git pull --ff-only  # if not already done
docker compose --env-file deploy/.env exec api alembic upgrade head

# Verify the migration applied
docker compose --env-file deploy/.env exec api alembic current
# Expected: 20260526_worker_failure (head)
```

**Step 4 — Rebuild api container (picks up the new monitor hook).**

```bash
docker compose --env-file deploy/.env build api
docker compose --env-file deploy/.env up -d --force-recreate api

# Watch api boot logs for the new wiring
docker compose --env-file deploy/.env logs api --tail 50 | grep -E "task_death_alert_hook_constructed|async_task_monitor_spawned"
# Expected:
#   task_death_alert_hook_constructed
#   async_task_monitor_spawned interval_seconds=30.0 ... task_death_hook_wired=True
```

**Step 5 — Install the systemd units.**

```bash
sudo cp /opt/trading/deploy/audit/systemd/recovery-agent-poll.service \
        /etc/systemd/system/recovery-agent-poll.service
sudo cp /opt/trading/deploy/audit/systemd/recovery-agent-poll.timer \
        /etc/systemd/system/recovery-agent-poll.timer

sudo systemctl daemon-reload
sudo systemctl enable --now recovery-agent-poll.timer
```

**Step 6 — Verify the timer is armed.**

```bash
systemctl list-timers recovery-agent-poll.timer
# Expected: NEXT column shows ~60s out; ACTIVATES shows the .service unit
```

**Step 7 — Smoke test via synthetic alert.**

```bash
# INSERT a synthetic worker_failure alert directly into the alerts
# table to drive the agent through its full code path without
# actually killing a worker.
docker compose --env-file deploy/.env exec postgres psql -U app_service -d trading -c \
  "INSERT INTO alerts (account_id, severity, category, message, detail)
   VALUES (
     (SELECT id FROM accounts LIMIT 1),
     'P0',
     'worker_failure',
     'SYNTHETIC: recovery-agent install smoke test',
     '{\"task_name\":\"order_placement_worker.run_forever\",
       \"exit_reason\":\"exception\",
       \"exception_type\":\"TimeoutError\",
       \"exception_repr\":\"TimeoutError(synthetic)\",
       \"observed_at_utc\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}'::jsonb
   );"

# Wait 60-90s. The next tick of recovery-agent-poll.timer fires and
# processes the synthetic alert.

# Verify the audit chain has a RECOVERY_ACTION_TAKEN row
docker compose --env-file deploy/.env exec api /opt/venv/bin/python -m services.audit.verify_chain --env paper | tail -5

# Verify the alerts row is now acknowledged + resolved
docker compose --env-file deploy/.env exec postgres psql -U app_service -d trading -c \
  "SELECT id, acknowledged, resolved_at_utc IS NOT NULL AS resolved,
          detail->'recovery_outcome'->>'decision' AS decision
   FROM alerts WHERE message LIKE 'SYNTHETIC:%';"

# Check Discord #critical for the recovery summary embed
# Title: "Recovery agent: invoke_replay for order_placement_worker.run_forever"
# (decision is invoke_replay because TimeoutError is in TRANSIENT_EXCEPTION_TYPES;
# the orphan-CID query likely returns empty in a quiet system → replay
# subprocess returns invoked=False but the agent still processes the alert)
```

**Step 8 — Cleanup the synthetic alert (optional but recommended).**

```bash
docker compose --env-file deploy/.env exec postgres psql -U app_service -d trading -c \
  "DELETE FROM alerts WHERE message LIKE 'SYNTHETIC:%';"

# Note: the RECOVERY_ACTION_TAKEN audit row is immutable per alembic
# 0005 — it stays in the chain. That's correct behavior (the chain is
# append-only); the operator can ignore the synthetic row in future
# verify_chain runs.
```

### Operations

```bash
# Tail the agent's per-tick output
journalctl -u recovery-agent-poll.service --since '5 min ago'

# How often is the timer firing?
systemctl list-timers recovery-agent-poll.timer

# Force a tick manually (for testing or post-incident)
sudo systemctl start recovery-agent-poll.service
journalctl -u recovery-agent-poll.service --since '1 min ago'

# Disable temporarily (e.g., during a planned operator-led incident)
sudo systemctl stop recovery-agent-poll.timer
# Re-enable:
sudo systemctl start recovery-agent-poll.timer

# Rotate the Discord webhook URL
# Repeat Step 2 above with a new URL; no restart needed (the script
# re-reads the file on every tick).
```

### Failure modes

| Symptom | Diagnosis | Fix |
|---|---|---|
| Discord `#critical` silent after a known worker death | Either the monitor hook isn't wired OR the recovery agent timer isn't firing | Check Step 4 boot logs for `task_death_hook_wired=True`; then Step 6 to verify the timer; then `journalctl -u recovery-agent-poll.service` for the per-tick logs |
| `recovery_agent_no_critical_webhook_url` WARNING in journal | Step 2 not done or perms wrong | Re-run Step 2 |
| `recovery_agent_db_init_failed` ERROR | DATABASE_URL build failed | Check sops decrypt: `sops -d secrets/paper.enc.yaml | grep app_service_password`. Check `journalctl -u recovery-agent-poll.service` for the bash wrapper's error |
| Synthetic alert in Step 7 stays acknowledged=FALSE | Timer firing but agent crashing per-tick | `journalctl -u recovery-agent-poll.service -n 100` for the traceback. Common cause: alembic migration in Step 3 was skipped; the `worker_failure` enum value doesn't exist yet |
| `recovery_agent_alert_processing_failed` with FillProcessingError | Replay subprocess hit a terminal fill-processor error | Investigate via the replay script's exit code in the alert's `detail.recovery_outcome.replay_exit_code` — 3 = fill_processing_error per `scripts/operator_tools/replay_executions.py` docstring |
| Replay subprocess timeout (exit 124 / `timed_out=True`) | ib_gateway wedged (drill 6 pattern) | Check `docker compose ps ib_gateway` + autoheal logs; the synchronous restart via autoheal should self-heal within ~5.5min |

### Operator runbook reference

For the full lineage + design rationale:
- `Docs/decisions-log.md` 2026-05-18 drill 5 retrospective (the
  original incident; `/tmp/drill5_recovery.py` lineage)
- `Docs/decisions-log.md` 2026-05-25 drill 6 retrospective (the
  gateway-stuck-state pattern; autoheal sidecar context)
- `Docs/agentic-patterns.md` Pattern 6 — VPS cron / systemd timers
  is the 24/7 floor
- `scripts/operator_tools/replay_executions.py` — the recovery tool
  the agent invokes (clientId=99, fill_processor lineage)
- `scripts/operator_tools/recovery_agent.py` — the agent itself
  (classification logic, exit code contract, A-gates)
