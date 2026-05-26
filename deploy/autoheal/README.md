# autoheal sidecar

Operator runbook for the `autoheal` service in `docker-compose.yml`.

## What is it?

`autoheal` is a sidecar container that monitors all containers labeled `autoheal=true` and invokes `docker restart <container>` whenever Docker's healthcheck reports `(unhealthy)`. It's a small (~10MB) Alpine + bash + `curl` + `jq` script that polls the Docker API every ~30s.

Upstream: <https://github.com/willfarrell/docker-autoheal>

## Why is it here?

Drill 6 (2026-05-25) was the second time in 7 days that the `ib_gateway` Java process got stuck after a nightly auto-restart, leaving the IBKR API port unreachable for hours-to-days until manual operator intervention. Pairing this sidecar with the drill-6 healthcheck improvement (probes BOTH external socat AND internal Java ports — see PR #239) closes the recovery loop:

1. `ib_gateway` Java stuck → internal port 4002 refuses → healthcheck `(unhealthy)` within ~3 min
2. `autoheal` polls every 30 s → sees `(unhealthy)` → invokes `docker restart ib_gateway`
3. IBC re-logs in (~6 s per drill 6 evidence) → healthcheck `(healthy)` again
4. `bar_sync`'s next 21:00 UTC cycle succeeds against the recovered gateway

See `Docs/decisions-log.md` 2026-05-25 entry "ib_gateway stuck-at-login recurrence + recovery (drill 6)" for the full retrospective.

## What containers does it monitor?

Only containers with the label `autoheal: 'true'` in `docker-compose.yml`. Currently:

| Service | Label | Healthcheck | Failure mode autoheal recovers |
|---|---|---|---|
| `ib_gateway` | `autoheal=true` | Probes both 4004 (external socat) AND 4002 (internal Java) | Stuck IBC after nightly restart (drill 5 + drill 6) |

To opt another service in:

1. Add `labels: { autoheal: 'true' }` to its service definition
2. Verify the service has a `healthcheck:` block that reliably detects failure states (autoheal trusts the healthcheck; a flaky healthcheck → restart loop)
3. Verify the service's failure modes are restart-recoverable (don't autoheal Postgres unless you really want to restart it under load)

## What containers does it NOT monitor?

`api`, `lean_local`, `postgres`, `discord_bot`, `webhook_pusher`, `nextjs`, `caddy`, and all profile-gated services are NOT labeled. Reasons:

- **`postgres`** — a stuck Postgres is rare and almost always indicates a real problem (disk full, OOM, deadlock) that benefits from operator triage rather than blind restart
- **`api`** — lifespan startup errors (bad sops bundle, alembic mismatch) are best surfaced via operator alert, not papered over by restart loops
- **`lean_local`** — already restarted daily by the host-side `lean-local-daily-restart.timer` systemd timer (post-bar_sync data-layer cache refresh); double-restart cadence would be noisy
- **`caddy`, `nextjs`, `discord_bot`, `webhook_pusher`** — operator hasn't observed stuck-state failure modes on these; their existing healthchecks + `restart: unless-stopped` policy is sufficient

If operator observes a real stuck-state pattern on any of the above, opt them in via the label.

## Trade-off: Docker socket exposure

`autoheal` requires read+write access to `/var/run/docker.sock`. Anything that can write to that socket can spawn privileged containers, mount host paths, run arbitrary commands as root on the host — **so the autoheal container has root-equivalent access to the host.**

Mitigations:

- **Pin to a specific image SHA** after first deploy if security-paranoid (we currently use `willfarrell/autoheal:latest` for simplicity). Lookup current SHA via `docker pull willfarrell/autoheal:latest && docker inspect ...`
- **Read the source** before deployment: <https://github.com/willfarrell/docker-autoheal>. The runtime script is ~150 lines of bash; auditable in 15 min
- **Treat any autoheal compromise as host-root compromise.** No regression vs. the existing trust posture (the operator's SSH key already grants host-root), but a new attack surface exists

If the trade-off is unacceptable, the alternative is a host-side `systemd` timer that runs `docker compose ps` + restarts unhealthy containers. More transparent, no Docker-socket-in-container exposure, but adds host-side complexity.

## Debug commands

```bash
# Is autoheal running?
docker compose --env-file deploy/.env ps autoheal

# What is autoheal seeing? (look for "Container N is unhealthy" lines)
docker compose --env-file deploy/.env logs autoheal --tail 100

# What containers does autoheal think it should monitor?
docker ps --filter "label=autoheal=true" --format 'table {{.Names}}\t{{.Status}}'

# Force a healthcheck failure on ib_gateway to test the recovery loop:
#   1. SSH to VPS
#   2. docker exec trading-ib_gateway-1 sh -c 'pkill -9 socat'   # kill socat
#   3. Watch `docker compose ps ib_gateway` transition:
#      healthy → (~3 min) unhealthy → (~30 s) Restarting → (~2 min) healthy
#   4. Check autoheal logs: should see one "Container N is unhealthy" line + restart
docker exec trading-ib_gateway-1 sh -c 'pkill -9 socat'
watch -n 5 'docker compose --env-file deploy/.env ps ib_gateway'
```

## Opt out (temporarily disable)

```bash
docker compose --env-file deploy/.env stop autoheal
```

To re-enable: `docker compose --env-file deploy/.env start autoheal`.

To remove entirely:

1. Comment out the `autoheal:` service block in `docker-compose.yml`
2. Remove the `labels:` block from `ib_gateway:` (or set `autoheal: 'false'`)
3. `docker compose up -d`

## Tuning

| Env var | Default in this repo | Effect |
|---|---|---|
| `AUTOHEAL_CONTAINER_LABEL` | `autoheal` | Only restart containers with this label. `all` = every container with a healthcheck |
| `AUTOHEAL_INTERVAL` | `30` (seconds) | How often autoheal polls the Docker API. Default upstream is `5`; we raised it to reduce socket noise |
| `AUTOHEAL_START_PERIOD` | unset | If unset, autoheal honors each container's own `healthcheck.start_period`. `ib_gateway`'s 120s is preserved (IBC takes ~90s to log in fresh) |
| `WEBHOOK_URL` | unset | Optional — notify a Discord/Slack webhook on each restart. Skipping for now; the bar_sync alert path at `consecutive_count >= 2` is the operator-visible signal |

## Bound on time-to-recovery

From-stuck-state to fully-recovered:

- `healthcheck.interval × retries` = `60 s × 3` = **180 s** (Docker reports `(unhealthy)`)
- `AUTOHEAL_INTERVAL` = **30 s** (autoheal polls + decides)
- `docker restart` = **~10 s**
- IBC re-login + healthcheck `start_period` = **~120 s** (IBC's 90 s + buffer)

**Total: ~5.5 min worst case.** Much better than the 18 h observed in drill 6.

## Lineage

- Introduced 2026-05-25 as drill 6 follow-up #3
- Drill 5 (2026-05-18) was the prior incident; drill 6 retrospective covers both
- Paired-with PR #239 (`fix(deploy): ib_gateway healthcheck probes both external + internal ports`) — autoheal needs the healthcheck to actually flag failures to act on them; #239 makes that work
