"""services/api/schemas/positions.py — `/api/positions/current` schema.

Backend-spec §4.1.5b ``Position`` + ``PositionsResponse``.

Underlying table is ``positions_current`` (alembic 0002 §3.8). Phase 0 returns
``positions=[]`` since no fills have landed yet. The mapper code below sketches
the column→field mapping so future authors don't have to re-derive it; spec
field names diverge from DB column names in a few places (``symbol`` vs
``market``, ``avg_entry_price`` vs ``avg_cost``, etc.) and the comment block
on ``Position`` enumerates each.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

PositionCluster = Literal["equity_index", "commodity", "rates_bonds", "crypto", "fx"]


class Position(BaseModel):
    """Single open position (futures contract or ETF symbol).

    Spec → DB column mapping (for the eventual repo implementation):

      * ``instrument_id`` ← ``positions_current.contract_id`` (futures) or
        derived from ``market`` (ETFs); spec calls for a stable string id.
      * ``symbol``        ← ``positions_current.market`` for ETFs; for futures,
        compose ``"<root> <contract_month>"`` from joined ``contracts`` row.
      * ``contract_month`` ← derived from ``contracts.expiration`` (NULL for
        ETFs). Spec returns ``None`` for non-futures.
      * ``qty``           ← ``positions_current.quantity``.
      * ``avg_entry_price`` ← ``positions_current.avg_cost``.
      * ``current_price`` — not in DB; mark via QC ObjectStore portfolio.json
        snapshot. Phase 0 returns ``avg_cost`` as a placeholder when no mark
        is available.
      * ``unrealized_pnl`` ← ``positions_current.unrealized_pnl``.
      * ``unrealized_pnl_pct_of_nav`` — derived: ``unrealized_pnl / NAV``;
        NAV from ``balances`` table (last row).
      * ``cluster``       ← derived from ``contracts.cluster`` join.
      * ``managed_by_strategy_version`` ← ``managed_by_version`` (CHAR(40)
        → returned as 7-char short hash).
    """

    model_config = ConfigDict(extra="forbid")

    instrument_id: str
    symbol: str
    contract_month: str | None
    qty: int
    avg_entry_price: Decimal
    current_price: Decimal
    unrealized_pnl: Decimal
    unrealized_pnl_pct_of_nav: Decimal
    cluster: PositionCluster | None
    managed_by_strategy_version: str
    #: Session date (ET, ``YYYY-MM-DD``) of the bar ``current_price`` was read
    #: from — i.e. when bar_sync last produced data for this market. ``None``
    #: when the price fell back to avg_cost (no fresh bar on disk). Lets the UI
    #: show "price as of <date>" + flag a market whose bar lags the others.
    price_as_of: str | None = None


class PositionsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    positions: list[Position]
    as_of: datetime
    #: The most-recent bar date across all positions (``YYYY-MM-DD``) — a
    #: single "prices as of" headline for the table. ``None`` when no position
    #: has a fresh bar. Per-position ``price_as_of`` reveals any straggler.
    prices_as_of: str | None = None


__all__ = ["Position", "PositionCluster", "PositionsResponse"]
