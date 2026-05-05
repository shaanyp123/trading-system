# CLAUDE CODE DEV GUIDE GENERATOR

## ROLE

You are a senior staff engineer who has shipped multiple production trading systems in Python + TypeScript. You are obsessive about coding conventions because you have lived through what happens when they aren't enforced: drift across pull requests, inconsistent error handling that masks real bugs, audit-log writers that race-condition under load, secrets accidentally committed, frontend bundles that balloon past performance budgets.

You are producing the **Claude Code Dev Guide** — the canonical patterns + conventions document that every future Claude Code coding session reads at session start. This document is read by AI agents (specifically Claude Code), not by humans, and its job is to keep the implementation consistent across hundreds of sessions over the 12-month build.

This is NOT another spec. The specs say WHAT the system is. The implementation guide says WHEN to build each piece. This dev guide says **HOW Claude Code writes the code** — patterns, conventions, anti-patterns, exact templates.

## INPUT MATERIALS

Three complete documents at:
- `/Users/shaanpatel/Documents/GitHub/Trading/backend-spec.md` (~4500 lines)
- `/Users/shaanpatel/Documents/GitHub/Trading/frontend-spec.md` (~4900 lines)
- `/Users/shaanpatel/Documents/GitHub/Trading/implementation-guide.md` (~2000 lines)

You don't need to read these end-to-end. Use Grep + targeted Read to find:
- Specific patterns the specs reference (e.g., audit-log writer, JCS canonicalization, advisory lock, SSE event emitter, kill-switch state machine)
- Locked tooling (Python 3.11+, asyncpg, SQLAlchemy 2.x async, Alembic, structlog, pydantic v2, FastAPI, Next.js 14+, TanStack Query, shadcn/ui, etc.)
- Anti-patterns explicitly forbidden (don't write to audit_log without advisory lock; don't modify risk engine without `risk-review-approved` label; etc.)

## AUDIENCE

The reader is a Claude Code session about to write code in this repo. The reader has full context of the current task but no memory of prior sessions. The dev guide is the persistent context that keeps every session aligned.

Write in second person ("you") addressed to the AI agent. Be direct and dense. No hedging.

## OPERATOR CONTEXT (binding for review-checklist sections)

- Solo non-coding operator. They review code by checking that conventions are followed, NOT by reading the code itself.
- Review surface is the in-app PR review surface (plain-English summary + risk impact + backtest delta + tests). Diff is reference, not the gate.
- Operator's "did Claude Code do it right?" check = "did Claude Code follow the documented patterns?"

## OUTPUT

Write the complete dev guide to a NEW file at `/Users/shaanpatel/Documents/GitHub/Trading/claude-dev-guide.md`.

**Length target: 1500–2500 lines.** Favor concrete code snippets over prose. Every pattern should have a runnable example.

## REQUIRED SECTIONS

### 1. Session Protocol (what Claude Code does at start and end of every coding session)

- **Session start:** files to read in order (CLAUDE.md if present, this dev guide, the relevant spec section, the relevant implementation-guide week, any open PRs); how to load context efficiently; how to verify branch state; how to check whether prior sessions left work in flight (uncommitted changes, draft PRs)
- **Session end:** what to commit (one logical change per commit; conventional-commits style: `feat(scope): description`); how to update implementation-guide verification gates if a Phase 0 task completed; how to draft PR via in-app review surface vs direct merge for hot-fix whitelist; what to leave for the operator
- **Ambiguity protocol:** when to escalate vs decide. Rules:
  - Strategy logic ambiguity → ALWAYS escalate (operator decision; PR-blocking)
  - Risk-engine ambiguity → ALWAYS escalate
  - Pattern ambiguity within whitelist (e.g., naming, file organization within `services/observability/`) → decide using existing patterns in the repo; document the choice in the PR
  - New canonical pattern needed → escalate; propose; once approved, add to this dev guide via PR
- **Test-before-commit rule:** every code change runs `make test` (or equivalent) locally first; CI verifies; no exceptions

### 2. Repo Layout (canonical structure)

Document the pnpm workspace + Python package structure. Reference the implementation guide and specs for exact paths. Cover:

- `apps/web/` — Next.js + TypeScript
- `services/discord-bot/` — Python + discord.py
- `services/api/` — FastAPI backend
- `services/risk/` — risk engine (forbidden whitelist)
- `services/signal/` — signal generation (forbidden whitelist)
- `services/audit/` — audit log + hash chain (forbidden whitelist)
- `services/execution/` — order placement (forbidden whitelist)
- `services/reconciliation/` — TWS + FlexQuery reconciliation (forbidden whitelist)
- `services/calibration/` — slippage calibration (forbidden whitelist)
- `services/agent/decisions/`, `services/agent/risk_actions/`, `services/agent/parameter_changes/` — forbidden whitelist
- `services/agent/reporting/`, `services/agent/monitoring/`, `services/agent/integrations/`, `services/agent/prompts/system/` — allowed for hot-fix whitelist
- `services/observability/`, `services/monitoring/`, `infrastructure/retry/`, `infrastructure/broker_reconnect/`, `infrastructure/logging/` — allowed for hot-fix whitelist
- `packages/api-types/` — TypeScript types codegen'd from FastAPI's OpenAPI
- `alembic/` — DB migrations (forbidden whitelist; PR required)
- `deploy/` — Caddyfile, docker-compose, systemd units
- `secrets/` — sops-encrypted env files (`dev.enc.yaml`, `paper.enc.yaml`, `live.enc.yaml`)
- `tests/unit/`, `tests/integration/`, `tests/e2e/` — test split
- `scripts/` — operational scripts (`rotate-secrets.sh`, `verify_export.py`, etc.)

For each, state: what goes in it, what doesn't, hot-fix whitelist status.

### 3. Python Backend Coding Standards

Cover:
- Python version: 3.11+
- Formatter: `ruff format` (Black-compatible); `ruff check --fix` for lint
- Type checker: `mypy --strict` mandatory; no `Any`, no implicit `Optional`
- Type hints: every function signature; every dataclass; pydantic v2 for any boundary type
- Naming: `snake_case` functions/vars, `PascalCase` classes, `UPPER_CASE` constants, `_leading_underscore` for module-private
- Async-first: every IO function `async def`; never block the event loop; use `asyncio.gather` for concurrent calls
- Error envelope: ALL FastAPI errors return `{error_code: str, message: str, details: dict | None}`; provide a base exception class `AppError` that maps to this envelope via FastAPI exception handler
- Logging: `structlog` with the canonical fields (`event`, `level`, `timestamp_utc`, `monotonic_ns`, `service_name`, `audit_event_uuid` if relevant, `signal_uuid` if relevant, `strategy_hash` if relevant); never use `print` or `logging` directly
- Datetime: always `TIMESTAMPTZ` UTC at storage; always render via `formatET()` helper at presentation layer; never trust browser clock
- Decimal precision: use `decimal.Decimal` for all money/price values; never `float`; serialize as strings in API payloads
- Idempotency keys: UUIDv7 for all writes (use `uuid7` package); for `client_order_id` use the locked 33-char format

Provide a code snippet for each: example function with full type hints, example logger usage, example pydantic model, example error.

### 4. TypeScript Frontend Coding Standards

Cover:
- TypeScript strict mode in `tsconfig.json` (no implicit any, strict null, strict function types)
- ESLint + Prettier (or Biome) configured
- Naming: `camelCase` functions/vars, `PascalCase` components/types, `kebab-case` files
- React patterns: server components by default; `"use client"` only when needed (interactivity, hooks); no class components
- Data fetching: TanStack Query exclusively for server state; never `useEffect` + `fetch`; staleness per locked stale-data thresholds
- Client state: Zustand for narrow client-only state; never duplicate server state
- Forms: react-hook-form + zod schemas
- Datetime: ALL date rendering via `formatET()` helper from `apps/web/lib/datetime.ts`; never use raw `Date.toLocaleString` or `Intl.DateTimeFormat` directly in components
- Numbers: ALL numeric values rendered via `formatNumber()` / `formatPnL()` / `formatPct()` helpers; tabular-nums CSS applied via component primitive
- Imports: ordered (1) external packages, (2) `@/` aliased internal, (3) relative; no circular imports
- File size: components ≤300 lines; if larger, split

Provide snippets: a fully-typed component, a TanStack Query hook, a zod schema + form, a Zustand store.

### 5. Architecture Patterns (canonical implementations — ACTUAL CODE)

Provide a working code template for each. Each is a pattern Claude Code will use over and over; it must be canonical and consistent.

#### 5.1 Audit-log Writer
Postgres advisory lock + SERIALIZABLE transaction + JCS canonicalization + retry on serialization failure. Provide the full async function:

```python
async def append_audit_event(
    session: AsyncSession,
    event_type: str,
    payload: dict[str, Any],
    *,
    repaired_for_sequence_no: int | None = None,
    repaired_for_event_timestamp: datetime | None = None,
) -> AuditLogRecord:
    """Append-only audit log writer with hash-chain integrity.
    
    Concurrency: SERIALIZABLE isolation + advisory lock on AUDIT_CHAIN_LOCK_ID.
    Canonical serialization: JCS (RFC 8785) for hash determinism.
    Retry: 5x exponential backoff on serialization failure; HALT_NEW after 5.
    """
    # Full implementation here — show the retry loop, the lock acquisition,
    # the prev_hash query, the JCS serialization, the hash computation, the INSERT.
```

Include: the exact retry decorator, the JCS helper import, the advisory lock ID constant.

#### 5.2 SSE Event Emitter
Server-side helper that emits events with global monotonic `sequence_no`, RFC-3339-ms `server_now`, JCS-canonical payload. Show the full helper class with `emit(event_type, data)` method.

#### 5.3 Kill-Switch State Machine
The state transition handler with severity flag. Show: trigger detection → atomic state update → audit emit → SSE broadcast → alert routing. Include the `KillSwitchSeverity` enum, the `RiskState` enum, the transition validator (you can't go NORMAL → CONVALESCENT directly).

#### 5.4 QC Instruction Processor (Phase 1)
Idempotent processor that reads `/instructions/<sequence_no>.json` from QC ObjectStore, dedups by `instruction_id`, executes, writes ack to `/instruction_acks/<sequence_no>.json`. Include the `Instruction` and `Acknowledgment` pydantic models.

#### 5.5 Reconciliation Diff
TWS API real-time vs FlexQuery EOD diff with tolerance bands per the locked Reconciliation Tolerances Table. Show: pull both, compute deltas, apply tolerance + grace periods (T+1 for fees/dividends), emit `reconciliation_break` audit event if exceeded.

#### 5.6 Position-Sizing Algorithm (Stages 0-5)
The full algorithm: Stage 0 universe filter → Stage 1 inverse-vol weighting → Stage 2 per-position cap with 50% override → Stage 3 cluster shrink-to-fit (≤10 iterations, 0.1% tolerance) → Stage 4 gross/net cap → Stage 5 lot-rounding (banker's rounding; drop sub-minimum). Each stage is a function with explicit pre/post conditions documented.

#### 5.7 PR Review Surface Artifact Generator
The function that produces the 7 artifacts (plain-English summary, risk impact summary, backtest delta, test results, files affected, diff, in-app buttons) and stores them on the `prs` row. Include the prompts agent uses to generate the plain-English summary (locked template).

#### 5.8 Vol-Target Multiplier Composition
The MIN-of-multipliers function. Show: read each active multiplier (`m_capital_event`, `m_convalescent`, `m_monthly_dd`), MIN them with implicit 1.0 floor, return `m_combined`. Include unit-test cases for the example combinations from the spec.

#### 5.9 Operator-Friendly PR Plain-English Summary Template
The exact prompt used for the agent's plain-English summary generation: max 200 words, structure (what changed / why / behavior change). Include the system-prompt template used.

### 6. Testing Patterns

Cover:
- **Unit test structure:** one test file per source file under `tests/unit/`; pytest fixtures via `conftest.py`; arrange-act-assert pattern
- **Integration test structure:** under `tests/integration/`; use `testcontainers` for Postgres; mock QC ObjectStore via fake S3-compatible store; mock IBKR via `ib-async` test fixtures
- **E2E patterns:** Playwright for web flows; WebAuthn virtual authenticator (Chrome DevTools Protocol)
- **Golden-test parity for QC adapter:** how to write one — fixtures, expected JCS-canonical outputs, byte-for-byte comparison modulo `{ingest_clock_ts, ingest_uuid, sequence_no}`
- **vectorbt-vs-LEAN parity test:** how to write — same strategy code, same data, run both, compare per-trade slippage diff (≤5bps), aggregate P&L (≤0.5% starting equity), trade count (≤5%)
- **Mock conventions:** prefer real test containers over mocks for DB; mock external services (QC, IBKR, Anthropic API, Resend) via `respx` for HTTP and `pytest-asyncio` for async
- **Test naming:** `test_<function>_<condition>_<expected>` (e.g., `test_audit_append_concurrent_writers_serialize_correctly`)
- **Coverage:** `pytest --cov` with minimum 90% for `services/risk/`, `services/audit/`, `services/execution/`; 70% elsewhere
- **CI gates:** every PR runs unit + integration; weekly cron runs golden-test + vectorbt-vs-LEAN parity

Provide example test for each category.

### 7. Database Patterns

Cover:
- **Alembic migration conventions:** filename format `YYYY-MM-DD_<short_description>.py`; one logical change per migration; `upgrade()` AND `downgrade()` always implemented and tested
- **Additive vs transformative:** additive deploys without downtime; transformative requires maintenance window (Sat 17:00 ET to Sun 18:00 ET); migration runner must check for active CME session and abort if running outside window
- **Partition-by-year setup for `audit_log`:** how to add yearly partitions via Alembic; the cron job that runs Dec 31 each year
- **asyncpg connection pool:** sizing per-service; default `min_size=5, max_size=20`; statement timeout 30s for app, 60s for slippage-calibration jobs; isolation level `READ COMMITTED` default, `SERIALIZABLE` only for `audit_log` writes via context manager
- **Hash-chain canonicalization helper:** `jcs_serialize(payload: dict) -> bytes` using `pyjcs`; `compute_record_hash(prev_hash: bytes, payload_jcs: bytes) -> bytes`
- **Trigger SQL for immutability:** the exact `CREATE TRIGGER` and `EVENT TRIGGER` statements with `REVOKE TRUNCATE`

Provide an example Alembic migration following all conventions.

### 8. Frontend Patterns

Cover:
- **Next.js App Router conventions:** `app/` directory structure; `layout.tsx` per route group; `page.tsx` per route; server components by default; `"use client"` discipline
- **TanStack Query patterns:** query keys hierarchy `[domain, resource, params]` (e.g., `['signals', 'pending']`, `['trades', tradeId]`); refetch policies per stale-data threshold; invalidation on mutation
- **Zustand store patterns:** narrow stores per domain; no global store; persist only `auth_strength` and similar to localStorage
- **shadcn/ui composition:** prefer composing primitives over wrapping; tabular-nums via Tailwind class
- **SSE consumer:** custom hook `useSSE(eventTypes: EventType[])` using `@microsoft/fetch-event-source`; handles reconnect + replay via `last-event-id`
- **Form patterns:** react-hook-form + zod resolver; error display via shadcn Form components
- **Loading / error / stale states:** every data-driven component handles all four states (loading, error, partial-data, stale-data with yellow badge); no exceptions

Provide example custom hook + component for each pattern.

### 9. Cross-Cutting Patterns

Cover:
- **Secrets workflow:** sops decrypts at container start via init container; secrets exposed as env vars only; never write to disk in plaintext; rotation via `scripts/rotate-secrets.sh`
- **Config management:** all config from env vars; `pydantic-settings` for parsing + validation at boot; settings class is the single source of config truth
- **Feature flags:** `routes.config.ts` for per-route phase gating; `NEXT_PUBLIC_PHASE` env var for coarse phase gate; runtime feature flags forbidden (deployment-controlled only)
- **Error envelopes (frontend):** TanStack Query error handling shows toast on 4xx/5xx with the backend's `error_code` mapped to localized message; never show raw error text
- **Audit emission discipline:** every state-changing API endpoint emits an audit event before returning; the response includes `audit_event_uuid` for traceability

### 10. Phase-Specific Dev Priorities

For each phase, what to implement first within the phase, which integration points come first, what tests gate which deploys.

#### Phase 0 (weeks 0–8)
- Week 1 first build: FastAPI `/health` endpoint that returns 200; Postgres connection; structlog setup
- Week 2: audit_log table + hash chain + advisory lock writer
- Week 3: QC ObjectStore audit adapter scaffold (write-side from QC algo; poll-side from backend)
- Week 4: golden-test harness for QC parity; first parity run
- Week 5: REST scaffolding for Phase 1 endpoints; SSE channel
- Week 6: frontend Next.js scaffold; WebAuthn registration on `/setup`
- Week 7: end-to-end signal-to-fill paper round trip
- Week 8: buffer; competence assessment

#### Phase 1 (months 2–5)
- Month 2: monitoring stack first (Prometheus + alerting); then live trading begins
- Month 3: slippage recalibration cron; vectorbt-vs-LEAN parity test
- Month 4: PR review surface complete (operator-friendly artifacts)
- Month 5: cutover preparation

#### Phase 2 (months 5–9)
- ib-async integration with comprehensive test coverage BEFORE cutover
- Direct IBKR market data feed
- LEAN Local docker setup
- Cutover execution
- Vol-carry strategy preparation (sequential addition; new strategy version starts 30-day paper)

#### Phase 3 (months 9–12)
- Investor PDF generation (Typst)
- CPA reader role middleware (redaction)
- LLC/legal structure prep
- Family money onboarding flow

### 11. Anti-Patterns (DO NOT)

Explicit list. Each entry: what NOT to do, why, what to do instead.

Sample entries:
- DO NOT write to `audit_log` without acquiring `pg_advisory_xact_lock(AUDIT_CHAIN_LOCK_ID)` — silent hash chain corruption under concurrency. Use the `append_audit_event()` helper exclusively.
- DO NOT modify files in `services/risk/**`, `services/signal/**`, `services/audit/**`, `services/execution/**`, `services/reconciliation/**`, `services/calibration/**`, `services/agent/decisions/**`, `services/agent/risk_actions/**`, `services/agent/parameter_changes/**`, `services/agent/prompts/decision/**`, or `alembic/**` without an `risk-review-approved` PR label — the pre-merge linter will block the PR. PR through human review.
- DO NOT introduce a new SSE `event_type` without adding it to the locked enum in both backend Pydantic and frontend TypeScript types — type drift breaks the multiplexed channel. Add to enum + Alembic migration + codegen + tests.
- DO NOT introduce a new audit `event_type` without adding it to the locked taxonomy in backend §3 — schema can't be locked otherwise. Add to enum + tests + at least one test that emits and reads back.
- DO NOT use `float` for money — use `decimal.Decimal`. Serialize as string in API payloads.
- DO NOT use `datetime.now()` without timezone — always `datetime.now(tz=UTC)`. Always render via `formatET()` at presentation layer.
- DO NOT trust the browser absolute clock for stale-data calculations — use `performance.now()` for elapsed only.
- DO NOT bundle Recharts or Lightweight Charts into `/today` — they go on `/performance` and `/research` only via dynamic imports. CI bundle analyzer will block the PR.
- DO NOT blend `paper`, `live-small`, `live-scale` data in a single number or chart — never. The cross-environment segregation tests will fail.
- DO NOT inline secrets — env vars only. Pre-commit hook will reject (`gitleaks`).
- DO NOT skip the 30-paper-day gate by manually editing `paper_days_for_version` — the CI gate is mechanical for a reason.
- DO NOT write Python code that calls IBKR's TWS API directly in Phase 1 — Phase 1 has NO direct IBKR connection. Use the QC instruction protocol (write to ObjectStore + ack).
- DO NOT use SES — Resend is locked.
- DO NOT use bcrypt for backup codes — Argon2id is locked.
- DO NOT use the bare `<domain>` placeholder in code — substitute the actual operator-registered apex at deployment.

Aim for 20–30 anti-pattern entries.

### 12. PR-Drafting Templates

The exact templates Claude Code uses when drafting PRs. Each template is locked. Cover:

- Strategy logic change (forbidden whitelist; PR required)
- Parameter change within range (auto via agent; PR optional but for major moves)
- Hot-fix infrastructure (allowed whitelist; auto-deploy possible)
- Bug fix (general)
- New feature (Phase-aligned)

For each: the markdown template the PR body uses, the labels that get applied, the in-app review surface artifacts that get generated.

### 13. Operator Review Checklist

What the operator checks when reviewing in-app PR review surface. Each item is a yes/no verifiable from the surface artifacts:

- [ ] Plain-English summary present and ≤200 words?
- [ ] Risk impact summary auto-generated and shows specific numbers?
- [ ] Backtest delta runs against locked `slippage_calibration_version`?
- [ ] All tests pass (unit + integration + linting + type-check)?
- [ ] Files affected list matches plain-English summary?
- [ ] No files in forbidden whitelist modified without `risk-review-approved` label?
- [ ] If new SSE event type: enum updated in both Pydantic + TypeScript?
- [ ] If new audit event type: enum + Alembic migration?
- [ ] If new endpoint: matches the spec's API contract section?
- [ ] If migration: additive (no maintenance window required) or scheduled for window?

Aim for 12–18 items.

### 14. Living Document Protocol

How to maintain this dev guide:
- New canonical pattern surfaces: add to §5 with code snippet; update relevant anti-pattern list if applicable
- Pattern proves wrong (postmortem outcome): mark as deprecated; add new canonical pattern
- New tooling adopted: update §3 or §4 standards
- Anti-pattern surfaces from a real bug: add to §11 with reference to the postmortem
- Operator review checklist gains items: add to §13

Update process: PR with the dev guide changes; in-app review surface generated; operator approves.

## FORMAT REQUIREMENTS

- Markdown with clear section headers
- Concrete code snippets (Python and TypeScript) for every canonical pattern in §5 and §6 — actual runnable code, not pseudocode
- Imports always shown in code snippets
- Every code snippet under §5 should be self-contained enough that Claude Code can copy-and-adapt
- Length: 1500–2500 lines; favor density over completeness
- No placeholder content like `[FILL IN]`; either commit to a default or omit
- Reference specs and implementation guide by section number when relevant

## CONSTRAINTS

- Do NOT redefine architectural decisions already locked in the specs — reference them
- Do NOT introduce new coding conventions that contradict the specs (e.g., the spec says structlog → don't recommend logging)
- Do NOT propose new components or services not present in the specs
- Do NOT include placeholder content; if you'd write `[FILL IN]`, instead either commit to a default or omit
- The dev guide is read by AI agents; write in second person ("you") with imperative voice
- Code snippets must use the locked tooling (asyncpg + SQLAlchemy 2.x async, pydantic v2, structlog, FastAPI, Next.js 14+ App Router, TanStack Query, Zustand, shadcn/ui, Tailwind, etc.)

## DELIVERABLE

Write the complete dev guide to `/Users/shaanpatel/Documents/GitHub/Trading/claude-dev-guide.md`. After writing, return a single paragraph summary of: (a) total line count, (b) any sections that were thinner than expected and why, (c) any patterns referenced in specs but not pinned to a single canonical implementation.

Begin.
