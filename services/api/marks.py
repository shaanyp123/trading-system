"""services/api/marks.py — process-local live-mark-store holder.

Crypto-pivot §3.9 (``Docs/crypto-pivot-delta-spec.md``): the positions
read surface needs the live perp marks the api-resident Coinbase
market-data worker (delta spec §3.2,
``services/data/coinbase_market_data.py``) already maintains in its
in-memory ``MarkStore``. Pre-this-module there was no seam between the
lifespan-owned worker object and the route layer — the positions mapper
could only mark at ``avg_cost`` even while fresh WS ticks sat one
attribute away.

This module is that seam: a module-level holder the lifespan registers
``worker.mark_store`` into after start and clears on stop, mirroring the
:mod:`services.api.heartbeats` module-singleton precedent (one
process-wide instance, retrieved via a getter; routes never construct
it). The holder is deliberately typed against a narrow
:class:`MarkStoreLike` Protocol so route modules don't import the whole
worker module (httpx/websockets surface) just to read a price.

**Staleness contract:** a mark older than
:data:`MARK_STALE_THRESHOLD_S` is treated as ABSENT by callers — the
same 3-minute policy the worker's staleness watchdog enforces (strategy
§7 data-outage row; ``CoinbaseMarketDataConfig.stale_threshold_s``).
Serving a stale mark as live would be dishonest exactly when the
operator most needs to know the feed is down; callers fall through to
their next mark source instead. :func:`fresh_mark` packages the check.

In-memory by design — lost on api restart and honestly reported as
absent (``get_mark_store()`` returns None) until the lifespan re-registers
a store, mirroring the heartbeat-registry semantics.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Final, Protocol

#: Mark age (seconds) past which a live mark is treated as absent.
#: Mirrors the market-data worker's locked 3-minute staleness policy
#: (strategy §7 / ``CoinbaseMarketDataConfig.stale_threshold_s``);
#: duplicated here so route modules don't import the worker module.
MARK_STALE_THRESHOLD_S: Final[float] = 180.0


class MarkLike(Protocol):
    """Structural view of one mark record (``coinbase_market_data.Mark``)."""

    @property
    def price(self) -> Decimal: ...

    @property
    def observed_at_utc(self) -> datetime: ...


class MarkStoreLike(Protocol):
    """The narrow read surface routes need from the worker's ``MarkStore``."""

    def latest(self, product_id: str) -> MarkLike | None: ...


#: Module-level holder. The convention mirrors the heartbeat registry:
#: one process-wide slot, written only by the api lifespan.
_MARK_STORE: MarkStoreLike | None = None


def set_mark_store(store: MarkStoreLike) -> None:
    """Register the market-data worker's live mark store.

    Called by the api lifespan immediately after the worker task spawns;
    idempotent (a re-registration simply replaces the reference).
    """
    global _MARK_STORE
    _MARK_STORE = store


def get_mark_store() -> MarkStoreLike | None:
    """Return the registered store, or None when the worker isn't running.

    None is the honest no-live-marks signal (worker disabled via setting,
    startup failed, or shutdown in progress) — callers fall through to
    their next mark source.
    """
    return _MARK_STORE


def clear_mark_store() -> None:
    """Drop the registration (lifespan shutdown path; also test teardown)."""
    global _MARK_STORE
    _MARK_STORE = None


def fresh_mark(
    store: MarkStoreLike,
    product_id: str,
    now_utc: datetime,
) -> tuple[Decimal, datetime] | None:
    """Return ``(price, observed_at_utc)`` iff the mark exists and is fresh.

    A mark older than :data:`MARK_STALE_THRESHOLD_S` returns ``None`` —
    stale marks are treated as absent per the worker's 3-minute policy
    (see module docstring). Raises :class:`ValueError` on a naive
    ``now_utc`` (dev-guide §3 / A06).
    """
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be tz-aware (dev-guide §3 / A06); got naive")
    mark = store.latest(product_id)
    if mark is None:
        return None
    age_s = (now_utc - mark.observed_at_utc).total_seconds()
    if age_s > MARK_STALE_THRESHOLD_S:
        return None
    return mark.price, mark.observed_at_utc


__all__ = [
    "MARK_STALE_THRESHOLD_S",
    "MarkLike",
    "MarkStoreLike",
    "clear_mark_store",
    "fresh_mark",
    "get_mark_store",
    "set_mark_store",
]
