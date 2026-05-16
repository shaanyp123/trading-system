"""services/risk/order_terminal_status_processor.py — propagate non-fill
terminal order statuses from IBKR to the backend.

Closes Defect #2 from the 2026-05-13 production drill. Pre-this-module,
the order-placement worker's :meth:`_on_order_status` callback acted
only on ``status="filled"``. When IBKR emitted ``cancelled`` /
``rejected`` (including the ``ValidationError`` form for un-resolvable
contracts), the backend silently dropped the event:

* ``orders.status`` stayed at ``pending`` forever
* No audit row was written
* No SSE event reached the operator's web/Discord surfaces
* The reconciliation cycle had no way to learn the order was dead on
  the broker side; its only signal is the orders + positions table

This module mirrors :mod:`services.risk.fill_processor` but for the
non-fill terminal branch. The plan-then-apply split lives inside the
single :func:`process_terminal_status_event` for now — the surface is
small enough (no position / balance / trade mutations; just an audit
row + an orders UPDATE) that the pure-policy + I/O separation would
add code without clarifying intent.

**A02 BINDS** — file is on the forbidden whitelist
(``services/risk/**``). ``risk-review-approved`` required.
**A01 enforced** — audit-first via ``append_audit_event``.
**A05 enforced** — Decimals throughout.
**A06 enforced** — datetimes are tz-aware UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from services.audit.event_types import AuditEventType
from services.audit.writer import Environment, PhaseAtEmit, append_audit_event

log = structlog.get_logger()


#: Locked enum of terminal order statuses this module handles. ``inactive``
#: is included because IBKR uses it for several non-fill terminal states
#: (margin reject, contract not found at validation). Mapped to
#: ``rejected`` in the orders.status column so the operator's wire shape
#: stays canonical.
TerminalOrderStatus = Literal["cancelled", "rejected", "inactive"]


#: Mapping from ``TerminalOrderStatus`` → canonical ``orders.status``
#: enum value (per alembic 0002 CHECK constraint). ``inactive`` collapses
#: to ``rejected`` because IBKR's API surfaces validation errors as
#: ``Inactive`` rather than ``Rejected``.
_STATUS_DB_VALUE: Final[dict[TerminalOrderStatus, str]] = {
    "cancelled": "cancelled",
    "rejected": "rejected",
    "inactive": "rejected",
}


#: Mapping from ``TerminalOrderStatus`` → audit event type. Each terminal
#: status writes one row to ``audit_log`` so the operator can deep-link
#: from the audit explorer + downstream consumers (Discord webhook
#: pusher, reconciliation planner's "what closed yesterday" projection)
#: can subscribe to the canonical SSE event type.
_STATUS_AUDIT_EVENT_TYPE: Final[dict[TerminalOrderStatus, AuditEventType]] = {
    "cancelled": AuditEventType.ORDER_CANCELLED,
    "rejected": AuditEventType.ORDER_REJECTED,
    "inactive": AuditEventType.ORDER_REJECTED,
}


@dataclass(frozen=True, slots=True)
class TerminalStatusPayload:
    """Inputs to :func:`process_terminal_status_event`.

    ``broker_order_id`` is the IBKR-side numeric order id. ``status_kind``
    is one of the three locked terminal kinds — the worker derives it
    from the orderStatusEvent's status field via
    :data:`services.execution.ibkr_adapter._ORDER_STATUS_KIND_MAP`.

    ``rejection_reason`` carries any IBKR-side error code + message (e.g.,
    "321 - Please enter a local symbol or an expiry"). May be None for
    plain ``cancelled`` events without a message.

    ``observed_at_utc`` is the wall-clock when the worker received the
    event from the IBKR adapter (NOT when IBKR emitted it; that
    timestamp is not surfaced through the ib-async callback).
    """

    broker_order_id: int
    status_kind: TerminalOrderStatus
    rejection_reason: str | None
    observed_at_utc: datetime


@dataclass(frozen=True, slots=True)
class TerminalStatusResult:
    """Output of :func:`process_terminal_status_event`.

    ``signal_id`` may be None if the orders row was placed manually via
    TWS (no signal_id linkage); the worker's main path never produces
    such rows, but defensive nullability lets downstream consumers
    handle the case if it ever appears.
    """

    order_id: UUID
    signal_id: UUID | None
    market: str
    new_status: str
    audit_event_uuid: UUID
    audit_sequence_no: int


class OrderTerminalStatusError(Exception):
    """Raised by :func:`process_terminal_status_event` on terminal failure.

    Categories:

    * ``ORDER_NOT_FOUND`` — no orders row matches the client_order_id.
      The worker's main path never produces orphan orderStatus events
      (placement always writes the orders row first), so this is a
      data-quality concern; caller logs at WARNING and drops the event.
    * ``ORDER_ALREADY_TERMINAL`` — orders.status is already one of
      ``filled / cancelled / rejected``. Idempotent re-fire from IBKR;
      caller drops silently (info-level log).
    * ``DB_ERROR`` — Postgres-level failure during UPDATE or audit
      append. Caller logs at ERROR + does NOT add to dedupe set so a
      manual retry path remains open.
    """

    def __init__(
        self, error_code: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = details or {}


async def process_terminal_status_event(
    *,
    session_factory: async_sessionmaker[Any],
    client_order_id: str,
    payload: TerminalStatusPayload,
    env: Environment,
    phase_at_emit: PhaseAtEmit = 1,
) -> TerminalStatusResult | None:
    """Propagate a cancelled / rejected / inactive IBKR orderStatus to the backend.

    Pipeline (audit-first per backend-spec §2.10.1):

      1. SELECT the orders row by client_order_id → capture id + signal_id
         + market + current status.
      2. If already terminal (filled / cancelled / rejected), return
         None — caller treats as idempotent no-op.
      3. Build the audit payload + append the audit row (own SERIALIZABLE
         transaction via ``append_audit_event``).
      4. UPDATE orders.status + rejection_reason in its own transaction.
      5. Return the result with the audit_event_uuid for SSE stamping.

    Returns None when the order is unknown OR already terminal. Returns
    a populated :class:`TerminalStatusResult` otherwise.

    Raises :class:`OrderTerminalStatusError` only on Postgres-level
    failures during UPDATE; the caller's exception handler logs +
    leaves the dedupe set open so an operator-initiated retry remains
    possible.
    """
    # Step 1: lookup orders row
    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id, signal_id, market, status, broker_order_id "
                    "FROM orders WHERE client_order_id = :coid LIMIT 1"
                ),
                {"coid": client_order_id},
            )
        ).fetchone()

    if row is None:
        log.warning(
            "order_terminal_status_order_not_found",
            client_order_id=client_order_id,
            broker_order_id=payload.broker_order_id,
            status_kind=payload.status_kind,
        )
        return None

    order_id = UUID(str(row.id))
    signal_id: UUID | None = UUID(str(row.signal_id)) if row.signal_id is not None else None
    market = str(row.market)
    current_status = str(row.status)

    # Step 2: idempotency check. IBKR may re-fire status events on
    # reconnect; only act once per order_id x terminal_kind.
    if current_status in ("filled", "cancelled", "rejected"):
        log.info(
            "order_terminal_status_already_terminal",
            client_order_id=client_order_id,
            broker_order_id=payload.broker_order_id,
            current_status=current_status,
            inbound_status_kind=payload.status_kind,
        )
        return None

    # Step 3: audit-first.
    new_status = _STATUS_DB_VALUE[payload.status_kind]
    audit_event_type = _STATUS_AUDIT_EVENT_TYPE[payload.status_kind]
    audit_payload: dict[str, Any] = {
        "order_id": str(order_id),
        "signal_id": str(signal_id) if signal_id is not None else None,
        "client_order_id": client_order_id,
        "broker_order_id": payload.broker_order_id,
        "market": market,
        "prior_status": current_status,
        "new_status": new_status,
        "rejection_reason": payload.rejection_reason,
        "observed_at_utc": payload.observed_at_utc.isoformat(),
    }

    # Resolve the account_id from the order row's signal — fall back to a
    # query if the orders row has no signal_id (manual TWS path).
    async with session_factory() as session:
        if signal_id is not None:
            account_row = (
                await session.execute(
                    text("SELECT account_id FROM signals WHERE id = :sid LIMIT 1"),
                    {"sid": signal_id},
                )
            ).fetchone()
            if account_row is None:
                raise OrderTerminalStatusError(
                    error_code="DB_ERROR",
                    message=(
                        f"orders row {order_id} references signal_id={signal_id} "
                        "but no matching signals row exists"
                    ),
                    details={"order_id": str(order_id), "signal_id": str(signal_id)},
                )
            account_id = UUID(str(account_row.account_id))
        else:
            # Fallback: pull the single active account. Phase 1 has exactly
            # one operator; multi-account expansion would need a richer
            # query joining on broker_order_id or contract metadata.
            acct_row = (
                await session.execute(
                    text(
                        "SELECT id FROM accounts "
                        "WHERE active_to IS NULL "
                        "ORDER BY active_from DESC LIMIT 1"
                    )
                )
            ).fetchone()
            if acct_row is None:
                raise OrderTerminalStatusError(
                    error_code="DB_ERROR",
                    message="no active accounts row found for orphan-order audit attribution",
                )
            account_id = UUID(str(acct_row.id))

    async with session_factory() as audit_session:
        record = await append_audit_event(
            audit_session,
            audit_event_type,
            audit_payload,
            account_id=account_id,
            env=env,
            phase_at_emit=phase_at_emit,
            source_clock_ts=payload.observed_at_utc,
        )

    # Step 4: UPDATE orders.status + rejection_reason
    async with session_factory() as upd_session:
        async with upd_session.begin():
            await upd_session.execute(
                text(
                    "UPDATE orders SET "
                    "  status = :new_status, "
                    "  rejection_reason = :rejection_reason "
                    "WHERE id = :order_id"
                ),
                {
                    "new_status": new_status,
                    "rejection_reason": payload.rejection_reason,
                    "order_id": order_id,
                },
            )

    log.info(
        "order_terminal_status_propagated",
        client_order_id=client_order_id,
        broker_order_id=payload.broker_order_id,
        order_id=str(order_id),
        new_status=new_status,
        rejection_reason=payload.rejection_reason,
        audit_event_uuid=str(record.event_uuid),
        audit_sequence_no=record.sequence_no,
    )

    return TerminalStatusResult(
        order_id=order_id,
        signal_id=signal_id,
        market=market,
        new_status=new_status,
        audit_event_uuid=record.event_uuid,
        audit_sequence_no=record.sequence_no,
    )


def _now_utc() -> datetime:
    """Tz-aware UTC `now` factory; defaults [A06]."""
    return datetime.now(tz=UTC)


__all__ = [
    "OrderTerminalStatusError",
    "TerminalOrderStatus",
    "TerminalStatusPayload",
    "TerminalStatusResult",
    "process_terminal_status_event",
]
