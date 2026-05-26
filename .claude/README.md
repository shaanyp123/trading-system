# `.claude/` — Project-level Claude Code configuration

This directory holds Claude Code configuration that is committed to the repo + shared across all operator sessions on this project. See `Docs/claude-setup-overhaul.md` for the design rationale.

## Layout

| Path | Purpose |
|---|---|
| `settings.json` | Project-level Claude Code settings (hooks registered here). Committed; shared. |
| `settings.local.json` | Personal/operator-local settings (gitignored). |
| `hooks/` | Hook scripts referenced from `settings.json`. |
| `commands/` | Project-level slash commands (invoke via `/<name>`). |
| `agents/` | Project-level subagents (Claude can spawn via the Agent tool). |
| `skills/` | Project-level skills (model-invoked based on description match). |
| `worktrees/` | Operator-local worktree workspaces (gitignored). |

## Slash commands

| Command | What it does |
|---|---|
| `/pre-pr-checklist` | Run before submitting a PR — checks risk-path label, test status, audit-trail completeness, backtest-delta requirement, ultrareview reminder |
| `/verify-chain <env>` | Generates the canonical SSH ceremony for `verify_chain --env <env>` against api container |
| `/eod-recon [date]` | Generates observation + diagnostic commands for the daily EOD reconciliation cycle |
| `/health-check` | Generates the operator-side SSH ceremony for a quick system health snapshot — containers, last cycles (bar_sync/LEAN/recon), risk state, audit chain, IBKR connection, autoheal status |

## Subagents

| Agent | When to use |
|---|---|
| `risk-review` | Review code changes against canonical patterns + anti-patterns + safety invariants. Invoke when touching `services/risk`, `services/audit`, `services/execution`, `services/reconciliation`, `services/signal`, `services/calibration`, `services/agent/`, or `alembic/` |

## Hooks (active)

| Hook | Trigger | Behavior |
|---|---|---|
| `risk_path_guard.sh` | PreToolUse on Edit/Write/MultiEdit | Warns if file_path matches the A02 forbidden-without-label whitelist. Informational; exit 0. |
| `secret_handling_guard.sh` | PreToolUse on Bash | Warns if command matches known secret-emitting patterns (sops, docker logs of secret-touching services, psql on credential tables) without a file redirect. Informational; exit 0. |

## Phase 1+ extensions (not in this overhaul)

- `/deploy-vps` slash command for the docker-compose ceremony
- `gate-verification` skill triggered when operator says "verify week N gate"
- Additional hooks: locked-decisions edit warning, file-index update reminder, push-to-main approval guard
- Plugin packaging — promote `commands/` + `agents/` + `skills/` into a `.claude-plugin/plugin.json`-anchored plugin so it's installable from a marketplace

## Maintenance

- Slash commands have frontmatter (`description`, `argument-hint`, `allowed-tools`); update them when behavior changes
- Hook scripts have inline comments + smoke-test invocations documented in `Docs/claude-setup-overhaul.md` WS#4
- Subagent descriptions drive WHEN Claude invokes them — keep them specific to maximize hit rate without false positives
