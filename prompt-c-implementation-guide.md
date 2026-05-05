# IMPLEMENTATION GUIDE GENERATOR

## ROLE

You are a senior delivery manager / technical program manager with experience shipping production trading systems at small CTAs and prop shops. You have:
- Run multiple Phase 0 → live-trading transitions
- Coordinated solo-operator + AI-pair-programmer builds
- Shipped systems that survived their first drawdown without operator panic
- Authored runbooks that on-call engineers actually use

You will produce a comprehensive **implementation guide** that the operator uses as a working handbook through Phase 3 (12 months). This is NOT another spec — it is the build sequence + decision register + runbook + risk plan, derived from the specs that already exist.

## INPUT MATERIALS

Two complete technical specifications at:
- `/Users/shaanpatel/Documents/GitHub/Trading/backend-spec.md` (~4500 lines)
- `/Users/shaanpatel/Documents/GitHub/Trading/frontend-spec.md` (~4900 lines)

Read targeted sections of these as needed. You don't need to read 9500 lines word-for-word — use Grep + targeted Read calls to find specific decisions, schemas, phase tables, and constraints.

## OPERATOR PROFILE (binding context for sequencing)

- Solo operator, US-based (NJ), finance background (3 years banking, BS finance, SIE only)
- **No coding ability.** Implementation is via Claude Code as pair-programmer. Operator's role is operational competence (read logs, deploy, verify, decide), NOT authoring strategy or system code.
- $30–35k total capital pool; $15–25k initial live trading; rest in reserve for infra + buffer
- Will add up to $250k of family capital after 12+ months of clean live track record AND legal structure (LLC + securities lawyer consult)
- Goal: 6–12 month live track record that qualifies for prop firm allocation OR first F&F commit
- 50 hard-cognitive hours/week available; 5–8 hrs/week of those for the first 8 weeks goes to **operational learning** (Python basics, cloud, git, Docker, log reading) — NOT to authoring code
- Trades alone for the first 12 months; no second operator
- Strategy: multi-asset systematic trend-following on micro futures + bond ETFs
- Path C: live track record on QuantConnect months 1–4, custom backend Phase 2+

## OUTPUT

Write the implementation guide to a NEW file at `/Users/shaanpatel/Documents/GitHub/Trading/implementation-guide.md`. Produce a complete, dense, working handbook covering ALL sections below.

**Length target: 2000–3500 lines.** Favor concrete tasks over prose. Every Phase 0 day should have an action; every Phase 0 week should have a verification gate.

## REQUIRED SECTIONS

### 1. Document Conventions
- How tasks are formatted: `[OPERATOR]`, `[CLAUDE_CODE]`, `[BOTH]` task tags
- Verification gate format: explicit "done means" criteria for each phase
- Decision-point format: condition → choice → action register
- Update protocol: this is a living document; flag when to update what

### 2. Pre-Phase-0 Setup Checklist (before week 1 begins)

A flat checklist of accounts and tooling needed before Phase 0 starts. Group by:
- **Accounts to open** (each with: provider, why needed, tier/plan, cost, expected turnaround time, blocker-status flag)
  - Domain registrar (Cloudflare or Namecheap; ~$10–15/yr; same-day)
  - IBKR Pro (futures + Level 2 options; ~1–2 weeks turnaround — START EARLIEST)
  - QuantConnect (fresh organization; same-day; $20/mo Quant Researcher tier)
  - Hetzner Cloud (Ashburn primary VPS + Falkenstein watchdog; same-day; ~$25–45/mo combined)
  - Resend (email backup; free tier sufficient initially; same-day)
  - Discord (free; create server; same-day)
  - GitHub (personal account + GitHub App for in-app PR review surface; same-day)
  - Sentry (free tier; same-day)
  - S3 or Backblaze B2 (encrypted backups; ~$1–3/mo; same-day)
- **Tooling on operator's laptop** (Docker Desktop, Python 3.11+, git, `sops`, `age`, Claude Code, IBKR's TWS for Phase 0 paper inspection)
- **Cost commitment verification** (sum of monthly fixed: $80–320/mo; soft alert ceiling $200; hard alert $300)
- **Critical path** items (start IBKR Pro application Day 1; everything else can wait week 1)

### 3. Phase 0 Week-by-Week Plan (weeks 0–8)

For each week, produce:

```
## Week N — <theme>

**Goals (2–4 bullets):**
**Critical path:**
**Daily tasks:**
- Mon: …
- Tue: …
- Wed: …
- Thu: …
- Fri: …
**Verification gate (end of week N):**
- [ ] <concrete observable: e.g., "domain DNS resolves to Hetzner VPS IP"; "first paper trade executed on QC and audit event ingested into local Postgres">
**Risks this week:**
**If blocked:** <escalation path>
```

Use the spec's locked Phase 0 timeline:
- **Week 1:** Apex domain registered. IBKR Pro application submitted. Hetzner VPS provisioned (Ashburn). QC fresh organization created + paper paper trading kicks off (paper-day clock starts). Repo scaffolded. v1 strategy code authoring begins (Claude Code authors; operator reviews).
- **Week 2:** Phase 1 sub-universe verified (QC bundled-data executability + 50% single-contract-notional rule). v1 strategy committed and trading on QC paper. Sops setup with age key + paper backup in fireproof safe.
- **Week 3:** QC ObjectStore audit adapter scaffolded (writes events with monotonic sequence_no + JCS canonical). Backend skeleton begins (FastAPI, Postgres 16, Alembic migrations for `audit_log` table with hash chain).
- **Week 4:** QC adapter golden-test parity verified (byte-for-byte identical records modulo `{ingest_clock_ts, ingest_uuid, sequence_no}`). Backend audit-log immutability enforced (Postgres triggers + EVENT TRIGGER for TRUNCATE + REVOKE). Concurrency tested (advisory lock + SERIALIZABLE retry).
- **Week 5:** Custom backend Phase 0 surface ships: REST scaffolding for Phase 1 endpoints, SSE channel `/api/sse/events`. Caddy reverse proxy configured.
- **Week 6:** Frontend Phase 0 scaffolding ships: Next.js app, WebAuthn registration flow on `/setup`, Today page rendering against mock data, Discord bot skeleton with `/positions` and `/halt`.
- **Week 7:** Frontend integrates with backend live data (post-mock). End-to-end signal-to-fill round trip tested in paper. Phase 1 surfaces complete per per-page table.
- **Week 8:** Buffer + Phase 1 handover. 30 CME paper sessions verified complete. Operator passes operational competence assessment (can deploy, restart, read logs, invoke kill switch). Live trading begins month 2.

For each week, include verification gates that are mechanically testable (curl, grep, log line, etc.), not just "I think it works."

### 4. Phase 1 Milestone Plan (months 2–5)

Less granular than Phase 0 (weekly ↔ monthly here). Cover:
- **Month 2:** Live trading begins on QC; live-small environment tag; daily liveness probe ack expected; first reconciliation-break possible on dividend ex-dates
- **Month 3:** First slippage recalibration (monthly cadence); first weekly vectorbt-vs-LEAN parity check; first PR review surface walkthrough (agent drafts a parameter PR; operator reviews via in-app surface)
- **Month 4:** Mid-Phase-1 review checkpoint (Sharpe trajectory; drawdown profile; signal acceptance rate); decision point: continue / pivot / pause
- **Month 5:** Phase 2 cutover preparation begins; pre-cutover automated checklist run; cutover date selected (≥5 CME sessions advance)

### 5. Phase 2 Milestone Plan (months 5–9)

Cover the cutover, custom infra hardening, ib-async integration, second-strategy preparation (vol carry on SPX defined-risk), capacity scaling.

### 6. Phase 3 Milestone Plan (months 9–12)

Capital scaling. Family-money legal structure prep (LLC + securities lawyer consult). Prop firm vs. RIA decision point. CPA reader role enrollment if family money lands.

### 7. Component Dependency Graph

Mermaid diagram showing build order. What blocks what. Examples:
- Domain → Caddy → HTTPS → WebAuthn enrollment → operator access
- IBKR Pro account → QC connection → paper trading → audit ingestion
- Audit log schema + JCS canonicalization → QC adapter → golden test
- Frontend Today page → SSE channel + REST `/api/today/digest` → backend in place

### 8. Decision-Point Register

Table of decisions the operator MUST make at specific milestones. For each: trigger condition → choice → action → audit log entry needed.

Examples:
- Phase 1 → 2 cutover date selection (mid-month 5; ≥5 CME sessions advance; if blocked by HALT_NEW state in 24h prior, defer)
- Live size scale-up after first clean month (if month 1 ends green health score + no HALT_NEW: scale from $5k to $10k initial → $15k → up to allocated capital)
- Decommission floor trigger (auto-halt + human review; operator decides resume vs. retire vs. new strategy version)
- Family money acceptance gate (month 12+ contingent on: clean track record + LLC + lawyer consult + Sharpe ≥ 1.5 over 9–12 months + max DD ≤ 15%)
- 475(f) tax election (CPA consultation gate; operator + CPA decide before filing)
- Vacation mode use (operator's discretion; spec already locks the mechanics)
- Parameter range expansion proposal (operator decides whether to widen agent-mutable range; PR-required)

### 9. Operational Runbook Excerpts

Common scenarios with step-by-step responses. Each:
- Symptom (what operator sees)
- Likely cause
- Verification (how to confirm cause)
- Resolution (specific commands, Discord commands, web actions)
- Escalation (when to escalate; to whom — likely just "page yourself with reflection time")

Cover at minimum:
- Discord delivery failing for >10 min
- Reconciliation break detected
- Margin auto-trim invoked
- QC ObjectStore poll failing 5–9 min then >10 min
- Vol regime detector trip (HALT_NEW)
- Heartbeat engagement timeout (defensive risk envelope)
- Backend unreachable (external watchdog email arrives)
- WebAuthn enrollment failure on first browser
- Decommission floor triggered
- IBKR margin call (broker-mandated liquidation outside system control)
- Operator vacation start/end procedure

### 10. Risk Register

Top 8–12 failure modes with: probability (low/med/high), impact (low/med/high/catastrophic), mitigation, monitoring signal.

Cover at minimum:
- Trend-follow drawdown of 15–20% in months 4–8 (HIGH probability; documented expectation; mitigation: hard-coded -20% trailing kill; family money education in advance)
- WebAuthn enrollment fails on first browser (MED prob; mitigation: TOTP fallback + backup codes; setup flow tolerates)
- QC adapter audit gap during Phase 1 live (MED prob; mitigation: gap-detection + repair flow; weekly golden test)
- Operator psychological burnout from a real drawdown (MED-HIGH prob; mitigation: pre-commit risk thresholds; weekly check-ins; vacation mode)
- Family money pressure during a drawdown month 8 (MED prob; mitigation: explicit advance communication of expected DD profile; documented patience window)
- IBKR account opening rejected or delayed >2 weeks (LOW prob; impact: blocks Phase 0 week 2+; mitigation: apply Day 1; prepare alternate broker plan only if 2-week mark misses)
- Cutover failure (LOW prob; impact: HIGH; mitigation: pre-cutover automated checklist + abort conditions)
- VPS catastrophic failure (LOW prob; impact: HIGH; mitigation: external watchdog + IBKR phone desk + Gitea mirror restore)
- Decommission floor triggered during family-money window (MED prob; mitigation: explicit operator override path; new strategy version starts 30-day paper)
- Tax surprise at year-end (MED prob; mitigation: 1099-B reconciliation pass after Feb 15; CPA consultation Phase 1)

### 11. Plan of Action — First 2 Weeks

The most concrete part. Day-by-day, including specific commands and URLs.

```
## Day 1 (Monday, Week 1)

[OPERATOR] 08:00 — Open IBKR Pro account application: https://www.interactivebrokers.com/en/index.php?f=4969
  - Account type: Individual; market data subscriptions: defer to TWS install
  - Funding: $25k initial deposit (do not fund yet — wait for approval)
[OPERATOR] 09:00 — Register domain at Cloudflare or Namecheap: <your-domain>
  - Cost: ~$10–15/year
  - Configure DNS pointing to placeholder
[CLAUDE_CODE] 10:00 — Initialize repo at github.com/<operator>/trading-system
  - Create branch protection on main: CI pass required
  - Create GitHub App for in-app PR review surface
…
```

Continue through Day 5 of Week 1, then Week 2 in slightly less detail.

### 12. Update Protocol

How the operator maintains this document during the build:
- Phase 0 weeks: update verification gates with actual completion dates; flag any deviations
- Phase 1+: update Decision-Point Register with each decision made (timestamp + rationale + audit reference)
- Risk Register: update with new identified risks; mark mitigated risks closed
- Runbook: add new scenarios as they're encountered; resolved scenarios get postmortem links

## FORMAT REQUIREMENTS

- Markdown with clear section headers
- Use task tags `[OPERATOR]`, `[CLAUDE_CODE]`, `[BOTH]` consistently
- Verification gates as explicit checkboxes
- Mermaid for the dependency graph
- Concrete commands, URLs, and references — no "configure your tooling appropriately"
- Reference specs by section number when the guide assumes spec content (e.g., "per backend-spec §4.1.5")
- Length: 2000–3500 lines; favor density and concreteness over completeness of every edge case

## CONSTRAINTS

- Do NOT redefine architectural decisions already locked in the specs — reference them
- Do NOT introduce new tooling not present in the specs
- Do NOT propose new components not present in the specs
- Do NOT generate placeholder content like "[FILL IN HERE]"; either commit to a default or omit
- Where operator must decide something, surface it explicitly in the Decision-Point Register, not as embedded `[QUESTION FOR OPERATOR]` flags
- The guide is for ONE operator on a SINGLE-OPERATOR system; do not reference team workflows
- Phase 0 weekly verification gates must be MECHANICALLY TESTABLE (a curl, a grep, a log line, a Discord command output) — not subjective

## DELIVERABLE

Write the complete implementation guide to `/Users/shaanpatel/Documents/GitHub/Trading/implementation-guide.md`. After writing, return a single-paragraph summary of (a) what was produced, (b) any sections that came up shorter than expected and why, and (c) any specs gaps the implementation guide forced you to flag.

Begin.
