# Claude Code Orientation

You are working on a solo-operator algorithmic trading system. The operator is non-coding (finance background, no programming) and relies on you for all implementation.

> **🔴 PIVOT IN PROGRESS (2026-07-08): the IBKR/LEAN/CME system described below is being RETIRED.** The repo is pivoting to Coinbase CFM crypto perpetual-style futures. Read, in order: `Docs/recent-architecture-changes.md` (final entry), `Docs/crypto-perps-strategy.md` (+ Amendments), `research/crypto_perps/REPORT.md`, and **`Docs/crypto-pivot-delta-spec.md`** (the build plan) before trusting anything below about IBKR, LEAN, bar_sync, or the daily cycle. Chassis rules (audit chain, forbidden paths, structlog/Decimal/Argon2id/Resend, SSE discipline) remain in force.

## Operational status

> **🚀 Paper trading is OPERATIONAL.** Day 1 was 2026-05-20. Daily cycle: 17:00 ET `BarSyncWorker` → 21:00 UTC bar_sync end → 21:10 UTC `lean_local` systemd restart → 21:30 UTC LEAN signal cycle → 22:30 UTC EOD reconciliation. Live-money cutover (`live-small` env tag) remains a future Phase milestone — today is paper-only.

> **🔄 Architecture: post-pivot Phase 1.** Direct IBKR via `ib-async` to a Dockerized `ib_gateway` sidecar; LEAN runs locally in `lean_local`; api owns the bar-fetch responsibility via `services/data/bar_sync.py` (`clientId=3`) writing LEAN's on-disk format to a shared Docker volume. The "Phase 1 → Phase 2 cutover" split from older spec text is RETIRED.

**For the full pivot chain** (architecture pivot 2026-05-12 + data-layer pivot v2 2026-05-21 + LEAN futures saga 2026-05-22 → 2026-05-24): read `Docs/recent-architecture-changes.md` BEFORE consulting any other foundation doc. Skipping it means recommending retired patterns.

## Read these at the start of every session

1. **`Docs/recent-architecture-changes.md`** — chronological pivot log; supersedes earlier spec text on Phase 1 architecture + data layer
2. **`Docs/claude-dev-guide.md` §1 (Session Protocol)** — how to start/end a session, ambiguity protocol, test-before-commit rule
3. **`Docs/claude-dev-guide.md` §1.5 (Locked Decisions Quick Reference)** — auth, SSE, endpoints, Phase 1 architecture, backtest authority, domain placeholder, email provider, clientId allocations. Memorize these; never deviate without escalation.
4. **`Docs/decisions-log.md`** — canonical log of where reality differs from the specs. Read before assuming a spec value (monthly costs, Hetzner DC, hardware SKUs, third-party pricing) is current.
5. **The relevant section of `implementation-guide.md`** for the current week/task (operator tells you which)
6. **The relevant `Docs/backend-spec.md` or `Docs/frontend-spec.md` section** if you're implementing against a specific subsystem
7. **`Docs/file-index.md`** — code + ops surfaces snapshot with current status, recent PR history, forbidden-path annotations. Reference when you need to know "what's in services/risk/" or "what was PR #232".

## Critical constraints

- **Phase 1 backend HAS direct IBKR connection (post-pivot 2026-05-12).** Market data + broker state via `ib-async` to a Dockerized `ib_gateway` container. LEAN runs locally on the VPS in a `lean_local` container and POSTs signal events to `POST /api/internal/lean/signals`. **Pre-pivot rule (RETIRED):** "Phase 1 backend has NO direct IBKR connection." See `Docs/recent-architecture-changes.md` for full context + `Docs/claude-dev-guide.md` §1.5 + anti-pattern `[A13]` revised.
- **Forbidden file path whitelist** — see `Docs/claude-dev-guide.md` §11 anti-pattern `[A02]`. Modifying `services/risk/**`, `services/signal/**`, `services/audit/**`, `services/execution/**`, `services/reconciliation/**`, `services/calibration/**`, `services/agent/decisions/**`, `services/agent/risk_actions/**`, `services/agent/parameter_changes/**`, `services/agent/prompts/decision/**`, or `alembic/**` requires `risk-review-approved` PR label. Pre-merge linter will block otherwise. **The IBKR client at `services/execution/ibkr_client.py` is on this list; the LEAN signals endpoint at `services/api/routes/internal/lean.py` is on the §2.3 hot-fix whitelist via `services/api/**`.**
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
- Hot-fix whitelist exists for infra changes (logging, retry, monitoring); see dev guide §2.3 + §10 for paths. Strategy/risk/audit code goes through PR review.

## When in doubt, escalate

Escalate to the operator (don't decide unilaterally) when:
- Strategy logic ambiguity
- Risk-engine ambiguity
- A locked decision in `Docs/claude-dev-guide.md` §1.5 conflicts with a request
- A new canonical pattern is needed (propose; it goes into dev guide §5 via PR after approval)

For pattern ambiguity within already-allowed scope (naming, file organization within an allowed directory), decide using existing patterns and document the choice in the PR.

## Reference docs (read at session start)

| Path | Purpose |
|---|---|
| `README.md` | Operator-facing top-level index |
| `CLAUDE.md` | This file — orientation for you |
| `Docs/recent-architecture-changes.md` | Pivot log; supersedes older spec text on architecture + data layer |
| `Docs/file-index.md` | Code + ops surfaces snapshot (status, PR history, forbidden-path notes) |
| `implementation-guide.md` | Operator's daily handbook; week-by-week build plan + decision register + runbook |
| `Docs/backend-spec.md` | Backend architecture: schemas, APIs, risk framework, audit log, security |
| `Docs/frontend-spec.md` | Frontend architecture: pages, components, real-time, auth |
| `Docs/claude-dev-guide.md` | YOUR canonical patterns + conventions + anti-patterns + session protocol |
| `Docs/decisions-log.md` | Append-only log of decisions + deviations from specs as the build progresses |
| `Docs/live-money-cutover-plan.md` | Phase-future plan for live-small env tag — not currently active |
| `Docs/ai-and-strategy-overview.md` | High-level system context |
| `Prompts/` | Generation prompts (archived; do not reference for current work) |
| `Archive/` | Reserved for superseded versions |

For the full file index of code + ops surfaces (services/, lean/, apps/, deploy/, etc.), see `Docs/file-index.md`.
