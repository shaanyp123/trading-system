"""Unit tests for services.webhook_pusher.event_pushes (Pivot-PR-E).

Pure-function tests for Discord embed builders + plan_event_push routing.

A22 N/A (no audit_log writes). A06 enforced (every datetime tz-aware UTC).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from services.webhook_pusher.event_pushes import (
    COLOR_ORDER_FILLED,
    COLOR_SIGNAL_APPROVED,
    COLOR_SIGNAL_DEFERRED,
    COLOR_SIGNAL_EMITTED,
    COLOR_SIGNAL_REJECTED,
    EventChannel,
    OrderFilledEvent,
    SignalDecisionEvent,
    SignalEmittedEvent,
    build_order_filled_embed,
    build_signal_decision_embed,
    build_signal_emitted_embed,
    plan_event_push,
)

_TS_UTC = datetime(2026, 5, 12, 21, 30, tzinfo=UTC)


def _signal_emitted() -> SignalEmittedEvent:
    return SignalEmittedEvent(
        signal_id="9d2f7a1c-b54e-83a1-4d9e-7c1b2f0a1234",
        market="/MES",
        direction="long",
        target_contracts=1,
        decision_price=Decimal("5234.50"),
        emitted_at_utc=_TS_UTC,
        environment="paper",
        strategy_version="9d2f7a1",
        anomaly_reasons=(),
    )


def _signal_decision(decision: str = "approved") -> SignalDecisionEvent:
    return SignalDecisionEvent(
        signal_id="9d2f7a1c-b54e-83a1-4d9e-7c1b2f0a1234",
        market="/MES",
        direction="long",
        decision=decision,
        decided_at_utc=_TS_UTC,
        decided_by_user_id="operator",
        environment="paper",
        diary_reasoning_text=None if decision == "approved" else "vol regime elevated",
    )


def _order_filled() -> OrderFilledEvent:
    return OrderFilledEvent(
        order_id="12345",
        client_order_id="9d2f7a1c-b54e83a1-4d9e7c1b2f0a-1",
        signal_id="9d2f7a1c-b54e-83a1-4d9e-7c1b2f0a1234",
        market="/MES",
        side="buy",
        quantity=Decimal("1"),
        fill_price=Decimal("5234.25"),
        commission_usd=Decimal("0.85"),
        filled_at_utc=_TS_UTC,
        environment="paper",
    )


class TestColorConstants:
    def test_colors_distinct(self) -> None:
        colors = {
            COLOR_SIGNAL_EMITTED,
            COLOR_SIGNAL_APPROVED,
            COLOR_SIGNAL_REJECTED,
            COLOR_SIGNAL_DEFERRED,
            COLOR_ORDER_FILLED,
        }
        # APPROVED + ORDER_FILLED intentionally share green; the other 3 are distinct.
        assert len(colors) >= 4

    def test_colors_24bit(self) -> None:
        for color in (
            COLOR_SIGNAL_EMITTED,
            COLOR_SIGNAL_APPROVED,
            COLOR_SIGNAL_REJECTED,
            COLOR_SIGNAL_DEFERRED,
            COLOR_ORDER_FILLED,
        ):
            assert 0 <= color <= 0xFFFFFF


class TestSignalEmittedEmbed:
    def test_basic_shape(self) -> None:
        embed = build_signal_emitted_embed(_signal_emitted())
        assert "LONG signal: /MES" in embed["title"]
        assert embed["color"] == COLOR_SIGNAL_EMITTED
        assert isinstance(embed["fields"], list)
        assert len(embed["fields"]) == 6  # no anomaly → 6 fields

    def test_anomaly_badge(self) -> None:
        event = SignalEmittedEvent(
            signal_id="abc",
            market="/MES",
            direction="long",
            target_contracts=1,
            decision_price=Decimal("5234.50"),
            emitted_at_utc=_TS_UTC,
            environment="paper",
            strategy_version="9d2f7a1",
            anomaly_reasons=("vol_regime_z_high", "capacity_above_alert"),
        )
        embed = build_signal_emitted_embed(event)
        assert "anomaly" in embed["title"].lower()
        # 6 base fields + 1 anomaly field
        assert len(embed["fields"]) == 7
        anomaly_field = embed["fields"][-1]
        assert anomaly_field["name"] == "Anomaly reasons"
        assert "vol_regime_z_high" in anomaly_field["value"]

    def test_naive_timestamp_rejected(self) -> None:
        event = SignalEmittedEvent(
            signal_id="abc",
            market="/MES",
            direction="long",
            target_contracts=1,
            decision_price=Decimal("5234.50"),
            emitted_at_utc=datetime(2026, 5, 12, 21, 30),  # naive
            environment="paper",
            strategy_version="9d2f7a1",
        )
        with pytest.raises(ValueError, match="tz-aware"):
            build_signal_emitted_embed(event)

    def test_footer_contains_signal_id(self) -> None:
        embed = build_signal_emitted_embed(_signal_emitted())
        assert "9d2f7a1c-b54e-83a1-4d9e-7c1b2f0a1234" in embed["footer"]["text"]


class TestSignalDecisionEmbed:
    def test_approved_green(self) -> None:
        embed = build_signal_decision_embed(_signal_decision("approved"))
        assert embed["color"] == COLOR_SIGNAL_APPROVED
        assert "APPROVED" in embed["title"]

    def test_rejected_gray(self) -> None:
        embed = build_signal_decision_embed(_signal_decision("rejected"))
        assert embed["color"] == COLOR_SIGNAL_REJECTED
        assert "REJECTED" in embed["title"]

    def test_deferred_amber(self) -> None:
        embed = build_signal_decision_embed(_signal_decision("deferred"))
        assert embed["color"] == COLOR_SIGNAL_DEFERRED
        assert "DEFERRED" in embed["title"]

    def test_reasoning_field_appears_for_reject(self) -> None:
        embed = build_signal_decision_embed(_signal_decision("rejected"))
        reasoning_fields = [f for f in embed["fields"] if f["name"] == "Reasoning"]
        assert len(reasoning_fields) == 1
        assert "vol regime" in reasoning_fields[0]["value"]

    def test_reasoning_field_omitted_for_approve(self) -> None:
        embed = build_signal_decision_embed(_signal_decision("approved"))
        reasoning_fields = [f for f in embed["fields"] if f["name"] == "Reasoning"]
        assert len(reasoning_fields) == 0

    def test_reasoning_truncated(self) -> None:
        event = SignalDecisionEvent(
            signal_id="abc",
            market="/MES",
            direction="long",
            decision="rejected",
            decided_at_utc=_TS_UTC,
            decided_by_user_id="operator",
            environment="paper",
            diary_reasoning_text="x" * 2000,
        )
        embed = build_signal_decision_embed(event)
        reasoning_value = next(f["value"] for f in embed["fields"] if f["name"] == "Reasoning")
        assert len(reasoning_value) <= 1024

    def test_invalid_decision_raises(self) -> None:
        event = SignalDecisionEvent(
            signal_id="abc",
            market="/MES",
            direction="long",
            decision="ignored",  # not in locked set
            decided_at_utc=_TS_UTC,
            decided_by_user_id="operator",
            environment="paper",
        )
        with pytest.raises(ValueError, match="unsupported decision"):
            build_signal_decision_embed(event)


class TestOrderFilledEmbed:
    def test_basic_shape(self) -> None:
        embed = build_order_filled_embed(_order_filled())
        assert "FILLED" in embed["title"]
        assert "BUY" in embed["title"]
        assert embed["color"] == COLOR_ORDER_FILLED
        # 7 base fields (Side, Qty, Fill Price, Notional, Commission, Env, Filled)
        assert len(embed["fields"]) == 7

    def test_notional_computed(self) -> None:
        embed = build_order_filled_embed(_order_filled())
        notional_field = next(f for f in embed["fields"] if f["name"] == "Notional")
        # qty=1 * fill_price=5234.25 -> $5234.25
        assert notional_field["value"] == "$5234.25"

    def test_footer_contains_all_three_ids(self) -> None:
        embed = build_order_filled_embed(_order_filled())
        footer = embed["footer"]["text"]
        assert "9d2f7a1c-b54e83a1-4d9e7c1b2f0a-1" in footer  # client_order_id
        assert "9d2f7a1c-b54e-83a1-4d9e-7c1b2f0a1234" in footer  # signal_id
        assert "12345" in footer  # broker_order_id


class TestPlanEventPush:
    def test_signal_emitted_routes_to_signals(self) -> None:
        plan = plan_event_push(channel=EventChannel.SIGNALS, event=_signal_emitted())
        assert plan.channel == EventChannel.SIGNALS
        assert plan.dedupe_key.startswith("signal_emitted:")

    def test_signal_decision_routes_to_signals(self) -> None:
        plan = plan_event_push(channel=EventChannel.SIGNALS, event=_signal_decision("approved"))
        assert plan.dedupe_key.startswith("signal_approved:")

    def test_order_filled_routes_to_fills(self) -> None:
        plan = plan_event_push(channel=EventChannel.FILLS, event=_order_filled())
        assert plan.channel == EventChannel.FILLS
        assert plan.dedupe_key.startswith("order_filled:")

    def test_signal_to_fills_channel_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"EventChannel\.SIGNALS"):
            plan_event_push(channel=EventChannel.FILLS, event=_signal_emitted())

    def test_fill_to_signals_channel_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"EventChannel\.FILLS"):
            plan_event_push(channel=EventChannel.SIGNALS, event=_order_filled())


class TestModuleContract:
    def test_public_surface(self) -> None:
        from services.webhook_pusher import event_pushes

        for name in (
            "EventChannel",
            "EventPushPlan",
            "OrderFilledEvent",
            "SignalDecisionEvent",
            "SignalEmittedEvent",
            "build_order_filled_embed",
            "build_signal_decision_embed",
            "build_signal_emitted_embed",
            "plan_event_push",
        ):
            assert hasattr(event_pushes, name), f"missing public export: {name}"
