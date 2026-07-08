"""services/risk/sizing.py — risk-side nano-contract sizing enforcement.

Crypto-pivot C0-B4 (delta spec §3.4): "CME contract math → nano-contract
math (contract_size x mark), integer rounding, per-asset 1.4xE / gross
3.0xE caps, §5.6/§5.7 canonical patterns preserved in shape." The
V1-era Stages 0-5 pipeline (inverse-vol weighting, cluster shrink,
Higham PSD repair) died with the CME universe; what §5.6 locks — and
what this rewrite preserves — is the SHAPE contract:

* **pure function, no DB I/O** — inputs in, result + trace out;
* **Decimal at the money boundary** ([A05]) — this module runs entirely
  in Decimal (the float domain is contained to the parity-locked signal
  engine, ``services/signal/crypto_trend.py``);
* **staged pipeline with a serializable trace** — every stage records
  what it did into ``sizing_trace`` (JSONB-able, Decimals as strings)
  so the operator can audit exactly why a target was clamped;
* **§5.7 vol-target multiplier composition consumed, not re-derived** —
  ``m_combined`` arrives from ``services.risk.multipliers.m_combined()``
  (MIN-of-multipliers, never compounded) and is applied here as the
  first stage.

Division of labor with the signal engine (§3.3): the engine computes
integer-contract targets in float64 for bit-parity with the reference
backtest; THIS module is the independent risk-side belt-and-suspenders
that re-checks those targets in Decimal against live marks and the hard
caps before anything reaches the execution adapter. In a correct system
the clamp stages are no-ops — a non-no-op clamp is logged and traced,
never silent.

Pipeline (each stage traced):

  Stage M — apply ``m_combined`` (risk-state multiplier; §5.7):
            ``floor(|target| x m) x sign``.
  Stage A — per-asset cap: |contracts| x contract_size x mark ≤ 1.4 x E
            (clamp down to the largest compliant integer).
  Stage G — portfolio gross cap: Σ notionals ≤ 3.0 x E (proportional
            scale-down, floored per asset — same discipline as the
            engine's gross-cap stage).
  Stage L — integer/min-lot: targets are whole contracts; |n| < 1 → 0.

A02 BINDS — ``services/risk/**`` forbidden path.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Final

import structlog

log = structlog.get_logger()

#: Locked Amendment B cap fractions (delta spec §3.4; mirrors
#: ``services.risk.crypto_parameters`` — the seed test locks the pair).
DEFAULT_PER_ASSET_CAP_FRAC: Final[Decimal] = Decimal("1.4")
DEFAULT_GROSS_CAP_FRAC: Final[Decimal] = Decimal("3.0")

#: Trace schema tag persisted alongside the targets (successor of the
#: V1-era ``sizing_trace_v1``).
SIZING_TRACE_SCHEMA_VERSION: Final[str] = "crypto_sizing_trace_v1"

_ONE: Final[Decimal] = Decimal(1)


class SizingError(Exception):
    """Raised on unusable inputs (missing mark, non-positive equity/size).

    The caller must treat this as "do not trade this cycle" — never
    default a missing mark to zero and size against it.
    """


@dataclass(frozen=True, slots=True)
class CryptoProductSpec:
    """Per-asset venue spec the notional math needs (runtime-discovered)."""

    asset: str  # "BTC" / "ETH"
    product_id: str  # e.g. a CDE perp id discovered at runtime — never hardcoded
    contract_size: Decimal  # base-asset units per contract (nano: 0.01 / 0.10)


@dataclass(frozen=True, slots=True)
class SizingInputs:
    """Inputs to :func:`run_sizing_pipeline`. Pure data; no I/O handles."""

    equity_usd: Decimal
    m_combined: Decimal  # §5.7 MIN-composition output (1.0 when no multiplier active)
    engine_targets: dict[str, int]  # asset → signed integer contracts (signal engine)
    marks: dict[str, Decimal]  # asset → current mark (perp quote)
    specs: dict[str, CryptoProductSpec]
    per_asset_cap_frac: Decimal = DEFAULT_PER_ASSET_CAP_FRAC
    gross_cap_frac: Decimal = DEFAULT_GROSS_CAP_FRAC
    parameter_set_hash: str | None = None  # stamped into the trace


@dataclass(frozen=True, slots=True)
class SizingResult:
    """Final integer targets + the audit trace."""

    target_contracts: dict[str, int]
    sizing_trace: dict[str, Any]
    clamped: bool  # True when any stage changed any target


def _dec_str(value: Decimal) -> str:
    return str(value)


def _floor_int(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def _notional(contracts: int, spec: CryptoProductSpec, mark: Decimal) -> Decimal:
    return abs(Decimal(contracts)) * spec.contract_size * mark


def run_sizing_pipeline(inputs: SizingInputs) -> SizingResult:
    """Enforce the §3.4 caps on the engine's targets; emit the trace.

    Raises :class:`SizingError` on unusable inputs. Never returns
    fractional contracts. A clamp is logged at WARNING with the full
    before/after — in normal operation every stage is a no-op because
    the engine already sized within the same caps.
    """
    if inputs.equity_usd <= 0:
        raise SizingError(f"equity must be positive; got {inputs.equity_usd}")
    if inputs.m_combined <= 0 or inputs.m_combined > _ONE:
        raise SizingError(f"m_combined must be in (0, 1]; got {inputs.m_combined}")
    for asset, target in inputs.engine_targets.items():
        if asset not in inputs.specs:
            raise SizingError(f"no product spec for asset {asset!r}")
        if target != 0:
            mark = inputs.marks.get(asset)
            if mark is None or mark <= 0:
                raise SizingError(f"no usable mark for asset {asset!r} (got {mark!r})")
            if inputs.specs[asset].contract_size <= 0:
                raise SizingError(f"non-positive contract_size for asset {asset!r}")

    trace: dict[str, Any] = {
        "schema_version": SIZING_TRACE_SCHEMA_VERSION,
        "equity_usd": _dec_str(inputs.equity_usd),
        "m_combined": _dec_str(inputs.m_combined),
        "per_asset_cap_frac": _dec_str(inputs.per_asset_cap_frac),
        "gross_cap_frac": _dec_str(inputs.gross_cap_frac),
        "parameter_set_hash": inputs.parameter_set_hash,
        "engine_targets": dict(sorted(inputs.engine_targets.items())),
        "marks": {a: _dec_str(m) for a, m in sorted(inputs.marks.items())},
        "products": {
            a: {"product_id": s.product_id, "contract_size": _dec_str(s.contract_size)}
            for a, s in sorted(inputs.specs.items())
        },
        "stages": {},
    }

    # ---- Stage M: risk-state multiplier (§5.7 output; MIN, not compounded) --
    after_m: dict[str, int] = {}
    for asset, target in inputs.engine_targets.items():
        if target == 0 or inputs.m_combined == _ONE:
            after_m[asset] = target
        else:
            sign = 1 if target > 0 else -1
            after_m[asset] = _floor_int(abs(Decimal(target)) * inputs.m_combined) * sign
    trace["stages"]["m_combined"] = dict(sorted(after_m.items()))

    # ---- Stage A: per-asset cap (1.4 x E) -----------------------------------
    per_asset_cap_usd = inputs.per_asset_cap_frac * inputs.equity_usd
    after_a: dict[str, int] = {}
    for asset, target in after_m.items():
        if target == 0:
            after_a[asset] = 0
            continue
        spec = inputs.specs[asset]
        mark = inputs.marks[asset]
        notional = _notional(target, spec, mark)
        if notional <= per_asset_cap_usd:
            after_a[asset] = target
        else:
            sign = 1 if target > 0 else -1
            max_contracts = _floor_int(per_asset_cap_usd / (spec.contract_size * mark))
            after_a[asset] = max_contracts * sign
            log.warning(
                "sizing_per_asset_cap_clamped",
                asset=asset,
                engine_target=target,
                clamped_target=after_a[asset],
                notional_usd=_dec_str(notional),
                cap_usd=_dec_str(per_asset_cap_usd),
            )
    trace["stages"]["per_asset_cap"] = dict(sorted(after_a.items()))

    # ---- Stage G: portfolio gross cap (3.0 x E), proportional + floored ----
    gross_cap_usd = inputs.gross_cap_frac * inputs.equity_usd
    gross = sum(
        (_notional(t, inputs.specs[a], inputs.marks[a]) for a, t in after_a.items() if t != 0),
        Decimal(0),
    )
    after_g: dict[str, int] = dict(after_a)
    if gross > gross_cap_usd and gross > 0:
        scale = gross_cap_usd / gross
        for asset, target in after_a.items():
            if target == 0:
                continue
            sign = 1 if target > 0 else -1
            after_g[asset] = _floor_int(abs(Decimal(target)) * scale) * sign
        log.warning(
            "sizing_gross_cap_clamped",
            gross_usd=_dec_str(gross),
            cap_usd=_dec_str(gross_cap_usd),
            before=dict(sorted(after_a.items())),
            after=dict(sorted(after_g.items())),
        )
    trace["stages"]["gross_cap"] = {
        "gross_notional_usd": _dec_str(gross),
        "cap_usd": _dec_str(gross_cap_usd),
        "targets": dict(sorted(after_g.items())),
    }

    # ---- Stage L: integer / min-lot (|n| >= 1 or 0) --------------------------
    final: dict[str, int] = {a: (t if abs(t) >= 1 else 0) for a, t in after_g.items()}
    trace["stages"]["min_lot"] = dict(sorted(final.items()))
    trace["final_targets"] = dict(sorted(final.items()))

    clamped = final != dict(inputs.engine_targets)
    trace["clamped"] = clamped
    return SizingResult(target_contracts=final, sizing_trace=trace, clamped=clamped)


__all__ = [
    "DEFAULT_GROSS_CAP_FRAC",
    "DEFAULT_PER_ASSET_CAP_FRAC",
    "SIZING_TRACE_SCHEMA_VERSION",
    "CryptoProductSpec",
    "SizingError",
    "SizingInputs",
    "SizingResult",
    "run_sizing_pipeline",
]
