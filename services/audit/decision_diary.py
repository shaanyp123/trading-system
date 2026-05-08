"""Decision diary writer — pure-policy validator + INSERT plan (backend-spec §3.13).

Same plan-then-apply shape as services/risk/sizing.py and services/risk/
state_machine.py: this module returns ``DiaryEntryPlan`` structs; the
caller (``services/api/routes/decision_diary.py`` when it lands) owns the
DB I/O — INSERT into ``decision_diary``.

Spec deviations and clarifications:

1. Tag enum — backend-spec §3.13 specifies the canonical tag enum:
   ``data_concern``, ``regime_concern``, ``size_concern``, ``manual_judgment``,
   ``other`` — matching the alembic 0003 CHECK constraint. The
   ``implementation-guide.md`` §11 Day 9 prompt suggested a different tag
   list (``signal_override``, ``parameter_change_reviewed``, etc.); that
   list is NOT in the schema and would fail the DB CHECK at INSERT. Per
   dev-guide §1.3, spec wins on architecture; this module follows the
   spec.

2. No separate ``decision_diary_logged`` audit_log event. The
   ``implementation-guide.md`` Day 9 prompt mentions writing a
   ``decision_diary_logged`` audit event, but that event_type is not in
   the locked taxonomy (backend-spec §3.30). The decision_diary row itself
   IS the audit-style record (it carries ``ts_utc``, ``monotonic_ns``,
   ``author``); a duplicate audit_log entry would be redundant. Anti-
   pattern A04 ("DO NOT introduce a new audit event_type without adding
   it to the locked taxonomy") binds here — if the operator wants a
   parallel audit_log entry, that's a separate PR with a taxonomy
   migration.

3. Reasoning_text length: spec §3.13 only enforces ``length(reasoning_text)
   >= 10`` for ``author='operator'``. The ``implementation-guide.md``
   prompt also mentioned 2000-char ceiling; this module enforces both
   (``len(reasoning_text) <= 2000``) at the validator surface as a soft
   input limit even though the DB doesn't enforce it. Keeps the API
   surface predictable.

Constraints enforced (matching alembic 0003 CHECK constraints):
- ``entry_class IN ('signal_response','forward_looking','general')``
- ``tag IN ('data_concern','regime_concern','size_concern',
  'manual_judgment','other')``
- ``author IN ('operator','agent')``
- ``entry_class='signal_response' -> linked_signal_id IS NOT NULL``
- ``entry_class != 'signal_response' -> linked_signal_id IS NULL``
- ``author='operator' -> length(reasoning_text) >= 10``
- ``length(reasoning_text) <= 2000`` (this module only)

Tests in ``tests/unit/test_decision_diary.py`` cover each branch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final
from uuid import UUID


class EntryClass(StrEnum):
    SIGNAL_RESPONSE = "signal_response"
    FORWARD_LOOKING = "forward_looking"
    GENERAL = "general"


class DiaryTag(StrEnum):
    DATA_CONCERN = "data_concern"
    REGIME_CONCERN = "regime_concern"
    SIZE_CONCERN = "size_concern"
    MANUAL_JUDGMENT = "manual_judgment"
    OTHER = "other"


class Author(StrEnum):
    OPERATOR = "operator"
    AGENT = "agent"


#: Operator entries must justify the action; the DB enforces this. The agent
#: bypass exists because the agent's ``reasoning_text`` is generated and may
#: legitimately be a 1-line summary referencing a longer prompt log.
OPERATOR_REASONING_MIN_CHARS: Final[int] = 10
REASONING_MAX_CHARS: Final[int] = 2000


class DecisionDiaryError(ValueError):
    """Raised when a diary entry would violate a CHECK constraint.

    ValueError-subclass so FastAPI handlers can trap with a single except
    branch and return a 400 with the canonical error envelope per dev-guide
    §3.6.
    """


@dataclass(frozen=True, slots=True)
class DiaryEntryPlan:
    """A validated diary entry, ready for INSERT.

    The caller writes this row in a single transaction; no audit_log entry
    is written alongside (see module docstring item 2).

    Fields mirror ``decision_diary`` columns (backend-spec §3.13). ``ts_utc``
    and ``monotonic_ns`` are filled by the policy at validation time so the
    row carries an authoritative single-source timestamp pair.
    """

    account_id: UUID
    entry_class: EntryClass
    linked_signal_id: UUID | None
    linked_market_id: str | None
    tag: DiaryTag
    ts_utc: datetime
    monotonic_ns: int | None
    author: Author
    reasoning_text: str


def plan_diary_entry(
    *,
    account_id: UUID,
    entry_class: EntryClass,
    linked_signal_id: UUID | None,
    linked_market_id: str | None,
    tag: DiaryTag,
    author: Author,
    reasoning_text: str,
    ts_utc: datetime | None = None,
    monotonic_ns: int | None = None,
) -> DiaryEntryPlan:
    """Validate a proposed diary entry and return an INSERT-ready plan.

    Raises ``DecisionDiaryError`` on any constraint violation. The DB CHECK
    constraints will catch the same cases at INSERT time; pre-validating
    here gives the API a clear 400 response instead of an opaque
    IntegrityError.

    ``ts_utc`` defaults to ``datetime.now(tz=UTC)`` (per anti-pattern A06);
    callers can pass an explicit timestamp for replay / backfill.
    """
    # entry_class -> linked_signal_id constraint (spec §3.13)
    if entry_class == EntryClass.SIGNAL_RESPONSE:
        if linked_signal_id is None:
            raise DecisionDiaryError(
                "entry_class='signal_response' requires linked_signal_id "
                "(decision_diary CHECK constraint signal_response_requires_link)"
            )
    else:
        if linked_signal_id is not None:
            raise DecisionDiaryError(
                f"entry_class={entry_class.value!r} forbids linked_signal_id "
                "(decision_diary CHECK constraint non_signal_response_no_link)"
            )

    # author / reasoning_text length constraints
    if author == Author.OPERATOR and len(reasoning_text) < OPERATOR_REASONING_MIN_CHARS:
        raise DecisionDiaryError(
            f"reasoning_text must be at least {OPERATOR_REASONING_MIN_CHARS} characters "
            f"for operator entries; got {len(reasoning_text)} "
            "(decision_diary CHECK constraint operator_min_10)"
        )
    if len(reasoning_text) > REASONING_MAX_CHARS:
        raise DecisionDiaryError(
            f"reasoning_text exceeds {REASONING_MAX_CHARS}-char input limit; "
            f"got {len(reasoning_text)}"
        )

    return DiaryEntryPlan(
        account_id=account_id,
        entry_class=entry_class,
        linked_signal_id=linked_signal_id,
        linked_market_id=linked_market_id,
        tag=tag,
        ts_utc=ts_utc if ts_utc is not None else datetime.now(tz=UTC),
        monotonic_ns=monotonic_ns,
        author=author,
        reasoning_text=reasoning_text,
    )


__all__ = [
    "OPERATOR_REASONING_MIN_CHARS",
    "REASONING_MAX_CHARS",
    "Author",
    "DecisionDiaryError",
    "DiaryEntryPlan",
    "DiaryTag",
    "EntryClass",
    "plan_diary_entry",
]
