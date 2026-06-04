"""``research/screen`` — research-only (NON-authoritative) fast evaluation.

Two evaluators live here, both for idea triage only — neither is an authority.
LEAN is the engine of record (design D1); every result that informs a graduation
decision is reproduced in LEAN (P2) and, for daily strategies, gated by the §6.6
parity rail.

* :mod:`research.screen.daily_eval` — a dependency-light numpy close-to-close
  mark-to-market evaluator. No vectorbt required, so P1 runs anywhere numpy does.
* :mod:`research.screen.vbt_screen` — an OPTIONAL vectorbt-accelerated daily
  sweep (design D2). vectorbt is imported lazily; its absence never breaks the
  core harness.
"""

from __future__ import annotations
