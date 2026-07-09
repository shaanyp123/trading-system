"""services/api/schemas/signal_proximity.py — response models for /api/signals/proximity.

Crypto-pivot §3.9 (``Docs/crypto-pivot-delta-spec.md``): the entry-side
"Watching" projection. The pre-pivot CME gate table (Donchian / Trend /
Efficiency-Ratio per-gate chips, ``Docs/signal-proximity-design.md``)
died with the LEAN cycle; proximity is now **distance to a hysteresis
direction flip** per asset, read straight from the strategy worker's
persisted runtime (``strategy_worker_status.engine_state`` — the
``AssetRuntime.to_json`` shape, Decimals as strings) plus the latest
``strategy_decisions`` outcome JSONB (per-asset trend score + the mark
captured at the 00:05 UTC decision).

The URL is preserved; the response SHAPE is wire-breaking by design —
the §3.9 PR swaps both sides atomically.

**Mark honesty note.** ``engine_state`` carries no product_id, so there
is no live-mark reference to resolve here; the smallest honest option is
the mark recorded in the decision outcome (``outcome.assets.<A>.mark``,
observed at the 00:05 UTC decision). The Watching table's mark column is
informational — the live mark lives on ``/api/positions/current`` and
``/api/today/exit-proximity``.

All numeric fields are Decimal-as-string per [A05]; floats from the
parity-locked engine's JSONB re-anchor via ``Decimal(str(x))`` at this
boundary (same discipline as ``services/api/schemas/cycle.py``).

Every mapper below is a pure function so the unit tests pin the math
(``tests/unit/test_api_signal_proximity_route.py``) — no clock reads,
no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from services.api.repos.cycle import StrategyDecisionRow, WorkerStatusRow

# Import-only dependency on the forbidden path (allowed per the §3.9
# build plan): the Amendment B canonical seed is the fallback when no
# parameter_sets row is active yet (fresh DB).
from services.risk.crypto_parameters import AMENDMENT_B_CANONICAL_PARAMETERS

#: Amendment B canonical HYSTERESIS_DAYS — the fallback when the active
#: parameter set is absent or carries an unusable value.
FALLBACK_HYSTERESIS_DAYS: Final[int] = int(AMENDMENT_B_CANONICAL_PARAMETERS["HYSTERESIS_DAYS"])


class AssetProximityView(BaseModel):
    """One asset's hysteresis-flip proximity row (delta spec §3.9)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset: str
    trend_score: Decimal | None = Field(
        default=None,
        description=(
            "S1 trend score from the latest decision outcome "
            "(outcome.assets.<A>.row.trend); None while warming up or "
            "before the first decision."
        ),
    )
    current_dir: int = Field(
        description="Applied direction the engine is holding: -1 / 0 / +1.",
    )
    pending_dir: int | None = Field(
        default=None,
        description=(
            "Direction awaiting hysteresis confirmation (-1 / +1); None when no flip is pending."
        ),
    )
    pending_count: int = Field(
        description="Consecutive daily bars the pending direction has persisted.",
    )
    days_to_flip: int | None = Field(
        default=None,
        description=(
            "hysteresis_days - pending_count when a flip is pending "
            "(pending_dir set and different from current_dir); None otherwise."
        ),
    )
    hysteresis_days: int = Field(
        description="HYSTERESIS_DAYS from the active parameter set (Amendment B fallback).",
    )
    lockout_active: bool = Field(
        description="True while the post-stop re-entry lockout covers today's UTC date.",
    )
    lockout_days_left: int | None = Field(
        default=None,
        description=(
            "Calendar days (UTC ordinals) until the lockout clears; None when no lockout is active."
        ),
    )
    vol_blocked: bool
    mark: Decimal | None = Field(
        default=None,
        description=(
            "Mark recorded at the last 00:05 UTC decision (informational; "
            "NOT a live tick — see module docstring)."
        ),
    )
    last_decision_date: date | None = Field(
        default=None,
        description="Worker's last completed decision date (UTC).",
    )
    as_of_utc: datetime = Field(
        description="When the worker last persisted this engine state.",
    )


class SignalProximityResponse(BaseModel):
    """``GET /api/signals/proximity`` envelope.

    Fresh-DB / worker-never-ran contract: ``assets`` is empty — HTTP 200,
    never an error.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    assets: list[AssetProximityView]
    server_now: datetime


# ---------------------------------------------------------------------------
# Pure mappers (math pinned by tests)
# ---------------------------------------------------------------------------


def _to_decimal(raw: Any) -> Decimal | None:
    """Best-effort JSONB scalar → Decimal (None on absent/unparseable).

    Floats re-anchor via ``str()`` so the wire value matches the JSONB
    literal rather than the float's full binary expansion.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, (int, float, str)):
        try:
            return Decimal(str(raw))
        except (InvalidOperation, ValueError):
            return None
    return None


def _to_int(raw: Any, default: int = 0) -> int:
    if isinstance(raw, bool):
        return default
    if isinstance(raw, int):
        return raw
    return default


def hysteresis_days_from_parameters(parameters: Mapping[str, object] | None) -> int:
    """Resolve HYSTERESIS_DAYS from a ``parameter_sets`` JSONB dict.

    The seeded values are Decimal-as-string per [A05] (``"2"``). Missing
    row, missing key, unparseable or non-positive values all degrade to
    the Amendment B canonical constant — the display of a proximity
    countdown must never 500 over a config read.
    """
    if parameters is None:
        return FALLBACK_HYSTERESIS_DAYS
    raw = parameters.get("HYSTERESIS_DAYS")
    if raw is None or isinstance(raw, bool):
        return FALLBACK_HYSTERESIS_DAYS
    try:
        value = int(str(raw).strip())
    except ValueError:
        return FALLBACK_HYSTERESIS_DAYS
    return value if value > 0 else FALLBACK_HYSTERESIS_DAYS


def days_to_flip(
    *,
    current_dir: int,
    pending_dir: int | None,
    pending_count: int,
    hysteresis_days: int,
) -> int | None:
    """``hysteresis_days - pending_count`` while a flip is genuinely pending.

    A flip is pending when ``pending_dir`` is set AND differs from the
    applied direction (delta spec §3.9 / strategy Amendment B hysteresis-
    hold semantics). Otherwise None — the asset is steady.
    """
    if pending_dir is None or pending_dir == current_dir:
        return None
    return hysteresis_days - pending_count


def _trend_score_from_outcome(outcome: Mapping[str, Any], asset: str) -> Decimal | None:
    """``outcome.assets.<asset>.row.trend`` — defensively parsed."""
    assets = outcome.get("assets")
    if not isinstance(assets, Mapping):
        return None
    entry = assets.get(asset)
    if not isinstance(entry, Mapping):
        return None
    row = entry.get("row")
    if not isinstance(row, Mapping):
        return None
    return _to_decimal(row.get("trend"))


def _mark_from_outcome(outcome: Mapping[str, Any], asset: str) -> Decimal | None:
    """``outcome.assets.<asset>.mark`` — the 00:05 decision-time mark."""
    assets = outcome.get("assets")
    if not isinstance(assets, Mapping):
        return None
    entry = assets.get(asset)
    if not isinstance(entry, Mapping):
        return None
    return _to_decimal(entry.get("mark"))


def asset_runtime_to_view(
    asset: str,
    runtime: Mapping[str, Any],
    *,
    trend_score: Decimal | None,
    mark: Decimal | None,
    hysteresis_days: int,
    last_decision_date: date | None,
    as_of_utc: datetime,
    today_ord: int,
) -> AssetProximityView:
    """Map one ``engine_state.<asset>`` object (``AssetRuntime.to_json``
    shape) to the wire view.

    Lockout semantics mirror the engine (``AssetRuntime.to_engine_state``):
    the engine compares ``bar_index < lockout_until`` with ``bar_index =
    date.toordinal()`` of the decided bar, so ``lockout_active`` is
    ``today_ord < lockout_until_ord``.
    """
    current_dir = _to_int(runtime.get("applied_dir"))
    pending_dir_raw = _to_int(runtime.get("pending_dir"))
    pending_dir: int | None = pending_dir_raw if pending_dir_raw != 0 else None
    pending_count = _to_int(runtime.get("pending_count"))
    lockout_until_ord = _to_int(runtime.get("lockout_until_ord"), default=-1)
    lockout_active = today_ord < lockout_until_ord
    return AssetProximityView(
        asset=asset,
        trend_score=trend_score,
        current_dir=current_dir,
        pending_dir=pending_dir,
        pending_count=pending_count,
        days_to_flip=days_to_flip(
            current_dir=current_dir,
            pending_dir=pending_dir,
            pending_count=pending_count,
            hysteresis_days=hysteresis_days,
        ),
        hysteresis_days=hysteresis_days,
        lockout_active=lockout_active,
        lockout_days_left=(lockout_until_ord - today_ord) if lockout_active else None,
        vol_blocked=bool(runtime.get("vol_blocked")),
        mark=mark,
        last_decision_date=last_decision_date,
        as_of_utc=as_of_utc,
    )


def build_signal_proximity_response(
    *,
    worker: WorkerStatusRow | None,
    decision: StrategyDecisionRow | None,
    hysteresis_days: int,
    now: datetime,
) -> SignalProximityResponse:
    """Assemble the full response from repo rows. Pure — tests pin it.

    Fresh-DB / worker-never-ran contract: ``worker`` is None (or its
    ``engine_state`` is empty) → empty ``assets`` list, HTTP 200. Assets
    render in sorted order (BTC, ETH) for a deterministic wire shape.
    """
    if now.tzinfo is None:
        raise ValueError("now must be tz-aware (dev-guide §3 / A06); got naive")
    if worker is None:
        return SignalProximityResponse(assets=[], server_now=now)

    outcome: Mapping[str, Any] = decision.outcome if decision is not None else {}
    today_ord = now.date().toordinal()
    assets = [
        asset_runtime_to_view(
            asset,
            runtime,
            trend_score=_trend_score_from_outcome(outcome, asset),
            mark=_mark_from_outcome(outcome, asset),
            hysteresis_days=hysteresis_days,
            last_decision_date=worker.last_decision_date,
            as_of_utc=worker.updated_at_utc,
            today_ord=today_ord,
        )
        for asset, runtime in sorted(worker.engine_state.items())
        if isinstance(runtime, Mapping)
    ]
    return SignalProximityResponse(assets=assets, server_now=now)


__all__ = [
    "FALLBACK_HYSTERESIS_DAYS",
    "AssetProximityView",
    "SignalProximityResponse",
    "asset_runtime_to_view",
    "build_signal_proximity_response",
    "days_to_flip",
    "hysteresis_days_from_parameters",
]
