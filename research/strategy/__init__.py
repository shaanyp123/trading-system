"""``research/strategy`` — the resolution-agnostic strategy contract + references.

A :class:`~research.strategy.contract.ResearchStrategy` maps a ``BarSeries`` to a
per-bar target position (integer contracts), look-ahead-safe. The SAME contract
is driven by the simple daily evaluator (P1, research-only) and, from P2, by
LEAN (the authority). Reference strategies land incrementally: buy-and-hold (P1),
then time-series momentum / Donchian / the V1 adapter.
"""

from __future__ import annotations
