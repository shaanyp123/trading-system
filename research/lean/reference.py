"""Build a :class:`LeanRunSpec` for a reference strategy over one instrument.

The LEAN-free, unit-testable bridge from a :class:`~research.config.schema.RunConfig`
to a driver run spec that targets :class:`ResearchRunnerAlgorithm`
(``research/lean/projects/research_runner.py``) — the authoritative engine for
``engine: lean`` reference-strategy runs (``donchian`` / ``buy_and_hold``). It maps
config to the runner's ``parameters`` block and carries the result metadata LEAN's
output does not (symbol / multiplier / strategy name).

Production V1 is NOT built here — it runs the real ``lean/v1_strategy.py`` (which
POSTs) via :func:`research.eval.reproduce_v1.build_v1_run_spec`. The research runner
is self-contained + non-posting, so its spec needs no ``strategies/`` mount and no
``posts_to_api`` flag (the driver applies the isolation env uniformly regardless).
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from research.config.schema import RunConfig
from research.data.contract_specs import get_spec
from research.lean.driver import LeanRunSpec

#: Repo path (read-only input) to the generic runner algorithm.
_RESEARCH_RUNNER: Final = Path("research/lean/projects/research_runner.py")

#: Strategy refs the research runner can express inline (V1 goes via reproduce_v1).
REFERENCE_STRATEGIES: Final = ("buy_and_hold", "donchian")


def _strategy_label(strategy_ref: str, *, channel: int, contracts: int) -> str:
    """Report label matching the numpy twins' ``.name`` (donchian.py / buy_and_hold.py)."""
    if strategy_ref == "donchian":
        return f"donchian({channel},{contracts})"
    return f"buy_and_hold({contracts})"


def build_reference_run_spec(cfg: RunConfig, symbol: str) -> LeanRunSpec:
    """A :class:`LeanRunSpec` running ``cfg``'s reference strategy on ``symbol`` in LEAN.

    ``symbol`` is one instrument from ``cfg.universe`` (the runner is single-
    instrument; the caller loops). Requires a bounded window — LEAN needs a concrete
    end date, so ``date_range.end`` is mandatory for ``engine: lean`` runs.
    """
    if cfg.strategy_ref not in REFERENCE_STRATEGIES:
        raise ValueError(
            f"build_reference_run_spec only handles {REFERENCE_STRATEGIES}, got "
            f"{cfg.strategy_ref!r} — production V1 runs via reproduce_v1.build_v1_run_spec"
        )
    if cfg.end is None:
        raise ValueError(
            "engine='lean' needs date_range.end — LEAN backtests a bounded window "
            "(the numpy screen can run open-ended, but the authority cannot)"
        )
    spec = get_spec(symbol)
    channel = cfg.channel or 20
    contracts = abs(cfg.contracts)  # reference strategies are long-only (size magnitude)
    parameters: dict[str, str] = {
        "RESEARCH_SYMBOL": symbol,
        "STRATEGY_REF": cfg.strategy_ref,
        "DONCHIAN_CHANNEL": str(channel),
        "CONTRACTS": str(contracts),
        "START_DATE": cfg.start.strftime("%Y%m%d"),
        "END_DATE": cfg.end.strftime("%Y%m%d"),
        "STARTING_CASH_USD": str(int(cfg.starting_cash if cfg.starting_cash is not None else 100_000)),
        "RESOLUTION": cfg.resolution,
        "COSTS_MODEL": cfg.costs_model,
    }
    return LeanRunSpec(
        algorithm_type_name="ResearchRunnerAlgorithm",
        algorithm_source=_RESEARCH_RUNNER,
        parameters=parameters,
        data_root=cfg.data_root,
        symbol=symbol,
        multiplier=float(spec.multiplier),
        strategy_name=_strategy_label(cfg.strategy_ref, channel=channel, contracts=contracts),
        extra_package_mounts=(),  # self-contained: no strategies/ import
        starting_cash=cfg.starting_cash,
        posts_to_api=False,
    )
