"""services/discord_bot/embeds.py — pure-function embed builders.

All builders take typed data + return a ``discord.Embed``. No I/O. No
side effects. The bot's command handlers compose: api call → embed
builder → ``interaction.response.send_message(embed=embed, ephemeral=True)``.

Embed shape conventions (per frontend-spec §6.3 + Discord embed limits):

  * Title format: ``"<Surface> — <env> · <HH:MM ET>"`` per spec §6.3
    examples (e.g. ``"Open positions (5) — paper · 17:35 ET"``).
  * Color: see ``EMBED_COLOR_*`` constants below; mirrors severity
    convention from spec §6.2 ``#alerts`` color usage.
  * Footer: bot identity + audit env tag — operator-visible at-a-glance
    "which environment am I looking at" cue.
  * Discord field-value limit is 1024 chars; field-name limit 256;
    description limit 4096; total embed payload limit 6000. Phase-0
    payloads are tiny (<300 chars typical) — limits aren't a Day-23
    concern but are documented for the Phase-1 fan-out paths that may
    push longer alert text into ``#critical``.

Time formatting: rendered in ``America/New_York`` per spec §6.3 examples
(``17:35 ET``). The api returns ``server_now`` UTC; the bot converts via
``zoneinfo.ZoneInfo("America/New_York")`` (stdlib; no extra dep needed).

A06 enforced: every datetime input is validated tz-aware UTC before
formatting (``raise ValueError`` on naive). The bot never emits a
non-tz-aware timestamp.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import discord

from services.discord_bot.api_client import (
    ApiClientHTTPError,
    BarSyncStatusResponse,
    HealthResponse,
    PositionsResponse,
    SignalApproveResponse,
)

# ---------------------------------------------------------------------------
# Color tokens (Discord embeds use 24-bit RGB ints)
# ---------------------------------------------------------------------------

#: Healthy / OK / no-action-needed — matches spec §6.2 #alerts P0/P1/P2
#: convention's "success" green.
EMBED_COLOR_OK = 0x10B981

#: Informational / neutral — used for /positions snapshot embeds.
EMBED_COLOR_INFO = 0x3B82F6

#: Warning — degraded health, soft errors. Spec §6.3 doesn't enumerate but
#: this matches the frontend §9.1 P1 amber.
EMBED_COLOR_WARNING = 0xF59E0B

#: Critical — kill-switch fired, hash-chain break, P0 surface. Mirrors
#: frontend §9.1 P0 red.
EMBED_COLOR_CRITICAL = 0xEF4444


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


_ET = ZoneInfo("America/New_York")


def _require_aware_utc(value: datetime, *, field: str) -> datetime:
    """Reject naive datetimes per dev-guide A06.

    The api guarantees tz-aware UTC on every timestamp it serializes; this
    guard catches bot-side bugs where someone constructs a naive datetime
    in a fixture or in glue code.
    """
    if value.tzinfo is None:
        raise ValueError(f"{field} must be tz-aware UTC; got naive datetime")
    return value


def _format_et(value: datetime, *, with_date: bool = False) -> str:
    """Render a UTC datetime in America/New_York time per spec §6.3 examples.

    With ``with_date=False`` returns ``"HH:MM ET"`` (the per-command title
    convention). With ``with_date=True`` returns ``"YYYY-MM-DD HH:MM ET"``
    (used inside multi-line bodies where context is needed).
    """
    aware = _require_aware_utc(value, field="datetime")
    local = aware.astimezone(_ET)
    fmt = "%Y-%m-%d %H:%M ET" if with_date else "%H:%M ET"
    return local.strftime(fmt)


def _env_pill(environment: str) -> str:
    """Format the env tag for the title prefix per spec §6.3.

    Examples in the spec use ``[paper-NORMAL · v9d2f7a1]``; the Phase-0
    bot doesn't have access to ``risk_state`` from every command (only
    ``/status`` does), so the simpler ``paper`` form is used in titles.
    The full ``[env-state · version]`` form is a Phase-1 enhancement
    when the bot also keeps a local cache of ``risk_state`` from SSE
    subscriptions.
    """
    return environment


def env_tag(environment: str) -> str:
    """Format the env identifier for the footer chip per cutover plan §9
    line 360 (``env=PAPER`` / ``env=LIVE``).

    Map:
      * ``"paper"`` → ``"env=PAPER"``
      * ``"live-small"`` → ``"env=LIVE"``
      * ``"live-scale"`` → ``"env=LIVE"``
      * other (e.g., ``"dev"``) → ``"env=<UPPERCASED>"``

    Both live tiers (live-small for the Day-1 $25k cutover; live-scale
    post-$100k tier graduation per cutover plan §2) map to ``LIVE`` so
    the operator's at-a-glance visual cue is binary: PAPER vs LIVE.
    The granular tier info stays in the api logs + audit chain.

    Per cutover plan L11, Discord channels (signals, fills, alerts,
    critical, ops, audit) are SHARED between paper + live; this tag is
    the operator's primary disambiguator at the bot embed layer.
    """
    normalized = (environment or "").strip().lower()
    if normalized in {"live-small", "live-scale"}:
        return "env=LIVE"
    if normalized == "paper":
        return "env=PAPER"
    return f"env={normalized.upper()}"


def _format_signed_decimal(value: Decimal) -> str:
    """Render a Decimal with sign prefix (``+1.25`` / ``-3.40``).

    Used for unrealized P&L columns. Decimal arithmetic per A05 — never
    floats. The ``+`` is added for non-negative values to give consistent
    width in tabular layouts; the operator sees signed direction at a
    glance instead of guessing from a leading digit.
    """
    if value >= 0:
        return f"+{value}"
    return str(value)


# ---------------------------------------------------------------------------
# /positions
# ---------------------------------------------------------------------------


def build_positions_embed(
    response: PositionsResponse,
    *,
    environment: str,
) -> discord.Embed:
    """Build the ``/positions`` embed.

    Empty-state UX (Phase 0 reality):
      * ``positions=[]`` renders a single description line "No open
        positions." matching the frontend §2.2.3 empty-state pattern.
      * Color stays INFO blue (not WARNING amber) — empty positions is
        the normal state when no signals have fired yet, NOT a degraded
        state.

    Populated state (Phase 1+ when fills land):
      * Title: "Open positions (N) — <env> · HH:MM ET" per spec §6.3.
      * Body: monospace code block with one row per position; columns
        symbol / qty / avg / mark / uPnL. Decimal values rendered via
        :func:`_format_signed_decimal` for uPnL so signs line up.
    """
    as_of_local = _format_et(response.as_of)
    title_count = len(response.positions)
    title = f"Open positions ({title_count}) — {_env_pill(environment)} · {as_of_local}"

    embed = discord.Embed(
        title=title,
        color=EMBED_COLOR_INFO,
    )
    embed.set_footer(text=f"trading-system bot · {env_tag(environment)}")

    if not response.positions:
        embed.description = (
            "No open positions.\n"
            "_(Phase 0 baseline — positions populate after the Week 4 Wed dispatcher "
            "wires fills from QC ObjectStore.)_"
        )
        return embed

    # Populated state: monospace code block. Discord ignores leading
    # whitespace inside ``` blocks so column alignment via spaces works.
    header = f"{'Sym':<8} {'Qty':>5} {'Avg':>10} {'Mark':>10} {'uPnL':>10}"
    separator = "-" * len(header)
    rows = [header, separator]
    for p in response.positions:
        rows.append(
            f"{p.symbol:<8} {p.qty:>5} "
            f"{p.avg_entry_price!s:>10} {p.current_price!s:>10} "
            f"{_format_signed_decimal(p.unrealized_pnl):>10}"
        )
    body = "\n".join(rows)
    # Discord description limit is 4096 chars; 50-row populated state at
    # ~50 chars/row = ~2500 chars — well within limit. If this ever
    # truncates we'd need to switch to multi-embed pagination, but Phase 0
    # has 0 positions so the limit is theoretical.
    embed.description = f"```\n{body}\n```"
    return embed


# ---------------------------------------------------------------------------
# /halt
# ---------------------------------------------------------------------------


_HALT_CONFIRM_BODY = (
    "Halting will:\n"
    "• Cancel pending working orders\n"
    "• Pause new entries (no new signals will be filled)\n"
    "• Existing positions exit normally per their stop/target rules\n\n"
    "RESUME requires a post-incident review write-up + WebAuthn UV — **web-only**.\n"
    "(Discord cannot perform WebAuthn re-auth per dev-guide §1.5.)"
)


def build_halt_confirm_embed(
    *,
    reason: str,
    environment: str,
) -> discord.Embed:
    """Build the confirmation embed for ``/halt <reason>``.

    Confirm-before-execute pattern per spec §6.3:
      * Title: warning prefix + reason (truncated to 100 chars).
      * Body: explains side-effects + the non-resumable-via-Discord rule.
      * Color: WARNING amber (operator should pause, not panic — this is
        a deliberate user action, not an alert).

    The actual ✓ / ✗ buttons are attached by the command handler via
    ``discord.ui.View``; this builder produces only the embed.
    """
    truncated_reason = reason if len(reason) <= 100 else reason[:97] + "..."
    embed = discord.Embed(
        title=f"⚠️ Confirm halt — {truncated_reason}",
        description=_HALT_CONFIRM_BODY,
        color=EMBED_COLOR_WARNING,
    )
    embed.add_field(
        name="Trigger",
        value="`manual_judgment` (operator-initiated via Discord)",
        inline=False,
    )
    embed.add_field(name="Reason", value=f"```\n{reason}\n```", inline=False)
    embed.set_footer(
        text=f"trading-system bot · {env_tag(environment)} · click ✓ to confirm or ✗ to cancel"
    )
    return embed


def build_halt_invoke_success_embed(
    *,
    environment: str,
    audit_event_uuid: str,
    new_state: str,
    severity: str | None,
) -> discord.Embed:
    """Build the post-confirm success embed (Week 4 Wed dispatcher onwards)."""
    severity_text = severity or "routine"
    embed = discord.Embed(
        title=f"🛑 Halt invoked — state: {new_state}",
        description=(
            f"Kill switch fired. System paused.\n"
            f"Severity: `{severity_text}`\n"
            f"Audit: `{audit_event_uuid}`"
        ),
        color=EMBED_COLOR_CRITICAL,
    )
    embed.add_field(
        name="Resume",
        value=(
            "RESUME requires a post-incident review write-up + WebAuthn UV.\n"
            "Open the web app to resume."
        ),
        inline=False,
    )
    embed.set_footer(text=f"trading-system bot · {env_tag(environment)}")
    return embed


def build_halt_invoke_error_embed(
    *,
    environment: str,
    error: ApiClientHTTPError,
) -> discord.Embed:
    """Build the post-confirm error embed.

    Special-cases the Day-15 501 ``KILL_SWITCH_HANDLER_NOT_WIRED`` stub
    so the operator sees the dispatcher-PR pointer instead of a generic
    error. All other failures (auth, network, validation) render the
    canonical error_code + message verbatim.
    """
    if error.error_code == "KILL_SWITCH_HANDLER_NOT_WIRED":
        embed = discord.Embed(
            title="⚠️ Kill-switch handler not yet wired",
            description=(
                "The api accepted the request body but the kill-switch handler "
                "is still 501-stubbed. The Week 4 Wed dispatcher PR wires the "
                "real `services/risk/state_machine.plan_invoke_kill_switch` "
                "+ audit_log INSERT + SSE emit path.\n\n"
                "**Operator action:** none — re-run `/halt <reason>` after the "
                "dispatcher lands. The bot's confirm flow is fully working; "
                "only the backend handler is missing."
            ),
            color=EMBED_COLOR_WARNING,
        )
    else:
        embed = discord.Embed(
            title=f"❌ Halt failed — {error.error_code}",
            description=format_kill_switch_invoke_failure(error),
            color=EMBED_COLOR_CRITICAL,
        )
    embed.set_footer(text=f"trading-system bot · {env_tag(environment)}")
    return embed


# ---------------------------------------------------------------------------
# /capital-deposit + /capital-withdraw
# ---------------------------------------------------------------------------


_CAPITAL_EVENT_CONFIRM_BODY = (
    "**Confirm capital event.** This will INSERT a `capital_events` row + "
    "an audit-chain entry. When the event clears the 5%-of-equity threshold, "
    "the 30-session capital_event mode activates "
    "(m_capital_event=0.5 for sessions 1-5, then 1.0 for sessions 6-30).\n\n"
    "Deposits also reset the trailing-drawdown baseline; withdrawals leave "
    "the baseline unchanged."
)


def build_capital_event_confirm_embed(
    *,
    event_type: str,
    amount_usd: str,
    reason: str,
    environment: str,
) -> discord.Embed:
    """Confirmation embed for ``/capital-deposit`` + ``/capital-withdraw``.

    Operator clicks ✓ → api invokes the capital event. Same confirm
    pattern as ``/halt`` (60s timeout, invoker-only, single-use).
    """
    truncated_reason = reason if len(reason) <= 100 else reason[:97] + "..."
    title_prefix = "💰" if event_type == "deposit" else "💸"
    embed = discord.Embed(
        title=f"{title_prefix} Confirm capital {event_type} — ${amount_usd}",
        description=_CAPITAL_EVENT_CONFIRM_BODY,
        color=EMBED_COLOR_WARNING,
    )
    embed.add_field(name="Event type", value=f"`{event_type}`", inline=True)
    embed.add_field(name="Amount USD", value=f"`{amount_usd}`", inline=True)
    embed.add_field(name="Reason", value=f"```\n{truncated_reason}\n```", inline=False)
    embed.set_footer(
        text=f"trading-system bot · {env_tag(environment)} · click ✓ to confirm or ✗ to cancel"
    )
    return embed


def build_capital_event_success_embed(
    *,
    environment: str,
    event_type: str,
    amount_usd: str,
    threshold_met: bool,
    post_event_equity: str,
    capital_event_audit_event_uuid: str,
    mode_started_audit_event_uuid: str | None,
) -> discord.Embed:
    """Post-confirm success embed for a recorded capital event."""
    title_prefix = "💰" if event_type == "deposit" else "💸"
    color = EMBED_COLOR_OK if threshold_met else EMBED_COLOR_INFO
    embed = discord.Embed(
        title=f"{title_prefix} Capital {event_type} recorded — ${amount_usd}",
        description=(
            f"Post-event equity: `${post_event_equity}`\n"
            f"Threshold met: `{threshold_met}` "
            f"({'30-session mode activated' if threshold_met else 'no mode activation'})\n"
            f"Audit: `{capital_event_audit_event_uuid}`"
        ),
        color=color,
    )
    if mode_started_audit_event_uuid is not None:
        embed.add_field(
            name="Mode started audit",
            value=f"`{mode_started_audit_event_uuid}`",
            inline=False,
        )
    embed.set_footer(text=f"trading-system bot · {env_tag(environment)}")
    return embed


def build_capital_event_error_embed(
    *,
    environment: str,
    event_type: str,
    error: ApiClientHTTPError,
) -> discord.Embed:
    """Post-confirm error embed for a failed capital event invocation."""
    embed = discord.Embed(
        title=f"❌ Capital {event_type} failed",
        description=format_kill_switch_invoke_failure(error),
        color=EMBED_COLOR_CRITICAL,
    )
    embed.set_footer(text=f"trading-system bot · {env_tag(environment)}")
    return embed


def format_kill_switch_invoke_failure(error: ApiClientHTTPError) -> str:
    """Format an :class:`ApiClientHTTPError` as embed-body text.

    Surfaces the canonical envelope fields: error_code, message, optional
    details. HTTP status code rendered in the leading line so the operator
    can tell auth (401) from validation (422) from server (5xx) at a glance.
    """
    parts = [f"HTTP {error.status_code} — `{error.error_code}`", "", error.message]
    if error.details:
        # Single short line per detail key; Discord's value field caps at
        # 1024 chars so we truncate per-key. Phase-0 details are single
        # short keys (bucket / limit / retry_after_s for rate limit).
        details_lines = [f"• `{k}`: `{v}`" for k, v in error.details.items()]
        parts.append("")
        parts.append("Details:")
        parts.extend(details_lines)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------


def build_status_embed(
    response: HealthResponse,
    *,
    now: datetime,
) -> discord.Embed:
    """Build the ``/status`` embed.

    Phase 0 surfaces the api's ``GET /api/health`` payload:
      * status (ok | degraded) → embed color
      * environment + version + db_connected as inline fields
      * checks list expanded as one field per check (typically just
        postgres in Day 23)

    The Phase-1 ``/status`` per spec §6.3 also surfaces risk_state +
    vacation + watchdog + recon + open positions count + pending
    signals count + strategy version. Day 23 ships the health subset
    only; the Phase-1 expansion happens when the relevant Phase-0
    endpoints land (Week 4-5 mostly already shipped — see Day 15
    `/api/system/status` + Day 17 watchdog endpoint).
    """
    color = EMBED_COLOR_OK if response.status == "ok" else EMBED_COLOR_WARNING
    title = f"Status — {_env_pill(response.environment)} · {_format_et(now)}"
    embed = discord.Embed(
        title=title,
        color=color,
    )
    embed.add_field(name="Status", value=f"`{response.status}`", inline=True)
    embed.add_field(name="Version", value=f"`{response.version}`", inline=True)
    embed.add_field(
        name="DB connected",
        value="`true`" if response.db_connected else "`false`",
        inline=True,
    )
    if response.checks:
        # Compact one-line-per-check render; matches spec §6.3 /health
        # tabular shape (without weights — those are Phase 1+).
        check_lines = []
        for c in response.checks:
            ok_glyph = "✅" if c.ok else "❌"
            latency_str = f" — {c.latency_ms} ms" if c.latency_ms is not None else ""
            detail_str = f" — {c.detail}" if c.detail else ""
            check_lines.append(f"{ok_glyph} `{c.name}`{latency_str}{detail_str}")
        embed.add_field(name="Checks", value="\n".join(check_lines), inline=False)
    embed.set_footer(text=f"trading-system bot · {env_tag(response.environment)}")
    return embed


# ---------------------------------------------------------------------------
# /barsync — last bar_sync cycle outcome
# ---------------------------------------------------------------------------


def build_bar_sync_embed(
    response: BarSyncStatusResponse,
    *,
    environment: str,
    now: datetime,
) -> discord.Embed:
    """Build the ``/barsync`` embed from ``GET /api/system/bar-sync``.

    Three color/title branches off ``response.status``:

      * ``ok`` — green; "Bar sync OK" + per-market counts + duration.
      * ``degraded`` — amber; lists the failed markets so the operator
        sees which symbol(s) missed before LEAN's 21:30 UTC read.
      * ``pending`` — info blue; "no cycle since restart" (NOT a fault).

    ``consecutive_failure_count`` / ``consecutive_sentinel_count`` are
    surfaced only when non-zero — a clean steady state stays uncluttered.
    """
    if response.status == "ok":
        color = EMBED_COLOR_OK
        headline = "Bar sync OK"
    elif response.status == "degraded":
        color = EMBED_COLOR_WARNING
        headline = "Bar sync DEGRADED"
    else:
        color = EMBED_COLOR_INFO
        headline = "Bar sync pending"

    title = f"{headline} — {_env_pill(environment)} · {_format_et(now)}"
    embed = discord.Embed(title=title, color=color)

    if response.status == "pending":
        # No cycle has run since the api last restarted.
        running = "running" if response.worker_running else "not running"
        last_fired = response.last_fired_session_date_et or "never"
        embed.description = (
            f"No bar_sync cycle recorded since the api last restarted "
            f"(worker {running}). Last fired session: `{last_fired}`."
        )
        embed.set_footer(text=f"trading-system bot · {env_tag(environment)}")
        return embed

    embed.add_field(
        name="Markets",
        value=f"✅ `{response.successful_count}` / ❌ `{response.failed_count}` "
        f"of `{response.total_markets}`",
        inline=True,
    )
    if response.duration_seconds is not None:
        embed.add_field(
            name="Duration",
            value=f"`{response.duration_seconds}s`",
            inline=True,
        )
    if response.cycle_completed_at_utc is not None:
        embed.add_field(
            name="Completed",
            value=f"`{_format_et(response.cycle_completed_at_utc, with_date=True)}`",
            inline=True,
        )

    if response.failed_markets:
        fail_lines = [
            f"❌ `{m.market}` — {m.error or 'unknown error'}" for m in response.failed_markets
        ]
        # Discord field-value cap is 1024 chars; the Phase-1 universe is 11
        # markets so the joined text stays well under, but truncate
        # defensively to keep the embed valid if error strings balloon.
        value = "\n".join(fail_lines)
        if len(value) > 1024:
            value = value[:1010] + "\n… (truncated)"
        embed.add_field(name="Failed markets", value=value, inline=False)

    badges = []
    if response.consecutive_failure_count > 0:
        badges.append(f"consecutive dirty cycles: `{response.consecutive_failure_count}`")
    if response.consecutive_sentinel_count > 0:
        badges.append(f"consecutive OI-sentinel cycles: `{response.consecutive_sentinel_count}`")
    if badges:
        embed.add_field(name="Watch", value="\n".join(badges), inline=False)

    embed.set_footer(text=f"trading-system bot · {env_tag(environment)}")
    return embed


# ---------------------------------------------------------------------------
# /approve — confirm + post-confirm embeds
# ---------------------------------------------------------------------------


def build_approve_invalid_uuid_embed(
    *,
    raw_signal_id: str,
) -> discord.Embed:
    """Operator typed something that's not a UUID — render an ephemeral hint.

    Truncates ``raw_signal_id`` to 100 chars in the title to avoid
    embed-title-too-long crashes if the operator pastes garbage.
    """
    truncated = raw_signal_id if len(raw_signal_id) <= 100 else raw_signal_id[:97] + "..."
    return discord.Embed(
        title="❌ Invalid signal_id",
        description=(
            f"`{truncated}` doesn't look like a UUID.\n\n"
            "Copy the signal_id from the `#signals` channel embed footer "
            "(format: `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`)."
        ),
        color=EMBED_COLOR_WARNING,
    )


def build_approve_confirm_embed(
    *,
    signal_id: str,
    environment: str,
) -> discord.Embed:
    """Build the confirm-before-approve embed.

    Mirror of :func:`build_halt_confirm_embed` shape: WARNING amber +
    explanation of side-effects + the trigger field. The
    ``ApproveConfirmView`` (registered alongside) attaches the
    ✓ Approve / ✗ Cancel buttons.

    Phase 1+ enhancement: pre-fetch the signal via
    ``GET /api/signals?id_prefix=...`` to display market + direction +
    target_contracts in the confirm body so the operator sees what they're
    about to approve. Today the bot only has the UUID (the operator
    copied it from `#signals`); deferred to keep PR C scope tight.
    """
    embed = discord.Embed(
        title="🟢 Confirm signal approval",
        description=(
            "Approving this signal will:\n"
            "1. UPDATE the signal row to `status='approved'` + write a "
            "`signal_approved` audit event.\n"
            "2. Hand off to the `OrderPlacementWorker` which forwards to "
            "IBKR within ~5s.\n\n"
            "RESUME / cancel via `/system` → kill-switch if you need to "
            "halt before the order fires."
        ),
        color=EMBED_COLOR_WARNING,
    )
    embed.add_field(
        name="Signal ID",
        value=f"`{signal_id}`",
        inline=False,
    )
    embed.set_footer(
        text=f"trading-system bot · {env_tag(environment)} · click ✓ to approve or ✗ to cancel"
    )
    return embed


def build_approve_success_embed(
    *,
    environment: str,
    response: SignalApproveResponse,
) -> discord.Embed:
    """Post-confirm success embed when the api accepted the approval.

    OK green color (the action succeeded; the operator's followup is
    "watch `#fills` for the broker confirmation"). Surfaces the
    audit_event_uuid + audit_sequence_no for cross-reference with the
    audit log + system page.
    """
    embed = discord.Embed(
        title=f"✅ Signal approved — {response.signal_id[:8]}",
        description=(
            f"Status: `{response.new_status}`\n"
            f"Audit: `{response.audit_event_uuid}` (seq #{response.audit_sequence_no})\n\n"
            "OrderPlacementWorker will forward to IBKR within ~5s. "
            "Watch `#fills` for the broker confirmation embed."
        ),
        color=EMBED_COLOR_OK,
    )
    embed.set_footer(text=f"trading-system bot · {env_tag(environment)}")
    return embed


def build_approve_error_embed(
    *,
    environment: str,
    error: ApiClientHTTPError,
) -> discord.Embed:
    """Post-confirm error embed when the api rejected the approval.

    Special-cases the two operator-actionable error codes per the api's
    ``services.api.routes.signals.approve_signal`` mapping:

      * 404 ``SIGNAL_NOT_FOUND`` — operator typed/pasted a stale UUID.
      * 409 ``SIGNAL_NOT_PENDING`` — fast-double-click race; the signal
        was already approved/rejected by another path (web, prior /approve).

    Other errors (auth, 5xx, validation) render the canonical envelope
    via :func:`format_kill_switch_invoke_failure` (the same helper used
    for halt errors — the format is generic).
    """
    if error.error_code == "SIGNAL_NOT_FOUND":
        embed = discord.Embed(
            title="❌ Signal not found",
            description=(
                "The api doesn't have a signal with that ID. The signal_id "
                "may have been mistyped, or the signal predates the "
                "current accounts row.\n\n"
                "Check `#signals` for the latest pending signals + their "
                "embed-footer UUIDs."
            ),
            color=EMBED_COLOR_WARNING,
        )
    elif error.error_code == "SIGNAL_NOT_PENDING":
        embed = discord.Embed(
            title="⚠️ Signal not pending",
            description=(
                "The signal already left the `pending` state — likely "
                "approved/rejected by another path (web `/signals` page, "
                "or a prior `/approve`). The api row is correct as-is.\n\n"
                "Check the audit log entry on the system page if you "
                "need to confirm who acted."
            ),
            color=EMBED_COLOR_WARNING,
        )
    else:
        embed = discord.Embed(
            title=f"❌ Approve failed — {error.error_code}",
            description=format_kill_switch_invoke_failure(error),
            color=EMBED_COLOR_CRITICAL,
        )
    embed.set_footer(text=f"trading-system bot · {env_tag(environment)}")
    return embed


__all__ = [
    "EMBED_COLOR_CRITICAL",
    "EMBED_COLOR_INFO",
    "EMBED_COLOR_OK",
    "EMBED_COLOR_WARNING",
    "build_approve_confirm_embed",
    "build_approve_error_embed",
    "build_approve_invalid_uuid_embed",
    "build_approve_success_embed",
    "build_bar_sync_embed",
    "build_halt_confirm_embed",
    "build_halt_invoke_error_embed",
    "build_halt_invoke_success_embed",
    "build_positions_embed",
    "build_status_embed",
    "format_kill_switch_invoke_failure",
]
