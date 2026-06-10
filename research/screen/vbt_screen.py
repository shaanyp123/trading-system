"""Optional vectorbt daily fast-screen seam (design D2).

vectorbt is an OPTIONAL research extra, NOT a core dependency — it is absent from
the research venv by default. This module establishes the optional-dependency
seam (availability check + graceful failure) without importing vectorbt at module
load, so the core harness runs anywhere numpy does.

P1 scope is the seam itself; the dependency-light :mod:`research.screen.daily_eval`
numpy evaluator is the screen actually in use. A vectorbt-accelerated sweep is NOT
implemented — P4 shipped the parameter sweep on numpy instead
(:mod:`research.eval.sweep`, driven by a validity config on ``engine='daily'``).
Any future vbt path must be gated by the §6.6 parity rail against LEAN before it
can inform a graduation decision (vbt never has authority; design D1/D2).
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
    """Unimplemented vectorbt sweep seam (the shipped P4 sweep is numpy).

    P4's parameter sweep landed on the numpy screen (``research/eval/sweep.py``,
    via a validity config on ``engine='daily'``), not vectorbt. Calling this
    surfaces the missing-dependency guidance (if vectorbt is absent) and the fact
    that any future vbt path stays parity-gated (design D2).
    """
    require_vectorbt()
    raise NotImplementedError(
        "the vectorbt daily sweep is not implemented — use engine='daily' with a "
        "validity config (the numpy sweep, research/eval/sweep.py). Any future vbt "
        "path must pass §6.6 parity vs LEAN before use (design D2)."
    )
