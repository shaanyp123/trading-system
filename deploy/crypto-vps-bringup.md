# Crypto-Perps VPS Bringup — Operator Runbook

Fresh-host bringup for the Coinbase CFM crypto-perps system. Written for
the post-pivot reality (2026-07-09): the original Ashburn paper VPS was
**cancelled without backup** (operator-directed fresh start), the sops/age
secrets pipeline is **retired** (operator-approved amendment, see
`Docs/decisions-log.md` 2026-07-09), and the stack is much lighter than
the CME era — no LEAN, no IB Gateway, no bar_sync.

**What runs here:** Caddy (TLS) → FastAPI api + Postgres + Discord bot +
webhook pusher + Coinbase market-data worker (in-process), and — once C1
lands — the strategy worker (00:05 UTC decision + 30 s risk loop).

**Secrets model (amended):** ONE plain-YAML file at
`/opt/trading-secrets/secrets.yaml`, root-authored from
`deploy/secrets.template.yaml`, never in the repo. Recovery = re-issue
every key from its dashboard (Appendix A). There is no encryption
ceremony, no age key, nothing to keep in a safe.

---

## Step 0 — Provision the box (Hetzner Cloud console)

* Hetzner Cloud → New server → location **Ashburn (us-east)**.
* Image: **Ubuntu 24.04**. Type: **CPX11** (2 vCPU / 2 GB) — the stack
  idles well under 1 GB now; resize later if Postgres wants more.
* SSH key: add your Mac's public key (`cat ~/.ssh/id_ed25519.pub`;
  generate with `ssh-keygen -t ed25519` if you don't have one).
* Note the server IP. Point the apex `A` record (`spratcapital.com`) at it
  (same registrar panel as before). The paper env is served at the APEX —
  there is no `paper.` subdomain (Caddyfile has apex + `www.` + reserved
  `live.` blocks only; see the Day-5 watchdog incident in decisions-log).
  TLS (Step 6) needs DNS resolving first — do this early, it can take a
  few minutes.

```bash
ssh root@<NEW_IP>
```

## Step 1 — Base packages

```bash
apt-get update && apt-get install -y docker.io docker-compose-v2 git python3-yaml
systemctl enable --now docker
```

## Step 2 — Read-only deploy key + clone

```bash
ssh-keygen -t ed25519 -f /root/.ssh/deploy_key -N "" -C "trading-vps-deploy"
cat /root/.ssh/deploy_key.pub
```

Copy the printed line → GitHub repo → Settings → Deploy keys → Add —
**leave "Allow write access" UNCHECKED**. Then:

```bash
cat > /root/.ssh/config <<'EOF'
Host github.com
  IdentityFile /root/.ssh/deploy_key
  IdentitiesOnly yes
EOF
git clone git@github.com:shaanyp123/trading-system.git /opt/trading
cd /opt/trading
git config --global --add safe.directory /opt/trading
```

## Step 3 — Author the secrets file

```bash
mkdir -p /opt/trading-secrets
cp /opt/trading/deploy/secrets.template.yaml /opt/trading-secrets/secrets.yaml
chmod 0600 /opt/trading-secrets/secrets.yaml
nano /opt/trading-secrets/secrets.yaml
```

Fill per the template's inline comments. Bringup minimum (everything
else can stay `<TODO_...>` and the matching feature stays off):

1. `postgres.*` — two fresh `openssl rand -hex 32` values.
2. `coinbase.*` — the CDP key name + PEM. **Do NOT hand-type or paste
   the PEM into an editor or chat** (2026-07-09 incident: a key pasted
   into a Claude session had to be revoked). Instead: `scp` the
   downloaded CDP JSON to the VPS and inject it script-assisted so the
   key never renders on screen, then `shred -u` the temp file and
   delete the JSON from the PC (password-manager file attachment is
   the only surviving copy). Claude generates the inject script; it
   validates PEM shape + YAML round-trip and prints only line counts.
3. `discord.*` — bot token, guild id, a fresh `api_bearer_token`,
   webhook URLs (re-issue from Discord if the old ones were lost —
   Appendix A).
4. `internal.*`, `totp.encryption_key` — fresh values per the template's
   generate commands.
5. `resend.api_key` — OPTIONAL (deferred 2026-07-09): placeholder =
   email alerting off; Discord remains the alert channel.

The generate commands are in the template comments next to each key.
Save (Ctrl-O, Enter, Ctrl-X). Don't fix ownership — the bringup script
sets uid 1000 + 0400 for the containers.

## Step 4 — Author deploy/.env

```bash
cp /opt/trading/deploy/.env.example /opt/trading/deploy/.env
chmod 0600 /opt/trading/deploy/.env
nano /opt/trading/deploy/.env
```

Set `POSTGRES_SUPERUSER_PASSWORD` (a third `openssl rand -hex 32`) and
check `DOMAIN`/`ACME_EMAIL`. `SECRETS_DIR` default is already right.

## Step 5 — Run the bringup script

```bash
cd /opt/trading
bash deploy/day5-bringup.sh
```

Idempotent end-to-end: validates the secrets file, builds the api image,
brings up Postgres, runs alembic (fresh DB → full migration chain),
sets the app-role passwords, starts api + caddy, prints the one-time
`SETUP_TOKEN` (capture into 1Password — it bootstraps your operator
login), and gates on `/api/health`.

## Step 6 — Verify from outside + bring up the rest

```bash
# from your laptop:
curl -fsS https://spratcapital.com/api/health
```

Then back on the VPS:

```bash
docker compose --env-file deploy/.env up -d discord_bot webhook_pusher
docker compose --env-file deploy/.env ps
```

All containers healthy = bringup done.

## Step 7 — Seed the account (fresh DB, Amendment B)

```bash
docker compose --env-file deploy/.env exec api python -c "
import os, sys, runpy, yaml
pw = yaml.safe_load(open('/run/secrets/secrets.yaml'))['postgres']['app_service_password']
os.environ['DATABASE_URL'] = f'postgresql+asyncpg://app_service:{pw}@postgres:5432/trading'
sys.argv = ['bootstrap_live_account', '--env', 'paper', '--mint-from-defaults',
            '--external-account-id', 'operator', '--no-dry-run', '--confirm']
runpy.run_module('scripts.operator_tools.bootstrap_live_account', run_name='__main__')
"
```

Two things this wrapper gets right that a bare `docker compose exec …
python -m scripts.operator_tools.bootstrap_live_account` does not (both
bit the 2026-07-09 bringup): `docker compose exec` bypasses the
entrypoint, so `DATABASE_URL` must be built in-process from the mounted
secrets file; and the script's `--external-account-id` **default is the
retired IBKR account number** — the paper convention is `operator`.

(Full bootstrap — account + risk_state + parameter head row — is correct
here because the DB is fresh. On an already-bootstrapped env you'd add
`--seed-params-only`; see the script docstring.) Mints the Amendment B
parameter set (`services/risk/crypto_parameters.py`) + genesis rows. The audit chain starts fresh — the CME-era history was
intentionally discarded with the old VPS (decisions-log 2026-07-09).

## Step 8 — Coinbase canary drills

Run `deploy/coinbase_execution/README.md` end to end — those drills are
the C0 execution exit gates and must pass before the strategy worker is
wired to real order flow.

---

## Appendix A — Secret re-issue runbook (disaster recovery)

There are no backups of the secrets file by design. If the VPS dies,
provision a new one (Steps 0–2) and re-issue:

| Key | Where to re-issue | Notes |
|---|---|---|
| `postgres.*`, `internal.*`, `totp.encryption_key`, `discord.api_bearer_token` | generate fresh (`openssl rand -hex 32` etc.) | nothing external to revoke; fresh DB gets fresh values. A lost `totp.encryption_key` orphans enrolled TOTP secrets — re-enroll via a new SETUP_TOKEN. |
| `coinbase.*` | portal.cdp.coinbase.com → API keys | REVOKE the old key first, then create anew (ECDSA, View+Trade only, IP-allowlist the new VPS) |
| `discord.bot_token` | discord.com/developers → your app → Bot → Reset Token | old token dies on reset |
| `discord.webhook_urls.*` | Discord server → channel settings → Integrations → Webhooks | delete old, create new per channel |
| `resend.api_key` | resend.com → API Keys | revoke old, create new |
| `anthropic.*` | console.anthropic.com → API Keys | when the agent phase is live |
| `github.*` / `s3.*` | github.com/settings/apps / provider console | only if those features are in use |

## Appendix B — What died with the old VPS (2026-07-09)

Cancelled without backup, operator-directed: the CME-era Postgres data
(audit chain, paper trade history, recon snapshots), Caddy certs (auto
re-issued), the sops age-key copy, and any secrets only ever filled
server-side. Nothing live depended on any of it; the CME system was
already fully decommissioned (decisions-log 2026-07-08/09). Docs/ +
git history remain the only — and sufficient — record of that era.

## Appendix C — Redeploying code changes (added 2026-07-09, post-C1-start)

The redeploy ceremony is one command (the script's `--rebuild` flag, added
2026-07-09 evening, rebuilds ALL app images even when cached — so the
alembic step runs inside the fresh image — then force-recreates every
RUNNING app container onto the new images):

```bash
cd /opt/trading
git pull
bash deploy/day5-bringup.sh --rebuild
```

History (why the flag exists): the original script's build step was
skip-if-cached and its alembic step ran inside whatever image existed, so
on a stale image new migrations silently no-op'd while the script reported
success (this bit the 2026-07-09 launch three separate ways). The interim
hand ceremony (explicit `docker compose build` + `up -d --force-recreate`
per service) had its own trap: `docker compose restart` reboots the
EXISTING container on its OLD image — it never adopts a freshly built one
(bit the #364 hotfix deploy the same evening). `--rebuild` encodes both
lessons. The worker recreate is safe mid-day: startup recovery re-reads
persisted state, sees an already-handled decision date, and resumes the
30 s loop.

**Caddyfile-only changes** don't need `--rebuild` (the config is
bind-mounted). Ceremony — validate BEFORE restarting, and `sleep` before
the smoke curl (a curl fired immediately after `restart` races the
listener bind and reports a phantom outage; false-alarmed the #368 gate
deploy):

```bash
cd /opt/trading && git pull
docker run --rm -v /opt/trading/deploy/Caddyfile:/etc/caddy/Caddyfile:ro \
  -e DOMAIN=spratcapital.com -e ACME_EMAIL=x@example.com \
  caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile 2>&1 | tail -1  # expect: Valid configuration
docker compose --env-file deploy/.env restart caddy
sleep 8
curl -fsS https://spratcapital.com/api/health >/dev/null && echo health-ok
```

Verify the running code actually changed (don't trust the build banner):

```bash
docker compose --env-file deploy/.env exec api git -C / rev-parse HEAD 2>/dev/null \
  || docker compose --env-file deploy/.env exec api python -c "import services; print('probe a changed symbol instead')"
```

Heartbeat / DB smoke queries: `psql -U app_service` prompts for a password
the operator doesn't have at hand — query via the api container instead
(credentials from the mounted secrets file):

```bash
docker compose --env-file deploy/.env exec api python -c "
import asyncio, asyncpg, yaml
pw = yaml.safe_load(open('/run/secrets/secrets.yaml'))['postgres']['app_service_password']
async def main():
    conn = await asyncpg.connect(user='app_service', password=pw, database='trading', host='postgres')
    for r in await conn.fetch('SELECT risk_loop_heartbeat_utc, risk_loop_tick_count, marks_stale FROM strategy_worker_status'):
        print(dict(r))
    await conn.close()
asyncio.run(main())
"
```
