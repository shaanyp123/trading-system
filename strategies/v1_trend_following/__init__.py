"""v1_trend_following — Donchian / MA / Hurst trend-following strategy (Phase 1).

Public surface used by `services/signal/` and `lean/v1_qc_algorithm.py`. Internals
(indicators, audit-event payload shapes, sizing-trace skeleton) are exposed for
unit tests and for the risk engine to fill in Stage 0-5 sizing.

Strategy logic is locked in `Docs/backend-spec.md §2.3 ("Strategy v1")`; this
package implements that contract. Any change to the strategy logic requires a
PR with the `risk-review-approved` label per CLAUDE.md.
"""

from strategies.v1_trend_following.audit_events import (
    SignalEmittedPayload,
    SignalRejectedPayload,
    StrategyAuditEvent,
    UniverseExclusionPayload,
    UniverseInclusionPayload,
)
from strategies.v1_trend_following.parameters import (
    V1_CANDIDATE_UNIVERSE,
    V1_DEFAULTS,
    V1_SIDELINED_MARKETS,
    V1Parameters,
    default_v1_parameters,
)
from strategies.v1_trend_following.signals import (
    Bar,
    BarSeries,
    CandidateSignal,
    Direction,
    Position,
    RejectionReason,
    SignalGenerationResult,
)
from strategies.v1_trend_following.sizing_trace import SizingTraceV1, Stage0Trace
from strategies.v1_trend_following.strategy import STRATEGY_NAME, V1TrendFollowing

__all__ = [
    "STRATEGY_NAME",
    "V1_CANDIDATE_UNIVERSE",
    "V1_DEFAULTS",
    "V1_SIDELINED_MARKETS",
    "Bar",
    "BarSeries",
    "CandidateSignal",
    "Direction",
    "Position",
    "RejectionReason",
    "SignalEmittedPayload",
    "SignalGenerationResult",
    "SignalRejectedPayload",
    "SizingTraceV1",
    "Stage0Trace",
    "StrategyAuditEvent",
    "UniverseExclusionPayload",
    "UniverseInclusionPayload",
    "V1Parameters",
    "V1TrendFollowing",
    "default_v1_parameters",
]
