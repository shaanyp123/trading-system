"""services/discord_shared/cycle_digest.py — §3.8 digest embed builder (pure).

Crypto-pivot delta spec §3.8: the ``/cycle`` daily-decision outcome
digest embed (per-asset trend score → target → action → est. costs)
rendered from the ``GET /api/system/cycle`` payload (§3.7).
ANNOUNCE-ONLY (dev-guide §1.5 locked): the embed dict carries no
buttons, no approval affordances.

Lives in the dependency-neutral ``services.discord_shared`` package —
**stdlib imports only** (see the package ``__init__`` contract) —
because it is SHARED across two container runtimes with disjoint deps:

* the Discord bot's on-demand ``/cycle`` slash command
  (``services/discord_bot/commands/cycle.py`` renders the returned dict
  via ``discord.Embed.from_dict``); the bot image has NO sqlalchemy —
  and ``services.webhook_pusher.__init__`` eagerly imports the
  sqlalchemy-touching dispatcher, so the builder cannot live there
  (that exact ImportError failed the discord_bot docker-build CI job);
* the webhook_pusher's 00:10 UTC scheduled push — the
  :class:`~services.webhook_pusher.cycle_digest_scheduler.CycleDigestScheduler`
  posts the same dict to #daily-brief via
  ``services.webhook_pusher.cycle_digest.plan_cycle_digest_push`` (the
  EventPushPlan-typed planner half, which stays in the pusher package);
  the pusher image has NO discord.py, so the reverse re-homing (under
  ``services.discord_bot``, whose ``__init__`` eagerly imports
  discord.py) is equally forbidden.

One builder ⇒ the on-demand and scheduled digests are pixel-identical.

Graceful-degradation contract (task-locked): every payload state renders
— completed / dispatching / failed / skipped decisions, a decision
missing for today (worker down), and no decision at all (fresh DB). The
no-decision / worker-down digests SAY SO instead of erroring.

[A05]: all money values pass through as the api's Decimal-as-string
payloads — no float math here. [A06]: every datetime tz-aware UTC.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final
from zoneinfo import ZoneInfo

# Embed colors (24-bit RGB ints; mirrors the event_pushes palette).
COLOR_DIGEST_OK: Final[int] = 0x2ECC71  # green — decision completed
COLOR_DIGEST_INFO: Final[int] = 0x3498DB  # blue — dispatching / in-flight
COLOR_DIGEST_WARN: Final[int] = 0xF39C12  # amber — skipped / missing / no data
COLOR_DIGEST_FAIL: Final[int] = 0xE74C3C  # red — decision failed

_ET = ZoneInfo("America/New_York")

_FOOTER_TEXT: Final[str] = "cycle digest · announce-only"


def _fmt_et(iso_ts: str | None) -> str:
    """RFC-3339 UTC string → 'YYYY-MM-DD HH:MM ET' (or 'n/a')."""
    if not iso_ts:
        return "n/a"
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return "n/a"
    if ts.tzinfo is None:
        return "n/a"
    return ts.astimezone(_ET).strftime("%Y-%m-%d %H:%M ET")


def _fmt_usd(raw: Any, *, cents: bool = False) -> str:
    """Decimal-as-string passthrough with a $ prefix ('n/a' when absent).

    No float parsing — the api already serialized the exact Decimal
    ([A05]); ``decimal.Decimal`` (stdlib) is used ONLY to round for
    display when ``cents=True`` (e.g. equity arrives with the venue's
    full 8-dp scale — "1549.10000000" must render "$1549.10", not raw).
    Display-only: nothing rounded here feeds computation.
    An unparseable string falls back to the exact passthrough.
    """
    if raw is None or not isinstance(raw, str) or not raw:
        return "n/a"
    display = raw
    if cents:
        try:
            display = f"{Decimal(raw):.2f}"
        except InvalidOperation:
            display = raw
    return f"-${display[1:]}" if display.startswith("-") else f"${display}"


def _fmt_multiplier(raw: Any) -> str:
    """Trim a Decimal-as-string multiplier for display ("1.000000" → "1",
    "0.500000" → "0.5"). Exact passthrough when unparseable."""
    if not isinstance(raw, str) or not raw:
        return str(raw)
    try:
        return format(Decimal(raw).normalize(), "f")
    except InvalidOperation:
        return raw


def _asset_line(entry: Mapping[str, Any]) -> str:
    """One asset's 'score → target → action → costs' line (§3.8)."""
    trend = entry.get("trend_score")
    prior = entry.get("prior_contracts")
    target = entry.get("final_target")
    action = entry.get("action")
    parts = [
        f"trend {trend if trend is not None else 'n/a'}",
        f"target {target if target is not None else 'n/a'}"
        + (f" (prior {prior})" if prior is not None else ""),
        (action.upper() if isinstance(action, str) else "n/a"),
        f"est cost {_fmt_usd(entry.get('est_cost_usd'))}",
    ]
    line = " → ".join(parts[:3]) + f" · {parts[3]}"
    extras: list[str] = []
    if entry.get("mark") is not None:
        extras.append(f"mark {_fmt_usd(entry.get('mark'))}")
    if entry.get("stop_level") is not None:
        extras.append(f"stop {_fmt_usd(entry.get('stop_level'))}")
    if extras:
        line += "\n" + " · ".join(extras)
    return line


def _worker_line(worker: Mapping[str, Any] | None) -> str:
    """Risk-loop heartbeat summary ('worker down' cases say so)."""
    if not isinstance(worker, dict) or worker.get("risk_loop_heartbeat_utc") is None:
        return "no risk-loop heartbeat recorded — strategy worker has not run"
    age = worker.get("seconds_since_heartbeat")
    fresh = bool(worker.get("heartbeat_fresh"))
    marks_stale = bool(worker.get("marks_stale"))
    freshness = "fresh" if fresh else "STALE — worker may be down"
    marks = "marks STALE" if marks_stale else "marks OK"
    return f"heartbeat {age if age is not None else '?'} s ago ({freshness}) · {marks}"


def build_cycle_digest_embed(
    payload: Mapping[str, Any],
    *,
    now_utc: datetime,
    gates_summary: str | None = None,
) -> dict[str, Any]:
    """Build the §3.8 digest embed dict from the §3.7 cycle payload.

    Handles every payload state without raising: completed / dispatching /
    failed / skipped decisions, a decision missing for today's UTC date
    (worker down), and no decision at all (fresh DB). ANNOUNCE-ONLY: the
    returned dict is a bare embed — callers must not attach components.
    """
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be tz-aware UTC; got naive datetime")

    decision = payload.get("decision")
    worker = payload.get("worker")
    fields: list[dict[str, Any]] = []
    today = now_utc.astimezone(UTC).date().isoformat()

    if not isinstance(decision, dict):
        title = "Daily decision — none recorded yet"
        color = COLOR_DIGEST_WARN
        description = (
            "No strategy decision has been recorded. The strategy worker has "
            "not completed its first 00:05 UTC cycle (fresh deployment, or "
            "the worker is down)."
        )
    else:
        decision_date = str(decision.get("decision_date") or "?")
        status = str(decision.get("status") or "unknown")
        env = decision.get("env")
        missing_today = decision_date < today
        if missing_today:
            title = f"Daily decision — MISSING for {today}"
            color = COLOR_DIGEST_WARN
            description = (
                f"No decision recorded for {today}. Last decision: "
                f"{decision_date} ({status}). The strategy worker may be down."
            )
        else:
            title = f"Daily decision — {decision_date} · {status}"
            color = {
                "completed": COLOR_DIGEST_OK,
                "dispatching": COLOR_DIGEST_INFO,
                "failed": COLOR_DIGEST_FAIL,
            }.get(status, COLOR_DIGEST_WARN)
            description = f"Decided at {_fmt_et(decision.get('decided_at_utc'))}."

        skip_reason = decision.get("skip_reason")
        if isinstance(skip_reason, str) and skip_reason:
            fields.append({"name": "Skip reason", "value": skip_reason[:1024], "inline": False})

        assets = decision.get("assets")
        if isinstance(assets, list):
            for entry in assets:
                if not isinstance(entry, dict):
                    continue
                asset = str(entry.get("asset") or "?")
                fields.append({"name": asset, "value": _asset_line(entry)[:1024], "inline": False})

        account_bits = [f"equity {_fmt_usd(decision.get('equity_usd'), cents=True)}"]
        if decision.get("m_combined") is not None:
            account_bits.append(f"m_combined {_fmt_multiplier(decision.get('m_combined'))}")
        account_bits.append(
            "weekly-halved YES" if decision.get("weekly_halved") else "weekly-halved no"
        )
        if isinstance(env, str) and env:
            account_bits.append(env)
        fields.append(
            {"name": "Account", "value": " · ".join(account_bits)[:1024], "inline": False}
        )

    fields.append({"name": "Risk loop", "value": _worker_line(worker)[:1024], "inline": False})
    if gates_summary:
        # §10 gate tracker one-liner (pre-rendered by the caller via
        # services.discord_shared.gates_digest.format_gates_summary_line
        # — passed as a string to keep this module's import DAG flat).
        fields.append({"name": "§10 gates", "value": gates_summary[:1024], "inline": False})
    next_close = payload.get("next_friday_close_utc")
    fields.append(
        {
            "name": "Next CDE Friday close",
            "value": _fmt_et(next_close if isinstance(next_close, str) else None),
            "inline": False,
        }
    )

    return {
        "title": title[:256],
        "description": description[:4096],
        "color": color,
        "fields": fields,
        "footer": {"text": _FOOTER_TEXT},
        "timestamp": now_utc.astimezone(UTC).isoformat(),
    }


__all__ = [
    "COLOR_DIGEST_FAIL",
    "COLOR_DIGEST_INFO",
    "COLOR_DIGEST_OK",
    "COLOR_DIGEST_WARN",
    "build_cycle_digest_embed",
]
