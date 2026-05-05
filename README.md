# Trading System

A solo-operator algorithmic trading system. Multi-asset systematic trend-following on micro futures + bond ETFs. Built over 12 months across four phases (Phase 0 = 8-week foundation; Phase 1 = live track record on QuantConnect; Phase 2 = custom infrastructure migration; Phase 3 = capital scaling).

This repo currently contains **documentation only**. Code lands during Phase 0 (week 1+).

---

## Repo layout

```
/
├── README.md                          # This file — start here
├── CLAUDE.md                          # Orientation for Claude Code sessions (auto-discovered)
├── implementation-guide.md            # Operator's daily handbook ← OPEN THIS DAILY
│
├── Docs/                              # Reference documentation
│   ├── backend-spec.md                # Backend architecture (~4500 lines)
│   ├── frontend-spec.md               # Frontend architecture (~4900 lines)
│   └── claude-dev-guide.md            # Coding patterns + anti-patterns (read by Claude Code)
│
├── Prompts/                           # Generation prompts (archived; reproducibility)
│   ├── prompt-a-backend-spec.md
│   ├── prompt-b-frontend-spec.md
│   ├── prompt-c-implementation-guide.md
│   ├── prompt-d-claude-dev-guide.md
│   └── prompt-e-tandem-review.md
│
└── Archive/                           # Reserved for superseded versions
```

When code lands in Phase 0 week 1, additional top-level directories will appear: `apps/`, `services/`, `packages/`, `alembic/`, `deploy/`, `secrets/`, `scripts/`, `tests/`.

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

---

## How to start (Day 1, Monday)

Pre-Day-1: spend 30 min reading **`implementation-guide.md` §1, §2, §11**. That's it. Don't try to read everything.

Then execute `implementation-guide.md` §11 day-by-day. Day 1 actions in priority order:

1. **08:00** — Open IBKR Pro account application ([interactivebrokers.com](https://www.interactivebrokers.com/en/index.php?f=4969)). 1–2 week turnaround. **Critical path** — start first.
2. **09:00** — Register apex domain at Cloudflare or Namecheap (~$10–15/yr). This becomes `<your-domain>` everywhere in the docs.
3. **10:00** — Provision Hetzner Cloud accounts: Ashburn primary VPS (CCX13 ~$25/mo) + Falkenstein watchdog (CX11 ~$5/mo).
4. **11:00** — Initialize GitHub repo + create GitHub App for in-app PR review surface.
5. **Afternoon** — Create QuantConnect organization (fresh org; Quant Researcher tier $20/mo).
6. **End of day** — Check off Day 1 verification gate in `implementation-guide.md` §11.

Open ONE Claude Code session at a time. Each new session: it auto-reads `CLAUDE.md`, which points it at `Docs/claude-dev-guide.md` §1 (Session Protocol). Tell it which `implementation-guide.md` section you're working on. It does the implementation work; you review.

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
