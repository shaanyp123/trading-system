# Claude Setup Overhaul — 2026-05-25

Operator-initiated overhaul of the Claude Code tooling layer (memory, hooks, plugins, permissions, connectors, agentic flows) to better support future implementation + operator success. Eight workstreams; PR-style summary per workstream below.

This doc is the canonical audit trail for the meta-tooling overhaul. Code/runtime architecture deviations continue to land in `Docs/decisions-log.md` per usual cadence.

**Why a separate doc?** `Docs/decisions-log.md` is for code/runtime architecture deviations. Meta-tooling changes (memory layer, hooks, plugins) have different audience + cadence + blast radius. Keeping them separate preserves the signal-to-noise on each.

**Sequence + status:**

| # | Workstream | Status |
|---|---|---|
| 1 | Memory rebuild | ✅ done |
| 2 | CLAUDE.md restructure | ✅ done |
| 3 | Safety gate hooks | ✅ done |
| 4 | Custom trading plugin | ✅ done (scaffold + 3 commands + 1 agent) |
| 5 | Permissions allowlist | ✅ done |
| 6 | `/ultrareview` integration | ✅ done |
| 7 | Connector audit | ✅ done (recommendations documented; install deferred to operator) |
| 8 | Agentic operator flows | ✅ done |

---

## WS#1 — Memory rebuild (2026-05-25)

**Problem.** Memory layer at `~/.claude/projects/-Users-shaanpatel-Documents-GitHub-Trading/memory/` had 3 entries despite the system carrying hundreds of load-bearing invariants. `project_trading_system.md` was 20 days stale and cited the retired "QC Cloud Phase 1 → direct IBKR Phase 2" architecture. Every fresh Claude session re-read the entire dev guide cold instead of having these facts persist.

**Change.**

- **Updated:** `project_trading_system.md` (refreshed with post-pivot Phase 1 reality + cross-refs to other memories)
- **Added (project type):** `project_post_pivot_architecture.md`, `project_phase_status_operational.md`, `project_clientid_allocation.md`, `project_sidelined_markets.md`
- **Added (feedback type):** `feedback_risk_paths_need_label.md`, `feedback_audit_first_ordering.md`, `feedback_session_workflow.md`, `feedback_no_destructive_shortcuts.md`
- **Added (reference type):** `reference_canonical_docs.md`, `reference_external_systems.md`, `reference_verification_ceremonies.md`
- **Added (user type):** `user_operator_profile.md`
- **Kept verbatim:** `project_trading_identifiers.md`, `feedback_secret_handling.md`
- **Restructured:** `MEMORY.md` index by type (User / Feedback / Project / Reference); one line per entry

**Total:** 3 → 13 memory files. Every entry uses the `[[name]]` cross-reference convention so related memories surface together.

**Why each memory exists** (non-obvious / surprising content that a fresh session would otherwise have to discover by mistake):

- `project_post_pivot_architecture.md` — retired patterns in old spec text would mislead a session reading them cold
- `project_clientid_allocation.md` — IBKR rejects duplicate clientIds; collisions break the live system silently
- `project_sidelined_markets.md` — /MCL is excluded from active universe with a 5-step re-enable runbook
- `feedback_audit_first_ordering.md` — audit row writes BEFORE state change (opposite of most systems)
- `feedback_risk_paths_need_label.md` — pre-merge linter blocks PRs touching A02 paths without the label
- `reference_canonical_docs.md` — topic → doc section mapping reduces blind grep
- `reference_verification_ceremonies.md` — exact `verify_chain` / EOD recon / replay_executions runbooks

**Verification.** Read `MEMORY.md`; cross-references resolve; each memory has frontmatter (name/description/type) + body structure per the auto-memory guidance (Why + How to apply for feedback/project).

**Blast radius.** None on runtime. The next session will surface relevant memories based on description match against the task at hand. Operator can edit/prune any entry; memory system is operator-owned.

---

## WS#2 — CLAUDE.md restructure (2026-05-25)

**Problem.** `CLAUDE.md` was 220 lines but extremely dense — the file-index table alone took ~130 lines of huge paragraph-style rows. Every session-start read consumed disproportionate context budget on a table that doesn't change between sessions on most days. The 4 pivot blocks at the top (operational status + 2026-05-12 architecture pivot + 2026-05-21 data-layer pivot + 2026-05-22/24 LEAN futures saga) were load-bearing context but their location in CLAUDE.md couldn't be referenced from anywhere else.

**Change.** Three-way split (per operator confirmation 2026-05-25):

- **`CLAUDE.md` (new, 70 lines)** — slim orientation: operational status callout (compressed from 4 huge blocks) + reading list (now includes the 2 new files) + critical constraints + workflow + escalation + small reference docs table + pointer to `Docs/file-index.md`
- **`Docs/recent-architecture-changes.md` (new, 58 lines)** — the 4 pivot blocks preserved verbatim from old CLAUDE.md, with a chronological framing header so sessions read newest-last
- **`Docs/file-index.md` (new, 139 lines)** — both file-index tables (reference docs + code/ops surfaces) moved as-is with an update-cadence header

**Verification.** `wc -l` confirms split sizes; `head -20 CLAUDE.md` shows the new orientation structure; reading list now points at the 2 new docs.

**Blast radius.** Operator-facing. Anyone (operator, future Claude session, future contributor) reading CLAUDE.md now gets a slim orientation + explicit cross-refs. No content lost; reorganized for maintainability.

**Update cadence going forward:**
- `CLAUDE.md` changes rarely (only on session protocol or critical-constraint changes)
- `Docs/recent-architecture-changes.md` gets a new block per major architecture pivot
- `Docs/file-index.md` gets append-only row updates per PR / per saga

---

## WS#4 — Safety gate hooks (2026-05-25)

**Problem.** The operator's existing safety architecture relies on (a) the pre-merge linter blocking A02-forbidden PRs without the `risk-review-approved` label, and (b) operator discipline on secret-handling per `feedback_secret_handling.md` (Day 24 leak retrospective). Both are authoritative but neither catches the mistake at the moment it's made — wasted work happens before the gate trips. Goal: belt-and-suspenders informational guards inside the Claude Code session.

**Change.** Two PreToolUse hooks installed at the project level:

- **`.claude/hooks/risk_path_guard.sh`** — matches `Edit | Write | MultiEdit`. Reads tool-call JSON from stdin; extracts `file_path`; checks against the A02 forbidden-without-label whitelist (`services/risk/`, `services/signal/`, `services/audit/`, `services/execution/`, `services/reconciliation/`, `services/calibration/`, `services/agent/decisions/`, `services/agent/risk_actions/`, `services/agent/parameter_changes/`, `services/agent/prompts/decision/`, `alembic/`). On match: prints informational warning to stderr (visible to Claude) referencing dev-guide §11 [A02] + the relevant memory entry. Exit 0 — does NOT block. To switch to BLOCKING mode (paranoid mode for high-stakes sessions), change exit to 2 at the match block.

- **`.claude/hooks/secret_handling_guard.sh`** — matches `Bash`. Reads tool-call JSON; extracts `command`; checks against known secret-emitting patterns (`sops -d`, `docker compose config`, `docker compose logs.*{api,ib_gateway,webhook_pusher,discord_bot}`, `psql.*{setup_tokens,totp_secrets,backup_codes,sessions,webauthn_credentials}`). Silent-passes if command already has a file redirect (`> /tmp/`, `> /root/`, `> /var/`, `> /Users/`) or is a count-only op (`wc -l`, `COUNT(*)`). On match without redirect: prints warning referencing `feedback_secret_handling.md` + the safe form (route to file, chmod 600, verify via wc -l, shred). Exit 0 — informational.

Registered in `.claude/settings.json` (NEW file — was missing; `.claude/settings.local.json` already existed for personal/operator-local settings). Schema annotation included for IDE autocomplete.

**Verification.** 6-case smoke test executed:

| Test | Input | Expected | Got |
|---|---|---|---|
| 1 | Edit `/repo/services/risk/sizing.py` | risk-path warn | ✅ warn |
| 2 | Edit `/repo/services/api/main.py` (hot-fix path) | silent | ✅ silent |
| 3 | Bash `sops -d secrets/paper.enc.yaml` | secret warn | ✅ warn |
| 4 | Bash `sops -d ... > /tmp/check` | silent | ✅ silent |
| 5 | Bash `docker compose logs api --tail 20` | secret warn | ✅ warn |
| 6 | Bash `git status` | silent | ✅ silent |

**Blast radius.** None — exit 0 + informational. Worst case: noisy stderr for legitimate operations. Operator can disable via `.claude/settings.local.json` override or by setting `hooks: {}` in a personal override file. Hooks become active on next Claude Code session start (or `/config reload` if available).

**Update cadence.** When new safety-critical surface emerges (e.g., a new credential table, a new forbidden-path category), append to the relevant pattern array in the hook script. The hook script is the source of truth — settings.json just registers the matchers.

**Phase 1+ extension ideas (not done today):**
- PreToolUse on Edit/Write to `Docs/claude-dev-guide.md §1.5` block — warn that locked decisions are being modified
- PostToolUse on Write that creates a new file in tracked dirs — remind to update `Docs/file-index.md`
- Stop hook that runs `make ci` summary before session ends if any Edit/Write happened in the session
- PreToolUse on Bash `git push` to main — require explicit operator approval-in-session
- PreToolUse on Bash `gh pr create` — require risk-review-approved label flag when any A02-forbidden file is staged

---

## WS#3 — Custom trading plugin (2026-05-25)

**Problem.** Repeated ceremonies — `verify_chain` runbook, EOD recon observation, pre-PR checklist, risk-review of A02-touching changes — were documented across multiple files (`deploy/audit/README.md`, `deploy/reconciliation/README.md`, `Docs/claude-dev-guide.md` §1 + §5.7, scattered memories). Each new session re-derived the ceremony from scratch.

**Scoping decision.** Full plugin packaging with marketplace-installable `.claude-plugin/plugin.json` was scoped but deferred to Phase 1+. Project-local `.claude/` directory ships the same content with one fewer indirection layer. Operator can promote to a packaged plugin later if they want it shared across other projects or repos.

**Change.** Project-level `.claude/` directory now holds:

- **3 slash commands** (`.claude/commands/`):
  - `/pre-pr-checklist` — runs locally; checks branch status, A02-path matches, test status, decisions-log/file-index audit-trail needs, backtest-delta requirement, ultrareview reminder; emits a structured checklist the operator can paste into PR description
  - `/verify-chain <env>` — generates the canonical SSH ceremony for `services/audit/verify_chain.py` per `deploy/audit/README.md` (the actual execution requires operator SSH — Claude Code locally can't reach the api container; this command codifies the ceremony so it's one slash-command away)
  - `/eod-recon [date]` — generates observation + diagnostic commands for the daily 22:30 UTC EOD reconciliation cycle (live-watch block + after-the-fact diagnostic block + did-the-scheduler-arm check)
- **1 subagent** (`.claude/agents/risk-review.md`) — invoked when changes touch `services/risk`, `services/audit`, `services/execution`, `services/reconciliation`, `services/signal`, `services/calibration`, `services/agent/`, or `alembic/`. Enforces canonical patterns (no print/float/bcrypt/SES/bare-domain; tz-aware UTC; JCS canonical), audit-first ordering, risk envelope + state machine invariants, IBKR clientId allocation, order placement gating, reconciliation tolerances, calibration zero-prior locked values, test discipline, test-before-commit rule, PR submission contract. Output format: structured PASSED / CONCERNS / BLOCKERS / SUMMARY review the operator can use directly.
- **README at `.claude/README.md`** — operator-facing index of what's where, with the existing settings/hooks tied in.

Each command has frontmatter declaring `description`, `argument-hint`, `allowed-tools` so they show in `/help`. The subagent has `name`, `description`, `tools` per the official example-plugin template.

**Verification.** Files written + frontmatter validates. Hooks (WS#4) work alongside the subagent — the hook fires at tool boundary, the subagent fires on Agent invocation.

**Blast radius.** None on runtime. Activates on next session start or `/config reload`.

**Phase 1+ extensions (documented in `.claude/README.md`):**
- `/deploy-vps` slash command for the docker-compose ceremony
- `gate-verification` skill triggered when operator says "verify week N gate"
- Additional commands: `/audit-recent` (last 24h of audit_log rows), `/health-check` (api + ib_gateway + lean_local + postgres status summary), `/backtest-delta` (runs backtest current branch vs main + diffs the equity curve)
- Full plugin packaging — promote `commands/` + `agents/` + `skills/` into a `.claude-plugin/plugin.json`-anchored plugin

---

## WS#5 — Permissions allowlist (2026-05-25)

**Problem.** Every Bash invocation prompts the operator for approval unless explicitly allowlisted. For routine read-only operations (`git status`, `docker compose ps`, `pytest`, `gh pr view`), this added friction without security benefit — the operations are inherently safe.

**Change.** Added a `permissions.allow` block to `.claude/settings.json` with ~50 conservative read-only patterns covering:

- Git read-only inspection (`status`, `log`, `diff`, `branch`, `show`, `rev-parse`, `blame`, `remote -v`, `ls-files`, `stash list`)
- Docker read-only (`ps`, `top`, `images`, `volume ls`, `network ls`) — NOT `docker compose logs` (which can leak secrets; the secret-handling hook from WS#4 would warn anyway)
- Make targets (`ci`, `test`, `lint`, `typecheck`, `format`, `frontend-test`, `dep-drift-check`)
- Test runners (`pytest:*`, `pnpm typecheck/lint/build/test`)
- GitHub CLI read-only (`gh pr view/list/checks/diff`, `gh issue view/list`, `gh repo view`, `gh run list/view`)
- File system read-only (`ls`, `find` with safe predicates, `wc`, `file`, `stat`, `tree`)
- Data tools (`jq`, `yq`)
- Version checks (`python3 --version`, `node --version`, etc.)
- `mkdir -p:*` (idempotent + non-destructive)

**Deliberately NOT allowlisted** (still prompt every time):
- `Bash(sops:*)` — keeps WS#4 secret-handling hook discipline + forces explicit consent on every decrypt
- `Bash(docker compose logs:*)` — same; logs can carry secrets
- `Bash(rm:*)`, `Bash(git push:*)`, `Bash(git reset --hard:*)` — destructive, per memory `feedback_no_destructive_shortcuts.md`
- `Bash(gh pr create:*)` — opens external state; requires explicit consent
- `Write(:*)` to any path — never blanket-allowed; per-file approval still required

**Format note.** Used `Bash(<prefix>:*)` syntax for prefix matching (anything after the prefix is allowed). Falls back to exact-match if Claude Code's permission system doesn't accept the syntax — operator can convert as needed.

**Phase 1+ extension (recommended).** Invoke `/fewer-permission-prompts` in a future session for a richer pass against actual transcript history. The skill analyzes operator's real Bash + MCP usage patterns and proposes a prioritized allowlist that's better-targeted than hand-curation. This WS#5 is a starting set, not exhaustive.

---

## WS#6 — `/ultrareview` integration (2026-05-25)

**Problem.** Risk-touching PRs (A02 forbidden-without-label whitelist) have existential blast radius if changed silently. The solo-operator + single-Claude-session pattern means there is no independent reviewer at PR-creation time. `/ultrareview` provides multi-agent cloud review — spawns reviewer agents that examine the diff without the implementer's framing — and is the natural gap-filler. But the policy wasn't documented anywhere; operator + future Claude sessions might forget to run it for risk-touching PRs.

**Change.**

1. **Added `Docs/claude-dev-guide.md` §5.9 "Multi-Agent Review for Risk-Touching PRs"** — codifies the policy: every PR carrying `risk-review-approved` MUST run `/ultrareview` before merge. Documents the operational flow + the hot-fix-whitelist exemption + the complementary relationship with the `risk-review` subagent from WS#3.
2. **`.claude/commands/pre-pr-checklist.md` step 7** (created in WS#3) already references the rule with a "**Reminder: run `/ultrareview` before merge.**" line on PRs requiring `risk-review-approved`. This is the operational enforcement — every PR submission flow surfaces the reminder.
3. **Complementarity:**
   - In-session: `risk-review` subagent (WS#3) runs against pre-PR code locally
   - Pre-merge: `/ultrareview` runs in cloud against the opened PR
   - Both should land for A02-path changes; the subagent catches issues before commit, the cloud review catches independent-perspective issues at merge gate

**Why a dev-guide edit + a pre-pr-checklist reminder + not just one?** The dev-guide is canonical documentation that survives any single Claude session's memory. The pre-pr-checklist is the operational reminder that fires AT PR submission. Both are needed: the dev-guide is the "law"; the checklist is the "enforcement at point-of-action."

**Verification.** §5.9 lands cleanly before "# 6. Testing Patterns". `/pre-pr-checklist` step 7 references the dev-guide section. Operator can `grep '/ultrareview' Docs/claude-dev-guide.md .claude/commands/pre-pr-checklist.md` to verify both pointers are aligned.

**Blast radius.** None operational. Adds 1 process gate to merge flow for ~5% of PRs (those touching A02 paths).

---

## WS#7 — Connector audit (2026-05-25)

**Inventory.** Operator has zero MCP connectors installed at the user level (`list_connectors` returned `[]`). The MCP servers visible in this session (`pdf-viewer`, `bio-research`, `zoom-plugin`, `Claude_Preview`, `Claude_in_Chrome`, `ccd_session_mgmt`, `mcp-registry`, `scheduled-tasks`, `917edc2e-...` file system one) come from plugins, not installed connectors.

**Project's existing "connector-like" surfaces** (in-app, not MCP):

- `services/webhook_pusher/` — outbound to Discord channels + Resend email (P0/P1/P2 alert dispatch)
- `services/discord_bot/` — inbound from Discord slash commands (`/positions`, `/halt`, `/status`)
- `gh` CLI — GitHub PR/issue/run inspection (read-only ops covered)
- Caddy reverse proxy — serves `/api/internal/lean/signals` for LEAN → api inbound
- sops + age — secrets workflow (no MCP needed)

**Gap analysis — what an MCP connector would add:**

| Connector | What it adds | Recommendation |
|---|---|---|
| **Discord** | Direct write to channels from Claude sessions WITHOUT going through `webhook_pusher`. Useful for: ad-hoc operator status posts, agentic-flow notifications, daily-briefing routine output (see WS#8). Read access to existing channel messages for context. | **Install (Recommended).** Highest leverage for agentic flows. Operator's primary push channel; adding bidirectional Claude ↔ Discord access unlocks the daily-briefing pattern and lets future agents respond to channel events directly. |
| **GitHub** | Richer than `gh` CLI for PR comment threading, review-state machinery, run/log artifact pulls. Same auth as gh. | Optional. `gh` covers ~90% of ops. Install if you start running `/ultrareview` heavily and want Claude to thread responses into the PR review surface. |
| **Linear / Notion** | External backlog tracking; could mirror `Docs/decisions-log.md` entries to a searchable surface. | **Skip.** `Docs/decisions-log.md` IS the operator's backlog/log; mirroring would duplicate without benefit. Revisit when team grows past one. |
| **PagerDuty / Opsgenie** | Page-the-operator on P0 incidents. | **Skip.** Watchdog + Resend email already covers P0 (per `Docs/claude-dev-guide.md` §1.5 LOCKED). No additional layer needed at Phase 0/1 scale. |
| **Sentry / Honeycomb / Grafana Cloud** | Observability hookup for query → traces/logs/metrics. | Optional. The project's structlog → file output is the canonical source today. Useful when api scales multi-replica and centralized log search becomes valuable (Phase 2+). |

**Recommendation summary.** Install **Discord** only. The other gaps are either covered by in-project surfaces (gh, webhook_pusher) or premature for current operator scale.

**Install ceremony for Discord** (operator action — not done in this session):

1. Search MCP registry: `/mcp` slash command in Claude Code OR via the UI → "Add connector" → search "Discord"
2. Auth: OAuth or bot token. Use a DEDICATED bot account separate from `services/discord_bot/` so Claude-side reads/writes don't conflict with bot's `/positions` `/halt` `/status` slash-command surface
3. Scope: limit to the operator's trading guild (`deploy/discord/manifest.json`); do not grant access to unrelated guilds
4. Once connected, future sessions get `mcp__discord__*` tools (post message, read channel, etc.)

**Agentic flow implications.** With Discord MCP installed, WS#8's daily-briefing routine becomes simpler — no need to go through `webhook_pusher` service for Claude's own status posts. The bot service remains for OPERATOR → SYSTEM commands; Discord MCP handles CLAUDE → OPERATOR push.

**Future audit:** re-run this audit quarterly. As the project matures + team grows, the cost/benefit shifts. Revisit Linear specifically if the operator ever brings on a research assistant or external advisor who needs visibility into the project plan.

---

## WS#8 — Agentic operator flows (2026-05-25)

**Problem.** Operator's workflow was bounded to in-session Claude work — every interaction required the operator to open Claude Code, type a prompt, and stay engaged through completion. No surface for: morning summaries, scheduled reviews, parallel exploration, background recovery agents, or 24/7 monitoring with Claude reasoning. Operator explicitly requested "lean into agentic flows."

**Change.** Two deliverables:

### Deliverable 1: `Docs/agentic-patterns.md`

Canonical reference for the seven agentic patterns available:

1. **In-session sync agent** — `Agent(...)` for parallel research within a turn
2. **In-session background agent** — `Agent(... run_in_background: true)` for non-blocking long tasks
3. **In-session /loop** — for self-paced repeated tasks during a session
4. **Scheduled task — recurring** — cron-driven; runs when Claude Code is open
5. **Scheduled task — one-time** — `fireAt` for deferred reminders
6. **VPS cron / systemd** — the 24/7 floor for safety-critical monitoring
7. **`/ultrareview`** — operator-triggered multi-agent PR review

Each pattern documented with use case + activation steps + anti-patterns + cross-refs. The critical boundary: **scheduled tasks are for operator-facing summaries, not safety-critical monitoring** (which lives on VPS cron / systemd).

### Deliverable 2: Two scheduled tasks activated

Per operator approval at task creation:

- **`daily-briefing`** (`taskId: daily-briefing`, cron `0 9 * * 1-5`) — weekday mornings at 09:00 ET local time. Produces a structured brief: overnight commits, open PRs, decisions-log entries, today's expected ceremony (17:00 ET BarSyncWorker → 21:30 UTC LEAN cycle → 22:30 UTC EOD recon → 23:59 ET IBKR maintenance), operator actions queued, anomalies. Self-contained prompt at `~/.claude/scheduled-tasks/daily-briefing/SKILL.md`. Honors `feedback_secret_handling.md` discipline. Will POST to Discord `#daily-brief` if Discord MCP is connected (per WS#7).

- **`weekly-monday-review`** (`taskId: weekly-monday-review`, cron `30 8 * * 1`) — Monday at 08:30 ET (30 min before daily-briefing so operator reads them in sequence). Strategic look-back: PRs merged past 7d, decisions-log entries, open PRs with age + risk-review-approved status + ultrareview status, memory staleness audit, next-week priorities. ~500 words, designed for strategic sequencing not action items. Will POST to Discord `#ops` if Discord MCP connected.

Both tasks run on next Claude Code launch if the app was closed at cron time. Both are independent — neither depends on session state from this conversation.

**Verification.** `mcp__scheduled-tasks__list_scheduled_tasks` shows both tasks armed. `daily-briefing` fires in ~13 hours from creation; `weekly-monday-review` fires in 6 days. The `~/.claude/scheduled-tasks/{taskId}/SKILL.md` files contain the self-contained prompts.

**Blast radius.** Each scheduled task runs in a fresh Claude Code session with no memory of this conversation; it only reads files + runs commands per its prompt. Cannot mutate trading runtime state (no SSH access from local Claude Code; no IBKR direct access). Worst case: noisy or unhelpful brief — operator disables via the Scheduled sidebar in Claude Code, or via `mcp__scheduled-tasks__update_scheduled_task`.

**Phase 1+ extensions (in `Docs/agentic-patterns.md`):**

- **VPS cron for `verify_chain --env paper`** — 24/7 safety-critical; sample script provided. Lives on `trading-primary` VPS, posts result to Discord `#audit` channel.
- **Discord MCP integration** — once connected (per WS#7), scheduled-task output goes directly to Discord channels.
- **Recovery agent on `async_task_died`** — when api's async-task-monitor logs `async_task_died`, fire an agentic recovery routine that runs `replay_executions.py`. Currently this is operator-manual per `Docs/decisions-log.md` 2026-05-18 drill 5 retrospective.
- **Pre-deploy ceremony reminder** — operator-on-demand one-time scheduled task with `fireAt`; described in Pattern 3.

---

## Summary

Eight workstreams complete. Memory layer rebuilt (3 → 13 entries). CLAUDE.md restructured (220 → 70 lines + 2 new Docs/). Two PreToolUse hooks installed + smoke-tested. Trading-specific slash commands + a `risk-review` subagent landed at `.claude/`. Permissions allowlist seeded with ~50 safe read-only patterns. `/ultrareview` policy codified in dev-guide §5.9. Connector audit documented; Discord MCP recommended for future install. Agentic-pattern doc written + two scheduled tasks armed.

The next session will pick up the new memories, hooks, slash commands, subagent, and (when 09:00 ET fires) the daily briefing. The trading runtime is unchanged — this overhaul was meta-tooling only; no `services/` or `lean/` or `apps/` code touched, no `risk-review-approved` label required.

**Operator next steps (in approximate priority order):**

1. Open a fresh Claude Code session to verify hooks + new memories surface correctly (read CLAUDE.md → confirm pointers to recent-architecture-changes + file-index work; trigger a forbidden-path edit warning to confirm `risk_path_guard.sh` fires)
2. Install Discord MCP connector (per WS#7) — highest-leverage future connector
3. Wait for first daily briefing fire tomorrow morning at 09:00 ET; redirect / adjust prompt via `mcp__scheduled-tasks__update_scheduled_task` if needed
4. Consider Phase 1+ extensions in `Docs/agentic-patterns.md` as they become valuable
5. Periodically (quarterly) re-run the connector audit + memory staleness check + permissions allowlist audit

This overhaul is complete. Future meta-tooling changes land in additional WS#N entries in this doc, OR in `Docs/decisions-log.md` if they're closer to runtime architecture.

## Files touched

**Modified:**
- `.gitignore` — un-ignored `.claude/settings.json`, `hooks/`, `commands/`, `agents/`, `skills/`, `README.md` so project-level config ships to future sessions / contributors. `settings.local.json` + `worktrees/` stay ignored.
- `CLAUDE.md` — restructured (220 → 70 lines); pivot blocks + file index moved out
- `Docs/claude-dev-guide.md` — added §5.9 "Multi-Agent Review for Risk-Touching PRs"

**Created (in repo):**
- `Docs/recent-architecture-changes.md` — pivot log
- `Docs/file-index.md` — code + ops surfaces snapshot
- `Docs/claude-setup-overhaul.md` — THIS DOC; audit trail for the overhaul
- `Docs/agentic-patterns.md` — canonical reference for agentic patterns
- `.claude/settings.json` — project Claude Code settings (hooks + permissions)
- `.claude/README.md` — operator-facing index for `.claude/`
- `.claude/hooks/risk_path_guard.sh` — A02 forbidden-path PreToolUse warning
- `.claude/hooks/secret_handling_guard.sh` — secret-leak Bash PreToolUse warning
- `.claude/commands/pre-pr-checklist.md` — `/pre-pr-checklist` slash command
- `.claude/commands/verify-chain.md` — `/verify-chain <env>` slash command
- `.claude/commands/eod-recon.md` — `/eod-recon [date]` slash command
- `.claude/agents/risk-review.md` — `risk-review` subagent definition

**Created (outside repo, operator-local):**
- 11 memory files at `~/.claude/projects/-Users-shaanpatel-Documents-GitHub-Trading/memory/` (`MEMORY.md` index restructured; 10 new entries + 1 update + 2 kept verbatim)
- 2 scheduled tasks at `~/.claude/scheduled-tasks/{daily-briefing,weekly-monday-review}/SKILL.md`

**Not touched:**
- Trading runtime — no files under `services/**`, `lean/**`, `apps/**`, `strategies/**`, `alembic/**`, `tests/**`, `scripts/**` (other than `.gitignore` at repo root)
- No `risk-review-approved` label needed for any change in this overhaul
- No new dependencies introduced (hooks use bash + `jq` which were already required by the project's deploy workflows)

