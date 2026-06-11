# Graduation runbook — research → production parameters (design D6)

How a parameter candidate becomes production V1 configuration. First exercised
end-to-end 2026-06-11 (charter PR E dry-run, zero-delta candidate; ledger:
`strategies/v1_trend_following/GRADUATION.md`). Every step (incl. the rollback
path) is mandatory for a VALUE-changing candidate; the dry-run notes where a zero-delta candidate may
cite existing artifacts instead of re-running.

**Governance invariants (do not weaken):**

- A graduation PR touches `strategies/**` → it REQUIRES the
  `risk-review-approved` label, **applied by the operator, never by Claude**
  (charter; dev-guide §2.2 spirit). ⚠️ Note: the mechanical CI gate
  (`.github/workflows/forbidden-paths.yml`) does NOT currently match
  `strategies/` — the label here is charter-enforced, exactly like
  `lean/v1_strategy.py` PRs (#335 precedent). Extending the CI regex to
  `strategies/` is a separate operator decision.
- Strategy-logic ambiguity → escalate (dev-guide §1.3); Claude never decides
  parameter VALUES. Candidates come from operator-approved research questions.
- The backtest harness NEVER tunes in-sample and ships the winner directly:
  candidates pass the P4 anti-overfitting suite (walk-forward OOS +
  multiple-testing haircut, #332) before a PR exists.
- LEAN is the engine of record (design D1): the authoritative candidate run is
  the REAL `lean/v1_strategy.py` (Stage 0-5 sized, explicit PR-#339 costs) in
  the isolated container — never the numpy screen alone.

## The ceremony

0. **Define the candidate** in a research config (sweep allowed at this stage).
   Record the research question + why this candidate (operator-approved scope).
1. **Screen + validity (numpy, fast):** `make research RUN=<cfg>` with a
   `validity:` block — P4 walk-forward (rolling IS/OOS), OOS-ranked sweep,
   multiple-testing haircut (#332). A candidate that fails OOS or loses to the
   buy-and-hold benchmark stops here.
2. **Authoritative candidate run (real engine):** drive the REAL V1 with the
   candidate parameters over the standard acceptance window
   (2023-09-01 → latest snapshot date):
   - refresh the data snapshot (`research/lean/README.md` §1 — read-only COPY);
   - `build_v1_run_spec(data_root, parameters=<candidate>, start=…, end=…)` +
     `run_backtest(..., backend="docker")` — the driver enforces isolation
     (`--network none`, POST stub, dummy bearer, never the live volume).
3. **Baseline run:** same window, production parameters. If code + data are
   unchanged since the recorded authoritative baseline
   (`research/lean/README.md` "Authoritative V1 P&L"), cite it instead of
   re-running — state the run dir + recorded numbers.
4. **Backtest-delta artifact** (the operator gates on this, not the diff):
   table of candidate vs baseline — total return, CAGR, Sharpe, realized vol,
   max-DD, total fees, fills/trades, margin events, ruin flags — plus a
   plain-English read of WHY the delta moves (which mechanism, which markets).
   **Hard stops: any margin event, liquidation, ruin banner, or implausible
   leverage in the candidate run → STOP, report, no PR.**
5. **Trust-bridge check:** cite the latest measured V1↔live decision-match
   (PR #340 methodology; `research/lean/README.md` "Measured trust bridge").
   If the candidate changes SIGNAL logic (not just sizing/exits), re-run
   `reproduce_v1` against the live oracle on the current regime window.
6. **The graduation PR** (`strategies/**`):
   - update the parameter value(s) in
     `strategies/v1_trend_following/parameters.py` (+ every test pinning them);
   - append a row to `strategies/v1_trend_following/GRADUATION.md` (candidate
     table, authoritative numbers, validity verdict, trust-bridge citation);
   - PR body: plain-English summary + risk impact + the step-4 delta table +
     tests. Conventional commit `feat(strategy): …`.
7. **Review:** subagent risk-review (adversarial, live-impact focus) + the
   operator triggers `/ultrareview` (charter-required for `strategies/**`).
   Fold findings; REQUEST-CHANGES blocks.
8. **Operator gate:** review per dev-guide §13, apply `risk-review-approved`
   (operator-applied only), sign off, squash-merge. Claude NEVER merges a
   `strategies/**` PR.
9. **Deploy + DB head update:** `strategies/` ships TWO ways — baked into the
   api image (rebuild api) and volume-mounted read-only into `lean_local`
   (picked up at the next 21:10 UTC restart after `git pull`):
   - VPS: `git pull --ff-only` → `docker compose --env-file deploy/.env build api`
     → `up -d api`;
   - if parameter VALUES changed: update the `parameter_sets` DB head pointer
     (the runtime source of truth — LEAN's nightly fetch reads it) via the
     sanctioned seed tool (`--seed-params-only`, PR #307) or an
     operator-approved UPDATE; verify the new hash on `/system`;
   - ⚠️ the seed tool emits NO audit row (a bare idempotent INSERT — its own
     docstring records "A01 N/A"; `PARAMETER_CHANGE_APPLIED` audit events come
     from the AGENT parameter-change path only). For an operator graduation the
     governance record is the merged PR + the ledger row + the new row's
     `first_active_at` — the same A01-N/A posture as the bootstrap genesis row;
     revisit at live-cutover;
   - close the SUPERSEDED head: the active-row query is `last_active_at IS
     NULL ORDER BY first_active_at DESC`, and the seed tool never retires the
     old row — set the prior head's `last_active_at` (operator-approved
     UPDATE) so exactly one row reads active; `verify_chain --env paper` after.
10. **Post-deploy observation (mandatory):** next 21:30 UTC LEAN cycle —
    heartbeat clean, `parameters_fetch_failed` absent, rejection mix sane vs
    the backtest's expectation; EOD recon clean; `verify_chain` OK. Log the
    observation in the decisions log.
11. **Rollback (if observation fails):** revert the graduation PR (normal PR,
    same label ceremony), restore the prior `parameter_sets` head (re-open its
    `last_active_at`, retire the new row), redeploy, re-observe the next
    cycle, and record the abort in the ledger row + decisions log.

## Dry-run provenance (2026-06-11)

The pipeline was first traversed with a deliberately SAFE candidate —
re-affirming `V1_DEFAULTS` exactly (zero behavioral delta, documentation-only
`strategies/**` change) — to prove the path before any value-changing
graduation: research artifacts cited (authoritative acceptance
`lean_20260611T042002Z`, +3.42% / 9.1% vol / fees $223.03; trust bridge
PR #340), ledger row appended, `strategies/**` PR opened, subagent-reviewed,
then HANDED to the operator unmerged for the label + sign-off. Steps 9-10
collapse to "no deploy effect" for a docs-only record (api rebuild optional;
no DB head change).
