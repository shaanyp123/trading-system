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


class PositionsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    positions: list[Position]
    as_of: datetime


__all__ = ["Position", "PositionCluster", "PositionsResponse"]
