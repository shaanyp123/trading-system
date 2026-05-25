---
description: Run the canonical pre-PR checklist before submitting a PR for operator review
argument-hint: (no args — operates on current branch)
allowed-tools: [Bash, Read, Glob, Grep]
---

# Pre-PR checklist

Run this BEFORE opening a PR for operator review. The checklist verifies the work matches the project's PR-submission contract per `Docs/claude-dev-guide.md` §1 + §5.7, including risk-path label requirements, test status, and audit-trail completeness.

## Steps

Run all checks in parallel (independent), then assemble a structured checklist that the operator can copy into the PR description.

### 1. Branch + commit status

Run in parallel:
```
git status --short
git log --oneline origin/main..HEAD
git diff origin/main...HEAD --name-only
git rev-parse --abbrev-ref HEAD
```

Capture:
- Current branch name
- Number of commits ahead of `main`
- List of changed files
- Any uncommitted work (warn if present)

### 2. Risk-path label requirement

For each changed file, check whether it matches the A02 forbidden-without-label whitelist:
- `services/risk/**`
- `services/signal/**`
- `services/audit/**`
- `services/execution/**`
- `services/reconciliation/**`
- `services/calibration/**`
- `services/agent/decisions/**`
- `services/agent/risk_actions/**`
- `services/agent/parameter_changes/**`
- `services/agent/prompts/decision/**`
- `alembic/**`

If ANY changed file matches: output **"⚠️ This PR requires the `risk-review-approved` label"** with the list of matched files. Reference `Docs/claude-dev-guide.md` §11 [A02].

### 3. Hot-fix-whitelist annotation

For each changed file, check whether it's on the hot-fix whitelist (`services/api/**`, `services/data/**`, `strategies/**`, `lean/**`, `watchdog/**`, `scripts/operator_tools/**`, `.github/**`, `deploy/**`). If yes: note "hot-fix scope; no `risk-review-approved` label required" for transparency in the PR description.

### 4. Test status

Run `make ci` (or its components individually if make is unavailable):
- `make lint` — ruff + dep-drift-check + gitleaks
- `make typecheck` — mypy --strict
- `make test` — pytest --cov
- `make frontend-test` — pnpm typecheck + lint + build

Report each as ☑ pass / ☐ fail / ⊘ skipped. **A PR MUST NOT be opened if any check fails per dev-guide §1 test-before-commit rule.**

### 5. Docs audit-trail check

Check whether the changes warrant any of:
- New entry in `Docs/decisions-log.md` (if this PR deviates from a spec OR introduces a new canonical pattern)
- Updated row in `Docs/file-index.md` (if this PR creates a new tracked file in `services/`, `lean/`, `apps/`, `scripts/`, `deploy/`, `tests/`)
- Updated `Docs/recent-architecture-changes.md` (if this PR is part of an architecture pivot)
- New entry in `Docs/claude-setup-overhaul.md` (if this PR is a meta-tooling change)

Output: ☑ done / ☐ needed (with which doc) / ⊘ not applicable.

### 6. Backtest delta (if strategy/risk paths touched)

If the PR touches `strategies/**` or `services/risk/sizing.py` or any signal-affecting code, the operator gates on a backtest delta. Note: "Backtest delta required — operator-side validation step before merge."

### 7. ultrareview reminder (if risk-review-approved required)

Per `Docs/claude-setup-overhaul.md` WS#6: PRs carrying `risk-review-approved` must run `/ultrareview` before merge. If this PR is on that path, add a final line: "**Reminder: run `/ultrareview` before merge.**"

## Output format

Emit a checklist block the operator can paste verbatim into the PR description:

```
## Pre-PR checklist

- ☑/☐ Tests pass (`make ci`)
- ☑/☐ Risk-review-approved label required: yes/no (matched files: ...)
- ☑/☐ Hot-fix whitelist scope: yes/no
- ☑/☐ decisions-log entry: done/needed/N/A
- ☑/☐ file-index updated: done/needed/N/A
- ☑/☐ recent-architecture-changes updated: done/needed/N/A
- ☑/☐ backtest delta: included/required/N/A
- ☑/☐ ultrareview required before merge: yes/no
```

Then a short plain-English summary the operator can use as the PR description: what changed, why, risk impact, what to look for in review.

## Notes

- This command runs LOCALLY (no SSH to VPS needed). Operator runs it from the project root after committing changes locally.
- The actual PR submission is via `gh pr create` once the checklist is green.
- If any check fails or warrants caveats, surface them prominently — don't bury them.
