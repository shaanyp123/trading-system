"""Position sizing pipeline — Stages 0 through 5 (backend-spec §2.4.1).

This module is the canonical implementation of the locked five-stage position-
sizing algorithm. Inputs are CandidateSignals (from `strategies.v1_trend_following`),
current equity, a covariance matrix, momentum z-scores, and contract metadata.
Output is a `SizingResult` with target contract counts per market and the full
intermediate trace persisted to `signals.sizing_trace` (JSONB).

Pure-function design (no DB I/O):
- The pipeline is a synchronous, deterministic function with no audit writes
  or session arguments. It returns `pending_audit_events` as data.
- The caller (signal service / risk dispatcher) flushes those to
  `services/audit/writer.append_audit_event()` in a separate transaction. This
  keeps the test surface mockable per anti-pattern A22 ("audit events are not
  emitted from within unit tests; mock writer in unit tests, real DB only in
  integration tests").
- Bonus: the pipeline can run in `verify_universe.py` and other read-only
  contexts without instantiating an `AsyncSession`.

Decimals + numpy split (anti-pattern A05):
- Money / price values are `Decimal` at module boundaries.
- Internal matrix math uses `numpy.float64` because eigendecomposition in pure
  Python is impractical. Conversion happens at the stage boundaries; the trace
  always serializes back to `Decimal`-as-string for JCS canonicalization.

Stage summary (full algorithm in backend-spec §2.4.1):
- Stage 0 — universe filter: exclude any market where 1-contract notional
  exceeds 50% of equity. Per-market `single_contract_overrides` map carries
  exceptions (e.g. /MES at $20k+).
- Stage 1 — inverse-vol weighting: raw weights = 1/sigma, normalized; portfolio
  realized vol via `sqrt(wTSigmaw)`; leverage = `vol_target_annual_effective /
  portfolio_realized_vol_annual`; unconstrained_notional[i] = leverage x
  weight[i] x equity.
- Stage 2 — per-position cap: 25% target weight by default; markets in the
  override list get 50% hard floor (matches Stage 0 override semantics).
- Stage 3 — cluster shrink: iterative scaling of clusters that breach
  cluster_cap x equity; PSD repair on Sigma (Higham nearest-PSD via
  `np.linalg.eigh` clamp-and-reconstruct); on non-convergence (>10 iter)
  drop the lowest-momentum signal in the binding cluster and restart.
- Stage 4 — gross/net cap: gross 3.0x equity, net 1.5x equity; uniform shrink.
- Stage 5 — lot rounding: banker's rounding (`Decimal.ROUND_HALF_EVEN`); markets
  whose pre-round notional rounds to zero are tagged in `sub_minimum_drops`.

Unit tests in `tests/unit/test_sizing.py` cover the §10.1 inventory:
- Stage 0 universe filter at $15k / $25k / $50k / $100k
- Stage 2 50%-override for /MES at $20k
- Stage 3 cluster shrink convergence (<=10 iter)
- Stage 3 non-convergence drops lowest-momentum signal and restarts
- Stage 5 lot rounding sub_minimum_size detection
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Final, Literal

import numpy as np
import numpy.typing as npt
import structlog

from strategies.v1_trend_following.signals import CandidateSignal, Direction
from strategies.v1_trend_following.sizing_trace import (
    SizingTraceV1,
    Stage1Trace,
    Stage2Trace,
    Stage3Trace,
    Stage4Trace,
    Stage5Trace,
    UniverseExclusion,
)

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default per-position weight cap. Stage 2 caps unconstrained_notional at
#: ``target_cap_pct x equity`` for markets NOT in the override list.
DEFAULT_TARGET_CAP_PCT: Final[Decimal] = Decimal("0.25")

#: Per-position hard floor (override) — markets in ``single_contract_overrides``
#: get this as their cap instead of ``target_cap_pct``. Per backend-spec §2.4.1
#: ("25% target weight / 50% override").
DEFAULT_HARD_FLOOR_PCT: Final[Decimal] = Decimal("0.50")

#: Stage 3 cluster cap — no single cluster may exceed this fraction of equity.
#: Spec leaves the value implicit (only that cluster cap binding is a unit-test
#: target in §10.1). 1.0x equity is the conservative default — total gross is
#: bounded above by ``gross_cap``; clusters share within that envelope.
DEFAULT_CLUSTER_CAP_PCT: Final[Decimal] = Decimal("1.00")

#: Default Stage 4 gross / net leverage caps (backend-spec §2.4.1).
DEFAULT_GROSS_CAP: Final[Decimal] = Decimal("3.00")
DEFAULT_NET_CAP: Final[Decimal] = Decimal("1.50")

#: Stage 0 single-contract notional bar. Spec text: "exclude any market where
#: 1-contract notional > 50% x equity".
STAGE_0_NOTIONAL_PCT: Final[Decimal] = Decimal("0.50")

#: Stage 3 cluster-shrink loop bounds (backend-spec §2.4.1).
CLUSTER_SHRINK_MAX_ITER: Final[int] = 10
CLUSTER_SHRINK_TOLERANCE: Final[Decimal] = Decimal("0.001")  # 0.1%

#: Annualization factor for vol scaling (252 CME trading days/yr).
TRADING_DAYS_PER_YEAR: Final[int] = 252

#: Schema version for the persisted ``signals.sizing_trace`` JSONB (matches
#: ``SizingTraceV1`` shape). Bumped on shape change with an Alembic migration.
SIZING_TRACE_SCHEMA_VERSION: Final[int] = 1


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ContractMetadata:
    """Per-market metadata required by the sizing pipeline.

    Sourced from the ``contracts`` table (backend-spec §3.29) at runtime;
    constructed inline in unit tests.

    - ``single_contract_notional``: USD value of one contract at the current
      decision_price (price x multiplier for futures; price x 1 for ETFs).
      Stage 0 compares this to ``50% x equity``.
    - ``cluster``: macro grouping for Stage 3 (e.g. "equity_index", "rates",
      "commodity", "crypto"). Markets in the same cluster share a cap.
    - ``min_lot``: minimum tradable contract count (1 for both futures and
      cash equity ETFs in V1; reserved for future markets where lot ≠ 1).
    """

    market: str
    single_contract_notional: Decimal
    cluster: str
    min_lot: int = 1


@dataclass(frozen=True, slots=True)
class PendingAuditEvent:
    """A queued audit event the caller must flush via append_audit_event().

    Three types are emitted by the sizing pipeline (per backend-spec §2.4.1
    "Dependencies | Audit"): ``psd_repair_applied``, ``universe_exclusion``,
    ``universe_inclusion``. Payload shapes mirror the locked taxonomy in
    backend-spec §3.30 and the strategy-side declarations in
    ``strategies.v1_trend_following.audit_events``.
    """

    event_type: Literal[
        "psd_repair_applied",
        "universe_exclusion",
        "universe_inclusion",
    ]
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SizingResult:
    """Output of ``run_sizing_pipeline``.

    - ``target_contracts``: signed integer per market; sign matches the
      direction of the originating ``CandidateSignal`` (long → +, short → -).
      Markets dropped by Stage 0 / sub-minimum at Stage 5 / non-convergence
      at Stage 3 are absent from this dict (caller treats absence as 0).
    - ``sizing_trace``: full Stage 0-5 trace per spec §2.4.1.
    - ``pending_audit_events``: tuple of audit events the caller writes via
      ``append_audit_event`` in a separate transaction.
    """

    target_contracts: dict[str, int]
    sizing_trace: SizingTraceV1
    pending_audit_events: tuple[PendingAuditEvent, ...]


class SizingError(Exception):
    """Raised when the sizing pipeline cannot produce a valid result.

    Distinct from individual-stage drops (which silently exclude a market) —
    this is reserved for invariant violations (mismatched sigma index,
    non-positive equity, etc.) that indicate a programming bug, not strategy
    state. Caller should NOT catch this; let it propagate up to the kill-switch.
    """


# ---------------------------------------------------------------------------
# Numerical helpers
# ---------------------------------------------------------------------------


def _nearest_psd(
    sigma: npt.NDArray[np.float64],
) -> tuple[npt.NDArray[np.float64], bool, float, float]:
    """Higham nearest-PSD repair via eigenvalue clamping (backend-spec §2.4.1).

    Returns ``(repaired_matrix, was_repaired, min_eigval_pre, min_eigval_post)``.
    A matrix already PSD is returned unchanged; the caller emits an audit event
    only when ``was_repaired`` is True.

    Algorithm:
        1. Symmetric eigendecomposition (``np.linalg.eigh``).
        2. If smallest eigenvalue >= 0: matrix is PSD; return as-is.
        3. Otherwise clamp negative eigenvalues to 0 and reconstruct
           ``Sigma' = V Lambda' VT``. Re-symmetrize via ``(Sigma' + Sigma'T)/2`` to scrub
           floating-point antisymmetry from the matmul.
    """
    eigvals, eigvecs = np.linalg.eigh(sigma)
    min_eigval_pre = float(eigvals.min())
    if min_eigval_pre >= 0:
        return sigma, False, min_eigval_pre, min_eigval_pre
    eigvals_clamped = np.clip(eigvals, 0.0, None)
    repaired = eigvecs @ np.diag(eigvals_clamped) @ eigvecs.T
    # Re-symmetrize to remove floating-point antisymmetry from the matmul.
    repaired = (repaired + repaired.T) / 2.0
    min_eigval_post = float(np.linalg.eigvalsh(repaired).min())
    return repaired, True, min_eigval_pre, min_eigval_post


def _portfolio_vol_annual(
    weights: npt.NDArray[np.float64],
    sigma_annual: npt.NDArray[np.float64],
) -> float:
    """Annualized portfolio realized vol from weights + covariance.

    ``sigma_annual`` MUST be PSD (caller passes the output of ``_nearest_psd``).
    Returns ``sqrt(wT Sigma w)``; never negative even with floating-point noise.
    """
    var = float(weights @ sigma_annual @ weights)
    return math.sqrt(max(var, 0.0))


def _to_decimal_str(value: float, *, places: int = 6) -> str:
    """Quantize a numpy float to a Decimal string for JCS-stable trace output.

    All trace numbers serialize as strings (anti-pattern A05). ``places=6`` is
    enough precision for vol / weight values; lot counts are ints.
    """
    return str(Decimal(str(value)).quantize(Decimal(10) ** -places))


# ---------------------------------------------------------------------------
# Stage 0 — universe filter
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Stage0Output:
    active_markets: tuple[str, ...]
    excluded: tuple[UniverseExclusion, ...]
    overrides_applied: tuple[str, ...]
    audit_events: tuple[PendingAuditEvent, ...]


def _stage_0_universe_filter(
    *,
    candidate_markets: Sequence[str],
    equity: Decimal,
    contracts_metadata: Mapping[str, ContractMetadata],
    single_contract_overrides: Mapping[str, Decimal],
    evaluated_at_utc: str,
) -> _Stage0Output:
    """Filter candidate markets by the 1-contract-notional <= 50% x equity rule.

    Markets in ``single_contract_overrides`` whose value <= ``equity`` bypass
    the rule (the override threshold is the minimum equity at which the
    override activates; e.g. /MES → $20,000). The override list is also
    surfaced in the Stage 2 trace so the per-position cap can switch from
    25% → 50% for the same markets.

    Emits one audit event per excluded market (``universe_exclusion``).
    Re-inclusion (``universe_inclusion``) is the symmetric event but requires
    knowledge of the prior session's universe, which this pure function
    doesn't have — the caller computes the diff and emits inclusions.
    """
    if equity <= 0:
        raise SizingError(f"equity must be positive; got {equity}")

    active: list[str] = []
    excluded: list[UniverseExclusion] = []
    overrides_applied: list[str] = []
    audit_events: list[PendingAuditEvent] = []

    threshold_notional = STAGE_0_NOTIONAL_PCT * equity

    for market in candidate_markets:
        meta = contracts_metadata.get(market)
        if meta is None:
            # Missing metadata is a programming error, not a strategy state.
            raise SizingError(f"missing ContractMetadata for market={market}")

        override_threshold = single_contract_overrides.get(market)
        is_override_active = override_threshold is not None and equity >= override_threshold

        if meta.single_contract_notional <= threshold_notional or is_override_active:
            active.append(market)
            if is_override_active:
                overrides_applied.append(market)
        else:
            exclusion: UniverseExclusion = {
                "market": market,
                "reason": "single_contract_notional_exceeds_50pct_equity",
                "single_contract_notional": str(meta.single_contract_notional),
                "current_equity": str(equity),
            }
            excluded.append(exclusion)
            audit_events.append(
                PendingAuditEvent(
                    event_type="universe_exclusion",
                    payload={
                        "market": market,
                        "exclusion_reason": "single_contract_notional_exceeds_50pct_equity",
                        "single_contract_notional": str(meta.single_contract_notional),
                        "current_equity": str(equity),
                        "evaluated_at_utc": evaluated_at_utc,
                    },
                )
            )

    return _Stage0Output(
        active_markets=tuple(active),
        excluded=tuple(excluded),
        overrides_applied=tuple(overrides_applied),
        audit_events=tuple(audit_events),
    )


# ---------------------------------------------------------------------------
# Stage 1 — inverse-vol weighting
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Stage1Output:
    unconstrained_notional: dict[str, Decimal]
    trace: Stage1Trace


def _stage_1_inverse_vol(
    *,
    active_markets: Sequence[str],
    equity: Decimal,
    sigma_annual_full: npt.NDArray[np.float64],
    sigma_index: Sequence[str],
    vol_target_pct_annual: Decimal,
    m_combined: Decimal,
) -> _Stage1Output:
    """Compute inverse-vol weighted notionals scaled to portfolio vol target.

    Steps:
      1. Slice the full Sigma to the active subset.
      2. raw_weight[i] = 1/sigma_i_annual; normalize to sum to 1.0 → weights w.
      3. portfolio_realized_vol_annual = sqrt(wT Sigma_active w).
      4. effective_vol_target_annual = vol_target_pct_annual x m_combined.
      5. leverage = effective_vol_target / portfolio_realized_vol_annual.
      6. unconstrained_notional[i] = leverage x w[i] x equity.

    The trace persists annualized portfolio vol AND ``effective_vol_target_daily``
    (annualized / sqrt(252)) per the spec example shape.
    """
    if not active_markets:
        empty_trace: Stage1Trace = {
            "sigma_per_market": {},
            "raw_weights": {},
            "unconstrained_weight": {},
            "portfolio_realized_vol": "0",
            "effective_vol_target_daily": "0",
            "m_combined": str(m_combined),
            "unconstrained_notional": {},
        }
        return _Stage1Output(unconstrained_notional={}, trace=empty_trace)

    idx_lookup = {m: i for i, m in enumerate(sigma_index)}
    try:
        active_idx = [idx_lookup[m] for m in active_markets]
    except KeyError as e:
        raise SizingError(
            f"market {e.args[0]} present in active set but missing from sigma_index"
        ) from e

    sigma_active = sigma_annual_full[np.ix_(active_idx, active_idx)]
    sigma_diag = np.diag(sigma_active)
    if (sigma_diag <= 0).any():
        bad = [active_markets[i] for i in range(len(active_markets)) if sigma_diag[i] <= 0]
        raise SizingError(f"non-positive variance for markets={bad}")

    sigma_per_market = np.sqrt(sigma_diag)  # annualized vol per market
    raw_weights = 1.0 / sigma_per_market
    weights = raw_weights / raw_weights.sum()  # normalized to sum to 1.0

    portfolio_vol_annual = _portfolio_vol_annual(weights, sigma_active)
    effective_vol_target_annual = float(vol_target_pct_annual * m_combined)
    effective_vol_target_daily = effective_vol_target_annual / math.sqrt(TRADING_DAYS_PER_YEAR)

    if portfolio_vol_annual <= 0:
        raise SizingError("portfolio_realized_vol_annual is zero — Sigma degenerate?")
    leverage = effective_vol_target_annual / portfolio_vol_annual

    equity_f = float(equity)
    unconstrained: dict[str, Decimal] = {}
    for i, market in enumerate(active_markets):
        notional_f = leverage * float(weights[i]) * equity_f
        unconstrained[market] = Decimal(str(notional_f)).quantize(Decimal("0.01"))

    trace: Stage1Trace = {
        "sigma_per_market": {
            market: _to_decimal_str(float(sigma_per_market[i]))
            for i, market in enumerate(active_markets)
        },
        "raw_weights": {
            market: _to_decimal_str(float(raw_weights[i]))
            for i, market in enumerate(active_markets)
        },
        "unconstrained_weight": {
            market: _to_decimal_str(float(weights[i])) for i, market in enumerate(active_markets)
        },
        "portfolio_realized_vol": _to_decimal_str(portfolio_vol_annual),
        "effective_vol_target_daily": _to_decimal_str(effective_vol_target_daily),
        "m_combined": str(m_combined),
        "unconstrained_notional": {
            market: str(notional) for market, notional in unconstrained.items()
        },
    }
    return _Stage1Output(unconstrained_notional=unconstrained, trace=trace)


# ---------------------------------------------------------------------------
# Stage 2 — per-position cap
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Stage2Output:
    capped_notional: dict[str, Decimal]
    trace: Stage2Trace


def _stage_2_per_position_cap(
    *,
    unconstrained_notional: Mapping[str, Decimal],
    equity: Decimal,
    target_cap_pct: Decimal,
    hard_floor_pct: Decimal,
    overrides_applied: Sequence[str],
) -> _Stage2Output:
    """Apply per-position cap to each market.

    - Markets in ``overrides_applied`` get ``hard_floor_pct x equity`` (50%).
    - All other markets get ``target_cap_pct x equity`` (25%).
    - ``capped_notional[i] = min(unconstrained[i], cap[i])``.
    """
    target_cap = target_cap_pct * equity
    floor_cap = hard_floor_pct * equity
    overrides_set = frozenset(overrides_applied)

    capped: dict[str, Decimal] = {}
    for market, notional in unconstrained_notional.items():
        cap = floor_cap if market in overrides_set else target_cap
        # Compare on absolute magnitude — direction is applied at the very end
        # (Stage 5). Stage 1 already returns positive notionals.
        capped[market] = min(notional, cap)

    trace: Stage2Trace = {
        "target_cap_pct": str(target_cap_pct),
        "hard_floor_pct": str(hard_floor_pct),
        "capped_notional": {market: str(notional) for market, notional in capped.items()},
        "single_contract_overrides_applied": list(overrides_applied),
    }
    return _Stage2Output(capped_notional=capped, trace=trace)


# ---------------------------------------------------------------------------
# Stage 3 — cluster shrink (with PSD repair + non-convergence drop-restart)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Stage3Output:
    scaled_notional: dict[str, Decimal]
    trace: Stage3Trace
    audit_events: tuple[PendingAuditEvent, ...]
    dropped_markets: tuple[str, ...]


def _stage_3_cluster_shrink(
    *,
    capped_notional: Mapping[str, Decimal],
    contracts_metadata: Mapping[str, ContractMetadata],
    psd_repair_was_applied: bool,
    cluster_cap_pct: Decimal,
    equity: Decimal,
    momentum_z_scores: Mapping[str, Decimal],
    max_iter: int = CLUSTER_SHRINK_MAX_ITER,
) -> _Stage3Output:
    """Iteratively shrink clusters that exceed ``cluster_cap x equity``.

    Up to ``max_iter`` (default ``CLUSTER_SHRINK_MAX_ITER`` = 10) passes with
    ``CLUSTER_SHRINK_TOLERANCE`` (0.1%) convergence. On non-convergence: drop
    the lowest-momentum signal in the binding cluster (rolling 60-day return
    z-score, ascending) and restart with the reduced market set.

    PSD repair is performed by the pipeline orchestrator BEFORE Stage 1 (the
    covariance matrix is shared between Stage 1 vol scaling and Stage 3
    cluster decisions; repairing once at the top means both stages see the
    same matrix). Stage 3 only records ``psd_repair_was_applied`` in its trace
    — it doesn't repair itself. The audit event is emitted by the orchestrator.

    ``max_iter`` is a parameter (vs. an unconditional constant) so that unit
    tests can force the non-convergence drop branch by passing 0 — the inner
    convergence loop runs zero iterations, the breach persists, and the drop-
    lowest-momentum-and-restart path fires.

    The trace records: total iterations across restarts, whether convergence
    was reached, whether PSD repair was applied earlier in the pipeline, the
    list of clusters that were scaled in the FINAL pass, and any markets
    dropped (so the caller can emit ``signal_rejected`` events for them).
    """
    cluster_cap = cluster_cap_pct * equity

    working: dict[str, Decimal] = dict(capped_notional)
    dropped: list[str] = []
    total_iterations = 0
    convergence_met = False
    final_scaled_clusters: list[str] = []

    while True:
        if not working:
            convergence_met = True
            break

        # One convergence loop on the current working set.
        loop_iter = 0
        scaled_clusters_this_loop: list[str] = []
        loop_converged = False
        while loop_iter < max_iter:
            loop_iter += 1
            total_iterations += 1
            cluster_totals: dict[str, Decimal] = {}
            for market, notional in working.items():
                cluster = contracts_metadata[market].cluster
                cluster_totals[cluster] = cluster_totals.get(cluster, Decimal("0")) + notional

            max_relative_breach = Decimal("0")
            this_iter_scaled: list[str] = []
            for cluster, total in cluster_totals.items():
                if total <= cluster_cap:
                    continue
                this_iter_scaled.append(cluster)
                if cluster_cap > 0:
                    shrink = cluster_cap / total
                    relative_breach = total / cluster_cap - Decimal("1")
                else:
                    # cluster_cap == 0: any positive notional is permanently
                    # over-cap. Treat shrink as 0 (zero out) and breach as
                    # "infinite" (above any tolerance) so convergence fails.
                    shrink = Decimal("0")
                    relative_breach = Decimal("9999")
                if relative_breach > max_relative_breach:
                    max_relative_breach = relative_breach
                for market in working:
                    if contracts_metadata[market].cluster == cluster:
                        working[market] = (working[market] * shrink).quantize(Decimal("0.01"))

            if this_iter_scaled:
                scaled_clusters_this_loop = this_iter_scaled

            if max_relative_breach <= CLUSTER_SHRINK_TOLERANCE:
                loop_converged = True
                break

        if loop_converged:
            convergence_met = True
            final_scaled_clusters = scaled_clusters_this_loop
            break

        # Non-convergence: find the binding cluster's lowest-momentum market,
        # drop it, restart. ``binding cluster`` = any cluster still above cap
        # in the FRESH (pre-shrink) working set, since shrunk values may be 0.
        fresh = {m: capped_notional[m] for m in working}
        cluster_totals_final: dict[str, Decimal] = {}
        for market, notional in fresh.items():
            cluster = contracts_metadata[market].cluster
            cluster_totals_final[cluster] = (
                cluster_totals_final.get(cluster, Decimal("0")) + notional
            )
        binding_clusters = [c for c, t in cluster_totals_final.items() if t > cluster_cap]
        if not binding_clusters:
            convergence_met = True
            final_scaled_clusters = scaled_clusters_this_loop
            break

        # Pick markets in any binding cluster; choose the one with the lowest
        # momentum z-score. Ties break on alphabetical market name (deterministic
        # for replay; arbitrary but stable).
        binding_set = frozenset(binding_clusters)
        candidates = [m for m in fresh if contracts_metadata[m].cluster in binding_set]
        if not candidates:
            convergence_met = True
            final_scaled_clusters = scaled_clusters_this_loop
            break

        def _drop_key(market: str) -> tuple[Decimal, str]:
            return (momentum_z_scores.get(market, Decimal("0")), market)

        drop_market = min(candidates, key=_drop_key)
        dropped.append(drop_market)
        # Restart on the reduced market set with fresh (pre-shrink) notionals.
        working = {m: capped_notional[m] for m in fresh if m != drop_market}

    trace: Stage3Trace = {
        "iterations": total_iterations,
        "convergence_tolerance_met": convergence_met,
        "psd_repair_applied": psd_repair_was_applied,
        "scaled_clusters": final_scaled_clusters,
        "dropped_due_to_non_convergence": dropped,
    }
    return _Stage3Output(
        scaled_notional=working,
        trace=trace,
        audit_events=(),
        dropped_markets=tuple(dropped),
    )


# ---------------------------------------------------------------------------
# Stage 4 — gross / net cap
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Stage4Output:
    scaled_notional: dict[str, Decimal]
    trace: Stage4Trace


def _stage_4_gross_net(
    *,
    cluster_scaled_notional: Mapping[str, Decimal],
    directions: Mapping[str, Direction],
    equity: Decimal,
    gross_cap: Decimal,
    net_cap: Decimal,
) -> _Stage4Output:
    """Uniform-shrink notionals so gross <= ``gross_cap x equity`` and
    abs(net) <= ``net_cap x equity`` (whichever binds tighter).

    Notionals at this stage are positive magnitudes; signs come from
    ``directions`` (LONG = +1, SHORT = -1). FLAT shouldn't appear here
    (filtered earlier) but is treated as +1 for safety (won't matter — FLAT
    won't have notional).
    """
    if not cluster_scaled_notional:
        empty_trace: Stage4Trace = {
            "gross_pre_scale": "0",
            "gross_post_scale": "0",
            "net_pre_scale": "0",
            "net_post_scale": "0",
        }
        return _Stage4Output(scaled_notional={}, trace=empty_trace)

    gross_pre: Decimal = Decimal("0")
    for notional in cluster_scaled_notional.values():
        gross_pre += notional
    net_pre: Decimal = Decimal("0")
    for market, notional in cluster_scaled_notional.items():
        if directions.get(market, Direction.LONG) == Direction.LONG:
            net_pre += notional
        else:
            net_pre -= notional
    gross_max = gross_cap * equity
    net_max = net_cap * equity

    shrink_factors: list[Decimal] = [Decimal("1")]
    if gross_pre > gross_max:
        shrink_factors.append(gross_max / gross_pre)
    if abs(net_pre) > net_max:
        shrink_factors.append(net_max / abs(net_pre))
    shrink = min(shrink_factors)

    scaled = {
        market: (notional * shrink).quantize(Decimal("0.01"))
        for market, notional in cluster_scaled_notional.items()
    }
    gross_post: Decimal = Decimal("0")
    for notional in scaled.values():
        gross_post += notional
    net_post: Decimal = Decimal("0")
    for market, notional in scaled.items():
        if directions.get(market, Direction.LONG) == Direction.LONG:
            net_post += notional
        else:
            net_post -= notional

    trace: Stage4Trace = {
        "gross_pre_scale": str(gross_pre),
        "gross_post_scale": str(gross_post),
        "net_pre_scale": str(net_pre),
        "net_post_scale": str(net_post),
    }
    return _Stage4Output(scaled_notional=scaled, trace=trace)


# ---------------------------------------------------------------------------
# Stage 5 — lot rounding
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Stage5Output:
    target_contracts: dict[str, int]
    trace: Stage5Trace


def _stage_5_lot_rounding(
    *,
    final_notional: Mapping[str, Decimal],
    contracts_metadata: Mapping[str, ContractMetadata],
    directions: Mapping[str, Direction],
) -> _Stage5Output:
    """Convert notionals to integer contract counts via banker's rounding.

    For each market:
      1. ``raw_count = notional / single_contract_notional`` (always positive
         here; sign is applied at the end from ``directions``).
      2. Round to integer using ``Decimal.ROUND_HALF_EVEN`` (banker's rounding).
      3. If rounded == 0: market is a sub-minimum drop; tag and exclude.
      4. ``deviation_pct = abs(raw_count - rounded) / raw_count`` if raw_count > 0.
      5. Apply sign: ``target = +rounded`` if LONG, ``-rounded`` if SHORT.

    Returns ``target_contracts`` keyed only by markets that survive (sub-
    minimum drops are absent — caller treats absence as 0).
    """
    target: dict[str, int] = {}
    pre_round: dict[str, str] = {}
    rounded_map: dict[str, int] = {}
    deviation_pct: dict[str, str] = {}
    sub_minimum_drops: list[str] = []

    for market, notional in final_notional.items():
        meta = contracts_metadata[market]
        if meta.single_contract_notional <= 0:
            raise SizingError(
                f"single_contract_notional must be positive for {market}; "
                f"got {meta.single_contract_notional}"
            )
        raw_count = notional / meta.single_contract_notional
        rounded = int(raw_count.to_integral_value(rounding=ROUND_HALF_EVEN))

        pre_round[market] = str(raw_count.quantize(Decimal("0.0001")))
        rounded_map[market] = rounded
        if raw_count > 0:
            dev = (abs(raw_count - Decimal(rounded)) / raw_count).quantize(Decimal("0.0001"))
            deviation_pct[market] = str(dev)
        else:
            deviation_pct[market] = "0.0000"

        # Stage 5 sub-minimum: rounded count is below the contract's minimum
        # lot size (defaults to 1, so any rounded == 0 trips). Direction sign
        # is applied at the end so the comparison here is on absolute count.
        if rounded < meta.min_lot:
            sub_minimum_drops.append(market)
            continue

        signed = rounded if directions.get(market, Direction.LONG) == Direction.LONG else -rounded
        target[market] = signed

    trace: Stage5Trace = {
        "contract_count_pre_round": pre_round,
        "rounded": rounded_map,
        "rounding_deviation_pct": deviation_pct,
        "sub_minimum_drops": sub_minimum_drops,
    }
    return _Stage5Output(target_contracts=target, trace=trace)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SizingInputs:
    """Aggregate input bundle for ``run_sizing_pipeline``.

    Grouped into a frozen dataclass so test fixtures and the signal service
    pass a single object — keeps the function signature flat.
    """

    candidate_signals: tuple[CandidateSignal, ...]
    equity: Decimal
    contracts_metadata: Mapping[str, ContractMetadata]
    sigma_annual: npt.NDArray[np.float64]
    sigma_index: tuple[str, ...]
    momentum_z_scores: Mapping[str, Decimal]
    vol_target_pct_annual: Decimal
    m_combined: Decimal = Decimal("1.0")
    target_cap_pct: Decimal = DEFAULT_TARGET_CAP_PCT
    hard_floor_pct: Decimal = DEFAULT_HARD_FLOOR_PCT
    cluster_cap_pct: Decimal = DEFAULT_CLUSTER_CAP_PCT
    gross_cap: Decimal = DEFAULT_GROSS_CAP
    net_cap: Decimal = DEFAULT_NET_CAP
    single_contract_overrides: Mapping[str, Decimal] = field(default_factory=dict)
    evaluated_at_utc: str = ""


def run_sizing_pipeline(inputs: SizingInputs) -> SizingResult:
    """Run all 5 stages of the sizing pipeline and return a ``SizingResult``.

    See module docstring for the algorithm. Pure function — no DB I/O. The
    caller flushes ``pending_audit_events`` to ``append_audit_event`` after.

    Raises ``SizingError`` on invariant violations (negative equity, missing
    metadata, sigma index mismatch, degenerate Sigma). Caller should NOT catch
    SizingError — it indicates a programming bug, not a strategy state.
    """
    if inputs.equity <= 0:
        raise SizingError(f"equity must be positive; got {inputs.equity}")
    if inputs.sigma_annual.shape != (len(inputs.sigma_index), len(inputs.sigma_index)):
        raise SizingError(
            f"sigma_annual shape {inputs.sigma_annual.shape} must match "
            f"sigma_index length {len(inputs.sigma_index)} squared"
        )

    candidate_markets = tuple(s.market for s in inputs.candidate_signals)
    directions: dict[str, Direction] = {s.market: s.direction for s in inputs.candidate_signals}

    pending_events: list[PendingAuditEvent] = []

    # PSD repair on Sigma runs once up-front per backend-spec §2.4.1 ("every
    # covariance Sigma used for portfolio-vol or cluster shrink runs through
    # nearest_psd"). Both Stage 1 (Sigma for portfolio-vol target) and Stage 3
    # (Sigma for cluster decisions) use the repaired matrix. Audit event for the
    # repair is emitted here once; Stage 3 trace records the boolean.
    sigma_repaired, was_repaired, eig_pre, eig_post = _nearest_psd(inputs.sigma_annual)
    if was_repaired:
        pending_events.append(
            PendingAuditEvent(
                event_type="psd_repair_applied",
                payload={
                    "min_eigval_pre": str(eig_pre),
                    "min_eigval_post": str(eig_post),
                    "matrix_dim": int(inputs.sigma_annual.shape[0]),
                    "evaluated_at_utc": inputs.evaluated_at_utc,
                },
            )
        )

    # Stage 0
    s0 = _stage_0_universe_filter(
        candidate_markets=candidate_markets,
        equity=inputs.equity,
        contracts_metadata=inputs.contracts_metadata,
        single_contract_overrides=inputs.single_contract_overrides,
        evaluated_at_utc=inputs.evaluated_at_utc,
    )
    pending_events.extend(s0.audit_events)

    # Stage 1
    s1 = _stage_1_inverse_vol(
        active_markets=s0.active_markets,
        equity=inputs.equity,
        sigma_annual_full=sigma_repaired,
        sigma_index=inputs.sigma_index,
        vol_target_pct_annual=inputs.vol_target_pct_annual,
        m_combined=inputs.m_combined,
    )

    # Stage 2
    s2 = _stage_2_per_position_cap(
        unconstrained_notional=s1.unconstrained_notional,
        equity=inputs.equity,
        target_cap_pct=inputs.target_cap_pct,
        hard_floor_pct=inputs.hard_floor_pct,
        overrides_applied=s0.overrides_applied,
    )

    # Stage 3 — Sigma no longer required (repaired upstream); cluster decisions
    # use notional sums alone. Caller passes ``psd_repair_was_applied`` flag
    # so the trace can record it.
    s3 = _stage_3_cluster_shrink(
        capped_notional=s2.capped_notional,
        contracts_metadata=inputs.contracts_metadata,
        psd_repair_was_applied=was_repaired,
        cluster_cap_pct=inputs.cluster_cap_pct,
        equity=inputs.equity,
        momentum_z_scores=inputs.momentum_z_scores,
    )
    pending_events.extend(s3.audit_events)

    # Stage 4
    s4 = _stage_4_gross_net(
        cluster_scaled_notional=s3.scaled_notional,
        directions=directions,
        equity=inputs.equity,
        gross_cap=inputs.gross_cap,
        net_cap=inputs.net_cap,
    )

    # Stage 5
    s5 = _stage_5_lot_rounding(
        final_notional=s4.scaled_notional,
        contracts_metadata=inputs.contracts_metadata,
        directions=directions,
    )

    sizing_trace: SizingTraceV1 = {
        "schema_version": SIZING_TRACE_SCHEMA_VERSION,
        "stage_0_universe": {
            "active_markets": list(s0.active_markets),
            "excluded": list(s0.excluded),
            "strategy_inputs": {},  # filled by the strategy module upstream
        },
        "stage_1_inverse_vol": s1.trace,
        "stage_2_per_position_cap": s2.trace,
        "stage_3_cluster": s3.trace,
        "stage_4_gross_net": s4.trace,
        "stage_5_lot_rounding": s5.trace,
    }

    log.info(
        "sizing_pipeline_completed",
        equity=str(inputs.equity),
        candidates=len(candidate_markets),
        active_after_stage_0=len(s0.active_markets),
        excluded_at_stage_0=len(s0.excluded),
        dropped_at_stage_3=len(s3.dropped_markets),
        dropped_at_stage_5=len(s5.trace["sub_minimum_drops"]),
        targets=len(s5.target_contracts),
    )
    return SizingResult(
        target_contracts=s5.target_contracts,
        sizing_trace=sizing_trace,
        pending_audit_events=tuple(pending_events),
    )


__all__ = [
    "DEFAULT_CLUSTER_CAP_PCT",
    "DEFAULT_GROSS_CAP",
    "DEFAULT_HARD_FLOOR_PCT",
    "DEFAULT_NET_CAP",
    "DEFAULT_TARGET_CAP_PCT",
    "SIZING_TRACE_SCHEMA_VERSION",
    "STAGE_0_NOTIONAL_PCT",
    "ContractMetadata",
    "PendingAuditEvent",
    "SizingError",
    "SizingInputs",
    "SizingResult",
    "run_sizing_pipeline",
]
