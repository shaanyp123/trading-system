"""services/api/schemas/fills.py — `/api/fills` schema.

Backend-spec §4.1.5b ``Fill`` + ``FillsResponse``.

Underlying table is ``fills`` (alembic 0002 §3.5) — partitioned by
``created_at``; FK to ``orders`` is non-strict (composite PK on the parent).
The repo will JOIN ``fills → orders`` to derive ``signal_uuid`` since fills
themselves only carry ``order_id``.

Spec field name → DB column name mapping (for the eventual repo):

  * ``fill_uuid``       ← ``fills.id`` (UUID).
  * ``order_uuid``      ← ``fills.order_id``.
  * ``signal_uuid``     ← JOIN ``orders.signal_id`` via ``orders.id =
                          fills.order_id``.
  * ``instrument_id``   ← derived from ``orders.contract_id`` / ``orders.market``.
  * ``side``            ← ``orders.direction``.
  * ``qty``             ← ``fills.fill_quantity``.
  * ``price``           ← ``fills.fill_price``.
  * ``slippage_bps``    ← ``fills.realized_slippage_bps``.
  * ``expected_price`` — derived from ``signals.expected_fill_price`` via
                         the orders.signal_id linkage; nullable when the
                         signal didn't carry an expected price (Phase 0
                         strategy doesn't yet emit expected_fill_price).
  * ``filled_at``       ← ``fills.filled_at_utc``.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

FillSide = Literal["buy", "sell"]


class Fill(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fill_uuid: str
    order_uuid: str
    signal_uuid: str
    instrument_id: str
    side: FillSide
    qty: int
    price: Decimal
    slippage_bps: Decimal
    expected_price: Decimal
    filled_at: datetime


class FillsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fills: list[Fill]
    next_cursor: str | None
    has_more: bool


__all__ = ["Fill", "FillSide", "FillsResponse"]
