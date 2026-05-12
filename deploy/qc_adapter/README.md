# Day 28 — qc_adapter operator runbook

End-to-end smoke + deploy procedure for `services/qc_adapter/`. This is
the A27 smoke fixture per `Docs/claude-dev-guide.md` §6.8 alternative (b)
(operator-runbook checklist) — same shape as
`deploy/webhook_pusher/README.md`, `deploy/audit/README.md`, and
`deploy/discord_bot/README.md`.

The qc_adapter is the first piece of the Phase-1 pipeline shipped Day 28
(Week 7 Thu). It runs a 3-directory poll loop against QC ObjectStore at
60s/60s/5s cadences (per backend-spec §3.19), ingests `signal_emitted`
events into `audit_log` + `signals`, and routes malformed records to
`data_quality_events`. PR-B/C/D extend the orchestrator to handle
`order_*` + `fill` + reconciliation events.

If anything fails, capture the exact error + the step number and stop.
Root-cause discipline per dev-guide §1.3 — we debug rather than blow past
it.

## Prerequisites

- Ashburn VPS reachable via SSH (`ssh root@178.156.239.84`).
- `/opt/trading` checked out at the PR-A commit (the Phase-1-onset
  pipeline build pt 1/5 — `git pull origin main` if the VPS is behind).
- `secrets/paper.enc.yaml` contains the two QuantConnect fields:
  - `quantconnect.user_id` — the numeric QC **User ID** from the
    QC account page (avatar → My Account) → mapped to `QC_ADAPTER_QC_USER_ID`
    env var. NOT the organization slug (hex string).
  - `quantconnect.api_token` — QC API token (rotate via QC dashboard if
    suspected leaked) → mapped to `QC_ADAPTER_QC_API_TOKEN`.
- `secrets/paper.enc.yaml` decryption working: the age key lives at
  `/etc/credstore.encrypted/age_key` per `deploy/.env`'s
  `SOPS_AGE_KEY_FILE`. Day 6 carryover verified `wc -c == 64` on
  `app_service_password`; if `sops -d` errors, fix that before
  continuing.
- An `accounts` row with `active_to IS NULL` exists. Operator confirms
  via:
  ```sh
  docker compose --env-file deploy/.env exec -T postgres \
    psql -U app_owner -d trading -c \
    "SELECT id, external_account_id FROM accounts WHERE active_to IS NULL;"
  ```
  If empty, run the api setup wizard (`https://spratcapital.com/setup`)
  to bootstrap before continuing.

### Architecture note

- Networks: `internal` (Postgres writes via `app_service` role) +
  `egress` (outbound TLS to `https://www.quantconnect.com/api/v2`).
- Auth model: HTTP Basic with username = QC user_id, password =
  `sha256("{api_token}:{timestamp}")` hex-digested. Both credentials
  flow in via the sops bundle volume; the container's entrypoint maps
  them to `QC_ADAPTER_QC_USER_ID` + `QC_ADAPTER_QC_API_TOKEN`.
- The orchestrator runs three independent directory loops in parallel
  via `asyncio.gather` — a failure on one directory doesn't affect the
  other two. Continuous-failure escalation matches backend-spec §6.6.1:
  log → P1 alert at 5 min → HALT_NEW at 10 min (HALT trigger lands in
  PR-B+ via `services.risk.dispatch.apply_state_transition`).

## Step 1 — Verify sops contents

On the VPS:

```bash
ssh root@178.156.239.84
cd /opt/trading

export SOPS_AGE_KEY_FILE=/etc/credstore.encrypted/age_key

# Decrypt to a tmpfs path; never to disk.
sops --decrypt secrets/paper.enc.yaml > /dev/shm/paper.decrypted.yaml
chmod 600 /dev/shm/paper.decrypted.yaml

# Both QC fields must be filled (NOT <TODO_…> placeholders).
grep -E '^\s+(user_id|api_token):' /dev/shm/paper.decrypted.yaml \
  | sed 's/api_token:.*/api_token: [REDACTED]/'
# Expected: two lines, user_id with a numeric value, api_token
# present (redacted by this grep).

# Clean up immediately if either is missing.
test -s /dev/shm/paper.decrypted.yaml && \
  grep -q '<TODO' /dev/shm/paper.decrypted.yaml && \
  { echo "FAIL: TODO placeholders still present in paper.enc.yaml — fill them via 'sops secrets/paper.enc.yaml' before continuing"; rm -f /dev/shm/paper.decrypted.yaml; exit 1; }
```

**On mismatch:** if either field shows `<TODO_FROM_DAY_3_QC_TOKEN_REGEN>`
or `<TODO_FROM_DAY_3_QC_ORG>`, edit the encrypted file with `sops
secrets/paper.enc.yaml`, paste the values from the QC account page, save
+ exit (sops re-encrypts on close), commit + push, redeploy.

## Step 2 — Build the container

```bash
# Builds `ghcr.io/${GHCR_OWNER}/trading-qc-adapter:latest`.
# Expected wall-time: ~30-45s (the Dockerfile import sanity-check at the
# end of the builder stage takes a few seconds).
time docker compose --env-file deploy/.env build qc_adapter
```

**On error:** if the import sanity check at the end of the builder
fails with `ModuleNotFoundError`, see the per-message tip in the build
log — typically a missing pip dep in the `Dockerfile` `pip install`
list. Compare the import list at the bottom of the Dockerfile builder
stage against the actual import surface of
`services/qc_adapter/main.py`.

## Step 3 — Bring up the container

```bash
docker compose --env-file deploy/.env up -d qc_adapter

# Watch the boot logs. Expect within 5-10s:
#   qc_adapter_booting environment=paper ...
#   qc_adapter_account_resolved account_id=<uuid>
#   qc_adapter_orchestrator_started service_name=qc_adapter ...
docker compose --env-file deploy/.env logs -f --tail=50 qc_adapter
# Ctrl-C when you see orchestrator_started.
```

**If the container restarts in a loop:** check
`docker compose logs qc_adapter` for one of these exit codes:
  - `2` → fail-closed at entrypoint (sops field missing/placeholder).
    Fix in Step 1.
  - `3` → `qc_adapter_no_active_account`. Bootstrap an accounts row
    first via the api setup wizard.
  - `4` → uncaught exception in run_forever. Capture the stack trace.

## Step 4 — Cycle smoke

Wait one full cadence (~60s for `/events/`) and then look for the
`orchestrator_cycle_completed` event:

```bash
docker compose --env-file deploy/.env logs --since 90s qc_adapter \
  | grep -E 'orchestrator_cycle_completed|qc_no_new_keys|qc_fetched_keys'
```

**Expected output (no signals emitted yet by QC algo):**
```
orchestrator_cycle_completed directory=/events/ fetched=0 events_ingested=0 ...
qc_no_new_keys ... last_consumed_sequence_no=0
```

**On 401/403 from QC:** the auth pair is wrong. Re-check that
`quantconnect.user_id` is the numeric QC User ID (NOT the organization
slug/hex string) — find it via avatar → My Account on QC's site.
DP-023 (Day 28 carryover) was exactly this: the sops field was filled
with the organization slug and QC returned "UserID not valid".

## Step 5 — End-to-end paper round-trip (when QC algo emits)

This step lands at Day 28 PR-E once the QC algorithm is wired to emit
real `signal_emitted` events. Expected sequence:

1. QC algorithm fires its 17:30 ET daily cycle.
2. Within 60s, qc_adapter's structlog shows
   `qc_signal_emitted_audit_appended` + `qc_signal_emitted_row_inserted`.
3. `audit_log` row count grows by one per signal; `signals` table
   carries a `pending` row.
4. PR-E's Discord embed lands in `#signals` with an Approve button.

Verify in real-time:

```bash
# Continuous log tail focused on signal_emitted ingestion.
docker compose --env-file deploy/.env logs -f --tail=0 qc_adapter \
  | grep -E 'qc_signal_emitted|orchestrator_cycle_completed'
```

```sh
# Postgres-side: row count + the chain still walks clean.
docker compose --env-file deploy/.env exec -T postgres psql -U app_owner \
  -d trading -c "SELECT COUNT(*) FROM signals WHERE status='pending';"
```

## Step 6 — verify_chain still passes

Audit-chain integrity end-to-end after qc_adapter writes start landing:

```bash
# Follow deploy/audit/README.md Steps 1-3 verbatim (it's already a
# stable runbook). Expected output: CHAIN OK: <N> rows verified.
```

## Step 7 — Cleanup

```bash
# Always shred the decrypted secrets file when done.
shred -u /dev/shm/paper.decrypted.yaml
exit  # SSH session
```

## Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| Container won't start, exit 2 | Missing sops field | Step 1 |
| Container exits 3 | No active account row | Run `/setup` wizard |
| `QCObjectStoreAuthError` on every cycle | Wrong user_id or expired token | Rotate via QC dashboard + sops |
| `qc_no_new_keys` forever | QC algo not writing to `events/` yet | Expected pre-Day-28-PR-E |
| `consecutive_failures` grows past 5 | QC platform outage or auth break | Check QC status page; alert P1 lands at 5 min in PR-B+ |
| Chain break on `verify_chain` | Catastrophic — incident_review per backend-spec §6.6.3 | Capture full audit_log; do NOT continue |

## Token rotation

QC API tokens rotate via the QC account dashboard. Procedure:

1. Generate new token on QC; immediately:
2. `sops secrets/paper.enc.yaml` → update `quantconnect.api_token` →
   save + exit (sops re-encrypts).
3. `git add secrets/paper.enc.yaml && git commit -m "chore(secrets): rotate QC token"`
4. `git push origin main`
5. On VPS: `git pull --ff-only && docker compose --env-file deploy/.env up -d --force-recreate qc_adapter`
6. Revoke the old token in the QC dashboard.

The container picks up the new token at next boot; the in-flight cycle
finishes with the old token, then subsequent cycles use the new one.
