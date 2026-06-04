"""Render a THROWAWAY per-run ``lean.json`` into a temp project dir (design §4.3).

The driver renders a fresh config per run; **production ``lean/lean.json`` is never
edited** — it is a read-only input. The rendered config mirrors the production
template *shape* (so the ``infrastructure/lean_local`` entrypoint's deep-merge onto
the upstream Launcher config behaves identically), with per-run overrides:
``algorithm-type-name`` / ``algorithm-location`` / the ``parameters`` block /
``environment`` (``backtesting``).

LEAN's backtest window + starting cash for the production ``v1_strategy.py`` are set
inside ``initialize()`` (``set_start_date`` / ``set_cash``), not in ``lean.json`` —
so they are NOT renderable for V1 (reproduce-V1 uses V1's own hard-coded window).
The research *reference* strategies (e.g. the Donchian under
``research/lean/projects/``) instead read ``START_DATE`` / ``END_DATE`` /
``STARTING_CASH_USD`` from the rendered ``parameters`` block, so the driver fully
controls their window + cash. All parameter values are strings — LEAN's
``get_parameter`` returns strings (design D8: re-materialize at the boundary).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import structlog

_log = structlog.get_logger(__name__)

#: Production LEAN config — READ-ONLY source of the live V1 ``parameters`` block.
_PROD_LEAN_JSON: Final = Path("lean/lean.json")

#: Rendered config filename inside the throwaway project dir.
CONFIG_FILENAME: Final = "lean.json"


@dataclass(frozen=True, slots=True)
class LeanProjectConfig:
    """The per-run inputs that vary; everything else is fixed boilerplate."""

    algorithm_type_name: str  # e.g. "V1TrendFollowingAlgorithm" or "DonchianReferenceAlgorithm"
    algorithm_location: str  # e.g. "v1_strategy.py" or "donchian_reference.py"
    parameters: dict[str, str]  # LEAN parameter map (all string values)
    description: str = "research throwaway config (P2 LEAN driver)"
    environment: str = "backtesting"
    #: In-container results dir for the raw-docker backend (mounted to the host
    #: output dir). ``None`` leaves it to the backend default (the CLI's ``--output``).
    results_destination: str | None = None


def _stringify_params(params: Mapping[str, object]) -> dict[str, str]:
    """Coerce a parameter map to all-string values (LEAN's surface).

    Booleans render as ``"True"``/``"False"`` to match the production
    ``_coerce_bool_param`` literal check in ``lean/v1_strategy.py``.
    """
    out: dict[str, str] = {}
    for key, value in params.items():
        if isinstance(value, bool):
            out[str(key)] = "True" if value else "False"
        elif isinstance(value, Decimal):
            out[str(key)] = format(value, "f")  # no scientific notation
        else:
            out[str(key)] = str(value)
    return out


def load_production_v1_parameters(lean_json: Path = _PROD_LEAN_JSON) -> dict[str, str]:
    """Read the live ``parameters`` block from production ``lean/lean.json``.

    READ-ONLY (design: ``lean/lean.json`` is a read-only input). This is exactly
    "the live params" the reproduce-V1 run (design §8) backtests with.
    """
    data = json.loads(lean_json.read_text(encoding="utf-8"))
    params = data.get("parameters")
    if not isinstance(params, dict) or not params:
        raise ValueError(f"{lean_json}: missing/empty 'parameters' block")
    return _stringify_params(params)


def reference_parameters(
    base: dict[str, object],
    *,
    start: date,
    end: date,
    starting_cash: int,
) -> dict[str, str]:
    """Build a reference-strategy ``parameters`` block with window + cash baked in.

    The research reference algorithms read these to drive ``set_start_date`` /
    ``set_end_date`` / ``set_cash`` from config — so the driver, not the algorithm,
    owns the backtest window.
    """
    params: dict[str, object] = dict(base)
    params["START_DATE"] = start.strftime("%Y%m%d")
    params["END_DATE"] = end.strftime("%Y%m%d")
    params["STARTING_CASH_USD"] = str(int(starting_cash))
    return _stringify_params(params)


def render_config(cfg: LeanProjectConfig) -> dict[str, object]:
    """Return the throwaway config as a dict (pure; tested without LEAN).

    Mirrors the production template shape: the ``backtesting`` environment carries
    only ``live-mode: false`` and inherits its handler bindings from the upstream
    Launcher config via the entrypoint deep-merge (identical to production).
    """
    if not cfg.parameters:
        raise ValueError("LeanProjectConfig.parameters must be non-empty")
    rendered: dict[str, object] = {
        "algorithm-language": "Python",
        "algorithm-type-name": cfg.algorithm_type_name,
        "algorithm-location": cfg.algorithm_location,
        "description": cfg.description,
        "parameters": _stringify_params(cfg.parameters),
        "environments": {cfg.environment: {"live-mode": False}},
    }
    if cfg.results_destination is not None:
        rendered["results-destination-folder"] = cfg.results_destination
    return rendered


def write_config(project_dir: Path, cfg: LeanProjectConfig) -> Path:
    """Write the rendered ``lean.json`` into ``project_dir``; return its path.

    Refuses to write to the production ``lean/`` directory — a defensive guard so a
    mis-wired call can never clobber ``lean/lean.json`` (design: never edit prod).
    """
    project_dir = project_dir.resolve()
    if project_dir == _PROD_LEAN_JSON.resolve().parent:
        raise ValueError(
            f"refusing to render a throwaway config into the production lean/ dir "
            f"({project_dir}); render into a temp project dir instead"
        )
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / CONFIG_FILENAME
    path.write_text(json.dumps(render_config(cfg), indent=2) + "\n", encoding="utf-8")
    _log.debug(
        "research_lean_config_rendered",
        path=str(path),
        algorithm_type_name=cfg.algorithm_type_name,
        environment=cfg.environment,
        param_keys=sorted(cfg.parameters),
    )
    return path
