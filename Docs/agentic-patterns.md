# Agentic Operator Flows

Canonical patterns for leaning the operator's workflow on agentic + scheduled + autonomous Claude execution. Created 2026-05-25 as part of the `claude-setup-overhaul.md` WS#8 work.

Goal: shift the operator from "Claude is a session-bounded implementer" to "Claude is a continuous collaborator — sometimes synchronous, sometimes in-session-async, sometimes scheduled." Each pattern below has a specific use case + activation steps + escalation path.

## Pattern landscape

| Pattern | Tool | When to use | Runs when | Memory |
|---|---|---|---|---|
| **In-session sync agent** | `Agent(...)` | Independent research / parallel exploration within a turn | While operator session is active | Fresh per invocation |
| **In-session background agent** | `Agent(... run_in_background: true)` | Long-running task you don't need to block on (PR review, repo audit) | Operator session, but doesn't block main turn | Fresh per invocation |
| **In-session /loop** | `/loop <cmd>` | Self-paced repeated task within a session ("poll deploy every 5 min") | While operator session is active | Fresh per iteration |
| **Scheduled task — recurring** | `mcp__scheduled-tasks__create_scheduled_task(cronExpression: ...)` | Daily / weekly / monthly recurring routine (briefing, audit, reminder) | When Claude Code app is open at cron time (or on next launch) | Fresh per run |
| **Scheduled task — one-time** | `mcp__scheduled-tasks__create_scheduled_task(fireAt: ...)` | Reminder / deferred action ("at 3pm tomorrow, check X") | At the specified moment (or on next launch) | Fresh per run |
| **VPS cron** | systemd timer + bash script | 24/7 monitoring (verify_chain hourly, daily recon, etc.) — runs even when operator is offline | Always (host OS scheduler) | None (stateless scripts) |
| **/ultrareview** | `/ultrareview` slash command | Multi-agent independent review of an opened PR | Operator-initiated; billed | Fresh agents per run |

**Key boundary.** Scheduled tasks run **only when the Claude Code app is open** (or on next launch). For truly 24/7 monitoring (audit chain integrity, recon snapshot, LEAN cycle health), use **VPS cron / systemd** — NOT scheduled tasks. The existing `services/api/async_task_monitor.py` + `deploy/lean_local/systemd/lean-local-daily-restart.timer` are the right surfaces.

Scheduled tasks fill a different gap: **operator-facing summaries + reminders that benefit from Claude's synthesis ability + are anchored to the operator's working hours.**

---

## Pattern 1 — Morning daily briefing (recurring scheduled task)

**Use case.** When the operator opens Claude Code in the morning, they get a one-shot brief of overnight activity + today's reminders. Replaces ad-hoc "what happened overnight?" questions.

**Schedule.** Weekdays at 09:00 ET. Runs on next app launch if app was closed.

**Prompt template (self-contained — task runs in a fresh session with no memory of this conversation):**

```
You are the daily-briefing routine for the solo-operator algorithmic trading system.

Working directory: /Users/shaanpatel/Documents/GitHub/Trading

Produce a structured brief for the operator. Use Bash + Read tools only. NEVER pipe credential-laden output to stdout — route to file with chmod 600, verify via wc -l (see memory feedback_secret_handling.md).

Steps:
1. `git log --since="24 hours ago" --pretty='%h %s' main` — list overnight commits
2. `gh pr list --state open --limit 10` — list open PRs needing operator review
3. Read Docs/decisions-log.md last ~50 lines — note any 2026-05-NN entries newer than yesterday
4. Read CLAUDE.md operational status section
5. Read ~/.claude/projects/-Users-shaanpatel-Documents-GitHub-Trading/memory/MEMORY.md
6. Cross-check today's date against project_phase_status_operational memory for Day N

Output (under 300 words, no emoji, professional tone):

# Daily Briefing — <YYYY-MM-DD ET> — Day N

## Overnight changes (since yesterday 09:00 ET)
- Commits: <count>; notable: <summary>
- Open PRs: <count> (titles)
- Decisions-log new entries: <list with one-line summary each>

## Today's expected ceremony
- 17:00 ET — BarSyncWorker cycle
- 21:30 UTC — LEAN signal cycle
- 22:30 UTC — EOD reconciliation
- 23:59 ET — IBKR overnight maintenance restart

## Operator actions queued
- <items from decisions-log "follow-ups" if any>
- <PRs awaiting operator review>
- <scheduled-task results from past 24h flagged as needing attention>

## Audit chain status
- Cannot run verify_chain locally (requires VPS SSH). Operator should run /verify-chain paper if last known good is >24h old.

## Anomalies
- <anything surprising in git log, PRs, or decisions-log; otherwise "None observed">

If anything blocks operator's morning workflow (a P0 alert from last night, a P1 unresolved, an audit-chain hint of trouble), surface it AT THE TOP of the brief in bold.
```

**Activation:** see "Recommended starter tasks" below.

---

## Pattern 2 — Weekly Monday strategic review (recurring scheduled task)

**Use case.** Monday morning structured look-back over the past week — what landed, what's pending, what to prioritize. Operator already does this informally; this turns it into a recurring artifact.

**Schedule.** Mondays at 08:30 ET (before the daily-briefing runs at 09:00).

**Prompt template:**

```
Weekly Monday review for the trading system.

Working directory: /Users/shaanpatel/Documents/GitHub/Trading

Steps:
1. `git log --since="7 days ago" --pretty='%h %s' main` — full week of commits
2. `gh pr list --state merged --limit 20 --search "merged:>=$(date -v-7d +%Y-%m-%d)"` — PRs merged in last 7d
3. `gh pr list --state open` — open PRs needing operator
4. Read Docs/decisions-log.md last ~200 lines — extract 2026-05-NN entries from last 7d
5. Read Docs/claude-setup-overhaul.md if any new entries
6. Read CLAUDE.md operational status

Output:

# Weekly Review — Week of <YYYY-MM-DD>

## What landed (merged PRs)
- <numbered list with PR# + title + 1-line impact>

## Architecture / decisions
- <decisions-log entries from last 7d>

## Pending work
- Open PRs by age (oldest first):
- Decisions-log "follow-up" items still open:

## Risk + safety touch-points (PRs with risk-review-approved label)
- <count>; list with verification status (did /ultrareview run?)

## Next-week priorities (operator's call — surface what's blocking)
- <items still blocked + brief reason>

## Memory + tooling state
- Any memory entries that look stale (>14 days for project-type entries)?

Keep it under 500 words. The operator will react to this brief by sequencing the week.
```

---

## Pattern 3 — Pre-deploy ceremony reminder (one-time scheduled task, on-demand)

**Use case.** Operator is about to deploy a PR to VPS; ask Claude to fire a reminder of the ceremony steps 5 min before they SSH in.

**Schedule.** `fireAt: <timestamp 5min from now>` — operator runs this manually when they're about to deploy.

**Prompt template:**

```
Pre-deploy ceremony reminder.

Output the operator-side deploy ceremony for the trading system. Per Docs/recent-architecture-changes.md current architecture block (Option C data-layer pivot):

1. SSH to VPS; `git pull --ff-only`
2. `docker compose --env-file deploy/.env build api lean_local` (or whichever services changed)
3. `docker compose --env-file deploy/.env up -d --force-recreate api lean_local`
4. Watch api logs for `bar_sync_worker_spawned` at boot + (at 17:00 ET) `bar_sync_cycle_firing` → `bar_sync_cycle_completed failed_markets=[]`
5. Watch lean_local logs for clean `v1_strategy initialized` + at 21:30 UTC `v1_signals_generated session_date=… signals_emitted_count=…`
6. Verify `verify_chain --env paper` still passes (via /verify-chain paper slash command for the ceremony)

Per feedback_no_destructive_shortcuts.md: if anything fails, DO NOT use --no-verify, git reset --hard, or force-recreate as workarounds. Investigate root cause.

Output the ceremony block + a checklist the operator can paste into their SSH terminal.
```

---

## Pattern 4 — Background agent for repo-wide research (in-session)

**Use case.** While working on a primary task, kick off a research agent to investigate something orthogonal in parallel. E.g., "audit all usages of `Decimal` vs `float` across the codebase" while you keep working on a feature.

**Activation.** Use the `Agent` tool with `run_in_background: true`. Subagent type `Explore` for read-only searches; `general-purpose` for tasks requiring multi-step reasoning.

**Pattern:**

```
Agent(
  description="Audit Decimal vs float usage",
  subagent_type="Explore",
  prompt="Grep the entire repo for `float(` patterns; classify each as (a) intended (monetary serialization to wire format), (b) bug (money calc), (c) unrelated (non-monetary). Report findings under 300 words.",
  run_in_background=true
)
```

The main session continues; the background agent's results land when you next inspect them. Use for: tech-debt audits, security sweeps, dependency analysis, doc consistency checks.

---

## Pattern 5 — /loop for in-session monitoring

**Use case.** During a long ceremony (deploy, recovery, debugging), keep Claude actively polling a state every N minutes without operator having to re-prompt.

**Activation.** `/loop <interval> <task>` — e.g., `/loop 2m grep deploy status in api logs`.

**When NOT to use.** For 24/7 monitoring → VPS cron. For one-shot waits → background bash with `run_in_background` and notification. /loop is specifically for **active operator session + recurring task that benefits from each iteration's reasoning.**

---

## Pattern 6 — VPS cron / systemd timers (the 24/7 floor)

**Use case.** Anything that must run when the operator is asleep / offline. Already deployed:

- `deploy/lean_local/systemd/lean-local-daily-restart.timer` — daily 21:10 UTC restart of lean_local container
- `services/api/async_task_monitor.py` — every 30s probes the 3 long-lived async tasks (order placement, recon scheduler, heartbeat probe)
- `services/reconciliation/eod_cycle.py::ReconciliationScheduler` — daily 22:30 UTC EOD recon
- `services/data/bar_sync.py::BarSyncWorker` — daily 17:00 ET bar fetch

**Landed extensions:**

- **VPS systemd timer for `verify_chain --env paper`** — landed 2026-05-26 (claude-setup-overhaul follow-up). Files: `scripts/operator_tools/verify_chain_to_discord.sh` + `deploy/audit/systemd/verify-chain-daily.{service,timer}`. Runs daily at 02:00 ET (06:00 UTC); POSTs to Discord `#audit` channel via a dedicated webhook URL stored at `/etc/trading/audit-webhook.url` (chmod 600). Independent of api's `webhook_pusher` by design so compounded failures (chain broken + api down) still surface. Install ceremony in `deploy/audit/README.md`.

- **Recovery agent for `async_task_died` events** — landed 2026-05-26 (drill-5 / drill-7 follow-up). Files: `scripts/operator_tools/recovery_agent.py` + `scripts/operator_tools/recovery_agent_tick.sh` + `deploy/audit/systemd/recovery-agent-poll.{service,timer}`. Fires every 60s; polls `alerts WHERE category = 'worker_failure' AND acknowledged = FALSE`, classifies the failure (transient vs hard crash), invokes `scripts/operator_tools/replay_executions.py` for transient failures, audit-first emits `RECOVERY_ACTION_TAKEN` before the alerts UPDATE + Discord post. Discord posts go to `#critical` via dedicated webhook at `/etc/trading/critical-webhook.url`. Pairs with autoheal sidecar (PR #240, handles gateway stuck-state) and verify-chain cron (PRs #242-#245) as the third pillar of the 24/7 safety net. Install ceremony in `deploy/audit/README.md` ("Recovery agent — install ceremony").

  Closes the manual operator step from drill 5 (2026-05-18): when an `OrderPlacementWorker` task died, the operator had to hand-run `/tmp/drill5_recovery.py` (the transient precursor of `replay_executions.py`) to recover backend-blind fills. Drill 7 repeated the same pattern. The agent now decides + acts within ~60s, with classification fail-closed on unknown exception types (operator-gated investigation rather than blind replay).

**Phase 1+ extensions (still recommended):**

---

## Pattern 7 — Discord MCP integration plan

Once Discord MCP is connected (per `claude-setup-overhaul.md` WS#7), the daily-briefing routine can POST its output to `#daily-brief` Discord channel directly. Pattern:

```
After producing the brief, post it to Discord:
1. Use mcp__discord__post_message with channel='daily-brief' and content=<the brief markdown>
2. If the brief contains a "CRITICAL" or "P0" section, ALSO post to '#critical' channel
3. Acknowledge: "Brief posted to #daily-brief at <ISO timestamp>"
```

Until Discord MCP is connected, the brief lands in the Claude Code session output only. Operator can copy-paste manually OR pipe through `services/webhook_pusher/cli.py` if they want a bridge.

---

## Anti-patterns

- **Don't create a scheduled task for anything safety-critical.** Audit chain integrity, IBKR connection health, EOD recon — these belong on VPS cron / systemd because they must fire even when the operator isn't around. Scheduled tasks are for OPERATOR-FACING summaries, not for monitoring.
- **Don't put credentials in scheduled-task prompts.** Each task's prompt is stored at `~/.claude/scheduled-tasks/{taskId}/SKILL.md` in plaintext. If the task needs a secret, source it from sops at runtime, not from the prompt itself.
- **Don't bypass the operator on risk decisions.** Background agents + scheduled tasks should SURFACE issues, not act on them. The operator gates merge, halt, recovery — always. See memory feedback_session_workflow.md.
- **Don't run /ultrareview from a scheduled task.** /ultrareview is operator-billed and operator-triggered. A scheduled task that auto-fires it would charge the operator without consent.

---

## Maintenance

- Update this doc when a new agentic pattern emerges + is adopted into the operator workflow
- Review scheduled tasks quarterly via `mcp__scheduled-tasks__list_scheduled_tasks` — disable any that have low signal
- VPS cron entries belong in `deploy/` per the existing pattern (e.g., `deploy/cron/` if/when this grows)

## Cross-refs

- `Docs/claude-setup-overhaul.md` WS#7 — Discord MCP install rationale
- `Docs/claude-setup-overhaul.md` WS#8 — agentic flows audit (this doc is the deep-dive companion)
- `Docs/claude-dev-guide.md` §1 — session protocol (what's locked vs operator-chosen)
- `Docs/claude-dev-guide.md` §5.9 — /ultrareview policy
- `.claude/commands/` — slash commands (synchronous in-session ceremony)
- `.claude/agents/risk-review.md` — risk-review subagent (in-session)
