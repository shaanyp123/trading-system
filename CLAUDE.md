# Claude Code Orientation

You are working on a solo-operator algorithmic trading system. The operator is non-coding (finance background, no programming) and relies on you for all implementation.

## Read these at the start of every session

1. **`Docs/claude-dev-guide.md` §1 (Session Protocol)** — how to start/end a session, ambiguity protocol, test-before-commit rule
2. **`Docs/claude-dev-guide.md` §1.5 (Locked Decisions Quick Reference)** — auth, SSE, endpoints, Phase 1 architecture, backtest authority, domain placeholder, email provider. Memorize these; never deviate without escalation.
3. **`Docs/decisions-log.md`** — canonical log of where reality differs from the specs. Read before assuming a spec value (monthly costs, Hetzner DC, hardware SKUs, third-party pricing) is current.
4. **The relevant section of `implementation-guide.md`** for the current week/task (operator tells you which)
5. **The relevant `Docs/backend-spec.md` or `Docs/frontend-spec.md` section** if you're implementing against a specific subsystem

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

### Reference docs (read at session start)
| Path | Purpose |
|---|---|
| `README.md` | Operator-facing top-level index |
| `CLAUDE.md` | This file — orientation for you |
| `implementation-guide.md` | Operator's daily handbook; week-by-week build plan + decision register + runbook |
| `Docs/backend-spec.md` | Backend architecture: schemas, APIs, risk framework, audit log, security |
| `Docs/frontend-spec.md` | Frontend architecture: pages, components, real-time, auth |
| `Docs/claude-dev-guide.md` | YOUR canonical patterns + conventions + anti-patterns + session protocol |
| `Docs/decisions-log.md` | Append-only log of decisions + deviations from specs as the build progresses |
| `Prompts/` | Generation prompts (archived; do not reference for current work) |
| `Archive/` | Reserved for superseded versions |

### Code + ops surfaces (current state as of Day 3)
| Path | Purpose | Status |
|---|---|---|
| `strategies/v1_trend_following/` | V1 Donchian/MA/Hurst/ATR strategy logic; `parameters.py`, `indicators.py`, `signals.py`, `audit_events.py`, `sizing_trace.py`, `strategy.py` | Day 2 — entry pipeline real; exit pipeline scaffolded for Week 3–4 |
| `lean/v1_qc_algorithm.py` | QC LEAN wrapper for the strategy | Day 2 — heartbeat-only; full wiring Week 4 |
| `tests/unit/test_strategy_v1.py` | 16 tests covering entry pipeline + indicators + parameter validation | Day 2 |
| `tests/integration/test_audit_immutability.py` | testcontainers Postgres 16; 6 tests verifying audit_log UPDATE/DELETE/TRUNCATE blocked + attribution.expected_* immutable | Day 3 — passes when Docker is up; skips cleanly otherwise |
| `deploy/github-app/` | Canonical manifest + operator runbook for the in-app PR review surface app | Day 2 — app created (App ID 3615825 / Installation ID 129868686) |
| `deploy/discord/` | Canonical manifest + operator runbook for the Discord guild + bot (7 channels) | Day 2 — guild + bot created |
| `deploy/sops/` | Operator runbook + per-env secret schema templates (`secret_schemas/{dev,paper,live}.template.yaml`) | Day 2 — runbook executed; templates ready for Day 3 encryption |
| `.sops.yaml` | sops creation rules with 3 real age recipients (dev/paper/live) | Day 2 |
| `scripts/sops_init.sh` | Helper that substitutes age pubkeys into `.sops.yaml` (idempotent) | Day 2 |
| `secrets/{dev,paper,live}.enc.yaml` | Encrypted env files. App ID + Installation ID + IBKR account + webauthn rp_id substituted; remaining values are `<TODO>` placeholders, operator pastes via `sops <file>` later | Day 3 |
| `alembic.ini`, `alembic/env.py`, `alembic/script.py.mako` | Migration runner config; reads `DATABASE_URL` from env (sops at deploy time) | Day 3 |
| `alembic/versions/0001_audit_log.py` | `audit_log` partitioned by `ingest_clock_ts`; yearly partitions 2026–2031; `uuid_generate_v7()` SQL function | Day 3 |
| `alembic/versions/0002_core_tables.py` | accounts, setup_tokens, contracts, signals, orders, fills, trades, attribution, positions, balances; orders/fills/trades/attribution partitioned by year | Day 3 |
| `alembic/versions/0003_risk_tables.py` | strategy_versions, parameters, parameter_sets, slippage_calibration_versions, decision_diary, risk_state; closes deferred FKs from 0002 | Day 3 |
| `alembic/versions/0004_ops_tables.py` | reconciliation_breaks, data_quality_events, agent_actions, vacation_mode, qc_adapter_cursor, capital_events, cost_events, liveness_probes, pdt_day_trade_log, dividend_history, incident_reviews, universe_state, alerts (+`alert_category` enum), macro_events | Day 3 |
| `alembic/versions/0005_immutability.py` | audit_log BEFORE UPDATE/DELETE blocker, BEFORE TRUNCATE blockers (parent + each yearly partition), attribution expected_* lock, REVOKE TRUNCATE FROM PUBLIC | Day 3 |
| `alembic/versions/0006_roles.py` | app_service, app_service_readonly, app_owner (NOLOGIN), dba_breakglass (SUPERUSER NOLOGIN); per-role grants; per-role audit_log REVOKEs; passwords set out-of-band from sops | Day 3 |
| `services/`, `infrastructure/`, `apps/web/`, `packages/`, `watchdog/` | Scaffolded directories with `__init__.py` only | filled Days 4–8 + Week 5+ per `implementation-guide.md` §3 |

See `Docs/claude-dev-guide.md` §2 for the canonical full-repo layout.
