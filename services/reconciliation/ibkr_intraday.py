"""services/reconciliation/ibkr_intraday.py — per-cycle reqPositions client for recon.

Option C (2026-05-28). The EOD reconciliation cycle's position-quantity check
moves off IBKR FlexQuery XML (which only reports settlement-cleared positions,
so same-day fills land after the clearing cutoff and trigger false-positive
halts) onto IBKR's real-time TWS API view via ``reqPositions``. This module
owns the broker round-trip for that view; the recon orchestrator (PR-B) wires
it in, and FlexQuery stays the source-of-truth for cash / NAV / position MTM.

**Connection model — per-cycle (mirrors bar_sync clientId=3).** Each call
constructs an :class:`~services.execution.ibkr_adapter.IbAsyncIbkrClient`,
connects, reads positions, and disconnects in a ``finally`` block. The
once-per-day recon cadence makes a per-cycle connect's latency irrelevant, and
a gateway-down failure is localized to that one cycle rather than leaving a
stale long-lived socket (the source of prior Error 326 wedges + drill-6
stuck-at-login). Pattern lifted from ``services/data/bar_sync.py::run_cycle``.

**clientId=4.** Claims the reserved 4-7 slot per dev-guide §1.5 LOCKED. Isolated
from the order worker (clientId=1) and bar_sync (clientId=3) so a recon bug can
never wedge order placement. See :data:`DEFAULT_RECON_CLIENT_ID`.

**Fresh-fetch (Q4 Path B — adopted 2026-05-29).** This calls
``IbkrClient.get_positions_fresh()`` (``reqPositionsAsync()`` → awaits
``positionEnd``), NOT ``get_positions()`` (which reads ib-async's local
``IB.positions()`` cache). Path A (trust the connect-time cache) was the
original choice on the assumption it had backed the order worker's clientId=1
margin pre-check for ~6 weeks — but that worker is LONG-LIVED, so its cache is
warm by read time. This module's per-cycle ``connect → read → disconnect``
client is a different regime: ``connectAsync()`` does not await the startup
``reqPositions``/``positionEnd`` handshake, so a read issued immediately after
connect can return an EMPTY cache. That race fired in production on 2026-05-29
22:30 UTC (``raw_count=0`` while the account genuinely held /M2K + /MES,
confirmed real by ``scripts/operator_tools/recon_positions_probe.py``) and
tripped a false ``NORMAL → HALT_NEW``. Path B removes the dependence on connect
timing. As belt-and-suspenders we retry the fresh fetch on an empty result (see
:data:`RECON_FRESH_FETCH_MAX_ATTEMPTS`); a persistent empty while the backend
holds futures is handled one layer up by the ``run_eod_cycle`` empty-result
guard (degrade to FlexQuery + P1 alert, NOT auto-halt). See decisions-log
2026-05-29 + ``Docs/option-c-recon-design.md``.

**A02 BINDS** — this file is on the forbidden whitelist
(``services/reconciliation/**``); `risk-review-approved` required for changes.
The module IMPORTS from ``services.execution`` (the ``IbkrClient`` Protocol,
the ``IbAsyncIbkrClient`` adapter, and ``IbkrPlacementError``) but does NOT
modify it.

**A05 enforced** — quantity is ``Decimal`` end-to-end; the adapter's
``IbkrPosition.quantity`` is already ``Decimal`` and we pass it through.

**A27** — this module talks to IBKR (a third-party platform). It is dead code
until PR-B wires it into ``run_eod_cycle``, so there is no live-broker contract
to smoke yet; the production-shape verification is the Option-C deploy ceremony
(``deploy/reconciliation/README.md`` cutover step), where the first 22:30 UTC
cycle running ``source=reqpositions`` against the real gateway pins the
observed ``/MES`` / ``TLT`` symbol values. Until then this is unproven against
a real ``reqPositions`` cycle.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

import structlog

from services.execution.ibkr_adapter import IbAsyncIbkrClient
from services.execution.ibkr_client import IbkrClient
from services.execution.types import IbkrPlacementError, IbkrPosition

log = structlog.get_logger()


#: TWS API clientId for the EOD reconciliation positions fetch. Claims the
#: reserved 4-7 slot per dev-guide §1.5 LOCKED. Per-cycle connect/disconnect
#: (mirrors ``bar_sync`` clientId=3); isolated from the order-placement worker
#: (clientId=1) and bar_sync (clientId=3). A clientId collision raises IBKR
#: Error 162 + wedges the colliding client for ~30 min, so this value must not
#: overlap any other IBKR-connecting code path. Documented in dev-guide §1.5.
DEFAULT_RECON_CLIENT_ID: Final[int] = 4

#: Path B (2026-05-29) retry budget for the fresh ``reqPositions`` fetch when
#: it returns an EMPTY list. ``reqPositionsAsync()`` awaits ``positionEnd`` so
#: the first attempt is normally authoritative; the retry exists only as
#: belt-and-suspenders for any residual ib-async edge where ``positionEnd``
#: races ahead of the position rows. A genuinely flat account costs
#: ``(MAX_ATTEMPTS - 1) * SETTLE`` seconds of extra wall-clock — negligible at
#: the once-daily recon cadence and far cheaper than a false halt. A persistent
#: empty (backend holds futures) is caught by the ``run_eod_cycle``
#: empty-result guard, which degrades to FlexQuery rather than auto-halting.
RECON_FRESH_FETCH_MAX_ATTEMPTS: Final[int] = 3

#: Seconds to settle between empty fresh-fetch attempts (see above).
RECON_FRESH_FETCH_SETTLE_SECONDS: Final[float] = 1.5


@dataclass(frozen=True, slots=True)
class ReconPosition:
    """Minimal position shape the reconciliation planner ingests.

    Just the two fields the position-quantity check needs:

    * ``market`` — the canonical symbol (``/MES`` for futures, ``TLT`` for
      ETFs). Already normalized by the adapter's ``_contract_from_ib`` (FUT
      ``secType`` → ``f"/{symbol}"``; everything else → ``symbol``), so it
      matches the backend's ``positions_current.market`` convention with no
      extra normalization here.
    * ``quantity`` — signed (positive = long, negative = short).

    MTM fields (market price, unrealized PnL) are deliberately ABSENT —
    ``reqPositions`` doesn't carry ``markPrice`` / ``fifoPnlUnrealized``
    (``IbkrPosition.market_price_usd`` is hard-coded ``None`` in the adapter),
    so under Option C those still come from FlexQuery.
    """

    market: str
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class ReconPositionsFetchError(Exception):
    """Raised when the per-cycle ``reqPositions`` fetch fails terminally.

    Wraps the underlying :class:`~services.execution.types.IbkrPlacementError`
    (gateway down, auth failure, TWS session dropped mid-call) so the recon
    orchestrator (PR-B) catches a reconciliation-package-owned type rather than
    importing the execution-layer exception into its ``except`` clause. PR-B's
    ``run_eod_cycle`` catches this, falls back to the FlexQuery position list,
    and emits a P1 alert.

    The original ``IbkrPlacementError`` is preserved on ``__cause__`` (raised
    via ``raise ... from exc``); ``underlying_exception_class`` is copied across
    for log/alert convenience without forcing PR-B to unwrap ``__cause__``.
    """

    operation: str  # "connect" or "reqPositions"
    detail: str
    underlying_exception_class: str


async def fetch_recon_positions(
    *,
    account_id: str | None = None,
    host: str = "ib_gateway",
    port: int = 4004,
    client_id: int = DEFAULT_RECON_CLIENT_ID,
    client_factory: Callable[[], IbkrClient] | None = None,
    connect_timeout_seconds: float = 30.0,
) -> tuple[ReconPosition, ...]:
    """Fetch the broker's current open positions via a per-cycle TWS session.

    Construct → connect → ``get_positions`` → disconnect, mirroring the
    ``bar_sync`` per-cycle pattern. Zero-quantity positions are dropped (matches
    ``build_broker_view`` / ``build_backend_view`` so the planner's symmetric-
    difference comparison doesn't generate false-positive breaks for closed
    positions the broker still lists). The disconnect runs in a ``finally``
    block and swallows its own errors so a disconnect failure never masks a
    successful fetch.

    :param account_id: IBKR account number (e.g. ``"U25655583"``). When ``None``
        the adapter uses the default account on the TWS session.
    :param host: ib_gateway hostname (Docker DNS name on the internal network).
    :param port: 4004 for paper, 4003 for live (socat-published per the
        gnzsnz/ib-gateway-docker convention).
    :param client_id: TWS API clientId. Defaults to
        :data:`DEFAULT_RECON_CLIENT_ID` (4).
    :param client_factory: test seam — returns an :class:`IbkrClient`. When
        ``None``, a real :class:`IbAsyncIbkrClient` is constructed from the
        host/port/account/clientId/timeout args.
    :param connect_timeout_seconds: max wall-clock for the TWS handshake.
    :returns: tuple of :class:`ReconPosition`, zero-qty rows dropped.
    :raises ReconPositionsFetchError: on terminal connect / ``reqPositions``
        failure. PR-B catches this + falls back to FlexQuery + P1 alert.
    """
    bound = log.bind(
        service_name="reconciliation",
        client_id=client_id,
        account_id=account_id,
    )

    def _build_default_client() -> IbkrClient:
        return IbAsyncIbkrClient(
            host=host,
            port=port,
            account_id=account_id,
            client_id=client_id,
            connect_timeout_seconds=connect_timeout_seconds,
        )

    factory = client_factory if client_factory is not None else _build_default_client
    client = factory()

    bound.info("recon_positions_fetch_started")
    connected = False
    try:
        try:
            await client.connect()
            connected = True
        except IbkrPlacementError as exc:
            bound.error(
                "recon_positions_fetch_failed",
                operation="connect",
                detail=exc.detail,
                underlying_exception_class=exc.underlying_exception_class,
            )
            raise ReconPositionsFetchError(
                operation="connect",
                detail=f"recon ib_gateway connect failed: {exc.detail}",
                underlying_exception_class=exc.underlying_exception_class,
            ) from exc

        # Path B (2026-05-29): a FRESH ``reqPositions`` round-trip (awaits
        # ``positionEnd``), NOT the connect-time cache. Retry on an empty
        # result as belt-and-suspenders for any residual ib-async timing edge;
        # a persistent empty while the backend holds futures is handled by the
        # ``run_eod_cycle`` empty-result guard (degrade, don't auto-halt). See
        # the module docstring "Fresh-fetch (Q4 Path B)" section.
        raw_positions: list[IbkrPosition] = []
        attempts = 0
        for attempt in range(1, RECON_FRESH_FETCH_MAX_ATTEMPTS + 1):
            attempts = attempt
            try:
                raw_positions = await client.get_positions_fresh()
            except IbkrPlacementError as exc:
                bound.error(
                    "recon_positions_fetch_failed",
                    operation="reqPositions",
                    detail=exc.detail,
                    underlying_exception_class=exc.underlying_exception_class,
                )
                raise ReconPositionsFetchError(
                    operation="reqPositions",
                    detail=f"recon reqPositions failed: {exc.detail}",
                    underlying_exception_class=exc.underlying_exception_class,
                ) from exc
            if raw_positions or attempt == RECON_FRESH_FETCH_MAX_ATTEMPTS:
                break
            bound.warning(
                "recon_positions_fetch_empty_retrying",
                attempt=attempt,
                max_attempts=RECON_FRESH_FETCH_MAX_ATTEMPTS,
                settle_seconds=RECON_FRESH_FETCH_SETTLE_SECONDS,
            )
            await asyncio.sleep(RECON_FRESH_FETCH_SETTLE_SECONDS)

        positions = tuple(
            ReconPosition(market=pos.contract.market, quantity=pos.quantity)
            for pos in raw_positions
            if pos.quantity != 0
        )
        bound.info(
            "recon_positions_fetch_completed",
            positions_count=len(positions),
            raw_count=len(raw_positions),
            attempts=attempts,
        )
        return positions
    finally:
        # Only disconnect a session we actually opened. Best-effort: a
        # disconnect failure must never mask the fetched result (or a
        # fetch failure being propagated) — swallow + log.
        if connected:
            try:
                await client.disconnect()
            except Exception as exc:
                bound.warning("recon_positions_disconnect_failed", error=str(exc))


__all__ = [
    "DEFAULT_RECON_CLIENT_ID",
    "ReconPosition",
    "ReconPositionsFetchError",
    "fetch_recon_positions",
]
