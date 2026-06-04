"""Optional vectorbt daily fast-screen seam (design D2).

vectorbt is an OPTIONAL research extra, NOT a core dependency — it is absent from
the research venv by default. This module establishes the optional-dependency
seam (availability check + graceful failure) without importing vectorbt at module
load, so the core harness runs anywhere numpy does.

P1 scope is the seam itself; the dependency-light :mod:`research.screen.daily_eval`
numpy evaluator is the screen P1 actually uses. The vectorbt-accelerated batch
parameter sweep — where vectorbt's vectorization earns its keep — lands in P4,
and every vbt result is gated by the §6.6 parity rail against LEAN before it can
inform a graduation decision (vbt never has authority; design D1/D2).
"""

from __future__ import annotations

import importlib.util


def vectorbt_available() -> bool:
    """True if ``vectorbt`` is importable (without importing it)."""
    return importlib.util.find_spec("vectorbt") is not None


def require_vectorbt() -> None:
    """Raise a clear, actionable error if vectorbt is not installed."""
    if not vectorbt_available():
        raise RuntimeError(
            "vectorbt is not installed. It is an OPTIONAL research extra; install "
            "it in the research venv to use the accelerated daily sweep "
            "(`pip install vectorbt`). The core harness does not require it — the "
            "numpy evaluator at research.screen.daily_eval is the P1 screen."
        )


def daily_sweep() -> None:
    """Placeholder for the P4 vectorbt-accelerated parameter sweep.

    Intentionally unimplemented in P1: a vbt sweep is only worth its dependency at
    sweep scale (P4), and every vbt result must pass §6.6 parity vs LEAN before
    use. Calling it surfaces both the missing-dependency guidance (if vectorbt is
    absent) and the phase gating.
    """
    require_vectorbt()
    raise NotImplementedError(
        "vectorbt daily sweep lands in P4 (parity-gated). P1 uses the numpy "
        "evaluator at research.screen.daily_eval.evaluate_daily."
    )
