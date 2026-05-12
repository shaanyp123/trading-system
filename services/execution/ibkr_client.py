"""services/execution/ibkr_client.py — IBKR client Protocol + factory.

Pivot-PR-B (post-pivot 2026-05-12). Defines the abstract ``IbkrClient``
Protocol that the rest of the execution layer programs against. The
concrete ``ib-async``-backed implementation lives in
``services/execution/ibkr_adapter.py``.

**Architectural pivot:** This client replaces the pre-pivot QC ObjectStore
instruction protocol (backend-spec §4.5.2 RETIRED). Orders flow direct via
``ib-async`` to a Dockerized ``ib_gateway`` container; no JSON-instruction-
via-shared-store handshake. See ``Docs/decisions-log.md`` 2026-05-12 entry
+ backend-spec §1.2 (post-pivot architecture).

**A02 BINDS** — this file is on the forbidden whitelist
(``services/execution/**``); `risk-review-approved` required for any
future changes. The decision to use Protocol + adapter (vs. a concrete class)
was made so tests can inject mocks at the seam without monkeypatching
``ib_async`` internals.

**A13 (revised) compatibility** — this file IS the canonical direct-IBKR
path. The pre-pivot rule ("do NOT call IBKR TWS API directly in Phase 1")
was inverted on 2026-05-12; this file lands in that revision.

**A27** BINDS for the IBKR platform; satisfied via ``deploy/ibkr/README.md``
operator runbook + the futures-sim precondition smoke (placeOrder +
cancelOrder round-trip on IBKR paper /MES) before the PR can be deployed
to a live account.
"""

from __future__ import annotations

from typing import Protocol

from services.execution.types import (
    IbkrAccountSummary,
    IbkrCancelOrderResult,
    IbkrConnectionState,
    IbkrContractRef,
    IbkrPlaceOrderRequest,
    IbkrPlaceOrderResult,
    IbkrPosition,
    OrderStatusCallback,
)


class IbkrClient(Protocol):
    """Async Protocol for the IBKR client.

    Method semantics intentionally close to the underlying ``ib-async``
    API to keep the adapter thin (1:1 method calls; minimal type
    translation). Each method may raise ``IbkrPlacementError`` on
    terminal-failure (gateway disconnect, auth failure, etc.) but a
    rejection-from-IBKR is reported in the ``IbkrPlaceOrderResult.status``
    field, NOT raised — the dispatcher needs both the rejection metadata
    AND a successful return to log the rejection audit event.

    The connection lifecycle is managed by the adapter implementation:
    `connect()` is called once at process startup; `disconnect()` is
    called at shutdown. Methods automatically reconnect on transient
    disconnects + retry once (no caller-side retry needed for transient
    blips).
    """

    async def connect(self) -> IbkrConnectionState:
        """Establish or refresh the TWS API session.

        Idempotent: calling on an already-connected client is a no-op.
        Raises ``IbkrPlacementError`` on terminal-failure (auth, network).
        """
        ...

    async def disconnect(self) -> None:
        """Cleanly close the TWS API session.

        Called at graceful container shutdown. Best-effort: errors are
        logged but don't propagate.
        """
        ...

    async def connection_state(self) -> IbkrConnectionState:
        """Return current connection health snapshot without I/O.

        Called by the health-check loop every 30s. Lightweight: reads
        from cached ``IB`` state; doesn't make a TWS API call.
        """
        ...

    async def place_order(self, request: IbkrPlaceOrderRequest) -> IbkrPlaceOrderResult:
        """Submit an order to IBKR.

        Returns the placement result with the IBKR-assigned ``broker_order_id``.
        On rejection at submit (e.g., insufficient margin), returns a result
        with ``status='rejected'`` + populated ``rejection_*`` fields.
        On terminal failure (ib_gateway unreachable), raises ``IbkrPlacementError``.

        Caller is responsible for: (a) writing the audit event BEFORE
        calling this method (audit-first per spec §2.10.1), (b) catching
        ``IbkrPlacementError`` and triggering kill-switch on consecutive
        failures.
        """
        ...

    async def cancel_order(self, client_order_id: str) -> IbkrCancelOrderResult:
        """Submit a cancel request for a working order.

        Returns when the cancel request is acknowledged by IBKR. The
        actual cancel-confirmed transition arrives asynchronously via
        the order-status event stream; the dispatcher subscribes to those
        events separately.

        Raises ``IbkrPlacementError`` on terminal failure. Raises
        ``KeyError`` if the ``client_order_id`` is not known to this
        IBKR session (caller should treat as "already cancelled or filled").
        """
        ...

    async def cancel_all_orders(self) -> int:
        """Submit cancels for ALL working orders held by this client.

        Used by the kill-switch flatten-all flow. Returns the count of
        cancel requests submitted. Individual cancel-confirmations arrive
        async; this method returns when the bulk submit is acknowledged.

        Raises ``IbkrPlacementError`` on terminal failure.
        """
        ...

    async def get_positions(self) -> list[IbkrPosition]:
        """Snapshot all positions for the configured account.

        Returns a fresh list (not cached) — calls ``IB.reqPositions()``
        synchronously. Used by reconciliation (60s cadence during CME
        session) + the dispatcher's pre-trade margin check.
        """
        ...

    async def get_account_summary(self) -> IbkrAccountSummary:
        """Snapshot cash + margin metrics for the configured account.

        Returns a fresh snapshot — calls ``IB.reqAccountSummary()``.
        Used by the risk-engine margin-protocol gate (backend-spec §2.4.5).
        """
        ...

    async def resolve_contract(self, market: str) -> IbkrContractRef:
        """Resolve a market symbol (e.g., '/MES') to an IBKR contract.

        For futures, picks the front-month continuous contract using
        IBKR's contract-search API + the roll-calendar from
        ``services.scheduler.calendar_import``. Result is cached so
        subsequent calls for the same market hit the cache.

        Raises ``KeyError`` if the market is not in the Phase 1
        sub-universe (locked in
        ``strategies.v1_trend_following.parameters.V1_CANDIDATE_UNIVERSE``).
        """
        ...

    async def subscribe_order_status(self, callback: OrderStatusCallback) -> None:
        """Register an async callback fired on every order-status transition.

        The callback receives a fully-populated
        :class:`services.execution.types.OrderStatusUpdate` per IBKR
        ``orderStatusEvent``. Implementations MUST:

        * Be idempotent — calling twice with the same callback registers
          the callback exactly once. (The adapter uses object-identity to
          dedupe.)
        * Schedule callback invocations off the broker event loop so a
          slow callback can't backpressure the broker's event delivery.
        * Catch exceptions raised by the callback + log them WITHOUT
          re-raising — a misbehaving callback must not crash the broker
          connection. Critical fill-handling logic still has to defend
          itself, but the adapter provides the outer try/except.

        Required pre-condition: :meth:`connect` must have been called at
        least once (the underlying ``ib_async.IB`` instance has to exist
        before its ``orderStatusEvent`` is attachable). Implementations
        MAY assert this; the worker code path calls ``connect`` before
        subscribing.

        No deregistration API in Phase 1: the worker subscribes once at
        startup and the adapter holds the reference for the process'
        lifetime. Multi-callback support is deliberately out of scope —
        the worker is the only consumer today and a single callback
        keeps the eventual idempotency surface narrow.
        """
        ...


__all__ = ["IbkrClient"]
