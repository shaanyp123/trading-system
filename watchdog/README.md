# `watchdog/` — External Watchdog (Hetzner Nuremberg)

Stdlib-only Python script + systemd timer that monitors the Trading System
backend. Runs every 5 minutes; on 3 consecutive failures (≈ 15 minutes
unreachable) it alerts the operator via **Resend email** (primary) and
**Discord `#critical` webhook** (best-effort).

Per `Docs/backend-spec.md` §1.6 + §2.12 the watchdog is **alert-only** — no
authority to halt or modify backend state.

**Phase 0 alert channel: Resend (primary), Discord (best-effort).** Per
`Docs/decisions-log.md` 2026-05-07, Discord webhook POSTs from this VPS's
Hetzner Nuremberg IP are blocked at the Cloudflare WAF — even with proper
User-Agent. The webhook URL itself is valid (laptop curl from a residential
IP delivers fine), but Cloudflare's threat-intelligence layer treats Hetzner
data-center IPs as suspicious and returns 500 (or 403 in some configurations).

So Phase 0 ships Resend as the working alert channel. The watchdog still
attempts Discord on every alert — they're independent in `run_once`, so a
Discord failure can't block the email path — but Discord delivery is
best-effort. If Cloudflare relaxes their stance on Hetzner IPs (or if you
later move the watchdog to a different provider), Discord will start
delivering automatically with no code change.

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
- [ ] **Resend account provisioned** with `spratcapital.com` verified as a
      sending domain, API key generated, and `resend.api_key` +
      `resend.from_address` filled in `secrets/paper.enc.yaml`. Verify:
      `sops -d --extract '["resend"]["api_key"]' secrets/paper.enc.yaml`
      should print a `re_xxx...` value (not the placeholder string).
- [ ] Discord `#critical` webhook URL is also in `secrets/paper.enc.yaml`
      (Day 2 PR #11). Phase 0 attempts Discord on every alert as a
      best-effort secondary; it usually returns 500 from Hetzner Nuremberg
      due to Cloudflare-blocking the data-center IP range, but the watchdog
      still tries (channel-isolation guarantees Discord failures don't block
      the email path).

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

The env file holds the Resend + Discord + health-URL config. Source values
from `secrets/paper.enc.yaml` on your laptop:

```bash
sops -d --extract '["resend"]["api_key"]'                     secrets/paper.enc.yaml
sops -d --extract '["resend"]["from_address"]'                secrets/paper.enc.yaml
sops -d --extract '["discord"]["webhook_urls"]["critical"]'   secrets/paper.enc.yaml
```

On the VPS, write the file (replace `<...>` placeholders with the decrypted
values above):

```bash
ssh root@188.245.37.16
cat > /opt/trading-watchdog/watchdog.env <<'EOF'
# Phase 0 paper trading runs on the apex domain; the architectural plan
# splits Phase 1 into apex (live) + paper.<your-domain> (staging) but
# until that split lands, point at the apex.
WATCHDOG_HEALTH_URL=https://spratcapital.com/api/health
WATCHDOG_OPERATOR_EMAIL=shaanrpatel2@gmail.com
WATCHDOG_DISCORD_WEBHOOK_URL=<discord-critical-webhook>
WATCHDOG_RESEND_API_KEY=<resend-api-key>
WATCHDOG_RESEND_FROM=<resend-from-address>
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

### Step 5 — Enable the systemd timer

> ⚠️ **Do this BEFORE the manual smoke test.** systemd's
> `StateDirectory=trading-watchdog` setting creates `/var/lib/trading-watchdog/`
> with the right ownership on first service activation. Running the script
> manually before enabling the timer fails with `PermissionError` because
> the directory doesn't exist yet.

```bash
systemctl daemon-reload
systemctl enable --now trading-watchdog.timer

# Confirm timer is scheduled
systemctl list-timers trading-watchdog.timer
# Expect: NEXT shows ~5 min from now; LAST shows the boot-time first tick
```

The first systemd-fired tick runs immediately (`OnBootSec=1min`); after that,
every 5 minutes. Each tick exits cleanly — the service is `Type=oneshot`.

### Step 6 — Smoke-test the script manually

Now that systemd has created the state directory, the manual run works:

```bash
sudo -u trading-watchdog \
  env $(grep -v '^#' /opt/trading-watchdog/watchdog.env | xargs) \
  python3 /opt/trading-watchdog/watchdog.py
```

Expected output: a single JSON line on stdout. After Day 5's Caddy + api
brought up `https://spratcapital.com/api/health`, a happy tick looks like:

```json
{"timestamp_utc": "...", "level": "info", "service_name": "watchdog",
 "event": "watchdog_tick_completed", "watchdog_id": "hetzner-nuremberg-1",
 "health_url": "https://spratcapital.com/api/health",
 "check_success": true, "check_status_code": 200, "check_error": null,
 "consecutive_failures": 0, "decision_reason": "check ok",
 "email_sent": false, "email_error": null,
 "discord_sent": false, "discord_error": null}
```

Before Day 5 (deploy not done yet), every tick reports
`check_success: false` with `URLError [Errno -2] Name or service not
known`. That's expected on the pre-deploy timeline.

After the smoke test, reset the counter so the timer starts fresh:

```bash
rm -f /var/lib/trading-watchdog/state.json
```

### Step 7 — Forced-503 alert wiring test

To confirm the Resend (and best-effort Discord) alert path actually fires,
run 3 ticks back-to-back against a URL guaranteed to 503.

> ⚠️ **Lesson from Day 4 deploy (2026-05-07 → 2026-05-08):** the original
> version of this step `sed`-ed the URL into `watchdog.env` and required
> a manual `mv` to restore. The operator missed the restore step and the
> systemd timer kept ticking against the test sentinel for ~7 hours,
> generating an email storm (88 consecutive failures, ~7 emails fired
> via the 60-min cooldown). The pattern below uses an **inline env-var
> override** that lives only for the test ticks — the canonical
> `watchdog.env` file is never modified, so there's nothing to restore
> and nothing to forget.

```bash
ssh root@188.245.37.16

# The override URL is supplied AFTER the env-file load so it shadows
# the file's value FOR THIS INVOCATION ONLY. The file itself stays
# pointed at the real URL the whole time; the systemd timer's next tick
# (5 min later) automatically uses the real URL again.
for i in 1 2 3; do
  echo "=== Tick $i ==="
  sudo -u trading-watchdog \
    env $(grep -v '^#' /opt/trading-watchdog/watchdog.env | grep -v '^WATCHDOG_HEALTH_URL=' | xargs) \
    WATCHDOG_HEALTH_URL=https://httpbin.org/status/503 \
    python3 /opt/trading-watchdog/watchdog.py
done

# Reset state so the next legitimate tick starts the failure counter at 0:
rm -f /var/lib/trading-watchdog/state.json

# Sanity-check the canonical URL is still set correctly (paranoia after the
# Day 4 incident — verify the env file was never accidentally mutated):
grep WATCHDOG_HEALTH_URL /opt/trading-watchdog/watchdog.env
# Expected: WATCHDOG_HEALTH_URL=https://spratcapital.com/api/health
# If you see anything else (httpbin.org, paper.spratcapital.com, etc.),
# fix immediately and reset state again.
```

Tick 3 should show `"email_sent": true, "email_error": null` and an email
will arrive in the operator's inbox with subject `[CRITICAL] Trading System
unreachable — 3 consecutive failures from hetzner-nuremberg-1`.

Discord on tick 3 will likely show `"discord_sent": false, "discord_error":
"discord send failed: HTTP Error 500: Internal Server Error"` — that's
Cloudflare blocking Hetzner data-center IPs, not a bug. The email path
delivers regardless thanks to channel isolation in `run_once`.

### Step 8 — Capture artifacts for the audit trail

Note these for the watchdog close-out entry in `Docs/decisions-log.md`:

- VPS static IP (should match Day 1 capture: `188.245.37.16`)
- systemd timer first-fire timestamp (from Step 5's `list-timers`)
- Resend test-alert email subject + message-id (from Resend dashboard logs;
  optional — Gmail receipt is sufficient evidence)
- Tick 3 JSON output from Step 7 (showing `email_sent: true`)

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

- **Resend API key** — primary alert channel for Phase 0. Rotate via the
  Resend dashboard → API Keys → Revoke + create new. Update
  `secrets/paper.enc.yaml` via `sops`. Re-run Step 4 to refresh the VPS
  env file. `systemctl restart trading-watchdog.service` (no daemon-reload
  needed — only the env file changed).
- **Discord webhook URL** — best-effort secondary; mostly blocked from this
  VPS at Cloudflare anyway. Rotate via Discord channel settings →
  Integrations → Webhooks → regenerate. Update `secrets/paper.enc.yaml`,
  re-run Step 4. The watchdog will still attempt to deliver and still fail
  with 500; restoring it to working state requires Cloudflare's IP
  reputation to relax (out of our control) or moving the watchdog to a
  different IP block.
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
| Resend API returns 5xx | `email_sent=false`, `email_error` logged; Discord still attempts (best-effort) | Resend retries on next tick. Check Resend dashboard for service status. |
| Resend API key revoked / quota exceeded | Same as above; `email_error` will say `401` or `429` | Rotate API key (see Rotating credentials above) or upgrade Resend plan. |
| Discord webhook returns 500 (Cloudflare IP block) | `discord_sent=false`, `discord_error` logged; **Resend email still delivers** | Expected during Phase 0 — no action. The email path is the canonical alert; Discord is best-effort. |
| Discord webhook revoked | `discord_sent=false`, `discord_error: "Unknown Webhook"` | Same as above — no action needed since Discord is best-effort here. Optionally rotate the webhook to clean up the journal. |
| Watchdog VPS itself unreachable | Backend can't see the watchdog's `/api/internal/watchdog` push (when wired Week 5+) → backend will eventually emit `watchdog_unreachable` defensive-envelope trigger per §1.6 + §2.12 | Operator notices via the backend's own degradation path |
| Both alert channels fail | systemd journal still records the failure on this VPS | If the backend is unreachable AND email is also down, your only signal is operator habit (daily review / direct check). This is the residual risk of relying on a single email provider. Phase 2 hardening could add a second SMTP provider (Postmark, SES) as a third channel. |

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
