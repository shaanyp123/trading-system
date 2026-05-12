# `deploy/lean_local/` — operator runbook for LEAN Local

Pivot-PR-A (post-pivot 2026-05-12). A27 satisfier per dev-guide §6.8
alternative (b) — operator-runbook with N concrete fact-checks against
the real LEAN runtime + the real backend `/api/internal/lean/signals`
endpoint.

This runbook walks the operator through wiring + smoke-testing the
`lean_local` Docker container against a freshly-deployed backend. The
ceremony at the end of this runbook is the canonical "LEAN Local is alive
and authenticated against the backend" precondition for the Pivot-PR-D
signal-dispatch wiring to begin.

**Prereqs:**

* Backend deployed at `a27884a` (Day 28 carryover state) or later, with
  Pivot-PR-A merged + the api container rebuilt with the LeanAuthMiddleware
  + `/api/internal/lean/signals` route.
* sops decryption working on the VPS (`/etc/credstore.encrypted/age_key`
  configured per the Day 5 carryover pattern).
* `services/api/entrypoint.py` reads `lean.api_bearer_token` from sops →
  exports `API_LEAN_LOCAL_BEARER_TOKEN`.
* SSH access to the operator's VPS at the standard `trading@<host>` path.

---

## Step 1 — Generate the LEAN bearer + add to sops

Generate a fresh 32-byte URL-safe base64 token on the operator's
workstation:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Edit the paper-env sops bundle:

```bash
sops secrets/paper.enc.yaml
```

Add (or replace the placeholder) under the `lean:` section:

```yaml
lean:
  api_bearer_token: <paste-the-32-byte-base64-value>
```

Save + exit. sops re-encrypts on save. Commit + push:

```bash
git add secrets/paper.enc.yaml
git commit -m "ops: paper env adds lean.api_bearer_token for Pivot-PR-A"
git push
```

Repeat for `secrets/live.enc.yaml` if you're going straight to live
(usually you don't — paper smoke first).

> **Why a separate bearer (vs reusing `discord.api_bearer_token`):** so
> a compromise of one container can't grant access via the other.
> Defense-in-depth at the service-account boundary.

---

## Step 2 — Re-decrypt the sops bundle on the VPS

The VPS has a pre-decrypted snapshot at `/opt/trading/secrets-decrypted/decrypted.yaml`
mounted into containers as `/run/secrets/decrypted.yaml`. After editing
the encrypted file, the on-disk snapshot is stale until you re-decrypt.
This is **DP-026** from the Day 28 carryover — the deploy script does
not auto-re-decrypt on sops edits.

On the VPS:

```bash
ssh trading@<vps-host>
cd /opt/trading
git pull --ff-only
sops -d secrets/paper.enc.yaml > /opt/trading/secrets-decrypted/decrypted.yaml.tmp \
  && mv /opt/trading/secrets-decrypted/decrypted.yaml.tmp /opt/trading/secrets-decrypted/decrypted.yaml \
  && chmod 600 /opt/trading/secrets-decrypted/decrypted.yaml
```

Verify the new field landed:

```bash
grep -A1 'lean:' /opt/trading/secrets-decrypted/decrypted.yaml
```

You should see `api_bearer_token: <your-32-byte-string>` (NOT a `<TODO...>`
placeholder).

---

## Step 3 — Build + restart api with the LEAN bearer env var

The api needs to re-read `decrypted.yaml` at boot to pick up the new
`API_LEAN_LOCAL_BEARER_TOKEN` env var. Either restart it cleanly or
rebuild.

```bash
docker compose --env-file deploy/.env restart api
docker compose --env-file deploy/.env logs api 2>&1 | tail -20
```

Look for the standard api startup lines:

```
api_starting environment=paper version=<sha>
api_ready
```

Verify the bearer env var landed:

```bash
docker compose exec api env | grep -i lean
```

You should see `API_LEAN_LOCAL_BEARER_TOKEN=<your-bearer>`. If you see
nothing, the entrypoint mapping didn't fire — verify the sops field
name is `lean.api_bearer_token` (not `lean_local.api_bearer_token` or
similar).

---

## Step 4 — Build + start the lean_local container

```bash
docker compose --env-file deploy/.env build lean_local
docker compose --env-file deploy/.env up -d lean_local
docker compose --env-file deploy/.env logs lean_local 2>&1 | tail -50
```

Expected boot sequence in the logs (from `infrastructure/lean_local/entrypoint.sh`
+ LEAN's own logs):

```
[lean_local_entrypoint] api_base=http://api:8000 live_mode=false env=paper
[lean_local_entrypoint] launching: dotnet /Lean/QuantConnect.Lean.Launcher.dll
... LEAN startup ...
Initialize: v1_strategy initialized (post-pivot 2026-05-12) live_mode=False api_base=http://api:8000 ...
```

Then the heartbeat POST should land on the backend. On the api side:

```bash
docker compose --env-file deploy/.env logs api 2>&1 | grep lean_event_received
```

Expected log line (one per backend cycle that LEAN's `_post_event` fired,
including the initialize heartbeat):

```
lean_event_received event_type=lean_strategy_initialized algorithm_id=v1_trend_following ...
```

If you see `lean_event_received` lines, **LEAN Local is alive +
authenticated against the backend**. Proceed.

If you see `lean_auth_required` or `bot_auth_invalid_token` lines on the
api side, the bearer in `decrypted.yaml` does not match what the api is
expecting. Re-run Step 2 + Step 3.

---

## Step 5 — Confirm 17:30 ET cycle heartbeats land

LEAN's `_on_daily_signal_cycle` fires at 17:30 ET wall-clock daily.
After the next cycle (could be up to 24h depending on when you deployed):

```bash
docker compose --env-file deploy/.env logs api --since 24h 2>&1 | grep 'event_type=lean_cycle_heartbeat'
```

Expected: at least one log line with `event_type=lean_cycle_heartbeat`
containing `session_date_et` + `equity_usd` + `live_mode` fields.

If the cycle didn't fire after 24h, check LEAN's logs for warmup status
— with `MA_SLOW_DAYS=200`, the algorithm warms up for 200 daily bars
before `is_warming_up` flips to False. In backtest mode warmup is
instant; in live mode it waits real-time, so the first cycle may take
~10 calendar days from a fresh container.

---

## Step 6 — Audit chain integrity check

The Pivot-PR-A scope does NOT write audit_log rows (heartbeats are
operational, not audit-relevant). But run `verify_chain` to confirm
the existing chain still walks cleanly after the new code paths
deployed.

```bash
docker compose --env-file deploy/.env exec api /opt/venv/bin/python -m services.audit.verify_chain --env paper
```

Expected: `CHAIN OK: 2 rows verified` (from Day 25 + Day 27 carryover
state). If this returns CHAIN BREAK, escalate per `deploy/audit/README.md`
incident-review procedure.

---

## Step 7 — Cleanup + record state

If everything is green, no further action needed. The `lean_local`
container will run continuously, emit a heartbeat per 17:30 ET cycle,
and stay alive across `docker compose restart` cycles.

Record the deploy state in the next session's `Docs/decisions-log.md`
carryover entry:

* Commit SHA the api was deployed at (`docker compose --env-file deploy/.env config | grep image`)
* First `lean_event_received` timestamp from api logs
* `verify_chain` row count

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `lean_local` exits with code 2 + log line `lean.api_bearer_token missing or empty` | Step 2 didn't refresh decrypted.yaml on the VPS | Re-run Step 2 on the VPS; verify the field with `grep` |
| `lean_local` exits with code 2 + log line `lean.api_bearer_token still has placeholder value '<TODO...>'` | sops template field is unedited | Re-run Step 1 with a real token |
| `docker compose up -d lean_local` fails with `image quantconnect/lean:latest not found` | Image hasn't been pulled yet | `docker compose pull lean_local` then `up -d` |
| api logs show `lean_auth_required` on `/api/internal/lean/signals` POSTs | LeanAuthMiddleware rejected the bearer | Compare `decrypted.yaml::lean.api_bearer_token` to `docker compose exec api env | grep LEAN`. They must match byte-for-byte. |
| api logs show `lean_event_signal_emitted_not_wired` warnings | LEAN emitted a `signal_emitted` event but the endpoint is Pivot-PR-A scope (heartbeat only) | Expected — this is the future-PR scope. Pivot-PR-D wires this. Until then, LEAN should emit only `lean_strategy_initialized` + `lean_cycle_heartbeat`. |
| `verify_chain` returns CHAIN BREAK | Something unrelated wrote a malformed audit row; not LEAN's fault | Escalate per `deploy/audit/README.md` incident-review procedure |
| LEAN container restart-loops with `dotnet` segfault | Insufficient RAM (LEAN's .NET runtime needs ~500MB) | Check `free -h` on the VPS; if RAM is exhausted by other containers, consider upgrading from CCX13 → CCX23 (~$10/mo step-up) |
| LEAN logs `Failed to load the algorithm. Cannot find class 'V1TrendFollowingAlgorithm' in file 'v1_strategy.py'` | The file rename from `v1_qc_algorithm.py` didn't take effect in the mounted volume | `docker compose down lean_local`; verify `ls /opt/trading/lean/v1_strategy.py` on VPS; if `v1_qc_algorithm.py` is still present, the git pull didn't apply (`git pull --ff-only` from VPS); re-up |
| LEAN logs `Connection to ib_gateway:4002 refused` | `ib_gateway` container not yet running (Pivot-PR-B not deployed) | Expected during Pivot-PR-A scope. LEAN runs in backtest mode (`LEAN_LIVE_MODE=false` default) so this should not happen unless someone flipped the env var early. Verify `docker compose --env-file deploy/.env config | grep LEAN_LIVE_MODE`. |

---

## Token rotation procedure

Per backend-spec §6.6 + dev-guide §1.5 (LOCKED), the LEAN bearer rotates
with the quarterly secrets rotation alongside the Discord bot bearer.

1. Generate new bearer (Step 1's command).
2. Update `secrets/paper.enc.yaml` AND `secrets/live.enc.yaml`.
3. Commit + push.
4. On VPS: re-decrypt sops bundle (Step 2).
5. Restart **api FIRST** (so the new bearer is in `API_LEAN_LOCAL_BEARER_TOKEN`).
6. Restart `lean_local` SECOND (so the new bearer is in `LEAN_LOCAL_BEARER_TOKEN`
   and matches what api expects).
7. Confirm Step 4's smoke (one `lean_event_received` line) within 60s of
   restart.
8. **Revoke the old bearer** — there is no separate "old bearer revoke"
   step because the api only checks against the currently-configured
   token via constant-time-compare. The instant the api restart in Step
   5 completes, the old bearer no longer authenticates.

---

## Cross-references

- Pivot-PR-A rationale: `Docs/decisions-log.md` 2026-05-12 entry.
- Backend-spec §2.3 (Signal Engine post-pivot): describes the LEAN → api
  signal flow at the architectural level.
- `services/api/middleware.py::LeanAuthMiddleware`: source of the
  bearer-check logic.
- `services/api/routes/internal/lean.py`: source of the
  `/api/internal/lean/signals` endpoint.
- `lean/v1_strategy.py`: the LEAN algorithm wrapper that POSTs to the
  endpoint.
- `infrastructure/lean_local/entrypoint.sh`: container entrypoint that
  resolves sops → env vars → LEAN config.
- `deploy/ibkr/README.md` (Pivot-PR-B): IBKR Gateway operator runbook for
  the `paper-ibkr` + `live-ibkr` LEAN environments.
