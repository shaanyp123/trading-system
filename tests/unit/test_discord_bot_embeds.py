"""Unit tests for ``services.discord_bot.embeds`` — pure-function builders.

A22 N/A (no Postgres I/O); A27 N/A here (the live Discord runtime is
exercised at the Day 23 carryover smoke per ``deploy/discord_bot/README.md``).
A06 enforced (every datetime is tz-aware UTC).

Coverage matrix:

  * Color tokens are non-overlapping ints
  * ``_format_et`` round-trips UTC → America/New_York
  * ``_format_et`` rejects naive datetimes
  * ``build_positions_embed`` empty-state + populated-state + title format +
    color + footer
  * ``build_halt_confirm_embed`` shape + reason truncation + warning color
  * ``build_halt_invoke_success_embed`` shape + critical color + audit_event_uuid
    surfaced
  * ``build_halt_invoke_error_embed`` 501 KILL_SWITCH_HANDLER_NOT_WIRED special
    case + generic error case
  * ``format_kill_switch_invoke_failure`` includes status_code + error_code +
    message + details
  * ``build_status_embed`` ok-state (green) + degraded-state (warning) + check
    rendering
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import discord
import pytest

from services.discord_bot.api_client import (
    ApiClientHTTPError,
    HealthCheck,
    HealthResponse,
    PositionRow,
    PositionsResponse,
)
from services.discord_bot.embeds import (
    EMBED_COLOR_CRITICAL,
    EMBED_COLOR_INFO,
    EMBED_COLOR_OK,
    EMBED_COLOR_WARNING,
    _format_et,
    _format_signed_decimal,
    _require_aware_utc,
    build_halt_confirm_embed,
    build_halt_invoke_error_embed,
    build_halt_invoke_success_embed,
    build_positions_embed,
    build_status_embed,
    format_kill_switch_invoke_failure,
)

# ---------------------------------------------------------------------------
# TestColorConstants
# ---------------------------------------------------------------------------


_NOW = datetime(2026, 5, 19, 21, 5, tzinfo=UTC)


class TestColorConstants:
    def test_colors_are_distinct(self) -> None:
        colors = {EMBED_COLOR_OK, EMBED_COLOR_INFO, EMBED_COLOR_WARNING, EMBED_COLOR_CRITICAL}
        assert len(colors) == 4

    def test_colors_fit_in_24_bit(self) -> None:
        for color in (EMBED_COLOR_OK, EMBED_COLOR_INFO, EMBED_COLOR_WARNING, EMBED_COLOR_CRITICAL):
            assert 0 <= color <= 0xFFFFFF


# ---------------------------------------------------------------------------
# TestFormatHelpers
# ---------------------------------------------------------------------------


class TestFormatHelpers:
    def test_format_et_renders_short(self) -> None:
        # 2026-05-15 12:30 UTC = 08:30 ET (DST in May)
        dt = datetime(2026, 5, 15, 12, 30, tzinfo=UTC)
        assert _format_et(dt) == "08:30 ET"

    def test_format_et_renders_with_date(self) -> None:
        dt = datetime(2026, 5, 15, 12, 30, tzinfo=UTC)
        assert _format_et(dt, with_date=True) == "2026-05-15 08:30 ET"

    def test_format_et_rejects_naive(self) -> None:
        dt = datetime(2026, 5, 15, 12, 30)
        with pytest.raises(ValueError, match="must be tz-aware UTC"):
            _format_et(dt)

    def test_require_aware_utc_passes_aware(self) -> None:
        dt = datetime(2026, 1, 1, tzinfo=UTC)
        assert _require_aware_utc(dt, field="x") is dt

    def test_require_aware_utc_rejects_naive(self) -> None:
        dt = datetime(2026, 1, 1)
        with pytest.raises(ValueError, match="my_field"):
            _require_aware_utc(dt, field="my_field")

    def test_format_signed_decimal_positive(self) -> None:
        assert _format_signed_decimal(Decimal("1.25")) == "+1.25"

    def test_format_signed_decimal_zero(self) -> None:
        assert _format_signed_decimal(Decimal("0")) == "+0"

    def test_format_signed_decimal_negative(self) -> None:
        assert _format_signed_decimal(Decimal("-3.40")) == "-3.40"


# ---------------------------------------------------------------------------
# TestPositionsEmbed
# ---------------------------------------------------------------------------


def _empty_positions(as_of: datetime) -> PositionsResponse:
    return PositionsResponse(positions=[], as_of=as_of)


def _populated_positions(as_of: datetime) -> PositionsResponse:
    rows = [
        PositionRow(
            instrument_id="MES-2026-06",
            symbol="/MES",
            contract_month="JUN",
            qty=1,
            avg_entry_price=Decimal("5230.00"),
            current_price=Decimal("5237.50"),
            unrealized_pnl=Decimal("37.50"),
            unrealized_pnl_pct_of_nav=Decimal("0.0007"),
            cluster="equity_index",
            managed_by_strategy_version="v1-trend",
        ),
        PositionRow(
            instrument_id="ZN-2026-06",
            symbol="/ZN",
            contract_month="JUN",
            qty=-2,
            avg_entry_price=Decimal("112.50"),
            current_price=Decimal("112.05"),
            unrealized_pnl=Decimal("45.00"),
            unrealized_pnl_pct_of_nav=Decimal("0.0008"),
            cluster="rates_bonds",
            managed_by_strategy_version="v1-trend",
        ),
    ]
    return PositionsResponse(positions=rows, as_of=as_of)


class TestPositionsEmbed:
    AS_OF = datetime(2026, 5, 15, 21, 35, tzinfo=UTC)  # 17:35 ET

    def test_empty_state_title_format(self) -> None:
        embed = build_positions_embed(_empty_positions(self.AS_OF), environment="paper")
        assert embed.title == "Open positions (0) — paper · 17:35 ET"

    def test_empty_state_color_is_info_blue(self) -> None:
        embed = build_positions_embed(_empty_positions(self.AS_OF), environment="paper")
        assert embed.color is not None
        assert embed.color.value == EMBED_COLOR_INFO

    def test_empty_state_description_says_no_positions(self) -> None:
        embed = build_positions_embed(_empty_positions(self.AS_OF), environment="paper")
        assert embed.description is not None
        assert "No open positions" in embed.description

    def test_empty_state_footer_includes_environment(self) -> None:
        embed = build_positions_embed(_empty_positions(self.AS_OF), environment="paper")
        assert embed.footer.text is not None
        # N2 — env=PAPER tag per cutover plan §9 line 360 for cross-env
        # disambiguation (Discord channels shared paper+live per L11).
        assert "env=PAPER" in embed.footer.text

    def test_populated_state_title_count(self) -> None:
        embed = build_positions_embed(_populated_positions(self.AS_OF), environment="paper")
        assert "(2)" in (embed.title or "")

    def test_populated_state_renders_each_symbol(self) -> None:
        embed = build_positions_embed(_populated_positions(self.AS_OF), environment="paper")
        assert embed.description is not None
        assert "/MES" in embed.description
        assert "/ZN" in embed.description

    def test_populated_state_uses_code_block(self) -> None:
        embed = build_positions_embed(_populated_positions(self.AS_OF), environment="paper")
        assert embed.description is not None
        assert embed.description.startswith("```")
        assert embed.description.endswith("```")

    def test_populated_state_signed_unrealized_pnl(self) -> None:
        embed = build_positions_embed(_populated_positions(self.AS_OF), environment="paper")
        assert embed.description is not None
        # Both rows have positive uPnL → both should show "+" prefix
        assert "+37.50" in embed.description
        assert "+45.00" in embed.description

    def test_environment_propagates_to_title(self) -> None:
        embed = build_positions_embed(_empty_positions(self.AS_OF), environment="live-small")
        assert "live-small" in (embed.title or "")


# ---------------------------------------------------------------------------
# TestHaltConfirmEmbed
# ---------------------------------------------------------------------------


class TestHaltConfirmEmbed:
    def test_title_includes_reason(self) -> None:
        embed = build_halt_confirm_embed(reason="regime change", environment="paper")
        assert "regime change" in (embed.title or "")

    def test_warning_color(self) -> None:
        embed = build_halt_confirm_embed(reason="x", environment="paper")
        assert embed.color is not None
        assert embed.color.value == EMBED_COLOR_WARNING

    def test_long_reason_is_truncated(self) -> None:
        long_reason = "x" * 250
        embed = build_halt_confirm_embed(reason=long_reason, environment="paper")
        # Title truncates at 100 chars + "..."
        title = embed.title or ""
        assert "..." in title
        # Body still contains the FULL reason in the dedicated field
        full_in_body = any(long_reason in (f.value or "") for f in embed.fields)
        assert full_in_body

    def test_body_documents_side_effects(self) -> None:
        embed = build_halt_confirm_embed(reason="x", environment="paper")
        body = embed.description or ""
        assert "Cancel pending working orders" in body
        assert "RESUME requires" in body
        assert "WebAuthn" in body

    def test_trigger_field_is_manual_judgment(self) -> None:
        embed = build_halt_confirm_embed(reason="x", environment="paper")
        trigger_field = next(f for f in embed.fields if f.name == "Trigger")
        assert "manual_judgment" in (trigger_field.value or "")

    def test_footer_includes_environment(self) -> None:
        embed = build_halt_confirm_embed(reason="x", environment="paper")
        assert embed.footer.text is not None
        assert "env=PAPER" in embed.footer.text


# ---------------------------------------------------------------------------
# TestHaltInvokeSuccessEmbed
# ---------------------------------------------------------------------------


class TestHaltInvokeSuccessEmbed:
    def test_critical_color(self) -> None:
        embed = build_halt_invoke_success_embed(
            environment="paper",
            audit_event_uuid="uuid-1",
            new_state="HALT_NEW",
            severity="incident_review",
        )
        assert embed.color is not None
        assert embed.color.value == EMBED_COLOR_CRITICAL

    def test_audit_event_uuid_in_body(self) -> None:
        embed = build_halt_invoke_success_embed(
            environment="paper",
            audit_event_uuid="abc-123",
            new_state="HALT_NEW",
            severity="routine",
        )
        assert "abc-123" in (embed.description or "")

    def test_new_state_in_title(self) -> None:
        embed = build_halt_invoke_success_embed(
            environment="paper",
            audit_event_uuid="x",
            new_state="HALT_NEW",
            severity=None,
        )
        assert "HALT_NEW" in (embed.title or "")

    def test_severity_defaults_to_routine_when_none(self) -> None:
        embed = build_halt_invoke_success_embed(
            environment="paper",
            audit_event_uuid="x",
            new_state="HALT_NEW",
            severity=None,
        )
        assert "routine" in (embed.description or "")

    def test_resume_field_documents_web_only_path(self) -> None:
        embed = build_halt_invoke_success_embed(
            environment="paper",
            audit_event_uuid="x",
            new_state="HALT_NEW",
            severity="routine",
        )
        resume_field = next(f for f in embed.fields if f.name == "Resume")
        assert "WebAuthn" in (resume_field.value or "")
        assert "web app" in (resume_field.value or "")


# ---------------------------------------------------------------------------
# TestHaltInvokeErrorEmbed
# ---------------------------------------------------------------------------


class TestHaltInvokeErrorEmbed:
    def test_special_case_kill_switch_handler_not_wired(self) -> None:
        error = ApiClientHTTPError(
            status_code=501,
            error_code="KILL_SWITCH_HANDLER_NOT_WIRED",
            message="Kill-switch handler stub.",
        )
        embed = build_halt_invoke_error_embed(environment="paper", error=error)
        assert embed.color is not None
        # Special case renders WARNING (amber) not CRITICAL (red) — it's
        # a known dispatcher gap, not a runtime failure.
        assert embed.color.value == EMBED_COLOR_WARNING
        body = embed.description or ""
        assert "Week 4 Wed dispatcher" in body
        assert "Operator action" in body

    def test_generic_error_renders_critical(self) -> None:
        error = ApiClientHTTPError(
            status_code=500,
            error_code="INTERNAL_ERROR",
            message="boom",
        )
        embed = build_halt_invoke_error_embed(environment="paper", error=error)
        assert embed.color is not None
        assert embed.color.value == EMBED_COLOR_CRITICAL
        title = embed.title or ""
        assert "INTERNAL_ERROR" in title
        body = embed.description or ""
        assert "boom" in body
        assert "HTTP 500" in body

    def test_error_with_details_renders_them(self) -> None:
        error = ApiClientHTTPError(
            status_code=429,
            error_code="RATE_LIMITED",
            message="Too many.",
            details={"bucket": "general", "limit": 100, "retry_after_s": 5},
        )
        body = format_kill_switch_invoke_failure(error)
        assert "Details:" in body
        assert "bucket" in body
        assert "general" in body
        assert "100" in body

    def test_error_without_details_omits_section(self) -> None:
        error = ApiClientHTTPError(
            status_code=401,
            error_code="UNAUTHORIZED",
            message="nope",
        )
        body = format_kill_switch_invoke_failure(error)
        assert "Details:" not in body


# ---------------------------------------------------------------------------
# TestStatusEmbed
# ---------------------------------------------------------------------------


def _ok_health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        environment="paper",
        version="abc1234",
        db_connected=True,
        checks=[HealthCheck(name="postgres", ok=True, latency_ms=4.21)],
    )


def _degraded_health() -> HealthResponse:
    return HealthResponse(
        status="degraded",
        environment="paper",
        version="abc1234",
        db_connected=False,
        checks=[
            HealthCheck(
                name="postgres",
                ok=False,
                latency_ms=None,
                detail="connection refused",
            ),
        ],
    )


class TestStatusEmbed:
    NOW = datetime(2026, 5, 15, 21, 35, tzinfo=UTC)  # 17:35 ET

    def test_ok_status_renders_green(self) -> None:
        embed = build_status_embed(_ok_health(), now=self.NOW)
        assert embed.color is not None
        assert embed.color.value == EMBED_COLOR_OK

    def test_degraded_status_renders_warning(self) -> None:
        embed = build_status_embed(_degraded_health(), now=self.NOW)
        assert embed.color is not None
        assert embed.color.value == EMBED_COLOR_WARNING

    def test_title_includes_environment_and_time(self) -> None:
        embed = build_status_embed(_ok_health(), now=self.NOW)
        title = embed.title or ""
        assert "paper" in title
        assert "17:35 ET" in title

    def test_status_field(self) -> None:
        embed = build_status_embed(_ok_health(), now=self.NOW)
        status_field = next(f for f in embed.fields if f.name == "Status")
        assert "ok" in (status_field.value or "")

    def test_version_field(self) -> None:
        embed = build_status_embed(_ok_health(), now=self.NOW)
        version_field = next(f for f in embed.fields if f.name == "Version")
        assert "abc1234" in (version_field.value or "")

    def test_db_connected_field_true(self) -> None:
        embed = build_status_embed(_ok_health(), now=self.NOW)
        db_field = next(f for f in embed.fields if f.name == "DB connected")
        assert "true" in (db_field.value or "").lower()

    def test_db_connected_field_false_on_degraded(self) -> None:
        embed = build_status_embed(_degraded_health(), now=self.NOW)
        db_field = next(f for f in embed.fields if f.name == "DB connected")
        assert "false" in (db_field.value or "").lower()

    def test_checks_render_with_glyphs(self) -> None:
        embed = build_status_embed(_ok_health(), now=self.NOW)
        checks_field = next(f for f in embed.fields if f.name == "Checks")
        body = checks_field.value or ""
        assert "✅" in body
        assert "postgres" in body
        assert "4.21" in body

    def test_checks_render_failure_glyph_and_detail(self) -> None:
        embed = build_status_embed(_degraded_health(), now=self.NOW)
        checks_field = next(f for f in embed.fields if f.name == "Checks")
        body = checks_field.value or ""
        assert "❌" in body
        assert "connection refused" in body

    def test_returns_discord_embed(self) -> None:
        embed = build_status_embed(_ok_health(), now=self.NOW)
        assert isinstance(embed, discord.Embed)


# ---------------------------------------------------------------------------
# env_tag — N2 per cutover plan §9 line 360
# ---------------------------------------------------------------------------


class TestEnvTag:
    """``env_tag()`` produces the cross-env disambiguator chip rendered in
    every embed footer (cutover plan §9 line 360 + L11 — Discord channels
    are shared between paper + live; the tag is the operator's primary
    visual disambiguator)."""

    def test_paper_maps_to_env_PAPER(self) -> None:
        from services.discord_bot.embeds import env_tag

        assert env_tag("paper") == "env=PAPER"

    def test_live_small_maps_to_env_LIVE(self) -> None:
        from services.discord_bot.embeds import env_tag

        assert env_tag("live-small") == "env=LIVE"

    def test_live_scale_maps_to_env_LIVE(self) -> None:
        """Both live tiers (live-small Day-1 cutover + live-scale post-$100k)
        map to the same `LIVE` visual cue — operator decision is binary at
        the embed layer; granular tier info stays in api logs + audit chain."""
        from services.discord_bot.embeds import env_tag

        assert env_tag("live-scale") == "env=LIVE"

    def test_dev_falls_through_to_uppercased(self) -> None:
        """`dev` is not a production tier; uppercase fallback ensures the
        operator never sees a paper/live mismatch but does see SOMETHING
        meaningful when running against dev locally."""
        from services.discord_bot.embeds import env_tag

        assert env_tag("dev") == "env=DEV"

    def test_empty_string_renders_empty_tag(self) -> None:
        from services.discord_bot.embeds import env_tag

        assert env_tag("") == "env="

    def test_whitespace_normalized(self) -> None:
        from services.discord_bot.embeds import env_tag

        assert env_tag("  paper  ") == "env=PAPER"
        assert env_tag("  live-small  ") == "env=LIVE"
