# `deploy/lean_local/` — operator runbook for LEAN Local

Pivot-PR-A (post-pivot 2026-05-12) baseline + **2026-05-21 data-layer
pivot v2 Option C** runbook. A27 satisfier per dev-guide §6.8
alternative (b) — operator-runbook with concrete fact-checks against
the real LEAN runtime + the real backend `/api/internal/lean/signals`
endpoint + the api-managed `lean_data` Docker volume.

This runbook walks the operator through wiring + smoke-testing the
`lean_local` Docker container under the Option C architecture (api's
`BarSyncWorker` writes bars on clientId=2; `lean_local` reads on-disk
via `FakeDataQueue` + `SubscriptionDataReaderHistoryProvider`). The
previous seed-data ceremony runbook (`seed-data.md`) was DELETED in the
2026-05-20 v1 attempt and remains deleted under Option C — the api owns
the data-producer role; no operator-side seed scripts to run. The v1
attempt's runbook fragments below the prereqs (Steps 2-3) were updated
in this PR to reflect Option C; the bearer-token wiring (Step 1) is
unchanged.

**Prereqs:**

* Backend deployed with the 2026-05-21 data-layer pivot v2 (Option C)
  merged — `services/data/bar_sync.py` present, `lean.json` reverted
  to `FakeDataQueue` + `SubscriptionDataReaderHistoryProvider`,
  `docker-compose.yml::api` has `lean_data:/Lean/Data:rw`, and
  `docker-compose.yml::lean_local` has `lean_data:/Lean/Data:ro` +
  `networks: [internal]`.
* `ib_gateway` container running + authenticated against the operator's
  IBKR paper account (`DUQ...`). Operator can verify by inspecting
  `docker compose logs ib_gateway` for `Authentication successful` +
  no recent `Error 162`. Used by BOTH the order worker (clientId=1) +
  the bar_sync worker (clientId=2).
* sops decryption working on the VPS (`/etc/credstore.encrypted/age_key`
  configured per the Day 5 carryover pattern).
* `services/api/entrypoint.py` reads `lean.api_bearer_token` from sops →
  exports `API_LEAN_LOCAL_BEARER_TOKEN`.
* SSH access to the operator's VPS at the standard `trading@<host>` path.

**What Option C changes vs the v1 attempt:**

* `lean.json` reverts to FakeDataQueue + SubscriptionDataReaderHistoryProvider — no IBKR plugin in LEAN's runtime.
* `infrastructure/lean_local/Dockerfile` reverts to single-stage (no multi-stage NuGet pull; no `CSharpAPI.dll` / `IBAutomater.jar` / etc.).
* `infrastructure/lean_local/entrypoint.sh` no longer reads `IB_USER_NAME` / `IB_PASSWORD` / `IB_ACCOUNT` / `QC_USER_ID` / `QC_API_TOKEN` from sops — those fields can stay in `secrets/<env>.enc.yaml` (the ib_gateway sidecar uses paper_username + paper_password to log into IBKR) but `lean_local` doesn't need them.
* `docker-compose.yml::lean_local` reverts `networks: [internal, egress]` → `[internal]` (no external HTTPS reach needed).
* `docker-compose.yml::api` adds `lean_data:/Lean/Data:rw` so the BarSyncWorker can write.
* `lean/v1_strategy.py::initialize` restores the `self._log_universe_freshness()` invocation — now catches api-side bar-sync failures (instead of catching operator-side seed-file staleness as it did pre-2026-05-20).

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

Save + exit. sops re-encrypts on save.

> **Why a separate bearer (vs reusing `discord.api_bearer_token`):** so
> a compromise of one container can't grant access via the other.
> Defense-in-depth at the service-account boundary.

---

## Step 2 — Verify IBKR paper-account credentials in sops (NEW 2026-05-20)

**NEW in the 2026-05-20 data-layer sub-pivot.** LEAN now connects to
IBKR via the `InteractiveBrokersBrokerage` data-queue-handler and needs
the paper-account credentials at boot. These are the SAME credentials
the `ib_gateway` sidecar uses for its own gateway login (Pivot-PR-B);
if `ib_gateway` is currently healthy + authenticated, the fields are
already populated.

Verify by inspecting the sops bundle:

```bash
sops -d --extract '["ibkr"]' secrets/paper.enc.yaml
```

You should see all 3 fields populated with real values (NOT the
`<TODO_FROM_PIVOT_PR_*>` placeholders):

```yaml
paper_username: <your-paper-account-username>
paper_password: <your-paper-account-password>
paper_account: DUQ<your-paper-account-id>
```

If any are still `<TODO_...>`, edit `secrets/paper.enc.yaml` via `sops`
and populate them. These came from the operator's IBKR portal at
2026-05-12 Pivot-PR-B bring-up.

Commit + push the sops updates from Step 1 (+ Step 2 if you had to
populate any TODO):

```bash
git add secrets/paper.enc.yaml
git commit -m "ops: paper env adds lean.api_bearer_token (Pivot-PR-A) + verify ibkr.paper_* (2026-05-20 data sub-pivot)"
git push
```

---

## Step 3 — Re-decrypt the sops bundle on the VPS

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
grep -A4 'ibkr:' /opt/trading/secrets-decrypted/decrypted.yaml | head -12
```

You should see:
- `api_bearer_token: <your-32-byte-string>` under `lean:` (NOT `<TODO...>`)
- `paper_username` + `paper_password` + `paper_account` under `ibkr:`,
  all populated (NOT `<TODO...>`)

---

## Step 4 — Build + restart api with the LEAN bearer env var

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

## Step 5 — Build + start the lean_local container

**2026-05-20 sub-pivot note:** the `docker compose build lean_local`
step now runs a multi-stage build that pulls the
`QuantConnect.Brokerages.InteractiveBrokers` v2.5.17699 NuGet package
during the builder stage. This requires the VPS to reach
`api.nuget.org`. First build typically takes 3-5 min; subsequent
builds use Docker's layer cache + finish in < 30s unless
`IBKR_PLUGIN_VERSION` is bumped.

```bash
docker compose --env-file deploy/.env build lean_local
docker compose --env-file deploy/.env up -d lean_local
docker compose --env-file deploy/.env logs lean_local 2>&1 | tail -80
```

Verify the IBKR plugin DLL landed in the final image (one-time sanity check):

```bash
docker compose exec lean_local ls -la /Lean/Launcher/bin/Debug/QuantConnect.Brokerages.InteractiveBrokers.dll
docker compose exec lean_local ls -la /Lean/Launcher/bin/Debug/QuantConnect.IBAutomater.dll
```

Both should exist (~few MB each). If either is missing, the builder
stage failed — see Troubleshooting.

Expected boot sequence in the logs (from `infrastructure/lean_local/entrypoint.sh`
+ LEAN's own logs):

```
[lean_local_entrypoint] merged config written to /Lean/config.json + /Lean/Launcher/bin/Debug/config.json (environment=paper-internal, algorithm-type-name=V1TrendFollowingAlgorithm)
[lean_local_entrypoint] api_base=http://api:8000 live_mode=true env=paper
[lean_local_entrypoint] launching: dotnet /Lean/Launcher/bin/Debug/QuantConnect.Lean.Launcher.dll
... LEAN startup ...
Loading Algorithm: v1_strategy.py
InteractiveBrokersBrokerage: Connecting to ib_gateway:4002 (clientId=10)
InteractiveBrokersBrokerage: Connected. Account=DUQ<...>. Server version <...>
Initialize: v1_strategy initialized (post-pivot 2026-05-12, Pivot-PR-D) live_mode=True api_base=http://api:8000 ...
Brokerage: subscribing to delayed market data (enable-delayed-streaming-data=true)
Warming up: requesting reqHistoricalData for 11 contracts (7 micros + 4 ETFs)
... ~30-60s of warmup as IBKR streams historical bars ...
Algorithm.WarmUp complete
```

**Critical 2026-05-20 boot signature:** the line
`InteractiveBrokersBrokerage: Connected` is the post-sub-pivot equivalent
of pre-sub-pivot's "warming up from on-disk seed". If you don't see it
within 60s of `docker compose up -d lean_local`, jump to Troubleshooting.

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
authenticated against the backend + reading IBKR data**. Proceed.

If you see `lean_auth_required` or `bot_auth_invalid_token` lines on the
api side, the bearer in `decrypted.yaml` does not match what the api is
expecting. Re-run Step 3 + Step 4.

---

## Step 6 — Confirm 17:30 ET cycle heartbeats land WITH FRESH IBKR DATA

LEAN's `on_daily_signal_cycle` fires at 17:30 ET wall-clock daily.
After the next cycle (could be up to 24h depending on when you deployed):

```bash
docker compose --env-file deploy/.env logs api --since 24h 2>&1 | grep 'event_type=lean_cycle_heartbeat'
```

Expected: at least one log line with `event_type=lean_cycle_heartbeat`
containing `session_date_et` + `equity_usd` + `live_mode` fields +
`signals_emitted_count` + `rejections_count`.

**Critical 2026-05-20 sub-pivot smoke:** the `signals_emitted_count`
+ `rejections_count` together should sum to **11** (all 7 micros +
all 4 ETFs evaluated). If they sum to less, OR if you see
`v1_history_unavailable failed_markets=[...]` in LEAN's logs, the
IBKR data-queue subscription failed for the listed markets — see
Troubleshooting.

```bash
docker compose --env-file deploy/.env logs lean_local --since 24h 2>&1 | grep -E 'v1_history_unavailable|v1_signals_generated'
```

Pre-sub-pivot this check existed but was chronically broken (the
2026-05-17 staleness incident). Post-sub-pivot the IBKR data path
returns current-trading-day bars including the settlement bar for
every market in the Phase 1 sub-universe, so `failed_markets` should
always be empty.

---

## Step 7 — Audit chain integrity check

The data-layer sub-pivot does NOT write audit_log rows (the data path
is read-only from IBKR's perspective). But run `verify_chain` to confirm
the existing chain still walks cleanly after the new code paths
deployed.

```bash
docker compose --env-file deploy/.env exec api /opt/venv/bin/python -m services.audit.verify_chain --env paper
```

Expected: `CHAIN OK: <N> rows verified` where `<N>` is whatever the
chain count was pre-deploy. If this returns CHAIN BREAK, escalate per
`deploy/audit/README.md` incident-review procedure.

---

## Step 8 — Cleanup + record state

If everything is green, no further action needed. The `lean_local`
container will run continuously, emit a heartbeat per 17:30 ET cycle,
read market data from IBKR on clientId=10 throughout the trading day,
and stay alive across `docker compose restart` cycles.

Record the deploy state in the next session's `Docs/decisions-log.md`
carryover entry:

* Commit SHA the api was deployed at (`docker compose --env-file deploy/.env config | grep image`)
* First `lean_event_received` timestamp from api logs
* First `InteractiveBrokersBrokerage: Connected` timestamp from `lean_local` logs
* `verify_chain` row count

---

## VPS-side cleanup (operator decision; NOT executed by the PR)

The 2026-05-20 sub-pivot retires the seed-file architecture and any
related cron/systemd unit. If the operator previously deployed the
2026-05-19 evening synthesis-cron stopgap (which only existed in a
parallel worktree + never landed on `main`, so likely nothing to clean
up), disable + remove:

```bash
ssh trading@<vps-host>
sudo systemctl disable --now lean-universe-synthesis.timer 2>/dev/null || echo "timer not installed — nothing to do"
sudo rm -f /etc/systemd/system/lean-universe-synthesis.service \
            /etc/systemd/system/lean-universe-synthesis.timer 2>/dev/null
sudo systemctl daemon-reload
```

After a 30-day soak with stable IBKR-data operation, the operator can
also drop the now-unused `trading_lean_data` Docker volume to reclaim
disk space (typically 200-500MB for the seeded universe):

```bash
docker compose down lean_local
docker volume rm trading_lean_data
docker compose up -d lean_local
```

The compose file's volume mount stays (so an operator-side rollback
can re-attach old seed data); the volume itself is what gets removed.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `lean_local` exits with code 2 + log line `lean.api_bearer_token missing or empty` | Step 3 didn't refresh decrypted.yaml on the VPS | Re-run Step 3 on the VPS; verify the field with `grep` |
| `lean_local` exits with code 2 + log line `IB_USER_NAME is empty` / `IB_PASSWORD is empty` / `IB_ACCOUNT is empty` | One of the 3 IBKR paper-account fields in sops is missing | Verify per Step 2 + re-run Step 3 to refresh decrypted.yaml |
| `lean_local` exits with code 2 + log line `IB_<field> still has placeholder value '<TODO_...>'` | The sops template field is unedited | Run `sops secrets/paper.enc.yaml`; populate the field; commit + push; re-decrypt on VPS |
| `docker compose build lean_local` fails in builder stage with `Unable to load the service index` or NuGet pull errors | VPS can't reach `api.nuget.org` (egress firewall, transient network) | Verify VPS network: `curl -sI https://api.nuget.org/v3/index.json` should return HTTP 200. If blocked, check Hetzner cloud firewall + retry. If a specific `IBKR_PLUGIN_VERSION` is unreachable, override with a known-good version: `docker compose build --build-arg IBKR_PLUGIN_VERSION=2.5.X.Y lean_local`. |
| `docker compose exec lean_local ls /Lean/Launcher/bin/Debug/QuantConnect.Brokerages.InteractiveBrokers.dll` returns "No such file or directory" | The Dockerfile's multi-stage COPY didn't execute, or the image was pulled from registry instead of built locally | Force a no-cache rebuild: `docker compose --env-file deploy/.env build --no-cache lean_local`. Verify the build logs show the builder stage finishing with `ls -la /publish/QuantConnect.Brokerages.InteractiveBrokers.dll` printing a non-zero file size. |
| LEAN logs `Sequence contains no matching element` shortly after Launcher startup | The IBKR plugin DLL is missing from the final image (Dockerfile build issue) OR LEAN is binding to a brokerage class the loaded DLLs don't expose | Re-check `/Lean/Launcher/bin/Debug/` contents per the previous row. If both `QuantConnect.Brokerages.InteractiveBrokers.dll` + `QuantConnect.IBAutomater.dll` are present, the Composer fail is elsewhere — check `lean/lean.json::data-queue-handler` is exactly `["InteractiveBrokersBrokerage"]` (NOT `"QuantConnect.Brokerages.InteractiveBrokers.InteractiveBrokersBrokerage"` — the short name is the correct alias). See `Docs/decisions-log.md` 2026-05-12 'Post-ceremony session' entry + 2026-05-20 entry for the backstory. |
| LEAN logs `Error 162: Trading TWS session is connected from a different IP address` | The operator's TWS Desktop OR browser session at `portal.interactivebrokers.com` is logged in for the same paper account AND IBKR enforces single-IP, OR a clientId collision between LEAN (clientId=10) and another client | (1) **Most common cause** per 2026-05-19 evening probe: operator's browser session is logged in. Close all IBKR browser tabs + TWS Desktop; restart `lean_local`: `docker compose restart lean_local`. (2) Verify clientId allocation per dev-guide §1.5 LOCKED — api=1, LEAN=10, probes=80-99. Use `docker compose logs ib_gateway 2>&1 \| grep -i 'client'` to see active client connections. (3) If recurring, the wedge can persist server-side for ~30 min after a disconnect — wait + retry. |
| `lean_local` logs `InteractiveBrokersBrokerage: Connecting to ib_gateway:4002 (clientId=10)` then hangs with no follow-up | The gateway accepted the TCP connection but isn't responding to API requests — typically because the gateway isn't authenticated to IBKR servers OR the operator's overnight-maintenance restart hung (per 2026-05-18 drill 5 incident) | Check `docker compose logs ib_gateway --tail=100` for "Authentication successful" or "Existing session detected". If the gateway isn't authenticated, restart it: `docker compose restart ib_gateway` + wait 60s. If "Existing session detected" — same fix as Error 162 above (close all other IBKR sessions). |
| api logs show `lean_auth_required` on `/api/internal/lean/signals` POSTs | LeanAuthMiddleware rejected the bearer | Compare `decrypted.yaml::lean.api_bearer_token` to `docker compose exec api env | grep LEAN`. They must match byte-for-byte. Re-run Steps 3 + 4. |
| api logs show `lean_event_signal_emitted_not_wired` warnings | LEAN emitted a `signal_emitted` event but the endpoint hasn't been wired to the dispatcher yet (PR-A scope was heartbeat only; PR-D wired it) | Verify api is at `99b8be9` or later. If you see this on a current build, it's a regression — escalate. |
| `verify_chain` returns CHAIN BREAK | Something unrelated wrote a malformed audit row; not LEAN's fault | Escalate per `deploy/audit/README.md` incident-review procedure |
| LEAN container restart-loops with `dotnet` segfault | Insufficient RAM (LEAN's .NET runtime needs ~500MB) | Check `free -h` on the VPS; if RAM is exhausted by other containers, consider upgrading from CCX13 → CCX23 (~$10/mo step-up) |
| LEAN logs `Failed to load the algorithm. Cannot find class 'V1TrendFollowingAlgorithm' in file 'v1_strategy.py'` | The file rename from `v1_qc_algorithm.py` didn't take effect in the mounted volume | `docker compose down lean_local`; verify `ls /opt/trading/lean/v1_strategy.py` on VPS; if `v1_qc_algorithm.py` is still present, the git pull didn't apply (`git pull --ff-only` from VPS); re-up |
| LEAN cycle log shows `v1_history_unavailable failed_markets=[...]` for one or more markets | IBKR's `reqHistoricalData` returned empty for the active contract on that session_date | (1) Check the operator's IBKR futures-trading entitlement covers the affected markets. (2) Check `docker compose logs lean_local --since 5m \| grep -i 'reqHistoricalData\|HistoricalData'` for IBKR error responses (e.g., Error 200 "No security definition has been found for the request"). (3) Verify the contract symbology — `lean.json` uses LEAN-canonical futures keys (`/MES`, `/MGC`, etc.); LEAN's continuous-contract resolver translates to IBKR contract IDs. If a market is consistently failing, there may be a symbology mismatch — escalate. |
| Need to verify the merged config inside the container | The entrypoint writes the deep-merged config to two locations; both must show the IBKR data-queue config | `docker compose exec lean_local cat /Lean/config.json \| python3 -c 'import json,sys; c=json.load(sys.stdin); env=c.get("environment"); e=c.get("environments",{}).get(env,{}); print("env:", env, "live-mode-brokerage:", e.get("live-mode-brokerage"), "data-queue:", e.get("data-queue-handler"), "history:", e.get("history-provider"))'`. Expected: `env: paper-internal live-mode-brokerage: PaperBrokerage data-queue: ['InteractiveBrokersBrokerage'] history: ['InteractiveBrokersBrokerage']`. |

---

## Token rotation procedure

Per backend-spec §6.6 + dev-guide §1.5 (LOCKED), the LEAN bearer rotates
with the quarterly secrets rotation alongside the Discord bot bearer.

1. Generate new bearer (Step 1's command).
2. Update `secrets/paper.enc.yaml` AND `secrets/live.enc.yaml`.
3. Commit + push.
4. On VPS: re-decrypt sops bundle (Step 3).
5. Restart **api FIRST** (so the new bearer is in `API_LEAN_LOCAL_BEARER_TOKEN`).
6. Restart `lean_local` SECOND (so the new bearer is in `LEAN_LOCAL_BEARER_TOKEN`
   and matches what api expects).
7. Confirm Step 5's smoke (one `lean_event_received` line) within 60s of
   restart.
8. **Revoke the old bearer** — there is no separate "old bearer revoke"
   step because the api only checks against the currently-configured
   token via constant-time-compare. The instant the api restart in Step
   5 completes, the old bearer no longer authenticates.

The IBKR paper credentials (Step 2) rotate independently per the
operator's IBKR portal password policy (typically every 90 days when
IBKR forces a password change). After an IBKR password change:

1. Update `secrets/paper.enc.yaml` `ibkr.paper_password` via sops.
2. Steps 3 + 4 + 5 above.
3. Watch `lean_local` logs for `InteractiveBrokersBrokerage: Connected`
   on the next restart.
4. Watch `ib_gateway` logs to confirm the sidecar's separate gateway
   login also authenticates (`ib_gateway` reads the same sops field
   `ibkr.paper_password` for its own login).

---

## Cross-references

- Pivot rationale (foundational): `Docs/decisions-log.md` 2026-05-12 entry.
- Data-layer sub-pivot rationale: `Docs/decisions-log.md` 2026-05-20 entry
  "Phase 1 data-layer pivot: IBKR delayed quotes replace seed-file architecture".
- Backend-spec §1.2 (current architecture diagram) — shows LEAN → ib_gateway
  data edge alongside api → ib_gateway order edge.
- Dev-guide §1.5 LOCKED — IBKR clientId allocation (api=1, LEAN=10, probes=80-99).
- `services/api/middleware.py::LeanAuthMiddleware`: source of the
  bearer-check logic.
- `services/api/routes/internal/lean.py`: source of the
  `/api/internal/lean/signals` endpoint.
- `lean/v1_strategy.py`: the LEAN algorithm wrapper that POSTs to the
  endpoint + calls `self.history` against IBKR.
- `lean/lean.json`: the data-queue-handler + history-provider config.
- `infrastructure/lean_local/Dockerfile`: multi-stage build that bakes
  the IBKR plugin DLL into the image.
- `infrastructure/lean_local/entrypoint.sh`: container entrypoint that
  resolves sops → env vars → LEAN config (including IB_USER_NAME /
  IB_PASSWORD / IB_ACCOUNT for the data-queue connection).
- `deploy/ibkr/README.md` (Pivot-PR-B): IBKR Gateway operator runbook —
  used by the api's `services/execution/ibkr_adapter.py` client on
  clientId=1 + LEAN's data-queue-handler on clientId=10. Same gateway,
  distinct sockets per IBKR's multi-client-id design.
- NuGet plugin: [QuantConnect.Brokerages.InteractiveBrokers](https://www.nuget.org/packages/QuantConnect.Brokerages.InteractiveBrokers)
  (pinned to v2.5.17699 in `infrastructure/lean_local/Dockerfile`).
