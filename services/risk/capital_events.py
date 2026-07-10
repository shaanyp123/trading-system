"""services/risk/capital_events.py — capital-event plan + apply.

Owns the operator-initiated capital-flow events (deposits, withdrawals)
that record an entry in the ``capital_events`` table + trigger the
30-session capital_event mode per backend-spec §2.4.4 + §3.20. Two-phase
plan/apply split mirrors :mod:`services.risk.dispatch` /
:mod:`services.risk.state_machine`:

* **Policy** (:func:`plan_capital_event`) — synchronous, deterministic,
  no I/O. Computes the post-event equity + drawdown-baseline reset value
  + threshold flag + canonical audit-event sequence (1 event below
  threshold; 2 events at/above threshold). Unit-testable without
  testcontainers (anti-pattern A22).

* **I/O** (:func:`apply_capital_event`) — consumes the plan + executes
  the writes against a real :class:`AsyncSession`. Audit-first ordering
  per backend-spec §2.10.1: the capital-event audit row (deposit or
  withdrawal) commits BEFORE the ``capital_events`` INSERT, and the
  optional ``CAPITAL_EVENT_MODE_STARTED`` audit row commits BEFORE the
  ``risk_state`` UPDATE that activates the mode.

Threshold semantics (locked):

* ``threshold_met = pct_of_pre_equity >= 5%`` (``CAPITAL_EVENT_THRESHOLD_PCT``)
* When met:
    - Deposit → ``dd_baseline_reset_to = post_event_equity`` (resets the
      trailing-drawdown reference; per backend-spec, a +5% deposit
      shouldn't immediately spike the trailing-DD percentage)
    - Withdrawal → ``dd_baseline_reset_to = None`` (drawdown baseline
      unchanged; the operator is taking capital out)
    - The 30-session capital_event mode activates: ``m_capital_event=0.5``
      for sessions 1-5, then ``1.0`` for sessions 6-30, then mode ends.
* When NOT met: row is recorded for forensic visibility; no mode
  activation; no baseline reset.

Bootstrap edge case (first cutover deposit on $0 equity):

* ``pre_event_equity = Decimal("0")`` makes ``pct_of_pre_equity``
  divide-by-zero. Handled by treating ``threshold_met = True``
  unconditionally on a first-deposit-from-zero (per cutover plan §7
  step 4 — "threshold_met = TRUE (definitionally on first deposit)").
* ``pct_of_pre_equity`` is recorded as ``Decimal("0")`` (sentinel; not
  used downstream).

Session-counter semantics (Phase 0 placeholder, crypto-era resolution):

The schema's ``capital_event_active_until_session_no`` +
``capital_event_vol_normalized_at_session_no`` fields (and the
``capital_events.capital_event_mode_session_start`` field) are INTEGERs
that index into a global CME session counter. That counter mechanism
never existed as a queryable field; this module accepts the current
session number as an explicit argument (``current_session_no``) and
computes ``+5`` and ``+30`` offsets per the spec. For the cutover-day
bootstrap deposit on a fresh live DB, the operator passes
``current_session_no=0``.

**Crypto-era resolution (decisions-log 2026-07-10):** post-CME a
"session" is one UTC calendar day (same calendar the CONVALESCENT
clean-day counter ticks on, per the 2026-07-09 amendment), and the
live session count is DERIVED FROM DATES, not from the absolute
counter fields: :func:`capital_event_session_count` computes the
UTC-day delta between the decision date and the most recent
threshold-met event's ``effective_at_utc`` date. The absolute
``*_session_no`` fields remain as written-forensic placeholders (the
``/capital-*`` flow still records them with the route's default
``current_session_no=0``) but no consumer reads them for sizing.

A01 BINDS — every audit row routes through :func:`append_audit_event`.
A02 BINDS — ``services/risk/**`` requires ``risk-review-approved`` label.
A04 BINDS — every emitted ``event_type`` is in the locked
:class:`~services.audit.event_types.AuditEventType` taxonomy.
A05 enforced — all monetary values pass through :class:`decimal.Decimal`
at the module boundary; never ``float``.
A06 enforced — all timestamps tz-aware UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final, Literal
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from services.audit.writer import Environment, PhaseAtEmit, append_audit_event

log = structlog.get_logger()


#: Capital-event audit event types we emit. Narrowed Literal so mypy
#: catches typos at compile time. Mirrors the pattern in
#: ``services.risk.state_machine.AuditEventTypeName`` + the
#: per-module ``PendingAuditEvent`` shapes in :mod:`services.risk.sizing`.
CapitalEventAuditTypeName = Literal[
    "capital_event_deposit",
    "capital_event_withdrawal",
    "capital_event_mode_started",
]


@dataclass(frozen=True, slots=True)
class PendingAuditEvent:
    """A queued audit event the caller flushes via :func:`append_audit_event`.

    Same shape as :class:`services.risk.sizing.PendingAuditEvent` +
    :class:`services.risk.state_machine.PendingAuditEvent` (deliberately
    per-module — each module narrows the Literal to its own emitted set).
    """

    event_type: CapitalEventAuditTypeName
    payload: dict[str, Any]


#: Capital event threshold per backend-spec §3.20 comment. Below this,
#: the event is recorded but does NOT trigger the 30-session mode.
CAPITAL_EVENT_THRESHOLD_PCT: Final[Decimal] = Decimal("0.05")

#: Sessions counted from event for the multiplier-normalization point.
#: Per backend-spec §2.4.4: sessions 1-5 use m_capital_event=0.5;
#: sessions 6-30 use 1.0 (mode-active flag); session 31+ ends mode.
CAPITAL_EVENT_VOL_NORMALIZED_AFTER_SESSIONS: Final[int] = 5
CAPITAL_EVENT_MODE_DURATION_SESSIONS: Final[int] = 30


def capital_event_session_count(
    *,
    decision_date: date,
    last_threshold_met_event_date_utc: date | None,
) -> int:
    """Sessions elapsed since the most recent threshold-met capital event.

    Crypto-era session semantics (decisions-log 2026-07-10): one session
    = one UTC calendar day, so the count is the plain UTC-day delta
    between the decision date and the event's ``effective_at_utc`` date:

    * no threshold-met event on record → ``0`` (no mode; multiplier
      inactive per :func:`services.risk.multipliers.m_combined`),
    * the event's own UTC day → ``0`` (that day's 00:05 UTC decision
      predates an intraday event; under the CME design the event was
      likewise observed at EOD recon, so the event session never ran
      half-sized — sessions 1-5 are the five FOLLOWING days),
    * days 1-5 after → ``1..5`` (m_capital_event = 0.5),
    * days 6-30 after → mode-active window, multiplier normalized,
    * day 31+ → mode over (callers compare against
      :data:`CAPITAL_EVENT_MODE_DURATION_SESSIONS`).

    A later threshold-met event restarts the clock (MAX event date wins
    upstream), mirroring ``apply_capital_event``'s overwrite of the
    ``risk_state`` window fields. A degenerate future-dated event clamps
    to ``0`` — the mode has not started before its event day.

    Pure + clock-free ([A22]); the caller supplies both dates.
    """
    if last_threshold_met_event_date_utc is None:
        return 0
    return max(0, (decision_date - last_threshold_met_event_date_utc).days)


class CapitalEventError(Exception):
    """Raised on policy violations the planner refuses to plan.

    Distinct categories surface via ``error_code`` so the route layer
    can translate to HTTP error codes per backend-spec §4.1:

    * ``"AMOUNT_INVALID"`` — non-positive amount.
    * ``"PRE_EVENT_EQUITY_NEGATIVE"`` — negative equity input.
    * ``"WITHDRAWAL_EXCEEDS_EQUITY"`` — withdrawal > pre-event equity.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True, slots=True)
class CapitalEventPlan:
    """Pure-policy result of :func:`plan_capital_event`.

    Captures everything the apply step needs to: (a) write the audit
    event(s), (b) INSERT the ``capital_events`` row, (c) optionally
    UPDATE the ``risk_state`` row to activate the 30-session mode.

    The ``audit_events`` tuple is sized 1 or 2:

    * Length 1: just CAPITAL_EVENT_DEPOSIT or CAPITAL_EVENT_WITHDRAWAL
      (threshold NOT met; no mode activation).
    * Length 2: CAPITAL_EVENT_DEPOSIT/WITHDRAWAL then
      CAPITAL_EVENT_MODE_STARTED (threshold MET; mode activates).
    """

    account_id: UUID
    event_type: Literal["deposit", "withdrawal"]
    amount_usd: Decimal
    """Magnitude only (positive). For withdrawal events, the
    post_event_equity computation subtracts this; for deposits, it
    adds. Negative inputs raise CapitalEventError(AMOUNT_INVALID)."""
    pre_event_equity: Decimal
    post_event_equity: Decimal
    pct_of_pre_equity: Decimal
    """``amount / pre_event_equity`` as a fraction (e.g., 0.05 = 5%).
    ``Decimal("0")`` sentinel when ``pre_event_equity == 0``
    (bootstrap-deposit case)."""
    threshold_met: bool
    """True when ``pct_of_pre_equity >= CAPITAL_EVENT_THRESHOLD_PCT``
    OR pre_event_equity == 0 (first deposit case)."""
    effective_at_utc: datetime
    dd_baseline_reset_to: Decimal | None
    """``post_event_equity`` for threshold-met deposits; ``None`` for
    withdrawals (drawdown baseline unchanged) and below-threshold
    events."""
    current_session_no: int
    """The global CME session counter at event time. Cutover-day
    bootstrap uses ``0``; subsequent events use whatever the operator
    supplies."""
    mode_active_until_session_no: int | None
    """``current_session_no + CAPITAL_EVENT_MODE_DURATION_SESSIONS``
    when threshold_met; ``None`` otherwise. Persisted to
    ``risk_state.capital_event_active_until_session_no``."""
    mode_vol_normalized_at_session_no: int | None
    """``current_session_no + CAPITAL_EVENT_VOL_NORMALIZED_AFTER_SESSIONS``
    when threshold_met; ``None`` otherwise. Persisted to
    ``risk_state.capital_event_vol_normalized_at_session_no``."""
    operator_reason: str
    """Free-text operator note (e.g., 'initial live funding'). Persisted
    in the audit payload but not in the capital_events table (the
    schema doesn't carry a reason column)."""
    audit_events: tuple[PendingAuditEvent, ...]


@dataclass(frozen=True, slots=True)
class AppliedCapitalEvent:
    """Outcome of executing a :class:`CapitalEventPlan`.

    Mirrors :class:`services.risk.dispatch.AppliedStateTransition` —
    the route layer reads this to populate the HTTP response body +
    SSE envelope. The two audit_event_uuid slots correspond to the
    two possible audit rows:

    * ``capital_event_audit_event_uuid`` — always set; UUID of the
      CAPITAL_EVENT_DEPOSIT or CAPITAL_EVENT_WITHDRAWAL row.
    * ``mode_started_audit_event_uuid`` — set when ``threshold_met``;
      UUID of the CAPITAL_EVENT_MODE_STARTED row. ``None`` otherwise.
    """

    capital_event_id: UUID
    capital_event_audit_event_uuid: UUID
    mode_started_audit_event_uuid: UUID | None
    event_type: Literal["deposit", "withdrawal"]
    amount_usd: Decimal
    threshold_met: bool
    post_event_equity: Decimal


def _build_audit_payload(
    *,
    plan_step: Literal["event", "mode_started"],
    account_id: UUID,
    event_type: Literal["deposit", "withdrawal"],
    amount_usd: Decimal,
    pre_event_equity: Decimal,
    post_event_equity: Decimal,
    pct_of_pre_equity: Decimal,
    threshold_met: bool,
    dd_baseline_reset_to: Decimal | None,
    current_session_no: int,
    mode_active_until_session_no: int | None,
    mode_vol_normalized_at_session_no: int | None,
    effective_at_utc: datetime,
    operator_reason: str,
) -> dict[str, Any]:
    """Canonical audit-payload shape per backend-spec §3.20 + JSON-
    canonicalisation rules (A05: Decimal-as-string)."""
    base: dict[str, Any] = {
        "account_id": str(account_id),
        "event_type": event_type,
        "amount_usd": str(amount_usd),
        "pre_event_equity": str(pre_event_equity),
        "post_event_equity": str(post_event_equity),
        "pct_of_pre_equity": str(pct_of_pre_equity),
        "threshold_met": threshold_met,
        "dd_baseline_reset_to": (
            str(dd_baseline_reset_to) if dd_baseline_reset_to is not None else None
        ),
        "current_session_no": current_session_no,
        "effective_at_utc": effective_at_utc.isoformat(),
        "operator_reason": operator_reason,
    }
    if plan_step == "mode_started":
        base["mode_active_until_session_no"] = mode_active_until_session_no
        base["mode_vol_normalized_at_session_no"] = mode_vol_normalized_at_session_no
    return base


def plan_capital_event(
    *,
    account_id: UUID,
    event_type: Literal["deposit", "withdrawal"],
    amount_usd: Decimal,
    pre_event_equity: Decimal,
    current_session_no: int,
    operator_reason: str,
    now_utc: datetime,
) -> CapitalEventPlan:
    """Build the plan for a capital event.

    Args:
        account_id: target account.
        event_type: ``"deposit"`` or ``"withdrawal"``.
        amount_usd: magnitude (positive Decimal). Non-positive raises
            ``CapitalEventError(AMOUNT_INVALID)``.
        pre_event_equity: account equity before the event (typically
            sourced from the latest ``balances.net_liquidation``).
            Negative raises ``PRE_EVENT_EQUITY_NEGATIVE``. Zero is
            accepted (bootstrap-deposit case).
        current_session_no: global CME session counter at event time;
            cutover-day bootstrap = 0.
        operator_reason: free-text note recorded in audit payload.
        now_utc: caller-supplied timestamp (tz-aware UTC). Kept as a
            parameter so the policy layer stays clock-free + tests can
            assert against fixed times.

    Raises:
        CapitalEventError: if any input fails validation.
    """
    if now_utc.tzinfo is None:
        raise CapitalEventError(
            error_code="TIMESTAMP_NAIVE",
            message="now_utc must be tz-aware UTC (A06)",
        )
    if amount_usd <= 0:
        raise CapitalEventError(
            error_code="AMOUNT_INVALID",
            message=f"amount_usd must be positive; got {amount_usd}",
        )
    if pre_event_equity < 0:
        raise CapitalEventError(
            error_code="PRE_EVENT_EQUITY_NEGATIVE",
            message=f"pre_event_equity must be >= 0; got {pre_event_equity}",
        )
    if event_type == "withdrawal" and amount_usd > pre_event_equity:
        raise CapitalEventError(
            error_code="WITHDRAWAL_EXCEEDS_EQUITY",
            message=(
                f"withdrawal amount_usd={amount_usd} exceeds "
                f"pre_event_equity={pre_event_equity}; cap at the "
                "current balance"
            ),
        )

    if event_type == "deposit":
        post_event_equity = pre_event_equity + amount_usd
    else:
        post_event_equity = pre_event_equity - amount_usd

    # Threshold + pct: bootstrap deposit (pre=0) is definitionally
    # threshold-met per cutover plan §7 step 4.
    if pre_event_equity == 0:
        pct_of_pre_equity = Decimal("0")
        threshold_met = True
    else:
        pct_of_pre_equity = amount_usd / pre_event_equity
        threshold_met = pct_of_pre_equity >= CAPITAL_EVENT_THRESHOLD_PCT

    # Drawdown baseline reset: only on threshold-met DEPOSITS. Withdrawals
    # leave the baseline alone (the operator is shrinking the account;
    # subsequent trailing-DD calculations should still reference the
    # pre-withdrawal peak).
    dd_baseline_reset_to: Decimal | None
    if threshold_met and event_type == "deposit":
        dd_baseline_reset_to = post_event_equity
    else:
        dd_baseline_reset_to = None

    mode_active_until: int | None
    mode_vol_normalized_at: int | None
    if threshold_met:
        mode_active_until = current_session_no + CAPITAL_EVENT_MODE_DURATION_SESSIONS
        mode_vol_normalized_at = current_session_no + CAPITAL_EVENT_VOL_NORMALIZED_AFTER_SESSIONS
    else:
        mode_active_until = None
        mode_vol_normalized_at = None

    # Audit events: always at least one (the actual deposit/withdrawal).
    # When threshold met, add the MODE_STARTED event AFTER the event row.
    # Narrowed string-literal to satisfy the CapitalEventAuditTypeName
    # alias on PendingAuditEvent (mypy doesn't narrow AuditEventType.value).
    audit_event_type_name: CapitalEventAuditTypeName = (
        "capital_event_deposit" if event_type == "deposit" else "capital_event_withdrawal"
    )
    event_payload = _build_audit_payload(
        plan_step="event",
        account_id=account_id,
        event_type=event_type,
        amount_usd=amount_usd,
        pre_event_equity=pre_event_equity,
        post_event_equity=post_event_equity,
        pct_of_pre_equity=pct_of_pre_equity,
        threshold_met=threshold_met,
        dd_baseline_reset_to=dd_baseline_reset_to,
        current_session_no=current_session_no,
        mode_active_until_session_no=mode_active_until,
        mode_vol_normalized_at_session_no=mode_vol_normalized_at,
        effective_at_utc=now_utc,
        operator_reason=operator_reason,
    )
    audit_events: list[PendingAuditEvent] = [
        PendingAuditEvent(event_type=audit_event_type_name, payload=event_payload)
    ]
    if threshold_met:
        mode_payload = _build_audit_payload(
            plan_step="mode_started",
            account_id=account_id,
            event_type=event_type,
            amount_usd=amount_usd,
            pre_event_equity=pre_event_equity,
            post_event_equity=post_event_equity,
            pct_of_pre_equity=pct_of_pre_equity,
            threshold_met=threshold_met,
            dd_baseline_reset_to=dd_baseline_reset_to,
            current_session_no=current_session_no,
            mode_active_until_session_no=mode_active_until,
            mode_vol_normalized_at_session_no=mode_vol_normalized_at,
            effective_at_utc=now_utc,
            operator_reason=operator_reason,
        )
        audit_events.append(
            PendingAuditEvent(
                event_type="capital_event_mode_started",
                payload=mode_payload,
            )
        )

    return CapitalEventPlan(
        account_id=account_id,
        event_type=event_type,
        amount_usd=amount_usd,
        pre_event_equity=pre_event_equity,
        post_event_equity=post_event_equity,
        pct_of_pre_equity=pct_of_pre_equity,
        threshold_met=threshold_met,
        effective_at_utc=now_utc,
        dd_baseline_reset_to=dd_baseline_reset_to,
        current_session_no=current_session_no,
        mode_active_until_session_no=mode_active_until,
        mode_vol_normalized_at_session_no=mode_vol_normalized_at,
        operator_reason=operator_reason,
        audit_events=tuple(audit_events),
    )


async def apply_capital_event(
    *,
    plan: CapitalEventPlan,
    db: AsyncSession,
    env: Environment,
    phase_at_emit: PhaseAtEmit,
) -> AppliedCapitalEvent:
    """Execute the plan's I/O — write audit events, INSERT capital_events
    row, optionally UPDATE risk_state mode fields.

    Step-by-step (audit-first per backend-spec §2.10.1):

    1. Write the CAPITAL_EVENT_DEPOSIT or CAPITAL_EVENT_WITHDRAWAL audit
       event via :func:`append_audit_event`. Capture the returned
       ``event_uuid`` for the ``capital_events.audit_event_uuid`` linkage.
    2. INSERT the ``capital_events`` row referencing that audit_event_uuid.
       The row records the immutable forensic snapshot of the event.
    3. If ``plan.threshold_met``: write the CAPITAL_EVENT_MODE_STARTED
       audit event + UPDATE the current ``risk_state`` row's
       ``capital_event_active_until_session_no`` +
       ``capital_event_vol_normalized_at_session_no`` fields. The
       risk_state row's ``audit_event_uuid`` is NOT changed (the prior
       state-transition row's UUID stays); a future read of these fields
       cross-references via the audit chain by event_type, not by FK.

    Returns:
        :class:`AppliedCapitalEvent` with the new capital_events row's
        ID + both audit event UUIDs (or just the first when threshold
        not met).
    """
    if not plan.audit_events:
        raise ValueError(
            "apply_capital_event: plan.audit_events is empty; "
            "policy layer must emit at least the event row"
        )

    # Step 1 — audit-first: write the CAPITAL_EVENT_DEPOSIT/WITHDRAWAL row.
    first_event = plan.audit_events[0]
    event_record = await append_audit_event(
        db,
        first_event.event_type,
        first_event.payload,
        account_id=plan.account_id,
        env=env,
        phase_at_emit=phase_at_emit,
    )

    # Step 2 — INSERT capital_events row, linked to the audit row.
    async with db.begin():
        result = (
            await db.execute(
                text(
                    "INSERT INTO capital_events ("
                    "    account_id, event_type, amount_usd, "
                    "    pre_event_equity, post_event_equity, "
                    "    pct_of_pre_equity, threshold_met, "
                    "    effective_at_utc, dd_baseline_reset_to, "
                    "    capital_event_mode_session_start, "
                    "    audit_event_uuid"
                    ") VALUES ("
                    "    :acc, :etype, :amt, "
                    "    :pre, :post, "
                    "    :pct, :thr, "
                    "    :eff, :dd, "
                    "    :sess_start, "
                    "    :audit_uuid"
                    ") RETURNING id"
                ),
                {
                    "acc": plan.account_id,
                    "etype": plan.event_type,
                    "amt": plan.amount_usd,
                    "pre": plan.pre_event_equity,
                    "post": plan.post_event_equity,
                    "pct": plan.pct_of_pre_equity,
                    "thr": plan.threshold_met,
                    "eff": plan.effective_at_utc,
                    "dd": plan.dd_baseline_reset_to,
                    "sess_start": (plan.current_session_no if plan.threshold_met else None),
                    "audit_uuid": event_record.event_uuid,
                },
            )
        ).one()
        capital_event_id = UUID(str(result.id))

    mode_started_audit_event_uuid: UUID | None = None
    if plan.threshold_met:
        # Step 3a — audit-first: write the MODE_STARTED row.
        mode_event = plan.audit_events[1]
        mode_record = await append_audit_event(
            db,
            mode_event.event_type,
            mode_event.payload,
            account_id=plan.account_id,
            env=env,
            phase_at_emit=phase_at_emit,
        )
        mode_started_audit_event_uuid = mode_record.event_uuid

        # Step 3b — UPDATE risk_state mode fields. Best-effort: if no
        # current risk_state row exists for this account (degenerate
        # state — bootstrap not run), we log a WARNING and proceed.
        # The audit row is durable; operator can repair the cache.
        async with db.begin():
            updated = (
                await db.execute(
                    text(
                        "UPDATE risk_state "
                        "SET capital_event_active_until_session_no = :until_no, "
                        "    capital_event_vol_normalized_at_session_no = :vol_norm_no "
                        "WHERE account_id = :acc AND is_current = TRUE "
                        "RETURNING id"
                    ),
                    {
                        "acc": plan.account_id,
                        "until_no": plan.mode_active_until_session_no,
                        "vol_norm_no": plan.mode_vol_normalized_at_session_no,
                    },
                )
            ).fetchone()
            if updated is None:
                log.warning(
                    "capital_event_risk_state_no_current_row",
                    account_id=str(plan.account_id),
                    note=(
                        "no is_current=TRUE risk_state row for this "
                        "account — the audit chain reflects the event "
                        "but the live cache wasn't updated. Operator "
                        "should run scripts/operator_tools/"
                        "bootstrap_live_account.py first."
                    ),
                )

    log.info(
        "capital_event_applied",
        account_id=str(plan.account_id),
        event_type=plan.event_type,
        amount_usd=str(plan.amount_usd),
        threshold_met=plan.threshold_met,
        capital_event_id=str(capital_event_id),
        capital_event_audit_event_uuid=str(event_record.event_uuid),
        mode_started_audit_event_uuid=(
            str(mode_started_audit_event_uuid) if mode_started_audit_event_uuid else None
        ),
        post_event_equity=str(plan.post_event_equity),
    )

    return AppliedCapitalEvent(
        capital_event_id=capital_event_id,
        capital_event_audit_event_uuid=event_record.event_uuid,
        mode_started_audit_event_uuid=mode_started_audit_event_uuid,
        event_type=plan.event_type,
        amount_usd=plan.amount_usd,
        threshold_met=plan.threshold_met,
        post_event_equity=plan.post_event_equity,
    )


# Convenience re-exports for the route + test surfaces.
__all__ = [
    "CAPITAL_EVENT_MODE_DURATION_SESSIONS",
    "CAPITAL_EVENT_THRESHOLD_PCT",
    "CAPITAL_EVENT_VOL_NORMALIZED_AFTER_SESSIONS",
    "AppliedCapitalEvent",
    "CapitalEventError",
    "CapitalEventPlan",
    "apply_capital_event",
    "capital_event_session_count",
    "plan_capital_event",
]
