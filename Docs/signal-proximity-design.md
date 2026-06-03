# V1 Signal Proximity Design

**Status:** SIGNED OFF 2026-05-28 — PR-A pending kickoff
**Created:** 2026-05-28
**Owner:** operator (Shaan)
**Implementer:** Claude Code (multi-session, one per PR)
**Precedent:** Mirrors the structure of `Docs/exit-pipeline-design.md` (3-PR plan, COMPLETE 2026-05-27).

> **ADDENDUM 2026-06-02 — entry gate #3 swapped (Hurst → Efficiency Ratio).** This design doc was authored when gate #3 was Hurst persistence (R/S). On 2026-06-02 that gate was replaced by the Kaufman Efficiency Ratio (ER), launched **active at `EFFICIENCY_RATIO_THRESHOLD=0.20`**. Read every "Hurst" / "hurst" / `hurst_*` reference below as the **"Trend Quality" / Efficiency-Ratio gate** (`MarketProximity.efficiency`, `EFFICIENCY_CLOSE_BAND=0.05` ER units, `closest_gate='efficiency'`, DB columns `efficiency_ratio_{value,threshold,state,headroom}`, UI column "Trend Quality"). The proximity machinery (per-gate PASS/CLOSE/FAIL classification, headroom math, sort key, persistence, endpoint, Watching table) is structurally unchanged — only the third gate's identity + close-band + column names changed, plus the raw ER value/threshold are now persisted so the live ER distribution is mineable for calibration. The legacy `'hurst'` `closest_gate` value is retained (API/TS literal + CHECK) for historic rows. See `Docs/decisions-log.md` 2026-06-02 + the gate-swap PR.

## 0. Motivation

The operator wants a "Watching" view on `spratcapital.com/signals` showing how close each market in the V1 universe is to triggering a signal. V1 has three pre-position gates (Donchian breakout, MA trend filter, Hurst persistence); the proximity view surfaces the per-gate state and distance-to-firing so the operator can see at-a-glance which markets are about to fire on the next daily 17:30 ET cycle.

**Non-goal:** intraday/live proximity. V1 evaluates on `Resolution.DAILY` — a live view would not match what LEAN actually decides on. See §1, decision D2.

## 1. Operator Design Decisions (LOCKED for this design)

These were resolved in the design conversation (2026-05-28). Subsequent PRs lock these unless explicitly revisited.

| # | Decision | Rationale |
|---|---|---|
| D1 | Single source of truth for proximity computation = the strategy module. LEAN ships pre-computed headroom values; API + frontend do not recompute. | Eliminates drift between "what we display" and "what LEAN decides on." The gate logic and the proximity-to-gate logic live in the same module. |
| D2 | Refresh cadence: daily, post-LEAN-cycle (~21:30 UTC). No streaming/live tick path. | V1 evaluates `Resolution.DAILY`; live proximity would not match what LEAN decides on. Cost of streaming (new clientId, duplicated math, ongoing complexity) not justified for V1. |
| D3 | Three gates shown: Donchian, Trend, Hurst. Exit-side proximity (trend-flip / MIN_HOLDING_DAYS) is OUT OF SCOPE for V0 of this feature. | Entry-side proximity has clearer operator value (deciding when to be at the desk); exit proximity is a V1 follow-up. |
| D4 | Persistence model: new dedicated `signal_proximity` table. NOT JSONB on `liveness_probes`. | Dedicated table is queryable, indexable, mixes no concerns with the liveness heartbeat. Cost is one migration. |
| D5 | Headroom normalization: per-gate "passing score" in conceptual `[fail, threshold, pass]` shape with raw numeric values shown alongside. See §3.2. | Comparable visual across three dimensionally-different gates without hiding the underlying numbers. |
| D6 | PR-A and PR-B require the `risk-review-approved` label (touches `strategies/v1_trend_following/**` + new Alembic migration). PR-C is frontend-only, no label required. | Per `CLAUDE.md` forbidden-path rules + dev-guide §11 A02. |
| D7 | Frontend refresh: match the existing `/signals` page's refresh model (no new SSE event type). Daily-resolution data; on-load + on-visibility-change refetch is sufficient. | `CLAUDE.md` forbids new SSE event types without enum migration. Daily data doesn't need push. |

## 2. Architecture Diagram

```
┌──────────────────────────────────────────────────────────────────────────┐
│                      lean_local Docker container                         │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │ lean/v1_strategy.py :: on_daily_signal_cycle (17:30 ET = 21:30 UTC)│  │
│  │                                                                    │  │
│  │  ┌──────────────────────────────────────────────────────────────┐  │  │
│  │  │ strategies/v1_trend_following/strategy.py                    │  │  │
│  │  │   V1TrendFollowing.generate_signals(...)                     │  │  │
│  │  │     for each market:                                         │  │  │
│  │  │       snapshot = _compute_snapshot(bars)                     │  │  │
│  │  │       gate_status = _evaluate_market(...)                    │  │  │
│  │  │       eval = MarketEvaluation(snapshot, headroom, status)   │  │  │
│  │  │     returns SignalGenerationResult(                          │  │  │
│  │  │       signals=..., rejections=..., market_evaluations=...)  │  │  │
│  │  └──────────────────────────────────────────────────────────────┘  │  │
│  │                                                                    │  │
│  │  POST /api/internal/lean/signals                                   │  │
│  │    event_type=lean_cycle_heartbeat                                 │  │
│  │    extra={..., "market_evaluations": [...]}    <-- NEW             │  │
│  └────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                         api (FastAPI)                                    │
│  services/api/routes/internal/lean.py::post_lean_signal                  │
│    if event_type == "lean_cycle_heartbeat" and market_evaluations:       │
│      repo.upsert_signal_proximity_rows(cycle_ts, evaluations)            │
│                                                                          │
│  services/api/routes/signals.py::GET /api/signals/proximity              │
│    returns latest row per (market) from signal_proximity table           │
└──────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                       frontend (Next.js)                                 │
│  apps/web/app/signals/page.tsx                                           │
│    "Watching" section — table sorted by closest-to-firing                │
│    Per row: market | Donchian gate | Trend gate | Hurst gate | overall   │
└──────────────────────────────────────────────────────────────────────────┘
```

**Data freshness gate:** LEAN's daily 17:30 ET cycle (= 21:30 UTC) is the only write path. The "Watching" view is therefore daily-resolution; values change once per session.

## 3. Proximity Definition

### 3.1 The three gates (canonical reference)

Per [`strategies/v1_trend_following/strategy.py::_evaluate_market`](strategies/v1_trend_following/strategy.py:178), evaluated in this order:

| Gate | Condition (long) | Condition (short) | V1 default |
|---|---|---|---|
| Donchian | `last_close > donchian_high` | `last_close < donchian_low` | 60-day lookback |
| Trend | `last_close > MA_FAST > MA_SLOW` | `last_close < MA_FAST < MA_SLOW` | 50 / 200 |
| Hurst | `hurst >= HURST_THRESHOLD` | `hurst >= HURST_THRESHOLD` (direction-agnostic) | 0.55 |

A market fires a signal when ALL three pass in the SAME direction. The proximity view shows per-gate state + the per-direction headroom.

### 3.2 Headroom — per-gate definition

Each gate emits two numbers: a numeric "headroom" value AND a categorical state in `{PASS, CLOSE, FAIL}`. The categorical state is what the UI colors; the numeric is what's shown alongside + drives sorting.

**Donchian** — emitted per direction (long + short separately) because the breakout is direction-specific.

- `long_donchian_headroom_pct = (donchian_high - last_close) / last_close`
  - `<= 0` → PASS (broken to upside this session)
  - `0 < x <= 0.01` (within 1%) → CLOSE
  - `> 0.01` → FAIL
- `short_donchian_headroom_pct = (last_close - donchian_low) / last_close`
  - Symmetric to long.

The market's overall Donchian state is the closer of the two; the UI displays both with the closer one highlighted.

**Trend** — also emitted per direction (long trend = `close > MA_FAST > MA_SLOW`; short trend = inverse).

- `long_trend_passing: bool` = `last_close > ma_fast > ma_slow`
- `short_trend_passing: bool` = `last_close < ma_fast < ma_slow`
- `long_trend_closer_gap_pct` = minimum of `(last_close - ma_fast)/last_close` and `(ma_fast - ma_slow)/last_close`; negative when failing. Symmetric for short.

UI shows the boolean prominently + the gap as a secondary number. State:
- PASS if the directional bool is true.
- CLOSE if false but `|closer_gap_pct| <= 0.005` (within 0.5%).
- FAIL otherwise.

**Hurst** — direction-agnostic.

- `hurst_value: Decimal`
- `hurst_headroom = hurst_value - hurst_threshold` (positive = passes, negative = fails)
- State:
  - PASS if `hurst_headroom >= 0.02`.
  - CLOSE if `-0.02 <= hurst_headroom < 0.02`.
  - FAIL if `hurst_headroom < -0.02`.

**Thresholds (0.01, 0.005, 0.02) are configurable** via constants in the proximity module so the operator can retune the CLOSE-band sensitivity without rebuilding LEAN.

### 3.3 Overall headroom + sort key

For each market: `overall_state = min(donchian_state, trend_state, hurst_state)` where the order is `PASS > CLOSE > FAIL` (i.e., overall is the WORST gate). The market's `closest_gate` is the one driving the worst state.

Sort key (for "closest-to-firing" sorting):
- Markets with `overall_state == PASS` already fired this cycle — display in a separate "fired today" group.
- Markets with `overall_state == CLOSE` — primary sort: ascending by the gap on `closest_gate`. These are the "about to fire" rows.
- Markets with `overall_state == FAIL` — secondary sort: by smallest gap to passing on `closest_gate`. The most-likely-to-fire-next-session rows.

### 3.4 Insufficient bar history

When a market has `RejectionReason.INSUFFICIENT_BAR_HISTORY` (fewer than `min_required_bars`), the proximity record is still emitted with `gate_status='warming_up'` and all numeric fields NULL. Frontend renders "Warming up — N daily bars available, need M".

## 4. New Strategy-Side Types

### 4.1 New module: `strategies/v1_trend_following/proximity.py`

Pure-Python, no LEAN imports. Owns the per-gate headroom math + state classification + threshold constants.

```python
from enum import StrEnum
from decimal import Decimal
from dataclasses import dataclass

# Thresholds — operator-tunable via PR (no parameter_set knob in V0).
DONCHIAN_CLOSE_BAND_PCT = Decimal("0.01")    # within 1% = CLOSE
TREND_CLOSE_BAND_PCT = Decimal("0.005")      # within 0.5% = CLOSE
HURST_CLOSE_BAND = Decimal("0.02")            # within 0.02 = CLOSE

class GateState(StrEnum):
    PASS = "pass"
    CLOSE = "close"
    FAIL = "fail"

@dataclass(frozen=True, slots=True)
class GateProximity:
    state: GateState
    headroom: Decimal | None  # None when insufficient data
    detail: str | None         # human-readable, optional

@dataclass(frozen=True, slots=True)
class MarketProximity:
    market: str
    long_donchian: GateProximity
    short_donchian: GateProximity
    long_trend: GateProximity
    short_trend: GateProximity
    hurst: GateProximity
    last_close: Decimal | None
    overall_state: GateState
    closest_gate: str  # 'donchian' | 'trend' | 'hurst' | 'history'
```

Public function:
```python
def compute_market_proximity(
    market: str,
    snapshot: _IndicatorSnapshot | None,  # None when warming up
    hurst_threshold: Decimal,
) -> MarketProximity:
    ...
```

### 4.2 Extension to `SignalGenerationResult` (in `signals.py`)

```python
@dataclass(frozen=True, slots=True)
class SignalGenerationResult:
    signals: tuple[CandidateSignal, ...]
    rejections: tuple[tuple[str, RejectionReason], ...]
    market_evaluations: tuple[MarketProximity, ...]  # NEW
```

**Backwards compatibility:** existing call sites of `generate_signals` continue to work because `market_evaluations` is appended; consumers that don't read it are unaffected. Pre-existing tests stay green.

### 4.3 Modifications to `_evaluate_market` + `generate_signals`

- `_evaluate_market` already computes the snapshot internally (line 216 of `strategy.py`). We expose the snapshot up to the caller by changing the return type from `CandidateSignal | RejectionReason` to a small wrapper dataclass `(outcome, snapshot)`. The strategy's gate logic stays bit-identical.
- `generate_signals` collects the per-market snapshots into a `tuple[MarketProximity, ...]` via `proximity.compute_market_proximity(...)`.

**Risk-review note:** the gate-evaluation logic itself does not change. This PR exposes already-computed values + classifies them. No new gate, no threshold change, no scoring change that affects what fires.

## 5. LEAN Wrapper Changes

[`lean/v1_strategy.py::on_daily_signal_cycle`](lean/v1_strategy.py:417) currently POSTs `lean_cycle_heartbeat` with `signals_emitted_count` + `rejections_count`. Extend the `extra` dict:

```python
self._post_event(
    "lean_cycle_heartbeat",
    extra={
        "session_date_et": session_date.isoformat(),
        "equity_usd": str(equity),
        "live_mode": bool(self.live_mode),
        "signals_emitted_count": signals_count,
        "rejections_count": rejections_count,
        "market_evaluations": [   # NEW
            {
                "market": ev.market,
                "long_donchian": {"state": ev.long_donchian.state.value,
                                  "headroom": str(ev.long_donchian.headroom) if ev.long_donchian.headroom is not None else None},
                # ... etc per gate
                "last_close": str(ev.last_close) if ev.last_close else None,
                "overall_state": ev.overall_state.value,
                "closest_gate": ev.closest_gate,
            }
            for ev in result.market_evaluations
        ],
    },
)
```

Payload size: ~10 markets × ~200 bytes = ~2 KB per heartbeat. Negligible.

## 6. API Changes

### 6.1 Pydantic schema update — `services/api/schemas/lean.py`

Add an optional `market_evaluations` field to `LeanEventRequest`. List of structured objects matching the LEAN-side dict. This MUST land in PR-A (alongside the LEAN emission) so heartbeats with the new field don't 422 the existing endpoint.

### 6.2 New Alembic migration — `signal_proximity` table

```sql
CREATE TABLE signal_proximity (
    id              BIGSERIAL PRIMARY KEY,
    cycle_ts_utc    TIMESTAMPTZ NOT NULL,
    session_date_et DATE NOT NULL,
    market          TEXT NOT NULL,

    -- raw snapshot values (NULL when warming up)
    last_close      NUMERIC,
    donchian_high   NUMERIC,
    donchian_low    NUMERIC,
    ma_fast         NUMERIC,
    ma_slow         NUMERIC,
    hurst_value     NUMERIC,
    hurst_threshold NUMERIC,
    atr             NUMERIC,

    -- per-gate state + headroom
    long_donchian_state         TEXT NOT NULL,   -- 'pass'|'close'|'fail'|'warming_up'
    long_donchian_headroom_pct  NUMERIC,
    short_donchian_state        TEXT NOT NULL,
    short_donchian_headroom_pct NUMERIC,
    long_trend_state            TEXT NOT NULL,
    long_trend_gap_pct          NUMERIC,
    short_trend_state           TEXT NOT NULL,
    short_trend_gap_pct         NUMERIC,
    hurst_state                 TEXT NOT NULL,
    hurst_headroom              NUMERIC,

    -- overall
    overall_state               TEXT NOT NULL,
    closest_gate                TEXT NOT NULL,
    gate_status                 TEXT NOT NULL,   -- 'pass'|'warming_up'|'failed_donchian'|...

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_signal_proximity_market_cycle
    ON signal_proximity (market, cycle_ts_utc DESC);
CREATE INDEX idx_signal_proximity_cycle_ts
    ON signal_proximity (cycle_ts_utc DESC);
```

**Retention:** keep all rows. Volume = ~10 rows/day. Negligible storage.

### 6.3 Heartbeat-handler change in `services/api/routes/internal/lean.py`

When `event_type == "lean_cycle_heartbeat"` and `market_evaluations` is present, INSERT rows into `signal_proximity`. Best-effort: a write failure logs + does NOT 5xx the heartbeat (so a downstream DB issue doesn't take down the LEAN cycle).

**Audit-first ordering note (per `feedback_audit_first_ordering`):** proximity rows are observational, NOT state-change. They do NOT require an audit_log entry before the write — they ARE the audit trail for the daily evaluation. Confirm this with operator before PR-B merges.

### 6.4 New endpoint — `GET /api/signals/proximity`

Returns latest row per market. Lives in `services/api/routes/signals.py` (existing file) or a new module — to be decided in PR-B based on whether `signals.py` is on the forbidden list. (`services/signal/**` is forbidden; `services/api/routes/signals.py` is NOT — it's an api route, not signal-engine code. Confirm at implementation time.)

Response shape:
```json
{
  "as_of_cycle_ts_utc": "2026-05-28T21:30:00Z",
  "markets": [
    {
      "market": "/MES",
      "overall_state": "close",
      "closest_gate": "donchian",
      "last_close": "5234.50",
      "long_donchian": {"state": "close", "headroom_pct": "0.012"},
      "short_donchian": {"state": "fail", "headroom_pct": "0.084"},
      "long_trend": {"state": "pass", "gap_pct": "0.018"},
      "short_trend": {"state": "fail", "gap_pct": "-0.018"},
      "hurst": {"state": "pass", "headroom": "0.03", "value": "0.58"},
      "gate_status": "warming_up_failed_donchian"
    }
  ]
}
```

## 7. Frontend Changes

### 7.1 New "Watching" section on `/signals`

Sits above the existing pending-signals list. Always visible (even when empty: shows "No markets being watched" instead of disappearing — the operator should see the system IS evaluating).

Per row:
- Symbol
- Three gate chips/bars (Donchian / Trend / Hurst) with PASS=green / CLOSE=yellow / FAIL=red
- Numeric headroom shown inside or below the chip
- "Closest gate" column highlighting the limiting condition
- Last close + cycle-ts-utc shown as a small footer per row

### 7.2 Refresh model

Match the existing `/signals` page refresh model (per D7). If the page already uses SSE for pending signals, hook into the same channel (no new event type — the existing channel just triggers a refetch). If polling, refetch on the same cadence. Resolved at PR-C implementation time by reading the existing page code.

### 7.3 Empty / error states

- **No proximity data yet today** (e.g., LEAN hasn't fired its first cycle since deploy): show "Waiting for first LEAN cycle today (next at 21:30 UTC)".
- **API error**: existing error-boundary UX.
- **Warming-up markets**: show in their own group at the bottom of the table.

## 8. Open Question Resolutions

### Q1: Do we surface direction-specific headroom (long + short separately) or just the closer one?

**LOCKED: BOTH.** Per §3.2, every gate emits long-direction and short-direction headroom independently. UI defaults to showing the closer side but reveals both on hover/expansion. Rationale: a market in a strong long trend but close to a downside Donchian break (rare but possible) needs both views.

### Q2: Should proximity rows go through `audit_log` like signal_emitted does?

**LOCKED: NO.** Proximity is observational, not a state-change. The `signal_proximity` table IS the durable audit trail for "what LEAN saw on each cycle." Confirm with operator before PR-B merges.

### Q3: Should the proximity computation also run in the api as a "shadow" for cross-checking?

**LOCKED: NO.** Per D1, single source of truth = strategy module. A shadow computation would re-introduce the drift risk we just designed away.

### Q4: What happens when `STRATEGY_DECOMMISSIONED=True`?

**LOCKED: ROWS STILL EMITTED with `gate_status='decommissioned'`.** The operator still wants to see what the gates WOULD have said if the strategy were active. The frontend renders these with a banner "Strategy decommissioned — view-only".

### Q5: Threshold tuning (CLOSE-band widths) — operator-tunable at runtime or PR-only?

**LOCKED: PR-ONLY for V0.** Constants in `proximity.py`. If operator wants to retune the CLOSE band, that's a small PR (no risk-review needed; pure UX). V1 (post-cutover) can add a `parameter_set` knob.

### Q6: Backfill — do we backfill historical proximity from existing LEAN logs?

**LOCKED: NO.** The migration creates an empty table; data starts flowing from the first heartbeat post-PR-A deploy. Historical proximity would require re-running strategy.py against historical bars, which is doable but out of scope for V0.

## 9. PR Breakdown

### PR-A: Strategy + LEAN emission (+ API tolerates new field)

**Scope:**
- New: `strategies/v1_trend_following/proximity.py` (pure-Python, ~200 LoC + tests).
- Modify: `strategies/v1_trend_following/signals.py` — add `market_evaluations` field to `SignalGenerationResult`. Add `MarketProximity` import. Re-export from `__init__.py`.
- Modify: `strategies/v1_trend_following/strategy.py` — modify `_evaluate_market` to expose snapshot; populate `market_evaluations` in `generate_signals`. Gate logic UNCHANGED.
- Modify: `lean/v1_strategy.py::on_daily_signal_cycle` — attach `market_evaluations` to the heartbeat POST.
- Modify: `services/api/schemas/lean.py::LeanEventRequest` — add optional `market_evaluations` field (no persistence yet).
- Modify: `services/api/routes/internal/lean.py` — heartbeat handler accepts the new field but does NOT persist it yet (logs the count as a sanity check).
- Tests:
  - `tests/unit/test_v1_signal_proximity.py` — per-gate state classification, headroom math, edge cases (insufficient history, decommissioned, exact-threshold ties).
  - `tests/unit/test_v1_signals.py` — assert `market_evaluations` is populated end-to-end for the existing test universe.
  - `tests/unit/test_lean_event_request.py` — Pydantic accepts the new field.

**Requires:** `risk-review-approved` label (touches `strategies/v1_trend_following/**`).

**Deployable independently:** YES. The new field flows through LEAN → API → /dev/null. No frontend impact.

**Acceptance gate:** After deploy, the next LEAN cycle's heartbeat carries `market_evaluations` and the api logs `lean_proximity_received market_count=N` at INFO. Verified via the `eod-recon` ceremony.

### PR-B: API persistence + endpoint

**Scope:**
- New: `alembic/versions/000X_signal_proximity.py` (the table from §6.2).
- New: `services/api/repositories/signal_proximity.py` — insert + latest-per-market query.
- New: `services/api/schemas/signal_proximity.py` — response model.
- New: `services/api/routes/signals.py::proximity_router` (or new module `signals_proximity.py` if conflict-avoidance demands).
- Modify: `services/api/routes/internal/lean.py` — heartbeat handler INSERTS rows when `market_evaluations` is present.
- Tests:
  - `tests/integration/test_signal_proximity_persistence.py` — heartbeat with evaluations → rows landed.
  - `tests/integration/test_signal_proximity_endpoint.py` — `GET /api/signals/proximity` returns latest-per-market.

**Requires:** `risk-review-approved` label (Alembic migration).

**Deployable independently:** YES, but only meaningful AFTER PR-A is in production (otherwise the heartbeat carries nothing to persist).

**Acceptance gate:** After deploy, the next LEAN cycle results in 10 rows in `signal_proximity`. `GET /api/signals/proximity` returns a non-empty array.

### PR-C: Frontend "Watching" section

**Scope:**
- New: `apps/web/components/signals/WatchingSection.tsx` (or similar — exact path resolved at PR-C time).
- New: `apps/web/lib/api/signals-proximity.ts` — typed fetch client.
- Modify: `apps/web/app/signals/page.tsx` — render `<WatchingSection />` above existing pending-signals list.
- Tests: component-level + page-level.

**Requires:** No label (frontend-only).

**Deployable independently:** YES, but only meaningful AFTER PR-B.

**Acceptance gate:** Operator opens `/signals` post-LEAN-cycle; "Watching" section renders all 10 markets in the V1 universe with at-a-glance per-gate state. The closest-to-firing market is at the top.

## 10. Test Plan

### 10.1 Strategy unit tests (PR-A)

- Donchian PASS / CLOSE / FAIL classification with exact-threshold ties.
- Trend filter PASS with `last_close > ma_fast > ma_slow` (long); FAIL when MA_FAST < MA_SLOW.
- Hurst PASS / CLOSE / FAIL with threshold=0.55, value=0.57 (PASS), 0.56 (CLOSE), 0.52 (FAIL).
- Insufficient history → `gate_status='warming_up'` + all numeric fields None.
- Decommissioned → `gate_status='decommissioned'` + headroom values STILL POPULATED (per Q4).
- Same-direction position → existing reject reason preserved; proximity STILL emitted.
- Property-based: for any synthetic snapshot, `overall_state == worst_gate_state`.

### 10.2 LEAN-wrapper test (PR-A)

Mock the strategy result + assert the heartbeat POST body contains a well-formed `market_evaluations` array. Existing LEAN tests cover the rest of the cycle.

### 10.3 Integration tests (PR-B)

- Heartbeat with 10 markets → 10 rows in `signal_proximity` with correct column values.
- Two consecutive heartbeats → both row sets persisted (no UPSERT-clobbering).
- `GET /api/signals/proximity` returns latest-per-market, ordered by `closest-to-firing` per §3.3.

### 10.4 E2E smoke (post PR-C, paper-only)

After the next 21:30 UTC LEAN cycle:
1. `GET /api/signals/proximity` returns ≥7 markets (the 6 active futures + 4 ETFs minus any warming-up).
2. `/signals` page renders the "Watching" section.
3. Closest-to-firing market is at the top.
4. Hurst gate state on /MES matches the value visible in the LEAN log line `v1_signal_rejected market=/MES reason=hurst_below_threshold` (or `signal_emitted` if it fired).

## 11. Risks + Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Strategy module change drifts from `_evaluate_market`'s gate logic | Display lies; operator misled | PR-A includes a test that asserts: for every rejection emitted by `_evaluate_market`, the corresponding `MarketProximity.overall_state == FAIL` and `closest_gate` matches the rejection reason. |
| Heartbeat payload bloat | LEAN cycle latency increase | Payload is ~2 KB. Negligible vs the existing heartbeat (~500 B). Monitor cycle-duration log line; no action needed unless > 5% regression. |
| API write fails during heartbeat | Proximity data missing for a day | Best-effort write (per §6.3); LEAN cycle stays clean. Next day's cycle backfills. Document recovery as "wait for next cycle" in the eod-recon runbook. |
| Frontend over-polls | Backend load | Daily-resolution data + on-load + on-visibility-change refetch only. No periodic polling. |
| Operator confusion: "fired today" vs "watching" | Display ambiguity | UI clearly separates: "Fired today" group (PASS) at top; "Watching" group (CLOSE) below; "Inactive" group (FAIL) collapsed by default. |

## 12. Out of Scope (Deferred)

- **Exit-side proximity** — how close held positions are to trend-flip / MIN_HOLDING_DAYS exit. V1 follow-up.
- **Live / intraday proximity** — would require new clientId on ib_gateway + duplicated math. Not justified for V1.
- **Operator-tunable CLOSE bands** — V0 has hardcoded 1% / 0.5% / 0.02 constants. V1 can add a `parameter_set` knob.
- **Historical proximity backfill** — start empty; data flows from PR-A deploy onward.
- **Mobile responsiveness optimization** — V0 inherits whatever the `/signals` page does today.
- **Per-market proximity history charts** — V0 shows latest only. V1 can add a sparkline of headroom over time.

## 13. Appendix: Files Touched Summary

| Path | PR | Change |
|---|---|---|
| `strategies/v1_trend_following/proximity.py` | A | NEW (pure-Python module) |
| `strategies/v1_trend_following/signals.py` | A | Add `market_evaluations` field to `SignalGenerationResult` |
| `strategies/v1_trend_following/strategy.py` | A | Expose snapshot from `_evaluate_market`; populate evaluations in `generate_signals` |
| `strategies/v1_trend_following/__init__.py` | A | Re-export new types |
| `lean/v1_strategy.py` | A | Attach `market_evaluations` to heartbeat POST |
| `services/api/schemas/lean.py` | A | Add optional `market_evaluations` field to `LeanEventRequest` |
| `services/api/routes/internal/lean.py` | A | Log count; B: persist rows |
| `tests/unit/test_v1_signal_proximity.py` | A | NEW |
| `tests/unit/test_v1_signals.py` | A | Extend coverage |
| `tests/unit/test_lean_event_request.py` | A | New field tolerance |
| `alembic/versions/000X_signal_proximity.py` | B | NEW migration |
| `services/api/repositories/signal_proximity.py` | B | NEW |
| `services/api/schemas/signal_proximity.py` | B | NEW |
| `services/api/routes/signals.py` (or new module) | B | NEW endpoint |
| `tests/integration/test_signal_proximity_persistence.py` | B | NEW |
| `tests/integration/test_signal_proximity_endpoint.py` | B | NEW |
| `apps/web/components/signals/WatchingSection.tsx` | C | NEW |
| `apps/web/lib/api/signals-proximity.ts` | C | NEW |
| `apps/web/app/signals/page.tsx` | C | Add `<WatchingSection />` |
| `Docs/decisions-log.md` | A, B, C | Append per-PR-merge entry |
| `Docs/file-index.md` | A, B, C | Update touched-files rows |

## 14. Sign-off Checklist — SIGNED OFF 2026-05-28

Operator confirms before PR-A kickoff:

- [x] D1-D7 in §1 are accepted (or specific changes noted).
- [x] Q1-Q6 in §8 are accepted.
- [x] PR-A / PR-B / PR-C split in §9 is accepted.
- [x] `risk-review-approved` label will be applied to PR-A and PR-B.
- [x] CLOSE-band thresholds (1% Donchian / 0.5% Trend / 0.02 Hurst) are accepted as V0 defaults.

## 15. PR-A Kickoff Prompt (for next Claude Code session)

> Read `Docs/signal-proximity-design.md` end-to-end. Implement PR-A per §9. Constraints:
>
> - Touch only the files listed in §13 for PR-A.
> - The gate-evaluation logic in `_evaluate_market` MUST stay bit-identical — this PR exposes already-computed values + classifies them, nothing else.
> - The new `proximity.py` module must have zero LEAN imports — pure-Python, pytest-runnable from the project root.
> - The Pydantic schema update in `LeanEventRequest` must accept the new field as optional + ignore it gracefully when absent (existing heartbeats from older LEAN deploys must NOT 422).
> - Apply `risk-review-approved` label on the PR.
> - Open the PR with a plain-English summary, risk-impact statement ("zero change to entry-signal logic; observation-only data exposure"), and a paste of the new test output.
> - Reference `Docs/signal-proximity-design.md` in the PR body.
>
> Run `pre-pr-checklist` before pushing.

## 16. PR-B Kickoff Prompt (for next Claude Code session, after PR-A merges)

> Read `Docs/signal-proximity-design.md` end-to-end. Implement PR-B per §9. Constraints:
>
> - PR-A is already merged + deployed; the api is logging `lean_proximity_received market_count=N` on each heartbeat.
> - Touch only the files listed in §13 for PR-B.
> - The Alembic migration is the canonical schema source; no inline `CREATE TABLE` elsewhere.
> - Best-effort writes: a DB failure logs and returns 202 — does NOT 5xx the heartbeat (per §6.3).
> - The new endpoint `GET /api/signals/proximity` must enforce the same auth as `GET /api/signals` (existing pattern in `signals.py` — match it).
> - Apply `risk-review-approved` label on the PR.
> - Open the PR with a plain-English summary, the migration SQL pasted into the body, and a paste of the new test output.
>
> Run `pre-pr-checklist` before pushing.

## 17. PR-C Kickoff Prompt (for next Claude Code session, after PR-B merges)

> Read `Docs/signal-proximity-design.md` end-to-end. Implement PR-C per §9. Constraints:
>
> - PR-B is already merged + deployed; `GET /api/signals/proximity` returns latest-per-market.
> - Touch only the files listed in §13 for PR-C.
> - The "Watching" section sits above the existing pending-signals list on `/signals`.
> - Match the existing page's refresh model (no new SSE event type — per D7).
> - Per-gate chips use the existing brand color tokens for PASS (green) / CLOSE (yellow) / FAIL (red).
> - Open the PR with a plain-English summary, before/after screenshots of `/signals`, and a paste of the new test output.
>
> Run `pre-pr-checklist` before pushing.
