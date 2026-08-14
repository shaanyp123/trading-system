"""Unit tests for the §10 gate-status Discord renderers.

* ``services.discord_shared.gates_digest`` — the ``/gates`` embed +
  the one-line nightly-digest summary (pure, stdlib-only)
* the nightly planner passthrough:
  ``plan_cycle_digest_push(..., gates_payload=...)`` appends the "§10
  gates" field; absent/None payload leaves the digest unchanged
* graceful degradation: garbage payloads render, never raise
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.discord_shared.cycle_digest import build_cycle_digest_embed
from services.discord_shared.gates_digest import (
    build_gates_embed,
    format_gates_summary_line,
)
from services.webhook_pusher.cycle_digest import plan_cycle_digest_push

NOW = datetime(2026, 7, 18, 0, 10, tzinfo=UTC)


def _gates_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "gates": [
            {
                "key": "A1",
                "title": "Consecutive complete recorded fills",
                "status": "green",
                "value": "18 consecutive",
                "target": ">= 15",
                "evidence_note": "statement match is operator-run",
            },
            {
                "key": "A4",
                "title": "Clean-day streak",
                "status": "red",
                "value": "0 consecutive",
                "target": ">= 30 days",
                "evidence_note": None,
            },
            {
                "key": "B3",
                "title": "CDE vs proxy funding",
                "status": "insufficient_data",
                "value": "no proxy-source rows recorded",
                "target": "within 2x",
                "evidence_note": None,
            },
        ],
        "days_live": 9,
        "phase_a_min_days": 45,
        "all_mechanical_green": False,
        "scale_up_eligible": False,
        "server_now": "2026-07-18T00:10:00Z",
    }
    payload.update(overrides)
    return payload


def _cycle_payload() -> dict[str, Any]:
    return {
        "decision": {
            "decision_date": "2026-07-18",
            "decided_at_utc": "2026-07-18T00:05:12Z",
            "status": "completed",
            "env": "paper",
            "equity_usd": "2251.38",
            "weekly_halved": False,
            "m_combined": "1.0",
            "skip_reason": None,
            "assets": [],
        },
        "worker": None,
        "next_friday_close_utc": "2026-07-24T21:00:00Z",
        "server_now": "2026-07-18T00:10:00Z",
    }


class TestSummaryLine:
    def test_chips_counts_and_days(self) -> None:
        line = format_gates_summary_line(_gates_payload())
        assert "🟢A1" in line
        assert "🔴A4" in line
        assert "⚪B3" in line
        assert "1 green · 1 red · 1 pending" in line
        assert "day 9/45" in line
        assert "SCALE-UP ELIGIBLE" not in line

    def test_eligible_flag_rendered(self) -> None:
        line = format_gates_summary_line(_gates_payload(scale_up_eligible=True))
        assert "SCALE-UP ELIGIBLE (mechanical)" in line

    def test_garbage_payload_degrades(self) -> None:
        assert format_gates_summary_line({}) == "gate status unavailable"
        assert format_gates_summary_line({"gates": "nope"}) == "gate status unavailable"


class TestGatesEmbed:
    def test_fields_and_evidence_notes(self) -> None:
        embed = build_gates_embed(_gates_payload(), now_utc=NOW)
        assert "§10 acceptance gates" in embed["title"]
        names = [f["name"] for f in embed["fields"]]
        assert any(name.startswith("🟢 A1") for name in names)
        assert any(name.startswith("🔴 A4") for name in names)
        a1_field = next(f for f in embed["fields"] if "A1" in f["name"])
        assert "operator-run" in a1_field["value"]
        assert "day 9 of the 45-day" in embed["description"]

    def test_no_components_announce_only(self) -> None:
        embed = build_gates_embed(_gates_payload(), now_utc=NOW)
        assert "components" not in embed

    def test_empty_payload_renders_no_data(self) -> None:
        embed = build_gates_embed({}, now_utc=NOW)
        assert "no data" in embed["title"]

    def test_naive_now_rejected(self) -> None:
        with pytest.raises(ValueError, match="tz-aware"):
            build_gates_embed(_gates_payload(), now_utc=datetime(2026, 7, 18))


class TestNightlyDigestGatesField:
    def test_gates_payload_appends_field(self) -> None:
        plan = plan_cycle_digest_push(_cycle_payload(), now_utc=NOW, gates_payload=_gates_payload())
        assert plan is not None
        field_names = [f["name"] for f in plan.embed["fields"]]
        assert "§10 gates" in field_names
        gates_field = next(f for f in plan.embed["fields"] if f["name"] == "§10 gates")
        assert "🟢A1" in gates_field["value"]

    def test_missing_gates_payload_leaves_digest_unchanged(self) -> None:
        plan = plan_cycle_digest_push(_cycle_payload(), now_utc=NOW, gates_payload=None)
        assert plan is not None
        field_names = [f["name"] for f in plan.embed["fields"]]
        assert "§10 gates" not in field_names

    def test_direct_builder_accepts_summary(self) -> None:
        embed = build_cycle_digest_embed(
            _cycle_payload(), now_utc=NOW, gates_summary="🟢A1 | 1 green"
        )
        gates_field = next(f for f in embed["fields"] if f["name"] == "§10 gates")
        assert gates_field["value"] == "🟢A1 | 1 green"


class TestEvidenceNoteUnderscoreEscape:
    """Regression for the 2026-08-14 /gates render defect: snake_case
    names inside the `_…_` evidence-note italics broke the span
    ("reconcile*statement*.py"). Underscores must arrive escaped.
    """

    def test_note_underscores_escaped(self) -> None:
        payload = _gates_payload()
        payload["gates"][0]["evidence_note"] = (
            "operator-run: scripts/operator_tools/reconcile_statement.py"
        )
        embed = build_gates_embed(payload, now_utc=NOW)
        a1_field = next(f for f in embed["fields"] if "A1" in f["name"])
        assert "reconcile\\_statement.py" in a1_field["value"]
        assert "reconcile_statement.py" not in a1_field["value"]
