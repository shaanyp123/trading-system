# Discord — Viewer Role Setup Runbook

How to add view-only members to the operator's Discord guild so trusted people can follow the operator's progress without being able to type, invoke slash commands, or interact with the trading bot.

This runbook is Discord-side configuration only — no code changes, no sops updates, no VPS deploy. Execution time: ~10 minutes for the role setup + a few seconds per friend added.

## When to use this

- Phase 0 (current — Day 24 onwards through ~Week 7): friends see an effectively empty trading guild. Bot is online but every slash-command reply is ephemeral. Adding friends now exposes nothing trading-related.
- Phase 1+ (Week 7+ — when the Week-4-Wed dispatcher wires real signals/fills/alerts to channels): every signal, fill, P&L number, kill-switch event will post to channels. Re-evaluate viewer access before that lands.

## What viewers CAN see (Phase 0 today)

- The guild structure (7 channels: `#daily-brief`, `#signals`, `#fills`, `#alerts`, `#critical`, `#ops`, `#audit`)
- The bot (`Trading System Bot`) online status in the member list
- Channel history (empty — Phase 0 has no channel posts yet)

## What viewers CANNOT see (with the role config below)

- Slash command responses (all ephemeral; only the invoker sees them — and viewers can't invoke at all)
- Each other's interactions (because they can't message at all)
- Operator's WebAuthn credentials, TOTP secret, backup codes, age key, sops bundle (none of those touch Discord)

## What viewers WILL see in Phase 1+ (when channels go live)

- `#daily-brief` — 17:00 ET daily summary with Net Liq, daily/WTD/MTD P&L, open position count, pending signal count, health score
- `#signals` — every candidate signal with market, direction, decision price, expected slippage
- `#fills` — every order fill with realized slippage, commission, position update
- `#alerts` — reconciliation breaks, vol-regime trips, calendar-unratified reminders (P1/P2)
- `#critical` — kill-switch events, audit chain breaks, halt-new transitions (P0)
- `#ops` — operational events (slash command invocations, vacation start/end, deploy notifications)
- `#audit` — append-only mirror of high-signal audit-log events

**This is full visibility into your trading operation.** If you want sensitive data hidden, decline viewer access OR set up a second guild with only `#daily-brief` and `#critical` (high-level only) and route the bot to both via Phase-1 config.

## Setup

### Step 1 — Create the `viewer` role

1. Open Discord, navigate to your `trading-system` server
2. Click the server name dropdown at the top-left → **Server Settings**
3. Left sidebar → **Roles** → **Create Role** (or **+** icon)
4. **Display** tab:
   - **Name**: `viewer`
   - **Color**: any muted color (gray works well — visually distinguishes from operator and bot)
   - **Display role members separately from online members**: optional (handy for sidebar visibility)
5. **Permissions** tab — these are the guild-level defaults. Apply these EXACTLY:

| Permission | State | Why |
|---|---|---|
| **View Channels** | ✅ on | Friends can SEE channels |
| **Read Message History** | ✅ on | Friends can scroll back |
| **Send Messages** | ❌ off | Friends can't type |
| **Send Messages in Threads** | ❌ off | Friends can't reply in threads |
| **Create Public Threads** | ❌ off | Friends can't open threads |
| **Create Private Threads** | ❌ off | Same |
| **Embed Links** | ❌ off | Defense in depth (no autolink injection) |
| **Attach Files** | ❌ off | No file uploads |
| **Add Reactions** | ❌ off (strict) OR ✅ on (lenient) | Reactions are visible to operator; choose based on signal-vs-noise preference |
| **Use External Emojis** / **Stickers** | ❌ off | Same reasoning |
| **Mention @everyone, @here, and All Roles** | ❌ off | No pings |
| **Use Application Commands** | ❌ off | **CRITICAL — blocks `/positions`, `/halt`, `/status`** |
| **Use Voice Activity** etc. | ❌ off | No voice |
| **All Manage \* permissions** | ❌ off | Defense in depth — no role/channel/server modification |

6. Click **Save Changes**

### Step 2 — (Optional) Per-channel permission overrides

The role-level defaults above are sufficient for view-only. But for defense in depth — protecting against an accidental `@everyone` permission drift in the future — explicitly apply the `viewer` role to each of the 7 channels:

For each channel (`#daily-brief`, `#signals`, `#fills`, `#alerts`, `#critical`, `#ops`, `#audit`):

1. Hover over the channel → gear icon (Edit Channel)
2. **Permissions** tab → **Add Role** → select `viewer`
3. Confirm at the channel level (in addition to role-level):
   - ✅ View Channel
   - ✅ Read Message History
   - ❌ Send Messages
   - ❌ Use Application Commands

This is belt-and-suspenders. If a future `@everyone` permission edit accidentally re-grants `Send Messages`, the channel-level deny on `viewer` still holds.

### Step 3 — Lock `/halt` to operator-only (recommended)

The bot's `HaltConfirmView.interaction_check` already blocks anyone but the invoker from clicking the **🛑 Confirm halt** button. But anyone with `Use Application Commands` can still INVOKE `/halt` and see the confirm view. With the role config in Step 1 viewers already can't fire any slash command, but adding a second guard for `/halt` specifically is one more layer:

1. Go to https://discord.com/developers/applications → click your `Trading System Bot` application
2. Left sidebar → **Integrations** (some Discord UIs show this as part of OAuth or App settings)
3. Find the `halt` command
4. **Default Member Permissions** → check `Administrator` only
5. Save

After this:
- `/halt` only appears in slash-command autocomplete for members with the Administrator permission (i.e., just you)
- `/positions` and `/status` remain available to all members with `Use Application Commands` (which is none, given the viewer role config above)

This is a defensive layer; with the viewer role from Step 1 you don't strictly need it, but it locks `/halt` to "operator only" forever even if you later experiment with broader viewer permissions.

### Step 4 — Invite friends + assign the role

1. Server name dropdown → **Invite People**
2. Click the pencil/edit icon next to the invite link to customize:
   - **Expire after**: 24 hours (one-time invites) OR 7 days (more forgiving for slow joiners)
   - **Max number of uses**: 1 per friend (for traceability — each friend gets their own link) OR small N for a friend-group
   - **Grant temporary membership**: OFF
3. Copy the invite link → send to friends via SMS / iMessage / Signal — NOT pasted into chat with a third-party AI or shared in any public log
4. After each friend joins, in the Discord client:
   - Right-click their username in the member list (right sidebar)
   - **Roles** submenu → check `viewer`
   - The role's restrictions apply to their account in your guild

### Step 5 — Verify

Have one trusted friend confirm:
- ✅ They can SEE all 7 channels in the sidebar
- ✅ They can SCROLL channel history (Phase 0: nothing to see)
- ❌ The message-input box at the bottom of each channel is gone / disabled
- ❌ Typing `/` in any channel shows NO bot commands in the autocomplete dropdown
- ❌ Right-clicking on a bot message (Phase 1+) shows no "interact" actions

If any of the "CANNOT" items above turn out to actually work for the viewer, paste the friend's account state + role list to the operator's Claude Code session — there's a permission drift somewhere.

## Token rotation (when re-execution is needed)

If you ever need to revoke a viewer's access:

1. Server Settings → Members → click the friend's username
2. Remove the `viewer` role from them OR click **Kick** at the bottom
3. Invalidate any active invite links: Server Settings → Invites → revoke

The viewer can't auto-rejoin without a fresh invite link.

## What happens to viewers when the system shifts to Phase 1+

The role config above is FORWARD-COMPATIBLE: when channels start receiving real signals/fills/alerts (Week 7+), viewers automatically start seeing them in the same channels because they have `View Channels` + `Read Message History` already granted. No action needed at that time — but RE-EVALUATE then whether you still want viewers to see that data.

If you don't, two clean options:
1. Kick viewers before Week 7 lands the dispatcher
2. Lock individual channels to operator-only (Channel Settings → Permissions → deny `viewer` role on the sensitive channels, leave `#daily-brief` / `#ops` open if you want a high-level update view)

## Files in this directory

| Path | Purpose |
| --- | --- |
| `README.md` | Operator click-by-click runbook for first-time Discord guild + bot creation (Day 2 task) |
| `manifest.json` | Canonical declaration of guild + channel + bot config |
| `viewer-role-setup.md` (this file) | How to add view-only members for friends/family |

## Notes for future Claude sessions

- The viewer role is a Discord-only construct; the bot has no awareness of viewer-vs-operator distinction at the code level
- Phase 0 today: viewers see nothing trading-related (all bot output ephemeral)
- Phase 1+ (post-dispatcher): viewers see EVERYTHING that posts to channels
- If a Phase-2 requirement emerges where viewers should see a high-level subset (e.g., daily P&L but not per-trade fills), the cleanest path is a second guild with a different channel set + the bot configured to fan out per-channel routing via `secrets/<env>.enc.yaml` `discord.webhook_urls`. The current single-guild design assumes uniform visibility for all members.
