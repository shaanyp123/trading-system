# Day 5 — Paper VPS deploy runbook

The Day 5 deploy brings up the first stack on the **Hetzner Ashburn primary
VPS** (CCX13, `178.156.239.84`). Goal: `curl https://spratcapital.com/api/health`
returns `{"status":"ok",...}` from the operator's laptop.

The deploy is split into a **one-time setup** (Steps 1-4) and a
**re-runnable bringup script** (Step 5). Once Steps 1-4 land, every future
deploy is a one-liner: `git pull && bash deploy/day5-bringup.sh`.

If anything fails, capture the exact error + the step number and stop. We
debug rather than blow past it (root-cause discipline per dev-guide §1.3).

---

## Step 1 — One-time VPS prep

SSH in as root (Hetzner-provisioned servers ship with root SSH key auth):

```bash
ssh root@178.156.239.84
```

Install supporting tooling. Hetzner's Ubuntu 24.04 image ships with Docker
already; we just add age, jq, and the sops binary:

```bash
apt update && apt install -y age jq nano
SOPS_VERSION=3.10.2
curl -sSfL "https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops-v${SOPS_VERSION}.linux.amd64" -o /tmp/sops
install -m 0755 /tmp/sops /usr/local/bin/sops
sops --version
mkdir -p /opt/trading /etc/credstore.encrypted
```

## Step 2 — Repo clone via SSH deploy key

Generate a deploy key on the VPS:

```bash
ssh-keygen -t ed25519 -N '' -f /root/.ssh/github_deploy_key -C "ashburn-deploy@spratcapital.com"
cat /root/.ssh/github_deploy_key.pub
```

Copy the printed public key. On your laptop browser, open
<https://github.com/shaanyp123/trading-system/settings/keys/new> and:

- **Title:** `ashburn-vps-deploy-key`
- **Key:** paste the line above
- **Allow write access:** UNCHECKED (read-only)
- Click **Add key**

Back on the VPS, wire the SSH config and clone:

```bash
cat >> /root/.ssh/config <<'EOF'
Host github-trading
  HostName github.com
  User git
  IdentityFile /root/.ssh/github_deploy_key
  IdentitiesOnly yes
EOF
chmod 600 /root/.ssh/config
ssh -T github-trading   # type 'yes' to accept github.com fingerprint

cd /opt/trading
git clone github-trading:shaanyp123/trading-system.git .
git config --global --add safe.directory /opt/trading
git log --oneline -5
```

## Step 3 — Install paper age private key

The paper age private key lives on `~/.config/sops/age/keys.txt` on your
laptop (and on the printed paper in your fireproof safe). The Day 2 paper
key fingerprint is `age1dth25vwm75fpc32an0e77y39je2q8uyqe4sx3ysxjlamnlu6n43qrpa4wh`.

From your **laptop**:

```bash
scp ~/.config/sops/age/keys.txt root@178.156.239.84:/etc/credstore.encrypted/age_key
```

Back on the VPS:

```bash
chmod 0400 /etc/credstore.encrypted/age_key
chown root:root /etc/credstore.encrypted/age_key

# Sanity check: round-trip a non-secret value through sops.
SOPS_AGE_KEY_FILE=/etc/credstore.encrypted/age_key \
  sops -d --extract '["webauthn"]["rp_id"]' /opt/trading/secrets/paper.enc.yaml
# Expected: spratcapital.com
```

## Step 4 — Author `deploy/.env` + fill Postgres app-role passwords in sops

### 4a — Generate + paste two app-role passwords into sops

```bash
cd /opt/trading
export EDITOR=nano
APP_SERVICE_PWD=$(openssl rand -hex 32)
APP_OWNER_PWD=$(openssl rand -hex 32)
echo "APP_SERVICE_PWD=$APP_SERVICE_PWD"
echo "APP_OWNER_PWD=$APP_OWNER_PWD"

SOPS_AGE_KEY_FILE=/etc/credstore.encrypted/age_key sops secrets/paper.enc.yaml
```

In nano, find the two `<TODO_FROM_DAY_3_POSTGRES_BOOTSTRAP>` placeholders
under the `postgres:` section. Replace each with the matching `APP_*_PWD`
value above. Save with **Ctrl-O**, Enter, **Ctrl-X**.

Verify both landed:

```bash
SOPS_AGE_KEY_FILE=/etc/credstore.encrypted/age_key \
  sops -d --extract '["postgres"]["app_service_password"]' secrets/paper.enc.yaml | wc -c
# Expected: 64 (or 65 with trailing newline)
SOPS_AGE_KEY_FILE=/etc/credstore.encrypted/age_key \
  sops -d --extract '["postgres"]["app_owner_password"]' secrets/paper.enc.yaml | wc -c
# Expected: 64 (or 65)

unset APP_SERVICE_PWD APP_OWNER_PWD
```

### 4a.1 — Back up the filled sops file (CRITICAL — prevents `git reset` data loss)

`secrets/paper.enc.yaml` is tracked in git. The VPS's deploy key is
read-only, so changes you make here can't be pushed back to GitHub.
**Future `git reset --hard` will wipe these passwords back to placeholder
strings**, breaking the next deploy.

Three protection options, in increasing order of robustness:

**Option A (manual backup, simplest):** copy the filled file outside the
repo. Restore before each deploy that does `git reset --hard`.

```bash
cp /opt/trading/secrets/paper.enc.yaml /etc/credstore.encrypted/paper.enc.yaml.backup
chmod 0400 /etc/credstore.encrypted/paper.enc.yaml.backup

# Future deploy pattern:
cp /etc/credstore.encrypted/paper.enc.yaml.backup /opt/trading/secrets/paper.enc.yaml
bash deploy/day5-bringup.sh
```

**Option B (proper, do this in a follow-up PR):** download the filled file
to your laptop, commit + push from there. The repo's `secrets/paper.enc.yaml`
becomes the canonical filled version. Future `git pull` on the VPS
preserves it forever.

```bash
# On laptop:
scp root@178.156.239.84:/opt/trading/secrets/paper.enc.yaml secrets/paper.enc.yaml
cd <local repo>
git add secrets/paper.enc.yaml
git commit -m "chore(secrets): fill paper.enc.yaml app-role passwords (Day 5 deploy)"
git push origin <branch>
# Open PR; merge.
```

**Option C (auto-restore in the script):** the bringup script could detect
a backup file and restore on each run. Not implemented yet — covered as a
Day 5 follow-up.

For Day 5, do **Option A**. The Option-B follow-up PR can land Day 6.

### 4b — Author `/opt/trading/deploy/.env`

```bash
cat > /opt/trading/deploy/.env <<EOF
GHCR_OWNER=shaanyp123
RELEASE_SHA=$(cd /opt/trading && git rev-parse --short HEAD)
ENVIRONMENT=paper
DOMAIN=spratcapital.com
ACME_EMAIL=shaanrpatel2@gmail.com
WATCHDOG_IP=188.245.37.16
SOPS_AGE_KEY_FILE=/etc/credstore.encrypted/age_key
ENV_FILE_NAME=paper.enc.yaml
POSTGRES_SUPERUSER_PASSWORD=$(openssl rand -hex 32)
SECRETS_DIR=/opt/trading/secrets-decrypted
API_LOG_LEVEL=INFO
EOF
chmod 0400 /opt/trading/deploy/.env
```

The `POSTGRES_SUPERUSER_PASSWORD` is a one-time bootstrap secret used only
for the postgres container's built-in `postgres` superuser. The app-level
roles (`app_service`, `app_owner`) authenticate with passwords from sops.

## Step 5 — Run the bringup script

This is the actual deploy. The script is **idempotent** — re-run it any
time (after a code update, after a reboot, after a config change) and it
will only do work that needs doing.

```bash
cd /opt/trading
bash deploy/day5-bringup.sh
```

What it does, in order:

1. Sanity-check `deploy/.env`, sops binary, age key
2. Decrypt sops yaml on the host → `/opt/trading/secrets-decrypted/decrypted.yaml`
3. Build the api image (skip if already cached)
4. Bring up postgres + wait for healthy
5. Run `alembic upgrade head` (idempotent — alembic skips applied migrations)
6. `ALTER ROLE` `app_service` + `app_owner` with sops-stored passwords
7. Bring up api + caddy
8. Print the `SETUP_TOKEN_EMITTED` line — **copy this into 1Password**
9. Verify `/api/health` returns `{"status":"ok","db_connected":true,...}`

The script ends with a green `Day 5 verification gate CLOSED` banner if
everything works. If any step fails, the script exits with a clear error
and the failing step number — paste back to Claude for debug.

## Step 6 — Verify from your laptop (the actual gate)

```bash
curl -fsS https://spratcapital.com/api/health | jq .
```

Expected:

```json
{
  "status": "ok",
  "environment": "paper",
  "version": "<sha>",
  "db_connected": true,
  "checks": [
    {"name": "postgres", "ok": true, "latency_ms": <small>, "detail": null}
  ]
}
```

Plus security headers (HSTS + CSP) added by Caddy.

If https returns 502 or connection refused: Caddy is still acquiring its
Let's Encrypt cert (~30s on first deploy). Wait + retry. If it still fails:

```bash
ssh root@178.156.239.84
docker compose --env-file /opt/trading/deploy/.env logs caddy | tail -30
```

## Step 7 — Watchdog reset (closes Day 4 carry-over)

The Hetzner Nuremberg watchdog has been alerting since ~21:00 ET 2026-05-07
because the Ashburn `/api/health` URL didn't resolve. As soon as Caddy is
serving, the next watchdog tick (≤5 min) flips to `check_success: true` and
the email storm self-resolves.

**Do NOT disable the watchdog timer** — its alerting against a real
DNS-failure proves the Resend pipeline. The first `check_success: true`
tick is itself a positive proof point.

## Step 8 — Ashburn ↔ Discord webhook test

Per the [Day 4 close-out entry](../../Docs/decisions-log.md), Cloudflare
blocks Discord webhook POSTs from the Hetzner Nuremberg IP. We don't yet
know about Ashburn. Test once api is up:

```bash
ssh root@178.156.239.84
WEBHOOK_URL=$(SOPS_AGE_KEY_FILE=/etc/credstore.encrypted/age_key \
  sops -d --extract '["discord"]["webhook_urls"]["ops"]' /opt/trading/secrets/paper.enc.yaml)
curl -sS -X POST -H 'content-type: application/json' \
  --user-agent 'trading-day5-test/1.0 (+spratcapital.com)' \
  -d '{"content": "[day5] Ashburn → Discord webhook test"}' \
  "$WEBHOOK_URL"
echo
unset WEBHOOK_URL
```

- `204` (or empty body) → Ashburn IP is NOT blocked; backend Discord channels stay Discord.
- `500/403` → Cloudflare also blocks Ashburn; backend channels migrate to Resend in a follow-up PR.

Append the result to the Day 4 follow-up in [decisions-log.md](../../Docs/decisions-log.md).

---

## Future deploys (after Step 1-4 done once)

```bash
ssh root@178.156.239.84
cd /opt/trading
git fetch origin main
git reset --hard origin/main   # discard any local docker-compose edits
bash deploy/day5-bringup.sh
```

That's it.

## Summary of secrets touched

| Secret | Where | Source |
|---|---|---|
| `POSTGRES_SUPERUSER_PASSWORD` | `deploy/.env` (VPS only) | `openssl rand -hex 32` at deploy |
| `postgres.app_service_password` | `secrets/paper.enc.yaml` | `openssl rand -hex 32` at Step 4a |
| `postgres.app_owner_password` | `secrets/paper.enc.yaml` | `openssl rand -hex 32` at Step 4a |
| Owner setup token | `1Password` (Secure Note) | api emits at Step 5 |

## Annual maintenance

- **2027-05-05** — rotate `paper` age key (existing reminder); renew
  `POSTGRES_SUPERUSER_PASSWORD` opportunistically at the same time.
