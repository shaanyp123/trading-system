# Claude Code Orientation

You are working on a solo-operator algorithmic trading system. The operator is non-coding (finance background, no programming) and relies on you for all implementation.

## Read these at the start of every session

1. **`Docs/claude-dev-guide.md` §1 (Session Protocol)** — how to start/end a session, ambiguity protocol, test-before-commit rule
2. **`Docs/claude-dev-guide.md` §1.5 (Locked Decisions Quick Reference)** — auth, SSE, endpoints, Phase 1 architecture, backtest authority, domain placeholder, email provider. Memorize these; never deviate without escalation.
3. **The relevant section of `implementation-guide.md`** for the current week/task (operator tells you which)
4. **The relevant `Docs/backend-spec.md` or `Docs/frontend-spec.md` section** if you're implementing against a specific subsystem

## Critical constraints

- **Phase 1 backend has NO direct IBKR connection.** Market data + broker state via QuantConnect ObjectStore push. Do NOT call TWS API directly until Phase 2 cutover.
- **Forbidden file path whitelist** — see `Docs/claude-dev-guide.md` §11 anti-pattern `[A02]`. Modifying `services/risk/**`, `services/signal/**`, `services/audit/**`, `services/execution/**`, `services/reconciliation/**`, `services/calibration/**`, `services/agent/decisions/**`, `services/agent/risk_actions/**`, `services/agent/parameter_changes/**`, `services/agent/prompts/decision/**`, or `alembic/**` requires `risk-review-approved` PR label. Pre-merge linter will block otherwise.
- **No `print()`, no stdlib `logging`** — use `structlog` with canonical fields (see dev guide §3).
- **No `float` for money** — use `decimal.Decimal`. Serialize as strings in API payloads.
- **No `bcrypt`** — Argon2id via `argon2-cffi`. (Locked.)
- **No SES** — Resend is the locked email provider.
- **No bare `<domain>` in code** — substitute the operator's registered apex at deployment.
- **No new SSE event types or audit event types** without enum migration.
- **Test before commit** — `make test` (or equivalent) passes locally before push. CI verifies; don't rely on it as the only gate.

## Workflow expectations

- The operator opens ONE Claude Code session per task at a time. They tell you which `implementation-guide.md` week/task to work on.
- You implement; the operator reviews via in-app PR review surface (§5.7 of dev guide describes the artifact generator). Diff is reference; the operator gates on plain-English summary + risk impact + backtest delta + tests.
- Verification gates at end of each Phase 0 week (`implementation-guide.md` §3) are mechanically testable (curl, grep, log line, Discord command). Don't claim done without running the gate.
- Hot-fix whitelist exists for infra changes (logging, retry, monitoring); see dev guide §10 + spec for paths. Strategy/risk/audit code goes through PR review.

## When in doubt, escalate

Escalate to the operator (don't decide unilaterally) when:
- Strategy logic ambiguity
- Risk-engine ambiguity
- A locked decision in `Docs/claude-dev-guide.md` §1.5 conflicts with a request
- A new canonical pattern is needed (propose; it goes into dev guide §5 via PR after approval)

For pattern ambiguity within already-allowed scope (naming, file organization within an allowed directory), decide using existing patterns and document the choice in the PR.

## File index

| Path | Purpose |
|---|---|
| `README.md` | Operator-facing top-level index |
| `CLAUDE.md` | This file — orientation for you |
| `implementation-guide.md` | Operator's daily handbook; week-by-week build plan + decision register + runbook |
| `Docs/backend-spec.md` | Backend architecture: schemas, APIs, risk framework, audit log, security |
| `Docs/frontend-spec.md` | Frontend architecture: pages, components, real-time, auth |
| `Docs/claude-dev-guide.md` | YOUR canonical patterns + conventions + anti-patterns + session protocol |
| `Prompts/` | Generation prompts (archived; do not reference for current work) |
| `Archive/` | Reserved for superseded versions |

When code lands during Phase 0: `apps/web/`, `services/<name>/`, `packages/api-types/`, `alembic/`, `deploy/`, `secrets/` (sops-encrypted), `scripts/`, `tests/`. See dev guide §2 for the canonical layout.
