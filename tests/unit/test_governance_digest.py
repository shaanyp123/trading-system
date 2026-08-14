"""Unit tests for ``services/discord_shared/governance_digest.py`` (pure builders).

Mirrors the cycle-digest builder coverage: field content, [A05]
string-money passthrough, graceful degradation on garbage payloads,
announce-only shape (no components), naive-now rejection. Stdlib-only
import surface is locked separately in
``tests/unit/test_discord_shared_import_hygiene.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.discord_shared.governance_digest import (
    MONTHLY_REFINEMENT_REMINDER,
    QUARTERLY_GOVERNANCE_CHECKLIST,
    QUARTERLY_REVALIDATION_REMINDER,
    build_monthly_report_embed,
    build_quarterly_report_embed,
)

NOW = datetime(2026, 8, 1, 0, 30, tzinfo=UTC)


def _report_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "period_start_utc": "2026-07-01",
        "period_end_utc": "2026-08-01",
        "trades": {
            "closed_count": 4,
            "winners": 1,
            "losers": 3,
            "realized_pnl_usd": "-53.5185",
            "realized_commission_usd": "3.20",
        },
        "fees": {
            "fill_count": 9,
            "commission_usd": "2.88",
            "exchange_fee_usd": "0.90",
            "total_usd": "3.78",
        },
        "funding_sources": [
            {
                "source": "coinbase_advanced",
                "observation_count": 744,
                "mean_abs_annualized_rate": "0.0850",
            },
            {
                "source": "binance_proxy",
                "observation_count": 93,
                "mean_abs_annualized_rate": "0.0700",
            },
        ],
        "funding_note": "Funding figures are observed-rate telemetry",
        "sweeps": {
            "completed_count": 0,
            "to_yield_usd": "0",
            "to_margin_usd": "0",
            "failed_count": 0,
        },
        "capital_events": [
            {
                "event_type": "deposit",
                "amount_usd": "802.10",
                "threshold_met": True,
                "effective_at_utc": "2026-07-18T15:30:00Z",
            }
        ],
        "server_now": "2026-08-01T00:30:00Z",
    }
    payload.update(overrides)
    return payload


def _field(embed: dict[str, Any], name: str) -> str:
    match = next((f for f in embed["fields"] if f["name"] == name), None)
    assert match is not None, f"field {name!r} missing: {[f['name'] for f in embed['fields']]}"
    return str(match["value"])


class TestMonthlyEmbed:
    def test_field_content(self) -> None:
        embed = build_monthly_report_embed(_report_payload(), now_utc=NOW)
        assert embed["title"] == "Monthly governance report — 2026-07"
        trades = _field(embed, "Trades")
        assert "4 closed (1W/3L)" in trades
        assert "-$53.5185" in trades  # [A05] string passthrough, sign preserved
        fees = _field(embed, "Fees")
        assert "$2.88" in fees and "$3.78" in fees
        funding = _field(embed, "Funding (telemetry)")
        assert "coinbase_advanced" in funding and "8.50%" in funding
        assert "binance_proxy" in funding and "7.00%" in funding
        assert "observed-rate telemetry" in funding  # honesty note rides along
        assert "no sweeps" in _field(embed, "Cash sweeps")
        capital = _field(embed, "Capital events")
        assert "2026-07-18 deposit $802.10 (threshold met)" in capital
        assert _field(embed, "Refinement loop") == MONTHLY_REFINEMENT_REMINDER

    def test_gates_summary_field_optional(self) -> None:
        embed = build_monthly_report_embed(
            _report_payload(), now_utc=NOW, gates_summary="🟢A1 | day 10/45"
        )
        assert _field(embed, "§10 gates") == "🟢A1 | day 10/45"
        without = build_monthly_report_embed(_report_payload(), now_utc=NOW)
        assert all(f["name"] != "§10 gates" for f in without["fields"])

    def test_sweeps_line_with_activity(self) -> None:
        payload = _report_payload(
            sweeps={
                "completed_count": 3,
                "to_yield_usd": "1200.00",
                "to_margin_usd": "400.00",
                "failed_count": 1,
            }
        )
        line = _field(build_monthly_report_embed(payload, now_utc=NOW), "Cash sweeps")
        assert "3 completed" in line
        assert "to_yield $1200.00" in line
        assert "to_margin $400.00" in line
        assert "1 failed" in line

    def test_announce_only_no_components(self) -> None:
        embed = build_monthly_report_embed(_report_payload(), now_utc=NOW)
        assert "components" not in embed

    def test_garbage_payload_never_raises(self) -> None:
        embed = build_monthly_report_embed({}, now_utc=NOW)
        assert _field(embed, "Trades") == "n/a"
        assert _field(embed, "Capital events") == "none"
        assert "no funding observations" in _field(embed, "Funding (telemetry)")

    def test_naive_now_rejected(self) -> None:
        with pytest.raises(ValueError, match="tz-aware"):
            build_monthly_report_embed(_report_payload(), now_utc=datetime(2026, 8, 1))


class TestQuarterlyEmbed:
    def test_adds_revalidation_and_checklist(self) -> None:
        payload = _report_payload(period_start_utc="2026-04-01", period_end_utc="2026-07-01")
        embed = build_quarterly_report_embed(payload, now_utc=NOW, quarter_label="2026-Q2")
        assert embed["title"] == "Quarterly governance report — 2026-Q2"
        assert _field(embed, "§4 quarterly revalidation") == QUARTERLY_REVALIDATION_REMINDER
        checklist = _field(embed, "Governance checklist")
        for item in QUARTERLY_GOVERNANCE_CHECKLIST:
            assert item in checklist
        assert checklist.count("☐") == len(QUARTERLY_GOVERNANCE_CHECKLIST)

    def test_monthly_reminder_not_duplicated_on_quarterly(self) -> None:
        embed = build_quarterly_report_embed(
            _report_payload(), now_utc=NOW, quarter_label="2026-Q2"
        )
        assert all(f["name"] != "Refinement loop" for f in embed["fields"])

    def test_naive_now_rejected(self) -> None:
        with pytest.raises(ValueError, match="tz-aware"):
            build_quarterly_report_embed(
                _report_payload(), now_utc=datetime(2026, 8, 1), quarter_label="2026-Q2"
            )


class TestRenderDefects20260814:
    """Regressions for the two 2026-08-14 monthly-report render defects
    (decisions-log): "$0E-8" scientific-notation passthrough and the
    underscore-italics mangling of snake_case names inside `_…_` spans.
    """

    def test_zero_exchange_fee_renders_plain_not_scientific(self) -> None:
        payload = _report_payload()
        payload["fees"] = dict(payload["fees"], exchange_fee_usd="0E-8")
        fees = _field(build_monthly_report_embed(payload, now_utc=NOW), "Fees")
        assert "$0.00000000" in fees
        assert "0E-8" not in fees

    def test_negative_scientific_notation_keeps_sign_prefix(self) -> None:
        payload = _report_payload()
        payload["trades"] = dict(payload["trades"], realized_pnl_usd="-5E-2")
        trades = _field(build_monthly_report_embed(payload, now_utc=NOW), "Trades")
        assert "-$0.05" in trades

    def test_plain_decimal_money_passthrough_unchanged(self) -> None:
        # [A05]: exact digits preserved — no rounding, no re-scaling.
        fees = _field(build_monthly_report_embed(_report_payload(), now_utc=NOW), "Fees")
        assert "$2.88" in fees and "$0.90" in fees and "$3.78" in fees

    def test_funding_note_underscores_escaped_in_italics(self) -> None:
        payload = _report_payload(funding_note="reconciled via reconcile_statement.py (gate A1)")
        funding = _field(build_monthly_report_embed(payload, now_utc=NOW), "Funding (telemetry)")
        assert "reconcile\\_statement.py" in funding
        assert "_reconciled via" in funding  # still wrapped in italics
        assert "reconcile_statement.py" not in funding  # no unescaped form
