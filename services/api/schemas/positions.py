"""services/api/schemas/positions.py — `/api/positions/current` schema.

Backend-spec §4.1.5b ``Position`` + ``PositionsResponse``.

Underlying table is ``positions_current`` (alembic 0002 §3.8).
Crypto-pivot §3.9 (``Docs/crypto-pivot-delta-spec.md``): the CDE-perp
read fields (``asset``, client/native stop, mark provenance) are ADDITIVE
with None defaults so every existing construction — and the deployed
frontend — keeps working unchanged. The wire contract of the pre-pivot
fields (names + semantics) is intentionally frozen until the §3.9
frontend-deltas PR lands; ``contract_month`` and ``prices_as_of`` are
retiring but MUST stay (null) until then.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PositionCluster = Literal["equity_index", "commodity", "rates_bonds", "crypto", "fx"]

#: Provenance of ``current_price``/``unrealized_pnl`` — the §3.9 mark
#: ladder, best rung first (see ``services/api/routes/positions.py``):
#: live WS mark → recon-written EOD mark → avg-cost fallback.
MarkSource = Literal["live_ws", "recon_eod", "fallback_avg_cost"]


class Position(BaseModel):
    """Single open position (CDE perp-style product; legacy ETF rows render too).

    Spec → DB column mapping:

      * ``instrument_id`` ← ``positions_current.contract_id`` (stable UUID)
        or the ``market`` string when contract_id IS NULL.
      * ``symbol``        ← ``positions_current.market`` (the CDE
        product_id post-pivot, e.g. ``BIP-20DEC30-CDE``).
      * ``qty``           ← ``positions_current.quantity``.
      * ``avg_entry_price`` ← ``positions_current.avg_cost``.
      * ``current_price`` / ``unrealized_pnl`` — §3.9 mark ladder; see
        ``mark_source`` for which rung priced this row.
      * ``unrealized_pnl_pct_of_nav`` — derived: ``unrealized_pnl / NAV``;
        NAV from ``balances`` (latest row).
      * ``cluster``       ← exposure metadata keyed by
        ``contracts.market_root`` (BTC/ETH → ``crypto``).
      * ``managed_by_strategy_version`` ← ``managed_by_version`` (CHAR(40)
        → returned as 7-char short hash).
      * ``asset`` / ``client_stop_price`` / ``native_stop_*`` — crypto-
        pivot §3.9 additive fields (contracts JOIN + worker engine_state).
    """

    model_config = ConfigDict(extra="forbid")

    instrument_id: str
    symbol: str
    #: RETIRING (crypto-pivot §3.9): CDE perp-style products have no
    #: contract-month notion the UI needs. Kept — always null — for the
    #: deployed frontend until the §3.9 frontend-deltas PR lands.
    contract_month: str | None
    qty: int
    avg_entry_price: Decimal
    current_price: Decimal
    unrealized_pnl: Decimal
    unrealized_pnl_pct_of_nav: Decimal
    cluster: PositionCluster | None
    managed_by_strategy_version: str
    #: RETIRING (crypto-pivot §3.9): the bar_sync session-date provenance
    #: this carried died with the on-disk bar source; mark provenance now
    #: lives in ``mark_source``/``mark_observed_at_utc``. Kept — always
    #: null — for the deployed frontend until the frontend PR lands.
    price_as_of: str | None = None
    asset: str | None = Field(
        default=None,
        description=(
            "Base-asset label (BTC/ETH) from contracts.market_root; None "
            "when the row has no contracts enrichment."
        ),
    )
    client_stop_price: Decimal | None = Field(
        default=None,
        description=(
            "The worker's client-side 2xATR soft stop for this asset "
            "(engine_state.client_stop_level); None when the worker has "
            "no armed stop or hasn't run."
        ),
    )
    native_stop_price: Decimal | None = Field(
        default=None,
        description=(
            "Resting venue stop-limit backstop price (orders.stop_price "
            "of the latest native-stop order); None when no native stop "
            "is tracked or the order row is missing."
        ),
    )
    native_stop_resting: bool | None = Field(
        default=None,
        description=(
            "True when the worker tracks a venue-resting native stop "
            "order id for this asset; None when engine state is absent."
        ),
    )
    mark_observed_at_utc: datetime | None = Field(
        default=None,
        description=(
            "When the price behind current_price was observed (WS tick "
            "time or recon mark time); None on the avg-cost fallback."
        ),
    )
    mark_source: str | None = Field(
        default=None,
        description='Mark-ladder rung: "live_ws" | "recon_eod" | "fallback_avg_cost".',
    )


class PositionsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    positions: list[Position]
    as_of: datetime
    #: RETIRING (crypto-pivot §3.9): headline bar-date companion to the
    #: per-position ``price_as_of``. Kept — always null — for the deployed
    #: frontend until the §3.9 frontend-deltas PR lands.
    prices_as_of: str | None = None


__all__ = ["MarkSource", "Position", "PositionCluster", "PositionsResponse"]
