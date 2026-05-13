# Discord Bot — Operator Runbook

Operator runbook for the `services/discord_bot/` container. Day 23 ships
the Phase 0 surface: `/positions` + `/halt` + `/status` slash commands
on the operator's Discord guild. This runbook satisfies dev-guide §6.8
alternative (b) for A27 (third-party platform contract — Discord).

The bot ↔ api auth model is **shared sops-decrypted Bearer token** per
backend-spec §6.6 + §4.4. The bot calls `http://api:8000/api/...` over
the `trading_internal` Docker network with `Authorization: Bearer
<token>`; the api validates via `services/api/middleware.BotAuthMiddleware`
and on match injects a service-account `SessionContext` (username
`discord-bot`, role `owner`, auth_strength `strong`).

---

## What you're producing (Day 23 carryover or Day 24 ceremony)

| Step | Artifact | Sensitivity |
| --- | --- | --- |
| 1 | `discord.api_bearer_token` value (one secret, deployed twice — bot + api) | **Critical** — service-account auth |
| 2 | Updated `secrets/paper.enc.yaml` (encrypted; committed to repo) | Encrypted |
| 3 | Bot container running on Hetzner Ashburn | — |
| 4 | Live smoke: `/status` in the dev guild returns the api health embed | — |

The bot token + guild ID + 7 channel IDs were captured during Day 2
(`deploy/discord/README.md` Steps 1–4) and live in
`secrets/paper.enc.yaml` `discord.bot_token` + `discord.guild_id`. Day 23
adds ONE new key (`discord.api_bearer_token`) to that file.

---

## Prereqs

- sops + age set up per `deploy/sops/README.md`. `SOPS_AGE_KEY_FILE`
  exported in your shell.
- VPS has the `services/api/Dockerfile` rebuilt from a commit at or after
  Day 23 PR merge (the api needs `BotAuthMiddleware` + the new
  `discord_bot_bearer_token` config field; the api-side mapping in
  `services/api/entrypoint.py` reads `discord.api_bearer_token` from the
  sops bundle and exports it as `API_DISCORD_BOT_BEARER_TOKEN`).
- Discord bot already invited to the operator's guild (Day 2
  `deploy/discord/README.md` Step 4).

---

## Step 1 — Generate the shared bearer token

The bot ↔ api bearer is a fresh secret minted at runbook execution. It
should be 32+ random bytes encoded as URL-safe base64 (matches the
watchdog bearer + setup-token format precedent).

On the operator laptop (NOT a shared shell — this value goes into sops
in Step 2):

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
# Example output (yours will differ):
# ZQ4bU7sJWxvI5pKj8Kj5K8j8Kj5K8j8Kj5K8j8Kj5K8
```

Copy the value to your clipboard. It will appear under the `discord:`
key in sops in Step 2.

**Do NOT paste this token into chat, this README, or any commit.** The
gitleaks CI gate scans for high-entropy tokens; the operator's
discipline is to never have one on disk outside the encrypted env files.
(See `Docs/decisions-log.md` 2026-05-05 QC token incident for the
template.)

---

## Step 2 — Add the bearer to sops (paper env)

```bash
# Open paper.enc.yaml for in-place edit (sops decrypts → editor → re-encrypts on save)
sops secrets/paper.enc.yaml
```

Add the new key under the existing `discord:` block:

```yaml
discord:
  bot_token: <existing value from Day 2>
  guild_id: <existing value from Day 2>
  webhook_urls:
    daily_brief: <existing value>
    signals:     <existing value>
    fills:       <existing value>
    alerts:      <existing value>
    critical:    <existing value>
    ops:         <existing value>
    audit:       <existing value>
  api_bearer_token: <paste your Step 1 value here>      # NEW for Day 23
```

Save + exit your editor (sops re-encrypts on save). Verify the
encrypted file looks intact:

```bash
sops -d secrets/paper.enc.yaml | grep -E 'api_bearer_token|bot_token|guild_id' | head -5
# Should print 3 lines (decrypt round-trip works; values are visible to
# you locally because you have the age key).
```

Commit the encrypted file (the plaintext NEVER leaves your laptop):

```bash
git add secrets/paper.enc.yaml
git commit -m "chore(secrets): add discord.api_bearer_token for Day 23 bot ↔ api auth"
git push
```

If you also operate `live-small` / `live-scale` envs, repeat Step 2 for
`secrets/live.enc.yaml`.

---

## Step 3 — Deploy to Hetzner Ashburn

SSH-delegated path (mirrors the Day 12 → 14 → 15 → 16 → 17 → 20
carryover pattern). On the VPS:

```bash
cd /opt/trading
git pull --ff-only

# Re-decrypt the sops bundle so the new api_bearer_token lands in
# /opt/trading/secrets-decrypted/decrypted.yaml. Mirror the
# day5-bringup.sh / Day-21-style decrypt step:
sops -d secrets/paper.enc.yaml > /opt/trading/secrets-decrypted/decrypted.yaml
chmod 600 /opt/trading/secrets-decrypted/decrypted.yaml

# Rebuild the api (picks up BotAuthMiddleware + entrypoint.py new mapping)
docker compose --env-file deploy/.env build api

# Build the bot image (Day 23 first time)
docker compose --env-file deploy/.env build discord_bot

# Restart api so it picks up the new API_DISCORD_BOT_BEARER_TOKEN env
# var, then start the bot:
docker compose --env-file deploy/.env up -d api discord_bot

# Watch the bot connect to the gateway:
docker compose --env-file deploy/.env logs -f discord_bot
# Expected log lines (in order):
#   discord_bot_starting environment=paper api_base_url=http://api:8000 guild_id=<your-id>
#   discord_commands_synced guild_id=<your-id> command_count=3 commands=['halt', 'positions', 'status']
#   discord_bot_ready bot_user=<bot-name>#<discriminator> guild_id=<your-id>
```

If `discord_commands_synced` fails to log within ~10s, check:
- `docker compose logs discord_bot 2>&1 | grep -i "login_failed\|notfound"` — the bot token may be invalid OR the guild ID wrong.
- Discord developer portal → your application → Bot tab → confirm token matches what's in sops.
- Re-invite the bot to your guild via the Day 2 OAuth URL if `NotFound` fires on `tree.sync`.

---

## Step 4 — Live smoke (Discord client — operator on phone or laptop)

Open Discord, navigate to your `trading-system` guild, click into the
`#ops` channel.

### `/status` — the canonical Day-23 verification gate (IG line 393)

1. Type `/status` and hit Enter.
2. Bot should reply with an ephemeral embed (only you can see it):
   ```
   Status — paper · HH:MM ET
   Status: ok
   Version: <git sha or build tag>
   DB connected: true
   Checks:
     ✅ postgres — <latency> ms
   ```
3. The reply should arrive within ~2 seconds. If it takes >3s the bot's
   internal defer fires and you see "Bot is thinking..." first.

If `/status` returns an embed with `❌ /status failed — BOT_AUTH_INVALID`,
the bot's bearer token doesn't match the api's. Re-run Step 2 + Step 3
ensuring you pasted the same value into sops AND that the bot's
container was restarted AFTER the bearer landed in
`/opt/trading/secrets-decrypted/decrypted.yaml`.

### `/positions`

1. Type `/positions` and hit Enter.
2. Phase-0 expected reply:
   ```
   Open positions (0) — paper · HH:MM ET
   No open positions.
   _(Phase 0 baseline — positions populate after the Week 4 Wed dispatcher
   wires fills from QC ObjectStore.)_
   ```

### `/halt` — confirm-button flow (do NOT confirm in production!)

1. Type `/halt test halt from Day 23 carryover smoke` and hit Enter.
2. Bot replies with an ephemeral confirmation embed + ✓ Confirm halt /
   ✗ Cancel buttons.
3. Click **✗ Cancel**. The embed updates to "Halt cancelled. System
   unchanged. No api call was made."
4. Re-issue `/halt test confirm path`.
5. This time click **✓ Confirm halt** (Phase-0 it's safe — the api's
   handler is 501-stubbed, no real state change).
6. Bot replies with:
   ```
   ⚠️ Kill-switch handler not yet wired
   The api accepted the request body but the kill-switch handler is
   still 501-stubbed. ...
   ```
7. Verify the api log on the VPS shows the request landed:
   ```bash
   docker compose --env-file deploy/.env logs api 2>&1 | grep kill_switch_invoke_stubbed | tail -1
   # Should show: kill_switch_invoke_stubbed trigger=manual_judgment reason='test confirm path'
   ```

This roundtrip closes the IG §3 Week 6 verification gate box 3 ("In
Discord: type `/positions` → bot responds with positions embed (empty
or mock data)") + extra coverage of the `/halt` confirm flow + the
501-stub error embed UX.

### `/approve <signal_id>` — confirm-button flow

Operator workflow: when a signal lands in `#signals`, copy the signal_id
from the embed footer (formatted UUID with hyphens), then run `/approve <id>`
in any channel.

#### Bad-input verification (no api call)

1. Type `/approve not-a-uuid` and hit Enter.
2. Bot replies ephemerally with:
   ```
   ❌ Invalid signal_id
   `not-a-uuid` doesn't look like a UUID.
   Copy the signal_id from the #signals channel embed footer
   (format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx).
   ```
3. No api round-trip; verify via api logs:
   ```bash
   docker compose --env-file deploy/.env logs api 2>&1 | grep "POST /api/signals" | tail -3
   # No new line for the bad-UUID attempt.
   ```

#### Stale-UUID verification (api round-trip → SIGNAL_NOT_FOUND)

1. Type `/approve 00000000-0000-0000-0000-000000000000` and hit Enter.
2. Bot replies with ephemeral confirm embed + ✓ Approve / ✗ Cancel.
3. Click **✓ Approve**.
4. Bot edits the message to:
   ```
   ❌ Signal not found
   The api doesn't have a signal with that ID. ...
   ```

#### Happy-path verification (requires a real pending signal)

If LEAN has emitted a signal and `#signals` shows an embed:

1. Copy the signal_id from the `#signals` embed footer.
2. Type `/approve <signal_id>` and hit Enter.
3. Bot replies with confirm embed; click **✓ Approve**.
4. Bot edits to:
   ```
   ✅ Signal approved — <first-8-chars>
   Status: approved
   Audit: <audit_event_uuid> (seq #<N>)
   OrderPlacementWorker will forward to IBKR within ~5s. Watch #fills
   for the broker confirmation embed.
   ```
5. Within ~5s, `#fills` channel should receive a fill embed (assuming
   IBKR accepts the order).
6. Verify api log:
   ```bash
   docker compose --env-file deploy/.env logs api 2>&1 | grep "signal_approve_processed" | tail -1
   ```

#### Cancel-path verification

1. Type `/approve <any-real-pending-signal-id>` and hit Enter.
2. Click **✗ Cancel**.
3. Bot edits to "Approve cancelled. System unchanged. No api call was made."
4. Verify the signal stays `status='pending'`:
   ```sql
   SELECT id, status FROM signals WHERE id = '<signal_id>';
   ```

---

## Step 5 — Cleanup

Nothing to clean up — the bot stays running. The Day 23 carryover
deferred VPS deploy lands HERE; from this point forward the bot is
part of the production stack.

If you need to take the bot offline temporarily:

```bash
docker compose --env-file deploy/.env stop discord_bot
# To resume:
docker compose --env-file deploy/.env start discord_bot
```

---

## Token rotation procedure

Quarterly per backend-spec §6.6 ("rotated quarterly with 1h overlap
window"). The rotation is two-step:

1. Mint a new value via Step 1.
2. Update `discord.api_bearer_token` in `secrets/paper.enc.yaml` (and
   `live.enc.yaml`).
3. Deploy api + bot together via Step 3 (single `docker compose up -d`
   restart for both).
4. Verify via Step 4 `/status`.

Because the bot + api restart together with the new token, there's no
overlap window in Phase 0 (single deployment unit). Phase 1+ may
implement true overlap when the bot moves to a separate cluster.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Bot logs `discord_bot_login_failed` | Wrong `discord.bot_token` | Re-mint in Discord developer portal → update sops → restart bot |
| Bot logs `tree.sync ... NotFound` | Bot not invited to guild OR wrong `guild_id` | Re-invite via Day 2 OAuth URL; verify guild ID |
| `/status` returns `BOT_AUTH_INVALID` | Bot's `api_bearer_token` ≠ api's | Re-run Step 2 + Step 3, ensure both sides got the same value |
| `/status` returns `CSRF_REJECTED` | Bot is sending POST without bearer (config bug) | Check `docker compose logs discord_bot` for missing-token errors at startup |
| `/halt` 501 KILL_SWITCH_HANDLER_NOT_WIRED | Expected (Day 15 stub; Week 4 Wed dispatcher wires the real handler) | No action — this is the canonical Phase-0 response |
| `/positions` returns empty list | Expected (Phase 0; positions_current is empty until Week 4 Wed dispatcher) | No action |
| Bot doesn't appear in member list | OAuth invite never ran | Re-execute Day 2 `deploy/discord/README.md` Step 4 |

---

## Files in this directory

| Path | Purpose |
| --- | --- |
| `README.md` (this file) | Operator click-by-click runbook. |

The bot's source code lives at `services/discord_bot/`; the api-side
auth middleware at `services/api/middleware.py:BotAuthMiddleware`; the
shared bearer-token sops key at `secrets/<env>.enc.yaml` `discord.api_bearer_token`.

---

## Notes for future Claude sessions

- Token rotation is a sops-edit + restart, no code change.
- If Phase 1 adds new slash commands, register them in
  `services/discord_bot/main.py:_register_commands` — the gateway
  resync happens automatically on next `setup_hook`.
- Phase 1 channel push paths (`#daily-brief`, `#signals`, `#fills`,
  `#alerts`, `#critical`, `#ops`, `#audit`) are NOT served by the
  Day 23 bot — they require the Phase-1 backend → bot push path
  (`POST /internal/discord/post`) which lands when the Week 4 Wed
  dispatcher + Phase-1 channel-fanout PR ships.
- Bot lives on `trading_internal + trading_egress` networks. NEVER
  expose the bot's HTTP listener to the public internet (the Phase 1
  IPC listener will bind to 0.0.0.0 ON THE INTERNAL NETWORK ONLY —
  Caddy must NOT proxy it).
