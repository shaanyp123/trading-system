"""services/api/schemas/cycle.py — response models for ``GET /api/system/cycle``.

Crypto-pivot §3.7: the daily-cycle projection (replaces the retired
bar-sync status): last decision outcome (per-asset trend score → target
→ action → est. costs), last risk-loop heartbeat, next Friday CDE close.

All numeric fields are Decimal-as-string per [A05] (Pydantic v2 emits
``Decimal`` fields as JSON strings). The per-asset numbers originate in
the worker's ``strategy_decision_v1`` ``outcome`` JSONB — floats written
by the parity-locked engine (see decisions-log 2026-07-09 C0-B3 decision
2: float inside the engine, Decimal at every money boundary). This
module IS such a boundary: values are re-anchored via ``Decimal(str(x))``
before serialization.

The outcome parse is defensive throughout — a missing/odd-shaped key
degrades to ``None`` (rendered as "n/a" by consumers), never a 500. The
Discord digest builders (``services/webhook_pusher/cycle_digest.py``)
and the frontend consume this wire shape.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from services.api.repos.cycle import StrategyDecisionRow, WorkerStatusRow

#: Risk loop ticks every 30 s (``WorkerConfig.risk_tick_interval_s``); the
#: heartbeat is called fresh within 3 missed ticks. Beyond that the worker
#: container is presumed down/wedged and the digest says so.
HEARTBEAT_FRESH_THRESHOLD_S = 90


class CycleAssetView(BaseModel):
    """One asset's slice of the decision outcome (score → target → action → costs)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset: str
    trend_score: Decimal | None = Field(
        default=None,
        description="S1 trend score from the decision bar (None while warming up).",
    )
    prior_contracts: int | None = None
    engine_target: int | None = Field(
        default=None,
        description="Raw engine target before the Decimal sizing re-check.",
    )
    final_target: int | None = Field(
        default=None,
        description="Post-sizing, post-dead-band integer contract target.",
    )
    action: str | None = Field(
        default=None,
        description='"buy" | "sell" | "hold" as planned at decision time.',
    )
    est_cost_usd: Decimal | None = Field(
        default=None,
        description="Aggregate fees across the executed legs (None until dispatch).",
    )
    mark: Decimal | None = None
    stop_level: Decimal | None = Field(
        default=None,
        description="Client stop level armed after the asset's legs completed.",
    )


class CycleDecisionView(BaseModel):
    """The latest ``strategy_decisions`` row, projected."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_date: str  # ISO date (UTC decision day)
    decided_at_utc: datetime
    status: str  # dispatching | completed | failed | skipped_*
    env: str
    equity_usd: Decimal | None
    weekly_halved: bool
    m_combined: Decimal | None
    skip_reason: str | None = Field(
        default=None,
        description="Populated on skipped_*/failed rows (outcome.skip_reason).",
    )
    assets: list[CycleAssetView]


class CycleWorkerView(BaseModel):
    """``strategy_worker_status`` heartbeat projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    risk_loop_heartbeat_utc: datetime | None
    seconds_since_heartbeat: int | None
    heartbeat_fresh: bool
    risk_loop_tick_count: int
    marks_stale: bool
    last_decision_date: str | None  # ISO date


class SystemCycleResponse(BaseModel):
    """``GET /api/system/cycle`` envelope.

    ``decision`` / ``worker`` are null on a fresh DB (worker never ran) —
    the no-decision-yet contract consumers must render, not error on.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: CycleDecisionView | None
    worker: CycleWorkerView | None
    next_friday_close_utc: datetime
    server_now: datetime


def _to_decimal(raw: Any) -> Decimal | None:
    """Best-effort JSONB scalar → Decimal (None on absent/unparseable).

    Floats are re-anchored via ``str()`` so the wire value matches the
    JSONB literal rather than the float's full binary expansion.
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


def _to_int(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    return None


def _asset_entry_to_view(asset: str, entry: dict[str, Any]) -> CycleAssetView:
    """Map one ``outcome.assets.<ASSET>`` object to the wire view."""
    row = entry.get("row")
    trend = row.get("trend") if isinstance(row, dict) else None
    stop = entry.get("stop")
    stop_level = stop.get("client_stop_level") if isinstance(stop, dict) else None
    action = entry.get("action")
    return CycleAssetView(
        asset=asset,
        trend_score=_to_decimal(trend),
        prior_contracts=_to_int(entry.get("prior_contracts")),
        engine_target=_to_int(entry.get("engine_target")),
        final_target=_to_int(entry.get("final_target")),
        action=action if isinstance(action, str) else None,
        est_cost_usd=_to_decimal(entry.get("est_cost_usd")),
        mark=_to_decimal(entry.get("mark")),
        stop_level=_to_decimal(stop_level),
    )


def decision_row_to_view(row: StrategyDecisionRow) -> CycleDecisionView:
    """Project a ``StrategyDecisionRow`` (+ its outcome JSONB) to the wire shape.

    Defensive parse: skip rows carry ``skip_reason`` and no ``assets``
    object; corrupt outcome shapes degrade to an empty asset list.
    """
    assets_raw = row.outcome.get("assets")
    assets: list[CycleAssetView] = []
    if isinstance(assets_raw, dict):
        assets = [
            _asset_entry_to_view(asset, entry)
            for asset, entry in sorted(assets_raw.items())
            if isinstance(entry, dict)
        ]
    skip_reason = row.outcome.get("skip_reason")
    return CycleDecisionView(
        decision_date=row.decision_date.isoformat(),
        decided_at_utc=row.decided_at_utc,
        status=row.status,
        env=row.env,
        equity_usd=row.equity_usd,
        weekly_halved=row.weekly_halved,
        m_combined=row.m_combined,
        skip_reason=skip_reason if isinstance(skip_reason, str) else None,
        assets=assets,
    )


def worker_row_to_view(row: WorkerStatusRow, *, now: datetime) -> CycleWorkerView:
    """Project a ``WorkerStatusRow`` + freshness classification."""
    seconds_since: int | None = None
    fresh = False
    if row.risk_loop_heartbeat_utc is not None:
        seconds_since = max(int((now - row.risk_loop_heartbeat_utc).total_seconds()), 0)
        fresh = seconds_since <= HEARTBEAT_FRESH_THRESHOLD_S
    return CycleWorkerView(
        risk_loop_heartbeat_utc=row.risk_loop_heartbeat_utc,
        seconds_since_heartbeat=seconds_since,
        heartbeat_fresh=fresh,
        risk_loop_tick_count=row.risk_loop_tick_count,
        marks_stale=row.marks_stale,
        last_decision_date=(
            row.last_decision_date.isoformat() if row.last_decision_date is not None else None
        ),
    )


__all__ = [
    "HEARTBEAT_FRESH_THRESHOLD_S",
    "CycleAssetView",
    "CycleDecisionView",
    "CycleWorkerView",
    "SystemCycleResponse",
    "decision_row_to_view",
    "worker_row_to_view",
]
