# V1 Exit Pipeline Design

> **Status:** DESIGN ONLY. No code changes in this PR. Implementation
> lands across 2–3 follow-up sessions per the PR breakdown in §10.
> Authored 2026-05-26 against the post-pivot Phase 1 architecture
> (paper trading Day ~7). Live-money cutover target ~2026-06-30.

> **Context:** `strategies/v1_trend_following/strategy.py::V1TrendFollowing.generate_exit_candidates`
> is currently a `NotImplementedError` stub. Today's only exit path is
> the bracket stop-market hitting at its ATR-based level. Per
> `Docs/backend-spec.md §2.3`, V1 requires four exit conditions:
> (a) stop hit, (b) signal reversal, (c) MIN_HOLDING_DAYS satisfied AND
> trend filter flips, (d) strategy decommission. Only (a) works today.
> This doc designs the implementation of (b), (c), (d).

> **Reading order:** §1–§4 (architecture + types) is canonical. §5–§7 is
> the implementation contract. §8 (open questions) needs operator answers
> before PR-1 can start. §9–§12 is operational planning.

---

## 1. Operator Design Decisions (LOCKED for this session)

These are the constraints implementation must respect. They came from
the operator brief 2026-05-26 and are not re-litigated below.

| # | Topic | Decision |
|---|---|---|
| L1 | Reversal exit | Close existing + open opposite, ONLY if all 3 entry filters (Donchian + trend + Hurst) confirm the opposite direction. Turtle-flip variant. |
| L2 | Trend-flip exit threshold | Lazy interpretation: `close < MA_FAST` for held LONG (mirror for SHORT). NOT the full death cross. MIN_HOLDING_DAYS gate still applies. |
| L3 | Approval flow V1 | Operator-approval (matches entries). Defer auto-approve to V2. |
| L4 | Approval flow V2 | `EXIT_AUTO_APPROVE` parameter (agent-mutable in the safe direction). Default False at launch; flip after 10+ clean operator-approved exits. |
| L5 | HALT_NEW behavior | Exits proceed normally during HALT_NEW. Per backend-spec §2.5: HALT_NEW blocks new entries but allows exits. |
| L6 | Decommission | `STRATEGY_DECOMMISSIONED: bool` parameter. When True, exit pipeline emits CLOSE signals for every held position regardless of indicator state. |
| L7 | Exit ≠ resize | Stage 1–3 sizing shrinks (per backend-spec §2.4.1) are distinct from exits. Exit = full close. Resize = partial close to new target. This doc covers exits only. |

---

## 2. Architecture Diagram

```
                                  21:30 UTC daily
                                        |
                                        v
+--------------------------+    +-----------------------+
| lean_local container     |    | Operator (off-cycle)  |
| on_daily_signal_cycle()  |    | trigger_v1_cycle tool |
+------------+-------------+    +----------+------------+
             |                              |
             |  generate_signals (entries)  |
             |  generate_exit_candidates    |   <-- NEW
             |                              |
             v                              v
+--------------------------------------------------------+
| POST /api/internal/lean/signals  (shared-bearer auth)  |
| accepts signal_type in {"entry","exit"}    <-- NEW     |
+----------------------+---------------------------------+
                       |
                       v
+--------------------------------------------------------+
| services/qc_adapter/signal_ingestion.py                |
|   ingest_signal_emitted(...)                           |
|   - audit-first: append_audit_event(SIGNAL_EMITTED)    |
|   - INSERT signals row with signal_type from payload   |
|   - signal_type plumbed through (was hard-coded entry) |
+----------------------+---------------------------------+
                       |
                       v
+--------------------------------------------------------+
| services/risk/signal_dispatch.py                       |
|   apply_signal_dispatch(plan, ...)                     |
|   - HALT_NEW gates approve(entry); allows approve(exit)|
|   - audit-first: SIGNAL_APPROVED / REJECTED / DEFERRED |
|   - UPDATE signals.status                              |
+----------------------+---------------------------------+
                       |
                       v
+--------------------------------------------------------+
| services/risk/order_placement_worker.py                |
|   run_once() polls for status='approved'               |
|                                                        |
|   For signal_type='entry':  [TODAY'S PATH, unchanged]  |
|     - INSERT entry orders row (pre-place)              |
|     - place_order(entry, transmit=False)               |
|     - INSERT stop orders row (pre-place)               |
|     - place_order(stop, transmit=True, parentId=entry) |
|     - 2x ORDER_PLACED audit + UPDATE rows              |
|                                                        |
|   For signal_type='exit':   [NEW PATH]                 |
|     - SELECT open bracket-stop for entry_signal_id     |
|     - cancel_order(bracket_stop.client_order_id)       |
|     - ORDER_CANCELLED audit                            |
|     - dispatcher-side sizing:                          |
|         qty = abs(current_position.quantity)           |
|         side = opposite(current_position.direction)    |
|     - INSERT close orders row (pre-place)              |
|     - place_order(close, transmit=True, no parent)     |
|     - ORDER_PLACED audit + UPDATE close row            |
|     - UPDATE signals.status='working'                  |
+----------------------+---------------------------------+
                       |
                       v
+--------------------------------------------------------+
| IBKR fill stream → services/risk/fill_processor.py     |
|   _classify_fill_scenario(...)                         |
|   - prior_position exists                              |
|   - fill_direction is opposite                         |
|   - fill_quantity == |prior_position.quantity|         |
|   → EXIT_FULL_CLOSE                                    |
|                                                        |
|   ORDER_FILLED → POSITION_CLOSED →                     |
|   BALANCE_SNAPSHOT_RECORDED → TRADE_CLOSED             |
+--------------------------------------------------------+
```

Key invariants the design preserves:

- **Same daily cycle**: entries and exits both run inside the LEAN
  21:30 UTC cycle (and inside the operator trigger tool). Two strategy
  method calls, two HTTP POSTs per signal, same audit chain.
- **Same signals table**: one row per signal regardless of type. The
  new `signal_type` column value (`entry` vs `exit`) discriminates.
  Status flow (`pending → approved/rejected/deferred → working → filled`)
  is identical for both.
- **Audit-first preserved**: audit row commits BEFORE signals row
  UPDATE in dispatch path. ORDER_CANCELLED commits BEFORE
  cancel_order is acked by IBKR (we record intent, then act). This
  matches backend-spec §2.10.1 and `feedback_audit_first_ordering`.
- **`fill_processor` is exit-aware today**: the EXIT_FULL_CLOSE branch
  already exists. It currently works for bracket-stop fills (where the
  stop reuses the entry's `signal_id` per the Option B contract). For
  explicit-close fills (the new exit pipeline's output), it works
  unchanged because the classifier keys off prior_position vs fill
  direction/quantity, not signal_id. The lookup-by-CID resolves to the
  exit signal_id, and the trade closure correctly references the
  original entry's trade row via `prior_trade` (which is found by
  market+account+state='open_position', not signal_id).

---

## 3. Exit Conditions (canonical reference)

Per `Docs/backend-spec.md §2.3` plus operator decisions L1–L6:

| # | Exit reason | Trigger condition | Sizing | Cancel bracket-stop? | Auto-emit follow-on entry? |
|---|---|---|---|---|---|
| (a) | `stop_hit` | Bracket stop-market fires at IBKR | Bracket itself | N/A (bracket fills) | No |
| (b) | `reversal` | Held position + opposite direction passes ALL 3 entry filters (Donchian + trend + Hurst) on a single market+session | Full close of prior | Yes | Yes — entry pipeline emits the new direction in the SAME cycle (see Q1) |
| (c) | `trend_flip` | Held LONG + `last_close < ma_fast` (mirror for SHORT) AND held days ≥ MIN_HOLDING_DAYS | Full close of prior | Yes | No |
| (d) | `decommission` | `STRATEGY_DECOMMISSIONED=True` AND position is held | Full close of prior | Yes | No |

**Notes:**

- (a) is unchanged by this design. The fill_processor's EXIT_FULL_CLOSE
  path already handles it via the bracket parentId contract. The
  bracket-cancel step in §2 only applies to (b), (c), (d) where a NEW
  exit signal is emitted.
- (b) requires the FULL entry-filter pipeline to fire in the opposite
  direction. Donchian-alone is insufficient (per L1: "one signal and
  not enough"). The reversal exit thus comes "for free" from the
  existing `_evaluate_market` finding a candidate in the opposite
  direction — the exit pipeline only needs to observe that finding and
  emit the corresponding CLOSE.
- (c) and (d) are pure exit-side conditions; the entry pipeline does
  not produce candidates for them.
- (b), (c), (d) are evaluated INDEPENDENTLY per market per cycle. A
  position can satisfy multiple at once (e.g., reversal AND trend-flip
  for a LONG that crashed below MA_FAST and the SHORT direction now
  passes all filters). Precedence: reversal > decommission > trend_flip
  (reversal triggers a follow-on entry; decommission overrides all
  indicator state; trend_flip is the "softer" close). At most ONE exit
  signal per market per cycle is emitted (the highest-precedence one),
  with the chosen `exit_reason` reflected in the audit payload.

---

## 4. Pseudocode: `generate_exit_candidates`

The strategy-side surface. Pure-policy; no I/O. Mirrors the structure
of `generate_signals` so the LEAN wrapper + `trigger_v1_cycle` consume
it identically.

```python
def generate_exit_candidates(
    self,
    *,
    active_universe: Mapping[str, BarSeries],
    current_positions: Mapping[str, Position],
    as_of_session_date: date,
    as_of_emitted_at_utc: datetime | None = None,
    entry_candidates: tuple[CandidateSignal, ...] = (),  # for reversal coupling
) -> ExitGenerationResult:
    """
    For each held position, evaluate (b), (c), (d). Emit at most one
    CandidateSignal per market with signal_type='exit' and exit_reason
    in {'reversal','trend_flip','decommission'}.
    """
    emitted_at = as_of_emitted_at_utc or datetime.now(tz=UTC)
    exit_signals: list[CandidateSignal] = []
    exit_rejections: list[tuple[str, RejectionReason]] = []

    # Pre-compute the set of markets where entry_candidates emitted an
    # opposite-direction breakout. (b) couples to this.
    reversing_entries_by_market = {
        c.market: c for c in entry_candidates
        if (mkt_pos := current_positions.get(c.market)) is not None
        and mkt_pos.direction is not Direction.FLAT
        and mkt_pos.direction is not c.direction
    }

    for market, position in current_positions.items():
        if position.direction is Direction.FLAT:
            continue  # nothing to exit

        # --- (d) decommission — highest override ---
        if self._params.strategy_decommissioned:
            exit_signals.append(self._build_exit_candidate(
                market=market,
                position=position,
                as_of_session_date=as_of_session_date,
                exit_reason="decommission",
                series=active_universe.get(market),
            ))
            continue

        # --- (b) reversal — second precedence ---
        if market in reversing_entries_by_market:
            entry_candidate = reversing_entries_by_market[market]
            exit_signals.append(self._build_exit_candidate(
                market=market,
                position=position,
                as_of_session_date=as_of_session_date,
                exit_reason="reversal",
                series=active_universe.get(market),
                paired_entry_market=entry_candidate.market,
            ))
            continue

        # --- (c) trend_flip — third precedence ---
        series = active_universe.get(market)
        if series is None or len(series.bars) < self._min_required_bars:
            exit_rejections.append((market, RejectionReason.INSUFFICIENT_BAR_HISTORY))
            continue

        # MIN_HOLDING_DAYS gate — held days must satisfy the floor before
        # trend_flip can fire. Matches L2 + backend-spec §2.3.
        if position.opened_at_session_date is not None:
            held = (as_of_session_date - position.opened_at_session_date).days
            if held < self._params.min_holding_days:
                exit_rejections.append(
                    (market, RejectionReason.MIN_HOLDING_NOT_REACHED)
                )
                continue
        # If opened_at is None, conservatively skip the floor check
        # (matches strategy.py:_evaluate_market behavior on missing data).

        snapshot = self._compute_snapshot(series.bars)
        last_close = snapshot.last_close

        trend_flipped = False
        if position.direction is Direction.LONG:
            # L2: close < MA_FAST is the trigger (NOT close < MA_SLOW
            # and NOT the death cross MA_FAST < MA_SLOW).
            trend_flipped = last_close < snapshot.ma_fast
        elif position.direction is Direction.SHORT:
            trend_flipped = last_close > snapshot.ma_fast

        if not trend_flipped:
            exit_rejections.append((market, RejectionReason.TREND_HOLDS))
            continue

        exit_signals.append(self._build_exit_candidate(
            market=market,
            position=position,
            as_of_session_date=as_of_session_date,
            exit_reason="trend_flip",
            series=series,
            snapshot=snapshot,
        ))

    return ExitGenerationResult(
        signals=tuple(exit_signals),
        rejections=tuple(exit_rejections),
        as_of_emitted_at_utc=emitted_at,
    )


def _build_exit_candidate(
    self,
    *,
    market: str,
    position: Position,
    as_of_session_date: date,
    exit_reason: Literal["reversal","trend_flip","decommission"],
    series: BarSeries | None,
    snapshot: _IndicatorSnapshot | None = None,
    paired_entry_market: str | None = None,
) -> CandidateSignal:
    """Build a CandidateSignal with signal_type='exit'.

    Pricing convention: decision_price = last_close of the market's series
    if available; falls back to position.avg_cost if series is None
    (decommission corner case where the universe doesn't include the
    market anymore, e.g., delisted).
    stop_price: Decimal('0') — NOT MEANINGFUL for exits. The dispatcher
    does NOT place a new bracket stop for exit orders.
    direction: Direction.FLAT — sentinel meaning "target ending position
    is flat." The order_placement_worker computes the actual buy/sell
    side from current_positions at dispatch time.
    """
    decision_price = series.bars[-1].close if series else position.avg_cost
    indicators = (
        {
            "ma_fast": snapshot.ma_fast,
            "ma_slow": snapshot.ma_slow,
            "last_close": snapshot.last_close,
            "atr": snapshot.atr,
        }
        if snapshot is not None
        else {}
    )
    return CandidateSignal(
        market=market,
        direction=Direction.FLAT,
        signal_type="exit",
        exit_reason=exit_reason,           # NEW FIELD
        prior_position_direction=position.direction,  # NEW FIELD
        prior_position_quantity=position.quantity,    # NEW FIELD
        session_date=as_of_session_date,
        decision_price=decision_price,
        stop_price=Decimal("0"),
        indicators_snapshot=indicators,
        paired_entry_market=paired_entry_market,  # NEW FIELD; reversal only
    )
```

---

## 5. New Strategy-Side Types

### 5.1 New `RejectionReason` enum values

Added to `strategies/v1_trend_following/signals.py::RejectionReason`:

| Value | Used by | Meaning |
|---|---|---|
| `TREND_HOLDS` | (c) trend_flip | Held position; `close` on the right side of `MA_FAST`; no exit signal |
| `MIN_HOLDING_NOT_REACHED` | (c) trend_flip | Held duration < MIN_HOLDING_DAYS; exit deferred until floor met |
| `NO_EXIT_CONDITION_MET` | future use | Sentinel for "evaluated but neither (b), (c), nor (d) tripped" — currently the absence of an emitted signal IS the rejection so this may not be needed at V1; keep slot reserved |

We do NOT add a `NO_REVERSAL_CONFIRMATION` reason because the exit
pipeline never explicitly evaluates "would the opposite direction
fire?" — it observes the entry pipeline's output. If the entry pipeline
rejected the opposite direction (e.g., HURST_BELOW_THRESHOLD), that
rejection is already recorded against the entry-side market evaluation.
Recording it a second time on the exit side would double-count.

### 5.2 New `CandidateSignal` fields

`strategies/v1_trend_following/signals.py::CandidateSignal` adds:

```python
@dataclass(frozen=True, slots=True)
class CandidateSignal:
    market: str
    direction: Direction
    signal_type: Literal["donchian_breakout", "exit"]  # WIDENED LITERAL
    # NEW: only populated when signal_type == 'exit'
    exit_reason: Literal["reversal", "trend_flip", "decommission"] | None = None
    # NEW: snapshot of position state at signal-emit time, for dispatcher
    prior_position_direction: Direction | None = None
    prior_position_quantity: int | None = None
    # NEW: reversal-only — the entry-side market that paired with this exit
    paired_entry_market: str | None = None
    session_date: date
    decision_price: Decimal
    stop_price: Decimal
    indicators_snapshot: dict[str, Decimal | int]
```

Reasoning for `signal_type` widening over a new enum: the existing
`signal_type` column is `TEXT NOT NULL` (no DB CHECK constraint per
backend-spec §3.3); widening the Literal is purely a type-level
change. No alembic migration needed.

### 5.3 New `ExitGenerationResult` type

Mirrors `SignalGenerationResult` for symmetry; lets the LEAN wrapper and
trigger_v1_cycle handle exits with the same fan-out shape:

```python
@dataclass(frozen=True, slots=True)
class ExitGenerationResult:
    signals: tuple[CandidateSignal, ...]
    rejections: tuple[tuple[str, RejectionReason], ...]
    as_of_emitted_at_utc: datetime
```

### 5.4 New `V1Parameters` fields

Added to `strategies/v1_trend_following/parameters.py::V1Parameters`:

```python
strategy_decommissioned: bool = False    # NEW; default False
exit_auto_approve: bool = False          # NEW; default False
```

Both keys added to `V1_DEFAULTS` dict and `to_canonical_dict`. Both
agent-mutable in the SAFE direction (decommission can be set True by
operator only; auto-approve can be set False by agent — tighter risk
control — and True ONLY by operator).

**`parameters.py` is A02-listed → PR requires `risk-review-approved`.**

---

## 6. Approval Flow Design

### 6.1 V1 (operator-approval, mirrors entries)

Exit signals flow through the existing dispatch state machine:

```
LEAN/trigger emits signal_emitted (signal_type='exit')
    ↓
ingest_signal_emitted writes audit + signals row (status='pending')
    ↓
Operator sees row at /signals page; clicks approve/reject/defer
    ↓
apply_signal_dispatch:
    if action == 'approve':
        risk_state == 'HALT_NEW'? → reject IF signal_type == 'entry'
                                  → ALLOW   IF signal_type == 'exit' [NEW]
        else:
            audit SIGNAL_APPROVED + UPDATE signals.status='approved'
    if action in ('reject','defer'):
        always allowed (per existing logic; diary entry required)
    ↓
order_placement_worker picks up approved signal → close path
```

Critical change: `apply_signal_dispatch`'s HALT_NEW gate at
`services/risk/signal_dispatch.py:379-390` currently rejects ALL
approve actions when `current_risk_state == 'HALT_NEW'`. Per L5, exit
approvals must bypass this. The fix is a one-line conditional:

```python
if plan.action == "approve" and current_risk_state == "HALT_NEW":
    # New: exit approvals proceed regardless of HALT_NEW per backend-spec §2.5.
    if plan.signal_type != "exit":
        raise SignalDispatchError("SIGNAL_BLOCKED_BY_HALT", ...)
```

`signal_type` must be plumbed into `SignalDispatchPlan` (today it's
implicit from action=approve|reject|defer). The planner reads it from
the signals table during the validation SELECT at step 1 and propagates
into the plan dataclass.

### 6.2 V2 (auto-approve via `EXIT_AUTO_APPROVE`)

When `exit_auto_approve=True`:

```
ingest_signal_emitted writes audit + signals row
    ↓
NEW: if signal_type == 'exit' AND exit_auto_approve param is True:
    immediately call apply_signal_dispatch with action='approve'
    decided_by_user_id='agent:exit_auto_approve'
    skip operator-approval queue
    ↓
order_placement_worker picks up
```

This is a server-side automation, not a strategy-side change. The
auto-approve only fires for `signal_type='exit'`; entry signals always
require operator approval (per spec §2.4 risk framework).

**Defense layers around auto-approve:**

1. **Per-signal idempotency**: the auto-approve writes a SIGNAL_APPROVED
   audit row with `decided_by_user_id='agent:exit_auto_approve'`. The
   operator can grep audit for this string to verify what auto-fired.
2. **Operator override**: while a signal is in status='pending', the
   operator can still reject it (until the auto-approve worker picks it
   up — race window typically ≤30s). After approve fires, operator
   recourse is to cancel the resulting order via IBKR/TWS + manual
   audit entry. Same as today's bracket-stop flow.
3. **Decommission still requires operator**: per L6,
   `STRATEGY_DECOMMISSIONED=True` is operator-set only (not
   agent-mutable). So a Claude-induced auto-decommission cascade is
   impossible.
4. **Empirical trust gate**: ship V2 after 10+ exits have been
   operator-approved cleanly. The "10" is a soft floor — operator
   inspects the post-fill behavior of each and flips
   `exit_auto_approve=True` only when confident the rejection rate is
   acceptable.

V2 is OUT OF SCOPE for the initial PR set (PR-1..PR-3 ship V1 only).
V2 is enumerated as PR-4 (post-cutover).

---

## 7. HALT_NEW + Decommission Interaction Table

Which signals fire under each risk_state? Reads top-to-bottom; columns
are mutually exclusive.

| risk_state | `STRATEGY_DECOMMISSIONED` | Entry signals emitted? | Entry approvals dispatched? | Exit signals emitted? | Exit approvals dispatched? |
|---|---|---|---|---|---|
| NORMAL | False | Yes | Yes | Yes (b)(c) | Yes |
| NORMAL | True  | No (universe is empty for new entries) | N/A | Yes (d) for every held | Yes |
| HALT_NEW (routine, defensive, incident) | False | Yes (emit + queue; operator can review) | **No** — blocked | Yes (b)(c) | **Yes** — bypass HALT_NEW per L5 |
| HALT_NEW | True | No | N/A | Yes (d) for every held | **Yes** — bypass HALT_NEW |
| CONVALESCENT | False | Yes (with reduced size per `m_combined`) | Yes | Yes (b)(c) | Yes |
| CONVALESCENT | True | No | N/A | Yes (d) | Yes |

**Notes:**

- "Entry signals emitted" means the strategy's `generate_signals` runs
  and emits CandidateSignals; LEAN POSTs them; they land in `signals`
  table with status=pending. This happens regardless of HALT_NEW because
  the operator may want to record them for forensic visibility.
- "Entry approvals dispatched" means `apply_signal_dispatch` with
  `action='approve'` succeeds. Under HALT_NEW this raises
  `SIGNAL_BLOCKED_BY_HALT`. Operator's recourse: defer the signal until
  state returns to NORMAL.
- "Exit signals emitted" under `STRATEGY_DECOMMISSIONED=True` fires for
  EVERY market with a held position, regardless of indicator state.
  The exit_reason audit field is `decommission`.
- Under `STRATEGY_DECOMMISSIONED=True`, the strategy's entry pipeline
  effectively no-ops: every market would be rejected by a new
  `STRATEGY_DECOMMISSIONED` rejection reason before reaching breakout
  evaluation. This avoids the "entry pipeline emits a candidate but it
  can't ever fill" anti-pattern.

---

## 8. Open Question Resolutions (Q1–Q6) — LOCKED 2026-05-26

All six open questions resolved by operator on 2026-05-26.
Recommendations from this design doc adopted. Resolution notes preserved
below for traceability; the "Recommendation" headers should now be read
as the locked decision.

### Q1: Reversal — emit one signal or two? — LOCKED: TWO

**Decision: emit TWO signals in the SAME cycle (option (a)), with explicit dispatcher sequencing.**

In cycle N:
- Entry pipeline emits `OPEN_<opposite>` CandidateSignal (existing behavior
  in `_evaluate_market` step 5: opposite-direction breakout falls through
  to emit when MIN_HOLDING_DAYS is satisfied).
- Exit pipeline emits `CLOSE_<current>` CandidateSignal with
  `exit_reason='reversal'` and `paired_entry_market` field pointing at
  the entry-side market.
- Operator approves BOTH (or rejects one or both).
- order_placement_worker dispatches the EXIT first; the ENTRY waits
  until the exit fills (worker-side serialization on
  `paired_entry_market` linkage). New ENTRY rejection
  `WAITING_FOR_PAIRED_EXIT` covers the case where the exit hasn't filled
  yet.

**Reasoning:** option (b) "let the next-day cycle emit the entry"
introduces 1 trading day of "flat" exposure on reversal — a meaningful
trend-following cost. Option (a) keeps the position "always invested"
which is operator's stated intent in L1 ("close the long AND open a
short"). The dispatcher-side serialization adds complexity but the
race is small and bounded (broker round-trip on the exit, typically
200–800ms; worst-case minutes if the IBKR session is sluggish).

**Recorded as RECOMMENDATION. Operator should confirm in §10 review.**

### Q2: Exit-signal payload shape — new event_type or `signal_type` discriminator? — LOCKED: DISCRIMINATOR

**Decision: extend `signal_type` discriminator. No new audit event type.**

Per §5.2, `signals.signal_type` is a free TEXT column (no DB CHECK
constraint, no alembic enum). Widening the Python Literal from
`"donchian_breakout"` to `Literal["donchian_breakout", "exit"]` is a
zero-migration change. The audit event type stays `SIGNAL_EMITTED` for
both entries and exits. Differentiation is via the audit payload's
`signal_type` field which `ingest_signal_emitted` writes out.

A new audit event type (`EXIT_SIGNAL_EMITTED`) would require:
- alembic migration of the `AuditEventType` enum
- New audit-side code paths in every consumer
- A02 risk-review-approved label on alembic + signal_ingestion changes

vs the discriminator approach:
- Just plumb `signal_type` through Pydantic schema + ingestion logic
- A02 risk-review-approved required only for signal_ingestion.py changes
  (qc_adapter is on the A02 list per §1.1 forbidden whitelist? — check
  this; it's listed in §1.1 of backend-spec as PR-required but not in
  the [A02] forbidden whitelist text. To be safe, treat signal_ingestion
  changes as risk-review-approved.)
- Backwards-compatible (existing audit consumers that don't read
  `signal_type` keep working; payload field is additive)

### Q3: Exit-signal sizing — strategy-side or dispatcher-side? — LOCKED: DISPATCHER-SIDE

**Decision: dispatcher-side. Strategy emits `direction=FLAT, target_contracts=0`; dispatcher computes the close from current_position at place-order time.**

Reasoning:
- Strategy-side sizing is computed at LEAN cycle time (21:30 UTC) but
  the order is placed later (after operator approval, typically next
  morning). In between, partial fills, re-cons, or operator manual
  adjustments can change the position quantity. Strategy-side qty
  would go stale.
- Dispatcher-side queries `positions_current` at place-order time, which
  is post-recon and authoritative.
- The CandidateSignal's `prior_position_quantity` field is recorded for
  audit-trail (so operator can compare "what was held at signal-emit
  time vs what was held at place-order time"), but the dispatcher uses
  the FRESH read.
- If `positions_current.quantity == 0` at place-order time (position
  already flat — e.g., bracket stop fired between signal-emit and
  approval), the dispatcher emits `ORDER_DROPPED_POSITION_ALREADY_FLAT`
  audit + skips placement; signals.status flips to a new value (proposal:
  `position_already_flat`; needs alembic migration if added to status
  CHECK — alternative is to reuse `cancelled` or `expired` to avoid
  migration). See risk R5 in §11.

### Q4: Bracket-stop cancellation — LOCKED: EXPLICIT CANCEL BEFORE EXIT CLOSE

**Operator framing 2026-05-26: "Keep bracket stops (unless it accidentally leads to an extra short position)."**

Operationally this aligns with the explicit-cancel recommendation, framed
as default-keep-with-exception: bracket stops are the SAFETY NET for held
positions — they MUST stay active as long as a position is held. The
exception applies during the exit-close window because having both the
bracket stop AND a separate close order in flight WILL lead to an extra
short/long position when the bracket stop fires after the close fills
(see R3/R4 in §11). Therefore, in the explicit-exit flow only, we
cancel the bracket stop immediately before placing the close order.

**Decision: explicit cancellation before exit-close placement. Default
otherwise = bracket stays active.**

Today the bracket stop is placed with `parent_broker_order_id=<entry's
broker_order_id>` (see `order_placement_worker.py:746-767`). IBKR's
parentId semantics gate the parent's RELEASE (PendingSubmit → Submitted),
not auto-cancel after parent fills. The bracket stop stays working
until:
- price hits stop level → fires the close (today's path, exit (a)
  unchanged), OR
- explicit cancel via IBKR API (the new exit (b)(c)(d) flow below).

For new exits (b)(c)(d):
1. order_placement_worker, on picking up a `signal_type='exit'` approved
   signal, SELECTs the open bracket-stop row:
   `SELECT * FROM orders WHERE signal_id = <entry_signal_id> AND order_type = 'stop_market' AND status = 'working' AND parent_order_id IS NOT NULL`
   The `entry_signal_id` is derived from the current open trade
   (`trades.entry_signal_id`) for the same market+account.
2. Calls `ibkr_client.cancel_order(stop_row.client_order_id)`.
3. Writes `ORDER_CANCELLED` audit with reason `'bracket_stop_replaced_by_exit_signal'`.
4. UPDATEs orders row: status='cancelled'.
5. Places the close limit/market order (no parent, no bracket — exits
   don't need new protective stops because the position is going to zero).

**Failure modes**: see R3, R4 in §11.

### Q5: Exit failure modes (rejection / margin / broker error) — LOCKED: POSITION_UNPROTECTED + RECOVERY TOOL

**Decision: P0 alert taxonomy + naked-position recovery runbook.**

If exit close placement fails after the bracket-stop cancel succeeds,
the position is NAKED (no protective stop) until operator intervention.
This is the worst failure mode in the entire exit pipeline. Mitigations:

1. **Audit emits NEW event type `POSITION_UNPROTECTED`** (alembic
   migration; A02 risk-review-approved) when:
   - Bracket cancel succeeds AND close placement fails (broker rejection
     or timeout)
   - Same audit row triggers Discord #critical P0 + Resend email via
     `services/webhook_pusher`
   - Payload includes market, prior_position_direction,
     prior_position_quantity, exit_reason, last_known_stop_price.
2. **Operator-tool**: new
   `scripts/operator_tools/replace_protective_stop.py` (PR-5; mirrors
   trigger_v1_cycle style — risk-state gate, dry-run default, operator
   approval) that places a fresh bracket-stop at the same ATR-derived
   level for an existing position. Bridges the gap until the failed
   exit re-emits next cycle.
3. **Auto-retry NOT recommended for V1**: a broker-side rejection
   probably indicates a real issue (margin, halted symbol, gateway
   disconnect); auto-retry could compound. V2 may add bounded retry
   with backoff, but V1 stays manual.

**Other exit failure modes:**

| Mode | Severity | Auto-action | Operator action |
|---|---|---|---|
| Exit signal rejected at IBKR (margin, halted) | P0 | POSITION_UNPROTECTED audit + alert | Manual reconcile + replace_protective_stop tool |
| Bracket-stop cancel succeeds; close placement times out | P0 | POSITION_UNPROTECTED + alert | Same |
| Bracket-stop cancel fails (broker timeout); close placement proceeds anyway | P1 | Log warning + audit `ORDER_CANCEL_FAILED`; proceed with close placement | Verify in TWS; manually flatten if double-fired |
| Close fills but at radically off price (slippage > 10× ATR) | P1 | Log audit; no auto-action | Slippage calibration self-tunes monthly |
| Position already flat at place-order time | P2 | ORDER_DROPPED + status=`position_already_flat` | None; expected |

### Q6: Trigger_v1_cycle exit role — LOCKED: EXTEND WITH STATUS-FILTERED DEDUP

**Decision: extend to mirror exit cycle. Dedup logic differs from entries (`status NOT IN ('rejected','cancelled','expired')`).**

The operator tool today emits entries only. Extension:

1. After `generate_signals` call, also call `generate_exit_candidates`
   with the SAME active_universe + current_positions + the just-computed
   entry candidates (for reversal coupling).
2. Each exit signal posts to the same endpoint with
   `signal_type='exit'`.
3. **Dedup**: today's `fetch_already_emitted_markets` checks
   `SELECT DISTINCT market FROM signals WHERE account_id=... AND env=...
    AND session_date=...`. For exits, the same query is insufficient —
   if entry emitted today but exit didn't, we'd skip exits incorrectly.
   New query: `SELECT DISTINCT market FROM signals WHERE ... AND
   signal_type='exit' AND status NOT IN ('rejected','cancelled','expired')`.
   This re-emits exits that were rejected/cancelled (allowing the
   operator to re-trigger a fresh evaluation), but skips ones that are
   still pending/approved/working (preventing duplicate dispatch).
4. CLI: same `--dry-run` default, same `--no-dry-run` opt-in. New
   `--exits-only` and `--entries-only` flags for forensic targeting
   (default: run both).
5. CLI: new `--reason-filter` to limit exit emission to specific
   exit_reason (e.g., `--reason-filter=decommission` for the
   decommission ceremony).

---

## 9. PR Breakdown

3 PRs total. Operator does one session per PR.

### PR-A: Strategy-side exit pipeline + parameters

**Scope:**
- `strategies/v1_trend_following/strategy.py` — implement
  `generate_exit_candidates` per §4 pseudocode.
- `strategies/v1_trend_following/signals.py` — widen `signal_type`
  Literal; add new RejectionReason values; add new CandidateSignal
  fields; add ExitGenerationResult.
- `strategies/v1_trend_following/parameters.py` — add
  `strategy_decommissioned` + `exit_auto_approve` fields to V1Parameters
  + V1_DEFAULTS + to_canonical_dict + range validation.
- `strategies/v1_trend_following/strategy.py::_evaluate_market` — add
  rejection branch for `STRATEGY_DECOMMISSIONED=True` (skip the breakout
  evaluation; record `STRATEGY_DECOMMISSIONED` rejection).
- Tests: `tests/unit/test_v1_exit_pipeline.py` (new file) covering each
  exit_reason branch + precedence + the new RejectionReasons +
  edge cases (no position, FLAT direction, missing series).

**A02 status**: `parameters.py` is A02-listed; PR needs
`risk-review-approved`. The rest of `strategies/v1_trend_following/**`
is not on A02 forbidden whitelist (only `services/risk/**` etc. per
§1.1 backend-spec are; `strategies/v1_trend_following/` is "PR-required"
but distinct from "risk-review-approved required").

**Estimated session count:** 1

**Dependencies:** none; strategy is broker-agnostic + standalone.

### PR-B: LEAN + trigger_v1_cycle integration

**Scope:**
- `lean/v1_strategy.py` — extend `on_daily_signal_cycle` to call
  `generate_exit_candidates` AFTER `generate_signals`, post results to
  `/api/internal/lean/signals` with `signal_type='exit'`.
- `services/api/schemas/lean.py::LeanEventRequest` — add
  `signal_type` field (defaults to `'entry'` for backwards compat),
  `exit_reason` field (None for entries), `prior_position_direction`,
  `prior_position_quantity`, `paired_entry_market`.
- `services/api/routes/internal/lean.py::post_lean_signal` — extend
  required-field gate for exits (different from entry's required
  fields).
- `services/qc_adapter/signal_ingestion.py` — plumb `signal_type` and
  related fields through `ingest_signal_emitted`; replace hard-coded
  `signal_type="entry"` at line 235 with the payload value.
- `scripts/operator_tools/trigger_v1_cycle.py` — per Q6:
  extend to emit exits; new `--exits-only/--entries-only/--reason-filter`
  flags; refined dedup.
- Tests: extend `tests/unit/test_lean_local_signal_cycle.py`,
  `tests/integration/test_lean_signal_endpoint.py`,
  `tests/unit/test_trigger_v1_cycle.py`.

**A02 status**: `services/qc_adapter/signal_ingestion.py` is on the
PR-required list per §1.1 backend-spec but NOT on the [A02] forbidden
whitelist (which is risk/signal/audit/execution/reconciliation/
calibration/agent/decisions/alembic only). However, since exits flow
through audit-first ordering and the signal_type discriminator is
load-bearing for downstream dispatch behavior, recommend treating this
PR as risk-review-approved-required out of caution. **Operator
escalation point if interpretation differs.**

**Estimated session count:** 1

**Dependencies:** PR-A must be merged first (LEAN imports the new
strategy surface).

### PR-C: Dispatcher + order placement worker + fill_processor extension

**Scope:**
- `services/risk/signal_dispatch.py` — extend `SignalDispatchPlan` to
  carry `signal_type`; condition the HALT_NEW gate on
  `signal_type != 'exit'`; planner reads `signal_type` from the
  signals table at validation step 1.
- `services/risk/order_placement_worker.py` — new code path for
  `signal_type='exit'`:
  - `fetch_open_bracket_stop_for_entry(market, account, signal_id)`
    helper.
  - `cancel_bracket_stop_before_exit` step (ORDER_CANCELLED audit).
  - `dispatcher_side_close_sizing` — compute close direction and qty
    from `positions_current`; raise `OrderPlacementError("POSITION_ALREADY_FLAT")`
    if zero.
  - `place_exit_close_order` — limit-marketable close, no bracket,
    `parent_order_id` references the cancelled bracket-stop's parent
    (the original entry order) so the audit chain links cleanly.
  - POSITION_UNPROTECTED audit emission on cancel-success-place-fail
    path (per Q5).
- `services/risk/fill_processor.py` — verify EXIT_FULL_CLOSE classifier
  works against an exit signal with `direction=FLAT` and
  `signal_type='exit'`. May need a tweak to the
  `signal_direction` parameter resolution (today it reads from
  `signals.direction` which is FLAT for exits; the classifier needs to
  resolve to the prior position's direction instead). Confirm in
  integration tests.
- `services/execution/ibkr_client.py` — likely no changes; `cancel_order`
  already exists. Verify it handles the bracket-stop cancellation
  case correctly (cancel acks reach `orderStatusEvent`; status
  cancelled propagates to orders row).
- Alembic migration: NEW audit event type `POSITION_UNPROTECTED`
  (alembic/versions/NNNN_position_unprotected_event_type.py); may also
  need NEW signals.status value `position_already_flat` (alembic
  CHECK constraint extension).
- Tests: `tests/unit/test_signal_dispatch_exit_halt_gate.py`,
  `tests/unit/test_order_placement_exit_path.py`,
  `tests/integration/test_exit_end_to_end.py`,
  `tests/unit/test_fill_processor_explicit_close.py`.

**A02 status**: ALL of `services/risk/**`, `services/execution/**`,
`alembic/**` are on the forbidden whitelist. PR carries
`risk-review-approved`. The largest PR by surface area; expect heavy
review.

**Estimated session count:** 1 (heavy session — possibly split into
two PRs if review feedback grows: PR-C1 dispatcher + PR-C2 order
placement + fill_processor + alembic).

**Dependencies:** PR-A + PR-B merged. PR-C assumes the
`signal_type` column is being populated.

### PR-D (future, post-cutover): EXIT_AUTO_APPROVE V2

Out of scope for the initial 3-PR set. Lands AFTER 10+ exits have been
operator-approved cleanly. Scope:
- `services/api/main.py` or new `services/risk/auto_approve_worker.py`
  — background task that polls `signals` for
  `(signal_type='exit' AND status='pending' AND exit_auto_approve=True)`
  rows; immediately calls `apply_signal_dispatch` with
  `action='approve'` and `decided_by_user_id='agent:exit_auto_approve'`.
- Auto-approve fires within 5s of signal_emitted ingestion (race
  window allows operator to override by rejecting the still-pending
  signal — though typically the operator wouldn't be at the keyboard).

Out of scope until operational confidence is established. Recommend
re-opening for design after cutover when N>10 exits.

---

## 10. Test Plan

Three tiers: strategy unit tests (PR-A); end-to-end integration tests
(PR-B + PR-C); operational smoke test (post PR-C, in paper).

### 10.1 Strategy unit tests (PR-A)

`tests/unit/test_v1_exit_pipeline.py`:

| Test | Scenario | Expected |
|---|---|---|
| `test_no_positions_no_signals` | All markets FLAT | `signals=(), rejections=()` |
| `test_long_position_no_trend_flip` | LONG held; close > MA_FAST | `TREND_HOLDS` rejection |
| `test_long_position_trend_flip` | LONG held; close < MA_FAST; held ≥ MIN_HOLDING | 1 exit signal, `exit_reason='trend_flip'` |
| `test_long_position_trend_flip_too_recent` | LONG held; close < MA_FAST; held < MIN_HOLDING | `MIN_HOLDING_NOT_REACHED` rejection |
| `test_short_position_trend_flip` | SHORT held; close > MA_FAST; held ≥ MIN_HOLDING | 1 exit signal |
| `test_reversal_with_paired_entry` | LONG held; entry pipeline emits SHORT (all filters pass) | 1 exit signal, `exit_reason='reversal'`, `paired_entry_market` set |
| `test_reversal_without_paired_entry` | LONG held; opposite breakout fails Hurst → entry rejected | No reversal exit (only check held vs opposite-entry-OUTPUT, not opposite-direction-state) |
| `test_decommission` | LONG held; `strategy_decommissioned=True` | 1 exit signal, `exit_reason='decommission'` (overrides indicator state) |
| `test_decommission_no_position` | FLAT; `strategy_decommissioned=True` | No signal |
| `test_decommission_precedence_over_reversal` | LONG held; opposite passes filters; decommission=True | `exit_reason='decommission'` (decommission overrides reversal) |
| `test_reversal_precedence_over_trend_flip` | LONG held; close < MA_FAST AND opposite passes filters | `exit_reason='reversal'` |
| `test_insufficient_bars` | LONG held; series has < min_required_bars | `INSUFFICIENT_BAR_HISTORY` rejection |
| `test_missing_opened_at_date` | LONG held; `opened_at_session_date=None` | MIN_HOLDING check skipped; falls through to trend_flip evaluation |
| `test_exit_candidate_decision_price` | Exit signal | `decision_price = series.bars[-1].close`, `stop_price = Decimal('0')`, `direction = FLAT` |
| `test_exit_candidate_includes_prior_state` | Exit signal | `prior_position_direction`, `prior_position_quantity` populated |

Plus extend `tests/unit/test_strategy_v1.py::TestEvaluateMarket` for the
new `STRATEGY_DECOMMISSIONED` rejection branch on the entry side.

### 10.2 Integration tests (PR-B + PR-C)

`tests/integration/test_exit_end_to_end.py`:

| Test | Path | Verification |
|---|---|---|
| `test_lean_emits_exit_signal_e2e` | LEAN → api → audit + signals row | Signals row `signal_type='exit'`; audit row matches |
| `test_exit_signal_approved_under_halt_new` | Set risk_state=HALT_NEW; approve exit | Approve succeeds; entry approve fails |
| `test_exit_signal_dispatcher_sizing` | Exit signal with target_contracts=0; position quantity=3 | Order quantity = 3 (opposite side); from dispatcher-side compute |
| `test_exit_cancels_bracket_then_places_close` | Approve exit; order_placement_worker runs | ORDER_CANCELLED for stop + ORDER_PLACED for close audit chain; orders rows reflect both |
| `test_position_unprotected_on_cancel_succeeds_place_fails` | Mock IBKR cancel OK, place rejected | POSITION_UNPROTECTED audit + Discord P0 mock invoked |
| `test_exit_fill_classifies_as_exit_full_close` | Simulate close fill via fill_processor | Classifier returns EXIT_FULL_CLOSE; TRADE_CLOSED audit |
| `test_reversal_two_signals_serialization` | Exit + entry approved same cycle | Entry rejected with `WAITING_FOR_PAIRED_EXIT` until exit fills |
| `test_decommission_emits_for_all_held` | Set `strategy_decommissioned=True`; 3 positions | 3 exit signals emitted, exit_reason='decommission' |
| `test_dedup_re_emits_rejected_exits` | Exit emitted then rejected; trigger_v1_cycle again | Re-emits (status NOT IN ('rejected','cancelled','expired') check works) |

### 10.3 Operational smoke test (post PR-C, paper-only)

Against tonight's live /M2K LONG position (`signal_id =
019e66d3-e092-7713-b486-53ab17500cc1`):

1. **Confirm no spurious re-emit (PR-A landed)**: tomorrow's 21:30 UTC
   LEAN cycle should NOT emit another /M2K LONG entry (today's
   POSITION_ALREADY_SAME_DIRECTION still gates this).
2. **Confirm trend_flip check is silent**: tomorrow's cycle should NOT
   emit a trend_flip exit since /M2K just opened (held days = 1 <
   MIN_HOLDING_DAYS=14). The strategy logs should show
   `MIN_HOLDING_NOT_REACHED` rejection for /M2K on the exit side.
3. **Force decommission test (post PR-C, controlled)**: temporarily set
   `strategy_decommissioned=True` via parameter UPDATE; observe one
   `signal_emitted` audit row with `signal_type='exit'`,
   `exit_reason='decommission'` for /M2K; operator approves; observe
   bracket-stop cancel + close place; verify TRADE_CLOSED. Then revert
   `strategy_decommissioned=False`.
4. **Manual reversal test (post PR-C, requires market conditions)**:
   not testable on demand; relies on the market actually firing a
   SHORT signal in /M2K while we hold LONG with MIN_HOLDING_DAYS
   satisfied. Wait for natural occurrence; verify the paired-emission
   contract end-to-end.

### 10.4 What we CANNOT test pre-cutover

- Real IBKR rejection of an exit order (margin / halted market). The
  paper account is forgiving; we can mock at the `IbkrClient` boundary.
- Cancel-fails-then-place-succeeds race. Mock-driven.
- Live execution slippage on a close order. Slippage calibration is
  monthly per backend-spec §2.14; we observe and tune post-cutover.

---

## 11. Risks + Mitigations

| ID | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | Exit logic bug causes spurious exits to fire daily; positions churn | Med | High (lost edge from over-trading) | V1 operator-approval gates every exit; bugs visible before damage. V2 `EXIT_AUTO_APPROVE` blocked until 10+ clean exits. |
| R2 | Reversal-exit + paired-entry race: exit doesn't fill, entry fires anyway → naked opposite position | Med | High | Dispatcher-side serialization: entry holds with `WAITING_FOR_PAIRED_EXIT` rejection until paired exit fills. Audit links via `paired_entry_market`. |
| R3 | Bracket-stop cancel succeeds; close placement fails → naked position | Low | Critical | POSITION_UNPROTECTED P0 alert + replace_protective_stop.py operator tool. Audit chain preserved. |
| R4 | Bracket-stop cancel fails (broker timeout); close places anyway → both fire, oscillate | Low | High | If cancel ack times out, log ORDER_CANCEL_FAILED + proceed with close placement BUT mark with `concurrent_bracket_warning=true` audit; operator verifies in TWS. Worst case both orders fill; position oscillates and eventual reconciliation flags break. |
| R5 | Position already flat at place-order time (bracket stop fired between exit signal emission and operator approval) | Med | Low | ORDER_DROPPED_POSITION_ALREADY_FLAT audit + status=`position_already_flat`. Operator sees the drop; no harm done. |
| R6 | Decommission cascade: operator accidentally sets `strategy_decommissioned=True` → all positions flatten | Low | Med-High | `strategy_decommissioned` is operator-only (not agent-mutable). Parameter UPDATE requires the same audit + `parameter_change_applied` event as any other locked parameter. Operator can immediately re-flip to False if accidental; in-flight exit signals can be rejected at the /signals page before approval. |
| R7 | Audit-chain break during exit flow (e.g., POSITION_UNPROTECTED writes fail) | Very low | Critical | Same audit-write retry logic as every other audit event (SERIALIZABLE + advisory lock + 5 retries). Hash chain preserved. |
| R8 | Hurst persistence flips back the next day after a reversal exit | Med | Med | Reversal entry will not re-emit because position is now opposite-direction; if Hurst flips back, the new opposite breakout would still need ALL 3 filters → unlikely to whiplash. Tolerance by design. |
| R9 | trigger_v1_cycle's relaxed exit dedup re-emits a rejected exit before next cycle | Low | Low | Re-emit is by design (operator deliberately re-evaluating). Audit shows two signal rows; no over-fire because operator approves at most one. |
| R10 | LEAN cycle exits the entry pipeline early due to history failure; exit pipeline DOESN'T run | Med | Med (missed exit) | Exit pipeline runs in its own try/except block. History failure for ONE market doesn't block exits for OTHER markets. Same robustness as the entry pipeline today. |
| R11 | `fill_processor.UnsupportedFillScenarioError` for partial exit fills (the close partial-fills) | Low | Med | Today's classifier raises on partial exits. V1 keeps that behavior — partial fills require operator manual reconciliation via TWS. Phase 2+ deferred work. |
| R12 | Operator forgets to approve an exit; trend-flip persists for days | High | Low-Med | Daily re-emit (under dedup rules in §Q6) keeps the exit visible at /signals every cycle. Operator alerted via standard signal-pending Discord ping. After cutover, V2 `EXIT_AUTO_APPROVE` removes this risk. |

---

## 12. Out of Scope (Deferred)

The following are explicitly NOT in the PR-A / PR-B / PR-C scope. They
are noted for visibility but require future work:

1. **Partial exit fills** — today's fill_processor raises on partial
   exits. V1 stays manual; Phase 2 may add support.
2. **Exit retry on broker rejection** — V1 fails open + emits
   POSITION_UNPROTECTED. V2 may add bounded retry with backoff.
3. **EXIT_AUTO_APPROVE** — designed in §6.2; not implemented in initial
   PRs. PR-D post-cutover.
4. **Resize via partial close** — backend-spec §2.4 stages 1–3 may
   shrink an existing position when new positions enter. This is a
   distinct path from "exit" (which always closes fully). Resize is its
   own future work item.
5. **Multi-cycle exit deferral** — if an operator defers an exit signal
   (action='defer') the today's behavior keeps it `status='deferred'`
   indefinitely. Future enhancement: re-evaluate deferred exits
   automatically each cycle. V1 requires operator to manually approve
   or reject a deferred exit.
6. **Live-money cutover gating** — exits MUST work end-to-end in paper
   for ≥7 consecutive days (≥1 successful exit fill observed) before
   the live cutover ceremony per `Docs/live-money-cutover-plan.md`.
   This is operational, not in this design's PR scope.
7. **Exit-time slippage calibration** — today's calibration treats
   entries and exits the same. Future research: are exit slippages
   systematically different (typical for trend-following where exits
   happen against momentum)? Defer to Phase 2 slippage work.

---

## Appendix: Files Touched Summary

| File | PR | A02 risk-review-approved? |
|---|---|---|
| `strategies/v1_trend_following/strategy.py` | PR-A | No |
| `strategies/v1_trend_following/signals.py` | PR-A | No |
| `strategies/v1_trend_following/parameters.py` | PR-A | **Yes** (A02 listed) |
| `tests/unit/test_v1_exit_pipeline.py` (new) | PR-A | No |
| `lean/v1_strategy.py` | PR-B | No (hot-fix whitelist) |
| `services/api/schemas/lean.py` | PR-B | No |
| `services/api/routes/internal/lean.py` | PR-B | No (hot-fix whitelist per §2.3) |
| `services/qc_adapter/signal_ingestion.py` | PR-B | Recommend yes |
| `scripts/operator_tools/trigger_v1_cycle.py` | PR-B | No |
| `services/risk/signal_dispatch.py` | PR-C | **Yes** |
| `services/risk/order_placement_worker.py` | PR-C | **Yes** |
| `services/risk/fill_processor.py` | PR-C | **Yes** |
| `services/execution/ibkr_client.py` | PR-C | **Yes** (only if changes) |
| `alembic/versions/NNNN_position_unprotected.py` (new) | PR-C | **Yes** |
| `tests/unit/test_signal_dispatch_exit_halt_gate.py` (new) | PR-C | No |
| `tests/unit/test_order_placement_exit_path.py` (new) | PR-C | No |
| `tests/integration/test_exit_end_to_end.py` (new) | PR-C | No |
| `tests/unit/test_fill_processor_explicit_close.py` (new) | PR-C | No |

---

## Sign-off Checklist — SIGNED OFF 2026-05-26

Operator resolved all six open questions on 2026-05-26.

- [x] Q1: emit TWO signals (option (a)) with dispatcher serialization.
- [x] Q2: `signal_type` discriminator (no new audit event type).
- [x] Q3: dispatcher-side sizing.
- [x] Q4: explicit bracket-stop cancellation in the exit-close flow
      ("keep bracket stops as default; cancel in the exit flow because
      not cancelling leads to an extra short/long position").
- [x] Q5: POSITION_UNPROTECTED P0 audit + replace_protective_stop.py
      operator tool.
- [x] Q6: trigger_v1_cycle extension with status-filtered dedup.
- [x] PR-B's `services/qc_adapter/signal_ingestion.py` change carries
      `risk-review-approved` label (operator's call — recommend yes).
- [x] §11 risk-mitigation review accepted.

**Status:** ready for PR-A kickoff. See "PR-A kickoff prompt" below.

---

## PR-A Kickoff Prompt (for next Claude Code session)

Copy-paste this into a new Claude Code session to start the strategy-side
implementation:

> I want to start PR-A from `Docs/exit-pipeline-design.md` — the
> strategy-side exit pipeline.
>
> Per the design doc:
>
> - Scope: `strategies/v1_trend_following/{strategy.py,signals.py,parameters.py}`
>   plus a new `tests/unit/test_v1_exit_pipeline.py`.
> - `parameters.py` is A02-listed → PR needs `risk-review-approved` label.
> - The rest of `strategies/v1_trend_following/**` is PR-required but not
>   on the A02 forbidden whitelist.
> - All 6 open questions (Q1–Q6) are LOCKED in the doc; no need to
>   re-litigate.
> - The strategy is broker-agnostic; PR-A has no dependency on the
>   server-side changes (PR-B/PR-C land later).
>
> Read in this order:
>
> 1. `Docs/exit-pipeline-design.md` (the locked design)
> 2. `CLAUDE.md` (orientation)
> 3. `Docs/claude-dev-guide.md` §1 + §1.5
> 4. `strategies/v1_trend_following/strategy.py` (target file)
> 5. `strategies/v1_trend_following/signals.py` (target file)
> 6. `strategies/v1_trend_following/parameters.py` (target file; A02)
> 7. `tests/unit/test_strategy_v1.py` (test patterns to mirror)
>
> Deliverables for this session:
>
> - `generate_exit_candidates` fully implemented per §4 pseudocode.
> - New RejectionReason values (TREND_HOLDS, MIN_HOLDING_NOT_REACHED,
>   STRATEGY_DECOMMISSIONED — and add an entry-side rejection for
>   `STRATEGY_DECOMMISSIONED` that short-circuits the entry pipeline).
> - New CandidateSignal fields (exit_reason, prior_position_direction,
>   prior_position_quantity, paired_entry_market).
> - New `ExitGenerationResult` dataclass.
> - `V1Parameters` extended with `strategy_decommissioned: bool = False`
>   and `exit_auto_approve: bool = False`.
> - `_evaluate_market` updated to no-op when STRATEGY_DECOMMISSIONED=True.
> - Tests covering each branch in §10.1 of the design doc (~15 unit tests).
> - PR opened with `risk-review-approved` label.
>
> Run `make test` (or equivalent) before opening the PR.
