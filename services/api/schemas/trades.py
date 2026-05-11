"""services/api/schemas/trades.py — `/api/trades` schema.

Backend-spec §4.1.2 — ``GET /api/trades`` (paginated list) +
``GET /api/trades/:id`` (single-row detail). The underlying table is
``trades`` (alembic 0002 §3.6 — partitioned by ``created_at``); Phase 0
returns empty lists / 404 since no execution path produces trades until
Week 7 Thu paper round-trip.

The list shape ``TradeSummary`` carries enough columns for the
``/trades`` filterable summary table (frontend-spec §2.3.1 layout —
Date / Market / Dir / Size / Status / Entry / Exit / P&L / Vers) plus
the canonical ``signal_uuid`` and ``id_prefix`` lookup key that the
command-palette (Phase 2) needs.

The detail shape ``TradeDetail`` carries the additional fields needed
for the minimal Phase-1 detail page (frontend-spec §2.3.4 — full
strategy_hash, parameter_set_hash, slippage_calibration_version,
commission, dividend P&L). The Phase 2 drawer view (lifecycle timeline,
attribution waterfall, decision diary linkage, agent commentary)
extends this shape later; the Phase 1 wire stays a strict subset so the
detail page never needs to short-circuit on missing fields when the
drawer ships.

Spec field name → DB column name mapping:

  * ``id``                          ← ``trades.id`` (UUID; primary key
                                       segment, composite with
                                       ``created_at`` for partition pruning).
  * ``account_id``                  ← ``trades.account_id``.
  * ``env``                         ← ``trades.env`` (``paper`` / ``live-small``
                                       / ``live-scale``).
  * ``market``                      ← ``trades.market``.
  * ``direction``                   ← ``trades.direction`` (``long`` / ``short``).
  * ``state``                       ← ``trades.state`` (CHECK in 0002 line 271-272).
  * ``opened_at_utc``               ← ``trades.opened_at_utc``.
  * ``closed_at_utc``               ← ``trades.closed_at_utc`` (nullable while open).
  * ``total_quantity``              ← ``trades.total_quantity``.
  * ``avg_entry_price``             ← ``trades.avg_entry_price``.
  * ``avg_exit_price``              ← ``trades.avg_exit_price`` (nullable).
  * ``realized_pnl_usd``            ← ``trades.realized_pnl_usd`` (nullable).
  * ``realized_commission_usd``     ← ``trades.realized_commission_usd``.
  * ``dividend_pnl_usd``            ← ``trades.dividend_pnl_usd`` (ETF only;
                                       defaults to 0).
  * ``entry_signal_id``             ← ``trades.entry_signal_id``.
  * ``entry_order_id``              ← ``trades.entry_order_id``.
  * ``exit_order_id``               ← ``trades.exit_order_id`` (nullable while
                                       open).
  * ``managed_by_version``          ← ``trades.managed_by_version`` (40-char
                                       SHA-1 short hash of strategy_version).
  * ``managed_by_strategy_version`` — first 7 chars of ``managed_by_version``
                                       (frontend renders this as the "Vers"
                                       column; backend computes inline since
                                       the DB stores the full 40-char form).
  * ``strategy_hash``               ← ``trades.strategy_hash`` (40-char).
  * ``parameter_set_hash``          ← ``trades.parameter_set_hash`` (64-char).
  * ``slippage_calibration_version_id`` ← ``trades.slippage_calibration_version_id``.

Decimals serialize as strings per dev-guide §3.8 (no float for money;
JCS-canonical wire). Timestamps as RFC 3339 UTC ``Z``-suffix strings.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

TradeDirection = Literal["long", "short"]
TradeState = Literal[
    "open_position",
    "closed",
    "stopped_out",
    "capacity_constrained",
]


class TradeSummary(BaseModel):
    """Row shape rendered in the ``/trades`` table.

    Carries enough columns for the spec §2.3.1 layout — Date / Market /
    Dir / Size / Status / Entry / Exit / P&L / Vers — plus the canonical
    ``id`` (UUID string) used as the row key + click-through to detail,
    and ``entry_signal_id`` so Discord ``#signals`` embed deep-links
    that arrive at ``/trades/:signal_uuid`` can resolve (the route
    handler tries trade_id-by-PK first, then falls back to
    entry_signal_id matching).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    env: str
    market: str
    direction: TradeDirection
    state: TradeState
    opened_at_utc: datetime
    closed_at_utc: datetime | None
    total_quantity: int
    avg_entry_price: Decimal
    avg_exit_price: Decimal | None
    realized_pnl_usd: Decimal | None
    entry_signal_id: str
    managed_by_strategy_version: str  # 7-char short hash for the "Vers" column


class TradeDetail(BaseModel):
    """Single-row payload rendered at ``/trades/:id``.

    Extends ``TradeSummary`` with the fields the minimal Phase-1 detail
    page (spec §2.3.4) needs: full ``managed_by_version`` SHA-1,
    strategy + parameter-set hashes, slippage calibration version,
    commission, dividend P&L, entry/exit order UUIDs.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    account_id: str
    env: str
    market: str
    direction: TradeDirection
    state: TradeState
    opened_at_utc: datetime
    closed_at_utc: datetime | None
    total_quantity: int
    avg_entry_price: Decimal
    avg_exit_price: Decimal | None
    realized_pnl_usd: Decimal | None
    realized_commission_usd: Decimal
    dividend_pnl_usd: Decimal
    entry_signal_id: str
    entry_order_id: str
    exit_order_id: str | None
    managed_by_version: str
    managed_by_strategy_version: str
    strategy_hash: str
    parameter_set_hash: str
    slippage_calibration_version_id: str


class TradesListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trades: list[TradeSummary]
    next_cursor: str | None
    has_more: bool


__all__ = [
    "TradeDetail",
    "TradeDirection",
    "TradeState",
    "TradeSummary",
    "TradesListResponse",
]
