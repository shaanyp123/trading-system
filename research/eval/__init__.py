"""``research/eval`` — result types, metrics, and the operator-legible report.

P1 ships the result container, a minimal metrics set (total return, CAGR, max
drawdown), and a report skeleton. The full ruin/risk metric suite (Sharpe,
Sortino, Calmar, vol-drag, risk-of-ruin, CVaR, margin-call frequency, peak
leverage) lands in P3; walk-forward / sweep / compare in P4 (design §9).
"""

from __future__ import annotations
