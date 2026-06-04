"""``research/`` — futures backtesting + research harness (DRIVES LEAN).

Design: ``Docs/futures-backtester-design.md`` (SIGNED OFF 2026-06-03). This is a
research / iteration / leverage-modeling / reporting layer. It does NOT implement
a backtest engine: LEAN is the engine of record (design D1). vectorbt is an
optional daily-only fast screen (design D2). Nothing here touches a forbidden
path (dev-guide §2.2), the production DB, or the live ``trading_lean_data``
Docker volume.

**House-rule exemption (design D8 / §13, operator-granted 2026-06-03):** the
production Decimal-for-money rule (dev-guide §3.8) is EXEMPTED inside this
package's analytics/screen/metrics modules — numpy/pandas/vbt are float by
nature. Contract-spec money values (multipliers, tick sizes) stay ``Decimal``;
the exemption covers derived analytics (returns, equity curves, risk metrics).
Every value that crosses into a governance artifact (a PR backtest delta,
``parameter_sets``, anything persisted to the audit chain) is re-materialized as
``Decimal``/string at the boundary.

**Build status:** P1 (daily spine). Daily resolution only — intraday (P5/P6) is
deferred per the 2026-06-03 sign-off (no vendor commitment). The harness is
resolution-agnostic, so intraday is a future data + ingestion job, not an engine
change.
"""

from __future__ import annotations

__all__ = ["__version__"]

#: Harness version. Bumped per phase; stamped into every run's ``result.json`` so
#: a report can be traced back to the code that produced it.
__version__ = "0.1.0-p1"
