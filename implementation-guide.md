# IMPLEMENTATION GUIDE
## Solo-Operator Algorithmic Trading System — Path C (QC → Custom Backend)
**Version:** 1.0 — initial build  
**Owner:** Operator (NJ)  
**Last updated:** 2026-05-05  
**Living document** — see §12 for update protocol

---

## TABLE OF CONTENTS

1. [Document Conventions](#1-document-conventions)
2. [Pre-Phase-0 Setup Checklist](#2-pre-phase-0-setup-checklist)
3. [Phase 0 Week-by-Week Plan (weeks 0–8)](#3-phase-0-week-by-week-plan)
4. [Phase 1 Milestone Plan (months 2–5)](#4-phase-1-milestone-plan)
5. [Phase 2 Milestone Plan (months 5–9)](#5-phase-2-milestone-plan)
6. [Phase 3 Milestone Plan (months 9–12)](#6-phase-3-milestone-plan)
7. [Component Dependency Graph](#7-component-dependency-graph)
8. [Decision-Point Register](#8-decision-point-register)
9. [Operational Runbook Excerpts](#9-operational-runbook-excerpts)
10. [Risk Register](#10-risk-register)
11. [Plan of Action — First 2 Weeks](#11-plan-of-action--first-2-weeks)
12. [Update Protocol](#12-update-protocol)

---

# 1. Document Conventions

## 1.1 Task Tags

Every task in this guide carries one of three ownership tags:

| Tag | Meaning |
|---|---|
| `[OPERATOR]` | Operator acts directly: clicks, signs up, reads, decides, verifies. No code required. |
| `[CLAUDE_CODE]` | Claude Code authors, tests, or deploys code. Operator reviews and approves the PR or command. |
| `[BOTH]` | Operator and Claude Code collaborate in the same session: operator describes intent, Claude Code authors, operator reviews output before proceeding. |

Operator's role throughout the build is **operational competence**, not code authorship. When a task says `[CLAUDE_CODE]`, the operator opens a Claude Code session, describes what is needed from this guide, reviews the output, and approves or redirects.

## 1.2 Verification Gate Format

Each Phase 0 week ends with a gate. Gates use this format:

```
**Verification gate (end of week N):**
- [ ] <observable criterion — exact command or UI check>
      Done means: <what a passing result looks like>
```

A gate item is satisfied only when the observable criterion produces the exact passing result. "I think it works" is not a gate pass. If any gate item is not satisfied, the week does not close and the risk described in "If blocked" applies.

## 1.3 Decision-Point Format

The Decision-Point Register (§8) uses this structure for each entry:

| Field | Content |
|---|---|
| **ID** | DP-NNN |
| **Trigger** | What condition or milestone causes this decision to arise |
| **Choice** | What the operator must decide (binary or N-way) |
| **Default** | The safe default if operator is uncertain |
| **Action** | What to do after deciding |
| **Audit entry required** | Yes / No; which `decision_diary` tag to use |

## 1.4 Spec References

This guide does not re-define locked architectural decisions. Where a rule originates in a spec, it is cited as:

- `backend-spec §N.N` — references `Docs/backend-spec.md`
- `frontend-spec §N.N` — references `Docs/frontend-spec.md`
- `claude-dev-guide §N.N` — references `Docs/claude-dev-guide.md`

When this guide's instruction conflicts with a spec, the spec wins. File a note in the Decision-Point Register with rationale.

## 1.5 Environment Tags

All live commands and configs reference one of three environments:

| Tag | Context |
|---|---|
| `[dev]` | Operator's laptop; mock everything; `secrets/dev.enc.yaml` |
| `[paper]` | Hetzner VPS; live services but QC paper trading; `secrets/paper.enc.yaml` |
| `[live]` | Hetzner VPS; QC live trading (Phase 1) or direct IBKR (Phase 2); `secrets/live.enc.yaml` |

Phase 0 runs entirely in `[dev]` and `[paper]`. Phase 1 transitions to `[live]`. Phase 2 retains `[live]` with added `[phase2]` Docker Compose profile.

---

# 2. Pre-Phase-0 Setup Checklist

Complete every item in this checklist before Week 1 day-tasks begin. The IBKR Pro application is the longest lead-time item: **start it on Day 1 of the entire engagement**.

## 2.1 Accounts to Open

| # | Provider | Why Needed | Plan / Tier | Estimated Cost | Expected Turnaround | Blocker? |
|---|---|---|---|---|---|---|
| 1 | **IBKR Pro** (interactivebrokers.com) | Futures brokerage; QC connects here in Phase 1; direct connection in Phase 2 | Individual account, Pro tier; enable futures + Level 2 options; defer market data subscriptions until TWS install | $0 account min (commissions only) | **1–2 weeks for approval + funding** | **YES — critical path; start Day 1** |
| 2 | **QuantConnect** (quantconnect.com) | LEAN Cloud execution for Phase 1; paper-day clock starts Week 1 | **Researcher** tier (~$60/mo as of 2026-05; spec's "$20" was outdated — see `Docs/decisions-log.md`); create fresh organization (do not reuse any existing org) | $60/mo | Same-day | YES — needed Week 1 |
| 3 | **Hetzner Cloud** (hetzner.com/cloud) | Primary VPS (Ashburn CCX13) + external watchdog VPS (EU; **Nuremberg** if Falkenstein unavailable) | CCX13 Ashburn + CX23 EU (CX11 retired; CX23 is current entry tier). Separate Hetzner projects required. | ~$25 + $5.59 = ~$30.59/mo | Same-day | YES — needed Week 1 |
| 4 | **Cloudflare** (cloudflare.com) or **Namecheap** (namecheap.com) | Domain registrar; apex domain needed for Caddy auto-cert + WebAuthn rpID | Free registrar account; domain registration ~$10–15/yr | $10–15/yr | Same-day | YES — needed Week 1 |
| 5 | **GitHub** (github.com) | Source control, CI/CD (GitHub Actions), GHCR image registry, in-app PR review surface | Free personal account; create GitHub App for in-app PR review surface | $0 | Same-day | YES — needed Week 1 |
| 6 | **Discord** (discord.com) | Operational alert delivery; slash command interface; liveness check | Free; create a **private server** (operator-only); do not use an existing personal server | $0 | Same-day | YES — needed Week 2 |
| 7 | **Resend** (resend.com) | Email backup for critical alerts and watchdog notifications | Free tier (100/day, 3k/mo) sufficient initially | $0 | Same-day | YES — needed Week 3 |
| 8 | **Sentry** (sentry.io) | Error tracking for backend services | Free tier (5k errors/mo) | $0 | Same-day | No — needed Week 5 |
| 9 | **S3 or Backblaze B2** | Encrypted backup storage (Postgres WAL + nightly dumps + GHCR image backup) | AWS S3 or Backblaze B2; Object Lock on S3 for immutability | ~$1–3/mo | Same-day | No — needed Week 4 |

## 2.2 Tooling on Operator's Laptop

Install all before Week 1 begins:

| Tool | Install Command / Source | Why |
|---|---|---|
| **Docker Desktop** | https://www.docker.com/products/docker-desktop | Run and test containers locally before VPS deploy |
| **Python 3.11+** | `brew install python@3.11` (macOS) or python.org | Run scripts, read code, run verify_chain.py |
| **git** | `brew install git` | Repo operations |
| **sops** | `brew install sops` | Decrypt secrets for local dev |
| **age** | `brew install age` | Key generation + age-encrypted secrets |
| **Claude Code** | https://claude.ai/code | Pair-programming interface |
| **IBKR TWS** | https://www.interactivebrokers.com/en/trading/tws.php | Paper-trade inspection during Phase 0; Phase 2 TWS API |
| **gh (GitHub CLI)** | `brew install gh` | Create PRs, manage GitHub App, watch CI |

Verify install:
```bash
docker --version && python3 --version && git --version && sops --version && age --version && gh --version
```
Expected: no errors; each prints a version string.

## 2.3 Cost Commitment Verification

Monthly fixed cost estimate at Phase 0 start:

| Item | Spec estimate | Actual (as of 2026-05-05) |
|---|---|---|
| Hetzner CCX13 (Ashburn primary) | ~$25 | $25 (matches) |
| Hetzner watchdog (EU, CX23 — CX11 retired) | ~$5 | $5.59 |
| QuantConnect (Researcher tier — was "$20 Quant Researcher" in spec) | $20 | **$60** (see `Docs/decisions-log.md`) |
| GitHub Pro (required for branch protection on private repo) | $0 | **$4** (see `Docs/decisions-log.md`) |
| Domain (amortized) | ~$1 | $1 |
| S3/Backblaze B2 | ~$2 | $2 (not yet provisioned) |
| Resend (free tier) | $0 | $0 |
| Sentry (free tier) | $0 | $0 |
| Anthropic API (agent; minimal Phase 0) | ~$5–20 | ~$5–20 |
| **Total Phase 0 estimate** | **$58–73/mo** | **$103–118/mo** |

At Phase 1 live start, Anthropic API usage increases. Monitor via `GET /api/system/costs`.

- **Soft alert ceiling:** $200/mo → agent auto-generates cost-review briefing
- **Hard alert ceiling:** $300/mo → trading continues; operator must manually review + reduce
- **Budget reserve:** $30–35k total pool minus $15–25k trading capital = $5–10k reserve for infra over 12 months

## 2.4 Critical Path

```
Day 1: IBKR Pro application submitted (1–2 week wait begins)
Day 1: Domain registered + DNS configured to Hetzner VPS IP (same-day)
Day 1: Hetzner VPS provisioned (same-day; capture IPv4)
Day 1: QuantConnect org created (same-day)
Day 1: GitHub repo created + CI scaffolding (same-day)

Week 2: IBKR Pro approval expected — if not received by end of Week 2, execute contingency (see DP-001)
Week 8: All 30 CME paper sessions required before live start
```

Everything except IBKR approval can be completed in Week 1. IBKR approval gates Phase 1 live trading, not Phase 0 paper development.

---

# 3. Phase 0 Week-by-Week Plan

Phase 0 = weeks 0–8. Week 0 is the setup week (§2 checklist). Weeks 1–8 are active build weeks.

Operator learning allocation: 5–8 hours/week on Python basics, git operations, Docker concepts, log reading. This is tracked informally — the goal is that by Week 8, the operator can read any service's `docker compose logs <service>` output and identify the failure mode without Claude Code's help.

---

## Week 1 — Infra Foundation + Paper Clock Starts

**Goals:**
- Apex domain registered and DNS pointing to Hetzner VPS
- Hetzner Ashburn VPS and Falkenstein watchdog provisioned
- QuantConnect fresh organization created; paper trading begins (30-session clock starts)
- GitHub repo scaffolded with CI; v1 strategy code authoring begins

**Critical path:** QC paper trading starts this week; every CME session counts toward the 30-session requirement.

**Daily tasks:**

- **Mon:** `[OPERATOR]` Submit IBKR Pro application. Register apex domain (Cloudflare or Namecheap). Provision Hetzner Ashburn CCX13 + Falkenstein CX11 — capture both public IPv4s. Create GitHub repo `trading-system` (private). `[CLAUDE_CODE]` Scaffold repo layout per backend-spec §1.1: directory tree, `pyproject.toml`, `docker-compose.yml` skeleton, GitHub Actions CI workflow (test + lint + type-check + gitleaks + docker build).
- **Tue:** `[OPERATOR]` Create QuantConnect fresh organization; note organization ID. Set up DNS A record for apex domain pointing to Hetzner Ashburn IPv4. `[CLAUDE_CODE]` Add branch protection to `main` (CI pass required; no direct push). Create GitHub App for in-app PR review surface; document App ID in repo.
- **Wed:** `[CLAUDE_CODE]` Author v1 strategy skeleton in `strategies/v1_trend_following/` — Donchian/MA trend-following on Phase 1 sub-universe (micro futures + bond ETFs); include sizing trace scaffold and audit event hooks. `[OPERATOR]` Review strategy logic description (not code); ask Claude Code to explain signal logic in plain English before approving the PR.
- **Thu:** `[CLAUDE_CODE]` Add v1 strategy to QuantConnect paper project; configure QC to use IBKR Pro paper credentials (use QC's paper broker until IBKR live account approved). `[OPERATOR]` Launch paper trading on QC. Confirm first paper session is live.
- **Fri:** `[BOTH]` Operator reads first paper session logs in QC dashboard; Claude Code explains log structure. `[CLAUDE_CODE]` Set up Hetzner VPS: install Docker, create `trading` user, clone repo to `/opt/trading`, configure UFW (ports 22 SSH key-only, 80, 443; block all else).

**Verification gate (end of Week 1):**
- [x] `curl -I https://<your-domain>` returns HTTP 200 or redirect (Caddy placeholder or Let's Encrypt)
      Done means: TLS cert issued; apex domain resolves to Hetzner Ashburn IPv4
      **Status 2026-05-08:** DONE — `curl -fsS -i https://spratcapital.com/api/health` from operator's laptop returns HTTP/2 200 + HSTS preload + CSP + `db_connected:true` JSON body on first cold-cache request. Caddy ACME cert acquired; HTTP/3 advertised via `alt-svc`. See `Docs/decisions-log.md` "Day 6 carryover morning — TLS verified end-to-end" entry.
- [x] QC organization exists; paper trading algorithm is **Running** status in QC dashboard
      Done means: at least 1 QC paper session tick visible in algorithm logs
      **Status 2026-05-07:** DONE — `v1_trend_following_paper` Running on QC Paper Brokerage since 2026-05-07 07:00 UTC (Day 4 close-out).
- [x] `gh repo view trading-system` shows repo with branch protection on `main`
      Done means: CI workflow file present; direct push to `main` blocked
      **Status 2026-05-05:** DONE — branch protection applied Day 1; required-status-checks expanded Day 3 close-out (5 checks gate merge: lint, gitleaks, typecheck, test, forbidden-paths).

**Risks this week:** QC algorithm fails to start (misconfigured LEAN parameters); Hetzner provisioning fails or wrong datacenter selected.

**If blocked:** QC algorithm won't start → Claude Code debugs LEAN configuration locally; do not defer paper clock start. Hetzner wrong datacenter → deprovision and re-provision; this is a 10-minute fix.

---

## Week 2 — Sub-Universe Verification + Sops Setup

**Goals:**
- Phase 1 sub-universe verified: all target markets meet 50%-single-contract-notional rule at $15–25k starting equity
- v1 strategy committed and trading on QC paper
- Sops + age secrets management initialized; paper key backup in fireproof safe

**Critical path:** universe verification gates live trading size assumptions; if a market fails at $15k, it must be excluded from the initial live universe.

**Daily tasks:**

- **Mon:** `[CLAUDE_CODE]` Author `services/risk/sizing.py` Stage 0 universe filter (backend-spec §2.4.1). Write unit tests: Stage 0 at $15k/$25k/$50k/$100k tiers; /MES 50%-override at $20k. `[OPERATOR]` Verify IBKR Pro application status (check email).
- **Tue:** `[BOTH]` Run sub-universe verification against QC bundled data for each Phase 1 contract. Confirm: ≥4 markets active at $15k; record exclusions with rationale in decision diary (see DP-002).
- **Wed:** `[OPERATOR]` Generate age key pair: `age-keygen -o key.txt`. Back up key.txt to fireproof safe (physical printout, acid-free paper). Delete key.txt from laptop; store age private key only at `~/.config/sops/age/keys.txt`. `[CLAUDE_CODE]` Initialize sops: create `.sops.yaml` with three age recipients (dev/paper/live). Create `secrets/paper.enc.yaml` with QC API token + DB password placeholder.
- **Thu:** `[CLAUDE_CODE]` Author decision diary writer service (`services/audit/decision_diary.py`); vacation mode handler (`services/scheduler/vacation.py`). Wire audit event types for `decision_diary_logged` and `vacation_mode_toggled` per backend-spec §3.30 enum.
- **Fri:** `[BOTH]` Operator SSH into Hetzner VPS; walk through `docker compose up` manually to confirm containers start. Claude Code debugs any startup failures. Confirm QC paper trading is still live and ticking.

**Verification gate (end of Week 2):**
- [x] Sub-universe check: run `python3 scripts/verify_universe.py --equity 15000` (Claude Code authors this script). Output lists all Phase 1 contracts; each shows PASS/FAIL with 1-contract notional vs. 50% threshold.
      Done means: ≥4 markets show PASS at $15,000 equity
      **Status (2026-05-08, Day 7):** ≥4 markets PASS — but only at $20k after **DP-002 invoked** raising initial capital $15k → $20k (insufficient PASS coverage at $15k). Decisions-log: 2026-05-08 Day 7 09:00 sub-universe + DP-002 entry.
- [x] Sops decrypt test: `sops -d secrets/paper.enc.yaml` on laptop returns plaintext YAML without error
      Done means: age key works; sops config correct
      **Status (2026-05-08):** verified after laptop sops `exec format error` fix (Day 6 carryover evening). `paper.enc.yaml` filled from VPS and committed via PR #29.
- [ ] IBKR Pro application email confirmation received (or follow-up submitted if no response)
      Done means: application in review; 2-week clock tracked
      **Status (2026-05-08):** still pending. **DP-001 trigger window opens at end of Week 2** — if no approval by then, execute DP-001 (alternate broker / extended timeline).

**Risks this week:** Age key backup missed or stored only digitally — if laptop lost, secrets become unrecoverable. IBKR approval further delayed.

**If blocked:** Sub-universe verification fails for all markets at $15k → consult DP-002 for alternate initial capital tier. IBKR not approved by end of Week 2 → execute DP-001.

---

## Week 3 — QC Adapter Scaffold + Backend Skeleton

**Goals:**
- QC ObjectStore audit adapter scaffolded (writes events with monotonic `sequence_no` + JCS canonical)
- Backend skeleton running: FastAPI, Postgres 16, Alembic migrations for `audit_log` table with hash chain

**Critical path:** audit schema migration must run before any golden test in Week 4.

**Daily tasks:**

- **Mon:** `[CLAUDE_CODE]` Author Postgres 16 schema: `audit_log` table with hash-chain columns (`record_hash`, `prev_hash`, `sequence_no`, `jcs_canonical_body`), `BEFORE UPDATE/DELETE` trigger blocking mutations, `EVENT TRIGGER` for TRUNCATE (backend-spec §2.10.2). Create Alembic migration `0001_audit_log.py`. Write unit test: `services/audit/writer.py` — hash chain on insert, SERIALIZABLE retry, advisory lock.
- **Tue:** `[CLAUDE_CODE]` Scaffold QC adapter service (`services/qc_adapter/`): poll `/events` every 60s, poll `/acks` every 5s; write ingested events to `audit_log` with `ingest_clock_ts`, `ingest_uuid`, `sequence_no`. Wire `qc_adapter_cursor` table to track last-processed event ID (backend-spec §3.19).
- **Wed:** `[CLAUDE_CODE]` Author reconciliation service skeleton (`services/reconciliation/`): position qty tolerance (0), cash tolerance ($5 / 1bps abs), T+1 grace, dividend ex-date 2× widening (backend-spec §2.6). Wire `reconciliation_breaks` table (backend-spec §3.15).
- **Thu:** `[CLAUDE_CODE]` Author alerts pipeline: `services/webhook_pusher/` Discord webhook + Resend email; channel routing by severity (P0/P1/P2 → `#alerts`, P0 → `#critical`). Wire `alerts` table (backend-spec §3.27). `[OPERATOR]` Create Discord server with 7 channels: `#daily-brief`, `#signals`, `#fills`, `#alerts`, `#critical`, `#ops`, `#audit`. Get each channel's webhook URL.
- **Fri:** `[CLAUDE_CODE]` Stand up FastAPI app (`services/api/`): health endpoint `GET /api/health`, SSE channel `/api/sse/events`, auth scaffolding. Caddy reverse-proxy config pointing to FastAPI (backend-spec §9.2.1). Deploy to `[paper]` VPS; run `docker compose up -d`.

**Verification gate (end of Week 3):**
- [x] `curl https://<your-domain>/api/health` returns `{"status":"ok","environment":"paper"}` with HTTP 200
      Done means: Caddy TLS terminating; FastAPI running; health endpoint live
      **Status (2026-05-08, Day 6 carryover):** DONE — same TLS curl that closes the Week 1 gate. Body returns `{"status":"ok","environment":"paper","version":"dev","db_connected":true,...}`. See `Docs/decisions-log.md` "Day 6 carryover morning — TLS verified end-to-end" entry.
- [x] On VPS: `docker compose logs audit | grep "audit_log migration"` shows migration applied
      Done means: Alembic ran `0001_audit_log.py`; table exists in Postgres
      **Status (2026-05-07, Day 5):** DONE — alembic 0001-0006 applied during Ashburn bringup; the api service container runs `alembic upgrade head` at startup. Verified via `tests/integration/test_audit_immutability.py` 6/6 pass against the same migrated schema. (Gate phrasing references the never-shipped standalone `audit` service container; the migrations are actually run by the api container per dev-guide §6.3 — same end state.)
- [x] `docker compose logs postgres | grep "TRIGGER"` shows trigger creation (or run `psql -c "\d+ audit_log"` and confirm BEFORE UPDATE trigger present)
      Done means: immutability trigger installed
      **Status (2026-05-05, Day 3):** DONE — alembic `0005_immutability.py` creates BEFORE UPDATE/DELETE row triggers + BEFORE TRUNCATE statement triggers (parent + each yearly partition). `tests/integration/test_audit_immutability.py` exercises all four cases against postgres:16 and passes.
- [ ] Discord: send a test message via webhook URL → message appears in `#alerts`
      Done means: webhook delivery working
      **Status (2026-05-11, Day 10):** `services/webhook_pusher/` shipped via PR #44 (Day 10 / Week 3 Thu) with planner + sender + dispatcher + CLI + 8-step operator runbook (`deploy/webhook_pusher/README.md`); 58 unit tests green (404/404 total). Discord guild + 7 webhook URLs created Day 2; Ashburn → Discord round-trip proven Day 6 carryover (HTTP 204; `Cloudflare-blocks-Hetzner-Discord` only affects Nuremberg watchdog, NOT Ashburn). **Gate closure binds on operator completing `deploy/webhook_pusher/README.md` Steps 3 + 5 + 6 on Ashburn** (Step 3 = bare-smoke P2 → `#alerts`; Step 5 = full P0 roundtrip → `#alerts` + `#critical` + Resend email; Step 6 = psql confirms `delivery_status` JSONB landed). Code ready; awaits operator runbook execution. See `Docs/decisions-log.md` "Day 10 09:00 — services/webhook_pusher/ alerts pipeline (PR #44)" entry.

**Bonus shipped this Week (not on the Week 3 verification gate):**
- `services/audit/writer.py` (PR #39, Day 8) — canonical hash-chain writer with `pg_advisory_xact_lock` + SERIALIZABLE + 5-attempt SQLSTATE-40001 retry; 22 unit tests for chain primitives + 4 testcontainers integration tests for the writer. Anti-pattern A01 ("DO NOT write to audit_log directly via INSERT") is enforceable from this PR forward. See `Docs/decisions-log.md` "Day 8 09:00 — services/audit/writer.py canonical hash-chain writer" entry.
- `alembic/versions/2026-05-09_qc_adapter_cursor_seed.py` (PR #40, Day 8) — first **operational** dated migration under dev-guide §7.1 hybrid scheme. Defensive idempotent re-seed (no-op against current schema since 0004 already inserts the rows). See `Docs/decisions-log.md` "Day 8 10:00 — alembic operational migration" entry.
- `services/reconciliation/recon.py` (PR #42, Day 9) — pure-policy `plan_reconciliation_check` per backend-spec §2.6 + §3.15. Returns `ReconciliationPlan` dataclass; caller (Week 4 dispatcher) owns DB I/O + audit writes + kill-switch invocation. Locked tolerances (position qty = 0, cash = max($5 abs, 1 bps × equity_baseline), dividend ex-date 2× widening); T+1 grace via prior_breaks input. 45 unit tests across 8 `Test*` classes; A22 enforced (zero audit writes from tests). Same plan-then-apply shape as PR #28 / PR #37. See `Docs/decisions-log.md` "Day 9 09:00 — services/reconciliation/recon.py pure-policy skeleton" entry.

**Risks this week:** Alembic migration fails due to Postgres role permissions → Claude Code debugs; check `app_owner` role grant. Discord webhook URL expires or is wrong → regenerate.

**If blocked:** Postgres trigger syntax error → Claude Code fixes; re-run migration. FastAPI won't start → check `docker compose logs api` for startup exception.

---

## Week 4 — Audit Golden Tests + Immutability Enforcement

**Goals:**
- QC adapter golden test parity verified: byte-for-byte identical records modulo `{ingest_clock_ts, ingest_uuid, sequence_no}`
- Backend audit-log immutability enforced and tested under concurrent writes
- Concurrency tested: advisory lock + SERIALIZABLE retry

**Critical path:** golden test parity gates Week 7's end-to-end round-trip verification.

**Daily tasks:**

- **Mon:** `[CLAUDE_CODE]` Author `tests/golden/` suite: 5 representative QC session events (signal_emitted, fill_received, position_reconciled, kill_switch_triggered, session_end). For each: capture raw QC ObjectStore JSON; apply JCS canonicalization; compute expected `record_hash`; run through adapter; assert output matches modulo the three mutable fields.
- **Tue:** `[CLAUDE_CODE]` Author `services/audit/verify_chain.py`: reads `audit_log` table; recomputes each `record_hash` from stored canonical body; checks each `prev_hash` links to prior row. Returns: PASS (clean chain) or list of broken sequence numbers.
- **Wed:** `[CLAUDE_CODE]` Inject fault scenarios: attempt direct UPDATE on `audit_log` row → confirm trigger blocks with error `P0001`; attempt TRUNCATE → confirm `EVENT TRIGGER` blocks. Write integration test asserting both are blocked even by `app_owner` role.
- **Thu:** `[CLAUDE_CODE]` Concurrency test: 10 concurrent writes to `audit_log` with SERIALIZABLE isolation; confirm no deadlock; advisory lock contention handled with retry backoff. Record P95 write latency.
- **Fri:** `[BOTH]` Run full golden test suite: `pytest tests/golden/ -v`. Review any failures with Claude Code. `[OPERATOR]` Read the test output; confirm understanding of what each golden test asserts.

**Verification gate (end of Week 4):**
- [x] `pytest tests/golden/ -v` passes all 5 golden test cases
      Done means: QC adapter produces byte-for-byte identical `record_hash` for each representative session event
      **Status (2026-05-12, Day 11):** DONE — `tests/golden/` shipped via PR #45 (Day 11 / Week 4 Mon) with 18 tests across 5 `Test*` classes covering byte-for-byte `record_hash` for the 5 representative QC session events (`signal_emitted`, `order_filled`, `reconciliation_check_passed`, `kill_switch_triggered`, `system_stopped` — locked taxonomy from `services/audit/event_types.py`, NOT IG's casual prose), sequential chain composition with baked-in tail hex, parser-is-payload-identity round-trip via `services.qc_adapter.payloads.parse_jsonl_record`, "modulo three mutable fields" recursive walk for `{ingest_clock_ts, ingest_uuid, sequence_no}` audit-side metadata, and fixture sanity asserts. A22 enforced: pure Python, zero `audit_log` INSERTs, zero testcontainers, zero mocking. `make test-golden` runs in 0.61s; full `make test` runs 412/412 green. See `Docs/decisions-log.md` "Day 11 09:00 — tests/golden/ QC adapter parity suite (PR #45)" entry.
- [ ] `python3 services/audit/verify_chain.py --env paper` returns `CHAIN OK: N rows verified`
      Done means: audit chain is intact; no broken prev_hash links
- [ ] `docker compose logs audit` shows `advisory_lock_acquired` and `SERIALIZABLE_retry` log lines from concurrency test
      Done means: advisory lock + retry loop functional under concurrent writes

**Risks this week:** JCS canonicalization produces different byte output than QC's serialization → debug JSON key ordering; Python `json-canonical` library must match QC's C# JCS output exactly.

**If blocked:** Golden test fails repeatedly after canonicalization fixes → consult backend-spec §2.10.1 for exact write-path algorithm. Escalation: if QC's ObjectStore serialization is non-deterministic, log a DP in §8 and assess Phase 1 architecture re-evaluation (backend-spec §11.1 kill criteria).

---

## Week 5 — Backend Phase 0 Surface + Caddy

**Goals:**
- REST scaffolding for all Phase 1 endpoints live (returning mock data or DB data where available)
- SSE channel `/api/sse/events` operational
- Caddy fully configured including rate limiting and watchdog IP allowlist

**Daily tasks:**

- **Mon:** `[CLAUDE_CODE]` Author all Phase 1 REST endpoint stubs per backend-spec §4.1: auth (`/api/auth/me`, `/api/setup/verify-token`), signals (`/api/signals`, `/api/signals/:id/approve`, etc.), system/risk (`/api/system/kill-switch`, `/api/system/kill-switch/invoke`), positions, orders, fills, alerts, health-score, today digest. Each returns correct schema shape with mock or DB data.
- **Tue:** `[CLAUDE_CODE]` SSE multiplexer (`/api/sse/events`): canonical envelope per backend-spec §4.2.1; event types: `signal`, `fill`, `position`, `pnl`, `alert`, `health`, `risk_state`. Implement 30s heartbeat (`event: ping`). Implement multi-tab eviction (per frontend-spec §4.6: last-connected tab wins; prior tab receives `evict` event).
- **Wed:** `[CLAUDE_CODE]` Caddy configuration: HTTPS termination, rate limiting (100 req/10s per IP on API; 5 req/10s on auth), watchdog IP allowlist for `/api/health` (only Falkenstein static IP + operator home IP), HSTS, CSP headers. Configure external watchdog cron on Falkenstein: ping `GET /api/health` every 5 min; send Resend email alert if 3 consecutive failures.
- **Thu:** `[CLAUDE_CODE]` Slippage calibration bootstrap: `services/calibration/` — zero-slippage prior (α=0, β=0); OLS fit structure per backend-spec §2.14; `slippage_calibration_versions` table migration (backend-spec §3.12).
- **Fri:** `[OPERATOR]` Test SSE from browser: open `https://<your-domain>/api/sse/events` with session cookie; confirm heartbeat `ping` events arrive every 30s. Test rate limit: run `for i in $(seq 1 110); do curl -s https://<your-domain>/api/health; done` — should see 429 after 100 requests.

**Verification gate (end of Week 5):**
- [ ] `curl https://<your-domain>/api/sse/events` (with auth cookie) streams `event: ping` lines every 30s
      Done means: SSE channel live; heartbeat operational
- [ ] `curl https://<your-domain>/api/today/digest` returns valid JSON matching `TodayDigestSchema` shape (mock data acceptable)
      Done means: REST scaffold complete for primary landing-page endpoint
- [ ] External watchdog test: block port 443 on VPS for 20 min via UFW (`ufw deny 443`), wait 15 min, check Resend email for watchdog alert. Immediately re-enable: `ufw allow 443`
      Done means: watchdog correctly detects outage and emails operator within 3 alert cycles (15 min)

**Risks this week:** Caddy auto-cert fails if DNS not fully propagated → wait 24h and retry; check `docker compose logs caddy` for ACME errors. Watchdog alert email delivered to spam → whitelist Resend sender in operator email.

**If blocked:** ACME cert fails → check that port 80 is open (Caddy HTTP-01 challenge requires it). SSE connection drops immediately → check Caddy `proxy_timeout` setting; default may be too short for long-lived SSE.

---

## Week 6 — Frontend Phase 0 Scaffolding

**Goals:**
- Next.js app scaffolded; WebAuthn registration flow on `/setup` end-to-end working
- Today page renders correctly against mock data
- Discord bot skeleton with `/positions` and `/halt` commands operational

**Daily tasks:**

- **Mon:** `[CLAUDE_CODE]` Scaffold Next.js frontend in `frontend/`: TypeScript, Tailwind CSS, shadcn/ui, TanStack Query, Recharts. Configure build to output static or SSR. Wire `NEXT_PUBLIC_API_BASE` env var. Create routing: `/`, `/trades`, `/trades/:id`, `/performance`, `/research` (404), `/system`, `/calendar`, `/login`, `/setup`, `/recover`.
- **Tue:** `[CLAUDE_CODE]` Auth flow: `/setup` page — WebAuthn registration ceremony using browser's `navigator.credentials.create()`; backend `/api/auth/webauthn/register-begin` + `/register-complete` (backend-spec §8.5.1). TOTP backup setup as second factor. 8 single-use backup codes generated and displayed once (frontend-spec §5.3). `/login` page — WebAuthn assertion or TOTP path.
- **Wed:** `[CLAUDE_CODE]` Today page (`/`): 12-col grid layout per frontend-spec §2.2.1; Health Score tile (gray "—" for insufficient-data state); P&L Summary 4-column; Queued Signals table with Approve/Reject/Defer buttons; Exposure breakdown bars; Positions table; Recent Fills feed; Active Alerts. Wire to mock data initially; SSE hook in place.
- **Thu:** `[CLAUDE_CODE]` Discord bot skeleton: `services/discord_bot/` using `discord.py`; `/positions` command (calls `GET /api/positions/current`; formats embed; Phase 0 supported per frontend-spec §6.1); `/halt <reason>` command (calls `POST /api/system/kill-switch/invoke`; confirm embed before executing). `/status` command for health check.
- **Fri:** `[OPERATOR]` Complete `/setup` flow in browser: navigate to `https://<your-domain>/setup`; enter setup token (Claude Code generates one-time token); register WebAuthn credential (Touch ID or hardware key); set up TOTP backup in authenticator app (1Password recommended); download backup codes and store in fireproof safe alongside age key. `[BOTH]` Test `/halt test-reason` in Discord — confirm kill-switch state transitions to HALT_NEW and back.

**Verification gate (end of Week 6):**
- [ ] Navigate to `https://<your-domain>/setup` → complete WebAuthn registration → navigate to `/login` → authenticate with WebAuthn without password → land on Today page
      Done means: WebAuthn enrollment + assertion round-trip working
- [ ] Today page loads with at least Health Score "—", empty Positions table, empty Fills feed, and no JS console errors
      Done means: frontend renders correctly against mock data with no runtime errors
- [ ] In Discord: type `/positions` → bot responds with positions embed (empty or mock data)
      Done means: Discord bot connected; slash command registered; backend API reachable from bot

**Risks this week:** WebAuthn registration fails in browser — wrong `rpId` (must match apex domain exactly); check `secrets/paper.enc.yaml` `webauthn.rp_id` value. Discord bot slash commands not appearing — need to re-invite bot with `applications.commands` scope.

**If blocked:** WebAuthn fails → fall back to TOTP-only session (frontend-spec §5.4 reduced-privileges mode) for immediate Week 6 gate; schedule WebAuthn fix for Week 7. Discord commands missing → `gh issue create` to track; fix before Week 8 gate.

---

## Week 7 — Frontend Live Integration + E2E Round-Trip

**Goals:**
- Frontend integrates with backend live data (off mock)
- End-to-end signal-to-fill round trip tested in QC paper
- Phase 1 surfaces complete per frontend-spec §2.1 surface enumeration

**Daily tasks:**

- **Mon:** `[CLAUDE_CODE]` Replace mock data with real API calls: TanStack Query hooks for all Today page sections; SSE connection in `useSSE` hook; reconnection strategy per frontend-spec §4.5 (initial retry 1s, backoff to 30s, max 1min). Wire Queued Signals buttons to live backend endpoints.
- **Tue:** `[CLAUDE_CODE]` Trades page (`/trades`): filterable summary table with date/market filters; CSV export button; minimal per-trade detail page at `/trades/:id` (signal, market, direction, status, fill_price, fill_qty, P&L — ensures Discord deep-links don't 404).
- **Wed:** `[CLAUDE_CODE]` Performance page (`/performance`): equity curve (Recharts; no benchmark yet); monthly returns table; CSV export. System page (`/system`): kill-switch UI + state display; read-only Risk Envelope tile; audit log basic table with date/event-type/environment filter; reconciliation status; watchdog status; minimal Account section (regenerate backup codes).
- **Thu:** `[BOTH]` Trigger a paper signal cycle: on QC, confirm signal emitted at 17:30 ET; watch `#signals` Discord channel for embed; click Approve button; watch `#fills` for fill embed; navigate to web Today page and confirm position appears in Positions table; navigate to `/trades` and confirm trade appears.
- **Fri:** `[CLAUDE_CODE]` Calendar page (`/calendar`): read-only event list (next 30 days); Forex Factory + Trading Economics macro events. Wire Discord `/calendar` command (Phase 1). `[OPERATOR]` Operator uses `/ratify` command in Discord to ratify tomorrow's calendar; confirm ratification recorded in audit log via web `/system` audit table.

**Verification gate (end of Week 7):**
- [ ] End-to-end paper round-trip: signal emitted on QC → appears in Discord `#signals` with Approve button → operator approves → fill posted to Discord `#fills` → fill appears on web Today page Recent Fills section → trade appears in `/trades`
      Done means: full signal-to-fill pipeline working; QC adapter ingesting; SSE pushing to frontend
- [ ] `python3 services/audit/verify_chain.py --env paper` still returns `CHAIN OK` after round-trip
      Done means: audit chain maintained through live signal/fill cycle
- [ ] Web `/system` kill-switch: click INVOKE HALT → state shows HALT_NEW in system page → Discord `/positions` returns `[HALTED]` badge → web resume button (re-auth required) → state returns to NORMAL
      Done means: kill-switch UI round-trip working; re-auth gate functional

**Risks this week:** QC paper signal doesn't fire at 17:30 ET — check QC algorithm market data subscription; may need next trading day. SSE connection drops during round-trip — check Caddy proxy buffer settings.

**If blocked:** QC adapter not ingesting fills → check `docker compose logs qc_adapter`; confirm QC ObjectStore credentials in `secrets/paper.enc.yaml`. SSE drops → add `proxy_buffering off` to Caddy config.

---

## Week 8 — Buffer + Phase 1 Handover

**Goals:**
- 30 CME paper sessions verified complete
- Operator passes operational competence assessment
- All Phase 1 surfaces complete and verified
- Pre-flight checklist for live cutover completed

**Critical path:** 30-session minimum is non-negotiable. If count is below 30 at start of Week 8, this week extends until it is met before live trading begins.

**Daily tasks:**

- **Mon:** `[OPERATOR]` Count QC paper sessions since Week 1. Target: ≥30 CME-counted sessions (check QC algorithm trade log for sessions with ≥1 market open). If below 30, Week 8 extends until met. `[CLAUDE_CODE]` Run full pre-flight checklist (see below).
- **Tue:** `[OPERATOR]` Operational competence assessment (self-administered):
  - Deploy a change: merge a trivial PR → watch CI → confirm VPS pulls new image → `docker compose logs api` shows new startup log
  - Read a log failure: Claude Code injects a synthetic error into `docker compose logs signal`; operator identifies the error type and which service without help
  - Invoke kill switch from Discord: `/halt assessment-test`; confirm HALT_NEW; resume from web with re-auth
  - Check watchdog status: navigate to `/system`; confirm watchdog last ping timestamp < 10 min
- **Wed:** `[BOTH]` Fund IBKR Pro account with $15–25k (operator decision per DP-003 on exact amount). Confirm IBKR Pro live account approved and active. If IBKR not yet approved, Phase 1 live deferred — Phase 0 extends.
- **Thu:** `[CLAUDE_CODE]` Switch QC organization from paper broker to IBKR Pro live credentials inside QC secure vault (operator provides credentials directly to QC; backend never receives them). Set environment tag to `live-small`. Final `verify_chain.py` run.
- **Fri:** `[OPERATOR]` Final sign-off: review Decision-Point Register (§8) for any open items. Update this guide's Week 8 gate with completion date. Confirm Phase 1 "live trading begins" milestone date.

**Pre-flight checklist (run before live start):**

- [ ] `pytest tests/unit/ tests/integration/ tests/golden/ -v` — all pass
- [ ] `python3 services/audit/verify_chain.py --env paper` — CHAIN OK
- [ ] `curl https://<your-domain>/api/health` — `{"status":"ok","environment":"paper"}`
- [ ] All 30 CME paper sessions confirmed in QC log
- [ ] vectorbt-vs-LEAN parity: trade count within 5%, P&L divergence ≤ 0.5% (backend-spec §11.1 success criteria)
- [ ] Sops: `sops -d secrets/live.enc.yaml` — decrypts without error on VPS
- [ ] Discord all 7 channels operational; `/positions`, `/halt`, `/status` commands responding
- [ ] WebAuthn + TOTP backup registered; 8 backup codes in safe
- [ ] IBKR Pro account approved, funded, and connected to QC live project
- [ ] Age key backup in fireproof safe (physical print); `dba_breakglass` password printed separately

**Verification gate (end of Week 8):**
- [ ] `grep "paper_session_complete" <(docker compose logs qc_adapter)` shows ≥30 unique session timestamps
      Done means: 30 CME paper sessions logged; clock requirement met
- [ ] Operator completes operational competence assessment without Claude Code assistance for log-reading and kill-switch tasks
      Done means: operator can operate the system without code knowledge
- [ ] `curl https://<your-domain>/api/today/digest` with valid live-environment session cookie returns HTTP 200 with `"environment":"live-small"`
      Done means: live environment active; Phase 1 begins

**Risks this week:** 30-session count not met → Week 8 extends; live deferred per backend-spec §11.1 kill criteria. IBKR not approved → execute DP-001 alternate broker path.

**If blocked:** Paper session count insufficient → do not start live trading; extend Phase 0. IBKR approval blocked past 3 weeks → activate DP-001.

---

# 4. Phase 1 Milestone Plan

**Phase 1:** months 2–5. Live trading begins on QC; `live-small` environment tag; custom backend receiving and auditing all events from QC.

## Month 2 — Live Trading Begins

**Key milestones:**
- First live signal emitted, approved, and executed via QC on real IBKR Pro account
- First live fill ingested to audit log; chain intact
- First reconciliation pass: positions in backend match IBKR FlexQuery
- Daily liveness probe started: operator clicks `[I'm here]` button in `#daily-brief` within engagement window (per backend-spec §2.7)

**Weekly cadence:**
- Every Mon 08:00 ET: weekly summary from agent in `#daily-brief`
- Every day 17:00 ET: daily brief in `#daily-brief`; operator clicks liveness button within 24h
- Every day after session: reconciliation runs automatically; any break → P0 alert to `#alerts`

**Decision required:** DP-003 (initial live capital amount: $15k, $20k, or $25k).

**Watch for:** first reconciliation break on dividend ex-dates (Reconciliation Runbook §9.3). Do not panic — widen tolerance as documented.

**Month 2 end gate:**
- [ ] ≥1 live fill confirmed in IBKR account statement AND in backend `fills` table with matching price/qty
- [ ] Reconciliation break count = 0 OR all breaks acknowledged and resolved in audit log
- [ ] Operator has clicked `[I'm here]` on ≥80% of daily-brief posts

## Month 3 — First Calibration + PR Review Surface

**Key milestones:**
- First slippage recalibration: 30 live fills available → OLS fit runs; new `slippage_calibration_versions` row; audit logged
- First weekly vectorbt-vs-LEAN parity check (backend-spec §11.1 success criteria: trade count within 5%, P&L divergence ≤ 0.5%)
- First PR drafted by Claude Ops agent: parameter tightening within mutable range → operator reviews via in-app GitHub PR surface → approve or redirect
- Performance page and Audit Explorer polished per frontend-spec §2.1 Phase 1 additions

**Decision required:** DP-004 (first parameter PR review: approve/reject/modify).

**Month 3 end gate:**
- [ ] `GET /api/calibration/versions` returns ≥1 version with `fit_method=ols` (not zero-slippage prior)
- [ ] Parity check script shows trade count within 5% and P&L divergence ≤ 0.5%
- [ ] ≥1 agent-drafted PR visible in GitHub and in-app review surface

## Month 4 — Mid-Phase-1 Review

**Key milestones:**
- First decommission-floor monitoring review: check live 30-day Sharpe (must be > 0) and live max DD (must be > -25%)
- Health score composite available (sufficient data by week 8 of live trading)
- First quarterly cost report from agent: Anthropic API + infrastructure costs vs. $200/mo soft ceiling
- Calendar ratification established as daily habit

**Decision required:** DP-005 (continue / pivot / pause based on Sharpe trajectory and drawdown profile).

**Month 4 end gate:**
- [ ] `GET /api/health-score` returns composite ≥ 50 (or "—" with explanation if still insufficient data)
- [ ] Decommission floor check: `GET /api/performance/summary?window=30d` shows Sharpe > 0 AND max_dd > -25%
- [ ] Monthly cost report received in `#ops` channel; total ≤ $200

## Month 5 — Phase 2 Prep Begins

**Key milestones:**
- Pre-cutover automated checklist scheduled for `D-1` 17:00 ET (backend-spec §1.5)
- LEAN Local installation on VPS: `docker compose --profile phase2 pull lean_local`
- ib-async library integrated; IB Gateway container added with `phase2` profile
- Cutover date selected ≥5 CME sessions in advance (DP-006)
- Phase 2 infrastructure paper-validated before cutover: signal-to-fill round-trip via ib-async (paper IB account) ≤ 5s

**Pre-cutover automated checklist items (runs at D-1 17:00 ET; backend-spec §1.5):**
1. LEAN parity check: `lean_local` vs `vectorbt` trade count within 5%, P&L divergence ≤ 0.5%
2. vectorbt parity: same check independent of LEAN
3. IB Gateway boot: `docker compose --profile phase2 up ib_gateway` connects to IBKR paper; health check passes
4. Paper test: one paper signal-to-fill via ib-async direct path ≤ 5s round-trip
5. No HALT_NEW in 24h prior: check `risk_state.kill_switch_state = 'NORMAL'` at check time
6. Audit chain integrity: `verify_chain.py` CHAIN OK
7. S3 restore test: most recent backup is < 24h old and restores without error
8. Slippage calibration HEAD: `slippage_calibration_versions` has a version dated within 35 days (monthly cadence + 5-day grace)

If ANY item fails: cutover deferred; `cutover_aborted` event logged; `#critical` alert sent; operator re-schedules via DP-006.

**Month 5 end gate:**
- [ ] `docker compose --profile phase2 up lean_local ib_gateway -d` starts without error
- [ ] IB Gateway connected to IBKR paper account; `docker compose logs ib_gateway` shows `TWS API connection accepted`
- [ ] Cutover date confirmed in Decision-Point Register DP-006 with ≥5 CME sessions advance notice
- [ ] Pre-cutover checklist dry-run passes (run manually 1 week before actual cutover date to catch issues early)

---

# 5. Phase 2 Milestone Plan

**Phase 2:** months 5–9. Custom infrastructure replaces QC Cloud. Direct IBKR via ib-async. Track record continuous via QC adapter drain mode (24h post-cutover).

## Month 5–6 — Cutover Execution

**Cutover sequence (locked per backend-spec §1.5):**

Pre-cutover checklist runs automatically at `D-1` 17:00 ET:
1. Positions reconciled to zero on QC side (all positions closed or no open positions)
2. Audit chain integrity confirmed: `verify_chain.py` CHAIN OK
3. `risk_state` not in HALT_NEW within 24h prior (if HALT_NEW → defer cutover date)
4. LEAN Local signal-to-fill test: ≤ 5s round-trip
5. IB Gateway health: connected to IBKR live account

**Cutover day (D):**
- 17:00 ET: switch QC algorithm to drain mode (no new signals; existing positions maintain via QC until flat)
- 17:30 ET: first signal cycle runs on Phase 2 custom signal engine
- First fill via ib-async confirmed in audit log
- QC adapter retained in backfill-reads-only mode for 24h (confirms audit parity)

**Abort condition:** if any pre-cutover checklist item fails → cutover deferred; log in DP-006 with reason. Third abort → architecture re-evaluation (backend-spec §11.3 kill criteria).

**Month 6 end gate:**
- [ ] First Phase 2 live signal-to-fill in `fills` table with `execution_path=ib_async`
- [ ] `verify_chain.py` CHAIN OK across Phase 1 → Phase 2 boundary (no gap in sequence_no)
- [ ] Kill-switch SLO verified: time from `/halt` command to IB Gateway order cancel ≤ 5s

## Month 7 — Frontend Phase 2 Features

**New surfaces per frontend-spec §2.1:**
- Per-trade detail drawer (in-table preview at `/trades`)
- Full attribution view per trade
- Advanced search on trades
- Parameter PR proposal interface in `/system`
- Deployment management UI (rollback button)
- Agent activity feed in `/system`

**Month 7 end gate:**
- [ ] `/system/pr/:id` renders agent-drafted PR with LEAN backtest delta, diff, rationale, risk attestation
- [ ] Deployment rollback tested: `POST /api/system/deployments/:id/rollback` (re-auth) → VPS pulls previous image SHA → services restart on old version

## Month 8 — Hardening + Stress Tests

**Key milestones:**
- Bulk-approve "standard" button enabled on Today page (Phase 2 feature; non-anomaly signals only)
- Stress test runner available: six scenarios accessible from Today page stress-test button
- PDF export pipeline: Typst + Recharts export for `/performance` page
- Polygon.io contingent connector: if QC data gaps observed in backtesting, activate (backend-spec §11.3)
- Capacity analysis: run sizing at 5×, 10×, 25× current capital for v1 strategy

**Month 8 end gate:**
- [ ] Stress test button on Today page triggers job; SSE `job` event streams progress; result displays in modal
- [ ] PDF export for performance page: `POST /api/performance/export/pdf` returns downloadable PDF with equity curve + monthly returns table

## Month 9 — Second Strategy Preparation

**Key milestones:**
- Phase 2 portfolio live Sharpe target 1.2 confirmed (or shortfall documented in Decision-Point Register)
- Second strategy preparation begins: vol carry on SPX defined-risk
- Full 30 CME paper sessions required for second strategy before live (same gate as v1)
- `strategy_versions` table ready for second entry (no migration needed — INSERT only)

**Second strategy addition process (locked order; do not skip steps):**
1. `[BOTH]` Author v2 strategy skeleton in `strategies/v2_vol_carry/` — PR-required path
2. `[CLAUDE_CODE]` Author walk-forward validation + held-out backtest per backend-spec §10.6
3. `[OPERATOR]` Run 30 CME paper sessions on v2 (new paper-day clock; independent of v1 clock)
4. `[CLAUDE_CODE]` Add `strategy_versions` INSERT for v2 (no migration; schema already supports)
5. `[OPERATOR]` DP-012 decision: confirm v1 live validation sufficient before v2 live activation

**Month 9 end gate:**
- [ ] `GET /api/performance/summary?window=90d` shows portfolio Sharpe ≥ 1.2 (or DP-007 decision logged if below)
- [ ] Second strategy skeleton PR merged to `strategies/v2_vol_carry/` with unit tests passing
- [ ] v2 walk-forward validation passes: held-out Sharpe > 0 on out-of-sample period

---

# 6. Phase 3 Milestone Plan

**Phase 3:** months 9–12. Capital scaling. Family-money legal preparation. Prop-firm track-record export.

## Month 9–10 — Legal Structure Preparation

**Key milestones:**
- LLC formation initiated (state of NJ or Delaware; consult CPA)
- Securities lawyer consult scheduled: confirm whether operator's activity is personal trading or investment advisory; confirm LLC structure appropriate for F&F capital
- CPA engagement: confirm 475(f) mark-to-market trader tax status eligibility; decide before next tax filing

**Decision required:** DP-008 (LLC state), DP-009 (475(f) election), DP-010 (securities lawyer scope).

**Month 10 end gate:**
- [ ] LLC formation confirmation document received
- [ ] Securities lawyer consultation completed; written memo filed in safe

## Month 10–11 — Track Record Export + Capacity Analysis

**Key milestones:**
- Prop-firm track record export: CSV with audit hash-chain verification (backend-spec §11.4)
- Capacity analysis at 5×, 10×, 25× current capital for both strategies
- F&F acceptance gate assessment (DP-011): clean track record confirmed (Sharpe ≥ 1.5 over 9–12 months AND max DD ≤ 15%)

**Month 11 end gate:**
- [ ] `GET /api/audit/export?format=csv&include_hashes=true` produces verifiable export with prev_hash/record_hash columns
- [ ] Capacity analysis document filed showing ADV headroom at each capital tier

## Month 12 — F&F Onboarding or Prop Firm Path

**Decision required:** DP-012 (prop firm allocation vs. F&F first commit vs. continue solo).

**F&F acceptance gate (ALL conditions must be met):**
- [ ] Live track record ≥ 12 months clean
- [ ] LLC operational + securities lawyer green-light
- [ ] Rolling Sharpe ≥ 1.5 over any 9+ month window
- [ ] Max drawdown ≤ 15% over entire track record
- [ ] CPA enrollment flow completed for any additional account principal
- [ ] Explicit advance communication to F&F investors of expected drawdown profile (15–20% is normal; -25% triggers system halt)

**Prop firm path alternative:** if F&F not ready, export track record per §11.4; apply to prop firm allocation program.

**Month 12 end gate:**
- [ ] DP-012 logged with decision rationale and next-action
- [ ] `GET /api/performance/summary?window=365d` shows Sharpe ≥ 1.5 AND max_dd ≥ -15% (or deviation documented)

---

# 7. Component Dependency Graph

```mermaid
graph TD
  subgraph Accounts["Accounts (pre-Phase-0)"]
    DOMAIN[Apex Domain<br/>Cloudflare/Namecheap]
    IBKR_ACCT[IBKR Pro Account<br/>1–2 weeks]
    QC_ORG[QC Organization<br/>same-day]
    HETZNER[Hetzner VPS<br/>Ashburn + Falkenstein]
  end

  subgraph Secrets["Secrets Layer"]
    AGE[age key pair<br/>operator generates]
    SOPS[sops .enc.yaml files<br/>dev / paper / live]
    AGE --> SOPS
  end

  subgraph Infra["Infrastructure"]
    CADDY[Caddy Reverse Proxy<br/>TLS + rate limit]
    DOMAIN --> CADDY
    HETZNER --> CADDY
    SOPS --> CADDY
  end

  subgraph Auth["Auth Layer"]
    WEBAUTHN[WebAuthn Enrollment<br/>/setup flow]
    TOTP[TOTP Backup<br/>authenticator app]
    CADDY --> WEBAUTHN
    WEBAUTHN --> OPERATOR_ACCESS[Operator Web Access<br/>all authenticated routes]
    TOTP --> OPERATOR_ACCESS
  end

  subgraph DB["Database"]
    PG[PostgreSQL 16<br/>audit_log + all tables]
    AUDIT_SCHEMA[audit_log schema<br/>Alembic 0001]
    PG --> AUDIT_SCHEMA
  end

  subgraph AuditChain["Audit Chain"]
    JCS[JCS Canonicalization<br/>canonical body]
    HASH_CHAIN[Hash Chain Writer<br/>SERIALIZABLE + advisory lock]
    IMMU[Immutability Triggers<br/>BEFORE UPDATE/DELETE + TRUNCATE]
    AUDIT_SCHEMA --> HASH_CHAIN
    JCS --> HASH_CHAIN
    IMMU --> AUDIT_SCHEMA
    HASH_CHAIN --> GOLDEN[Golden Tests<br/>5 representative events]
  end

  subgraph QCAdapter["QC Adapter"]
    QC_POLL[QC ObjectStore Poll<br/>events 60s / acks 5s]
    QC_ORG --> QC_POLL
    QC_POLL --> JCS
    QC_POLL --> HASH_CHAIN
    GOLDEN --> QC_READY[Adapter Production-Ready<br/>Week 4]
  end

  subgraph Paper["Paper Trading"]
    QC_ALG[QC Algorithm<br/>v1 Trend-Following<br/>paper broker]
    QC_ORG --> QC_ALG
    QC_ALG --> QC_POLL
    QC_ALG --> SESSIONS[30 CME Paper Sessions<br/>Week 1–7 clock]
    SESSIONS --> LIVE_GATE[Live Trading Gate<br/>Week 8]
  end

  subgraph Backend["Backend Services"]
    FASTAPI[FastAPI Service<br/>HTTP + SSE]
    SIGNAL[Signal Engine<br/>Donchian/MA 17:30 ET]
    RISK[Risk Engine<br/>Stages 0–5 + Rings + State Machine]
    EXEC[Execution Service<br/>QC OS write (Phase 1)]
    RECON[Reconciliation Service<br/>60s cadence]
    CALIB[Slippage Calibration<br/>OLS monthly]
    AGENT[Claude Ops Agent<br/>bounded tool use]
    PG --> FASTAPI
    PG --> SIGNAL
    SIGNAL --> RISK
    RISK --> EXEC
    EXEC --> QC_POLL
    RECON --> HASH_CHAIN
    CALIB --> HASH_CHAIN
    CADDY --> FASTAPI
  end

  subgraph Frontend["Frontend"]
    NEXTJS[Next.js App<br/>Today + Trades + System + Calendar]
    SSE[SSE Channel<br/>/api/sse/events]
    FASTAPI --> SSE
    SSE --> NEXTJS
    WEBAUTHN --> NEXTJS
    NEXTJS --> OPERATOR_ACCESS
  end

  subgraph Discord["Discord"]
    BOT[Discord Bot<br/>discord.py]
    WEBHOOK[Webhook Pusher<br/>7 channels]
    FASTAPI --> BOT
    FASTAPI --> WEBHOOK
    BOT --> OPERATOR_ACCESS
  end

  subgraph LivePath["Live Trading (Phase 1)"]
    IBKR_ACCT --> QC_ALG_LIVE[QC Algorithm<br/>IBKR Pro live creds in QC vault]
    QC_READY --> QC_ALG_LIVE
    LIVE_GATE --> QC_ALG_LIVE
    OPERATOR_ACCESS --> QC_ALG_LIVE
  end

  subgraph Phase2["Phase 2 Additions"]
    IBGW[IB Gateway<br/>Docker phase2 profile]
    LEAN_LOCAL[LEAN Local<br/>on-demand]
    IBKR_ACCT --> IBGW
    IBGW --> EXEC_P2[Execution Service<br/>ib-async direct]
  end
```

**Critical build-order rules:**
1. Domain → Caddy → HTTPS → WebAuthn → operator has authenticated access
2. age key → sops → secrets in containers → any service can start
3. `audit_log` schema migration → hash-chain writer → QC adapter → golden tests
4. 30 CME paper sessions → live trading gate
5. Phase 1 live trading continuous → Phase 2 cutover (no gap in audit chain)

---

# 8. Decision-Point Register

Maintain this table throughout the build. For each decision made: fill in **Date decided**, **Choice made**, and **Rationale** columns. Log a `decision_diary` entry for decisions that affect trading parameters or capital.

| ID | Trigger | Choice | Default | Action | Audit entry? | Date decided | Choice made | Rationale |
|---|---|---|---|---|---|---|---|---|
| **DP-001** | IBKR Pro not approved by end of Week 2 | Pursue alternate broker (TradeStation, TD Ameritrade) OR wait up to 4 weeks total | Wait; prepare TradeStation application in parallel | If waiting: submit TradeStation application at Week 2 end; continue Phase 0 development; Phase 1 live deferred until a broker is approved. Phase 0 extends; does NOT cancel. | No | | | |
| **DP-002** | Sub-universe verification shows < 4 markets pass at $15k | Raise initial capital to $20k, OR accept 2–3 markets, OR defer universe to $25k | Raise initial capital to $20k | Update initial live capital target in accounts table; update position-sizing Stage 0 threshold test | Yes — `decision_diary` tag: `universe_change` | | | |
| **DP-003** | Live trading begins (Month 2) | Initial live allocation: $15k, $20k, or $25k | $15k (smallest size; minimizes bleed during early track record) | Fund IBKR Pro account with chosen amount; set `accounts.initial_equity` in DB; log capital event | Yes — `capital_event` | | | |
| **DP-004** | First agent-drafted parameter PR arrives in GitHub | Approve / request modifications / reject | Read the rationale section carefully before deciding; approve only if within stated mutable range | If approved: merge PR → CI deploys → parameter_set_hash updated; auto-revert in 14 days if Sharpe degrades. If rejected: add decision diary note with reason | Yes — `parameter_change_reviewed` | | | |
| **DP-005** | Mid-Phase-1 review (Month 4): Sharpe and drawdown assessment | Continue as-is / pivot strategy parameters / pause trading | Continue if Sharpe > 0 and max DD > -20% (no decommission floor triggered) | If continue: no action. If pivot: create parameter-change PR with Claude Code. If pause: invoke vacation mode; plan parameter review; set return date | Yes — `mid_phase_review` | | | |
| **DP-006** | Phase 2 cutover date selection (Month 5) | Select specific date D ≥ 5 CME sessions in advance | Select for a Monday open when no tier-1 macro events are scheduled | Insert cutover row via Claude Code DB command (Phase 1: no UI for this yet); confirm pre-cutover checklist passes at D-1 17:00 ET | Yes — `cutover_scheduled` | | | |
| **DP-007** | Phase 2 portfolio Sharpe < 1.2 for 60+ days | Continue and reoptimize / pause second-strategy prep / invoke strategy review | Continue with documented shortfall; set 90-day watch window | Log DP entry; Claude Ops agent drafts strategy review; operator decides on parameter adjust within mutable range vs. full strategy review (requires PR) | Yes — `strategy_review_triggered` | | | |
| **DP-008** | LLC formation (Month 9–10) | NJ LLC vs. Delaware LLC | Consult CPA first; Delaware LLC common for investment vehicles | CPA engagement decision. File Articles of Organization. Update operator profile in system. | No | | | |
| **DP-009** | 475(f) mark-to-market election (before first tax filing after live trading) | Elect 475(f) vs. default 1256 treatment for Section 1256 contracts | Consult CPA; futures already have 60/40 treatment under §1256 — 475(f) may not apply or benefit | CPA decides; operator signs election form by due date. Non-reversible after filed. | No | | | |
| **DP-010** | Securities lawyer consult (Month 9–10) | Scope: personal trading opinion only vs. full F&F structure opinion | Full F&F structure opinion required before accepting any external capital | Follow lawyer's written advice; do not accept F&F capital until green-light memo received | No | | | |
| **DP-011** | F&F acceptance gate (Month 12) | Accept first F&F commit ($250k cap) vs. defer vs. prop firm path | Defer unless ALL F&F gate conditions met (see §6 month 12) | If accepting: enroll F&F principal via CPA reader role; multi-account INSERT (no migration needed); complete lawyer memo review | Yes — `capital_event` type `ff_commitment_accepted` | | | |
| **DP-012** | Month 12 path decision | Prop firm allocation / F&F first commit / continue solo | Continue solo if F&F gate conditions not all met | Log decision with rationale; set 6-month target for whichever path chosen | Yes — `decision_diary` tag `path_decision` | | | |
| **DP-013** | Decommission floor triggered (any phase) | Resume with new strategy version / retire strategy / pivot parameters | Stop trading immediately; do not override automatically; schedule 48h reflection window | Invoke vacation mode immediately; Claude Ops agent drafts post-incident review template; operator completes write-up before any resume | Yes — `kill_switch_triggered` severity `incident_review` | | | |
| **DP-014** | Vacation start (any time) | Duration: 1–7 days / 8–14 days / 15–30 days | Use minimum duration; system auto-halts new signals; existing positions continue to be managed | `/vacation start [days]` in Discord (Phase 1). Web-only resume (`/vacation end` not supported via Discord). Set calendar reminder for return. | Yes — `vacation_mode_toggled` | | | |
| **DP-015** | IBKR margin call (broker-mandated liquidation outside system control) | Manual review and decide on position rebuild plan / halt + wait | Invoke kill switch immediately after liquidation; do not attempt to re-enter positions same day | `/halt margin-call-YYYYMMDD` in Discord; acknowledge in audit; 24h minimum reflection before any resume; review position sizing parameters with Claude Code | Yes — `kill_switch_triggered` reason `ibkr_margin_call` | | | |
| **DP-016** | Parameter range expansion proposal (operator wants to widen agent-mutable range) | Widen range / keep current range | Keep current range; do not widen under drawdown | File as PR (must be PR; agent cannot loosen risk without human approval). Claude Code authors `PARAMETER_RANGE_*` constant update. CI linter verifies change. | Yes — `parameter_range_changed` | | | |
| **DP-017** | Hetzner Falkenstein watchdog capacity unavailable at provisioning | Pick alternate EU DC (Nuremberg / Helsinki) OR pick US-West (Hillsboro) OR wait | Nuremberg (closest substitute to Falkenstein — same Hetzner DE region) | Provision watchdog in chosen DC; capture static IPv4; substitute into `<watchdog_static_ip>` Caddy allowlist at deploy | No (infra) | 2026-05-05 | Nuremberg (NBG1) | Falkenstein had no CX-line capacity; Nuremberg matches the spec's intent of EU watchdog geographically separated from US Ashburn. CX23 ($5.59/mo) replaces retired CX11 SKU. See `Docs/decisions-log.md`. |
| **DP-018** | Branch protection on `main` requires GitHub Pro on private repos | Upgrade to Pro $4/mo / make repo public / skip branch protection | Upgrade to Pro | Upgrade in GitHub Billing; apply branch protection rule via `gh api PUT /repos/.../branches/main/protection` | No (infra) | 2026-05-05 | Upgraded GitHub Pro $4/mo | Public repo would expose strategy/risk/audit code — wrong trade for $4/mo. Skipping protection misses Week 1 mechanical gate. See `Docs/decisions-log.md`. |
| **DP-019** | QuantConnect "Quant Researcher $20/mo" tier from spec doesn't exist at that price | Use Researcher $60 / build direct-IBKR (Phase 2 architecture) early / skip live trading | Use Researcher $60 | Subscribe to Researcher tier in QC org settings; capture Org ID + User ID + API token | No (infra) | 2026-05-05 | Researcher $60/mo | $40/mo over spec but inside soft alert ceiling. Direct-IBKR-from-start would push live trading back 3+ months. See `Docs/decisions-log.md`. |

---

# 9. Operational Runbook Excerpts

Each scenario: symptom → likely cause → verification → resolution → escalation.

---

## RB-001 — Discord Delivery Failing > 10 Minutes

**Symptom:** Expected `#daily-brief` or `#alerts` message did not arrive; operator checks Discord server and finds no recent posts.

**Likely cause:** (a) `webhook_pusher` container crashed; (b) Discord webhook URL invalidated (Discord rotates on security events); (c) `discord_bot` lost gateway connection; (d) network egress blocked from VPS.

**Verification:**
```bash
# On VPS:
docker compose logs webhook_pusher --tail 50
docker compose logs discord_bot --tail 50
# Look for: ConnectionError, 401 Unauthorized, 404 Not Found on webhook URL
curl -s https://discord.com/api/v10/gateway  # from VPS — should return JSON
```

**Resolution:**
1. If `webhook_pusher` crashed: `docker compose restart webhook_pusher`; check logs again.
2. If webhook 401/404: regenerate webhook URL in Discord server settings → update in `secrets/paper.enc.yaml` or `secrets/live.enc.yaml` via sops: `sops secrets/live.enc.yaml` → edit `discord.webhook_urls.<channel>` → save → `docker compose restart webhook_pusher`.
3. If `discord_bot` lost gateway: `docker compose restart discord_bot`; discord.py reconnects automatically.
4. If network blocked: `curl -v https://discord.com/api/v10/gateway` from VPS; check UFW: `ufw status`; check Docker egress network rule.

**Escalation:** if delivery fails for >1 hour and kill-switch alert was expected: operator checks system state via web UI. If web UI unreachable too, check watchdog email for VPS-down alert.

---

## RB-002 — Reconciliation Break Detected

**Symptom:** P0 alert in `#alerts`: `"Reconciliation tolerance breach"` or web `/system` reconciliation status shows red.

**Likely cause:** (a) ex-dividend adjustment not yet reflected; (b) QC ObjectStore event delay; (c) manual IBKR activity outside system; (d) data entry error in fills ingestion.

**Verification:**
```bash
docker compose logs reconciliation --tail 100
# Look for: mismatch details — which symbol, qty discrepancy, cash discrepancy
psql $DATABASE_URL -c "SELECT * FROM reconciliation_breaks ORDER BY detected_at DESC LIMIT 5;"
```
Check `reconciliation_breaks.break_details` for specific mismatch type.

**Resolution:**
1. Dividend ex-date: check IBKR account activity for cash dividend credit. If within T+1 grace and < 2× tolerance, mark acknowledged: `POST /api/system/reconciliation/:id/acknowledge` with diary note.
2. QC event delay: if `reconciliation_breaks.source=qc_objectstore` and break is < 30 min old, wait for next QC adapter poll cycle (60s); re-check.
3. Manual IBKR activity: do NOT reconcile by hand without logging; use `decision_diary` tag `manual_reconciliation` with full explanation.
4. Persistent mismatch after 2h: invoke `/halt recon-break-investigation` to stop new signals while resolving. Do not resume until break is resolved and root-cause understood.

**Escalation:** if break involves a position-quantity mismatch > 1 contract: invoke kill switch; do not resume until IBKR statement confirms position matches backend.

---

## RB-003 — Margin Auto-Trim Invoked

**Symptom:** `#alerts` P0 message: `"Margin protocol: graduated trim sweep initiated at 85% margin utilization"`. Positions may have been reduced automatically.

**Likely cause:** equity drawdown or volatility spike increased margin requirement without system receiving signal to reduce.

**Verification:**
```bash
docker compose logs risk --tail 100
# Look for: margin_trim_sweep_executed, trim_amount, post_trim_margin_pct
psql $DATABASE_URL -c "SELECT * FROM risk_state LIMIT 1;"
psql $DATABASE_URL -c "SELECT * FROM orders WHERE created_at > NOW() - interval '1 hour' ORDER BY created_at DESC;"
```

**Resolution:**
1. Review trim details in audit log: `GET /api/audit?event_type=margin_trim_sweep_executed&limit=10`.
2. If margin < 80% post-trim: system resumes normally; log acknowledgement in decision diary.
3. If margin still > 80% post-trim: per backend-spec §2.4.5, system escalates to HALT_NEW. Check `risk_state.kill_switch_state`.
4. Do NOT manually override margin trim. If trim was wrong (e.g., erroneous margin figure from IBKR), invoke vacation mode and investigate data source.

**Escalation:** if HALT_NEW triggered and margin still elevated: call IBKR phone desk; do not attempt to reduce positions manually outside the system.

---

## RB-004 — QC ObjectStore Poll Failing

**Symptom:** `#ops` message: `"QC adapter poll failure"`. Web `/system` shows QC adapter status degraded.

**Likely cause:** (a) QC API credentials expired/rotated; (b) QC platform outage; (c) network egress blocked; (d) rate limit hit.

**5–9 minute failure (degraded):**
- System continues; signals may be delayed; no kill switch yet.

**>10 minute failure (defensive_envelope):**
- Backend automatically transitions to HALT_NEW defensive_envelope; no new signals until QC adapter recovers.

**Verification:**
```bash
docker compose logs qc_adapter --tail 100
# Look for: 401, 429, ConnectionError, timeout
curl -s "https://www.quantconnect.com/api/v2/authenticate" \
  -H "Authorization: Basic $(echo -n '<user_id>:<api_token>' | base64)"
# Should return {"success": true}
```

**Resolution:**
1. Check QuantConnect status page (quantconnect.com/status).
2. If QC outage: wait; system in HALT_NEW (safe); no action required. Monitor QC status page.
3. If credentials issue: `sops -d secrets/live.enc.yaml | grep quantconnect` → verify token still valid; if expired, rotate in QC dashboard → update sops → `docker compose restart qc_adapter`.
4. If rate limit: QC API allows 30 requests/minute; check adapter polling interval configuration.

**Escalation:** if QC outage exceeds 4h and positions are open: verify positions directly in IBKR TWS (download via IBKR web portal). Log manual position snapshot in decision diary.

---

## RB-005 — Vol Regime Detector Trip (HALT_NEW)

**Symptom:** `#critical` P0 alert: `"Kill switch invoked: vol_regime HALT_NEW"`. All new signals blocked.

**Likely cause:** 20-day realized vol of vol-target composite exceeded 2× long-run expected. This is a normal protective mechanism, not an error.

**Verification:**
```bash
psql $DATABASE_URL -c "SELECT kill_switch_state, triggered_reason, triggered_at FROM risk_state LIMIT 1;"
# Expected: kill_switch_state = 'HALT_NEW', triggered_reason = 'vol_regime'
docker compose logs risk --tail 50
# Look for: vol_target_multiplier composition, m_convalescent value
```

**Resolution:**
1. Check `#daily-brief` for context: what happened to markets? Normal for vol spikes after macro events.
2. Do nothing for now — system is in CONVALESCENT path once 5 clean sessions pass (backend-spec §2.4.3).
3. Log a decision diary entry acknowledging the halt (tag: `halt_acknowledgement`).
4. If severity = `incident_review` (decommission floor check): complete written incident review before resuming (see RB-010).
5. CONVALESCENT → NORMAL automatic after 5 sessions with `m_convalescent=0.5` multiplier limiting new position size.

**Escalation (when to contact yourself for reflection):** if halt lasts > 7 days, operator receives daily reminder briefing. At 7 days: schedule 1h reflection session to assess whether strategy logic needs review or market regime has genuinely changed.

---

## RB-006 — Heartbeat Engagement Timeout

**Symptom:** `#critical` P0 alert: `"Defensive risk envelope: operator engagement timeout > 24h"`. Kill switch invoked.

**Likely cause:** Operator missed `[I'm here]` liveness button on `#daily-brief` for > 24h. This is by design — system halts if operator disappears.

**Verification:**
```bash
psql $DATABASE_URL -c "SELECT * FROM liveness_probes ORDER BY created_at DESC LIMIT 10;"
# Confirm last probe ack timestamp
```

**Resolution:**
1. Click `[I'm here]` button in Discord `#daily-brief` immediately.
2. Acknowledge the P0 alert via `[Acknowledge]` button in `#alerts` or web `/system`.
3. Log decision diary entry with reason for missed engagement (tag: `engagement_miss`).
4. Resume from web UI (re-auth required): navigate to `/system` → Kill Switch section → Resume.

**Prevention:** set a recurring daily reminder at 17:30 ET in operator's calendar to check `#daily-brief`. The 24h window is generous but requires a daily habit.

---

## RB-007 — Backend Unreachable (Watchdog Email Arrives)

**Symptom:** Email from Resend with subject like `[TRADING WATCHDOG] API unreachable — 3 consecutive failures`.

**Likely cause:** (a) VPS rebooted and Docker services didn't restart; (b) Caddy crashed; (c) VPS out of disk; (d) entire VPS unreachable (network issue at Hetzner).

**Verification:**
```bash
# From operator's laptop:
ping <hetzner-vps-ip>           # Is VPS pingable?
ssh operator@<hetzner-vps-ip>  # Can operator SSH in?
# If SSH works:
docker compose ps               # Which services are running?
df -h                           # Disk space?
docker compose logs caddy --tail 50
```

**Resolution:**
1. If SSH works and Docker is running but Caddy is stopped: `docker compose start caddy`.
2. If Docker services are all stopped (VPS rebooted): `docker compose up -d` (systemd wrapper should have done this automatically — check `systemctl status trading.service`).
3. If disk full: `docker system prune -f` (removes unused images/containers); check `/opt/trading/logs/` for large log files; rotate logs.
4. If VPS fully unreachable: log in to Hetzner Cloud Console → check VPS status → power cycle if needed.
5. If Hetzner outage: check hetzner.com/status; wait; positions are safe (IBKR holds them independently).

**After recovery:** IBKR phone desk if positions need immediate attention while VPS is down. Call IBKR: 1-877-442-2757 (US).

---

## RB-008 — WebAuthn Enrollment Failure on First Browser

**Symptom:** `/setup` page shows enrollment error: "credential creation failed" or browser shows "security key error".

**Likely cause:** (a) `rpId` mismatch (must be exact apex domain, no subdomain); (b) browser doesn't support WebAuthn; (c) no compatible authenticator available (Touch ID, Face ID, hardware key).

**Verification:**
```bash
# Check backend webauthn config:
sops -d secrets/live.enc.yaml | grep -A3 webauthn
# Confirm rp_id: <your-domain> (NOT www.<your-domain> or anything with subdomain)
# Check browser console for WebAuthn error code
```

**Resolution:**
1. Try a different browser (Chrome 67+ or Firefox 60+ required for WebAuthn).
2. Confirm URL in browser matches apex domain exactly (e.g., `https://mytrading.com` not `https://www.mytrading.com`).
3. If no hardware authenticator: use Touch ID (macOS) or Windows Hello or plug in a FIDO2 USB key (YubiKey 5 recommended).
4. As fallback for immediate access: use TOTP backup path — navigate to `/login` → click "Use backup code or TOTP" → enter TOTP code from authenticator app. Note: TOTP session has reduced privileges (frontend-spec §5.4); kill-switch action requires WebAuthn.

**Escalation:** if WebAuthn consistently fails after browser and authenticator troubleshooting, open `[CLAUDE_CODE]` session to debug `POST /api/auth/webauthn/register-begin` response; check `challenge` and `rpId` in the `PublicKeyCredentialCreationOptions` object.

---

## RB-009 — Decommission Floor Triggered

**Symptom:** `#critical` P0 alert: `"DECOMMISSION FLOOR: kill switch severity=incident_review"`. System has auto-halted.

**Conditions that trigger decommission floor (backend-spec §2.4.3):**
- Live 30-day Sharpe < 0, OR
- Live max DD ≤ -25%, OR
- 60-day Sharpe underperforms backtest by > 2 standard deviations

**Resolution:**
1. **Do not panic.** This is a planned safeguard. System is in HALT_NEW + incident_review. No new positions opened.
2. Existing positions continue to be managed (they are NOT liquidated automatically — only new signals blocked).
3. Complete the incident review write-up (required to resume; system will not resume without it). Go to web `/system` → Kill Switch → Resume path shows "Incident Review Required" gate.
4. The write-up template is auto-generated by Claude Ops Agent and posted to `#ops`. Fill it out honestly.
5. Decision DP-013 applies: choose resume with new strategy version / retire strategy / pivot parameters.
6. If choosing new strategy version: new strategy requires 30 CME paper sessions before live — LEAN paper mode only.
7. Resume is web-only with re-auth; NOT possible via Discord.

**Escalation (family-money context):** if family capital is already invested at this point (Phase 3), explicit advance communication is required per the agreed patience window. Do not let a drawdown month cause panic decisions.

---

## RB-010 — IBKR Margin Call (Broker-Mandated Liquidation)

**Symptom:** IBKR email and/or TWS notification that positions are being liquidated by IBKR due to margin deficiency. This is outside system control.

**Immediate response (within 30 minutes):**
1. Log in to IBKR TWS or web portal immediately; view current positions.
2. In Discord: `/halt margin-call-YYYYMMDD` to synchronize system state with broker reality.
3. Wait for IBKR liquidation to complete before any system action.
4. Note: system will show a reconciliation break as IBKR positions change without system-originated fills. This is expected.

**After liquidation:**
1. Compare backend `positions` table with IBKR actual positions: `GET /api/positions/current` vs. IBKR web statement.
2. Manually log fills that represent IBKR-forced liquidation: `[CLAUDE_CODE]` session to insert `fills` rows with `source=ibkr_forced_liquidation`.
3. `verify_chain.py` will show these as out-of-band fills; log with provenance note in `backfill_reason`.
4. Do NOT resume trading for at least 24h. Review position sizing and margin utilization parameters.

**Root cause analysis:** why did utilization reach margin-call level? Check `risk_state.margin_utilization_history`. If margin protocol (85% trim sweep) ran and still resulted in call, the trim amount was insufficient → DP-016 to review margin trim parameters.

---

## RB-011 — Operator Vacation Start/End Procedure

**Start vacation:**
1. In Discord: `/vacation start <N>` where N = number of days (1–30).
2. System enters vacation mode: new signals blocked; existing positions continue managed by risk engine.
3. A vacation mode banner appears on web today page and Discord `#ops` daily note.
4. Set a calendar reminder for return date.
5. Confirm: `GET /api/system/vacation-mode` returns `{"active": true, "return_date": "YYYY-MM-DD"}`.

**End vacation:**
1. Web UI ONLY — `/vacation end` is NOT supported via Discord (frontend-spec §6.3).
2. Navigate to `https://<your-domain>/system` → Account section → End Vacation → re-auth required (UV, user-verification gesture on WebAuthn).
3. Confirm: `GET /api/system/vacation-mode` returns `{"active": false}`.
4. Check `#daily-brief` for market summary since departure; review any alerts accumulated during absence.
5. Click `[I'm here]` on the first daily brief after return to reset engagement timer.

**If returning early and web is inaccessible:** call IBKR phone desk (1-877-442-2757) to request manual position review. Then restore VPS per RB-007 and end vacation via web.

---

# 10. Risk Register

| # | Risk | Probability | Impact | Mitigation | Monitoring Signal |
|---|---|---|---|---|---|
| **R-001** | Trend-follow drawdown 15–20% in months 4–8 | HIGH | Medium (expected; not failure) | Hard-coded -20% trailing kill switch; -25% decommission floor; advance F&F education about expected DD profile before any external capital. Documented expectation: this is a normal drawdown for trend-following. | `GET /api/performance/summary` — max_dd field; P0 alert at -15% warning; kill at -20% |
| **R-002** | WebAuthn enrollment fails on first browser | MEDIUM | Low (TOTP fallback available) | TOTP fallback provides immediate reduced-privilege access. Backup codes in safe. Runbook RB-008. Resolution target: 1 day. | First `/setup` attempt; Discord `/status` command for auth method |
| **R-003** | QC adapter audit gap during Phase 1 live | MEDIUM | High (track record integrity) | Gap-detection logic in QC adapter (compares `sequence_no` to expected monotonic count). Repair flow via backfill provenance (backend-spec §2.10.1). Weekly golden test against 5 representative events. | `verify_chain.py` weekly run; P1 alert on sequence_no gap detected |
| **R-004** | Operator psychological burnout from real drawdown | MEDIUM-HIGH | High (poor decisions amplify losses) | Pre-commit risk thresholds documented and unchangeable without PR. Vacation mode available. Weekly check-in habit: operator reads health score + daily brief. DP-005 decision at Month 4 is an explicit pause gate. | Operator engagement pattern; liveness probe ack rate; decision diary tone |
| **R-005** | Family money pressure during drawdown (months 8–12) | MEDIUM | High (irreversible decisions under pressure) | Explicit advance communication to all F&F investors of expected drawdown profile (15–20% is normal; -25% triggers automatic halt). F&F acceptance gate requires this documentation before any commit. Written memo from securities lawyer. | F&F acceptance gate checklist (§6 Month 12); communication log in decision diary |
| **R-006** | IBKR account opening rejected or delayed > 2 weeks | LOW | High (gates Phase 1 live start) | Apply Day 1 (§2 critical path). Prepare TradeStation application in parallel starting Week 2 if no approval. QC supports TradeStation as alternate broker. Phase 0 development continues regardless. | DP-001 trigger: no approval email by end of Week 2 |
| **R-007** | Phase 2 cutover failure | LOW | High (trading continuity risk) | Pre-cutover automated checklist at D-1 17:00 ET (backend-spec §1.5). Three abort conditions defined. Phase 1 QC continues until cutover confirmed clean. Rollback: re-enable QC live in < 30 min. | Pre-cutover checklist result; DP-006 tracking; cutover abort event in audit log |
| **R-008** | VPS catastrophic failure / full compromise | LOW | High (system offline; positions unmanaged) | External watchdog (Falkenstein) emails operator within 15 min of outage. IBKR holds positions independently. S3 Object Lock backup for DB + images. Gitea mirror for code. 4-hour RTO. | Watchdog email; Hetzner Cloud Console monitoring; S3 backup timestamp |
| **R-009** | Decommission floor triggered during family-money window (Phase 3) | MEDIUM | High (trust erosion) | Decommission floor is a documented expected event for trend-following. Explicit advance communication to F&F before any capital acceptance. New strategy version requires 30 paper sessions before live. Override path documented in DP-013. | `#critical` alert; decommission_floor_triggered audit event |
| **R-010** | Tax surprise at year-end | MEDIUM | Medium (cash flow; not trading) | 1099-B reconciliation pass after Feb 15 via `GET /api/audit/export`. CPA engagement by Month 3. DP-009 for 475(f) election before first tax filing. Track basis in `fills` table from day 1. | Annual cost + P&L reconciliation; CPA review before filing |
| **R-011** | Age key lost or destroyed (both copies) | LOW | Catastrophic (all secrets unrecoverable; system rebuild from scratch) | Two physical copies: fireproof safe + safety deposit box. Annual rotation reminder. If only one copy survives: immediately make new second copy. | Annual calendar reminder for key rotation and backup verification |
| **R-012** | Second strategy added prematurely before Phase 1 validated | LOW but operator-controllable | High (diversifies attention; amplifies drawdown if both fail) | Spec explicitly requires Phase 1 live validation before second strategy addition. 30 CME paper sessions required. DP-012 gate. This is a process risk, not a technical one: operator discipline is the control. | DP-012 gate check; `strategy_versions` table; no second strategy before Phase 2 |

---

# 11. Plan of Action — First 2 Weeks

Specific, ordered, time-approximate tasks. All tasks reference the pre-Phase-0 checklist (§2) for account prereqs.

> **Day 1 status:** ✅ COMPLETED 2026-05-05. All tasks executed. Concrete values captured in `Docs/decisions-log.md`. The procedural steps below remain the canonical template; deviations and actuals (Hetzner DC, watchdog DC, QC pricing, GitHub Pro) are in the decisions log.

---

## Day 1 (Monday, Week 1)

**08:00 [OPERATOR]** Submit IBKR Pro account application.
- URL: https://www.interactivebrokers.com/en/index.php?f=4969
- Account type: Individual
- Enable: futures trading, Level 2 options (for Phase 2 vol carry strategy prep)
- Market data: defer subscriptions until TWS is installed; do not pay yet
- Funding: DO NOT fund yet — wait for account approval confirmation
- Note the application reference number in a physical notebook

**09:00 [OPERATOR]** Register apex domain.
- Provider: Cloudflare (preferred; includes free DNS + DDOS protection) or Namecheap
- Choose a domain name: something professional and personal (e.g., `<lastname>trading.com`); this becomes your WebAuthn `rpId` and is permanent
- Cost: ~$10–15/yr
- After registration: note the domain in a physical notebook

**09:30 [OPERATOR]** Provision Hetzner Cloud servers.
- Log in to hetzner.com/cloud → Create account if needed
- Create Project "trading-primary"
- In Ashburn region: create server `trading-primary` → Type CCX13 (2 vCPU / 8 GB RAM / 80 GB NVMe) → Ubuntu 24.04 LTS → add SSH public key → create
- Create Project "trading-watchdog" (separate project is cleaner)
- In Falkenstein region: create server `trading-watchdog` → Type CX11 (2 vCPU / 2 GB) → Ubuntu 24.04 LTS → add SSH public key → create
- **Capture both public IPv4 addresses** — write them down physically and digitally

**10:00 [OPERATOR]** Create GitHub account and repo.
- github.com → sign in or create account with username you'll use for `ghcr.io/operator/`
- Create new repo: `trading-system` → private → initialize with README
- Note: username choice is permanent for GHCR image paths

**10:30 [OPERATOR]** Create QuantConnect account.
- quantconnect.com → sign up (or log in)
- Create new Organization: name it after your project (e.g., "Patel Trading")
- Upgrade to Quant Researcher plan ($20/mo)
- Note: Organization ID found in Settings → My Organization

**11:00 [CLAUDE_CODE]** Initialize repository structure.
Open Claude Code session. Describe:

> "Initialize the trading-system repo at github.com/<your-username>/trading-system with the directory structure from backend-spec §1.1. Create:
> - Complete directory tree (all subdirectories listed in spec)
> - `pyproject.toml` with Python 3.11 requirement and dev dependencies (pytest, mypy, ruff, gitleaks)
> - `.github/workflows/ci.yml`: test + type-check + lint + gitleaks + docker build steps
> - `docker-compose.yml` skeleton with all 19 services (using placeholder images initially; phase2 profile for ib_gateway and lean_local)
> - `deploy/Caddyfile` skeleton
> - Branch protection rule on `main`: require CI to pass; no direct push"

Review Claude Code's output before approving. Confirm the CI workflow runs on every PR to `main`.

**14:00 [OPERATOR]** SSH into Hetzner primary VPS.
```bash
ssh root@<hetzner-ashburn-ip>
# Update OS:
apt update && apt upgrade -y
# Create trading user:
useradd -m -s /bin/bash trading
usermod -aG docker trading
# Install Docker:
curl -fsSL https://get.docker.com | sh
# Configure UFW:
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw enable
# Clone repo (will need GitHub credentials or deploy key):
mkdir -p /opt/trading
chown trading:trading /opt/trading
```

**15:00 [OPERATOR]** Configure DNS.
- Log into Cloudflare (or Namecheap) DNS management
- Add A record: `<your-domain>` → `<hetzner-ashburn-ip>` → TTL 300
- Add A record: `www.<your-domain>` → `<hetzner-ashburn-ip>` → TTL 300 (optional; Caddy will redirect to apex)
- DNS propagation: 5–30 minutes for Cloudflare; up to 24h for Namecheap

---

## Day 2 (Tuesday, Week 1)

**09:00 [OPERATOR]** Verify DNS propagation.
```bash
# From operator laptop:
dig +short <your-domain>      # Should return Hetzner IPv4
curl -I http://<your-domain>  # Should return something (even a connection refused is DNS working)
```

**09:30 [CLAUDE_CODE]** Set up GitHub App for in-app PR review surface.
Open Claude Code session. Describe:

> "Create a GitHub App configuration for the trading-system repo that enables in-app PR review. The app should:
> - Have read/write access to pull_requests and contents
> - Generate a private key for the API (will be stored in sops)
> - Create the GitHub App via gh CLI: `gh api POST /user/apps` or walk through the GitHub Apps settings UI
> After creating, output:
> - App ID
> - Installation ID
> - Instructions for adding private key to sops secrets"

> **2026-05-05 deviation:** `POST /user/apps` is not a real GitHub endpoint; the manifest flow requires a live HTTP redirect URL the backend doesn't have on Day 2. Manual UI walkthrough is the correct path. Canonical artifacts now live at `deploy/github-app/manifest.json` (declared permissions) and `deploy/github-app/README.md` (operator click-by-click). See `Docs/decisions-log.md` 2026-05-05 Day 2 — "GitHub App created via UI walkthrough, not API" + "GitHub App created and installed (complete)".

**11:00 [CLAUDE_CODE]** Author v1 strategy skeleton.
Open Claude Code session. Describe:

> "Author the v1 trend-following strategy in `strategies/v1_trend_following/`. This is a Donchian channel / moving average crossover trend-following system for the Phase 1 sub-universe (micro futures + bond ETFs). The strategy should:
> - Produce daily signals at 17:30 ET
> - Include a `sizing_trace` dict on each signal documenting Stage 0–5 inputs/outputs
> - Include audit event hooks for `signal_emitted`, `signal_rejected`, `universe_exclusion`, `universe_inclusion`
> - Be compatible with QuantConnect's LEAN algorithm framework (Python)
> - Include a corresponding QC algorithm wrapper in `lean/v1_qc_algorithm.py`
> Produce unit test stubs in `tests/unit/test_strategy_v1.py`."

Review the strategy description (not line-by-line code). Confirm: does the strategy logic match a Donchian/MA crossover trend system? Approve or redirect.

**14:00 [OPERATOR]** Discord server creation.
- Create new Discord server (private; operator-only)
- Create text channels: `#daily-brief`, `#signals`, `#fills`, `#alerts`, `#critical`, `#ops`, `#audit`
- Create Discord application at discord.com/developers → New Application → "Trading System Bot"
- Add Bot to application → copy bot token (store securely — this goes in sops)
- Invite bot to server with `applications.commands` and `bot` scopes; permissions: send messages, embed links, use slash commands

**15:00 [OPERATOR]** Generate sops age keys (3 keys: dev, paper, live).
```bash
# On operator laptop:
age-keygen -o ~/dev-key.txt    # save pub key + private key
age-keygen -o ~/paper-key.txt
age-keygen -o ~/live-key.txt
# Print ALL THREE to acid-free paper and store in fireproof safe
# Store private keys to ~/.config/sops/age/keys.txt (one per line)
# DELETE the .txt files after printing (keys.txt remains)
```

---

## Day 3 (Wednesday, Week 1)

**09:00 [CLAUDE_CODE]** Initialize sops configuration.
Open Claude Code session. Describe:

> "Initialize sops secrets management per backend-spec §8.1.1:
> 1. Create `.sops.yaml` in repo root with three creation rules matching dev/paper/live age public keys (operator will provide the three public keys as input)
> 2. Create `secrets/paper.enc.yaml` with all required secret keys (blank values to be filled) per the spec §8.1.1 sample structure
> 3. Create `secrets/dev.enc.yaml` with mock values safe for local development
> 4. Create `secrets/live.enc.yaml` with all required secret keys (blank values)
> 5. Ensure `.gitignore` does NOT exclude secrets/*.enc.yaml (these encrypted files belong in git)"

Fill in actual values for `secrets/paper.enc.yaml`: QC API token + organization ID, Discord bot token, Resend API key (after creating Resend account), Postgres password (choose a strong random password). Keep `secrets/live.enc.yaml` mostly blank for now — fill in when IBKR approved.

**11:00 [CLAUDE_CODE]** Author Postgres 16 schema migrations.
Open Claude Code session. Describe:

> "Author Alembic migrations for the initial Postgres schema per backend-spec §3. Specifically:
> 1. `alembic/versions/0001_audit_log.py` — `audit_log` table with all columns from spec §3.2 including hash-chain columns
> 2. `alembic/versions/0002_core_tables.py` — `signals`, `orders`, `fills`, `trades`, `positions`, `balances` tables from spec §§3.3–3.9
> 3. `alembic/versions/0003_risk_tables.py` — `risk_state`, `parameters`, `parameter_sets`, `strategy_versions`, `slippage_calibration_versions` from spec §§3.10–3.14
> 4. `alembic/versions/0004_ops_tables.py` — remaining tables from spec §§3.15–3.29
> 5. `alembic/versions/0005_immutability.py` — BEFORE UPDATE/DELETE triggers on audit_log, EVENT TRIGGER for TRUNCATE, REVOKE UPDATE/DELETE/TRUNCATE on app_service role per spec §2.10.2
> 6. `alembic/versions/0006_roles.py` — create Postgres roles per spec §8.2: app_service, app_service_readonly, app_owner, dba_breakglass
> Include unit tests for trigger blocking in tests/integration/test_audit_immutability.py"

Review migration list; approve or redirect. Do not merge until reviewed.

**14:00 [OPERATOR]** Install tooling on laptop (if not yet done from §2.2 checklist).
```bash
brew install python@3.11 git sops age gh
pip3 install claude-code  # or install via claude.ai/code
docker --version           # Docker Desktop should be running
```

---

## Day 4 (Thursday, Week 1)

**09:00 [CLAUDE_CODE]** Deploy v1 strategy to QuantConnect paper project.
Open Claude Code session. Describe:

> "Configure the v1 strategy to run on QuantConnect paper trading. Specifically:
> 1. Generate a `lean.json` configuration file in `lean/` with paper broker settings and the Phase 1 sub-universe (micro futures + bond ETFs)
> 2. The QC algorithm file `lean/v1_qc_algorithm.py` should use QC's paper broker
> 3. Document the manual steps needed in QC dashboard to upload the algorithm and start live paper trading (Claude Code cannot do this for the operator — operator must do it in QC web UI)
> Output: numbered steps for operator to follow in QuantConnect dashboard"

**10:00 [OPERATOR]** Launch paper trading on QuantConnect.
Follow the numbered steps from Claude Code. In QC dashboard:
- Upload algorithm code
- Create new Live Algorithm → paper broker → select IBKR paper account if available, else QC paper
- Start algorithm
- Confirm: status shows "Running" in QC Live Trading dashboard
- Note the start timestamp — this is the paper-day clock start

**13:00 [CLAUDE_CODE]** External watchdog setup on Falkenstein VPS.
Open Claude Code session. Describe:

> "Write a minimal external watchdog script for deployment on the Hetzner Falkenstein CX11 VPS. Per backend-spec §1.6 and §2.12:
> - Single Python script `watchdog/watchdog.py`
> - Pings `https://<your-domain>/api/health` every 5 minutes via GET request
> - On 3 consecutive failures: sends email via Resend API (SMTP or API call) to operator email
> - Uses a bearer token in the health check request (stored as env var on the watchdog VPS)
> - systemd timer unit file `watchdog/trading-watchdog.timer` + `watchdog/trading-watchdog.service`
> - Deployment instructions for the Falkenstein VPS"

Deploy to Falkenstein VPS:
```bash
ssh root@<hetzner-falkenstein-ip>
apt update && apt install -y python3 python3-pip
# Copy watchdog.py and systemd files
systemctl enable trading-watchdog.timer
systemctl start trading-watchdog.timer
```

---

## Day 5 (Friday, Week 1)

**09:00 [OPERATOR]** Verify QC paper trading (first session).
- Log into QC dashboard → Live Trading → confirm algorithm is Running
- Review Logs tab: confirm at least one market data event and one algorithm cycle logged
- Count this as Paper Session 1

**10:00 [CLAUDE_CODE]** Author FastAPI skeleton with health endpoint.
Open Claude Code session. Describe:

> "Create the FastAPI service skeleton in `services/api/`:
> - `main.py`: FastAPI app with CORS, CSRF double-submit, session cookie middleware
> - `GET /api/health`: returns `{status, environment, version, db_connected}` 
> - `POST /api/setup/verify-token`: one-time bootstrap endpoint per backend-spec §3.1.1
> - SSE endpoint scaffold at `/api/sse/events` (returns heartbeat pings for now)
> - Caddy Caddyfile at `deploy/Caddyfile` per backend-spec §9.2.1
> - `docker-compose.yml` with `api` and `caddy` and `postgres` services running
> Deploy to paper VPS and confirm health endpoint reachable."

**14:00 [OPERATOR]** Test deployment.
```bash
# From laptop:
curl -s https://<your-domain>/api/health
# Expected: {"status":"ok","environment":"paper","version":"...","db_connected":true}
```

If health returns 502 or connection refused, check `docker compose logs caddy` and `docker compose logs api` on the VPS for startup errors.

**15:00 [OPERATOR]** Week 1 review.
- Update Week 1 verification gate checkboxes at top of §3
- Log any deviations in the Decision-Point Register
- Confirm IBKR application email received (or note: pending)
- Paper session count: 1

---

## Day 6 (Monday, Week 2)

**09:00 [CLAUDE_CODE]** Author risk engine `services/risk/sizing.py` Stages 0–5.
Open Claude Code session. Describe:

> "Author the complete position-sizing pipeline in `services/risk/sizing.py` per backend-spec §2.4.1. Implement all five stages:
> - Stage 0: universe filter — exclude any market where 1-contract notional > 50% × equity
> - Stage 1: inverse-vol weighting (60-day σ; nearest_PSD repair if correlation matrix non-PSD per Higham algorithm)
> - Stage 2: per-position cap (25% target weight / 50% override)
> - Stage 3: cluster shrink (iterative ≤10; on non-convergence drop lowest-momentum signal in binding cluster and restart)
> - Stage 4: gross/net cap (3.0× / 1.5× equity)
> - Stage 5: lot rounding (banker's rounding; detect sub_minimum_size drops)
> 
> Each stage must return a `sizing_trace` dict for audit. Write unit tests in `tests/unit/test_sizing.py` with the specific test cases from backend-spec §10.1:
> - Stage 0 at $15k/$25k/$50k/$100k tiers
> - Stage 2 50%-override for /MES at $20k
> - Stage 3 non-convergence drops lowest-momentum signal
> - Stage 5 sub_minimum_size detection"

Review the explanation of each stage before approving.

**11:00 [CLAUDE_CODE]** Author sub-universe verification script.
Open Claude Code session. Describe:

> "Write `scripts/verify_universe.py --equity <amount>` that:
> 1. Loads Phase 1 contract list (micro futures: /MES, /MNQ, /MYM, /M2K, /MGC, /MCL, /MBT; bond ETFs: /ZN, /ZB, /ZF, /ZT)
> 2. For each contract: fetches 1-contract notional from QC bundled data (or hardcoded recent values as fallback)
> 3. Applies Stage 0 rule: 1-contract notional ≤ 50% × equity → PASS
> 4. Prints table: Market | 1-contract-notional | 50%-threshold | PASS/FAIL
> 5. Prints summary: N markets pass at $<equity>"

**14:00 [OPERATOR]** Check IBKR application status.
- Log in to interactivebrokers.com → Account Management
- Check application status: typically shows "Pending" for 1–2 weeks
- If status shows "Additional Information Required": respond to all requests within 24h to avoid delays
- If no status update after Day 7: call IBKR: 1-877-442-2757

---

## Day 7 (Tuesday, Week 2)

**09:00 [BOTH]** Run sub-universe verification.
```bash
# From operator laptop in repo root:
python3 scripts/verify_universe.py --equity 15000
python3 scripts/verify_universe.py --equity 20000
python3 scripts/verify_universe.py --equity 25000
```
Review results. Log in DP-002 if any adjustment to initial capital target is needed.

**10:30 [CLAUDE_CODE]** Author kill-switch state machine.
Open Claude Code session. Describe:

> "Author the kill-switch state machine in `services/risk/state_machine.py` per backend-spec §2.4.3. States: NORMAL, HALT_NEW_routine, HALT_NEW_defenv, HALT_NEW_incident, CONVALESCENT. All transitions must:
> 1. Write a `state_transition_*` audit event (via audit service, SERIALIZABLE)
> 2. Update `risk_state` table single-row with transition timestamp, reason, severity
> 3. Emit SSE `risk_state` event to connected frontend clients
> 
> Implement CONVALESCENT session counter: increments at each CME session close; resets on any trigger; CONVALESCENT → NORMAL after exactly 5 clean sessions.
> 
> Write unit tests per backend-spec §10.1 test inventory:
> - NORMAL → HALT_NEW for each trigger reason × each severity
> - HALT_NEW → CONVALESCENT (human resume)
> - CONVALESCENT → NORMAL after exactly 5 sessions
> - CONVALESCENT → HALT_NEW resets counter"

**14:00 [BOTH]** Operator learning session: kill-switch state machine.
Operator asks Claude Code to walk through the state machine diagram verbally. Goal: operator understands what triggers HALT_NEW (15+ conditions), what CONVALESCENT means (reduced size for 5 sessions), and when incident_review severity applies (requires written review before resume).

---

## Day 8 (Wednesday, Week 2)

**09:00 [OPERATOR]** Age key generation and backup.
```bash
# On operator laptop:
# Generate three separate keys (dev, paper, live):
age-keygen 2>&1 | tee ~/age-dev-key.txt
age-keygen 2>&1 | tee ~/age-paper-key.txt
age-keygen 2>&1 | tee ~/age-live-key.txt

# Add all private keys to sops key file:
cat ~/age-dev-key.txt >> ~/.config/sops/age/keys.txt
cat ~/age-paper-key.txt >> ~/.config/sops/age/keys.txt
cat ~/age-live-key.txt >> ~/.config/sops/age/keys.txt

# Print all three key files on acid-free paper — print NOW, immediately
# Store in fireproof safe at home
# Store second copy in safety deposit box or with a trusted person
# DELETE key files from laptop:
rm ~/age-dev-key.txt ~/age-paper-key.txt ~/age-live-key.txt
# ~/.config/sops/age/keys.txt remains — this is your only digital copy
```

Critical: if `keys.txt` is lost AND physical copies are lost, ALL encrypted secrets are permanently unrecoverable. The system cannot be restored. This is irreversible.

**10:00 [CLAUDE_CODE]** Initialize sops.
Open Claude Code session. Provide the three public keys from `keys.txt` (lines starting with `# public key:`). Describe:

> "Initialize sops secrets management:
> 1. Create `.sops.yaml` with three age creation rules:
>    - `secrets/dev.enc.yaml` → age1devkey...
>    - `secrets/paper.enc.yaml` → age1paperkey...
>    - `secrets/live.enc.yaml` → age1livekey...
> 2. Create `secrets/dev.enc.yaml` with safe mock values for local development (fake DB password, fake API tokens)
> 3. Create `secrets/paper.enc.yaml` with all required keys from backend-spec §8.1.1 structure (actual values for QC API token, Discord bot token, Resend API key, Postgres password; operator will provide these in the session)
> 4. Create `secrets/live.enc.yaml` with all required keys but blank/placeholder values (will be filled when IBKR approved)
> 5. Confirm each file is git-committable (sops-encrypted ciphertext only — no plaintext)"

After Claude Code creates files: `sops -d secrets/paper.enc.yaml` must return plaintext without error.

**14:00 [OPERATOR]** VPS secrets deployment.
Copy `secrets/paper.enc.yaml` to VPS (encrypted — safe to transfer):
```bash
scp secrets/paper.enc.yaml operator@<hetzner-ip>:/opt/trading/secrets/paper.enc.yaml
```
Copy age private key to VPS for runtime decryption:
```bash
# VPS: create systemd credential store
ssh root@<hetzner-ip>
systemd-creds encrypt --name=age_key < /dev/stdin > /etc/credstore.encrypted/age_key
# Paste: the PRIVATE key line from your paper/live age key (the "AGE-SECRET-KEY-..." line)
# Press Ctrl+D when done
chmod 600 /etc/credstore.encrypted/age_key
```
Configure systemd trading service per backend-spec §8.1.3 to load the age key credential.

---

## Day 9 (Thursday, Week 2)

> **Calendar mapping note (added 2026-05-10):** the operator's actual Day 9 = 2026-05-10 Sunday, NOT this nominal Thu Week 2. The ~1-day cadence drift documented in `Docs/decisions-log.md` 2026-05-09 Day 8 calendar-mapping entry continues. Both [CLAUDE_CODE] tasks below (09:00 decision_diary + vacation; 11:00 calendar_import) were already shipped Day 7 via PR #28 (`Docs/decisions-log.md` 2026-05-07 Day 6-9 [CLAUDE_CODE] chain entry). The operator's Day 9 substance shifted to **Week 3 Wed** work — `services/reconciliation/recon.py` (PR #42, see §3 Week 3 "Bonus shipped this Week"). The 14:00 [OPERATOR] sops workflow learning session below is the only Day 9 nominal task still open; not a delivery gate.

**09:00 [CLAUDE_CODE]** Decision diary service + vacation mode.
Open Claude Code session. Describe:

> "Author two services per backend-spec §§3.13 and 3.18:
> 1. `services/audit/decision_diary.py`: writes entries to `decision_diary` table; validates tag enum (`signal_override`, `parameter_change_reviewed`, `halt_acknowledgement`, `engagement_miss`, `path_decision`, `universe_change`, `mid_phase_review`, `strategy_review_triggered`, `cutover_scheduled`, `capital_event`, `manual_reconciliation`, `vacation_mode_toggled`); enforces `reasoning_text` 10–2000 char constraint; writes corresponding `decision_diary_logged` audit event
> 2. `services/scheduler/vacation.py`: `start_vacation(days: int)`, `end_vacation()` — validates max 30 days; writes `vacation_mode_toggled` audit event; updates `vacation_mode` table; emits SSE `risk_state` event
> Wire both to REST API endpoints:
> - `POST /api/decision-diary`
> - `POST /api/system/vacation-mode/start`
> - `POST /api/system/vacation-mode/end` (re-auth required; web-only)
> Write unit tests for vacation mode: max 30 days enforcement; end requires web session (not Discord path)."

**11:00 [CLAUDE_CODE]** Calendar import service.
Open Claude Code session. Describe:

> "Author `services/scheduler/calendar_import.py` per backend-spec §2.9:
> - Import macro events from Forex Factory (tier-1: NFP, CPI, FOMC, Fed Chair speak; tier-2: PMI, retail sales; tier-3: all others)
> - Import from Trading Economics as secondary source
> - Store events in `macro_events` table (backend-spec §3.28)
> - Run daily at 20:00 ET (APScheduler cron job)
> - Emit `calendar_event_imported` audit event for each new event
> - Calendar ratification: daily check at 16:00 ET — if tomorrow has tier-1 events that have NOT been ratified by operator, emit `calendar_unratified` kill-switch trigger (HALT_NEW routine severity)
> Wire Discord `/calendar` and `/ratify` command stubs (full implementation in Week 7)"

**14:00 [OPERATOR]** Learning session: sops workflow.
Practice the edit-a-secret workflow:
```bash
# Open secrets/paper.enc.yaml for editing (decrypts in-place to $EDITOR):
sops secrets/paper.enc.yaml
# Edit a value; save; file is re-encrypted automatically on close
# Confirm encrypted file changed:
git diff secrets/paper.enc.yaml   # should show different ciphertext
```
Goal: operator can independently update a secret value when credentials rotate, without Claude Code.

---

## Day 10 (Friday, Week 2)

> **Calendar mapping note (added 2026-05-11):** the operator's actual Day 10 = 2026-05-11 Monday, NOT this nominal Fri Week 2. The ~1-day cadence drift documented in `Docs/decisions-log.md` 2026-05-09 Day 8 + 2026-05-10 Day 9 calendar-mapping entries continues. The [CLAUDE_CODE] substance shifted to **Week 3 Thu** work — `services/webhook_pusher/` alerts pipeline (PR #44; see §3 Week 3 verification gate box 4 status note). The Day 10 [OPERATOR] tasks below (09:00 VPS startup walkthrough; 11:00 log-reading training; 14:00 Week 2 close-out) are largely already covered in spirit by the Day 5 + Day 6 carryover bringup work + Day 4 watchdog journald reads + ongoing PR-review surfaces; flagged as covered, not delivery gates. Week 2 close-out itself binds on DP-001 closure (IBKR approval window opens TODAY 2026-05-11), not on a calendar slot.

**09:00 [OPERATOR]** Full VPS startup walkthrough.
SSH into Hetzner VPS and run through the full startup sequence manually:
```bash
ssh operator@<hetzner-ip>
cd /opt/trading
# Pull latest code:
git pull origin main
# Start all services:
docker compose up -d
# Wait 30 seconds; check status:
docker compose ps
# Expected: api, caddy, postgres, audit, qc_adapter, discord_bot all show "running" or "healthy"
# Check each service log:
docker compose logs api --tail 20
docker compose logs postgres --tail 20
docker compose logs caddy --tail 20
docker compose logs qc_adapter --tail 20
```
Identify one log entry you don't understand → ask Claude Code to explain it in plain English. This is the start of operational log-reading competence.

**11:00 [BOTH]** Log reading training session.
Claude Code injects a synthetic error into the paper environment logs (a mock startup exception in the `signal` service). Operator reads `docker compose logs signal --tail 50` and identifies:
1. Which service has the error
2. What type of error it is (exception class name)
3. Whether it is a startup failure or a runtime failure

If operator cannot identify the error type without help within 5 minutes: Claude Code explains; repeat with a different synthetic error next week.

**14:00 [OPERATOR]** Week 2 close.
- Update verification gate checkboxes for Week 2 in §3
- Count paper sessions: target ≥ 5 by end of Week 2 (one per CME trading day since Week 1)
- Check DP-001 status: IBKR approved? If no approval yet but still within 2-week window, no action needed
- File DP-002 entry if any universe adjustment was needed based on sub-universe verification results

---

# 12. Update Protocol

This is a living document. Do not let it become stale. The following schedule and triggers govern when to update which section.

## 12.1 Phase 0 (Weekly Updates)

**Every Friday:**
- Mark completed verification gate items with date: `- [x] <item> — completed 2026-05-08`
- If any gate item is not met, add a red flag note below it: `> NOT MET — deferred to <date> — reason: <reason>`
- Record paper session count in a running tally at the top of the Week N section

**Any time a deviation occurs from the plan:**
- Add a deviation note inline: `> DEVIATION: <what changed> — <date> — DP-XXX logged`
- Log corresponding entry in Decision-Point Register §8

## 12.2 Phase 1+ (Decision-Point Updates)

**Each time a DP-XXX decision is made:**
1. Fill in the `Date decided`, `Choice made`, and `Rationale` columns in §8
2. If a `decision_diary` entry was required: confirm the `POST /api/decision-diary` was submitted and the audit reference is logged
3. If the decision changes a risk parameter or strategy version: confirm the corresponding PR was merged and the `strategy_version_deployed` or `parameter_change_applied` audit event is in the log

## 12.3 Risk Register Updates

**Add a new risk:**
- Add row to §10 table with all fields filled
- Assign `R-NNN` ID sequentially

**Close a mitigated risk:**
- Add `**(CLOSED YYYY-MM-DD)**` to the risk name
- Add a note in the Monitoring Signal column: `closed — mitigation confirmed`

## 12.4 Runbook Updates

**After any operational incident:**
1. Follow the applicable runbook entry
2. After resolution: add a postmortem note below the runbook entry:
   `> INCIDENT YYYY-MM-DD: <1-line summary> — root cause: <cause> — resolution time: Xh — new monitoring added: <yes/no>`
3. If the incident exposed a gap in the runbook: add a new entry `RB-NNN` for the missing scenario
4. Tag the incident in the audit log via `incident_reviews` table (backend-spec §3.25)

## 12.5 What NOT to Update Here

Do not update this guide for:
- Code-level changes (those belong in PRs and commit messages)
- Architecture decisions already locked in the specs (those belong in the specs, not this guide)
- Daily P&L or position tracking (those are in the database and Discord daily brief)
- Anything that is correctly derivable from git log

---

---

# 13. First Live Trading Day Checklist

The first live trading day (Month 2, after Phase 0 completes) is a milestone. Run this checklist on the morning of Day 1 of live trading. Complete every item before the first signal cycle fires at 17:30 ET.

## 13.1 Morning Checklist (08:00 ET, Day 1 Live)

**[OPERATOR] Account verification:**
- [ ] Log in to IBKR Pro account via TWS or web portal
      Confirm: account shows `live-small` funding ($15–25k per DP-003 decision); margin enabled; futures and ETFs trading permissions active
- [ ] Check IBKR account status: "Funded" or "Active" (not "Pending" or "Restricted")
- [ ] Confirm IBKR is connected to QuantConnect by checking QC dashboard → Live Trading → IBKR connection indicator

**[OPERATOR] System health check:**
```bash
# From VPS:
docker compose ps            # all services showing healthy
curl -s https://<your-domain>/api/health | python3 -m json.tool
# Expected: {"status":"ok","environment":"live-small","db_connected":true}
```
- [ ] API health: `status=ok`, `environment=live-small`
- [ ] All 14 Phase 1 services showing `running` or `healthy` in `docker compose ps`
- [ ] QC adapter last poll time < 5 min: `docker compose logs qc_adapter --tail 5 | grep poll_success`
- [ ] Audit chain intact: `python3 services/audit/verify_chain.py --env live` → `CHAIN OK`

**[OPERATOR] Discord check:**
- [ ] `#ops` channel shows no unresolved P0 alerts
- [ ] `/status` command in Discord returns: `NORMAL | live-small | <version-hash>`
- [ ] `/positions` returns: `No open positions` (expected on day 1 before first trade)

**[OPERATOR] Calendar ratification:**
- [ ] Run `/calendar` in Discord to see today's + tomorrow's macro events
- [ ] If any tier-1 macro event today: consider deferring first-day live trading by 1 session (voluntary; not required — system has 5-min-before/30-min-after macro pause built in)
- [ ] Ratify tomorrow's calendar: `/ratify` in Discord; confirm acknowledgement message appears

**Operator state of mind check (not automated — honest self-assessment):**
- [ ] You have read the risk register (§10) and understand that a 15–20% drawdown is expected and planned for
- [ ] You have NOT told any family members or friends you are "going live today" in a way that creates external performance pressure
- [ ] You can handle seeing a day-1 loss of $150–$300 (1% of $15k–$25k) without changing anything

## 13.2 Session Monitoring (17:00–18:30 ET, Day 1 Live)

At 17:00 ET: signal cycle will fire at 17:30 ET. Monitor:

```
17:00 ET:  [OPERATOR] Navigate to https://<your-domain>/ (web Today page)
           Confirm: SSE connection active (no DEGRADED banner)

17:30 ET:  [OPERATOR] Watch Discord #signals channel
           Expected: 1–4 signal embeds appear with [Approve] [Reject] [Defer] buttons
           
17:35 ET:  [OPERATOR] Review each signal
           - Check anomaly badges: if vol_regime_z_high on multiple signals, consider deferring some
           - Click Approve for signals without anomaly badges
           - For anomaly-flagged signals: read anomaly text; decide; use Reject if uncertain (free — can enter again next session)

17:40 ET:  [OPERATOR] Confirm approved signals appear in web Queued Signals section as "approved"
           
18:00 ET:  [OPERATOR] Watch Discord #fills channel
           Expected: fill embeds appear as QC executes at next session open (NOTE: fills typically arrive at next CME session open, not immediately)
           Day 1: fills may not arrive until tomorrow's CME open — this is normal
```

## 13.3 End-of-Day Review (19:00 ET, Day 1 Live)

- [ ] Check Discord `#daily-brief` for end-of-day brief (arrives at ~17:00 ET or 19:00 ET depending on scheduler config)
- [ ] Click `[I'm here]` liveness button — this is required within 24h; do it now on day 1
- [ ] Navigate to `/trades` on web — confirm any fills are visible with correct market/direction/price/P&L
- [ ] Navigate to `/system` → reconciliation status — confirm `PASS` or acknowledge any breaks
- [ ] Run `verify_chain.py` one more time: `docker compose exec api python3 services/audit/verify_chain.py --env live`
- [ ] Emotional debrief: write 2–3 sentences in a physical notebook about how the day felt. Day 1 is almost always uneventful — that is the goal.

## 13.4 What Counts as a Successful Day 1

Day 1 is successful if:
1. The system ran without requiring operator intervention beyond normal signal approval
2. At least one signal was approved (even if the fill doesn't arrive until next session)
3. The audit chain is intact at end of day
4. Operator clicked `[I'm here]` on the daily brief
5. Operator did NOT change any parameters, strategy settings, or code in response to day-1 results

Day 1 is NOT a success if the operator made any code or parameter changes in response to a single session's result. If the temptation arises to "fix" the strategy after one day: log it in the decision diary (tag: `engagement_miss` or `halt_acknowledgement`) and wait at least 5 sessions.

---

# 14. Operational Reference — Common Commands

This section is a fast-access reference for the most common operational tasks. Operator should be able to execute all of these without Claude Code assistance by the end of Phase 0.

## 13.1 VPS Access and Service Management

```bash
# SSH into primary VPS:
ssh operator@<hetzner-ashburn-ip>

# All Docker commands run from /opt/trading/:
cd /opt/trading

# Check all service status:
docker compose ps

# Start all services:
docker compose up -d

# Restart a single service (e.g., api):
docker compose restart api

# Check logs for a service (last 100 lines):
docker compose logs api --tail 100
docker compose logs postgres --tail 100
docker compose logs qc_adapter --tail 100
docker compose logs discord_bot --tail 100

# Follow logs in real time:
docker compose logs api -f

# Check resource usage:
docker stats --no-stream

# Check disk space:
df -h /opt/trading

# Check container health:
docker inspect trading_api_1 | python3 -c "import sys,json; c=json.load(sys.stdin)[0]; print(c['State']['Health']['Status'])"
```

## 13.2 Postgres Access

```bash
# Connect as app_service (read/write except audit_log UPDATE/DELETE):
psql "postgresql://app_service:<password>@localhost:5432/trading"

# Connect as app_owner (schema migrations):
psql "postgresql://app_owner:<password>@localhost:5432/trading"

# Break-glass (retrieve password from paper safe first):
psql "postgresql://dba_breakglass:<printed-password>@localhost:5432/trading"

# Useful quick queries:
psql $DATABASE_URL -c "SELECT kill_switch_state, triggered_reason, triggered_at FROM risk_state LIMIT 1;"
psql $DATABASE_URL -c "SELECT COUNT(*) FROM audit_log;"
psql $DATABASE_URL -c "SELECT * FROM reconciliation_breaks WHERE resolved_at IS NULL ORDER BY detected_at DESC LIMIT 5;"
psql $DATABASE_URL -c "SELECT status, COUNT(*) FROM signals GROUP BY status ORDER BY status;"
psql $DATABASE_URL -c "SELECT * FROM liveness_probes ORDER BY created_at DESC LIMIT 5;"
psql $DATABASE_URL -c "SELECT * FROM vacation_mode WHERE active = true LIMIT 1;"
```

## 13.3 Secrets Management

```bash
# View/edit a secrets file (decrypts for editing, re-encrypts on save):
sops secrets/paper.enc.yaml
sops secrets/live.enc.yaml

# Decrypt to stdout (for scripting — do not pipe to unencrypted files):
sops -d secrets/live.enc.yaml

# Extract a specific value:
sops -d secrets/live.enc.yaml | python3 -c "import sys,yaml; d=yaml.safe_load(sys.stdin); print(d['discord']['bot_token'])"

# After rotating a credential: edit sops file → commit → restart affected service
docker compose restart discord_bot  # after rotating Discord bot token
docker compose restart qc_adapter   # after rotating QC API token
```

## 13.4 Audit Chain Verification

```bash
# Full chain verification (run weekly or after any suspicious event):
docker compose exec api python3 services/audit/verify_chain.py --env paper
docker compose exec api python3 services/audit/verify_chain.py --env live

# Expected output:
# CHAIN OK: 1234 rows verified, 0 broken links, 0 hash mismatches

# If broken:
# CHAIN BROKEN: sequence_no 456 prev_hash mismatch (stored: abc123, computed: def456)
# Action: do NOT attempt manual repair; file incident report; contact Claude Code for root cause

# Check specific event type in chain:
psql $DATABASE_URL -c "
SELECT sequence_no, event_type, occurred_at, record_hash
FROM audit_log
WHERE event_type = 'kill_switch_triggered'
ORDER BY sequence_no DESC LIMIT 10;"
```

## 13.5 Deployment Workflow

```bash
# Standard deploy (after PR merges to main and CI passes):
# CI automatically builds Docker images and pushes to GHCR
# VPS deploy hook receives webhook from GHCR:
# docker compose pull && docker compose up -d
# This happens automatically — operator just monitors

# Verify deploy completed:
docker compose ps   # check image SHA updated (look for :latest or :<sha>)
curl https://<your-domain>/api/health  # confirm new version running

# Manual rollback (Phase 2 web UI not yet available in Phase 1):
# Get previous image SHA from GitHub Packages or GHCR:
# docker pull ghcr.io/<operator>/trading-api:<previous-sha>
# docker compose up -d api   # will use image currently in docker-compose.yml

# Check CI status:
gh run list --repo <operator>/trading-system --limit 5
gh run view <run-id>  # detailed CI output
```

## 13.6 Health Check Quick Commands

```bash
# Full system health via API:
curl -s https://<your-domain>/api/health | python3 -m json.tool

# Health score (requires auth session — use from browser or with cookie):
curl -s -b "session=<cookie-value>" https://<your-domain>/api/health-score | python3 -m json.tool

# QC adapter status (from VPS):
docker compose logs qc_adapter --tail 20 | grep -E "(poll_success|poll_fail|session_count)"

# Reconciliation status:
psql $DATABASE_URL -c "SELECT status, last_run_at FROM reconciliation_status LIMIT 1;" 2>/dev/null || \
  curl -s -b "session=<cookie-value>" https://<your-domain>/api/system/reconciliation-status | python3 -m json.tool

# Watchdog status (from Falkenstein VPS):
ssh root@<falkenstein-ip>
systemctl status trading-watchdog.timer
journalctl -u trading-watchdog.service --since "1 hour ago"
```

## 13.7 Sops Key Rotation (Annual)

Perform this rotation once per year (calendar reminder set). Steps:

```bash
# 1. Generate new age keys:
age-keygen -o ~/new-live-key.txt
age-keygen -o ~/new-paper-key.txt

# 2. Add new private keys to keys.txt:
cat ~/new-live-key.txt >> ~/.config/sops/age/keys.txt
cat ~/new-paper-key.txt >> ~/.config/sops/age/keys.txt

# 3. Update .sops.yaml with new public keys (old keys remain during transition):
sops .sops.yaml  # edit to add new recipient alongside old one

# 4. Re-encrypt secrets files with new keys:
sops updatekeys secrets/live.enc.yaml
sops updatekeys secrets/paper.enc.yaml

# 5. Remove old private keys from keys.txt and old recipients from .sops.yaml

# 6. Print new keys; store in safe; destroy old paper copies

# 7. Commit .sops.yaml changes and re-encrypted .enc.yaml files

# 8. Redeploy VPS to pick up new key (update /etc/credstore.encrypted/age_key):
ssh root@<hetzner-ip>
systemd-creds encrypt --name=age_key < /dev/stdin > /etc/credstore.encrypted/age_key.new
# Paste new AGE-SECRET-KEY-...
mv /etc/credstore.encrypted/age_key.new /etc/credstore.encrypted/age_key
docker compose restart  # picks up new key via sops_init container
```

---

# 15. Service Architecture Quick-Reference

This section summarizes what each service does, how to check if it is healthy, and what to do if it fails. Operator should internalize this table by end of Phase 0.

| Service | What It Does | Healthy log line | Failure symptom | First fix attempt |
|---|---|---|---|---|
| `caddy` | TLS termination, reverse proxy to FastAPI, rate limiting | `serving initial configuration` | `curl https://<domain>/api/health` returns connection refused or 502 | `docker compose restart caddy`; check `deploy/Caddyfile` for syntax errors |
| `api` | FastAPI HTTP + SSE server; all REST endpoints; SSE multiplexer | `Application startup complete` | API endpoints return 502 or timeout | `docker compose restart api`; check logs for Python exception |
| `postgres` | PostgreSQL 16; all tables; audit log with hash chain | `database system is ready to accept connections` | Other services can't connect; `psql` returns connection refused | `docker compose restart postgres`; if data corruption: stop services, restore from S3 backup |
| `signal` | Donchian/MA signal engine; runs at 17:30 ET; produces signals in `signals` table | `daily_cycle_complete session_date=...` | No signals appearing in Discord `#signals` after 17:30 ET | Check `docker compose logs signal`; confirm scheduler fired (`APScheduler job executed`) |
| `risk` | Position sizing Stages 0–5; risk rings; kill-switch state machine | `risk_rings_evaluated margin_util=0.XX` | Risk evaluation not running; signals not sized | `docker compose restart risk`; check `risk_state` table for stuck state |
| `execution` | Phase 1: writes instructions to QC ObjectStore; Phase 2: ib-async direct | `instruction_written order_id=...` | Orders not reaching QC/broker | Check `docker compose logs execution`; verify QC API credentials in sops |
| `reconciliation` | Compares backend positions/cash with broker every 60s during session | `reconciliation_pass position_match=true` | P0 alert in `#alerts`; reconciliation_breaks table has unresolved rows | See RB-002 runbook |
| `audit` | Hash-chain writer; SERIALIZABLE writes to `audit_log` | `audit_write_success sequence_no=NNN` | Audit writes failing; kill switch may trigger (incident_review) | Check Postgres connection; verify `app_service` role has INSERT on `audit_log`; run `verify_chain.py` |
| `qc_adapter` | Polls QC ObjectStore every 60s for events; polls acks every 5s | `poll_success events_ingested=N` | Events not appearing in audit log; reconciliation shows gaps | See RB-004 runbook; check QC API token |
| `discord_bot` | discord.py gateway; slash commands `/positions`, `/halt`, `/status` | `on_ready logged in as TradingBot` | Slash commands not responding | `docker compose restart discord_bot`; check Discord bot token in sops |
| `webhook_pusher` | HTTP webhooks to Discord channels; Resend email for critical alerts | `webhook_delivered channel=#alerts` | Discord messages not arriving | See RB-001 runbook |
| `scheduler` | APScheduler; fires signal cycle 17:30 ET; daily brief 17:00 ET; calendar import 20:00 ET | `job_executed id=signal_cycle` | Signal cycle not firing; daily brief not appearing | `docker compose restart scheduler`; check APScheduler job store in Postgres |
| `monitoring` | Health probes; Prometheus metrics export; watchdog ping confirmer | `health_check_ok` | Watchdog email arrives (backend unreachable) | See RB-007 runbook |
| `agent` | Claude Ops Agent; triggered by events + scheduled + operator queries | `agent_task_complete action=daily_briefing` | No daily briefing; no agent-drafted PRs | Check Anthropic API key in sops; check `agent_actions` table for failed attempts |
| `calibration` | OLS slippage calibration; runs monthly cron | `calibration_complete new_version_id=...` | No `slippage_calibration_versions` row after 30+ live fills | `docker compose restart calibration`; check `fills` table has sufficient data (≥30 rows) |
| `gitea` | GitHub mirror; daily sync; serves as DR backup for source code | `sync_complete repo=trading-system` | Mirror out of sync | `docker compose restart gitea`; force-sync: `docker compose exec gitea gitea admin repo sync` |

---

# 16. Phase 0 Learning Curriculum

The 5–8 hours/week of learning during Phase 0 follows this curriculum. This is not formal study — it is learning-by-doing alongside the build.

## Week 1–2: Foundations

**Python basics (2h):**
- Read the strategy file `strategies/v1_trend_following/` — don't understand every line; understand the structure: class, methods, log calls
- Understand `if __name__ == "__main__":` pattern
- Understand what a `dict`, `list`, and `Decimal` are — these appear in sizing traces

**Git basics (1h):**
- `git log --oneline -20` — view recent commits
- `git status` — what changed?
- `git diff` — what specifically changed?
- `git pull` — get latest code

**Docker basics (2h):**
- `docker compose ps` — what is running?
- `docker compose logs <service> --tail 50` — what happened?
- `docker compose restart <service>` — restart one service
- `docker compose up -d` — start all services in background

## Week 3–4: Logs and Database

**Log reading (2h/week):**
- Learn to distinguish: startup log (happens once at container start), runtime log (happens during operation), error log (contains Traceback or ERROR)
- Learn to find the root cause: when service A fails, often service B is the cause (e.g., `api` fails because `postgres` is not ready)
- Practice: `docker compose logs api 2>&1 | grep -i error`

**Database basics (2h/week):**
- Connect to Postgres: `psql $DATABASE_URL`
- `\dt` — list tables
- `SELECT * FROM risk_state LIMIT 1;` — view current risk state
- `SELECT COUNT(*) FROM audit_log;` — how many audit events?
- `SELECT * FROM signals WHERE status = 'pending' ORDER BY created_at DESC LIMIT 10;` — pending signals

## Week 5–6: Deployment and Auth

**Deployment workflow (2h):**
- Understand the CI/CD path: PR merge → GitHub Actions → Docker build → GHCR push → VPS webhook → `docker compose up -d`
- Practice: create a trivial change (update a comment in any file); open PR; watch CI; watch VPS update

**Auth and security (2h):**
- Understand the sops decrypt path: how secrets get from encrypted files into container environment variables
- Practice: `sops -d secrets/paper.enc.yaml | head -20`
- Understand WebAuthn at a conceptual level: browser generates a key pair; server stores the public key; each login is a signed challenge

## Week 7–8: Operations

**Operational drills (4h):**
- Kill switch round-trip: `/halt test-week7`; confirm HALT_NEW in web; resume via web with re-auth; confirm NORMAL
- Reconciliation check: `psql -c "SELECT * FROM reconciliation_breaks WHERE resolved_at IS NULL;"` — interpret result
- Audit chain check: run `verify_chain.py` and interpret output
- Discord command set: run all Phase 0 commands (`/positions`, `/halt`, `/status`); interpret responses

**Goal by end of Week 8:** Operator can perform all items in the operational competence assessment (§3 Week 8) without Claude Code assistance.

---

# 17. Spec-to-Guide Cross-Reference

Quick lookup: spec section → where this guide addresses it.

| Spec | Section | Guide Coverage |
|---|---|---|
| backend-spec §1.1 | Repository layout | §11 Day 1, `[CLAUDE_CODE]` repo scaffold |
| backend-spec §1.2 | Phase 1 architecture | §7 Component Dependency Graph |
| backend-spec §1.5 | Phase 1→2 cutover checklist | §5 Month 5–6; RB-009 (abort case) |
| backend-spec §2.4.1 | Position sizing Stages 0–5 | §3 Week 2 Day 6; §14 signal service row |
| backend-spec §2.4.3 | Kill-switch state machine | §3 Week 2 Day 7; RB-005; §14 risk service row |
| backend-spec §2.4.5 | Margin protocol | RB-003 |
| backend-spec §2.10.1 | Audit write path | §3 Week 3; §3 Week 4 golden tests |
| backend-spec §2.10.2 | Audit immutability triggers | §3 Week 4 Day 17 (Wednesday) |
| backend-spec §8.1 | Sops + age | §2 tooling; §3 Week 2 Days 8–9; §13.3 |
| backend-spec §8.2 | Postgres role hierarchy | §3 Week 3 migration `0006_roles`; §13.2 |
| backend-spec §8.5.1 | WebAuthn ceremony | §3 Week 6 Tuesday; RB-008 |
| backend-spec §9.1 | VPS specs | §2.1 Hetzner row; §13.1 |
| backend-spec §9.2 | Docker Compose layout | §11 Day 1; §14 all service rows |
| backend-spec §9.4.1 | Routine deploy | §13.5 |
| backend-spec §11.1 | Phase 0 deliverables + success criteria | §3 all weeks; §3 Week 8 pre-flight checklist |
| backend-spec §11.2 | Phase 1 | §4 all months |
| backend-spec §11.3 | Phase 2 | §5 all months |
| backend-spec §11.4 | Phase 3 | §6 all months |
| backend-spec §12 | Claude Ops Agent | §4 Month 3; §8 DP-004; §14 agent service row |
| frontend-spec §2.1 | Phase surface enumeration | §4 Month 2 and Month 3 |
| frontend-spec §5.1 | WebAuthn ceremony | §3 Week 6 Tuesday; RB-008 |
| frontend-spec §5.3 | 8 backup codes | §3 Week 6 Friday |
| frontend-spec §5.4 | TOTP-only reduced privileges | RB-008 |
| frontend-spec §6.1 | Discord surface phasing | §3 Week 6 Thursday |
| frontend-spec §6.2 | Discord channels | §2.1 Discord row; §3 Week 3 Thursday |

---

---

# 18. Phase 0 Paper Session Tracker

Update this table every Friday. The 30-session gate (§3 Week 8) is non-negotiable.

| Week | CME Sessions This Week | Cumulative Total | Notes |
|---|---|---|---|
| Week 1 | | | QC paper clock starts |
| Week 2 | | | |
| Week 3 | | | |
| Week 4 | | | |
| Week 5 | | | |
| Week 6 | | | |
| Week 7 | | | Must reach 30 by end of this week |
| Week 8 | | | Buffer; live trading gate |

A "CME session" counts if the QC algorithm was running and at least one market data event was logged in the QC algorithm logs for that trading day. Sessions that logged no data (e.g., algorithm was stopped) do NOT count.

Minimum pace to hit 30 by end of Week 7: **5 sessions/week** (Mon–Fri CME trading days). If a week has only 4 trading days (US holiday), count only 4.

If cumulative total at end of Week 7 is below 30: Week 8 extends until 30 sessions complete. Live trading defers. Record deviation in §12 update log.

---

# 19. Capital Scaling Timeline

This section documents the expected capital scaling path, contingent on performance milestones. All amounts are approximate; DP-003 governs the actual initial amount.

| Milestone | Trigger Condition | Target Allocation | Action Required |
|---|---|---|---|
| Phase 1 live start (Month 2) | IBKR account approved; Phase 0 complete | $15–25k (DP-003) | Fund IBKR Pro; update `accounts.initial_equity` |
| First scale-up (Month 3–4) | Month 1 ends green (Sharpe > 0); no HALT_NEW; no reconciliation breaks | +$5k (to ~$20–30k) | Capital event via `capital_events` INSERT; m_combined resets session counter for 5 sessions |
| Second scale-up (Month 5–6) | 3+ consecutive green months; max DD < 10%; health score composite ≥ 65 | +$5–10k (to ~$30–35k) | Same capital event process |
| Full initial allocation (Month 6–8) | Track record consistent; Phase 2 live and stable | Up to full $30–35k pool | Do not exceed reserve ($5k remains for infra + buffer) |
| F&F first commit (Month 12+) | ALL F&F gate conditions met (§6 Month 12); legal structure complete | Up to $250k additional | Full DP-011 process; LLC + lawyer memo required |

**Capital event session handling (backend-spec §2.4.4):**
Every capital event (deposit or withdrawal) triggers a 5-session reduced-size period with `m_capital_event = 0.5`. This is automatic. Sessions 6–30 post-event run at full size with mode-active flag. This is NOT a punitive measure — it is a deliberate risk management grace period after equity changes.

---

*End of implementation guide. Update Protocol per §12 when milestones complete.*
