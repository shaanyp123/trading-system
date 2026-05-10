"""services/api/schemas/orders.py — `/api/orders` schema.

Backend-spec §4.1.5b ``Order`` + ``OrdersResponse``.

Underlying table is ``orders`` (alembic 0002 §3.4) — partitioned by
``created_at``. Phase 0 returns ``orders=[]`` since no execution path emits
orders yet.

Spec response shape uses ``order_uuid`` (string) where the DB column is
``id`` (UUID). The spec status enum is a strict subset of the DB CHECK
constraint — DB allows ``pending`` and ``expired`` which the response model
maps to ``"working"`` and ``"cancelled"`` respectively (the closest semantic
neighbours), per the Day 5 carryover lesson that response shapes must match
the spec exactly even when the DB has more granular states.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

OrderStatus = Literal["working", "partially_filled", "filled", "cancelled", "rejected"]
OrderSide = Literal["buy", "sell"]
OrderType = Literal["limit_marketable", "stop_market", "limit"]


class Order(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_uuid: str
    client_order_id: str
    broker_order_id: str | None
    signal_uuid: str | None
    instrument_id: str
    side: OrderSide
    qty: int
    order_type: OrderType
    limit_price: Decimal | None
    status: OrderStatus
    submitted_at: datetime
    filled_qty: int
    filled_avg_price: Decimal | None


class OrdersResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    orders: list[Order]
    next_cursor: str | None
    has_more: bool


__all__ = ["Order", "OrderSide", "OrderStatus", "OrderType", "OrdersResponse"]
