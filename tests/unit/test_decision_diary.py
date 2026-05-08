"""Unit tests for ``services/audit/decision_diary.py``.

Backend-spec §3.13 CHECK constraints:
- entry_class in ('signal_response','forward_looking','general')
- tag in ('data_concern','regime_concern','size_concern','manual_judgment','other')
- author in ('operator','agent')
- entry_class='signal_response' AND linked_signal_id IS NOT NULL
- entry_class != 'signal_response' AND linked_signal_id IS NULL
- author='operator' -> length(reasoning_text) >= 10

Plus the module-only soft cap: length(reasoning_text) <= 2000.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from services.audit.decision_diary import (
    OPERATOR_REASONING_MIN_CHARS,
    REASONING_MAX_CHARS,
    Author,
    DecisionDiaryError,
    DiaryTag,
    EntryClass,
    plan_diary_entry,
)


def _account_id() -> object:
    return uuid4()


class TestEntryClassConstraints:
    """signal_response REQUIRES linked_signal_id; others FORBID it (§3.13)."""

    def test_signal_response_without_signal_id_raises(self) -> None:
        with pytest.raises(DecisionDiaryError, match="signal_response_requires_link"):
            plan_diary_entry(
                account_id=_account_id(),
                entry_class=EntryClass.SIGNAL_RESPONSE,
                linked_signal_id=None,
                linked_market_id="/MES",
                tag=DiaryTag.MANUAL_JUDGMENT,
                author=Author.OPERATOR,
                reasoning_text="missing the signal id linkage",
            )

    def test_signal_response_with_signal_id_succeeds(self) -> None:
        signal_id = uuid4()
        plan = plan_diary_entry(
            account_id=_account_id(),
            entry_class=EntryClass.SIGNAL_RESPONSE,
            linked_signal_id=signal_id,
            linked_market_id="/MES",
            tag=DiaryTag.MANUAL_JUDGMENT,
            author=Author.OPERATOR,
            reasoning_text="shorted /MES against trend; macro headline",
        )
        assert plan.linked_signal_id == signal_id

    def test_forward_looking_with_signal_id_raises(self) -> None:
        with pytest.raises(DecisionDiaryError, match="non_signal_response_no_link"):
            plan_diary_entry(
                account_id=_account_id(),
                entry_class=EntryClass.FORWARD_LOOKING,
                linked_signal_id=uuid4(),
                linked_market_id=None,
                tag=DiaryTag.REGIME_CONCERN,
                author=Author.OPERATOR,
                reasoning_text="planning a sub-universe expansion",
            )

    def test_general_with_signal_id_raises(self) -> None:
        with pytest.raises(DecisionDiaryError, match="non_signal_response_no_link"):
            plan_diary_entry(
                account_id=_account_id(),
                entry_class=EntryClass.GENERAL,
                linked_signal_id=uuid4(),
                linked_market_id=None,
                tag=DiaryTag.OTHER,
                author=Author.OPERATOR,
                reasoning_text="general note about something",
            )

    def test_forward_looking_without_signal_id_succeeds(self) -> None:
        plan = plan_diary_entry(
            account_id=_account_id(),
            entry_class=EntryClass.FORWARD_LOOKING,
            linked_signal_id=None,
            linked_market_id=None,
            tag=DiaryTag.DATA_CONCERN,
            author=Author.OPERATOR,
            reasoning_text="watching for vol regime shift",
        )
        assert plan.entry_class == EntryClass.FORWARD_LOOKING


class TestReasoningLengthConstraints:
    def test_operator_reasoning_below_min_raises(self) -> None:
        with pytest.raises(DecisionDiaryError, match="operator_min_10"):
            plan_diary_entry(
                account_id=_account_id(),
                entry_class=EntryClass.GENERAL,
                linked_signal_id=None,
                linked_market_id=None,
                tag=DiaryTag.OTHER,
                author=Author.OPERATOR,
                reasoning_text="too short",  # 9 chars
            )

    def test_operator_reasoning_at_min_succeeds(self) -> None:
        plan = plan_diary_entry(
            account_id=_account_id(),
            entry_class=EntryClass.GENERAL,
            linked_signal_id=None,
            linked_market_id=None,
            tag=DiaryTag.OTHER,
            author=Author.OPERATOR,
            reasoning_text="x" * OPERATOR_REASONING_MIN_CHARS,
        )
        assert plan.reasoning_text == "x" * OPERATOR_REASONING_MIN_CHARS

    def test_agent_reasoning_below_operator_min_succeeds(self) -> None:
        """Agent entries bypass the 10-char minimum (spec only enforces it
        for operator)."""
        plan = plan_diary_entry(
            account_id=_account_id(),
            entry_class=EntryClass.GENERAL,
            linked_signal_id=None,
            linked_market_id=None,
            tag=DiaryTag.OTHER,
            author=Author.AGENT,
            reasoning_text="ack",  # 3 chars; would fail for operator
        )
        assert plan.author == Author.AGENT

    def test_reasoning_above_max_raises(self) -> None:
        with pytest.raises(DecisionDiaryError, match=r"exceeds 2000"):
            plan_diary_entry(
                account_id=_account_id(),
                entry_class=EntryClass.GENERAL,
                linked_signal_id=None,
                linked_market_id=None,
                tag=DiaryTag.OTHER,
                author=Author.OPERATOR,
                reasoning_text="x" * (REASONING_MAX_CHARS + 1),
            )

    def test_reasoning_at_max_succeeds(self) -> None:
        plan = plan_diary_entry(
            account_id=_account_id(),
            entry_class=EntryClass.GENERAL,
            linked_signal_id=None,
            linked_market_id=None,
            tag=DiaryTag.OTHER,
            author=Author.OPERATOR,
            reasoning_text="x" * REASONING_MAX_CHARS,
        )
        assert len(plan.reasoning_text) == REASONING_MAX_CHARS


class TestPlanDefaults:
    def test_ts_utc_defaults_to_now_with_tzinfo(self) -> None:
        plan = plan_diary_entry(
            account_id=_account_id(),
            entry_class=EntryClass.GENERAL,
            linked_signal_id=None,
            linked_market_id=None,
            tag=DiaryTag.OTHER,
            author=Author.OPERATOR,
            reasoning_text="ten chars+",
        )
        assert plan.ts_utc.tzinfo is not None  # A06: tz-aware

    def test_explicit_ts_utc_preserved(self) -> None:
        explicit = datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC)
        plan = plan_diary_entry(
            account_id=_account_id(),
            entry_class=EntryClass.GENERAL,
            linked_signal_id=None,
            linked_market_id=None,
            tag=DiaryTag.OTHER,
            author=Author.OPERATOR,
            reasoning_text="ten chars+",
            ts_utc=explicit,
        )
        assert plan.ts_utc == explicit


class TestTagEnum:
    """Spec §3.13 tag enum: data_concern, regime_concern, size_concern,
    manual_judgment, other. Implementation guide's alternative tags
    (signal_override, parameter_change_reviewed, etc.) are NOT honored —
    they would fail the DB CHECK constraint."""

    def test_each_canonical_tag_accepts(self) -> None:
        for tag in DiaryTag:
            plan = plan_diary_entry(
                account_id=_account_id(),
                entry_class=EntryClass.GENERAL,
                linked_signal_id=None,
                linked_market_id=None,
                tag=tag,
                author=Author.OPERATOR,
                reasoning_text="ten chars+",
            )
            assert plan.tag == tag
