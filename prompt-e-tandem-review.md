# TANDEM REVIEW — Final Cross-Document Audit

## ROLE

You are a senior technical program manager doing the final integration audit on a documentation package before a 12-month build kicks off. Four artifacts have been built sequentially and verified pairwise. Your job: verify they hold together as a SET. You are NOT auditing any individual document — that work is done. You are auditing the seams between them.

This is the last review before implementation begins. Your verdict gates the start of Phase 0.

## INPUT MATERIALS

Four documents at `/Users/shaanpatel/Documents/GitHub/Trading/`:

1. **`backend-spec.md`** (~4500 lines) — what the backend system is (architecture, schemas, APIs, risk framework)
2. **`frontend-spec.md`** (~4900 lines) — what the frontend system is (pages, components, real-time, auth)
3. **`implementation-guide.md`** (~2022 lines) — when to build each piece, who does what (operator vs Claude Code), Phase 0 weekly plan, decision-point register, runbook
4. **`claude-dev-guide.md`** (~2750 lines) — how Claude Code writes code (canonical patterns, conventions, anti-patterns, session protocol)

## CONTEXT — what's already been verified (don't re-do)

- Each prompt went through 6 rounds of cold-read critique before its document was generated
- Specs A and B were cross-checked against each other; ~20 contract mismatches were found and patched
- A final ship-clean cold-read on patched specs found 2 trivial substantive items, both patched
- Dev guide was cross-checked against specs and implementation guide; SHIP WITH MINOR PATCHES, 2 load-bearing items patched (Phase 0 weekly schedule alignment + new §1.5 Locked Decisions Quick Reference)
- Implementation guide was reviewed; 3 minor gaps flagged and accepted (cutover via DB INSERT, self-referential breakglass contact, manual watchdog IP capture)

**Do NOT re-litigate individual documents.** Only flag ISSUES THAT SPAN 3+ DOCUMENTS or that arise specifically from the relationship between documents.

## YOUR MANDATE — strict scope

Verify the four documents hold together as a working SET that an operator + Claude Code can use through 12 months of build. Specifically check:

### 1. Vocabulary consistency across 3+ documents

For each of these terms, verify the same meaning is used everywhere it appears:
- "active universe" / "Phase 1 sub-universe" / "active markets"
- "CME session" / "CME RTH" / "CME 23-hour Globex"
- "paper days" / "CME paper sessions" / "30 trading days"
- "live-small" vs "live small" (formatting consistency)
- "in-app PR review surface" / "operator-friendly PR review surface" / "PR review surface"
- "kill-switch" / "kill switch" / "HALT_NEW"
- "audit_log" / "audit log" / "audit chain"
- "JCS" / "JSON Canonicalization Scheme" / "RFC 8785"
- "QC" / "QuantConnect" / "QC ObjectStore"
- "Phase 1 backend has no direct IBKR connection" — appears in all 4 docs?

If terms are used inconsistently or with shifting meaning, that's a finding.

### 2. Phase boundary and timeline alignment

Verify these all agree:
- Backend Phase 0 = weeks 0–8, Phase 1 = months 2–5, Phase 2 = months 5–9, Phase 3 = months 9–12
- Frontend Phase 0 = weeks 0–3 scaffolding, Phase 1 surfaces ship at backend week 8 / month 2 boundary
- Implementation guide §3 weeks 1–8 schedule
- Dev guide §10.1 weeks 1–8 schedule (was patched to align)
- "Live trading begins" — same week/month boundary in all 4?
- "Cutover" date selection (≥5 CME sessions advance) — same in all docs?

### 3. Numbers and locked values

Spot-check 10–15 key numeric values appear consistently across 3+ documents:
- Risk rings: 25% / 300% / 150% / cluster caps 60/80/80/40/30
- Vol target: 14% portfolio annualized
- Re-auth window: 5 minutes
- Session: 30 min idle / 24h absolute / 7d refresh
- Tab limit: N=4
- SSE replay buffer: 24h backend retention
- Backup codes: 8 codes, 10-char base32, 2 groups of 5
- Paper minimum: 30 CME sessions
- Phase 1 instruction round-trip: ~20s p99
- Phase 2 kill-switch SLO: ≤5s
- Margin protocol: 70% warn, 85% auto-trim, hard cap -30% gross/session
- Capital event: ≥5% deposit triggers 30-session mode
- Decommission floor: 30-day Sharpe < 0 OR max DD ≤ -25% OR 60-day Sharpe < backtest by > 2 SD

If a number appears as 14% in one doc and 15% in another, that's a finding.

### 4. Cross-references between documents

The documents reference each other. Verify references resolve:
- Does `implementation-guide.md` reference backend spec section numbers that still exist post-patches?
- Does `claude-dev-guide.md` §5 patterns match backend spec §3 schemas?
- Does `claude-dev-guide.md` §10.1 weeks match `implementation-guide.md` §3 weeks?
- Does `frontend-spec.md` API contract reference backend spec endpoints that exist?
- Spot-check 5–8 explicit cross-references; verify destination still matches reference.

### 5. Anti-pattern consistency

Anti-patterns appear in two places: dev guide §11 and as locked-decisions / forbidden whitelists in specs. Verify:
- Forbidden file path whitelist in dev guide matches backend spec
- "Don't blend environments" rule appears in: backend spec, frontend spec, dev guide
- "Don't use bcrypt / use Argon2id" — consistent
- "Don't use SES / use Resend" — consistent
- "Don't introduce new event types without enum migration" — does this rule exist consistently?

### 6. Decision-point alignment

Implementation guide §8 has a Decision-Point Register. Verify each decision-point references:
- The spec section that describes the trigger condition
- The spec section that describes the resulting state change
- The dev guide pattern (if applicable) that handles the audit log entry

If implementation guide says "decommission floor triggered → operator decides resume vs retire," verify that backend spec §3.X actually defines the decommission workflow and dev guide §5.X has the canonical state-transition pattern.

### 7. Operator mental model viability

Stand back and ask: can a non-coding operator hold all four documents simultaneously without confusion? Specifically:
- Are the document roles clearly delineated (what / when / how)?
- Does the operator know which doc to read for a given question?
- Are there topics covered in multiple docs that should be covered in only one (canonical source)?
- Are there topics covered in zero docs that the operator will need?

This is the most subjective check. Limit to 2–3 findings.

## HOW TO AUDIT EFFICIENTLY

You have 4 documents totaling ~14,000 lines. You will NOT read them end-to-end. Use Grep heavily:
- Grep for specific terms across all 4 files in parallel to verify vocabulary consistency
- Grep for specific numbers (e.g., `\b14%\|14 percent\b`) across all 4 files
- Grep for cross-reference patterns (e.g., `§4\.1\|backend-spec.md §`)
- Read targeted sections only when grep results suggest divergence

Time budget: **30 minutes**. Do NOT exceed 45.

## OUTPUT FORMAT

```
# Tandem Review Final Audit

## VERDICT
[ONE OF: SHIP | SHIP WITH MINOR FIXES | DO NOT SHIP]

## Section 1 — Vocabulary consistency
PASS / list of inconsistencies (max 5)

## Section 2 — Phase + timeline alignment
PASS / divergences

## Section 3 — Numbers and locked values
PASS / divergences

## Section 4 — Cross-reference integrity
PASS / broken references (with line numbers)

## Section 5 — Anti-pattern consistency
PASS / inconsistencies

## Section 6 — Decision-point alignment
PASS / gaps

## Section 7 — Operator mental model viability
2–3 brief findings

## Critical findings (would gate ship)
None / list

## Non-critical findings (can defer to Phase 0 living-doc updates)
List or "None"

## Final recommendation
One paragraph: ship / patch first / specifics
```

## HARD RULES

- Do NOT propose new architectural ideas
- Do NOT find issues to deliver value; if the documents hold together, say so
- Do NOT re-audit individual documents — only cross-document seams
- Do NOT exceed 5 findings per section; if more, you're miscategorizing
- Length cap: 1500 words for the report
- If a finding is a single-document issue (already-audited individual doc), skip it — that's not your scope
- Findings must reference 3+ documents OR specifically arise from the relationship between 2 documents

## YOUR DELIVERABLE

A concise report with the verdict and any cross-document issues that must be patched before the 12-month build begins. If the package holds together, the verdict is SHIP and the operator starts Phase 0 Day 1.

Begin.
