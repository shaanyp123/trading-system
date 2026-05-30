# V1 Exit Proximity Design ("Watching → Exits")

**Status:** SIGNED OFF 2026-05-30 — PR-A ready to kick off. Locked: **Q1 = (B) api-side stop join** (LEAN emits indicator dims only; the api joins the latest working `stop_market` order's `stop_price` at persist time; daily fidelity). **Q3 = Today page, beside Positions.** Q2/Q4/Q5/Q6/Q7 accepted at their recommended/PROPOSED values; NEAR-band defaults accepted (trend 0.5% / stop 1% / holding-days 3-day warn).
**Created:** 2026-05-30
**Owner:** operator (Shaan)
**Implementer:** Claude Code (multi-session, one per PR)
**Precedent:** Mirrors `Docs/signal-proximity-design.md` (entry-side proximity, COMPLETE — PR-A #283 / PR-B #284 / PR-C #286) and is the **explicitly-deferred follow-up** named there (signal-proximity §3 D3 + §12 "Exit-side proximity — V1 follow-up").

## 0. Motivation

Entry proximity (the "Watching" view on `/signals`) shows how close each *flat* market is to **opening** a position. This feature is the mirror image: for each **open position**, show how close it is to being **closed** by each exit trigger. The operator wants the same at-a-glance "should I be at the desk?" signal for the exit side — e.g. "/MES is 0.4% above its protective stop and 1 day past MIN_HOLDING with the close sitting right on MA_FAST" is a position about to exit on the next cycle.

**Non-goal:** intraday/live exit proximity for the indicator-driven triggers (trend_flip / reversal / decommission). Those are evaluated on `Resolution.DAILY` by the same LEAN cycle that evaluates entries, so a live view would not match what LEAN decides on (identical rationale to signal-proximity D2). **Exception — the stop-distance dimension is inherently a live/mark concept; see D3 + Q1, which is the central open question of this design.**

## 1. The four V1 exit triggers (canonical reference)

Grounded in [`strategies/v1_trend_following/strategy.py::generate_exit_candidates`](strategies/v1_trend_following/strategy.py:440) + `Docs/exit-pipeline-design.md`. Precedence: **decommission > reversal > trend_flip**; stop-hit is independent (fires at IBKR, not in this pipeline).

| # | Trigger | Condition | Proximity dimension | Data source |
|---|---|---|---|---|
| (a) | **Stop hit** | Bracket stop-market fills at IBKR. Stop level = entry ∓ `STOP_DISTANCE_ATR_MULT`(3.0)×ATR. Pipeline emits NO candidate for (a). | **mark vs stop**: `(mark − stop)/mark` for a long (mirror for short). Smaller = closer to stop-out. | **NOT the strategy module** — needs the live mark + the actual working stop price (IBKR / `orders` table). See D3/Q1. |
| (b) | **Reversal** | Held position + opposite direction passes ALL 3 entry filters (Donchian + Trend + Hurst). | This IS entry proximity for the *opposite* direction. The entry-side `signal_proximity` row already carries the opposite-direction headroom. | Reuse existing `signal_proximity` (the opposite side's gates). See Q2. |
| (c) | **Trend flip** | Held LONG + `close < MA_FAST` (mirror for SHORT) AND held ≥ `MIN_HOLDING_DAYS`. "Lazy" — `close vs MA_FAST` only, NOT the death cross. | TWO sub-dimensions: (i) **holding-days headroom** = `MIN_HOLDING_DAYS − held_days` (gates whether the flip can fire at all); (ii) **deterioration** = `(close − ma_fast)/close` for a long — how close `close` is to crossing below `MA_FAST`. | Strategy module (daily snapshot: `last_close`, `ma_fast`, `position.opened_at_session_date`). |
| (d) | **Decommission** | `STRATEGY_DECOMMISSIONED=True` → CLOSE every held position next cycle, regardless of indicators. | Binary flag — not a distance. Shown as a banner / "armed" badge, not a headroom bar. | Strategy params (`self._params.strategy_decommissioned`). |

**V1 defaults** (from `parameters.py`): `MIN_HOLDING_DAYS=14` (LOCKED), `MA_FAST_DAYS=50`, `MA_SLOW_DAYS=200`, `STOP_DISTANCE_ATR_MULT=3.0`, `ATR_LOOKBACK_DAYS=20` (LOCKED).

## 2. Operator Design Decisions (PROPOSED — confirm in §14)

| # | Decision | Rationale |
|---|---|---|
| **ED1** | Single source of truth for the **indicator-driven** dimensions (trend_flip holding-days + deterioration, decommission) = the strategy module, emitted by the same LEAN cycle, exactly like entry proximity. | Eliminates display-vs-decide drift (same principle as signal-proximity D1). The exit-pipeline math and the exit-proximity math live in the same module. |
| **ED2** | Refresh cadence for indicator dimensions: daily, post-LEAN-cycle (~21:30 UTC). No streaming. | V1 trend_flip/reversal/decommission evaluate `Resolution.DAILY`. |
| **ED3** | **Stop-distance is the exception.** PROPOSED for V0: compute stop proximity **daily, from the SAME LEAN cycle**, using that cycle's last close as the "mark" and the position's stored stop level — NOT a live intraday mark. This keeps ALL exit-proximity on one daily write path and one data source. The live/intraday stop view (true mark-to-stop) is deferred. **This is the central open question — see Q1.** | A live mark needs a streaming market-data path (new clientId, duplicated math) we deliberately avoided for entry proximity. Daily-close stop distance is a strictly-correct lower-fidelity proxy: "as of last close, the position sat X% above its stop." |
| **ED4** | Reversal proximity is **derived from the existing entry `signal_proximity` data**, not recomputed. For each held position, the "reversal closeness" = the opposite direction's overall entry state for that market. | The reversal exit (b) couples on the entry pipeline's opposite-direction output (per exit-pipeline L1). Reusing the entry proximity row keeps one source of truth + zero new math. See Q2. |
| **ED5** | New dedicated `exit_proximity` table (NOT JSONB, NOT reusing `signal_proximity`). One row per open position per cycle. | Different shape from entry proximity (per-position, carries held_days / stop distance / exit-trigger states). Dedicated table is queryable + indexable. Cost = one migration. |
| **ED6** | Render location: a **new "Exits" sub-view**. PROPOSED: a section on the **Today page** next to Positions (where open positions already live), NOT on `/signals` (which is entry-centric). **See Q3** — alternative is a second section on `/signals`. | Exit proximity is about positions you HOLD; the operator reads held positions on Today. Co-locating with the Positions table reconciles "what I hold" + "how close each is to closing." |
| **ED7** | PR-A + PR-B require `risk-review-approved` (touches `strategies/v1_trend_following/**` + Alembic). PR-C frontend-only, no label. | `CLAUDE.md` forbidden-path rules + dev-guide §11 A02 (identical to signal-proximity D6). |
| **ED8** | Frontend refresh: match the host page's existing refresh model. No new SSE event type. | `CLAUDE.md` forbids new SSE event types without enum migration (identical to signal-proximity D7). |

## 3. Proximity Definition

### 3.1 Exit states (per trigger)

Reuse the entry-side `GateState` enum (`{PASS, CLOSE, FAIL}`) from `strategies/v1_trend_following/proximity.py`, but **the semantics invert** for the exit side, which is a UX hazard worth a deliberate choice (see Q4). For exits we propose a distinct enum to avoid "PASS = good" confusion:

```python
class ExitState(StrEnum):
    HOLDING = "holding"      # comfortably far from this exit trigger
    NEAR = "near"            # within the CLOSE band — about to exit
    TRIGGERED = "triggered"  # this trigger fired (or would fire) THIS cycle
```

`HOLDING` (green / calm) → `NEAR` (yellow / attention) → `TRIGGERED` (red / closing). This reads correctly for an operator: green = position safe, red = position closing. (Contrast: entry-side green=PASS means "fired" — opposite valence. Keeping a separate enum prevents a color/meaning collision in shared components.)

### 3.2 Per-trigger headroom

**(c) Trend flip** — two gates, BOTH must be satisfied for the flip to fire, so the proximity is the *further* of the two (you exit only once both clear):

- **Holding-days gate**: `held_days_headroom = MIN_HOLDING_DAYS − held_days`.
  - `> ~3 days` → HOLDING; `0 < x ≤ 3` → NEAR; `≤ 0` → gate satisfied (flip *can* fire — defer to the deterioration gate for the overall state).
- **Deterioration gate** (long): `deterioration_pct = (last_close − ma_fast) / last_close` (mirror for short: `(ma_fast − last_close)/last_close`).
  - `> band` → HOLDING; `0 < x ≤ band` (close approaching MA_FAST from above) → NEAR; `≤ 0` → TRIGGERED (close already crossed → trend flip condition met).
  - PROPOSED band: `EXIT_TREND_NEAR_BAND_PCT = 0.005` (0.5%, mirrors entry trend band).
- **Overall trend_flip state**: if held_days gate NOT satisfied → cap at HOLDING/NEAR on the holding-days dimension (the flip *cannot* fire yet even if close < MA_FAST), and surface a "blocked by MIN_HOLDING (N days left)" detail. If held_days satisfied → state = the deterioration state.

**(a) Stop distance** (long): `stop_headroom_pct = (mark − stop_price) / mark` (mirror for short).
- `> band` → HOLDING; `0 < x ≤ band` → NEAR; `≤ 0` → TRIGGERED (stop breached as-of mark).
- PROPOSED band: `EXIT_STOP_NEAR_BAND_PCT = 0.01` (1%). `mark` = daily last close under ED3 (or live mark if Q1 resolves to the live path).
- Requires the **working stop price** — see Q1 for where it comes from.

**(b) Reversal** — derived (ED4): `reversal_state` = a mapping of the opposite direction's entry `overall_state` → exit semantics (entry PASS→TRIGGERED, entry CLOSE→NEAR, entry FAIL→HOLDING). No new math.

**(d) Decommission** — binary: `TRIGGERED` if `strategy_decommissioned` else `HOLDING`. No band.

### 3.3 Overall "closest exit" + sort key

For each open position:
- `closest_exit` = the trigger with the most-advanced state, precedence-aware: if decommission armed → `decommission`; else the trigger whose state is worst (TRIGGERED > NEAR > HOLDING), tie-break by smallest numeric headroom.
- `overall_exit_state` = state of `closest_exit`.

Sort the "Exits" view by closest-to-closing: `TRIGGERED` group first (closing this cycle), then `NEAR` ascending by headroom, then `HOLDING`.

### 3.4 Insufficient history / missing data

- Position with `< min_required_bars` → trend_flip dimensions NULL, `gate_status='warming_up'` (the position still shows, with stop distance if available).
- Position with no recorded `opened_at_session_date` → held_days NULL; mirror the strategy's conservative skip (trend_flip can't be gated) + show "holding-days unknown".
- No working stop found for stop-distance → that dimension NULL + "no stop on record" detail (this is also a latent POSITION_UNPROTECTED signal — cross-link to that alert).

## 4. New Strategy-Side Types (PR-A)

Extend `strategies/v1_trend_following/proximity.py` (or a sibling `exit_proximity.py` — decide at PR-A time per Q5) with:

```python
class ExitState(StrEnum): ...  # §3.1

@dataclass(frozen=True, slots=True)
class ExitTriggerProximity:
    state: ExitState
    headroom: Decimal | None   # None when N/A or warming up
    detail: str | None

@dataclass(frozen=True, slots=True)
class PositionExitProximity:
    market: str
    direction: str             # 'long' | 'short'
    held_days: int | None
    trend_flip: ExitTriggerProximity      # combines holding-days + deterioration
    stop: ExitTriggerProximity            # may be NULL-state under ED3/Q1
    reversal: ExitTriggerProximity        # derived from entry proximity (ED4)
    decommission: ExitTriggerProximity
    last_close: Decimal | None
    stop_price: Decimal | None
    overall_state: ExitState
    closest_exit: str          # 'stop'|'reversal'|'trend_flip'|'decommission'
    gate_status: str           # 'active'|'warming_up'|'decommissioned'|'min_holding_blocked'
```

Public function, pure-Python, no LEAN imports:
```python
def compute_position_exit_proximity(
    *, market, position, snapshot, params,
    stop_price: Decimal | None,
    reversal_entry_state: GateState | None,
    as_of_session_date: date,
) -> PositionExitProximity: ...
```

**Risk-review note:** like entry proximity, this exposes + classifies already-computed exit-pipeline values. The exit-trigger logic in `generate_exit_candidates` MUST stay bit-identical — no new trigger, no threshold change to what actually closes.

## 5. LEAN wrapper changes (PR-A)

`lean/v1_strategy.py::on_daily_signal_cycle` already runs `generate_exit_candidates` each cycle (the feature "piggybacks" on it). Extend the `lean_cycle_heartbeat` POST's `extra` dict with a `position_exit_evaluations` array (one per held position), mirroring how `market_evaluations` was added for entry proximity. Payload: ~2 open positions × ~250 B = negligible.

**Stop-price sourcing in LEAN (the Q1 crux):** the strategy module does not know the working stop. Options at PR-A: (i) LEAN passes the stored stop from its own bracket bookkeeping; (ii) the api joins the latest working `stop_market` order at persist time (PR-B), leaving LEAN to emit only the indicator dimensions. **Q1 decides this.**

## 6. API changes (PR-B)

### 6.1 Pydantic — add optional `position_exit_evaluations` to `LeanEventRequest` (lands in PR-A so heartbeats don't 422).

### 6.2 New Alembic migration — `exit_proximity` table:
```sql
CREATE TABLE exit_proximity (
    id              BIGSERIAL PRIMARY KEY,
    cycle_ts_utc    TIMESTAMPTZ NOT NULL,
    session_date_et DATE NOT NULL,
    market          TEXT NOT NULL,
    direction       TEXT NOT NULL,
    held_days       INTEGER,
    last_close      NUMERIC,
    stop_price      NUMERIC,
    -- per-trigger state + headroom
    trend_flip_state     TEXT NOT NULL,
    trend_flip_headroom  NUMERIC,          -- deterioration pct
    held_days_headroom   INTEGER,          -- MIN_HOLDING_DAYS - held_days
    stop_state           TEXT NOT NULL,
    stop_headroom_pct    NUMERIC,
    reversal_state       TEXT NOT NULL,
    decommission_state   TEXT NOT NULL,
    overall_state        TEXT NOT NULL,
    closest_exit         TEXT NOT NULL,
    gate_status          TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_exit_proximity_market_cycle ON exit_proximity (market, cycle_ts_utc DESC);
CREATE INDEX idx_exit_proximity_cycle_ts ON exit_proximity (cycle_ts_utc DESC);
```
Retention: keep all (~2 rows/day). If Q1 resolves to api-side stop join, the heartbeat handler enriches `stop_price`/`stop_state` from the latest working `stop_market` order before insert.

### 6.3 Heartbeat handler persists rows (best-effort; a DB failure logs + does NOT 5xx the heartbeat — identical to signal-proximity §6.3). Observational, NOT a state change → no `audit_log` entry (per signal-proximity Q2; confirm Q6).

### 6.4 New endpoint `GET /api/positions/exit-proximity` (or `/api/today/exit-proximity`) — latest row per open market. Path depends on ED6/Q3 render location.

## 7. Frontend changes (PR-C)

- New `ExitWatchingSection.tsx` rendered per ED6/Q3 (Today page beside Positions, or a section on `/signals`).
- Per row: symbol · direction · held days (vs MIN_HOLDING) · a chip per trigger (Stop / Trend-flip / Reversal / Decommission) colored HOLDING=green / NEAR=yellow / TRIGGERED=red · a **"Closest exit"** column naming the limiting trigger + its headroom.
- Decommission armed → row banner "Strategy decommissioned — closing next cycle".
- Empty state: "No open positions" (distinct from "no data yet").
- Refresh: host page's existing model (ED8).

## 8. Open Questions (MUST resolve in sign-off)

**Q1 (CENTRAL): Stop-distance fidelity + data source.** Three options:
- **(A) Daily-close proxy [PROPOSED V0]:** stop distance computed in the LEAN cycle from that cycle's last close as the mark. One write path, one data source, no new clientId. Lower fidelity (stale between cycles) but strictly-labelled "as of last close."
- **(B) API-side stop join, daily:** LEAN emits indicator dimensions only; the api joins the latest working `stop_market` order's `stop_price` at persist time + uses the last close as mark. Keeps stop bookkeeping in the api (where `orders` lives) rather than LEAN. Still daily.
- **(C) Live mark-to-stop:** a streaming/intraday path (new ib_gateway clientId + duplicated mark math). Highest fidelity, highest cost — the thing entry-proximity D2 deliberately rejected.
**Recommendation: (B)** — the working stop lives in the `orders` table (api side), and `replace_protective_stop` / the exit pipeline already manage it there; LEAN shouldn't re-derive it. Daily fidelity matches the rest of the view.

**Q2: Reversal proximity — derive from entry `signal_proximity`, or recompute?** PROPOSED: derive (ED4) — zero new math, one source of truth. Confirm the join (held position's market + opposite direction → that market's entry proximity row for the same cycle).

**Q3: Render location — Today page (beside Positions) or `/signals` (second section)?** PROPOSED: Today page (ED6), because exit proximity is about *held* positions. Operator may prefer one unified `/signals` "Watching" page (entries + exits together). **Operator pick.**

**Q4: Separate `ExitState` enum (HOLDING/NEAR/TRIGGERED) or reuse entry `GateState` (PASS/CLOSE/FAIL)?** PROPOSED: separate enum — the valence inverts (exit-green = "safe/holding" vs entry-green = "firing"), and a shared enum in shared chip components would invite a color/meaning bug. Costs a small mapping for the derived reversal dimension.

**Q5: New module `exit_proximity.py` or extend `proximity.py`?** PROPOSED: new sibling module `strategies/v1_trend_following/exit_proximity.py` — keeps entry vs exit proximity separately testable; both pure-Python. Minor: shared `_classify_*` band helpers could live in a common `_proximity_bands.py`.

**Q6: Audit-log the exit-proximity rows?** PROPOSED: NO (observational, the table IS the trail — identical to signal-proximity Q2). Confirm per `feedback_audit_first_ordering`.

**Q7: Decommission display when `parameter_sets` is empty (it currently is — see [[project_parameter_sets_empty]]).** The nightly LEAN cycle reads `STRATEGY_DECOMMISSIONED` from `lean.json` (defaults False), NOT the DB. So the decommission dimension reflects the LEAN-config flag, always `HOLDING` today. Confirm the operator understands the decommission chip tracks the LEAN config lever, not the (empty) DB flag.

## 9. PR Breakdown

### PR-A: Strategy + LEAN emission (+ API tolerates new field)
- New `exit_proximity.py` (per Q5) + types in §4; pure-Python + tests.
- Expose the exit-pipeline's per-position snapshot (held_days, last_close, ma_fast, direction) up to the caller WITHOUT changing `generate_exit_candidates`' decisions.
- `lean/v1_strategy.py` — attach `position_exit_evaluations` to the heartbeat.
- `services/api/schemas/lean.py` — optional `position_exit_evaluations` field.
- Tests: per-trigger state classification (trend_flip holding-days + deterioration; stop distance; decommission; derived reversal mapping); warming-up; min-holding-blocked; property test `overall_state == worst_trigger_state`; a drift test asserting every `generate_exit_candidates` emission ⇒ the matching trigger's state == TRIGGERED.
- **Requires `risk-review-approved`.** Deployable independently (field flows to /dev/null).

### PR-B: API persistence + endpoint
- Alembic `exit_proximity` table; repo (insert + latest-per-market); response schema; endpoint.
- Heartbeat handler persists rows; **if Q1=(B), enrich `stop_price` from latest working `stop_market` order.**
- Integration tests: heartbeat → rows; endpoint returns latest-per-position ordered closest-to-closing.
- **Requires `risk-review-approved`** (Alembic). Meaningful only after PR-A in prod.

### PR-C: Frontend "Exits" section
- `ExitWatchingSection.tsx` + typed client + host-page wiring (per Q3).
- **No label.** Meaningful only after PR-B.
- Acceptance: post-cycle, the section shows each open position with per-trigger state + closest-exit; the position nearest an exit is at the top.

## 10. Test Plan
- **PR-A unit:** trend_flip with held_days < / = / > MIN_HOLDING (14); deterioration long & short around the 0.5% band; stop distance around the 1% band with long & short; decommission armed/not; derived reversal mapping (entry PASS→TRIGGERED etc.); warming-up; opened_at None; property `overall == worst`; drift test vs `generate_exit_candidates`.
- **PR-A LEAN:** mock exit result → heartbeat body carries well-formed `position_exit_evaluations`.
- **PR-B integration:** heartbeat (2 positions) → 2 rows; stop enrichment join (if Q1=B); endpoint ordering.
- **PR-C E2E (paper):** after a 21:30 UTC cycle with ≥1 open position, the section renders; a position past MIN_HOLDING with close near MA_FAST shows trend_flip=NEAR; closest-exit column matches.

## 11. Risks + Mitigations
| Risk | Impact | Mitigation |
|---|---|---|
| Exit-proximity math drifts from `generate_exit_candidates` | Display lies about closeness-to-close | PR-A drift test: every emitted exit ⇒ matching trigger state TRIGGERED + closest_exit matches `exit_reason`. |
| Stop-distance uses a stale/daily mark (ED3/Q1) | Operator over-trusts a between-cycles number | Label explicitly "as of last close HH:MM"; Q1=(B) sources the real working stop; live mark deferred with a clear note. |
| Inverted color valence vs entry "Watching" | Operator misreads green/red | Separate `ExitState` enum + distinct labels (HOLDING/NEAR/TRIGGERED), Q4. |
| Decommission chip implies DB flag works | False confidence in the kill lever | Q7 note: chip tracks the LEAN `lean.json` config flag, not the empty `parameter_sets` DB row. Cross-link [[project_parameter_sets_empty]]. |
| No working stop on record for a held position | Stop dimension blank | Surface "no stop on record" + cross-link the POSITION_UNPROTECTED alert (it's a real risk signal, not just missing data). |

## 12. Out of Scope (Deferred)
- Live/intraday stop mark (Q1 option C).
- Operator-tunable NEAR bands (V0 hardcoded; V1 `parameter_set` knob).
- Historical exit-proximity backfill (start empty).
- Per-position exit-proximity history sparklines.
- Profit-target proximity (V1 has none — exit only on stop/reversal/trend_flip/decommission).

## 13. Appendix: Files Touched Summary
| Path | PR | Change |
|---|---|---|
| `strategies/v1_trend_following/exit_proximity.py` | A | NEW (pure-Python) |
| `strategies/v1_trend_following/strategy.py` | A | Expose per-position exit snapshot from `generate_exit_candidates`; logic UNCHANGED |
| `strategies/v1_trend_following/__init__.py` | A | Re-export new types |
| `lean/v1_strategy.py` | A | Attach `position_exit_evaluations` to heartbeat |
| `services/api/schemas/lean.py` | A | Optional `position_exit_evaluations` field |
| `services/api/routes/internal/lean.py` | A | Log count; B: persist (+ stop join if Q1=B) |
| `tests/unit/test_v1_exit_proximity.py` | A | NEW |
| `tests/unit/test_lean_event_request.py` | A | New field tolerance |
| `alembic/versions/000X_exit_proximity.py` | B | NEW migration |
| `services/api/repositories/exit_proximity.py` | B | NEW |
| `services/api/schemas/exit_proximity.py` | B | NEW |
| `services/api/routes/...exit_proximity endpoint` | B | NEW |
| `tests/integration/test_exit_proximity_persistence.py` | B | NEW |
| `tests/integration/test_exit_proximity_endpoint.py` | B | NEW |
| `apps/web/src/components/.../ExitWatchingSection.tsx` | C | NEW |
| `apps/web/src/lib/api/exit-proximity.ts` | C | NEW |
| host page (Today or `/signals`) | C | render section |
| `Docs/decisions-log.md` / `Docs/file-index.md` | A,B,C | per-PR entries |

## 14. Sign-off Checklist — SIGNED OFF 2026-05-30
- [x] ED1–ED8 (§2) accepted. **ED3 superseded by Q1=(B): stop level comes from the api-side `stop_market` join, NOT a LEAN daily-close-derived mark.**
- [x] **Q1 resolved → (B) api-side stop join.** LEAN emits only the indicator dimensions (trend_flip holding-days + deterioration, decommission, derived reversal); the api enriches `stop_price`/`stop_state` from the latest working `stop_market` order at PR-B persist time. Daily fidelity. Live mark-to-stop (C) deferred.
- [x] Q2 reversal-derive accepted (derive from entry `signal_proximity`).
- [x] **Q3 render location → Today page, beside Positions.** Endpoint path → `/api/today/exit-proximity` (or `/api/positions/exit-proximity`); resolve at PR-B time.
- [x] Q4 separate `ExitState` enum (HOLDING/NEAR/TRIGGERED) accepted.
- [x] Q5 new module `exit_proximity.py` accepted.
- [x] Q6 no-audit-log accepted (table is the trail).
- [x] Q7 decommission-chip-tracks-LEAN-config understood (`lean.json`/code default, not the seeded DB row; see [[project_parameter_sets_empty]]).
- [x] NEAR-band defaults accepted: trend 0.5% / stop 1% / holding-days 3-day warn.
- [x] PR-A/B/C split + `risk-review-approved` on A & B accepted.

## 15. PR-A Kickoff Prompt (after sign-off)
> Read `Docs/exit-proximity-design.md` end-to-end. Implement PR-A per §9. Constraints:
> - Touch only the §13 PR-A files.
> - `generate_exit_candidates` decision logic MUST stay bit-identical — this PR exposes + classifies already-computed values.
> - `exit_proximity.py` has zero LEAN imports — pure-Python, pytest from repo root.
> - `LeanEventRequest` accepts the new field as optional; older heartbeats must NOT 422.
> - Honor the Q1 resolution for stop sourcing (if Q1=B, LEAN emits indicator dims only; stop enrichment is PR-B).
> - Apply `risk-review-approved`. Open the PR with plain-English summary + risk-impact ("zero change to what closes; observation-only") + test output. Run `pre-pr-checklist`.

## 16. PR-B Kickoff Prompt (after PR-A merges)
> Read the design end-to-end. Implement PR-B per §9. Alembic migration is the canonical schema. Best-effort writes (DB failure logs, returns 202, never 5xx the heartbeat). If Q1=B, join the latest working `stop_market` order for `stop_price`/`stop_state`. Same auth as existing read endpoints. Apply `risk-review-approved`. Paste migration SQL + test output in the PR. Run `pre-pr-checklist`.

## 17. PR-C Kickoff Prompt (after PR-B merges)
> Read the design end-to-end. Implement PR-C per §9 at the Q3-chosen location. Match the host page's refresh model (no new SSE type). Chips use brand tokens HOLDING=green / NEAR=yellow / TRIGGERED=red. Open the PR with before/after screenshots + test output. Run `pre-pr-checklist`.
