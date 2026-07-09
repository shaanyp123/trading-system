# Discord — Operator Runbook

Operator guide for creating the Discord server, the seven Phase 0–1 channels, the bot application, and the OAuth invite. The captured tokens + IDs feed into the host secrets file (`/opt/trading-secrets/secrets.yaml`, schema `deploy/secrets.template.yaml`).

This runbook is the **Day 2 14:00** task in `implementation-guide.md` §11. Execution time: ~30 minutes total. The canonical declaration is `manifest.json` in this directory; this file walks through the click-by-click to reproduce that state.

The eighth channel (`#ask-agent`, supporting `/ask` command) is **Phase 2 only** — do NOT create it on Day 2. Spec reference: `frontend-spec.md §1615`.

---

## What you're producing

| Artifact | Where it goes | Sensitivity |
| --- | --- | --- |
| Discord server (guild) | account-bound; only the operator's account is a member | Private |
| 7 text channels | inside the guild | — |
| Bot application | `discord.com/developers/applications` | — |
| Bot token | 1Password (interim) → `secrets/{paper,live}.enc.yaml` `discord.bot_token` (Day 3) | **Critical** — server-wide bot identity |
| Guild ID | not secret, but co-located with bot config | safe to commit |
| 7 channel IDs (or webhook URLs) | `secrets/{paper,live}.enc.yaml` `discord.webhook_urls` | — |

The token is the only sensitive piece. Channel IDs and the guild ID are not secrets; they live alongside only because the host secrets file is the canonical config store.

---

## Step 1 — Create the server (guild)

1. Open https://discord.com in your browser, logged in to your operator account.
2. Click the green **+** in the left sidebar → **Create My Own** → **For me and my friends**.
3. **Server Name:** `trading-system` (or any name you prefer; the `guild_id` is what code uses).
4. Click **Create**. You land in the new server's `#general` channel.
5. (Optional) Delete `#general` once your seven canonical channels exist — keeps the channel list clean.

### Capture the Guild ID

1. **Settings (gear icon)** at bottom-left → **App Settings** → **Advanced** → enable **Developer Mode**.
2. Right-click the server name in the left sidebar → **Copy Server ID**.
3. Record this somewhere you can find it again on Day 3 (a sticky note, a TODO file, 1Password's "Notes" field on the trading-system entry — anywhere).

---

## Step 2 — Create the seven channels

Channels are case-sensitive in URLs but display lowercase. Create exactly these names (the bot code matches on these strings):

| # | Channel | Purpose (one-liner) |
| - | --- | --- |
| 1 | `daily-brief` | Daily morning briefing + weekly/monthly summaries |
| 2 | `signals` | Per-signal-cycle posts at 17:30 ET |
| 3 | `fills` | Order fills + cancellations + rejections |
| 4 | `alerts` | P1/P2 operational alerts (recon breaks, vol regime trips) |
| 5 | `critical` | P0 alerts (kill-switch, audit-chain break) |
| 6 | `ops` | Slash-command surface (/positions, /halt, /ratify, etc.) |
| 7 | `audit` | Append-only mirror of high-signal `audit_log` events |

For each channel:

1. Right-click the **Text Channels** category header → **Create Channel**.
2. **Channel Type:** Text.
3. **Name:** as listed above (no leading `#`).
4. Click **Create Channel**.

### Capture the seven Channel IDs

For each of the seven, right-click the channel name → **Copy Channel ID** (Developer Mode required from Step 1). Record all seven somewhere structured — a quick template:

```
trading-system Discord IDs (captured 2026-05-05):
  guild_id:      <paste from Step 1>
  daily-brief:   <paste>
  signals:       <paste>
  fills:         <paste>
  alerts:        <paste>
  critical:      <paste>
  ops:           <paste>
  audit:         <paste>
```

Paste this block into the host secrets file under the `discord:` key.

> **Note on webhook URLs vs channel IDs:** `manifest.json` and `backend-spec.md §8.1.1` reference `webhook_urls` per channel. Two posting modes exist: (a) the bot posts as itself via Channel ID, (b) per-channel webhooks post as named integrations. For Phase 0 we use mode (a) — bot posts via its token + Channel ID. The `webhook_urls` field in the secret schema is forward-compatible for Phase 2 if we add per-channel routing, but for Day 2 just capture Channel IDs and we'll wire either form on Day 3.

---

## Step 3 — Create the bot application

1. Open https://discord.com/developers/applications in a new tab.
2. **New Application** (top-right).
3. **Name:** `Trading System Bot`. Click **Create**.
4. You land on the application's General Information page. (No edits needed here.)
5. Left sidebar → **Bot**.
6. **Reset Token** → confirm. **A token appears once — copy it immediately.**
   - Store in 1Password as a Secure Note: `discord-bot-token` (operator's choice of vault path).
   - On Day 3 it migrates from 1Password to `/opt/trading-secrets/secrets.yaml` (and the same value to `live.enc.yaml`; bot is single-token across envs per `manifest.json`).
   - **Do NOT paste this token into chat, this file, or any commit.** The gitleaks CI gate scans for Discord-token-shaped strings; the operator's discipline is to never have one on disk outside the encrypted env files. (See `Docs/decisions-log.md` 2026-05-05 QC token incident.)
7. Scroll to **Privileged Gateway Intents**:
   - **Presence Intent:** OFF (we don't track member presence)
   - **Server Members Intent:** OFF
   - **Message Content Intent:** OFF (we use slash commands, not message-content scanning — keeping this off avoids the privileged-intent verification GitHub-style review at 100+ guilds, which we'll never hit anyway, but it's the right minimum)

---

## Step 4 — Generate the OAuth invite URL

1. Application sidebar → **OAuth2** → **URL Generator**.
2. **Scopes:** check **bot** AND **applications.commands**.
3. **Bot Permissions** (appears after checking `bot`):
   - Check **View Channels**
   - Check **Send Messages**
   - Check **Embed Links**
   - Check **Read Message History**
   - Permission integer at the bottom should be **84992**. If it shows anything else, uncheck everything and re-check just those four.
4. **Copy** the generated URL (at the bottom of the page).
5. Open that URL in a new tab → select your `trading-system` server from the dropdown → click **Authorize**.
6. The bot now appears in your server's member list.

---

## Step 5 — Verify the bot can post

Quick smoke test, no code required:

1. In any channel, type `/` to see if Discord shows ANY slash commands. Default Discord server commands like `/giphy` should be present; bot-specific commands won't be (we haven't registered any yet — that's Week 7).
2. Right-click the bot in the member list → its presence should show as offline (no service is connected to the token yet — that's expected).

The actual signal/fill posting smoke test happens Week 7 when the discord-bot service comes online (per `claude-dev-guide.md §10.1` Week 7 row).

---

## Step 6 — Day 3 handoff (preview)

The values you captured today land in `secrets/{paper,live}.enc.yaml` on Day 3 09:00:

```yaml
discord:
  bot_token: <Step 3 token, from 1Password>
  guild_id: <Step 1 guild_id>
  webhook_urls:
    daily_brief: <Step 2 channel_id>
    signals: <Step 2 channel_id>
    fills: <Step 2 channel_id>
    alerts: <Step 2 channel_id>
    critical: <Step 2 channel_id>
    ops: <Step 2 channel_id>
    audit: <Step 2 channel_id>
```

The schema lives at `deploy/secrets.template.yaml` with `<TODO_DISCORD_...>` placeholders that get substituted with your captured values.

---

## Token rotation procedure

If the bot token is ever leaked or you suspect compromise:

1. discord.com/developers → application → Bot → **Reset Token**.
2. Update `bot_token` in `/opt/trading-secrets/secrets.yaml` AND `secrets/live.enc.yaml`. Both encrypt + commit.
3. Restart the discord-bot service (`docker compose restart discord-bot` on each VPS).
4. The OLD token is invalidated immediately on reset — there's no overlap window. Operator-side: ensure you have shell access to do step 3 within ~5 minutes so message delivery downtime is minimized.

This procedure mirrors the QC token rotation logged in `Docs/decisions-log.md` 2026-05-05 — same operational discipline.

---

## Files in this directory

| Path | Purpose |
| --- | --- |
| `manifest.json` | Canonical declaration of guild + channel + bot config. Source-of-truth if Discord state needs to be reproduced (e.g. operator account migration). |
| `README.md` (this file) | Operator click-by-click runbook for first-time creation. |

---

## Notes for future Claude sessions

- Bot token is the only secret value. Channel IDs + Guild ID are not secrets.
- `#ask-agent` is Phase 2 only. Do not create on Day 2.
- The seven channel names are LOCKED — they map 1:1 to `services/discord-bot/` routing logic and the audit-event taxonomy in `manifest.json`. Adding a new channel requires a backend code change AND a manifest update; renaming an existing channel requires a search-replace across the bot's routing table.
