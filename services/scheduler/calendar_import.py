"""Calendar import + ratification scaffold (backend-spec §2.9 + §3.28).

This module ships the pure policy for macro event handling — tier
classification, deduplication, ratification check — so the signal pipeline
can already consume canonical ``MacroEvent`` data structures and the audit
chain can already record the right event names. The ACTUAL ingestion from
Forex Factory + Trading Economics, plus the Discord ``/calendar`` and
``/ratify`` command handlers, are Week 7 deliverables (per
``implementation-guide.md`` §11 Day 9 11:00 prompt: "Wire Discord
``/calendar`` and ``/ratify`` command stubs (full implementation in
Week 7)").

Scope of THIS module (Day 9):
- Tier classification: ``classify_tier(event_name)`` -> ``EventTier``.
  Tier-1 and tier-2 keyword matchers are LOCKED here so the rest of the
  system (kill-switch trigger, signal-cycle macro window) consumes a
  consistent taxonomy.
- Deduplication: ``dedupe_events(events)`` collapses same-(name, scheduled
  time, region) duplicates from multiple sources, preferring Forex Factory.
- Ratification check: ``check_ratification_for_date(events, target_date)``
  returns the list of UNRATIFIED tier-1 events scheduled for ``target_date``.
  Caller (the dispatch layer when it lands) invokes the kill-switch via
  ``services.risk.state_machine.plan_invoke_kill_switch`` with
  ``TransitionTrigger.CALENDAR_UNRATIFIED`` if the list is non-empty.
- Audit event payload builders for the four locked event types
  (``calendar_imported``, ``calendar_ratified``, ``calendar_unratified``,
  ``calendar_service_outage``).

Out of scope (deferred):
- Network fetch from Forex Factory + Trading Economics (Week 7; needs
  HTTP retry + caching + credential management).
- APScheduler cron job registration for daily 22:00 ET import + 23:00 ET
  ratification cutoff. The schedules are constants here; the actual
  ``schedule.add_job(...)`` calls happen in ``services/scheduler/main.py``
  when that lands.
- Discord ``/calendar`` and ``/ratify`` slash command handlers (Week 7
  with the rest of ``services/discord_bot``).

Schedule constants (backend-spec §2.9):
- ``CALENDAR_IMPORT_ET_HOUR/MINUTE``: 22:00 ET (NOT 20:00 ET as the
  implementation-guide §11 prompt suggests — the spec wins).
- ``RATIFICATION_CUTOFF_ET_HOUR/MINUTE``: 23:00 ET (NOT 16:00 ET).

Audit event names: spec §3.30 has ``calendar_imported`` (NOT
``calendar_event_imported`` as the implementation-guide §11 prompt names
it). A04 binds; canonical name wins.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Final, Literal

# ---------------------------------------------------------------------------
# Schedule constants
# ---------------------------------------------------------------------------

#: Daily macro calendar import job, ET wall clock. Backend-spec §2.9 row
#: "Schedules" specifies "22:00 ET calendar import". APScheduler with
#: ``ZoneInfo("America/New_York")`` handles DST natively.
CALENDAR_IMPORT_ET_HOUR: Final[int] = 22
CALENDAR_IMPORT_ET_MINUTE: Final[int] = 0

#: Daily ratification cutoff check. Spec §2.9: "23:00 ET ratification
#: cutoff check". Anything tier-1 still unratified at this point becomes
#: tomorrow morning's HALT_NEW (routine) trigger.
RATIFICATION_CUTOFF_ET_HOUR: Final[int] = 23
RATIFICATION_CUTOFF_ET_MINUTE: Final[int] = 0


# ---------------------------------------------------------------------------
# Enums + dataclasses
# ---------------------------------------------------------------------------


class EventTier(StrEnum):
    TIER_1 = "tier_1"  # NFP, CPI, FOMC, Fed Chair speak
    TIER_2 = "tier_2"  # PMI, retail sales
    TIER_3 = "tier_3"  # all others


class ImportSource(StrEnum):
    FOREX_FACTORY = "forex_factory"
    TRADING_ECONOMICS = "trading_economics"
    MANUAL = "manual"


# Tier-1 keyword patterns (case-insensitive). Backend-spec mentions
# "NFP, CPI, FOMC, Fed Chair speak" explicitly. Keyword matching is
# brittle by design — the source feeds use slightly different casings
# ("Non-Farm Payrolls" vs. "NFP" vs. "Nonfarm Payroll Employment Change").
# Centralized regex list lets the matcher be audited + extended in one place.
TIER_1_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\bNFP\b|non[-\s]?farm payroll", re.IGNORECASE),
    re.compile(r"\bCPI\b|consumer price index", re.IGNORECASE),
    re.compile(r"\bFOMC\b|federal open market committee", re.IGNORECASE),
    re.compile(r"\bfed (chair|chairman|chairwoman)\b", re.IGNORECASE),
    re.compile(r"\bppi\b|producer price index", re.IGNORECASE),
)

TIER_2_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\bpmi\b|purchasing managers", re.IGNORECASE),
    re.compile(r"\bretail sales\b", re.IGNORECASE),
    re.compile(r"\bism (manufacturing|services)\b", re.IGNORECASE),
    re.compile(r"\bgdp\b", re.IGNORECASE),
    re.compile(r"\bunemployment\b", re.IGNORECASE),
)


@dataclass(frozen=True, slots=True)
class MacroEvent:
    """Mirror of a ``macro_events`` row (backend-spec §3.28).

    Construct via ``MacroEvent(...)`` from raw API rows; pass through
    ``classify_tier`` to populate ``tier1_classification``; pass through
    ``dedupe_events`` to collapse multi-source duplicates before INSERT.
    """

    source: ImportSource
    event_name: str
    scheduled_at_utc: datetime
    region: str | None
    currency: str | None
    importance: Literal["high", "medium", "low"] | None
    tier1_classification: str | None  # 'NFP','CPI','FOMC',... when tier-1
    ratified_at_utc: datetime | None = None
    ratified_by: Literal["operator_discord", "operator_web"] | None = None


# ---------------------------------------------------------------------------
# Tier classification
# ---------------------------------------------------------------------------


def classify_tier(event_name: str) -> EventTier:
    """Classify an event name into tier-1 / tier-2 / tier-3.

    Tier-1 takes precedence over tier-2 if both match (defensive — the
    patterns shouldn't overlap, but if they do the more impactful tier wins).
    Empty / whitespace-only event names get tier-3 (everything that isn't
    explicitly elevated stays low-priority).
    """
    if any(pat.search(event_name) for pat in TIER_1_PATTERNS):
        return EventTier.TIER_1
    if any(pat.search(event_name) for pat in TIER_2_PATTERNS):
        return EventTier.TIER_2
    return EventTier.TIER_3


def tier1_label(event_name: str) -> str | None:
    """Return the canonical tier-1 label for an event name, or None.

    Used to populate ``macro_events.tier1_classification``. Maps any
    NFP-flavored name to 'NFP', any FOMC variant to 'FOMC', etc.
    """
    if re.search(r"\bNFP\b|non[-\s]?farm payroll", event_name, re.IGNORECASE):
        return "NFP"
    if re.search(r"\bCPI\b|consumer price index", event_name, re.IGNORECASE):
        return "CPI"
    if re.search(r"\bppi\b|producer price index", event_name, re.IGNORECASE):
        return "PPI"
    if re.search(r"\bFOMC\b|federal open market committee", event_name, re.IGNORECASE):
        return "FOMC"
    if re.search(r"\bfed (chair|chairman|chairwoman)\b", event_name, re.IGNORECASE):
        return "FED_CHAIR_SPEAK"
    return None


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

#: Source preference order: Forex Factory wins over Trading Economics on
#: identical events; manual overrides both.
SOURCE_PRIORITY: Final[dict[ImportSource, int]] = {
    ImportSource.MANUAL: 0,  # highest priority
    ImportSource.FOREX_FACTORY: 1,
    ImportSource.TRADING_ECONOMICS: 2,
}


def dedupe_events(events: Iterable[MacroEvent]) -> list[MacroEvent]:
    """Collapse same-(event_name, scheduled_at_utc, region) events.

    When a duplicate group exists, the source with the lowest
    ``SOURCE_PRIORITY`` value wins. Output ordering follows
    ``scheduled_at_utc`` ascending so consumers can stream chronologically.
    """
    by_key: dict[tuple[str, datetime, str | None], MacroEvent] = {}
    for ev in events:
        key = (ev.event_name.strip().lower(), ev.scheduled_at_utc, ev.region)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = ev
            continue
        if SOURCE_PRIORITY[ev.source] < SOURCE_PRIORITY[existing.source]:
            by_key[key] = ev
    return sorted(by_key.values(), key=lambda e: e.scheduled_at_utc)


# ---------------------------------------------------------------------------
# Ratification check
# ---------------------------------------------------------------------------


def check_ratification_for_date(
    events: Sequence[MacroEvent], target_date: date
) -> list[MacroEvent]:
    """Return tier-1 events scheduled on ``target_date`` that are NOT ratified.

    Caller (dispatch) compares this list to an empty set — non-empty means
    fire ``TransitionTrigger.CALENDAR_UNRATIFIED`` to the kill-switch.
    The check uses the event's ``scheduled_at_utc.date()`` in UTC, which is
    fine for the rate-of-change-over-day signal — sessions don't span days
    on the operator's wall clock for purposes of "tomorrow's calendar"
    queries.
    """
    out: list[MacroEvent] = []
    for ev in events:
        if classify_tier(ev.event_name) != EventTier.TIER_1:
            continue
        if ev.scheduled_at_utc.date() != target_date:
            continue
        if ev.ratified_at_utc is not None:
            continue
        out.append(ev)
    return sorted(out, key=lambda e: e.scheduled_at_utc)


# ---------------------------------------------------------------------------
# Audit event payload builders
# ---------------------------------------------------------------------------

CalendarAuditEventType = Literal[
    "calendar_imported",
    "calendar_ratified",
    "calendar_unratified",
    "calendar_service_outage",
]


@dataclass(frozen=True, slots=True)
class PendingAuditEvent:
    """Same shape as the other risk/audit modules' PendingAuditEvent."""

    event_type: CalendarAuditEventType
    payload: dict[str, Any]


def build_imported_event(
    *,
    macro_event_id: str,
    source: ImportSource,
    event_name: str,
    scheduled_at_utc: datetime,
    tier: EventTier,
    imported_at_utc: datetime,
) -> PendingAuditEvent:
    """Build a ``calendar_imported`` audit payload (locked taxonomy §3.30)."""
    return PendingAuditEvent(
        event_type="calendar_imported",
        payload={
            "macro_event_id": macro_event_id,
            "source": source.value,
            "event_name": event_name,
            "scheduled_at_utc": scheduled_at_utc.isoformat(),
            "tier": tier.value,
            "imported_at_utc": imported_at_utc.isoformat(),
        },
    )


def build_ratified_event(
    *,
    macro_event_id: str,
    ratified_by: Literal["operator_discord", "operator_web"],
    ratified_at_utc: datetime,
) -> PendingAuditEvent:
    return PendingAuditEvent(
        event_type="calendar_ratified",
        payload={
            "macro_event_id": macro_event_id,
            "ratified_by": ratified_by,
            "ratified_at_utc": ratified_at_utc.isoformat(),
        },
    )


def build_unratified_kill_switch_event(
    *,
    target_date: date,
    unratified_macro_event_ids: Sequence[str],
    cutoff_at_utc: datetime,
) -> PendingAuditEvent:
    """Audit payload for the cutoff-time check that fires the kill-switch.

    Caller pairs this with a ``state_transition_normal_to_halt`` event from
    ``services/risk/state_machine.py`` (trigger=CALENDAR_UNRATIFIED). Both
    events go through the same audit chain.
    """
    return PendingAuditEvent(
        event_type="calendar_unratified",
        payload={
            "target_date": target_date.isoformat(),
            "unratified_macro_event_ids": list(unratified_macro_event_ids),
            "cutoff_at_utc": cutoff_at_utc.isoformat(),
        },
    )


def build_service_outage_event(
    *,
    source: ImportSource,
    error_class: str,
    error_message: str,
    detected_at_utc: datetime,
) -> PendingAuditEvent:
    return PendingAuditEvent(
        event_type="calendar_service_outage",
        payload={
            "source": source.value,
            "error_class": error_class,
            "error_message": error_message,
            "detected_at_utc": detected_at_utc.isoformat(),
        },
    )


__all__ = [
    "CALENDAR_IMPORT_ET_HOUR",
    "CALENDAR_IMPORT_ET_MINUTE",
    "RATIFICATION_CUTOFF_ET_HOUR",
    "RATIFICATION_CUTOFF_ET_MINUTE",
    "SOURCE_PRIORITY",
    "TIER_1_PATTERNS",
    "TIER_2_PATTERNS",
    "CalendarAuditEventType",
    "EventTier",
    "ImportSource",
    "MacroEvent",
    "PendingAuditEvent",
    "build_imported_event",
    "build_ratified_event",
    "build_service_outage_event",
    "build_unratified_kill_switch_event",
    "check_ratification_for_date",
    "classify_tier",
    "dedupe_events",
    "tier1_label",
]
