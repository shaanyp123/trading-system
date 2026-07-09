"""services/api/position_mtm.py — position mark-to-market helpers.

Backend-spec §4.1.5b ``Position.current_price`` + ``Position.unrealized_pnl``.

Crypto-pivot §3.9 (2026-07-09): the "§3.2 source will replace this"
promise from the C0 decommission is now FULFILLED — the positions route
(``services/api/routes/positions.py``) marks against the Coinbase
market-data worker's live WS marks (rung 1 of the mark ladder, via
``services/api/marks.py``) and the 00:15 UTC recon-written
``positions_current.unrealized_pnl`` (rung 2, writer:
``services/reconciliation/eod_cycle.py``). This module keeps only what
that ladder still needs:

* :func:`unrealized_pnl` — the pure Decimal uPnL formula for a live mark
  (rung 1), kept here so the route stays thin and tests pin the math.
* :func:`compute_position_mtm` — the avg-cost fallback (rung 3, the
  long-standing Phase-1 contract): ``current_price = avg_cost``,
  ``unrealized_pnl = 0``, ``price_as_of = None``. Taken only when no
  fresh live mark exists AND recon hasn't written a mark.
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
    ``source`` is the diagnostic string surfaced in structlog / the wire
    payload's ``mark_source``. ``price_as_of`` is the retiring bar-date
    field — always ``None`` on the fallback path (kept until the §3.9
    frontend PR removes the wire field).
    """

    current_price: Decimal
    unrealized_pnl: Decimal
    source: str  # "fallback_avg_cost" / "unknown_market"
    price_as_of: str | None = None


def unrealized_pnl(
    mark: Decimal,
    avg_cost: Decimal,
    quantity: int,
    multiplier: Decimal,
) -> Decimal:
    """Signed uPnL for one position at a live mark (§3.9 ladder rung 1).

    ``(mark - avg_cost) x qty x multiplier`` — for CDE perps the
    multiplier is ``contracts.multiplier`` (the nano contract size the
    strategy worker wrote from runtime discovery, [A13]); omitting it is
    the exact futures-math bug the recon writer refuses to risk (see
    ``services/reconciliation/eod_cycle.py`` mark-source note). Pure
    Decimal end to end ([A05]).
    """
    return (mark - avg_cost) * Decimal(quantity) * multiplier


def compute_position_mtm(
    market: str,
    quantity: int,
    avg_cost: Decimal,
) -> PositionMtmResult:
    """Avg-cost fallback mark (§3.9 ladder rung 3) for a single position.

    Returns ``current_price=avg_cost``, ``unrealized_pnl=0`` — the
    honest "no price source reached this row" rendering. The ``source``
    field distinguishes a known market on the fallback path from a
    market with no exposure metadata at all.
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
    "unrealized_pnl",
]
