"""services/risk/multipliers.py — vol-target multiplier composition (§5.7).

Canonical pattern from ``Docs/claude-dev-guide.md`` §5.7, landed verbatim
with the C1 strategy worker (its first live consumer — the worker passes
``m_combined`` into ``services.risk.sizing.run_sizing_pipeline`` which
consumes, never re-derives, the composition per the §5.6/§5.7 contract).

Composition rule (LOCKED): **MIN of active multipliers, never
compounded**, with an implicit floor of ``1.0`` when nothing is active.

Inputs (crypto-pivot mapping):

* ``capital_event_session_count`` — sessions since a threshold-met
  capital event ("session" = UTC calendar day post-pivot, same calendar
  the CONVALESCENT counter ticks on). The C1 strategy worker derives it
  via :func:`services.risk.capital_events.capital_event_session_count`
  from the latest threshold-met ``capital_events`` row's
  ``effective_at_utc`` UTC date (the ``risk_state`` absolute
  ``*_session_no`` fields are Phase-0 placeholders no consumer reads
  for sizing).
* ``is_convalescent`` — current kill-switch state is CONVALESCENT
  (backend-spec §2.5: trading permitted at half size during the 5-clean-
  UTC-day probation window).
* ``monthly_dd_pct`` — calendar-month drawdown as a signed fraction
  (e.g. ``Decimal("-0.12")`` for -12%).

A02 BINDS — ``services/risk/**`` forbidden path; ``risk-review-approved``
required. A05 — Decimal only.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

import structlog

log = structlog.get_logger()

_M_CAPITAL_EVENT: Final[Decimal] = Decimal("0.5")
_M_CONVALESCENT: Final[Decimal] = Decimal("0.5")
_M_MONTHLY_DD: Final[Decimal] = Decimal("0.5")
_CAPITAL_EVENT_SESSIONS: Final[int] = 5
_MONTHLY_DD_THRESHOLD: Final[Decimal] = Decimal("-0.10")  # -10% in calendar month


def m_combined(
    capital_event_session_count: int,
    is_convalescent: bool,
    monthly_dd_pct: Decimal,
) -> Decimal:
    """MIN-of-multipliers. NOT compounded. Implicit floor of 1.0 when no multiplier active.

    Unit test cases (dev-guide §5.7, locked):

    - capital_event(sessions=3) + convalescent → min(0.5, 0.5) = 0.5  NOT 0.25
    - capital_event(sessions=6) + convalescent → min(1.0, 0.5) = 0.5
    - no active events → min() → 1.0 (floor)
    - monthly_dd=-12% alone → min(0.5) = 0.5
    """
    multipliers: list[Decimal] = []

    if 1 <= capital_event_session_count <= _CAPITAL_EVENT_SESSIONS:
        multipliers.append(_M_CAPITAL_EVENT)
        log.debug("m_capital_event_active", sessions=capital_event_session_count)

    if is_convalescent:
        multipliers.append(_M_CONVALESCENT)
        log.debug("m_convalescent_active")

    if monthly_dd_pct < _MONTHLY_DD_THRESHOLD:
        multipliers.append(_M_MONTHLY_DD)
        log.debug("m_monthly_dd_active", monthly_dd_pct=str(monthly_dd_pct))

    result = min(multipliers) if multipliers else Decimal("1.0")
    log.debug("m_combined_computed", m_combined=str(result), active_multipliers=len(multipliers))
    return result


__all__ = [
    "m_combined",
]
