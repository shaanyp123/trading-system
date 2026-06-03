"""SizingTrace shapes — TypedDict mirrors of the `signals.sizing_trace` JSONB column.

Backend-spec §2.4.1 specifies the full Stage 0-5 trace shape. This module declares
the typed contract so:
- the strategy can populate `Stage0Trace.strategy_inputs` (its contribution)
- the risk engine (Week 2 deliverable, `services/risk/sizing.py`) fills the rest
- consumers (PR review surface in Phase 1 month 4, audit explorer queries) have
  a single source of truth for the JSON shape

All numeric fields are stored as string-encoded Decimal per anti-pattern A05
("no float for money") so the JSONB round-trips through JCS canonicalization
without precision loss.

The strategy module ONLY fills `stage_0_universe.strategy_inputs` (per market).
Stages 1-5 are filled by `services/risk/sizing.py`. Until that service is
authored, the signal service can persist a partial sizing_trace with later
stages set to `None`; the JSONB column accepts nulls.
"""

from __future__ import annotations

from typing import TypedDict


class UniverseExclusion(TypedDict):
    market: str
    reason: str
    single_contract_notional: str  # Decimal as string
    current_equity: str


class StrategyInputs(TypedDict):
    """Per-market strategy indicator snapshot embedded in the sizing trace.

    Same structural shape as `audit_events.StrategyInputsTrace` — duplicated here
    rather than imported across modules so each module is self-contained. If
    these ever diverge, bump the schema version with a migration.
    """

    donchian_high: str
    donchian_low: str
    ma_fast: str
    ma_slow: str
    efficiency_ratio: str  # was `hurst` pre-2026-06-02; old JSONB rows keep `hurst`
    atr: str
    stop_price: str
    lookback_days_donchian: int


class Stage0Trace(TypedDict):
    """Stage 0 — universe filter + per-market strategy inputs.

    The strategy module fills `strategy_inputs`; risk engine fills `active_markets`
    and `excluded` (the 50%-single-contract-notional filter).
    """

    active_markets: list[str]
    excluded: list[UniverseExclusion]
    strategy_inputs: dict[str, StrategyInputs]  # keyed by market


class Stage1Trace(TypedDict):
    """Stage 1 — inverse-vol weighting + portfolio vol target.

    Filled by `services/risk/sizing.py`. Values keyed by market where applicable.
    """

    sigma_per_market: dict[str, str]
    raw_weights: dict[str, str]
    unconstrained_weight: dict[str, str]
    portfolio_realized_vol: str
    effective_vol_target_daily: str
    m_combined: str
    unconstrained_notional: dict[str, str]


class Stage2Trace(TypedDict):
    target_cap_pct: str
    hard_floor_pct: str
    capped_notional: dict[str, str]
    single_contract_overrides_applied: list[str]


class Stage3Trace(TypedDict):
    iterations: int
    convergence_tolerance_met: bool
    psd_repair_applied: bool
    scaled_clusters: list[str]
    dropped_due_to_non_convergence: list[str]


class Stage4Trace(TypedDict):
    gross_pre_scale: str
    gross_post_scale: str
    net_pre_scale: str
    net_post_scale: str


class Stage5Trace(TypedDict):
    contract_count_pre_round: dict[str, str]
    rounded: dict[str, int]
    rounding_deviation_pct: dict[str, str]
    sub_minimum_drops: list[str]


class SizingTraceV1(TypedDict, total=False):
    """Full sizing_trace JSONB shape per backend-spec §2.4.1.

    `total=False` because the strategy persists only Stage 0 with `strategy_inputs`
    filled in; risk engine appends Stages 1-5 in a follow-up update. By the time
    the signal status moves from `pending` -> `approved`, all six stages must be
    populated; integrity is checked by an Alembic migration's CHECK constraint
    when `services/risk/sizing.py` lands (Week 2).
    """

    schema_version: int  # bump when shape changes; Alembic migration required
    stage_0_universe: Stage0Trace
    stage_1_inverse_vol: Stage1Trace
    stage_2_per_position_cap: Stage2Trace
    stage_3_cluster: Stage3Trace
    stage_4_gross_net: Stage4Trace
    stage_5_lot_rounding: Stage5Trace
