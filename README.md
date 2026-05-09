# Trading System

A solo-operator algorithmic trading system. Multi-asset systematic trend-following on micro futures + bond ETFs. Built over 12 months across four phases (Phase 0 = 8-week foundation; Phase 1 = live track record on QuantConnect; Phase 2 = custom infrastructure migration; Phase 3 = capital scaling).

## Build status

| Phase | Window | Status |
|---|---|---|
| Phase 0 — foundation | Weeks 0–8 | 🔄 Week 2 closing — Days 1-7 ✅, Days 8-9 [CLAUDE_CODE] chain ✅ early via PR #28 |
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

## Days 1-10 status — Phase 0 Week 2 closing; Week 3 entered

Phase 0 Week 1 complete (verification gate 3/3 closed). Week 2 daily tasks complete; Days 8-9 [CLAUDE_CODE] chain landed early via PR #28. Week 2 verification gate: 2 of 3 boxes checked (sub-universe ≥4 at $20k after DP-002; sops decrypt verified; **IBKR Pro pending — DP-001 window opens Mon 2026-05-11**). Week 3 verification gate: 3 of 4 boxes checked at Day 8 (api/health TLS curl, audit_log migration applied, immutability triggers installed); the alerts-pipeline Discord webhook test stays open until `services/webhook_pusher/` ships (Week 3 Thu IG task).

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
