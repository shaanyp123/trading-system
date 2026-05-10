# Trading System

A solo-operator algorithmic trading system. Multi-asset systematic trend-following on micro futures + bond ETFs. Built over 12 months across four phases (Phase 0 = 8-week foundation; Phase 1 = live track record on QuantConnect; Phase 2 = custom infrastructure migration; Phase 3 = capital scaling).

## Build status

| Phase | Window | Status |
|---|---|---|
| Phase 0 — foundation | Weeks 0–8 | 🔄 Week 4 in progress — Days 1-12 ✅, IG §11 Days 8-9 [CLAUDE_CODE] chain ✅ early via PR #28; Week 3 alerts pipeline ✅ via PR #44; Week 4 golden tests ✅ via PR #45; **Week 3 gate 4/4** (Day 11 carryover #2 via PRs #47 + #48 — Discord webhook smoke `status=ok http=204`, embed confirmed in `#alerts`); webhook_pusher service container shipped Phase-1-early (PR #50) — api stays internal-only at all times per backend-spec §8.11; Week 4 gate 1/3 with code in for box 2 (`services/audit/verify_chain.py` CLI shipped Day 12 PR #51, `risk-review-approved`; gate flips to `[x]` after operator runs `deploy/audit/README.md` Step 3 on Ashburn); concurrency test Day 14 to follow |
| Phase 1 — QC live | Months 2–5 | ⏳ Not started |
| Phase 2 — direct IBKR | Months 5–9 | ⏳ Not started |
| Phase 3 — capital scaling | Months 9–12 | ⏳ Not started |

**Day 1 close (2026-05-05):** all account/infra prereqs done. IBKR submitted (account `U25655583`, $1 funded for priority review). Domain `spratcapital.com` (Cloudflare). Hetzner servers `trading-primary` (Ashburn CCX13) + `trading-watchdog` (Nuremberg CX23 — Falkenstein had no capacity). GitHub repo `trading-system` scaffolded (PR #1 merged), branch protection live on `main`. Cloudflare DNS apex+www → 178.156.239.84. Ashburn VPS bootstrapped (Docker, trading user, UFW). QuantConnect org "SPRAT Capital" on Researcher tier ($60/mo).

**Day 2 close (2026-05-05):** v1 trend-following strategy skeleton authored (Donchian + MA + Hurst R/S + ATR stop) with 16 unit tests passing, mypy --strict clean, parameters + Phase 1 sub-universe locked (PRs #4 + parameter-lock follow-up). GitHub App `trading-system-pr-review` created (App ID `3615825`, Installation ID `129868686`); private key in 1Password. Sops + age keys generated for dev/paper/live; `.sops.yaml` populated; paper backups in safe; `SOPS_AGE_KEY_FILE` workaround documented (sops 3.12 macOS). Discord server + 7 channels + bot created. DNS propagation verified. CI tightened to include typecheck + test (PR #8). Day 3 09:00 sops setup encrypts the captured Day 2 values into `secrets/{dev,paper,live}.enc.yaml`.

Concrete deviations from spec (Hetzner DC, QC pricing, GitHub Pro, sops macOS env-var, GitHub App UI not API, Hurst R/S choice, sub-universe lock, HURST_THRESHOLD = 0.55): see [`Docs/decisions-log.md`](Docs/decisions-log.md).

Code now lives in `strategies/v1_trend_following/` (real strategy code) and `lean/v1_qc_algorithm.py` (QC LEAN wrapper skeleton). Operator runbooks for the ongoing operational surfaces live in `deploy/{github-app,discord,sops}/`. Other top-level dirs (`apps/`, `services/`, `packages/`, `alembic/`, `infrastructure/`, `watchdog/`) are scaffolded only and fill in over Phase 0 Days 3–8.

---

## Repo layout

```
/
├── README.md                          # This file — start here
├── CLAUDE.md                          # Orientation for Claude Code sessions (auto-discovered)
├── implementation-guide.md            # Operator's daily handbook ← OPEN THIS DAILY
├── Makefile                           # make lint / typecheck / test / ci / all
├── pyproject.toml                     # Python 3.11 + ruff/mypy/pytest config
├── docker-compose.yml                 # 19-service stack; phase2 profile gates ib_gateway + lean_local
├── .github/workflows/ci.yml           # ruff + gitleaks (typecheck/test/docker-build commented until code lands)
├── .github/CODEOWNERS                 # forbidden-whitelist enforcement sentinel
├── .gitleaks.toml                     # secret-scan rules + allowlist
│
├── Docs/                              # Reference documentation
│   ├── backend-spec.md                # Backend architecture (~4800 lines)
│   ├── frontend-spec.md               # Frontend architecture (~4900 lines)
│   ├── claude-dev-guide.md            # Coding patterns + anti-patterns (read by Claude Code)
│   └── decisions-log.md               # Decisions and deviations from specs (append-only)
│
├── apps/web/                          # Next.js frontend (scaffold only; built Week 6)
├── services/                          # 13 Python services (audit, risk, signal, execution, ...) — see services/README.md
├── infrastructure/                    # retry, broker_reconnect, logging
├── strategies/v1_trend_following/     # v1 Donchian/MA strategy (built Day 2+)
├── lean/                              # QC LEAN config (built Day 4)
├── watchdog/                          # External watchdog Python script (deployed Day 4)
├── packages/                          # api-types, discord-types
├── alembic/versions/                  # DB migrations (built Week 3)
├── deploy/                            # Caddyfile + .env.example
├── secrets/                           # sops-encrypted *.enc.yaml only (plaintext blocked by .gitignore)
├── scripts/                           # helper scripts
├── tests/                             # unit / integration / golden / e2e
│
├── Prompts/                           # Generation prompts (archived; reproducibility)
└── Archive/                           # Reserved for superseded versions
```

---

## How to use each living document

### `implementation-guide.md` (operator's daily handbook)

**You open this every day.** It's your step-by-step working handbook through the 12-month build.

| Section | When to read it |
|---|---|
| §1 Conventions | Once, day 1 |
| §2 Pre-Phase-0 setup | Once, before week 1 starts |
| §3 Phase 0 weekly plan | At start of each Phase 0 week |
| §4 Phase 1 monthly plan | At start of each Phase 1 month |
| §5 Phase 2 milestones | When approaching Phase 2 |
| §6 Phase 3 milestones | When approaching Phase 3 |
| §7 Component dependency graph | When sequencing tasks |
| §8 Decision-Point Register | When facing a decision (cutover date, scale-up, decommission, etc.) |
| §9 Operational runbook | When something breaks at 2 AM |
| §10 Risk register | Quarterly review; before each phase transition |
| §11 First 2 weeks | **Day 1 — start here** |
| §12 Update protocol | Whenever you complete a task or hit a new scenario |

**Living-doc updates:** check off verification gates as you complete them; add new runbook entries as you encounter scenarios; log decisions in §8 with timestamp and rationale.

### `Docs/backend-spec.md` (architecture reference)

**You read this when you need to understand a specific subsystem.** Don't read end-to-end; use Grep/search.

Common reasons to open:
- "How does the audit log immutability work?" → §3 (schemas) + §8 (security)
- "What's the canonical SSE event format?" → §4.2
- "What are the exact risk-ring numbers?" → §2.4
- "What's the Phase 1 → Phase 2 cutover procedure?" → §1.2 + §11

**Living-doc updates:** specs change rarely. Architectural changes go through PR review (claude-dev-guide §11 anti-pattern A02 enforces this).

### `Docs/frontend-spec.md` (architecture reference)

Same pattern as backend-spec. Reach for it when you need:
- A page's behavior or data dependencies → §2 screen-by-screen
- An SSE event payload shape → §4
- A locked design token → §3 design tokens
- Discord bot phasing → §6

### `Docs/claude-dev-guide.md` (coding patterns)

**Claude Code reads this at the start of every coding session.** You read it weekly to stay oriented on patterns and to spot-check that Claude Code is following them.

Common reasons you (the operator) open it:
- Reviewing a PR: §13 operator review checklist
- Spot-checking a pattern: §5 canonical implementations
- Verifying an anti-pattern was avoided: §11 forbidden list
- Understanding session protocol: §1

**Living-doc updates:** new canonical pattern → add to §5; new anti-pattern from postmortem → add to §11; tooling change → §3 or §4.

### `Docs/decisions-log.md` (decisions + deviations)

**Append-only log of where reality differs from the specs.** Each entry: date, topic, spec reference, what spec said, what we actually did, rationale, cost/scope impact.

**You open it when:**
- Reading a spec value (cost, hardware, vendor, pricing) and want to know if it's still current
- A future session wants to understand "why is X done this way when the spec says Y?"
- Annual review of how the build diverged from the original plan

**Update protocol:** every session that makes a non-trivial decision adds an entry. Entries are append-only — if a decision is reversed, add a new entry referencing the old one.

---

## Days 1-12 status — Phase 0 Week 4 in progress

Phase 0 Week 1 complete (verification gate 3/3 closed). Week 2 daily tasks complete; IG §11 Days 8-9 [CLAUDE_CODE] chain landed early via PR #28. Week 2 verification gate: 2 of 3 boxes checked (sub-universe ≥4 at $20k after DP-002; sops decrypt verified; **IBKR Pro DP-001 PENDING — TODAY Wed 2026-05-13 is the last day of the trigger window; no email yet as of Day 12 09:00; operator escalation to alternate broker is the contingency if no approval by 23:59 ET**). Week 3 verification gate: 4 of 4 boxes closed (api/health TLS curl, audit_log migration applied, immutability triggers installed, Discord webhook smoke executed Day 11 carryover #2 via PRs #47 + #48). Week 3 "Bonus shipped" surface: `services/audit/writer.py` (PR #39, Day 8), `alembic/versions/2026-05-09_qc_adapter_cursor_seed.py` (PR #40, Day 8), `services/reconciliation/recon.py` (PR #42, Day 9). Week 4 verification gate: **1 of 3 boxes checked with code in for box 2** — box 1 closed via PR #45 Day 11 (`tests/golden/` 18 tests covering byte-for-byte `record_hash` parity, chain composition, parser identity, modulo-three audit-metadata invariant); box 2 code shipped via PR #51 Day 12 (`services/audit/verify_chain.py` CLI + `deploy/audit/README.md` operator runbook; gate flips to `[x]` after operator runs the runbook smoke on Ashburn); box 3 binds on Day 14's concurrency test in a separate forbidden-whitelist PR.

Per-day one-liners (full detail in [`Docs/decisions-log.md`](Docs/decisions-log.md)):

- **Day 1 (2026-05-05):** account/infra prereqs — IBKR `U25655583` submitted, Hetzner Ashburn (`trading-primary`) + Nuremberg (`trading-watchdog`), Cloudflare DNS for `spratcapital.com`, GitHub repo + branch protection, QC Researcher tier ($60/mo). PR #1; decisions-log Day 1 entries (9).
- **Day 2 (2026-05-05):** v1 trend-following strategy skeleton (Donchian + MA + Hurst R/S + ATR stop) + 16 unit tests + parameter lock + Phase 1 sub-universe locked + GitHub App `trading-system-pr-review` (App ID `3615825`) + Discord guild + 7 channels + sops/age keys generated. PRs #4 + #8; decisions-log Day 2 entries (10) + verdict.
- **Day 3 (2026-05-05):** Alembic migrations `0001_audit_log` → `0006_roles` (audit_log + core + risk + ops tables, partitioned by year, immutability triggers, role grants); sops `{dev,paper,live}.enc.yaml` encrypted with placeholders; `risk-review-approved` label + `forbidden-paths` CI gate live. PRs #9-13; decisions-log Day 3 entries (6).
- **Day 4 (2026-05-06):** QC live-broker = QC Paper / brokerage MODEL = IBKR Margin; LEAN parameter map; **paper-day clock STARTED**; external watchdog operational on Hetzner Nuremberg (stdlib-only); QC Python API migrated PascalCase → snake_case. PRs #15-23; decisions-log Day 4 entries (12).
- **Day 5 (2026-05-07):** FastAPI skeleton deployed to Ashburn (loopback healthy, structlog wired); docker-compose Day-5 subset (`phase1` profile gates non-Day-5 services); single-shot bringup script; dev-guide §6.8 platform smoke-test rule + A27 anti-pattern codified. PRs #24-27; decisions-log Day 5 entries (7).
- **Days 6-9 [CLAUDE_CODE] chain (2026-05-07 — landed early):** `services/risk/sizing.py` (Stage 0 universe filter) + `services/risk/state_machine.py` (kill-switch); `services/audit/decision_diary.py`; `services/scheduler/vacation.py` + `calendar_import.py`. Pure-policy modules; spec wins on every IG deviation. PR #28 (`risk-review-approved` label); decisions-log entry 2026-05-07 Day 6-9 chain.
- **Day 6 carryover (2026-05-08 morning):** TLS verified end-to-end (Week 1 gate fully closed); `paper.enc.yaml` filled and committed from VPS; bootstrap setup token captured to 1Password; laptop sops `exec format error` fixed; watchdog email-storm fix (apex/subdomain mismatch); QC `self.log()` confirmed working in live UI; backend stays on Discord (no Resend migration). PRs #29-32; decisions-log Day 6 carryover entries (8).
- **Day 7 (2026-05-08):** sub-universe verification — **DP-002 invoked, $15k → $20k initial capital** (≥4 markets PASS at $20k); kill-switch state machine verbal walkthrough (operator learning); Day 7 entry doc-closure + Week 1 gate. PRs #31, #33; decisions-log Day 7 entries (sub-universe + DP-002 + kill-switch walkthrough).
- **Day 8 (2026-05-09):** canonical audit-log writer shipped — `services/audit/writer.py` (`pg_advisory_xact_lock` + SERIALIZABLE + 5-attempt SQLSTATE-40001 retry + SHA-256 hash chain) + `event_types.py` (locked taxonomy mirror of §3.30) + `models.py` + `chain.py` (in-tree JCS canonicalizer; Decimal-aware; A05 float rejection); 22 chain unit tests + 4 testcontainers writer tests. First **operational** alembic migration `2026-05-09_qc_adapter_cursor_seed.py` (defensive idempotent re-seed). Three discoveries documented: (1) asyncpg's `SerializationError` is wrapped as generic `DBAPIError` not `OperationalError`; (2) `pg_advisory_xact_lock` is itself the snapshot-taking statement under SERIALIZABLE so retries are inherent to the spec'd pattern; (3) `0004_ops_tables.py` already inserts the §3.19 cursor rows. Day 8 calendar mapping: operator's actual cadence drifted ~1 day from IG nominal — today's substance is IG Week 3 Mon work in IG terms. PRs #39 (`risk-review-approved`) + #40 (`risk-review-approved`); decisions-log Day 8 entries (calendar mapping + 09:00 + 10:00) + Day 8 verdict.
- **Day 9 (2026-05-10):** `services/reconciliation/recon.py` shipped — pure-policy `plan_reconciliation_check` per backend-spec §2.6 + §3.15. Locked tolerances (position qty = 0, cash = max($5 abs, 1 bps × equity_baseline), dividend ex-date 2× widening). T+1 grace via `prior_breaks` input (data-based match on `(metric, market, delta)` tuple — substantive interpretation locked vs dev-guide §5.5's time-based reference). Emits 3 audit event types (`reconciliation_check_passed` / `_break_detected` / `_break_resolved`) — all values in `services.audit.event_types.AuditEventType`. 45 unit tests across 8 `Test*` classes; A22 enforced (zero audit writes from tests). Same plan-then-apply shape as PR #28 + PR #37 (state_machine, sizing, vacation, calendar_import, qc_adapter/poll). Day 9 calendar mapping continues Day 8 lock — operator's Day 9 substance is IG Week 3 Wed work; IG §11 Day 9 [CLAUDE_CODE] tasks (decision_diary + vacation + calendar_import) already shipped Day 7 via PR #28; IG §11 Day 9 14:00 [OPERATOR] sops workflow learning is open optional practice. PR #42 (`risk-review-approved`); decisions-log Day 9 entries (calendar mapping + 09:00) + Day 9 verdict.
- **Day 10 (2026-05-11):** `services/webhook_pusher/` alerts pipeline shipped — pure-policy `plan_alert_dispatch` planner + async `post_outbound_message` sender (explicit User-Agent per Day 4 PR #21 lesson; HTTP status → `DeliveryStatus` enum; single 429 retry with Discord JSON `retry_after` body preferred over header) + `dispatch_alert` orchestrator (SELECT alerts row → short-circuit on existing `delivery_status` for idempotency → fan out via sender → UPDATE JSONB) + operator CLI smoke. Severity routing locked: P0 → `{#alerts, #critical, email}`; P1/P2 → `#alerts`. `AlertCategory` enum mirrors alembic 0004 `alert_category` (29 values, A04 binds). 58 unit tests across 14 `Test*` classes (respx-mocked HTTP; A22 binds: zero live Discord/Resend hits). A27 satisfied via `deploy/webhook_pusher/README.md` 8-step operator runbook (same shape as `deploy/api/README.md` Day 5 + `watchdog/README.md` Day 4 + `lean/README.md` Day 4). `services/webhook_pusher/**` is on neither §2.2 nor §2.3 whitelist (regular PR review; no `risk-review-approved` label). Day 10 calendar mapping continues Day 8 + Day 9 lock — operator's Day 10 substance is IG Week 3 Thu work. PR #44; decisions-log Day 10 entries (calendar mapping + 09:00) + Day 10 verdict; Week 3 gate box 4 closure binds on operator runbook smoke (Steps 3+5+6 on Ashburn).
- **Day 11 (2026-05-12):** `tests/golden/` QC adapter parity suite shipped — 18 tests across 5 `Test*` classes covering byte-for-byte `record_hash` for the 5 representative QC session events (`signal_emitted`, `order_filled`, `reconciliation_check_passed`, `kill_switch_triggered`, `system_stopped` — locked taxonomy from `services/audit/event_types.py`, NOT IG's casual prose; deviation cross-linked in decisions-log "Day 11 (PR #45)" section). Per-fixture expected hex baked into module-level constants (drift in `chain.jcs_serialize` OR `chain.compute_record_hash` trips even with correct fixture read); chain-walk test asserts baked-in tail hex; round-trip test pins parser-is-payload-identity contract via `services.qc_adapter.payloads.parse_jsonl_record`; modulo-three test recursively walks payload to assert `{ingest_clock_ts, ingest_uuid, sequence_no}` audit-side metadata is absent. A22 enforced (pure Python, zero `audit_log` INSERTs, zero testcontainers, zero mocking); A27 does NOT bind (no third-party platform contract). 412/412 tests green (was 394 + 18 new). `tests/golden/**` is on neither whitelist (regular PR review). Day 11 calendar mapping continues Day 8-10 lock — operator's Day 11 substance is IG Week 4 Mon work; IG §11 Day 11 nominal had no [CLAUDE_CODE] tasks (Sat cushion + Week 1 close-out review, both already covered). PR #45; decisions-log Day 11 entries (calendar mapping + 09:00 + Day 10 smoke status check + DP-001 status check) + Day 11 verdict; closes IG §3 Week 4 verification gate box 1.
- **Day 12 (2026-05-13):** `services/audit/verify_chain.py` operator-facing CLI shipped — argparse + asyncio.run + `DATABASE_URL` env-var shell over the canonical `services.audit.chain.verify_chain` async walker (Day 8 PR #39). Walker's return signature extended `tuple[bool, int | None]` → `tuple[bool, int | None, int]` (rows-walked count is single source of truth for the runbook's `<N>`; no race vs concurrent writers). Output contract locked: `CHAIN OK: <N> rows verified` exit 0 / `CHAIN BREAK at sequence_no=<X> (after <K> verified rows)` exit 1 / usage error exit 2. Same plan-then-apply shape as `services/reconciliation/recon.py` and `services/risk/state_machine.py`. 21 new tests across 3 surfaces (5 walker via fake-session stub + 16 CLI via monkeypatched `_verify` + 2 testcontainers integration); 433/433 tests green. A27 BINDS — CLI invoked against the real Ashburn DB at deploy time — satisfied via `deploy/audit/README.md` 5-step operator runbook (same shape as `deploy/api/README.md` Day 5 + `watchdog/README.md` Day 4 + `deploy/webhook_pusher/README.md` Day 10/11). `services/audit/**` is on dev-guide §2.2 forbidden whitelist; PR carries `risk-review-approved` label. Day 12 calendar mapping continues Day 8-11 lock — IG §11 has no Day 12 entry (§11 covers Days 1-10 only); operator's Day 12 substance is IG **Week 4 Tue** work. **DP-001 final-day check: TODAY 2026-05-13 is last day of trigger window; no IBKR email as of Day 12 09:00; operator escalation to alternate broker is the 23:59 ET contingency.** PR #51 (`risk-review-approved`); decisions-log Day 12 entries (calendar mapping + 09:00 + DP-001 final-day check) + Day 12 verdict; closes IG §3 Week 4 verification gate box 2 once operator runs `deploy/audit/README.md` Step 3 on Ashburn (bundled with prerequisite VPS git pull + image rebuild).

The original Day-1-Monday-priority-list is preserved in `implementation-guide.md` §11 as the canonical template; this section's old "How to start" walkthrough was Day-1-specific and is now historical.

Open ONE Claude Code session at a time. Each new session: it auto-reads `CLAUDE.md`, which points it at `Docs/claude-dev-guide.md` §1 (Session Protocol) and `Docs/decisions-log.md`. Tell it which `implementation-guide.md` section you're working on. It does the implementation work; you review.

---

## Phase 0 living-doc backlog (week 1 housekeeping; from final tandem audit)

Three small navigability improvements to fold into Day 1–7. Not blockers; do them while waiting on IBKR account approval:

1. **Canonicalize "PR review surface" naming** to "operator-friendly PR review surface" (find/replace across all 4 living docs; ~5 min)
2. **Add `(backend-spec §X.Y)` backlinks to each Decision-Point Register row** in `implementation-guide.md` §8 (~16 entries; 10–15 min). Important for incident response — at 2 AM you don't want to search.
3. **Add canonical-source pointer** to `Docs/backend-spec.md` §11.1 and `Docs/claude-dev-guide.md` §10.1 noting that `implementation-guide.md` §3 is canonical for Phase 0 schedule (2 small edits)

---

## Generation provenance

This documentation was generated through 6+ rounds of cold-read critique on each prompt before generation, then cross-checked pairwise (specs vs each other; dev guide vs specs), then audited in tandem (all four documents together). Final tandem audit verdict: **SHIP** with three minor navigability items noted above.

The generation prompts are archived in `Prompts/` for reproducibility — if any document needs full regeneration, the prompt that produced it is preserved.

---

## When to bring questions back to a Claude conversation

The artifacts handle ~95% of build-time questions. Bring back to a fresh Claude conversation only when:
- You hit a genuinely architectural question the docs don't cover
- A risk register entry materializes (drawdown, family-money tension, etc.) and you want strategic counsel
- You're approaching a major phase transition (Phase 1 → 2 cutover, Phase 3 family-money decision)

For routine implementation work: Claude Code session + `CLAUDE.md` + `implementation-guide.md` is sufficient.
