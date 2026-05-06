# `watchdog/` — External Watchdog (Hetzner Nuremberg)

Stdlib-only Python script + systemd timer that monitors the Trading System
backend. Runs every 5 minutes; on 3 consecutive failures (≈ 15 minutes
unreachable) it alerts the operator via Resend email and the Discord
`#critical` webhook.

Per `Docs/backend-spec.md` §1.6 + §2.12 the watchdog is **alert-only** — no
authority to halt or modify backend state. Three independent alert channels
(Resend, Discord, systemd journal on a separate VPS) so a single-vendor
outage on any one doesn't silence the alarm.

## Files

| File | Purpose |
|---|---|
| `watchdog.py` | The script. Stdlib only. Importable for tests. |
| `trading-watchdog.service` | systemd one-shot unit (User/Group, hardening, EnvironmentFile) |
| `trading-watchdog.timer` | systemd timer — `OnUnitActiveSec=5min`, `RandomizedDelaySec=30s` |
| `README.md` | This file — Nuremberg VPS deploy runbook + ops |

## Cross-references

- Topology + region rationale: `Docs/backend-spec.md` §1.6 (note the
  Falkenstein → Nuremberg deviation in `Docs/decisions-log.md` 2026-05-05).
- VPS sizing (CX23 / Ubuntu 24.04 LTS): `Docs/backend-spec.md` §1.6 + §9.1.2.
- Watchdog secret schema (`internal.watchdog_bearer_token`, `resend.*`,
  `discord.webhook_urls.critical`): `deploy/sops/secret_schemas/paper.template.yaml`.
- Tests: `tests/unit/test_watchdog.py` (31 cases covering threshold,
  cooldown, state IO, payload shape, env-var fail-closed).

---

## Day 4 13:00 — Operator runbook: deploy to Hetzner Nuremberg VPS

**Pre-flight** (assumes the VPS is already provisioned per Day 1):

- [ ] You can SSH into the Nuremberg VPS as root: `ssh root@188.245.37.16`
      (the static IP captured in `Docs/decisions-log.md` 2026-05-05 entry).
- [ ] Ubuntu 24.04 LTS is up to date: `apt update && apt -y upgrade`.
- [ ] You have `secrets/paper.enc.yaml` decryptable on your laptop
      (`sops -d secrets/paper.enc.yaml | head` returns plaintext).
- [ ] **Resend API key is filled in `secrets/paper.enc.yaml`** under
      `resend.api_key`. If still placeholder (`<TODO_FROM_DAY_3_RESEND_PROVISION>`),
      provision Resend now: sign up at [resend.com](https://resend.com), grab
      an API key from the dashboard, and `sops secrets/paper.enc.yaml` to fill
      it. Without this, the watchdog deploys but email alerts won't fire (only
      Discord will).

### Step 1 — Create the watchdog system user

```bash
ssh root@188.245.37.16
useradd --system --home-dir /opt/trading-watchdog --create-home \
        --shell /usr/sbin/nologin trading-watchdog
```

### Step 2 — Copy the script + systemd units

From your laptop, in this repo's worktree:

```bash
WATCHDOG_HOST=root@188.245.37.16

# The Python script + systemd files
scp watchdog/watchdog.py "$WATCHDOG_HOST":/opt/trading-watchdog/watchdog.py
scp watchdog/trading-watchdog.service "$WATCHDOG_HOST":/etc/systemd/system/
scp watchdog/trading-watchdog.timer   "$WATCHDOG_HOST":/etc/systemd/system/
```

On the VPS:

```bash
chown trading-watchdog:trading-watchdog /opt/trading-watchdog/watchdog.py
chmod 0750 /opt/trading-watchdog/watchdog.py
```

### Step 3 — Verify Python on the VPS

The script needs Python 3.11+ (Ubuntu 24.04 ships 3.12 by default). Confirm:

```bash
ssh root@188.245.37.16 "python3 --version"
# Expect: Python 3.12.x (or later)
```

If older: `apt install -y python3.12`.

### Step 4 — Create the watchdog env file

The env file holds Resend / Discord / health-URL config. Source values from
`secrets/paper.enc.yaml` on your laptop, paste into the VPS file via
`ssh + cat - > /opt/trading-watchdog/watchdog.env`.

On your laptop, decrypt the relevant fields:

```bash
sops -d --extract '["resend"]["api_key"]'                  secrets/paper.enc.yaml
sops -d --extract '["resend"]["from_address"]'             secrets/paper.enc.yaml
sops -d --extract '["discord"]["webhook_urls"]["critical"]' secrets/paper.enc.yaml
sops -d --extract '["internal"]["watchdog_bearer_token"]'  secrets/paper.enc.yaml
```

On the VPS, write the file (replace `<...>` with the decrypted values):

```bash
ssh root@188.245.37.16
cat > /opt/trading-watchdog/watchdog.env <<'EOF'
WATCHDOG_HEALTH_URL=https://paper.spratcapital.com/api/health
WATCHDOG_OPERATOR_EMAIL=shaanrpatel2@gmail.com
WATCHDOG_RESEND_API_KEY=<resend-api-key-from-sops>
WATCHDOG_RESEND_FROM=<resend-from-address-from-sops>
WATCHDOG_DISCORD_WEBHOOK_URL=<discord-critical-webhook-from-sops>
WATCHDOG_ID=hetzner-nuremberg-1
WATCHDOG_STATE_PATH=/var/lib/trading-watchdog/state.json
EOF

chown root:trading-watchdog /opt/trading-watchdog/watchdog.env
chmod 0640 /opt/trading-watchdog/watchdog.env
```

> The `<watchdog-bearer-token>` from sops is intentionally NOT in the env
> file. It's reserved for the future `POST /api/internal/watchdog` push
> endpoint (backend-spec §4.5.3) — not used by Day 4's GET-only watchdog.
> Add when that endpoint lands (Week 5+).

### Step 5 — Smoke-test the script manually

Before enabling the timer, run the script once by hand to catch config issues:

```bash
ssh root@188.245.37.16
sudo -u trading-watchdog \
  env $(grep -v '^#' /opt/trading-watchdog/watchdog.env | xargs) \
  python3 /opt/trading-watchdog/watchdog.py
```

Expected output: a single JSON line on stdout, e.g.:

```json
{"timestamp_utc": "2026-05-06T17:30:00.000+00:00", "level": "info",
 "service_name": "watchdog", "event": "watchdog_tick_completed",
 "watchdog_id": "hetzner-nuremberg-1",
 "health_url": "https://paper.spratcapital.com/api/health",
 "check_success": false, "check_status_code": null,
 "check_error": "URLError [Errno -2] Name or service not known",
 "consecutive_failures": 1, "decision_reason": "failure 1/3 (under threshold)",
 "email_sent": false, "discord_sent": false}
```

(The check will fail until Day 5 brings up `paper.spratcapital.com` — that's
expected. What we're verifying is that the script runs, the env file loads,
and a state file gets written. After the smoke test, reset the counter:)

```bash
rm -f /var/lib/trading-watchdog/state.json
```

### Step 6 — Enable the systemd timer

```bash
ssh root@188.245.37.16
systemctl daemon-reload
systemctl enable --now trading-watchdog.timer

# Confirm timer is scheduled
systemctl list-timers trading-watchdog.timer
# Expect: NEXT shows ~5 min from now; LAST shows the boot-time first tick

# Tail the journal for the next tick
journalctl -u trading-watchdog.service -f
```

### Step 7 — Verify alert wiring (one-time)

To confirm Resend + Discord actually deliver, force a 3-strike alert by pointing
the watchdog at a URL guaranteed to 404:

```bash
ssh root@188.245.37.16

# Override the URL temporarily and run 3 ticks back-to-back
cp /opt/trading-watchdog/watchdog.env /opt/trading-watchdog/watchdog.env.bak
sed -i 's|^WATCHDOG_HEALTH_URL=.*|WATCHDOG_HEALTH_URL=https://httpbin.org/status/503|' \
       /opt/trading-watchdog/watchdog.env

# 3 ticks (the 3rd should fire alerts)
for i in 1 2 3; do
  sudo -u trading-watchdog \
    env $(grep -v '^#' /opt/trading-watchdog/watchdog.env | xargs) \
    python3 /opt/trading-watchdog/watchdog.py
done

# Confirm the alert came through:
#   - Email in your inbox (subject starts with "[CRITICAL]")
#   - Discord #critical channel has a new message starting with "[CRITICAL]"

# Restore real config + reset state
mv /opt/trading-watchdog/watchdog.env.bak /opt/trading-watchdog/watchdog.env
rm -f /var/lib/trading-watchdog/state.json
```

### Step 8 — Capture artifacts for the audit trail

Note these for the Day 4 close-out entry in `Docs/decisions-log.md`:

- VPS hostname + static IP (should match Day 1 capture: `188.245.37.16`)
- systemd timer first-fire timestamp (from `systemctl list-timers`)
- Resend test-alert message ID (from your Resend dashboard)
- Discord test-alert message link (from `#critical`)

---

## Operations

### Daily monitoring

The watchdog logs one line per tick to `journalctl -u trading-watchdog`. A
typical happy-path tick:

```
{"timestamp_utc": "...", "event": "watchdog_tick_completed",
 "check_success": true, "check_status_code": 200,
 "consecutive_failures": 0, "decision_reason": "check ok"}
```

If `check_success` is `false` for an unexpected reason, investigate the backend
first (the watchdog itself is rarely the problem).

### Forcing a state reset

If the alert cooldown is suppressing legitimate re-alerts (e.g., the operator
acknowledged + fixed an outage and wants the next failure to alert immediately):

```bash
ssh root@188.245.37.16
rm -f /var/lib/trading-watchdog/state.json
# next tick starts fresh
```

### Rotating credentials

- **Resend API key** — rotate via Resend dashboard, update
  `secrets/paper.enc.yaml`, then re-run Step 4 to refresh the VPS env file.
- **Discord webhook URL** — same flow, Discord channel settings → Integrations
  → Webhooks → regenerate.
- **Annual age key rotation (2027-05-05)** — covered by the existing rotation
  cadence (`Docs/decisions-log.md` Day 2). Re-encrypts the secrets; nothing
  watchdog-specific to do.

### Tuning knobs

These are LOCKED. Any change requires a `Docs/decisions-log.md` entry first:

| Constant | Default | Justification |
|---|---|---|
| `FAILURE_THRESHOLD` | 3 | Backend-spec §1.6 — "alerts at 3 (15 min)" |
| `ALERT_COOLDOWN_MINUTES` | 60 | Avoid alert spam during long outages; long enough that the operator can read + acknowledge before re-alert |
| `HTTP_TIMEOUT_SECONDS` | 10 | A backend that's slower than 10s on `/api/health` is itself a problem worth alerting on |

### Failure modes

| What fails | What the watchdog does | Operator response |
|---|---|---|
| Backend `/api/health` returns 503 | Increment counter; alert at 3 | Investigate backend per spec §6.2 |
| Backend unreachable (DNS / TCP) | Increment counter; alert at 3 | Check Hetzner status page; check Caddy is up |
| Resend API returns 5xx | `email_sent=false`, `email_error` logged; Discord still attempts | Discord still alerts you; Resend retry next tick |
| Discord webhook revoked | `discord_sent=false`, `discord_error` logged; Resend still sends | Email still alerts you; rotate webhook |
| Watchdog VPS itself unreachable | Backend can't see the watchdog's `/api/internal/watchdog` push (when wired Week 5+) → backend will eventually emit `watchdog_unreachable` defensive-envelope trigger per §1.6 + §2.12 | Operator notices via the backend's own degradation path |
| Both Resend + Discord fail | systemd journal still records the failure on this VPS | If the backend is unreachable AND both alert channels are down, your only signal is operator habit (daily review / direct check). This is the residual risk of running a single watchdog. |

### What to do if the watchdog itself misbehaves

The only way the watchdog harms anything is by sending false-positive alerts.
If that happens (e.g., known maintenance window):

```bash
ssh root@188.245.37.16
systemctl stop trading-watchdog.timer
# fix issue; restart
systemctl start trading-watchdog.timer
```

**Do not disable the timer permanently.** A silent watchdog is worse than a
noisy one.
