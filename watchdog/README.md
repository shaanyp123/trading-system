# `watchdog/` — External Watchdog (Hetzner Nuremberg)

Stdlib-only Python script + systemd timer that monitors the Trading System
backend. Runs every 5 minutes; on 3 consecutive failures (≈ 15 minutes
unreachable) it alerts the operator via Discord `#critical` webhook (and
Resend email, when that path is enabled — deferred to Phase 1; see below).

Per `Docs/backend-spec.md` §1.6 + §2.12 the watchdog is **alert-only** — no
authority to halt or modify backend state.

**Phase 0 = Discord-only.** Per `Docs/decisions-log.md` 2026-05-06, Resend
email alerting is deferred until Phase 1. Rationale: during paper trading
the operator is heavily monitoring Discord directly; a single reliable
channel is sufficient and avoids the Resend account / DNS-verification setup
on Day 4. The watchdog code is fully Resend-ready — flipping the email path
on later is a `sops secrets/paper.enc.yaml` edit + `systemctl restart` away.
See "Adding Resend later" at the bottom of this file.

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
- [ ] Discord `#critical` webhook URL is filled in `secrets/paper.enc.yaml`
      under `discord.webhook_urls.critical` (resolved Day 2 via PR #11; verify
      via `sops -d --extract '["discord"]["webhook_urls"]["critical"]' secrets/paper.enc.yaml`).
      **This is the one alert channel Phase 0 relies on.**

> Resend (email) is **not required** for Phase 0 deploy. The watchdog runs
> Discord-only by default; see "Adding Resend later" at the bottom of this
> file when Phase 1 is approaching.

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

### Step 4 — Create the watchdog env file (Discord-only Phase 0)

The env file holds the Discord webhook + health-URL config. Source the
Discord webhook from `secrets/paper.enc.yaml` on your laptop:

```bash
sops -d --extract '["discord"]["webhook_urls"]["critical"]' secrets/paper.enc.yaml
```

On the VPS, write the file (replace `<discord-critical-webhook>` with the
decrypted value above):

```bash
ssh root@188.245.37.16
cat > /opt/trading-watchdog/watchdog.env <<'EOF'
WATCHDOG_HEALTH_URL=https://paper.spratcapital.com/api/health
WATCHDOG_OPERATOR_EMAIL=shaanrpatel2@gmail.com
WATCHDOG_DISCORD_WEBHOOK_URL=<discord-critical-webhook>
WATCHDOG_ID=hetzner-nuremberg-1
WATCHDOG_STATE_PATH=/var/lib/trading-watchdog/state.json

# Phase 0: Resend email path is deferred — leave these unset (or empty).
# When you provision Resend later, fill these and `systemctl restart trading-watchdog.service`.
# WATCHDOG_RESEND_API_KEY=
# WATCHDOG_RESEND_FROM=
EOF

chown root:trading-watchdog /opt/trading-watchdog/watchdog.env
chmod 0640 /opt/trading-watchdog/watchdog.env
```

`WATCHDOG_OPERATOR_EMAIL` is required even in Discord-only mode — it's a
no-cost placeholder used when/if the email path is later enabled. Use the
operator's gmail.

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

To confirm Discord actually delivers, force a 3-strike alert by pointing the
watchdog at a URL guaranteed to 503:

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
#   - Discord #critical channel has a new message starting with "[CRITICAL]"
#   - (Email skipped: Phase 0 = Discord-only)

# Restore real config + reset state
mv /opt/trading-watchdog/watchdog.env.bak /opt/trading-watchdog/watchdog.env
rm -f /var/lib/trading-watchdog/state.json
```

### Step 8 — Capture artifacts for the audit trail

Note these for the Day 4 close-out entry in `Docs/decisions-log.md`:

- VPS hostname + static IP (should match Day 1 capture: `188.245.37.16`)
- systemd timer first-fire timestamp (from `systemctl list-timers`)
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

- **Discord webhook URL** — Discord channel settings → Integrations →
  Webhooks → regenerate; update `discord.webhook_urls.critical` in
  `secrets/paper.enc.yaml`; re-run Step 4 to refresh the VPS env file.
- **Resend API key** — only relevant once email path is enabled (see "Adding
  Resend later" below). Rotate via Resend dashboard; update
  `secrets/paper.enc.yaml`; re-run Step 4.
- **Annual age key rotation (2027-05-05)** — covered by the existing rotation
  cadence (`Docs/decisions-log.md` Day 2). Re-encrypts the secrets; nothing
  watchdog-specific to do.

### Adding Resend later (Phase 1)

Phase 0 ships Discord-only on the operational decision that one reliable
alert channel is enough during heavy paper-trading monitoring. To add Resend
email when Phase 1 approaches:

1. Sign up at [resend.com](https://resend.com) (free tier ≥ 3,000
   emails/month is sufficient for alert traffic).
2. Verify the sender domain in Resend's dashboard (DNS TXT records on
   `spratcapital.com` via Cloudflare).
3. Generate an API key and fill `resend.api_key` + `resend.from_address` in
   `secrets/paper.enc.yaml` via `sops secrets/paper.enc.yaml`.
4. SSH into the Nuremberg VPS, edit `/opt/trading-watchdog/watchdog.env`,
   uncomment + populate the two `WATCHDOG_RESEND_*` lines.
5. `systemctl restart trading-watchdog.service` (no daemon-reload needed —
   only the env file changed).
6. Re-run Step 7 to verify the email path delivers; both Discord and email
   should fire on the 3rd forced failure.

No code change required. The watchdog already reads both env vars on every
tick; the email path activates the moment they're populated.

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
| Discord webhook revoked | `discord_sent=false`, `discord_error` logged in journal | systemd journal is your only signal until you rotate the webhook. **In Phase 0 = Discord-only mode this is the single point of failure for alerting; check `journalctl -u trading-watchdog` daily during paper trading.** |
| Resend API returns 5xx (only relevant if email path is enabled) | `email_sent=false`, `email_error` logged; Discord still attempts | Discord still alerts you; Resend retry next tick |
| Watchdog VPS itself unreachable | Backend can't see the watchdog's `/api/internal/watchdog` push (when wired Week 5+) → backend will eventually emit `watchdog_unreachable` defensive-envelope trigger per §1.6 + §2.12 | Operator notices via the backend's own degradation path |
| Both alert channels fail | systemd journal still records the failure on this VPS | If the backend is unreachable AND alert channels are down, your only signal is operator habit (daily review / direct check). This is the residual risk of running a single watchdog. Phase 1 mitigation: add Resend as second channel (see "Adding Resend later"). |

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
