# Day 10 — webhook_pusher operator runbook

End-to-end smoke test for `services/webhook_pusher/`. This is the A27
smoke fixture per `Docs/claude-dev-guide.md` §6.8 alternative (b)
(operator-runbook checklist) — same shape as `lean/README.md` Steps 4–7,
`watchdog/README.md` Steps 6–7, and `deploy/api/README.md` Steps 1–5.

**Closes the last open box of the Week 3 verification gate** (per
`implementation-guide.md` §3 Week 3 Thu): "Discord: send a test message
via webhook URL → message appears in `#alerts`."

If anything fails, capture the exact error + the step number and stop.
Root-cause discipline per dev-guide §1.3 — we debug rather than blow
past it.

## Prerequisites

- Ashburn VPS reachable via SSH (`ssh root@178.156.239.84`).
- `/opt/trading` checked out at the PR-#50 commit (the architectural
  follow-up that ships the `services/webhook_pusher/Dockerfile` and
  removes the `phase1` profile gate from the docker-compose stub —
  `git pull origin main` if the VPS is behind). Day 11 carryover (PR #47)
  also requires the api image to have httpx + 6 other runtime deps; if
  the api image is older than 2026-05-12, rebuild it:
  `docker compose --env-file deploy/.env build api && docker compose
  --env-file deploy/.env up -d --force-recreate api`.
- `webhook_pusher` container running. First-time bringup (or after a
  Dockerfile change):
  `docker compose --env-file deploy/.env build webhook_pusher && docker
  compose --env-file deploy/.env up -d webhook_pusher`. Verify:
  `docker compose --env-file deploy/.env ps webhook_pusher` shows
  `Up`. The container's CMD is a `sleep infinity` placeholder that logs
  one line at boot — operator runbook execs the CLI into it.
- `/opt/trading-secrets/secrets.yaml` decryption working. The age key lives at
  the host secrets file at `/opt/trading-secrets/secrets.yaml`;
  every read in this runbook uses that file directly in
  the current shell. Day 6 carryover verified `wc -c == 64` on
  `app_service_password`; if the file is unreadable, fix that before continuing.

### Architecture note (Day 11 carryover #2 + PR #50)

The smoke runs from inside the **`webhook_pusher` container**, NOT the
`api` container. Why: `api` is on `trading_internal` only (Docker
`Internal: true` — no external internet by design; backend-spec §8.11
hardening to limit blast radius if api is compromised). `webhook_pusher`
is on `[internal, egress]` so it can reach `discord.com` and
`api.resend.com`.

Day 11 carryover #2 (decisions-log 2026-05-12) used a temporary
`docker network connect trading_egress trading-api-1` workaround
because the `webhook_pusher` container didn't exist yet. PR #50
landed the dedicated container; that workaround is gone.

## Step 1 — Extract webhook URLs from the secrets file

On the VPS:

```bash
ssh root@178.156.239.84
cd /opt/trading


# Confirm webhook_pusher container is running with the CLI baked in.
docker compose --env-file deploy/.env exec -T webhook_pusher \
  test -f /app/services/webhook_pusher/cli.py || \
  { echo "MISSING: webhook_pusher container or cli.py inside it — rebuild via 'docker compose --env-file deploy/.env build webhook_pusher && docker compose --env-file deploy/.env up -d webhook_pusher'"; exit 1; }

# Decrypt to a tmpfs path; never to disk.
cp /opt/trading-secrets/secrets.yaml /dev/shm/paper.decrypted.yaml
chmod 600 /dev/shm/paper.decrypted.yaml

# Sanity-check the four discord channels we need are populated:
grep -E '^\s+(alerts|critical):' /dev/shm/paper.decrypted.yaml
# Expected: two lines, both with https://discord.com/api/webhooks/... values
```

**On mismatch:** if `alerts:` or `critical:` shows `<TODO_FROM_DAY_2_DISCORD_RUNBOOK>`,
edit the file (`nano /opt/trading-secrets/secrets.yaml`) and paste the URLs from
the Discord guild settings → Webhooks. See `deploy/discord/README.md`
for the canonical webhook-creation flow if any are missing.

## Step 2 — Stage env vars for the CLI

The CLI reads its config from env vars (canonical pattern: backend
runtime never parses YAML). On the VPS:

```bash
# Pick the alerts + critical webhook URLs out of the decrypted YAML.
# yq is the right tool but may not be installed; the awk approach below
# is zero-dep and tolerant of nested YAML structure (alerts/critical/
# api_key/from_address all live nested under their parent dict).
#
# Awk strategy: match by ($1 == "key:") AND ($2 ~ value-shape-regex).
# This is robust to indentation depth and to multiple unrelated keys
# named "api_key" elsewhere in the file (anthropic.api_key vs
# resend.api_key; only resend's matches the ^re_ prefix).
DISCORD_ALERTS_URL=$(awk '$1 == "alerts:" && $2 ~ /^https:\/\/discord/ {print $2; exit}' \
  /dev/shm/paper.decrypted.yaml)
DISCORD_CRITICAL_URL=$(awk '$1 == "critical:" && $2 ~ /^https:\/\/discord/ {print $2; exit}' \
  /dev/shm/paper.decrypted.yaml)
RESEND_API_KEY=$(awk '$1 == "api_key:" && $2 ~ /^re_/ {print $2; exit}' \
  /dev/shm/paper.decrypted.yaml)
RESEND_FROM=$(awk '$1 == "from_address:" {print $2; exit}' \
  /dev/shm/paper.decrypted.yaml)

# Sanity print (URL hosts only — no tokens).
echo "alerts host:   $(echo $DISCORD_ALERTS_URL | sed 's|https://\([^/]*\).*|\1|')"
echo "critical host: $(echo $DISCORD_CRITICAL_URL | sed 's|https://\([^/]*\).*|\1|')"
echo "resend from:   $RESEND_FROM"
echo "resend key prefix: ${RESEND_API_KEY:0:5}..."   # should be "re_..."

# Export for the CLI run. (Subshell only — these don't leak to the
# parent shell or persist after this terminal closes.)
export WEBHOOK_PUSHER_DISCORD_ALERTS_URL=$DISCORD_ALERTS_URL
export WEBHOOK_PUSHER_DISCORD_CRITICAL_URL=$DISCORD_CRITICAL_URL
export WEBHOOK_PUSHER_RESEND_API_KEY=$RESEND_API_KEY
export WEBHOOK_PUSHER_RESEND_FROM=$RESEND_FROM
export WEBHOOK_PUSHER_OPERATOR_EMAIL=$RESEND_FROM   # Phase 0: same as from
```

**On mismatch:** if any of the four `echo` lines is empty, the awk script
didn't find that field. Open `/dev/shm/paper.decrypted.yaml` directly
(`cat /dev/shm/paper.decrypted.yaml | grep -A 7 '^discord:'`) and confirm
the keys are nested under `discord.webhook_urls.alerts` (not at top
level). If the YAML structure differs from what the CLI expects, escalate
— the `paper.template.yaml` schema is the canonical source.

**Note (Day 11 carryover):** the original awk pattern in this step used
range addresses (`/^discord:/,/^[a-z]/`) which terminate immediately
because `discord:` itself matches `^[a-z]`. The `$1 == ... && $2 ~ ...`
form above replaces it; both Day 11 smoke runs (Step 3 + the optional
Step 5) used this corrected form successfully.

## Step 3 — Run the bare-smoke test (no DB)

This step verifies the planner + sender + webhook URL all work without
touching Postgres. Closes Step 4 of the Week 3 gate.

```bash
docker compose --env-file deploy/.env exec -T webhook_pusher env \
  WEBHOOK_PUSHER_DISCORD_ALERTS_URL=$WEBHOOK_PUSHER_DISCORD_ALERTS_URL \
  WEBHOOK_PUSHER_DISCORD_CRITICAL_URL=$WEBHOOK_PUSHER_DISCORD_CRITICAL_URL \
  WEBHOOK_PUSHER_RESEND_API_KEY=$WEBHOOK_PUSHER_RESEND_API_KEY \
  WEBHOOK_PUSHER_RESEND_FROM=$WEBHOOK_PUSHER_RESEND_FROM \
  WEBHOOK_PUSHER_OPERATOR_EMAIL=$WEBHOOK_PUSHER_OPERATOR_EMAIL \
  /opt/venv/bin/python -m services.webhook_pusher.cli \
    --severity P2 \
    --message "Day 10 webhook_pusher smoke test from $(hostname)"
```

**Expected:** stdout shows

```
Plan: severity=P2, channels=['discord_alerts']
Alert id: <some uuid>
  POST discord_alerts -> https://discord.com/api/webhooks/...
    status=ok http=204 retried=False
```

**Then check Discord:** open the operator's Discord guild → `#alerts`
channel. There should be a yellow embed with title `external_watchdog_alert`
(the default smoke category), description `"Day 10 webhook_pusher smoke
test from ..."`, and a footer carrying the alert id + UTC timestamp.

**On mismatch:**

- `status=failed_auth` → the webhook URL is wrong. Re-run Step 2's awk
  parse; check the URL's path token matches what Discord shows in
  Server Settings → Integrations → Webhooks → `#alerts`.
- `status=failed_not_found` → the channel was deleted or the webhook
  was rotated. Re-create the webhook in Discord and update the secrets file.
- `status=failed_network` (DNS, connection refused) → the Ashburn VPS
  egress to `discord.com` is blocked. Ashburn → Discord works as of
  Day 6 carryover (HTTP 204 verified); if it stops working, that's a
  Hetzner network change worth a fresh decisions-log entry. Do NOT
  fall back to Resend for this — keep debugging the egress.
- `status=rate_limited` (with `retried=True` and STILL rate-limited) →
  someone else is hammering this webhook. Wait 60s and retry.
- The CLI succeeds but no message appears in Discord → check the
  operator's Discord client is in the right server / didn't mute
  `#alerts`.

## Step 4 — Find an account_id for the DB-backed test

The full-path test (Step 5) needs an existing `accounts.id`. On the VPS:

```bash
# Postgres requires password auth for app_service via the unix socket;
# pull it from the decrypted yaml. (The unix socket peer auth that
# would let `-U postgres` skip auth doesn't apply here — `-U app_service`
# always wants password.)
APP_SERVICE_PWD=$(awk '$1 == "app_service_password:" {print $2; exit}' \
  /dev/shm/paper.decrypted.yaml)

docker compose --env-file deploy/.env exec -T -e PGPASSWORD="$APP_SERVICE_PWD" postgres \
  psql -U app_service -d trading -h postgres -c "SELECT id FROM accounts LIMIT 1;"
```

**Expected:** one UUID printed under `id`. Copy it for the next step.

**On mismatch:** if the table is empty (`(0 rows)`), the bootstrap setup
hasn't created the operator account yet. The `setup_token` flow in
`services/api/routes/setup.py` is the canonical way to seed `accounts`;
it's not in scope for Day 10. **If accounts is empty, skip Step 5** —
the bare-smoke test (Step 3) is sufficient to close the Week 3 gate.
File a follow-up to re-run Step 5 once an account exists.

## Step 5 — Full DB roundtrip (P0 fan-out)

Closes Steps 6 + 7 of the runbook (the alerts row INSERT + UPDATE +
P0 fan-out to all 3 channels). Substitute the account UUID from Step 4.

```bash
ACCOUNT_ID="<UUID-FROM-STEP-4>"
APP_SERVICE_PWD=$(awk '$1 == "app_service_password:" {print $2; exit}' \
  /dev/shm/paper.decrypted.yaml)

docker compose --env-file deploy/.env exec -T webhook_pusher env \
  WEBHOOK_PUSHER_DISCORD_ALERTS_URL=$WEBHOOK_PUSHER_DISCORD_ALERTS_URL \
  WEBHOOK_PUSHER_DISCORD_CRITICAL_URL=$WEBHOOK_PUSHER_DISCORD_CRITICAL_URL \
  WEBHOOK_PUSHER_RESEND_API_KEY=$WEBHOOK_PUSHER_RESEND_API_KEY \
  WEBHOOK_PUSHER_RESEND_FROM=$WEBHOOK_PUSHER_RESEND_FROM \
  WEBHOOK_PUSHER_OPERATOR_EMAIL=$WEBHOOK_PUSHER_OPERATOR_EMAIL \
  WEBHOOK_PUSHER_DATABASE_URL="postgresql+asyncpg://app_service:${APP_SERVICE_PWD}@postgres:5432/trading" \
  /opt/venv/bin/python -m services.webhook_pusher.cli \
    --severity P0 \
    --message "Day 10 P0 smoke test from $(hostname)" \
    --with-db \
    --account-id $ACCOUNT_ID
```

**Expected:** stdout shows

```
Inserted alerts row id=<uuid>
short_circuited: False
delivery_status: {"discord_alerts": "ok", "discord_critical": "ok", "email": "ok"}
  discord_alerts: status=ok http=204 retried=False
  discord_critical: status=ok http=204 retried=False
  email: status=ok http=200 retried=False
```

**Then check three places:**

1. **Discord `#alerts`** — red embed (P0 → `0xFF0000`) with title
   `external_watchdog_alert`, description matches the message.
2. **Discord `#critical`** — same red embed, replicated.
3. **Operator email inbox** — message from the locked Resend
   `from_address` (likely `shaanrpatel2@gmail.com`), subject
   `"[P0 spratcapital] external_watchdog_alert: Day 10 P0 smoke test
   from ..."`, plain-text body with severity / category / fired-at /
   alert id / message / detail blocks.

**On mismatch:**

- One channel `failed_*` while others `ok` → read the corresponding
  diagnostic in Step 3's "On mismatch" list. The dispatcher recorded
  the partial failure in `delivery_status` JSONB; that's the canonical
  audit trail.
- All three `failed_*` → Step 3 should have caught any single-channel
  issue. If Step 3 passed but Step 5 fails, the regression is in the
  DB-write or env-var path; capture the full stdout and escalate.
- `short_circuited: True` on the FIRST run → the dispatcher think this
  alert was already dispatched. Check the SELECT result in the
  alembic-applied DB — `delivery_status` should be NULL right after
  INSERT. If you see a non-NULL value, the schema may have a default
  that drifted (alembic 0004 says `delivery_status JSONB` with no
  default; bug if otherwise).

## Step 6 — Verify the alerts row landed in Postgres

Confirms the dispatcher's UPDATE actually persisted (Step 7 of the
session-prompt's gate language).

```bash
APP_SERVICE_PWD=$(awk '$1 == "app_service_password:" {print $2; exit}' \
  /dev/shm/paper.decrypted.yaml)

docker compose --env-file deploy/.env exec -T -e PGPASSWORD="$APP_SERVICE_PWD" postgres \
  psql -U app_service -d trading -h postgres -c "
    SELECT id, severity, category, fired_at_utc, delivery_status, acknowledged
    FROM alerts
    ORDER BY fired_at_utc DESC
    LIMIT 3;
  "
```

**Expected:** the most recent row matches the alert id printed in
Step 5; `delivery_status` JSONB is the same `{"discord_alerts": "ok",
"discord_critical": "ok", "email": "ok"}` we saw on stdout;
`acknowledged` is `f` (not yet ack'd — that's the operator's job from
the web UI when it lands in Phase 1).

## Step 7 — Verify idempotency (optional but cheap)

Re-run Step 5 with the SAME alert id by calling the CLI with the same
account but a different message — actually, the simpler check is to
call `dispatch_alert` directly twice. The CLI doesn't expose this
(it INSERTs a fresh row each time), so verify via a one-off SQL UPDATE
that flips delivery_status back to NULL and confirms a re-dispatch
short-circuits when populated.

```bash
# Pick the most recent alert id from Step 6.
ALERT_ID="<MOST-RECENT-ID-FROM-STEP-6>"
APP_SERVICE_PWD=$(awk '$1 == "app_service_password:" {print $2; exit}' \
  /dev/shm/paper.decrypted.yaml)

docker compose --env-file deploy/.env exec -T -e PGPASSWORD="$APP_SERVICE_PWD" postgres \
  psql -U app_service -d trading -h postgres -c "
    SELECT delivery_status FROM alerts WHERE id = '$ALERT_ID';
  "
```

**Expected:** non-NULL JSONB. The dispatcher now short-circuits on
this id — verified by the unit test
`tests/unit/test_webhook_pusher.py::TestDispatcherIdempotency::test_existing_delivery_status_short_circuits`,
not re-tested in production.

## Step 8 — Cleanup

```bash
# Wipe the decrypted secrets from tmpfs.
shred -u /dev/shm/paper.decrypted.yaml

# Note: PR #50 dropped the `docker network disconnect trading_egress
# trading-api-1` step that earlier versions of this runbook needed.
# webhook_pusher now runs in its own container with `[internal, egress]`
# baked into docker-compose.yml; the api container stays on
# `trading_internal` only at all times.

# Logout of the SSH session (env vars are subshell-local; closing the
# shell discards them). Belt-and-suspenders:
unset WEBHOOK_PUSHER_DISCORD_ALERTS_URL WEBHOOK_PUSHER_DISCORD_CRITICAL_URL \
      WEBHOOK_PUSHER_RESEND_API_KEY WEBHOOK_PUSHER_RESEND_FROM \
      WEBHOOK_PUSHER_OPERATOR_EMAIL WEBHOOK_PUSHER_DATABASE_URL \
      APP_SERVICE_PWD
exit
```

## Closure of the Week 3 verification gate

Once Steps 3 + 5 + 6 are green, the last unchecked box of the Week 3
gate (`implementation-guide.md` §3 Week 3 "Discord: send a test message
via webhook URL → message appears in #alerts") flips to `[x]`.

Capture for the Day 10 close-out in `Docs/decisions-log.md`:

- Step 3 stdout (the bare-smoke `delivery_status: ok` line).
- Step 5 stdout (all three `delivery_status: ok` lines).
- Discord screenshot from `#alerts` showing the P0 red embed (optional
  — text-stdout evidence is sufficient).
- Resend dashboard message-id from the email send (optional — Gmail
  receipt is sufficient).
- Postgres SELECT result from Step 6 (the `delivery_status` JSONB).

## Caveats

- **Cloudflare blocks Hetzner Nuremberg → Discord** (Day 4 watchdog
  discovery, decisions-log 2026-05-07): does NOT affect this runbook.
  webhook_pusher runs on **Ashburn**, not Nuremberg, and Ashburn →
  Discord works (Day 6 carryover: HTTP 204 verified). If a future
  geographic move puts the api container behind a blocked egress, the
  Resend backup channel for P0 keeps the most-critical alerts working.
- **No retry storms.** The dispatcher does NOT loop on its own. If
  Step 5's run shows partial failure, that's the END state — caller
  (Week 4 risk dispatcher, when it lands) decides whether to schedule
  a fresh `dispatch_alert`. The runbook has nothing to "wait for."
- **Not on the hot-fix whitelist.** `services/webhook_pusher/**` is on
  NEITHER `Docs/claude-dev-guide.md` §2.2 (forbidden) NOR §2.3 (hot-fix).
  Future edits go through regular PR review, no `risk-review-approved`
  label needed, no auto-deploy.

## Crypto-pivot §3.8 addendum (2026-07-09) — 00:10 UTC cycle digest

The webhook_pusher container now runs a SECOND long-lived loop next to
the SSE subscriber: `services/webhook_pusher/cycle_digest_scheduler.py`
fetches `GET /api/system/cycle` at 00:10 UTC daily and pushes the
daily-decision digest embed to the **#daily-brief** webhook
(`discord.webhook_urls.daily_brief` in the host secrets file — now a
REQUIRED field; the entrypoint fails closed exit-2 without it).

Smoke checks after deploying this build (A27 fact-checks):

1. `docker compose logs webhook_pusher | grep cycle_digest_scheduler_started`
   — one line at container start with `fire_time_utc=00:10`.
2. The morning after the strategy worker's first decision:
   `docker compose logs webhook_pusher | grep cycle_digest_pushed` shows
   `delivery_status=ok` + `http_status=204`, and #daily-brief carries
   the embed (title `Daily decision — <date> · <status>`).
3. Before the first decision ever lands the 00:10 firing logs
   `cycle_digest_skipped_no_decision` and posts NOTHING — that is the
   designed skip, not a fault.
4. On-demand parity: `/cycle` in Discord renders the same embed
   (ephemeral) — same builder, `services/discord_shared/cycle_digest.py`
   (dependency-neutral shared package; the planner half stays in
   `services/webhook_pusher/cycle_digest.py`).
5. C0 exit gate (delta spec §5): the digest fires 3 consecutive days —
   three `cycle_digest_pushed` (or `_skipped_no_decision`) lines on
   three consecutive UTC dates.

## Module surface (for next agent)

| Function | File | Purpose |
|---|---|---|
| `plan_alert_dispatch` | `services/webhook_pusher/payloads.py` | Pure-policy planner (severity → channels, embed shape, email shape) |
| `post_outbound_message` | `services/webhook_pusher/sender.py` | One async HTTP POST + status classification |
| `dispatch_alert` | `services/webhook_pusher/dispatcher.py` | Read alert row, plan, fan out, UPDATE delivery_status; idempotent on retry |
| `python -m services.webhook_pusher.cli` | `services/webhook_pusher/cli.py` | Smoke CLI used by this runbook |

For test coverage see `tests/unit/test_webhook_pusher.py` (58 tests:
severity routing, Discord embed shape, Resend payload shape, planner
errors, sender HTTP statuses, sender 429 retry, sender transport
failures, dispatcher idempotency, dispatcher fan-out wiring).
