"""services/discord_shared/governance_digest.py — §3.8 governance-report embeds (pure).

Monthly + quarterly governance reports for the **#reports** channel
(delta spec §3.8: "monthly + quarterly governance reports (decisions-log
2026-07-08 refinement loops) pushed to #reports"). Mirrors the `/cycle`
digest architecture exactly: this module is the STDLIB-ONLY embed
builder in the dependency-neutral ``services.discord_shared`` package
(the import-hygiene test binds — no sqlalchemy/httpx/discord/pydantic);
the ``EventPushPlan``-typed planner + fire-window scheduler live in
``services.webhook_pusher.governance_reports``.

Inputs are the raw JSON dicts from ``GET /api/system/governance-report``
and ``GET /api/system/gates`` — parsed defensively; every missing field
renders "n/a" instead of raising (graceful-degradation contract, same
as the cycle digest). ANNOUNCE-ONLY (dev-guide §1.5 locked): bare embed
dicts, no components.

[A05]: money passes through as the api's Decimal-as-string payloads —
no float math. [A06]: every datetime tz-aware UTC.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from services.discord_shared.cycle_digest import md_italic, md_plain_decimal

# Embed colors (24-bit RGB ints; mirrors the cycle-digest palette).
COLOR_REPORT_MONTHLY: Final[int] = 0x3498DB  # blue — informational
COLOR_REPORT_QUARTERLY: Final[int] = 0x9B59B6  # purple — governance cadence

_FOOTER_TEXT: Final[str] = "governance report · announce-only"

#: Monthly refinement-loop reminder (decisions-log 2026-07-08,
#: "Refinement governance"): the operator's monthly prompt to check the
#: event-triggered review conditions — NOT a parameter-tuning prompt.
MONTHLY_REFINEMENT_REMINDER: Final[str] = (
    "Refinement loop (decisions-log 2026-07-08): check for venue changes "
    "(fee schedule, margin, Bermuda-perp availability) and performance "
    "tripwires; record findings in the decisions-log. Signal parameters "
    "are NOT tuned on schedule — risk knobs move only with evidence via "
    "amendment; signal knobs only via falsify-and-replace."
)

#: Quarterly §4 revalidation reminder (delta spec §4 / strategy §9).
QUARTERLY_REVALIDATION_REMINDER: Final[str] = (
    "Quarterly revalidation due (delta spec §4): extend the data set, "
    "re-run the §9 falsification suite against the frozen parameters, "
    "check live-vs-sim tracking, and log the verdict in the decisions-log."
)

#: Quarterly governance checklist (decisions-log 2026-07-08 refinement
#: governance, itemized).
QUARTERLY_GOVERNANCE_CHECKLIST: Final[tuple[str, ...]] = (
    "B1/B2/B3 telemetry gates reviewed (they run permanently, not just Phase A)",
    "§9 falsification suite re-run on extended data — verdict logged",
    "Live-vs-sim tracking within tolerance over the quarter",
    "Venue-change sweep: fee schedule / margin / product lineup / Bermuda-perp status",
    "Performance tripwires reviewed (slippage demotion triggers, weekly-loss counts)",
    "Decisions-log entries current; strategy-explainer change-log synced",
)


def _fmt_usd(raw: Any) -> str:
    """Decimal-as-string with a $ prefix ('n/a' when absent).

    Scientific notation is normalized to plain digits — a zero
    exchange fee arrives as "0E-8" and must render "$0.00000000",
    not "$0E-8" (2026-08-14 monthly-report render defect). Exact,
    display-only ([A05]); unparseable passes through.
    """
    if not isinstance(raw, str) or not raw:
        return "n/a"
    display = md_plain_decimal(raw)
    return f"-${display[1:]}" if display.startswith("-") else f"${display}"


def _fmt_pct_rate(raw: Any) -> str:
    """Annualized-rate fraction string → percent display ([A05]: stdlib
    Decimal, display-only; unparseable passes through)."""
    if not isinstance(raw, str) or not raw:
        return "n/a"
    try:
        return f"{Decimal(raw) * 100:.2f}%"
    except (InvalidOperation, ValueError):
        return raw


def _trades_line(trades: Mapping[str, Any] | None) -> str:
    if not isinstance(trades, Mapping):
        return "n/a"
    return (
        f"{trades.get('closed_count', 'n/a')} closed "
        f"({trades.get('winners', '?')}W/{trades.get('losers', '?')}L) · "
        f"realized P&L {_fmt_usd(trades.get('realized_pnl_usd'))} · "
        f"commissions {_fmt_usd(trades.get('realized_commission_usd'))}"
    )


def _fees_line(fees: Mapping[str, Any] | None) -> str:
    if not isinstance(fees, Mapping):
        return "n/a"
    return (
        f"{fees.get('fill_count', 'n/a')} fills · "
        f"commission {_fmt_usd(fees.get('commission_usd'))} + "
        f"exchange {_fmt_usd(fees.get('exchange_fee_usd'))} = "
        f"{_fmt_usd(fees.get('total_usd'))}"
    )


def _funding_lines(payload: Mapping[str, Any]) -> str:
    sources = payload.get("funding_sources")
    if not isinstance(sources, list) or not sources:
        return "no funding observations recorded"
    lines = []
    for src in sources:
        if not isinstance(src, Mapping):
            continue
        lines.append(
            f"{src.get('source', '?')}: mean |annualized| "
            f"{_fmt_pct_rate(src.get('mean_abs_annualized_rate'))} "
            f"({src.get('observation_count', '?')} obs)"
        )
    note = payload.get("funding_note")
    if isinstance(note, str) and note:
        lines.append(md_italic(note))
    return "\n".join(lines) if lines else "no funding observations recorded"


def _sweeps_line(sweeps: Mapping[str, Any] | None) -> str:
    if not isinstance(sweeps, Mapping):
        return "n/a"
    completed = sweeps.get("completed_count", 0)
    if completed == 0 and sweeps.get("failed_count", 0) == 0:
        return "no sweeps (cash manager dormant or idle)"
    return (
        f"{completed} completed · to_yield {_fmt_usd(sweeps.get('to_yield_usd'))} · "
        f"to_margin {_fmt_usd(sweeps.get('to_margin_usd'))} · "
        f"{sweeps.get('failed_count', 0)} failed"
    )


def _capital_events_line(payload: Mapping[str, Any]) -> str:
    events = payload.get("capital_events")
    if not isinstance(events, list) or not events:
        return "none"
    lines = []
    for e in events:
        if not isinstance(e, Mapping):
            continue
        ts = str(e.get("effective_at_utc") or "")[:10]
        flag = " (threshold met)" if e.get("threshold_met") else ""
        lines.append(f"{ts} {e.get('event_type', '?')} {_fmt_usd(e.get('amount_usd'))}{flag}")
    return "\n".join(lines) if lines else "none"


def _period_label(payload: Mapping[str, Any]) -> str:
    start = payload.get("period_start_utc")
    return str(start)[:7] if isinstance(start, str) and len(str(start)) >= 7 else "?"


def build_monthly_report_embed(
    report: Mapping[str, Any],
    *,
    now_utc: datetime,
    gates_summary: str | None = None,
) -> dict[str, Any]:
    """Monthly governance report embed (1st of month, prior month's data)."""
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be tz-aware UTC; got naive datetime")
    label = _period_label(report)
    fields: list[dict[str, Any]] = [
        {"name": "Trades", "value": _trades_line(report.get("trades"))[:1024], "inline": False},
        {"name": "Fees", "value": _fees_line(report.get("fees"))[:1024], "inline": False},
        {"name": "Funding (telemetry)", "value": _funding_lines(report)[:1024], "inline": False},
        {
            "name": "Cash sweeps",
            "value": _sweeps_line(report.get("sweeps"))[:1024],
            "inline": False,
        },
        {
            "name": "Capital events",
            "value": _capital_events_line(report)[:1024],
            "inline": False,
        },
    ]
    if gates_summary:
        fields.append({"name": "§10 gates", "value": gates_summary[:1024], "inline": False})
    fields.append(
        {"name": "Refinement loop", "value": MONTHLY_REFINEMENT_REMINDER[:1024], "inline": False}
    )
    return {
        "title": f"Monthly governance report — {label}"[:256],
        "description": (
            f"Period {report.get('period_start_utc', '?')} → "
            f"{report.get('period_end_utc', '?')} (exclusive end)."
        )[:4096],
        "color": COLOR_REPORT_MONTHLY,
        "fields": fields,
        "footer": {"text": _FOOTER_TEXT},
        "timestamp": now_utc.astimezone(UTC).isoformat(),
    }


def build_quarterly_report_embed(
    report: Mapping[str, Any],
    *,
    now_utc: datetime,
    gates_summary: str | None = None,
    quarter_label: str,
) -> dict[str, Any]:
    """Quarterly governance report: monthly shape + §4 revalidation +
    the governance checklist."""
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be tz-aware UTC; got naive datetime")
    fields: list[dict[str, Any]] = [
        {"name": "Trades", "value": _trades_line(report.get("trades"))[:1024], "inline": False},
        {"name": "Fees", "value": _fees_line(report.get("fees"))[:1024], "inline": False},
        {"name": "Funding (telemetry)", "value": _funding_lines(report)[:1024], "inline": False},
        {
            "name": "Cash sweeps",
            "value": _sweeps_line(report.get("sweeps"))[:1024],
            "inline": False,
        },
        {
            "name": "Capital events",
            "value": _capital_events_line(report)[:1024],
            "inline": False,
        },
    ]
    if gates_summary:
        fields.append({"name": "§10 gates", "value": gates_summary[:1024], "inline": False})
    fields.append(
        {
            "name": "§4 quarterly revalidation",
            "value": QUARTERLY_REVALIDATION_REMINDER[:1024],
            "inline": False,
        }
    )
    checklist = "\n".join(f"☐ {item}" for item in QUARTERLY_GOVERNANCE_CHECKLIST)
    fields.append({"name": "Governance checklist", "value": checklist[:1024], "inline": False})
    return {
        "title": f"Quarterly governance report — {quarter_label}"[:256],
        "description": (
            f"Quarter covering {report.get('period_start_utc', '?')} → "
            f"{report.get('period_end_utc', '?')} (exclusive end)."
        )[:4096],
        "color": COLOR_REPORT_QUARTERLY,
        "fields": fields,
        "footer": {"text": _FOOTER_TEXT},
        "timestamp": now_utc.astimezone(UTC).isoformat(),
    }


__all__ = [
    "COLOR_REPORT_MONTHLY",
    "COLOR_REPORT_QUARTERLY",
    "MONTHLY_REFINEMENT_REMINDER",
    "QUARTERLY_GOVERNANCE_CHECKLIST",
    "QUARTERLY_REVALIDATION_REMINDER",
    "build_monthly_report_embed",
    "build_quarterly_report_embed",
]
