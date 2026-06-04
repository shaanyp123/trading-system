"""LEAN driver layer for the research harness (design §4.3, P2).

LEAN is the engine of record (design D1). This package programmatically renders a
throwaway per-run config, launches a daily LEAN backtest against a read-only COPY
of the on-disk bars (never the live ``trading_lean_data`` volume), and parses the
output into the SHARED :class:`research.eval.results.BacktestResult`.

Nothing here edits production ``lean/lean.json`` or ``lean/v1_strategy.py``; both
are read-only inputs. LEAN/Docker may be absent locally or in CI — the real run is
gated behind an availability check (:mod:`research.lean.images`) that SKIPS rather
than silently passing.
"""

from __future__ import annotations

__all__ = ["__doc__"]
