# Parameter-Sets Bootstrap + Strategy-Decommission Lever — Design

> **Status: DESIGN — SIGNED OFF 2026-05-29 (operator delegated to recommended
> answers; cutover-plan lock kept). No code in this doc; implementation is a
> future session (start at PR-A).**
> Authored 2026-05-29. Mirrors the structure of `Docs/exit-pipeline-design.md`
> and `Docs/signal-proximity-design.md`.
>
> **One-line problem:** the `STRATEGY_DECOMMISSIONED` kill-switch and the
> §10.3 decommission smoke (`Docs/exit-pipeline-design.md`) assume an operator
> can "flip the flag via parameter UPDATE." There is **no `parameter_sets` row
> to UPDATE** (the table is empty), and — the bigger finding — the **canonical
> nightly signal generator (LEAN) does not read `parameter_sets` at all.** It
> reads its parameter map from `lean/lean.json`, which does not even contain the
> `STRATEGY_DECOMMISSIONED` key. So the lever is doubly inert: no DB row for the
> tools that *do* read the DB, and the real nightly cycle reads a config file
> that omits the key entirely.
>
> **Headline recommendation:** Option **(c)** — seed paper's baseline
> `parameter_sets` head row via the existing **operator-script** path
> (`scripts/operator_tools/bootstrap_live_account.py`), NOT an alembic migration.
> Option (a) (alembic seed migration) **conflicts with an existing LOCKED
> decision** (`Docs/live-money-cutover-plan.md` A2 / O7: "operator-script
> preferred; no schema change"). But seeding alone is **necessary-but-not-
> sufficient**: it unblocks the smoke via the `trigger_v1_cycle` path and fixes
> the `/system` UI, yet does **not** make the nightly LEAN kill-switch work. That
> gap is escalated as **Q3** and must be resolved before "decommission" can be
> claimed as a real operational lever.

---

## 0. Motivation

The exit-pipeline work (PRs #251/#252/#253, merged + deployed to paper
2026-05-27) shipped `STRATEGY_DECOMMISSIONED` as an operator-only kill-switch:
when `True`, the entry pipeline short-circuits with a `STRATEGY_DECOMMISSIONED`
rejection and the exit pipeline emits an `exit_reason='decommission'` CLOSE for
every held position (`strategies/v1_trend_following/strategy.py:299,523`).

`Docs/exit-pipeline-design.md` §10.3 step 3 ("Force decommission test") is the
operational smoke that proves this lever end-to-end:

> temporarily set `strategy_decommissioned=True` via parameter UPDATE; observe
> one `signal_emitted` audit row with `signal_type='exit'`,
> `exit_reason='decommission'` for /M2K; operator approves; observe bracket-stop
> cancel + close place; verify `TRADE_CLOSED`. Then revert
> `strategy_decommissioned=False`.

That smoke **cannot run today.** This design scopes the minimal, locked-
decision-consistent fix to unblock it, and surfaces a more important latent gap
(the nightly cycle's kill-switch is not wired to anything an operator can flip
at runtime).

> **Memory correction (logged):** the memory note
> `project_parameter_sets_empty.md` claimed that seeding a `parameter_sets` row
> "switches the entire system (incl. the nightly 21:30 UTC LEAN cycle + EOD
> recon) from code-defaults to DB-driven and stamps `parameter_set_hash` on
> every subsequent signal/order." **Verified false** (see §1): the nightly LEAN
> cycle reads `lean.json` (not the DB), EOD recon does not read `parameter_sets`
> at all, and the `parameter_set_hash` on signals is derived per-payload by the
> api (`services/qc_adapter/signal_ingestion.py:349`), independent of the table.
> The true blast radius of seeding is much smaller — see §2.

---

## 1. Verified Current State (the facts this design rests on)

All verified by reading source on 2026-05-29 unless noted. Line references are
to `main` at the session's HEAD.

| # | Fact | Evidence |
|---|---|---|
| F1 | `parameter_sets` is **empty (0 rows)** in the paper DB. | Per `project_parameter_sets_empty.md`, verified against the paper `postgres` container 2026-05-29. **Not re-verifiable from this workstation** (prod DB lives on the VPS) — see Q6: re-confirm as execution step 0. |
| F2 | The **nightly LEAN cycle reads params from `self.get_parameter()`** → LEAN's parameter map → `lean/lean.json` `parameters` block, with `V1_PARAMETER_DEFAULTS` as the in-code fallback for missing keys. **It never reads the DB.** | `lean/v1_strategy.py:203-210` (`initialize`), `:846-859` (`_get_v1_parameters` builds from `self._params`). |
| F3 | `lean/lean.json` `parameters` block **omits `STRATEGY_DECOMMISSIONED` and `EXIT_AUTO_APPROVE` entirely.** So the nightly cycle always coerces them to the SAFE default `False`. | `lean/lean.json:17-29` (11 keys, no decommission key); `lean/v1_strategy.py:157` fallback `"STRATEGY_DECOMMISSIONED": "False"` + `_coerce_bool_param` (`:180-194`). |
| F4 | The **operator off-schedule tool `trigger_v1_cycle.py` reads the DB** via `fetch_active_parameters_dict` → `SELECT parameters FROM parameter_sets WHERE last_active_at IS NULL ORDER BY first_active_at DESC LIMIT 1`, then `build_v1_parameters_from_dict` (which falls back to `default_v1_parameters()` on an empty dict). | `scripts/operator_tools/trigger_v1_cycle.py:1205-1226`, `:835-857`, `:1403/1425`. |
| F5 | `trigger_v1_cycle` has **no CLI override** for `STRATEGY_DECOMMISSIONED` (or any param). Flags are `--env`, `--session-date`, `--dry-run`, `--data-root`, `--api-url`, `--http-timeout-seconds`, `--allow-non-paper`, `--exits-only/--entries-only`, `--reason-filter`. `--reason-filter=decommission` only *filters which exit_reasons emit*; it does not *set* the flag. | `scripts/operator_tools/trigger_v1_cycle.py:304-418`. |
| F6 | The **`/system` risk-envelope UI also reads `parameter_sets`** (same head-pointer query) and falls back to spec defaults when empty. | `services/api/repos/phase1.py:618-641`, `services/api/routes/system.py:29-32`, decisions-log "Partial parameter_sets row falls back to spec defaults". |
| F7 | The **signal `parameter_set_hash` is derived per-payload** by the api: `parameter_set_hash=_derive_parameter_set_hash(payload)` = `sha256(jcs(payload))`. It is **not** sourced from the `parameter_sets` head row. Seeding the table does **not** change what is stamped on signals/orders. | `services/qc_adapter/signal_ingestion.py:349,516-524`; corroborated by decisions-log: "`parameter_sets` (the table) is distinct from `parameter_set_hash` (the column other tables carry)." |
| F8 | **No sync mechanism exists** that pushes `parameter_sets.parameters` → `lean.json`. (grep for `lean.json`/`get_parameter`/`set_parameter` across `services/` + `scripts/` returns nothing.) The `v1_strategy.py:139-141` comment claiming an operator UPDATE "propagates into the daily LEAN cycle" is **aspirational/misleading** — propagation would require a manual `lean.json` edit + `lean_local` restart. | grep; `lean/v1_strategy.py:139-141`. |
| F9 | The **canonical hash module was never built.** `strategies/v1_trend_following/parameters.py:152-153` says the hash is computed "by `services/version/composite_hash.py`" — **that file and the `services/version/` directory do not exist.** The only hashing primitive available is `services.audit.chain.jcs_serialize`. | `find` (no `services/version/`); grep. |
| F10 | An **operator-script INSERT path for `parameter_sets` already exists**: `bootstrap_live_account.py::_insert_parameter_set_idempotent` (`INSERT ... ON CONFLICT (parameter_set_hash) DO NOTHING`). It takes a JSON file `{parameter_set_hash, parameters}` and validates the hash is 64 hex chars **but does not compute or verify the hash against the content.** | `scripts/operator_tools/bootstrap_live_account.py:374-403,252-276`. |
| F11 | The live-cutover plan **already chose operator-script over migration** and **already chose "copy paper's head"** as live's provenance — which silently *assumes paper has a head row to copy*. | `Docs/live-money-cutover-plan.md` A2 (line 487: "Operator-script preferred (no schema change)"), O7 (line 522), §10 step 18 (line 405). |
| F12 | `parameter_sets` DDL: `parameter_set_hash CHAR(64) PRIMARY KEY, parameters JSONB NOT NULL, first_active_at TIMESTAMPTZ NOT NULL, last_active_at TIMESTAMPTZ`. **No partial-unique index on `last_active_at IS NULL`** — multiple "active" rows would silently coexist; the head query relies on `ORDER BY first_active_at DESC LIMIT 1` to disambiguate. | `alembic/versions/0003_risk_tables.py:81-90`. |
| F13 | backend-spec §3.11 defines the hash as **"SHA-256 over JCS({param_name: value} for params in Parameter Ranges Table only, alphabetized by name)."** "Parameter Ranges Table only" is the **agent-tunable ranges** — ambiguous whether the operator-only `STRATEGY_DECOMMISSIONED`/`EXIT_AUTO_APPROVE` flags (added 2026-05-26, after the spec) are part of the hash input. | `Docs/backend-spec.md:1371-1378`. |

> **Distinct concept — do not conflate.** `strategy_versions.decommissioned`
> (a BOOLEAN column at `alembic/versions/0003_risk_tables.py:49`) retires an
> entire *strategy version*. That is a **separate** mechanism from the
> `STRATEGY_DECOMMISSIONED` *parameter flag* this design addresses. This doc is
> only about the parameter flag (the exit-pipeline kill-switch).

---

## 2. The Core Problem: two parameter sources, only one is DB-driven

```
                    ┌───────────────────────── reads ─────────────────────────┐
                    │                                                          │
  ┌─────────────────────────────┐                          ┌──────────────────────────────┐
  │ NIGHTLY LEAN CYCLE (21:30Z)  │                          │ parameter_sets (DB)  [EMPTY]  │
  │ lean/v1_strategy.py          │                          │ head = last_active_at IS NULL  │
  │ self.get_parameter(...)      │                          └──────────────────────────────┘
  │   ↓ source: lean/lean.json   │                                   ▲              ▲
  │   ↓ fallback: V1_PARAMETER_   │                                   │ reads        │ reads
  │     DEFAULTS (code constant) │                            ┌───────┴──────┐ ┌─────┴────────┐
  │ STRATEGY_DECOMMISSIONED key   │                            │ trigger_v1_  │ │ /system risk- │
  │   NOT in lean.json → False    │   ← canonical signal gen   │ cycle.py     │ │ envelope UI   │
  └─────────────────────────────┘                            │ (off-sched)  │ │ (read-only)   │
              │                                                └──────────────┘ └──────────────┘
              │ emits signal_emitted                                   │
              ▼                                                        ▼
  ┌────────────────────────────────────────────────────────────────────────────┐
  │ api signal_ingestion → parameter_set_hash = sha256(jcs(PAYLOAD))             │
  │ (derived per-payload; NOT read from the parameter_sets head row)            │
  └────────────────────────────────────────────────────────────────────────────┘
```

**True consumers of the `parameter_sets` head row:** only (1) `trigger_v1_cycle`
(signal generation when the operator runs it manually) and (2) the `/system`
risk-envelope display. **Non-consumers:** the nightly LEAN cycle, the api signal
`parameter_set_hash` stamp, and EOD reconciliation.

**Consequence for the smoke:** §10.3 step 3 is satisfiable **only via the
`trigger_v1_cycle` path** (the "controlled" operator-driven run), because that is
the only signal-emitting path that reads the flag from a place an operator can
change at runtime. The smoke does **not** and **cannot** exercise the nightly
LEAN kill-switch as the system is wired today (F2/F3/F8).

---

## 3. The runtime question, answered

The operator asked: *does decommission take effect via a DB read or a code
constant?* The answer is **both, depending on the path** — which is exactly why
option (b) is only partially viable:

| Signal path | Source of `STRATEGY_DECOMMISSIONED` | DB read? | Operator can flip at runtime? |
|---|---|---|---|
| **Nightly LEAN (canonical, 21:30Z)** | `lean.json` param map → `V1_PARAMETER_DEFAULTS` fallback | **No** | Only by editing `lean.json` + restarting `lean_local` (no key present today) |
| **`trigger_v1_cycle` (off-schedule)** | DB `parameter_sets` head → `default_v1_parameters()` fallback | **Yes** | Only if a row exists to UPDATE (none today) + no CLI override |
| **`/system` UI (display only)** | DB `parameter_sets` head → spec defaults | **Yes** | Display only; does not emit signals |

So:
- The **nightly cycle's** decommission lever is effectively a **code/config
  constant** (`lean.json`). → Option (b) ("operate on code defaults") is the
  *de-facto current model* for the nightly cycle, but the operator has no
  runtime lever because the key isn't in `lean.json`.
- The **`trigger_v1_cycle` path's** lever is a **DB read**. → Option (a)/(c)
  (seed/UPDATE a row) is the only way to drive it, and that is the path the
  §10.3 smoke targets.

---

## 4. Option analysis + recommendation

### (a) Alembic seed migration that INSERTs the V1 `parameter_sets` row
- **Mechanics:** a new `alembic/versions/*.py` that INSERTs a baseline row.
- **Forbidden-path:** `alembic/**` requires the `risk-review-approved` label
  (dev-guide §2.2 / `[A02]`).
- **Conflicts with a LOCKED decision (F11).** `Docs/live-money-cutover-plan.md`
  A2 explicitly chose **operator-script, no schema change** for exactly this
  INSERT; O7 chose "copy paper's head." A seed migration also bakes
  environment/identity data into schema (the same anti-coupling argument
  `bootstrap_live_account.py:26-29` makes).
- **Verdict: REJECT.** Contrary to an existing locked decision; higher review
  cost; wrong layer for env-specific data.

### (b) Teach the decommission lever to operate on code-side defaults (no DB row)
- **For the nightly cycle:** already the de-facto model — add
  `"STRATEGY_DECOMMISSIONED": "True"` to `lean/lean.json` + restart `lean_local`.
  Viable, but `lean.json` is the **only** runtime lever and it is a redeploy-
  grade change, not a quick toggle.
- **For the smoke (`trigger_v1_cycle`):** would require **adding a CLI override**
  (e.g. `--set-param STRATEGY_DECOMMISSIONED=true` or a dedicated
  `--decommissioned` flag) so the smoke needs no DB row. This is a
  `scripts/operator_tools/**` change (NOT forbidden-path), small and surgical.
- **Downside:** leaves `parameter_sets` empty, so the `/system` UI keeps showing
  defaults and the live-cutover "copy paper's head" step (F11) stays blocked.
  Narrow fix; defers the root cause.
- **Verdict: VIABLE as a complement, not the primary fix.** Best as the
  mechanism for Q3 (nightly lever) and/or a convenience override, not as the way
  to unblock the smoke.

### (c) Operator-script seed of the baseline `parameter_sets` head row  ← RECOMMENDED
- **Mechanics:** mint the baseline V1 row (`parameters` = the canonical V1
  defaults, `STRATEGY_DECOMMISSIONED=False`) and INSERT it via the **existing**
  `bootstrap_live_account.py` path run with `--env paper` (or a thin paper-seed
  sibling — see Q5). Then the §10.3 smoke flips the flag with an in-place
  `UPDATE ... jsonb_set(...)` (or an event-sourced row swap — see Q2), runs
  `trigger_v1_cycle --reason-filter=decommission --no-dry-run`, then reverts.
- **Why it wins:**
  - Aligns with the **LOCKED** operator-script decision (F11). No forbidden-path
    label; regular PR review (`bootstrap_live_account.py:31-36`).
  - Makes the §10.3 smoke runnable **as literally written** ("via parameter
    UPDATE").
  - Fixes the `/system` risk-envelope UI (F6) to show real values.
  - **Unblocks the live-cutover dependency:** O7's "copy paper's head" needs
    paper to *have* a head; (c) creates it (F11).
  - **Low blast radius** (F7/F2): does not change signal `parameter_set_hash`
    stamping and does not touch the nightly LEAN cycle.
- **The one real gap:** minting the **first** hash. `bootstrap_live_account`
  copies an existing hash; it does not compute one (F10), and the canonical
  computation was never built (F9). → escalated as **Q1**.
- **Verdict: RECOMMEND as the primary fix**, paired with the Q3 decision on the
  nightly lever.

### Recommendation summary

> **Adopt (c)** to unblock the smoke, fix the UI, and clear the cutover
> dependency — implemented via the existing operator-script, not a migration.
> **Pair it with a Q3 decision** on whether the nightly LEAN kill-switch must
> also be operable (it is independent and lean.json-gated today). Treat **(b)'s
> CLI override** as an optional convenience and as the likely mechanism for the
> nightly lever if Q3 says "yes, wire it."
>
> **Necessary-but-not-sufficient caveat (must be signed off):** a green §10.3
> smoke via (c) proves the *trigger_v1_cycle* decommission path only. It does
> **not** prove that flipping the flag stops the *live nightly strategy*. Do not
> treat "smoke passed" as "the kill-switch works in production" until Q3 is
> resolved.

---

## 5. Locked decisions (proposed — for sign-off)

These I am confident about (each either re-states an existing locked decision or
is a low-risk pattern choice within already-allowed scope). **L-items are NOT
final until the operator signs off §11.**

- **L1 — Seed via operator-script, not alembic migration.** Re-affirms
  cutover-plan A2 (F11). Avoids the `alembic/**` forbidden-path label.
- **L2 — Baseline row carries the canonical V1 defaults with both bool flags =
  `False`.** `parameters` JSONB = the 12 canonical UPPER_CASE keys
  (`V1_DEFAULTS` ∪ `{STRATEGY_DECOMMISSIONED:False, EXIT_AUTO_APPROVE:False}`),
  serialized as the strategy already canonicalizes them
  (`V1Parameters.to_canonical_dict()` shape, decimals-as-strings).
- **L3 — The §10.3 smoke runs through `trigger_v1_cycle`, not the nightly LEAN
  cycle.** This matches §10.3's "controlled" framing and is the only DB-driven
  signal path (§2). The smoke runbook must say so explicitly.
- **L4 — Seeding does not change signal `parameter_set_hash` stamping.** Stated
  as an invariant the implementation must preserve (F7); any change to it is out
  of scope and would be its own risk-reviewed design.
- **L5 — `STARTING_CASH_USD` is NOT part of the `parameter_sets` row.** Per
  decisions-log 2026-05-?? lock: it lives in `lean.json` only and the agent must
  never `tighten_parameter` it. The baseline row excludes it.

---

## 6. Open Questions (escalate — operator sign-off required)

> Per `CLAUDE.md` / dev-guide §1.3, anything touching parameter identity or the
> risk/strategy surface is escalated, not decided unilaterally. Q1–Q4 are
> risk-adjacent.

### Q1 — What is the canonical `parameter_set_hash`, and what is its input scope? *(risk-adjacent — ESCALATE)*
backend-spec §3.11 (F13) says *"SHA-256 over JCS({param_name: value} for params
in **Parameter Ranges Table only**, alphabetized by name)"* — but
`composite_hash.py` was never built (F9), and the two operator-only flags
post-date the spec. Two candidate inputs:
- **(Q1-A) Spec-literal:** hash only the agent-tunable "Parameter Ranges Table"
  params, **excluding** `STRATEGY_DECOMMISSIONED`/`EXIT_AUTO_APPROVE`. Then the
  hash is **stable across decommission flips** → the §10.3 flip is a clean
  in-place `parameters` UPDATE with no PK churn (resolves the memory's
  "hash↔content desync" worry).
- **(Q1-B) Whole-set:** hash all 12 keys from `to_canonical_dict()`. Then
  flipping a flag changes the hash → the PK changes → the ceremony must
  INSERT-new + retire-old, not UPDATE-in-place (see Q2).
- **Recommendation:** **Q1-A** (spec-literal; stable hash; matches the existing
  `to_canonical_dict()` *minus* the two flags) **with the hash computed via the
  existing `services.audit.chain.jcs_serialize` primitive** so we don't invent a
  second canonicalizer. But this is a parameter-identity decision per §3.11 —
  **needs explicit sign-off**, and ideally a one-paragraph addition to
  backend-spec §3.11 clarifying flag scope.
- **Sub-question:** do we finally implement `services/version/composite_hash.py`
  (the spec's intended home) as the single source of truth for minting, or
  inline the computation in the seed tool? (Recommend: implement the module — it
  is referenced by `parameters.py` and is the correct long-term home.)

### Q2 — Decommission ceremony mechanics: in-place UPDATE vs event-sourced row-swap? *(risk-adjacent — ESCALATE)*
- If **Q1-A**: in-place `UPDATE parameter_sets SET parameters =
  jsonb_set(parameters,'{STRATEGY_DECOMMISSIONED}','true')` is consistent (hash
  unchanged). Simplest; matches §10.3's "via parameter UPDATE" wording.
- If **Q1-B**, or if we want the full event-sourced model: retire the old head
  (`last_active_at = now()`) and INSERT a new head row with the new hash. The
  sibling `parameters` table (F12 / §3.11) models exactly this with
  `prev_parameter_set_hash` chaining + an `audit_event_uuid`.
- **Coupled to Q4** (whether the full audit/event-sourcing ceremony is in scope).
- **Recommendation:** if Q1-A is chosen, adopt in-place UPDATE for the smoke;
  defer the event-sourced machinery to Q4. **Needs sign-off.**

### Q3 — Must the *nightly LEAN* kill-switch also be operable, or is the trigger-path smoke sufficient for now? *(scope — ESCALATE)*
This is the most consequential question. Today, flipping the DB flag does
**nothing** to the 21:30Z nightly cycle (F2/F3/F8). Options:
- **(Q3-A) Smoke-only scope (defer nightly lever):** accept that "decommission"
  currently means "stop the operator-driven `trigger_v1_cycle` path," document
  the limitation loudly, and treat the nightly lever as separate future work.
- **(Q3-B) Add the key to `lean.json` + restart:** make `STRATEGY_DECOMMISSIONED`
  an operable `lean.json` parameter (manual edit + `lean_local` restart). Cheap;
  redeploy-grade, not a hot toggle.
- **(Q3-C) Build a DB→`lean.json` sync** (or have LEAN POST-fetch the active
  param set from the api at `initialize`) so the DB flag genuinely drives the
  nightly cycle. Largest scope; makes the `v1_strategy.py:139-141` comment true.
- **Recommendation:** resolve this explicitly. My lean is **Q3-A for this
  design** (unblock the smoke now) **+ file Q3-C as a tracked follow-up** because
  a kill-switch that doesn't stop the live strategy is a dangerous half-measure
  to leave undocumented. **Needs sign-off** — this is a risk-control scope call.

### Q4 — Does the seed/flip need the full event-sourced audit ceremony now? *(risk-adjacent — ESCALATE)*
exit-pipeline-design R6 says a decommission "Parameter UPDATE requires the same
audit + `parameter_change_applied` event as any other locked parameter." None of
that machinery is wired for the operator-driven `parameter_sets` path today
(the agent's `parameter_changes` live under the forbidden
`services/agent/parameter_changes/**`). Options: (Q4-A) minimal seed + raw
UPDATE for the smoke, audit ceremony deferred; (Q4-B) build the
`parameter_change_applied` audit + `parameters`-table row as part of this work.
- **Recommendation:** **Q4-A** for unblocking the smoke (smallest reviewed
  change), **Q4-B tracked as follow-up** before any *live* decommission.
  **Needs sign-off.**

### Q5 — Reuse `bootstrap_live_account.py`, or add a paper-seed sibling? *(pattern — lean toward deciding, but flagging)*
`bootstrap_live_account` already INSERTs `parameter_sets` idempotently (F10) and
accepts `--env paper`. But its framing is "live cutover," its docstring's
extraction command assumes a non-empty paper table, and it does not mint a hash.
Options: (Q5-A) extend it (add a `--mint-from-defaults` mode that builds the
baseline row + computes the hash via Q1's chosen algorithm); (Q5-B) a new thin
`seed_parameter_set.py` operator tool dedicated to minting + INSERTing the
baseline. **Recommendation:** **Q5-A** (one tool, less surface) — but I'm
flagging rather than deciding because it changes a tool that also touches
`risk_state` bootstrap. **Confirm preference.**

### Q6 — Re-confirm the 0-row state + who/when runs the seed.
F1 is from memory (2026-05-29), not re-verifiable from this workstation.
Execution **step 0** must re-run, on the VPS:
`docker compose exec postgres psql -U postgres -d trading -c "SELECT count(*),
count(*) FILTER (WHERE last_active_at IS NULL) AS active FROM parameter_sets;"`
and confirm `0`. If a row already exists (e.g. someone seeded since), the plan
collapses to "UPDATE the existing head," skipping the INSERT.

---

## 7. PR breakdown (contingent on sign-off of §5 + §6)

> Sequencing assumes the recommended answers (L1–L5; Q1-A; Q2 in-place; Q3-A +
> follow-up; Q4-A; Q5-A). If the operator chooses differently, the breakdown
> shifts — call it out at sign-off.

- **PR-A — Hash minting + seed tooling (`scripts/operator_tools/**`,
  `services/version/composite_hash.py`).** Implement the canonical
  `parameter_set_hash` per Q1 (new `services/version/composite_hash.py` using
  `jcs_serialize`); extend `bootstrap_live_account.py` with a
  `--mint-from-defaults` mode (builds the baseline row from the canonical V1
  defaults, computes the hash, INSERTs idempotently). Unit tests:
  determinism/stability of the hash, idempotent INSERT, dry-run.
  - *Forbidden-path:* `services/version/` is **not** on the whitelist; verify
    before merge. `scripts/operator_tools/**` is not forbidden. **No
    `risk-review-approved` label expected** — but confirm the new module path.
- **PR-B — Decommission ceremony runbook + smoke wiring
  (`scripts/operator_tools/README.md`, `Docs/exit-pipeline-design.md` §10.3
  update).** Document the exact seed → UPDATE → `trigger_v1_cycle
  --reason-filter=decommission --no-dry-run` → verify `TRADE_CLOSED` → revert
  sequence, with the **L3/§2 caveat that this exercises the trigger path only.**
  No code beyond docs + possibly the optional `trigger_v1_cycle` convenience
  override from (b) if Q5/Q3 want it.
- **PR-C (follow-up, gated on Q3) — Nightly LEAN kill-switch. → DESIGN LOCKED
  2026-05-29; see §12.** Resolved to **Q3-C via api-fetch**: a new bearer-authed
  `GET /api/internal/lean/parameters` that `lean/v1_strategy.py` calls at the
  start of each daily cycle to override `STRATEGY_DECOMMISSIONED` from the DB
  head row (reusing the urllib + bearer plumbing v1_strategy already has).
  Fail-**open** to the lean.json default + loud alert on fetch failure; flag-only
  scope. Touches `services/api/**` + `lean/**` (both hot-fix scope — no
  `risk-review-approved` label) but is high-stakes → mandatory risk-review +
  tests + LEAN↔api smoke. **Implementation is a future session; merged code is
  inert until `lean_local` is rebuilt + restarted.**
- **PR-D (follow-up, gated on Q4) — Event-sourced audit ceremony.** Wire
  `parameter_change_applied` audit + `parameters`-table row for operator-driven
  parameter changes. **Touches forbidden paths (`services/audit/**` and/or
  `services/agent/parameter_changes/**`) → `risk-review-approved` required.**

---

## 8. Risks + mitigations

| ID | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| PR1 | "Smoke passed" is misread as "the live kill-switch works," but the nightly cycle is still un-decommissionable (F2/F3). | **High** if undocumented | **High** (false sense of a working safety lever) | L3 + the §4 caveat + Q3 must be on the sign-off checklist. Runbook states the limitation in bold. |
| PR2 | A wrong/hand-typed 64-hex hash is INSERTed (F10 doesn't verify hash↔content) → content-addressable invariant violated. | Med | Low today (nothing re-derives + compares), latent later | Q1 → compute the hash in code (composite_hash.py / `--mint-from-defaults`), never hand-type. Add a verify step. |
| PR3 | Two "active" head rows coexist (no partial-unique index, F12); head query picks by `first_active_at DESC`. | Low | Med (ambiguous active set) | Seed exactly one row; the ceremony UPDATEs in place (Q2) rather than INSERTing duplicates; consider a partial-unique index as future hardening (out of scope here). |
| PR4 | Seeding silently changes the `/system` UI from "defaults" to "real row" mid-session, surprising the operator. | Low | Low | Expected + desired (F6); call it out in the runbook so it's not mistaken for a bug. |
| PR5 | Operator runs the seed against the wrong env / a non-empty table. | Low | Med | Q6 step-0 count check; `bootstrap_live_account` is idempotent (`ON CONFLICT DO NOTHING`); `--env paper` needs no `--allow-non-paper` but live envs do. |
| PR6 | The §10.3 flip leaves the flag `True` if the revert step is skipped → next `trigger_v1_cycle` run emits decommission exits. | Med | Med | Runbook makes "revert to False" a checklist gate; the nightly LEAN cycle is unaffected (F2), so a stuck DB flag does **not** flatten the book on its own. |

---

## 9. Out of scope (deferred)

- **Implementing the full agent parameter-lifecycle** (`tighten_parameter` →
  `parameters` row → `parameter_sets` head swap) for operator-driven changes —
  only the minimal seed + smoke flip is in scope (Q4-A).
- **A partial-unique index on `last_active_at IS NULL`** (F12 hardening).
- **Changing how signals derive `parameter_set_hash`** (F7/L4) — explicitly
  preserved as-is.
- **`strategy_versions.decommissioned`** version-retirement flow (different
  concept; §1 note).
- **The nightly LEAN lever itself** unless Q3 says build it now (then it becomes
  PR-C).

---

## 10. Appendix: files + evidence

| Concern | File:line |
|---|---|
| Empty-table fallback (trigger) | `scripts/operator_tools/trigger_v1_cycle.py:835-857`, `:1205-1226` |
| Trigger CLI surface (no override) | `scripts/operator_tools/trigger_v1_cycle.py:304-418` |
| Nightly LEAN param load | `lean/v1_strategy.py:203-210`, `:846-859` |
| LEAN fallback defaults + bool coercion | `lean/v1_strategy.py:145-159`, `:180-194` |
| `lean.json` param block (no decommission key) | `lean/lean.json:17-30` |
| V1 defaults + dataclass + canonical dict | `strategies/v1_trend_following/parameters.py:36-63`, `:223-264` |
| Decommission read at signal time | `strategies/v1_trend_following/strategy.py:299-300,519-523` |
| Existing operator-script INSERT | `scripts/operator_tools/bootstrap_live_account.py:374-403` |
| `parameter_sets` DDL | `alembic/versions/0003_risk_tables.py:81-90` |
| Hash spec definition | `Docs/backend-spec.md:1371-1378` |
| Per-payload hash derivation | `services/qc_adapter/signal_ingestion.py:349,516-524` |
| `/system` UI reader | `services/api/repos/phase1.py:618-641` |
| Cutover-plan locked decisions | `Docs/live-money-cutover-plan.md` A2 (487), O7 (522), §10/18 (405) |
| The smoke being unblocked | `Docs/exit-pipeline-design.md` §10.3 (913-935) |

---

## 11. Sign-off — SIGNED OFF 2026-05-29

Operator delegated to the recommended answers (session 2026-05-29: "sign off
whatever you recommend"), keeping the cutover-plan A2/O7 lock intact. Recorded:

- [x] **L1–L5** accepted as written.
- [x] **Q1 → Q1-A** (spec-literal: hash the agent-tunable "Parameter Ranges
      Table" params only, **excluding** `STRATEGY_DECOMMISSIONED` /
      `EXIT_AUTO_APPROVE`). **Build `services/version/composite_hash.py`** as the
      single minting source using `services.audit.chain.jcs_serialize`. Add a
      one-paragraph backend-spec §3.11 clarification on flag scope (folded into
      PR-A).
- [x] **Q2 → in-place `UPDATE`** of the head row's `parameters` JSONB
      (consistent with Q1-A's stable hash; no PK churn).
- [x] **Q3 → Q3-A** (defer the nightly LEAN lever) **+ Q3-C tracked as a
      follow-up** (PR-C). **Accepted safety trade-off:** flipping the flag stops
      only the manual `trigger_v1_cycle` path, NOT the live 21:30 UTC nightly
      cycle, until PR-C ships. The PR-B runbook must state this in bold.
- [x] **Q4 → Q4-A** (minimal seed + raw `UPDATE` for the smoke; the
      event-sourced `parameter_change_applied` audit ceremony is deferred to
      PR-D and is **required before any *live* decommission**).
- [x] **Q5 → Q5-A** (extend `bootstrap_live_account.py` with a
      `--mint-from-defaults` mode rather than a new sibling tool).
- [ ] **Q6** — re-confirm 0-row state on the VPS. *Execution-time step 0, not a
      design decision; stays unchecked until run on the box.*
- [x] Acknowledged: **a green §10.3 smoke proves the trigger path only, not the
      live nightly kill-switch** (until PR-C / Q3-C ships).

**Lock decision (revisit cutover-plan A2/O7?): KEPT.** Rationale: the row is a
living, content-hashed, environment-divergent value — an operational concern,
not a schema one. The only real cost of the operator-script approach (a forgotten
seed after a DB restore silently reverts to code-defaults) is mitigated by adding
the seed step to the DB-restore runbook (folded into PR-A/PR-B docs).

**PR-A + PR-B shipped 2026-05-29** (PR #294 minter + `--mint-from-defaults`; PR
#295 runbook + §10.3 wiring). **PR-C design locked 2026-05-29** — see §12
(operator delegated: "do whatever you recommend" on the open questions).

---

## 12. PR-C design — nightly LEAN kill-switch (resolves Q3 / Q3-C)

> **Status: DESIGN — locked 2026-05-29 to the recommended answers (operator
> delegated the open questions). Implementation is a future session; merged code
> is INERT until `lean_local` is rebuilt + restarted on the VPS.**

### 12.1 Problem (restated)

The live 21:30 UTC nightly LEAN cycle reads its parameters from `lean/lean.json`,
which omits `STRATEGY_DECOMMISSIONED` — so flipping the DB flag does nothing to it
(F2/F3/F8). PR-A/PR-B made the **manual** `trigger_v1_cycle` decommission path
work; PR-C makes the **automatic** nightly cycle honor the same DB flag, closing
the L3/§2 "trigger-path-only" caveat the PR-B runbook calls out in bold.

### 12.2 Grounding facts (verified 2026-05-29)

- **`lean/lean.json` is mounted read-only** from the repo
  (`./lean:/Lean/Algorithm:ro`, `docker-compose.yml`) and rendered at boot by
  `infrastructure/lean_local/entrypoint.sh` (env-var substitution). It is not
  baked into the image and not writable by a sync job without dirtying the repo
  checkout on the VPS.
- **`lean/v1_strategy.py` already speaks HTTP to the api**: it imports
  `urllib.request`/`urllib.error`, holds the api base URL (`http://api:8000`) +
  the shared bearer (`lean.api_bearer_token`) read at `initialize()`, and POSTs
  every signal to `POST /api/internal/lean/signals` (`v1_strategy.py:11-17,
  86-92,162-215`). It already hard-fails closed at `initialize()` if the bearer
  is missing.
- **`lean_local` has the bearer + internal-network reach to api, but no DB
  credentials** (by design — it is a pure consumer).

### 12.3 Chosen mechanic (Q1) — api-fetch in the daily cycle

A new bearer-authed **`GET /api/internal/lean/parameters`** returns the active
`parameter_sets` head row's `parameters` (the same head-pointer query
`trigger_v1_cycle` + `/system` use; spec defaults when the table is empty).
`v1_strategy.py` calls it **at the start of each daily signal cycle** — reusing
the urllib + base-URL + bearer plumbing it already has — and overrides
`STRATEGY_DECOMMISSIONED` for that cycle via the existing `_coerce_bool_param`.

Rejected alternatives:

- **Q3-B (add the key to `lean.json`):** creates a **two-source consistency
  hazard** — the DB flag (manual path + `/system`) and a `lean.json` flag
  (nightly) could silently disagree on a *safety* lever. The worst failure mode
  for a kill-switch is "I flipped it but only one source saw it." Rejected.
- **Entrypoint shell-injection into the rendered `lean.json`:** fiddly JSON
  manipulation in shell; less testable than Python; still a boot-time fetch.
- **Direct DB read from `lean_local`:** would add DB credentials to a
  pure-consumer container — new credential surface. Rejected.
- **Fetch only at `initialize()`:** honored only as of the 21:10 UTC restart; a
  mid-day flip wouldn't be seen until a restart. Per-cycle fetch is strictly more
  responsive at negligible cost (one GET/day). Chosen over initialize-only — the
  operator can still force immediate pickup with `docker compose restart
  lean_local`, but won't need to for the next nightly cycle.

### 12.4 Fail-safe (Q2) — fail-OPEN to the lean.json default + loud alert

If the GET fails (api unreachable / timeout / non-2xx / malformed), the cycle
proceeds with the **`lean.json`/default value (`False`)** and emits a **loud
P1-grade alert + structured log** (`lean_parameters_fetch_failed`). Rationale:

- **The operator-approval gate is the real interlock.** V1 approves every signal
  before any order, so "keep trading on a transient blip" never auto-executes —
  the operator sees the normal signals (and the alert) and decides.
- **Hard-failing the LEAN boot/cycle on a param-fetch error would be worse** — it
  would take down the whole nightly cycle, not just the kill-switch.
- **The manual path (PR-B) is the immediate recourse** if the operator intended
  to decommission and sees the alert.
- Distinct from the existing **missing-bearer → hard fail-close** at
  `initialize()` (a config error), which stays as-is.

> **Load-bearing dependency — fail-open is only safe while `EXIT_AUTO_APPROVE`
> auto-approval is unbuilt + `False`.** Precise mechanism (verified): when
> `STRATEGY_DECOMMISSIONED=True` the strategy *emits* decommission exit signals;
> those are not orders. What actually prevents fail-open from auto-executing is
> that **every signal — entries AND exits — is operator-gated today**: entries are
> always `pending`→operator-`approved` before `order_placement_worker` claims
> them, and exit auto-approval (`EXIT_AUTO_APPROVE`) **is not implemented** (no
> auto-approve worker exists; default `False`; deferred to a post-cutover PR). So
> a fail-open cycle that wrongly reads `False` emits normal signals that all still
> require approval — nothing executes without the operator.
>
> **Forward-guard (must hold before any `EXIT_AUTO_APPROVE` auto-approval ships):**
> the moment exits can auto-approve, fail-open becomes dangerous — a nightly cycle
> that fail-opens to `False` while the operator intended to decommission would keep
> a *live* book auto-executing exits with no operator in the loop, silently
> defeating the kill-switch. Before that worker is built/enabled, this decision
> MUST be re-evaluated — either flip the nightly cycle to **fail-closed for exits**,
> or couple the auto-approve worker to a *healthy* kill-switch read (refuse to
> auto-approve when the most recent param fetch failed). The PR-C implementation
> must add a test that pins `EXIT_AUTO_APPROVE=False` as a precondition of the
> fail-open path and a pointer to this guard.

> ⚠️ **Highest-stakes decision in PR-C — the nightly kill-switch FAILS OPEN.** If
> you would rather it fail-*closed* (flatten the book on a fetch error), override
> this before implementation. Note: the approval gate already prevents
> auto-execution either way, and fail-closed would flood `/signals` with
> decommission exits on any transient api blip — which is why fail-open + a loud
> alert is the recommendation.

### 12.5 Scope (Q3) — flag-only

PR-C consumes **only `STRATEGY_DECOMMISSIONED`** from the fetched param set. The
endpoint returns the **full** active param set so a follow-up can make the
nightly cycle fully DB-driven (closing F8) without a new endpoint — but PR-C does
**not** change entry/sizing param sourcing (a larger behavioral change to signal
generation, out of scope here).

### 12.6 Review path (Q4)

`services/api/**` and `lean/**` are §2.3 hot-fix scope → **no
`risk-review-approved` label required**. But this rewires the live kill-switch, so
the implementation PR MUST: (a) carry unit tests for the endpoint + the
fetch/override + the fail-open path; (b) include a smoke-test fixture or
operator-runbook checklist for the LEAN↔api boundary (dev-guide §6.8 / `[A27]` —
third-party platform contract); (c) get a full `risk-review` subagent pass; (d)
be merged but **NOT deployed** until the operator explicitly runs the `lean_local`
rebuild + restart.

### 12.7 Implementation sketch (for the future session)

1. **api:** add `GET /api/internal/lean/parameters` to
   `services/api/routes/internal/lean.py` (bearer-auth via the existing
   `LeanAuthMiddleware`); return the active head row's `parameters`, or spec
   defaults when the table is empty (mirror `/system`'s fallback).
2. **lean:** in `v1_strategy.py`, at the top of the daily signal cycle, GET the
   param set, coerce `STRATEGY_DECOMMISSIONED` via `_coerce_bool_param`, override
   the cycle's `V1Parameters`; on any failure, keep the `lean.json` default +
   emit `lean_parameters_fetch_failed`.
3. **tests:** endpoint (empty table → defaults; seeded → value); v1_strategy
   fetch-success → override; fetch-failure → fail-open + alert; bearer-missing
   path unchanged.

### 12.8 Interaction with PR-A/PR-B/PR-D

PR-C makes the **same DB flag** the seed (PR-A) + ceremony (PR-B) operate on also
drive the nightly cycle. After PR-C ships **and deploys**, the PR-B runbook +
exit-pipeline §10.3 scope caveats are lifted (flipping the DB flag stops both the
manual path AND the live nightly cycle) — update those docs at that point
(tracked). PR-C does **not** add the `parameter_change_applied` audit event for
the flip; that remains **PR-D**, still required before any *live* decommission.
