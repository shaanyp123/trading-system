"""Unit tests for ``services/scheduler/calendar_import.py`` policy.

Coverage:
- Tier classification: tier-1 / tier-2 / tier-3 examples
- ``tier1_label`` canonical labels
- Deduplication: same event from multiple sources collapses, FF wins over TE
- Ratification check: tier-1 unratified for target date returned
- Audit event payload builders use the locked taxonomy names
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from services.scheduler.calendar_import import (
    EventTier,
    ImportSource,
    MacroEvent,
    build_imported_event,
    build_ratified_event,
    build_service_outage_event,
    build_unratified_kill_switch_event,
    check_ratification_for_date,
    classify_tier,
    dedupe_events,
    tier1_label,
)


def _evt(
    name: str,
    *,
    source: ImportSource = ImportSource.FOREX_FACTORY,
    when: datetime | None = None,
    region: str | None = "US",
    ratified: bool = False,
) -> MacroEvent:
    return MacroEvent(
        source=source,
        event_name=name,
        scheduled_at_utc=when or datetime(2026, 5, 9, 12, 30, tzinfo=UTC),
        region=region,
        currency="USD",
        importance="high",
        tier1_classification=tier1_label(name),
        ratified_at_utc=datetime(2026, 5, 9, 23, 0, tzinfo=UTC) if ratified else None,
        ratified_by="operator_web" if ratified else None,
    )


# ---------------------------------------------------------------------------
# Tier classification
# ---------------------------------------------------------------------------


class TestClassifyTier:
    def test_nfp_classifies_as_tier_1(self) -> None:
        assert classify_tier("Non-Farm Payrolls") == EventTier.TIER_1
        assert classify_tier("NFP") == EventTier.TIER_1
        assert classify_tier("US Non Farm Payrolls (Apr)") == EventTier.TIER_1

    def test_cpi_classifies_as_tier_1(self) -> None:
        assert classify_tier("Consumer Price Index") == EventTier.TIER_1
        assert classify_tier("CPI YoY") == EventTier.TIER_1

    def test_fomc_classifies_as_tier_1(self) -> None:
        assert classify_tier("FOMC Statement") == EventTier.TIER_1
        assert classify_tier("Federal Open Market Committee Press Conference") == EventTier.TIER_1

    def test_fed_chair_classifies_as_tier_1(self) -> None:
        assert classify_tier("Fed Chair Powell Speaks") == EventTier.TIER_1

    def test_pmi_classifies_as_tier_2(self) -> None:
        assert classify_tier("ISM Manufacturing PMI") == EventTier.TIER_2
        assert classify_tier("Purchasing Managers Index") == EventTier.TIER_2

    def test_retail_sales_classifies_as_tier_2(self) -> None:
        assert classify_tier("Retail Sales MoM") == EventTier.TIER_2

    def test_unknown_classifies_as_tier_3(self) -> None:
        assert classify_tier("Some random event") == EventTier.TIER_3
        assert classify_tier("") == EventTier.TIER_3

    def test_tier_1_overrides_tier_2_when_both_match(self) -> None:
        # "FOMC GDP discussion" hits both NFP/FOMC and GDP patterns; tier-1 wins.
        assert classify_tier("FOMC discussion of GDP") == EventTier.TIER_1


class TestTier1Label:
    def test_label_recovers_canonical_string(self) -> None:
        assert tier1_label("Non-Farm Payrolls") == "NFP"
        assert tier1_label("CPI YoY") == "CPI"
        assert tier1_label("PPI MoM") == "PPI"
        assert tier1_label("FOMC Statement") == "FOMC"
        assert tier1_label("Fed Chair Powell Speaks") == "FED_CHAIR_SPEAK"

    def test_label_returns_none_for_non_tier_1(self) -> None:
        assert tier1_label("ISM Manufacturing PMI") is None
        assert tier1_label("Random") is None


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


class TestDedupeEvents:
    def test_two_sources_same_event_collapses_to_one(self) -> None:
        ts = datetime(2026, 5, 9, 12, 30, tzinfo=UTC)
        ff = _evt("CPI YoY", source=ImportSource.FOREX_FACTORY, when=ts)
        te = _evt("CPI YoY", source=ImportSource.TRADING_ECONOMICS, when=ts)
        deduped = dedupe_events([ff, te])
        assert len(deduped) == 1

    def test_forex_factory_wins_over_trading_economics(self) -> None:
        ts = datetime(2026, 5, 9, 12, 30, tzinfo=UTC)
        ff = _evt("CPI YoY", source=ImportSource.FOREX_FACTORY, when=ts)
        te = _evt("CPI YoY", source=ImportSource.TRADING_ECONOMICS, when=ts)
        deduped = dedupe_events([te, ff])  # input order should not matter
        assert deduped[0].source == ImportSource.FOREX_FACTORY

    def test_manual_wins_over_forex_factory(self) -> None:
        ts = datetime(2026, 5, 9, 12, 30, tzinfo=UTC)
        manual = _evt("CPI YoY", source=ImportSource.MANUAL, when=ts)
        ff = _evt("CPI YoY", source=ImportSource.FOREX_FACTORY, when=ts)
        deduped = dedupe_events([ff, manual])
        assert deduped[0].source == ImportSource.MANUAL

    def test_different_times_kept_separate(self) -> None:
        ts1 = datetime(2026, 5, 9, 12, 30, tzinfo=UTC)
        ts2 = datetime(2026, 5, 9, 13, 30, tzinfo=UTC)
        a = _evt("CPI YoY", when=ts1)
        b = _evt("CPI YoY", when=ts2)
        deduped = dedupe_events([a, b])
        assert len(deduped) == 2

    def test_output_sorted_by_time(self) -> None:
        ts1 = datetime(2026, 5, 9, 8, 30, tzinfo=UTC)
        ts2 = datetime(2026, 5, 9, 12, 30, tzinfo=UTC)
        ts3 = datetime(2026, 5, 9, 14, 30, tzinfo=UTC)
        events = [_evt("Event B", when=ts2), _evt("Event A", when=ts1), _evt("Event C", when=ts3)]
        deduped = dedupe_events(events)
        assert [e.scheduled_at_utc for e in deduped] == [ts1, ts2, ts3]

    def test_dedupe_case_insensitive_event_name(self) -> None:
        ts = datetime(2026, 5, 9, 12, 30, tzinfo=UTC)
        a = _evt("CPI YoY", source=ImportSource.FOREX_FACTORY, when=ts)
        b = _evt("cpi yoy", source=ImportSource.TRADING_ECONOMICS, when=ts)
        deduped = dedupe_events([a, b])
        assert len(deduped) == 1


# ---------------------------------------------------------------------------
# Ratification check
# ---------------------------------------------------------------------------


class TestRatificationCheck:
    def test_unratified_tier_1_event_returned(self) -> None:
        target = date(2026, 5, 9)
        ev = _evt("CPI YoY", when=datetime(2026, 5, 9, 12, 30, tzinfo=UTC), ratified=False)
        result = check_ratification_for_date([ev], target)
        assert result == [ev]

    def test_ratified_tier_1_event_excluded(self) -> None:
        target = date(2026, 5, 9)
        ev = _evt("CPI YoY", when=datetime(2026, 5, 9, 12, 30, tzinfo=UTC), ratified=True)
        result = check_ratification_for_date([ev], target)
        assert result == []

    def test_tier_2_unratified_event_excluded(self) -> None:
        """Only tier-1 events trigger the kill switch on unratified state."""
        target = date(2026, 5, 9)
        ev = _evt("PMI", when=datetime(2026, 5, 9, 12, 30, tzinfo=UTC), ratified=False)
        result = check_ratification_for_date([ev], target)
        assert result == []

    def test_event_for_different_date_excluded(self) -> None:
        target = date(2026, 5, 9)
        ev = _evt("CPI YoY", when=datetime(2026, 5, 10, 12, 30, tzinfo=UTC), ratified=False)
        result = check_ratification_for_date([ev], target)
        assert result == []

    def test_multiple_unratified_returned_sorted(self) -> None:
        target = date(2026, 5, 9)
        late = _evt("FOMC", when=datetime(2026, 5, 9, 14, 0, tzinfo=UTC), ratified=False)
        early = _evt("CPI", when=datetime(2026, 5, 9, 8, 30, tzinfo=UTC), ratified=False)
        result = check_ratification_for_date([late, early], target)
        assert [e.scheduled_at_utc for e in result] == [
            datetime(2026, 5, 9, 8, 30, tzinfo=UTC),
            datetime(2026, 5, 9, 14, 0, tzinfo=UTC),
        ]


# ---------------------------------------------------------------------------
# Audit event builders
# ---------------------------------------------------------------------------


class TestAuditEventBuilders:
    def test_imported_event_uses_canonical_taxonomy(self) -> None:
        ev = build_imported_event(
            macro_event_id="me_001",
            source=ImportSource.FOREX_FACTORY,
            event_name="CPI YoY",
            scheduled_at_utc=datetime(2026, 5, 9, 12, 30, tzinfo=UTC),
            tier=EventTier.TIER_1,
            imported_at_utc=datetime(2026, 5, 9, 22, 0, tzinfo=UTC),
        )
        assert ev.event_type == "calendar_imported"
        assert ev.payload["tier"] == "tier_1"

    def test_ratified_event_uses_canonical_taxonomy(self) -> None:
        ev = build_ratified_event(
            macro_event_id="me_001",
            ratified_by="operator_web",
            ratified_at_utc=datetime(2026, 5, 9, 22, 30, tzinfo=UTC),
        )
        assert ev.event_type == "calendar_ratified"
        assert ev.payload["ratified_by"] == "operator_web"

    def test_unratified_kill_switch_event_lists_ids(self) -> None:
        ev = build_unratified_kill_switch_event(
            target_date=date(2026, 5, 10),
            unratified_macro_event_ids=["me_001", "me_002"],
            cutoff_at_utc=datetime(2026, 5, 9, 23, 0, tzinfo=UTC),
        )
        assert ev.event_type == "calendar_unratified"
        assert ev.payload["unratified_macro_event_ids"] == ["me_001", "me_002"]
        assert ev.payload["target_date"] == "2026-05-10"

    def test_service_outage_event_carries_error_metadata(self) -> None:
        ev = build_service_outage_event(
            source=ImportSource.FOREX_FACTORY,
            error_class="HTTPError",
            error_message="503 Service Unavailable",
            detected_at_utc=datetime(2026, 5, 9, 22, 0, tzinfo=UTC),
        )
        assert ev.event_type == "calendar_service_outage"
        assert ev.payload["source"] == "forex_factory"
        assert ev.payload["error_class"] == "HTTPError"
