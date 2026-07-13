"""services/reconciliation/coinbase_fetcher.py — Coinbase EOD recon fetcher.

Crypto-pivot C0 §3.5 (delta spec, 2026-07-09). Pulls the end-of-day
reconciliation snapshot from the Coinbase Advanced Trade surface via the
:class:`~services.execution.coinbase_client.CoinbaseBrokerClient`
transport Protocol — the fetcher-swap replacement for the retired IBKR
FlexQuery XML path (``flex_query_fetcher.py``, deleted in this PR per
the operator's dead-code mandate).

**What one snapshot carries (delta spec §3.5):**

* ``list_positions`` → the venue's CFM futures positions, mapped to
  the venue-neutral :class:`ReconPosition` shape the EOD cycle's
  ``positions_override`` seam consumes (the seam was deliberately
  preserved in crypto-pivot C0-B2b for exactly this fetcher).
* ``get_futures_balance_summary`` → equity/margin. ``cash_usd`` is the
  venue's ``total_usd_balance`` (aggregate USD across CFM + CBI — the
  system's cash lives on both sides of the §3.6 sweep boundary);
  ``net_liquidation_usd`` = ``total_usd_balance`` + ``unrealized_pnl``.
  Both raw fields are stamped into the balance-snapshot audit payload
  so any venue-side semantics surprise is forensically visible; the
  recon cash tolerance is re-based in Phase C1 config per the spec.
* ``list_fills`` (lookback window) → execution legs for the snapshot's
  operator/forensic record. The recon planner itself consumes only
  positions + cash; fills are informational, so a fills-fetch failure
  degrades (logged WARNING, empty tuple) instead of failing the cycle.
* **Funding settlements:** the broker transport Protocol exposes no
  dedicated funding-settlement endpoint. Hourly CDE funding telemetry
  is persisted by the §3.2 market-data worker into ``funding_rates``;
  venue-side funding cash movements surface in the balance summary the
  snapshot already carries. Nothing additional to fetch here.

**Market symbol convention (crypto, corrected 2026-07-11):**
``positions_current.market`` uses the BASE-ASSET symbol (``"BTC"`` /
``"ETH"``) — the convention the C1 strategy worker's fill pipeline
writes (#355). The venue reports positions under CDE ``product_id``s
(``BIP-20DEC30-CDE``), so this fetcher NORMALIZES ``product_id`` →
``base_asset`` at the boundary using the runtime-discovered product
list (``list_perp_products`` — never hardcoded, [A13]). The original
§3.5 assumption that ``positions_current`` carried product ids was
wrong against the worker as built and produced a mirror-image phantom
break pair (+auto-halt) on the first overnight position, 2026-07-11
00:15 UTC — see decisions-log. A product id absent from the discovery
map passes through VERBATIM with a WARNING: an instrument the system
cannot identify must surface as a recon break, never be silently
normalized away. Discovery returning an empty map while positions are
open fails the cycle (``CoinbaseReconFetchError``) — that is the #358
zero-products defect class, not a rogue position.

**Error contract:** transport failures on the load-bearing calls
(positions, balance summary) raise :class:`CoinbaseReconFetchError`;
``run_eod_cycle`` logs + returns ``None`` so the scheduler survives a
venue outage and tomorrow's cycle still fires — the same soft-fail
contract the FlexQuery client had.

Construction mirrors ``coinbase_adapter.CoinbaseExecutionAdapter``: the
transport ``client`` is keyword-injected (tests fake the Protocol; the
api lifespan constructs an ``SdkCoinbaseBrokerClient`` from the
``coinbase.api_key_name`` / ``coinbase.api_private_key`` secrets). No
transport logic is duplicated here.

A02 BINDS — ``services/reconciliation/**`` forbidden path;
``risk-review-approved`` required.
A05 enforced — Decimals end-to-end (the transport already coerces at
the venue boundary); serialized as strings in audit payloads.
A06 enforced — every datetime tz-aware UTC.
A25 — structlog only.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final, Protocol

import structlog

from services.execution.coinbase_client import CoinbaseBrokerClient
from services.execution.types import (
    BrokerError,
    BrokerFill,
    BrokerPosition,
    FuturesBalanceSummary,
)

log = structlog.get_logger()


#: Fills lookback for the snapshot's informational fill list: one EOD
#: cycle spacing (24h) + a 2h cushion for a late/re-run cycle. Fills are
#: NOT consumed by the recon planner (positions + cash are); the window
#: only bounds the forensic record attached to each snapshot.
DEFAULT_FILLS_LOOKBACK_HOURS: Final[int] = 26


@dataclass(frozen=True, slots=True)
class ReconPosition:
    """Minimal position shape the reconciliation planner ingests.

    Venue-neutral. Lived in ``eod_cycle.py`` between crypto-pivot
    C0-B2b (which moved it out of the deleted IBKR intraday fetcher and
    preserved the ``positions_override`` seam) and this §3.5 PR, which
    gives it the same home the seam's producer has — mirroring the old
    fetcher-owns-its-output-shape layout. ``eod_cycle`` re-exports it.

    * ``market`` — canonical symbol matching ``positions_current.market``
      (post-pivot: the BASE-ASSET symbol, e.g. ``"BTC"`` — the fetcher
      normalizes venue product ids via the runtime discovery map; an
      unmapped product id passes through verbatim, fail-visible).
    * ``quantity`` — signed (positive = long, negative = short).
    """

    market: str
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class CoinbaseReconSnapshot:
    """One EOD reconciliation snapshot from the Coinbase venue.

    The consumable input for ``run_eod_cycle``: ``positions`` feeds the
    planner's broker view through the ``positions_override`` seam;
    ``position_details`` carries the full venue rows for the
    mark-to-market refresh; ``cash_usd`` / ``net_liquidation_usd`` are
    derived from ``balance_summary`` (see module docstring for the
    field semantics); ``fills`` is the informational execution record.
    """

    positions: tuple[ReconPosition, ...]
    position_details: tuple[BrokerPosition, ...]
    balance_summary: FuturesBalanceSummary
    cash_usd: Decimal
    net_liquidation_usd: Decimal
    fills: tuple[BrokerFill, ...]
    pulled_at_utc: datetime
    product_to_asset: dict[str, str]
    """Runtime-discovered ``product_id`` → ``base_asset`` map used to
    normalize ``positions`` — carried on the snapshot so the EOD
    cycle's mark-to-market pass applies the SAME normalization when it
    looks up ``positions_current`` rows for ``position_details`` (raw
    venue rows, deliberately un-normalized for forensics).

    REQUIRED, deliberately (#375 review note N4): the field originally
    defaulted to ``{}``, which meant a future snapshot producer (the
    Phase C1 intraday probe reusing this dataclass) could forget the
    discovery map and every MTM lookup would silently fall back to raw
    venue product-id keys — the exact market-key mismatch class behind
    the 2026-07-11 phantom-break auto-halt. A producer with a genuinely
    flat book and empty discovery passes ``{}`` explicitly."""


class CoinbaseReconFetchError(Exception):
    """A load-bearing venue call failed; the cycle must soft-fail.

    Mirrors the retired ``FlexQueryFetchError`` contract: the EOD cycle
    logs at WARNING + returns ``None`` and the scheduler keeps running.
    """

    def __init__(self, *, operation: str, detail: str) -> None:
        self.operation = operation
        self.detail = detail
        super().__init__(f"{operation}: {detail}")


class ReconSnapshotFetcher(Protocol):
    """The seam ``run_eod_cycle`` programs against; tests inject fakes."""

    async def fetch_snapshot(self) -> CoinbaseReconSnapshot:
        """Pull one EOD snapshot; raise :class:`CoinbaseReconFetchError`
        when the venue's load-bearing calls fail."""
        ...


def recon_positions_from_broker(
    positions: tuple[BrokerPosition, ...] | list[BrokerPosition],
    *,
    product_to_asset: Mapping[str, str],
) -> tuple[ReconPosition, ...]:
    """Map venue positions → planner-ready :class:`ReconPosition` rows.

    Pure policy — no I/O. Zero-quantity rows are dropped (matches the
    backend view builder so closed positions the venue still lists
    don't generate false-positive breaks). Rows missing a ``product_id``
    are skipped with a WARNING — an empty market key could never match
    a backend row and would fabricate a phantom break.

    ``product_to_asset`` (runtime-discovered, [A13]) normalizes venue
    product ids to the base-asset symbols ``positions_current.market``
    actually carries (worker convention, #355). A product id absent
    from the map passes through VERBATIM with a WARNING — fail-visible:
    an instrument the system can't identify must surface as a break.
    Quantities are SUM-aggregated per resulting market key (defensive;
    one CDE product per asset today makes this a no-op).
    """
    aggregated: dict[str, Decimal] = {}
    for pos in positions:
        if not pos.product_id:
            log.warning(
                "coinbase_recon_position_missing_product_id",
                contracts=str(pos.contracts),
            )
            continue
        if pos.contracts == 0:
            continue
        asset = product_to_asset.get(pos.product_id)
        if asset is None:
            log.warning(
                "coinbase_recon_position_unmapped_product",
                product_id=pos.product_id,
                contracts=str(pos.contracts),
                note=(
                    "product id not in the runtime discovery map; market key "
                    "passes through verbatim and will surface as a recon "
                    "break if the backend holds no matching row"
                ),
            )
        market = asset if asset is not None else pos.product_id
        aggregated[market] = aggregated.get(market, Decimal(0)) + pos.contracts
    # A zero SUM (offsetting products on one asset) drops like any other
    # zero-quantity row — the documented contract this function keeps.
    return tuple(ReconPosition(market=m, quantity=q) for m, q in aggregated.items() if q != 0)


class CoinbaseEodFetcher:
    """EOD reconciliation snapshot fetcher over the Coinbase transport.

    Same construction pattern as the execution adapter: the transport
    ``client`` (Protocol) is keyword-injected; ``clock`` is injectable
    so tests pin ``pulled_at_utc``.
    """

    def __init__(
        self,
        *,
        client: CoinbaseBrokerClient,
        fills_lookback_hours: int = DEFAULT_FILLS_LOOKBACK_HOURS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if fills_lookback_hours <= 0:
            raise ValueError(f"fills_lookback_hours must be positive; got {fills_lookback_hours}")
        self._client = client
        self._fills_lookback_hours = fills_lookback_hours
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(tz=UTC))
        self._log = log.bind(fetcher="coinbase_eod")

    async def fetch_snapshot(self) -> CoinbaseReconSnapshot:
        """Pull positions + balance summary (+ best-effort fills).

        Raises :class:`CoinbaseReconFetchError` when the venue calls the
        planner depends on (positions, balance summary) fail or the
        balance summary omits ``total_usd_balance`` — a missing equity
        anchor must fail safe, never default to zero ([A05] discipline:
        no fabricated money values).
        """
        pulled_at = self._clock()
        if pulled_at.tzinfo is None:
            raise ValueError("clock must return tz-aware UTC datetimes per [A06]")

        try:
            broker_positions = await self._client.list_positions()
            summary = await self._client.get_futures_balance_summary()
            perp_products = await self._client.list_perp_products()
        except BrokerError as exc:
            raise CoinbaseReconFetchError(operation=exc.operation, detail=exc.detail) from exc

        product_to_asset = {p.product_id: p.base_asset for p in perp_products}
        open_positions = [p for p in broker_positions if p.contracts != 0 and p.product_id]
        if open_positions and not any(p.product_id in product_to_asset for p in open_positions):
            # The #358 defect class: discovery returned nothing usable
            # while the venue holds positions. Normalization would turn
            # EVERY open position into a phantom break pair (+auto-halt);
            # soft-fail the cycle instead — tomorrow retries.
            raise CoinbaseReconFetchError(
                operation="list_perp_products",
                detail=(
                    f"discovery mapped 0 of {len(open_positions)} open "
                    "positions; refusing to reconcile against phantom "
                    "market keys"
                ),
            )

        if summary.total_usd_balance is None:
            raise CoinbaseReconFetchError(
                operation="get_futures_balance_summary",
                detail=(
                    "venue omitted total_usd_balance; refusing to reconcile "
                    "against a fabricated zero"
                ),
            )

        cash_usd = summary.total_usd_balance
        unrealized = summary.unrealized_pnl
        if unrealized is None:
            if any(p.contracts != 0 for p in broker_positions):
                self._log.warning(
                    "coinbase_recon_unrealized_pnl_missing_with_open_positions",
                    position_count=len(broker_positions),
                    note=(
                        "balance summary omitted unrealized_pnl while positions "
                        "are open; net_liquidation_usd falls back to "
                        "total_usd_balance and may under/over-state NLV"
                    ),
                )
            unrealized = Decimal(0)
        net_liquidation_usd = cash_usd + unrealized

        fills: tuple[BrokerFill, ...]
        try:
            fills = tuple(
                await self._client.list_fills(
                    start_utc=pulled_at - timedelta(hours=self._fills_lookback_hours)
                )
            )
        except BrokerError as exc:
            # Informational surface only — degrade, don't fail the cycle.
            self._log.warning(
                "coinbase_recon_fills_fetch_degraded",
                operation=exc.operation,
                detail=exc.detail,
                lookback_hours=self._fills_lookback_hours,
            )
            fills = ()

        positions = recon_positions_from_broker(broker_positions, product_to_asset=product_to_asset)
        self._log.info(
            "coinbase_recon_snapshot_pulled",
            position_count=len(positions),
            fill_count=len(fills),
            cash_usd=str(cash_usd),
            net_liquidation_usd=str(net_liquidation_usd),
            discovered_product_count=len(product_to_asset),
            pulled_at_utc=pulled_at.isoformat(),
        )
        return CoinbaseReconSnapshot(
            positions=positions,
            position_details=tuple(broker_positions),
            balance_summary=summary,
            cash_usd=cash_usd,
            net_liquidation_usd=net_liquidation_usd,
            fills=fills,
            pulled_at_utc=pulled_at,
            product_to_asset=product_to_asset,
        )


__all__ = [
    "DEFAULT_FILLS_LOOKBACK_HOURS",
    "CoinbaseEodFetcher",
    "CoinbaseReconFetchError",
    "CoinbaseReconSnapshot",
    "ReconPosition",
    "ReconSnapshotFetcher",
    "recon_positions_from_broker",
]
