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

### Code + ops surfaces (current state as of 2026-05-12, Day 11)
| Path | Purpose | Status |
|---|---|---|
| `strategies/v1_trend_following/` | V1 Donchian/MA/Hurst/ATR strategy logic; `parameters.py`, `indicators.py`, `signals.py`, `audit_events.py`, `sizing_trace.py`, `strategy.py` | Day 2 — entry pipeline real; exit pipeline scaffolded for Week 3–4 |
| `lean/v1_qc_algorithm.py` | QC LEAN wrapper for the strategy | Day 4 — paper-day clock STARTED on QC Paper Brokerage; snake_case API; full wiring Week 4 |
| `tests/unit/test_strategy_v1.py` | 16 tests covering entry pipeline + indicators + parameter validation | Day 2 |
| `tests/unit/test_audit_chain.py` | 22 tests covering JCS canonicalization (Decimal-as-str, float rejection per A05, key-sort determinism), SHA-256 record-hash math, GENESIS_HASH | Day 8 — pure Python, no Docker (PR #39) |
| `tests/integration/test_audit_immutability.py` | testcontainers Postgres 16; 6 tests verifying audit_log UPDATE/DELETE/TRUNCATE blocked + attribution.expected_* immutable | Day 3 — passes when Docker is up; skips cleanly otherwise |
| `tests/integration/test_audit_writer.py` | testcontainers Postgres 16; 4 tests: single insert chain math, genesis prev_hash = zero32, 3 concurrent writers (continuous chain), deterministic SQLSTATE 40001 retry-path | Day 8 — passes when Docker is up; skips cleanly otherwise (PR #39) |
| `services/api/` | FastAPI skeleton (`main.py`, `config.py`, `db.py`, `entrypoint.py`, `errors.py`, `middleware.py`, `routes/`, `repos/`); structlog + Postgres pool | Day 5 — healthy on Ashburn (`/api/health` 200 over loopback); TLS end-to-end verified Day 6 carryover |
| `services/risk/` | `sizing.py` (Stage 0 universe filter, $15k/$25k/$50k/$100k tiers, /MES 50%-override) + `state_machine.py` (kill-switch transitions) | Day 6-9 chain — pure-policy modules shipped early via PR #28 (`risk-review-approved`) |
| `services/audit/` | `event_types.py` (locked taxonomy enum mirror of backend-spec §3.30) + `models.py` (`AuditLogRecord` DTO) + `chain.py` (in-tree JCS + SHA-256 record-hash + `verify_chain`) + `writer.py` (`append_audit_event` — `pg_advisory_xact_lock` + SERIALIZABLE + 5-attempt SQLSTATE-40001 retry) + `decision_diary.py` validator (PR #28) | Day 8 — canonical hash-chain entry point shipped via PR #39 (`risk-review-approved`); A01 enforceable from this PR forward |
| `services/scheduler/` | `vacation.py` mode handler + `calendar_import.py` macro-events seeding | Day 6-9 chain — shipped early via PR #28 (`risk-review-approved`) |
| `services/qc_adapter/` | `payloads.py` JSONL parser + `cursor.py` (`qc_adapter_cursor` row + canonical 3-directory enum) + `poll.py` (`plan_ingest_batch` orchestrator playbook) | Week 3 Tue scaffold — pure-policy plan-then-apply; HTTP fetcher + audit writer integration land Week 4 |
| `services/reconciliation/` | `recon.py` (`plan_reconciliation_check` — pure-policy diff returning `ReconciliationPlan` dataclass; tolerances 0/$5/1bps + 2× dividend widening + T+1 grace via prior_breaks; emits `reconciliation_check_passed` / `_break_detected` / `_break_resolved` audit events) | Day 9 — pure-policy plan-then-apply via PR #42 (`risk-review-approved`); Week 4 dispatcher wires it into the recon cron + RECON_MISMATCH kill-switch trigger |
| `tests/unit/test_reconciliation.py` | 45 tests across 8 `Test*` classes — exact match, position mismatch, cash abs/bps tolerances, T+1 grace, dividend 2× widening, multiple breaks, resolved priors, A06 timezone enforcement, A05 Decimal-as-str, canonical taxonomy validation | Day 9 — pure Python, no Docker (PR #42) |
| `services/webhook_pusher/` | `payloads.py` (`plan_alert_dispatch` pure-policy planner; severity → channel routing locked: P0 → `{#alerts, #critical, email}`; P1/P2 → `#alerts`; Discord embed shape + Resend email shape; `AlertCategory` 29-value enum mirror of `alert_category` Postgres enum + spec §3.27) + `sender.py` (async `post_outbound_message` with explicit User-Agent per Day 4 PR #21 lesson; HTTP status → `DeliveryStatus` mapping; single 429 retry with Discord JSON `retry_after` preferred over header) + `dispatcher.py` (`dispatch_alert` orchestrator; SELECT alerts row → short-circuit on existing `delivery_status` for idempotency → fan out via sender → UPDATE JSONB) + `cli.py` (operator smoke `python -m services.webhook_pusher.cli` bare-smoke + `--with-db` modes) | Day 10 — alerts delivery surface shipped via PR #44 (regular PR review; off both whitelists per §2.2 + §2.3); 58 unit tests; A22 + A27 (operator runbook) + A04 enforced. Week 4 risk dispatcher wires `dispatch_alert(...)` into kill-switch transitions + recon breaks + audit-chain breaks |
| `tests/unit/test_webhook_pusher.py` | 58 tests across 14 `Test*` classes — locked constants, severity routing, Discord embed shape (color/title/footer/fields/truncation/MAX cap/no-auth), Resend email shape (subject prefix/body detail/from-to/Bearer auth), planner errors (missing URL/missing email_identity/A06), planner determinism, sender HTTP status mapping (8 outcomes), 429 retry semantics (Discord JSON body preferred + cap), transport failures, header attachment, dispatcher idempotency + fan-out + alert-not-found, DeliveryStatus + ChannelName wire values, AlertCategory count==29 | Day 10 — respx-mocked HTTP, fake AsyncSession, no Docker (PR #44) |
| `deploy/webhook_pusher/README.md` | 8-step operator runbook (A27 satisfier per dev-guide §6.8 alternative (b)): Step 1 sops decrypt + URL extract; Step 2 stage env vars; Step 3 bare-smoke (P2); Step 4 find account_id; Step 5 P0 full roundtrip (`#alerts` + `#critical` + Resend); Step 6 psql verify `delivery_status` JSONB; Step 7 idempotency note; Step 8 cleanup (shred decrypted yaml). Closes Week 3 verification gate box 4 when operator runs Steps 3+5+6 | Day 10 — same shape as `deploy/api/README.md` Day 5 + `watchdog/README.md` Day 4 + `lean/README.md` Day 4 (PR #44) |
| `tests/golden/fixtures/qc_events/` | 5 representative QC §4.5.1 wire envelopes — `01_signal_emitted.json` (495-byte JCS, sizing_trace nested dict), `02_order_filled.json` (308-byte JCS, links to fixture 01 signal_uuid), `03_reconciliation_check_passed.json` (304-byte, mirrors PR #42 recon shape), `04_kill_switch_triggered.json` (267-byte), `05_system_stopped.json` (200-byte). All Decimal-as-string per A05; all timestamps `+00:00` per A06. event_type values use the locked `services/audit/event_types.py` taxonomy (NOT IG §3 Week 4 Mon's casual prose names) | Day 11 — pure data fixtures (PR #45) |
| `tests/golden/test_qc_parity.py` | 18 tests across 5 `Test*` classes — `TestRecordHashFromGenesis` (5: per-fixture deterministic record_hash from GENESIS, hex baked-in to module), `TestChainLinksForward` (2: sequential walk + first-link cross-check; tail hex baked-in), `TestRoundTripViaQCAdapterParser` (5: parser-is-payload-identity per fixture; re-asserts record_hash post-parse), `TestModuloThreeMutableFields` (2: `{ingest_clock_ts, ingest_uuid, sequence_no}` audit-side metadata MUST NOT appear in QC wire payload, top-level + recursive nested walk), `TestFixtureSchemaShape` (4: 5 canonical top-level keys + A04 taxonomy + A06 timezone + filesystem-listing match). `@pytest.mark.golden`; runs via `make test-golden` in 0.61s | Day 11 — pure Python, no Docker, A22 binds (zero audit_log INSERTs, zero testcontainers, zero mocking); A27 does NOT bind (no third-party platform contract); closes IG §3 Week 4 verification gate box 1 (PR #45) |
| `watchdog/watchdog.py` + systemd unit + timer | External watchdog: `GET /api/health` poll, Discord webhook alert on degraded, 60-min cooldown | Day 4 — deployed to Hetzner Nuremberg (`trading-watchdog`); apex URL fix Day 6 carryover |
| `deploy/github-app/` | Canonical manifest + operator runbook for the in-app PR review surface app | Day 2 — app created (App ID 3615825 / Installation ID 129868686) |
| `deploy/discord/` | Canonical manifest + operator runbook for the Discord guild + bot (7 channels) | Day 2 — guild + bot created |
| `deploy/sops/` | Operator runbook + per-env secret schema templates (`secret_schemas/{dev,paper,live}.template.yaml`) | Day 2 — runbook executed; templates ready for Day 3 encryption |
| `.sops.yaml` | sops creation rules with 3 real age recipients (dev/paper/live) | Day 2 |
| `scripts/sops_init.sh` | Helper that substitutes age pubkeys into `.sops.yaml` (idempotent) | Day 2 |
| `secrets/paper.enc.yaml` | Paper-env encrypted file. All Day-2 → Day-5 captured values filled (QC API token, Resend `re_*` API key + from-address, Discord webhook URLs, GitHub App private key, Postgres role passwords) | Day 6 carryover — filled and committed from VPS via PR #29 (Option B; ends `git reset --hard` data-loss class) |
| `secrets/{dev,live}.enc.yaml` | Dev + live encrypted env files. App ID + Installation ID + IBKR account + webauthn rp_id substituted; remaining values are `<TODO>` placeholders, operator pastes via `sops <file>` later | Day 3 |
| `alembic.ini`, `alembic/env.py`, `alembic/script.py.mako` | Migration runner config; reads `DATABASE_URL` from env (sops at deploy time) | Day 3 |
| `alembic/versions/0001_audit_log.py` | `audit_log` partitioned by `ingest_clock_ts`; yearly partitions 2026–2031; `uuid_generate_v7()` SQL function | Day 3 |
| `alembic/versions/0002_core_tables.py` | accounts, setup_tokens, contracts, signals, orders, fills, trades, attribution, positions, balances; orders/fills/trades/attribution partitioned by year | Day 3 |
| `alembic/versions/0003_risk_tables.py` | strategy_versions, parameters, parameter_sets, slippage_calibration_versions, decision_diary, risk_state; closes deferred FKs from 0002 | Day 3 |
| `alembic/versions/0004_ops_tables.py` | reconciliation_breaks, data_quality_events, agent_actions, vacation_mode, qc_adapter_cursor (TABLE + 3 §3.19 INSERT rows), capital_events, cost_events, liveness_probes, pdt_day_trade_log, dividend_history, incident_reviews, universe_state, alerts (+`alert_category` enum), macro_events | Day 3 |
| `alembic/versions/0005_immutability.py` | audit_log BEFORE UPDATE/DELETE blocker, BEFORE TRUNCATE blockers (parent + each yearly partition), attribution expected_* lock, REVOKE TRUNCATE FROM PUBLIC | Day 3 |
| `alembic/versions/0006_roles.py` | app_service, app_service_readonly, app_owner (NOLOGIN), dba_breakglass (SUPERUSER NOLOGIN); per-role grants; per-role audit_log REVOKEs; passwords set out-of-band from sops | Day 3 |
| `alembic/versions/2026-05-09_qc_adapter_cursor_seed.py` | Defensive idempotent re-seed of qc_adapter_cursor (`ON CONFLICT DO NOTHING` upgrade + intentional no-op downgrade); first **operational** migration under dev-guide §7.1 hybrid scheme | Day 8 — no-op against current schema since 0004 already inserts (PR #40) |
| `infrastructure/`, `apps/web/`, `packages/`, remaining `services/*` (signal, execution, calibration, agent, monitoring, observability, discord_bot) | Scaffolded directories with `__init__.py` only | filled Week 3+ per `implementation-guide.md` §3 |

See `Docs/claude-dev-guide.md` §2 for the canonical full-repo layout.
