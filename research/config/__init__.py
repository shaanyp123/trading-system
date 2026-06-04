"""``research/config`` — the operator's config-in interface (design §4.4).

The operator never edits engine code: a run is a YAML file (universe, dates,
resolution, strategy, sizing/leverage, engine, mode). P1 wires the daily
buy-and-hold path; later phases extend the same schema (the LEAN engine in P2,
leverage/sizing in P3, validity in P4).
"""

from __future__ import annotations
