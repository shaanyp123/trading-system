"""services/api/schemas/lean.py — signal/exit proximity wire schemas.

Crypto-pivot C0 (2026-07-08): the ``/api/internal/lean/*`` ingress and its
request/response models (LeanEventRequest, LeanEventAccepted,
LeanParametersResponse, LeanPositionsResponse) are RETIRED — signals are
generated in-process by the crypto strategy worker (delta spec §3.3), not
POSTed by an external LEAN container.

What REMAINS here are the gate/exit proximity wire schemas
(GateProximityItem, MarketEvaluationItem, ExitTriggerItem,
PositionExitEvaluationItem + their Literal vocabularies). They are the
persistence shape for the ``signal_proximity`` / ``exit_proximity``
heartbeat surfaces (services/api/repos/{signal,exit}_proximity.py) that
the Today page's Watching / Exit-Watching sections read. The module name
is historical; a later cleanup PR may rename it once the §3.3 crypto
signal engine defines its successor payloads.

See Docs/exit-pipeline-design.md §Q2 / §6.1 for the proximity design.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Signal-proximity heartbeat payload (PR-B of signal-proximity-design.md)
#
# Mirrors the wire shape emitted by
# ``lean/v1_strategy.py::_serialize_market_proximity`` and the canonical
# enums in ``strategies/v1_trend_following/proximity.py``. The api
# persists each item as one row in ``signal_proximity`` via
# ``services/api/repos/signal_proximity.py``.
# ---------------------------------------------------------------------------

#: Per-gate categorical state. Matches ``proximity.GateState`` (line 50).
GateStateLiteral = Literal["pass", "close", "fail"]

#: The strategy-side ``GateStatus`` literal (proximity.py line 72) — names
#: the per-market discriminator between "evaluated normally", "warming up",
#: and "decommissioned". Pinned to the same set as the migration's CHECK
#: constraint so a future addition fails at both layers in lockstep.
GateStatusLiteral = Literal["ok", "warming_up", "decommissioned"]

#: The per-market "which gate is driving the worst state" label. Matches
#: the values ``proximity.compute_market_proximity`` can emit. ``'hurst'``
#: is RETAINED for backward-compatibility with historic ``signal_proximity``
#: rows emitted before the 2026-06-02 Hurst→Efficiency-Ratio gate swap;
#: new rows emit ``'efficiency'``.
ClosestGateLiteral = Literal["donchian", "trend", "hurst", "efficiency", "history"]


# ---------------------------------------------------------------------------
# Exit-proximity heartbeat payload (PR-A of exit-proximity-design.md)
#
# Mirrors the wire shape emitted by
# ``lean/v1_strategy.py::_serialize_position_exit_proximity`` and the canonical
# enums in ``strategies/v1_trend_following/exit_proximity.py``. PR-A logs the
# count; PR-B persists each item as one row in ``exit_proximity``.
# ---------------------------------------------------------------------------

#: Per-trigger exit state. Matches ``exit_proximity.ExitState``.
ExitStateLiteral = Literal["holding", "near", "triggered"]

#: Position-level exit discriminator. Matches ``exit_proximity.GateStatus``.
ExitGateStatusLiteral = Literal["active", "warming_up", "min_holding_blocked", "decommissioned"]

#: Which exit trigger drives the overall state. Matches the ``closest_exit``
#: values ``compute_position_exit_proximity`` can return.
ClosestExitLiteral = Literal["stop", "reversal", "trend_flip", "decommission"]


class GateProximityItem(BaseModel):
    """One gate's wire-shape record.

    LEAN serializes ``GateProximity`` as ``{state, headroom, detail}`` with
    Decimal-as-string per A05. ``headroom`` is None when warming up;
    ``detail`` is an optional free-text tag (e.g., the literal
    ``"warming_up"`` returned by ``proximity._warming_up_gate``).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: GateStateLiteral = Field(
        ...,
        description="Categorical gate state. One of 'pass', 'close', 'fail'.",
    )
    headroom: Decimal | None = Field(
        default=None,
        description=(
            "Numeric headroom value (Decimal-as-string on the wire per A05). "
            "None when the market is warming up."
        ),
    )
    detail: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "Optional free-text qualifier (e.g., 'warming_up'). Short by "
            "design — the api stores it implicitly via gate_status, not a "
            "dedicated column."
        ),
    )


class MarketEvaluationItem(BaseModel):
    """One market's full proximity record on a single heartbeat.

    Wire shape: every field aligns 1:1 with the corresponding column on
    ``signal_proximity`` (with the per-gate sub-objects flattened into
    state + headroom columns at persistence time). Decimal-as-string per
    A05; absent numeric snapshot fields (last_close, etc.) survive as
    None to support the warming-up branch.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    market: str = Field(..., min_length=1, max_length=32)
    long_donchian: GateProximityItem
    short_donchian: GateProximityItem
    long_trend: GateProximityItem
    short_trend: GateProximityItem
    efficiency: GateProximityItem
    last_close: Decimal | None = Field(default=None)
    # Raw Efficiency Ratio + active threshold at evaluation time. Carried in
    # addition to the ``efficiency`` gate's state+headroom so the live ER
    # distribution is mineable directly from ``signal_proximity`` (interim
    # calibration evidence for the 0.20 launch threshold — no backtester yet;
    # see signal-proximity-design.md addendum + the gate-swap PR). Optional so
    # warming-up rows (value None) and older emitters stay valid.
    efficiency_ratio_value: Decimal | None = Field(default=None)
    efficiency_ratio_threshold: Decimal | None = Field(default=None)
    overall_state: GateStateLiteral
    closest_gate: ClosestGateLiteral
    gate_status: GateStatusLiteral


class ExitTriggerItem(BaseModel):
    """One exit trigger's wire-shape record (trend_flip / stop / reversal /
    decommission).

    LEAN serializes ``ExitTriggerProximity`` as ``{state, headroom, detail}``
    with Decimal-as-string per A05. ``headroom`` is None for the binary
    triggers (reversal / decommission) and for warming-up / no-stop rows.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: ExitStateLiteral = Field(
        ...,
        description="Categorical exit state. One of 'holding', 'near', 'triggered'.",
    )
    headroom: Decimal | None = Field(
        default=None,
        description=(
            "Numeric headroom (Decimal-as-string per A05). None for the binary "
            "triggers or when not computable (warming up / no stop on record)."
        ),
    )
    detail: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "Optional free-text qualifier (e.g., 'warming_up', 'blocked by MIN_HOLDING (3d left)')."
        ),
    )


class PositionExitEvaluationItem(BaseModel):
    """One open position's full exit-proximity record on a single heartbeat.

    Wire shape mirrors ``exit_proximity.PositionExitProximity``. Decimal-as-
    string per A05; ``stop`` is a NULL-state record under Q1 = (B) (LEAN does
    not know the working stop — the api joins it at PR-B persist time).
    PR-A logs the count only; PR-B persists each item as one ``exit_proximity``
    row.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    market: str = Field(..., min_length=1, max_length=32)
    direction: Literal["long", "short"]
    held_days: int | None = Field(default=None)
    trend_flip: ExitTriggerItem
    stop: ExitTriggerItem
    reversal: ExitTriggerItem
    decommission: ExitTriggerItem
    last_close: Decimal | None = Field(default=None)
    stop_price: Decimal | None = Field(default=None)
    overall_state: ExitStateLiteral
    closest_exit: ClosestExitLiteral
    gate_status: ExitGateStatusLiteral


__all__ = [
    "ClosestExitLiteral",
    "ClosestGateLiteral",
    "ExitGateStatusLiteral",
    "ExitStateLiteral",
    "ExitTriggerItem",
    "GateProximityItem",
    "GateStateLiteral",
    "GateStatusLiteral",
    "MarketEvaluationItem",
    "PositionExitEvaluationItem",
]
