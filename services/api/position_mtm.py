"""services/api/position_mtm.py — on-read mark-to-market for open positions.

Backend-spec §4.1.5b ``Position.current_price`` + ``Position.unrealized_pnl``.

Crypto-pivot C0 (2026-07-08, delta spec §1/§3.2): the Phase-1 price source
for this module was the LEAN on-disk daily zips written by the (now
retired) ``services.data.bar_sync`` worker. With that data layer
decommissioned there is no on-disk bar source, so every position marks at
its ``avg_cost`` with ``unrealized_pnl=0`` — the module's long-standing
fallback contract, now the only path. The Coinbase market-data service
(delta spec §3.2, ``services/data/coinbase_market_data.py``) becomes the
mark source in the C0 build and will replace this fallback with live perp
marks.

Fallback semantics (unchanged from Phase 1):

* ``current_price = avg_cost``, ``unrealized_pnl = 0``
* ``source`` is the diagnostic string surfaced in structlog / the wire
  payload: ``fallback_avg_cost`` for known-shape markets, or
  ``unknown_market`` when the market has no exposure metadata.
* ``price_as_of = None`` — no fresh bar was read.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import structlog

from services.api.exposure import V1_EXPOSURE_METADATA

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class PositionMtmResult:
    """Return shape for :func:`compute_position_mtm`.

    ``current_price`` falls back to ``avg_cost`` so the route caller can
    render the position row without a null. ``unrealized_pnl`` is signed.
    ``source`` is the diagnostic string surfaced in structlog for
    missing-data debugging. ``price_as_of`` is the priced bar's session
    date (ISO ``YYYY-MM-DD``) — always ``None`` until the §3.2 Coinbase
    market-data source lands.
    """

    current_price: Decimal
    unrealized_pnl: Decimal
    source: str  # "fallback_avg_cost" / "unknown_market"
    price_as_of: str | None = None


def compute_position_mtm(
    market: str,
    quantity: int,
    avg_cost: Decimal,
) -> PositionMtmResult:
    """Compute (current_price, unrealized_pnl) for a single position.

    C0 decommission state: no live price source exists (bar_sync/LEAN
    retired; Coinbase marks not yet wired), so this always returns the
    fallback (``current_price=avg_cost``, ``unrealized_pnl=0``). The
    ``source`` field distinguishes a known market on the fallback path
    from a market with no exposure metadata at all.
    """
    if V1_EXPOSURE_METADATA.get(market) is None:
        log.warning("position_mtm_unknown_market", market=market)
        return PositionMtmResult(
            current_price=avg_cost,
            unrealized_pnl=Decimal("0"),
            source="unknown_market",
        )
    return PositionMtmResult(
        current_price=avg_cost,
        unrealized_pnl=Decimal("0"),
        source="fallback_avg_cost",
        price_as_of=None,
    )


__all__ = [
    "PositionMtmResult",
    "compute_position_mtm",
]
