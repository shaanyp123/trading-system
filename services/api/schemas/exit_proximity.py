"""services/api/schemas/exit_proximity.py — response models for /api/today/exit-proximity.

Crypto-pivot §3.9 (``Docs/crypto-pivot-delta-spec.md``): the exit-side
"Stops" projection. The pre-pivot CME exit-trigger table (stop /
trend-flip / reversal / decommission chips off the ``exit_proximity``
table, ``Docs/exit-proximity-design.md``) died with the LEAN cycle;
exit proximity is now **distance to the client 2xATR stop and the
native 3xATR venue backstop** per open position, composed from
``positions_current`` (+ ``contracts`` enrichment), the strategy
worker's ``engine_state`` stops, the latest native-stop ``orders`` row,
and the api-resident live mark store (``services/api/marks``).

The URL is preserved; the response SHAPE is wire-breaking by design —
the §3.9 PR swaps both sides atomically.

**Headroom convention** (server-computed; the frontend only formats):
``(mark - stop) / mark`` for a long, ``(stop - mark) / mark`` for a
short — a positive fraction is distance-to-stop remaining; None when
either the mark or the stop is missing (no honest number to show).
Decimal throughout per [A05]; quantized to 6 dp for a deterministic
wire string.

**Ordering**: closest-to-stop first (smallest non-null headroom across
the two stops), with stopped-today / unprotected rows (non-zero
contracts but no native stop resting at the venue) sorted to the very
top — the rows the operator must see first.

Every mapper below is a pure function so the unit tests pin the math
(``tests/unit/test_api_exit_proximity_route.py``) — no clock reads,
no I/O.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

DirectionLiteral = Literal["long", "short"]

#: Wire-determinism quantum for the headroom fractions (Decimal division
#: otherwise widens the scale unpredictably).
_HEADROOM_QUANTUM: Final[Decimal] = Decimal("0.000001")


class PositionStopProximityView(BaseModel):
    """One open position's stop-proximity row (delta spec §3.9)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    product_id: str = Field(description="CDE product id (positions_current.market).")
    asset: str | None = Field(
        default=None,
        description=(
            "Base-asset label (BTC/ETH) from contracts.market_root; None "
            "when the row has no contracts enrichment."
        ),
    )
    direction: DirectionLiteral
    contracts: int = Field(description="Signed net contracts held in this product.")
    entry_vwap: Decimal | None = Field(
        default=None,
        description="Quantity-weighted average entry price (positions_current.avg_cost).",
    )
    mark: Decimal | None = Field(
        default=None,
        description=(
            "Live WS mark from the api-resident mark store; None when the "
            "store is absent or the mark exceeds the 3-minute staleness "
            "policy (services/api/marks)."
        ),
    )
    marks_stale: bool = Field(
        description=(
            "The strategy worker's marks_stale flag; True (conservative) "
            "when the worker has never reported."
        ),
    )
    client_stop_price: Decimal | None = Field(
        default=None,
        description=(
            "The worker's client-side 2xATR soft stop "
            "(engine_state.client_stop_level); enforced by the 30 s risk loop."
        ),
    )
    client_stop_headroom_frac: Decimal | None = Field(
        default=None,
        description="Distance-to-client-stop fraction (module docstring convention).",
    )
    native_stop_price: Decimal | None = Field(
        default=None,
        description=(
            "Resting venue 3xATR stop-limit backstop price (latest orders "
            "row for the worker's native_stop_client_order_id)."
        ),
    )
    native_stop_headroom_frac: Decimal | None = Field(
        default=None,
        description="Distance-to-native-stop fraction (module docstring convention).",
    )
    native_stop_resting: bool = Field(
        description=(
            "True when the worker tracks a venue-resting native stop order "
            "id for this asset. False with non-zero contracts = UNPROTECTED."
        ),
    )
    stopped_today: bool = Field(
        description="True when the engine's stop fired on today's UTC bar-day.",
    )


class ExitProximityResponse(BaseModel):
    """``GET /api/today/exit-proximity`` envelope.

    Fresh-DB / no-open-positions contract: ``positions`` is empty —
    HTTP 200, never an error.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    positions: list[PositionStopProximityView]
    server_now: datetime


# ---------------------------------------------------------------------------
# Pure mappers (math pinned by tests)
# ---------------------------------------------------------------------------


def stop_headroom_frac(
    *,
    mark: Decimal | None,
    stop: Decimal | None,
    direction: DirectionLiteral,
) -> Decimal | None:
    """Distance-to-stop as a fraction of the mark (module docstring).

    ``(mark - stop)/mark`` for longs, ``(stop - mark)/mark`` for shorts;
    None when either input is missing or the mark is non-positive (no
    honest denominator).
    """
    if mark is None or stop is None or mark <= 0:
        return None
    raw = (mark - stop) / mark if direction == "long" else (stop - mark) / mark
    return raw.quantize(_HEADROOM_QUANTUM)


def build_position_stop_view(
    *,
    product_id: str,
    asset: str | None,
    contracts: int,
    entry_vwap: Decimal | None,
    mark: Decimal | None,
    marks_stale: bool,
    client_stop_price: Decimal | None,
    native_stop_price: Decimal | None,
    native_stop_resting: bool,
    stopped_today: bool,
) -> PositionStopProximityView:
    """Compose one row: direction from the sign of ``contracts`` + the two
    server-computed headroom fractions. ``contracts`` must be non-zero
    (flat rows are filtered by the caller)."""
    direction: DirectionLiteral = "long" if contracts > 0 else "short"
    return PositionStopProximityView(
        product_id=product_id,
        asset=asset,
        direction=direction,
        contracts=contracts,
        entry_vwap=entry_vwap,
        mark=mark,
        marks_stale=marks_stale,
        client_stop_price=client_stop_price,
        client_stop_headroom_frac=stop_headroom_frac(
            mark=mark, stop=client_stop_price, direction=direction
        ),
        native_stop_price=native_stop_price,
        native_stop_headroom_frac=stop_headroom_frac(
            mark=mark, stop=native_stop_price, direction=direction
        ),
        native_stop_resting=native_stop_resting,
        stopped_today=stopped_today,
    )


def _sort_key(view: PositionStopProximityView) -> tuple[int, Decimal, str]:
    """Closest-to-stop first; stopped/unprotected rows before everything.

    Primary: attention group (stopped today OR no native stop resting =
    0, else 1). Secondary: MIN non-null headroom ascending (rows with no
    computable headroom sort last within their group via +Infinity).
    Tertiary: product_id for a stable, deterministic order.
    """
    attention = 0 if (view.stopped_today or not view.native_stop_resting) else 1
    headrooms = [
        h for h in (view.client_stop_headroom_frac, view.native_stop_headroom_frac) if h is not None
    ]
    min_headroom = min(headrooms) if headrooms else Decimal("Infinity")
    return (attention, min_headroom, view.product_id)


def sort_stop_views(
    views: list[PositionStopProximityView],
) -> list[PositionStopProximityView]:
    """Return a NEW list ordered per :func:`_sort_key` (input unmutated)."""
    return sorted(views, key=_sort_key)


__all__ = [
    "DirectionLiteral",
    "ExitProximityResponse",
    "PositionStopProximityView",
    "build_position_stop_view",
    "sort_stop_views",
    "stop_headroom_frac",
]
