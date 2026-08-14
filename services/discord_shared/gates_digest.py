"""services/discord_shared/gates_digest.py — §10 gate-status renderers (pure).

Strategy §10 acceptance-gate tracker surfaces (C1→C2 build): renders the
``GET /api/system/gates`` payload as (a) a full embed dict for the
on-demand ``/gates`` slash command and (b) a compact one-field summary
the 00:10 UTC nightly cycle digest appends
(``services/webhook_pusher/cycle_digest_scheduler.py`` fetches the gates
payload best-effort and passes it through
``build_cycle_digest_embed(..., gates=...)``).

Lives in the dependency-neutral ``services.discord_shared`` package —
**stdlib imports only** (same two-runtime constraint as
``cycle_digest.py``: the bot image has no sqlalchemy, the pusher image
has no discord.py). ANNOUNCE-ONLY: bare embed dicts, no components.

Graceful degradation: any missing / odd-shaped key renders "n/a" or is
skipped — never a raise (the nightly digest must not die on a gates
regression).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final

from services.discord_shared.cycle_digest import (
    COLOR_DIGEST_FAIL,
    COLOR_DIGEST_OK,
    COLOR_DIGEST_WARN,
    md_italic,
)

_FOOTER_TEXT: Final[str] = "§10 gate tracker · announce-only"

_STATUS_EMOJI: Final[dict[str, str]] = {
    "green": "🟢",
    "red": "🔴",
    "insufficient_data": "⚪",
}


def _gate_entries(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    gates = payload.get("gates")
    if not isinstance(gates, list):
        return []
    return [g for g in gates if isinstance(g, dict)]


def _status_emoji(raw: Any) -> str:
    return _STATUS_EMOJI.get(raw, "⚪") if isinstance(raw, str) else "⚪"


def _summary_counts(entries: list[Mapping[str, Any]]) -> tuple[int, int, int]:
    green = sum(1 for g in entries if g.get("status") == "green")
    red = sum(1 for g in entries if g.get("status") == "red")
    pending = len(entries) - green - red
    return green, red, pending


def format_gates_summary_line(payload: Mapping[str, Any]) -> str:
    """One-line §10 summary for the nightly digest field."""
    entries = _gate_entries(payload)
    if not entries:
        return "gate status unavailable"
    green, red, pending = _summary_counts(entries)
    chips = " ".join(f"{_status_emoji(g.get('status'))}{g.get('key', '?')}" for g in entries)
    days_live = payload.get("days_live")
    min_days = payload.get("phase_a_min_days")
    days_txt = (
        f"day {days_live}/{min_days}"
        if isinstance(days_live, int) and isinstance(min_days, int)
        else "days live n/a"
    )
    eligible = "SCALE-UP ELIGIBLE (mechanical)" if payload.get("scale_up_eligible") else None
    parts = [chips, f"{green} green · {red} red · {pending} pending", days_txt]
    if eligible:
        parts.append(eligible)
    return " | ".join(parts)


def build_gates_embed(
    payload: Mapping[str, Any],
    *,
    now_utc: datetime,
) -> dict[str, Any]:
    """Full ``/gates`` embed: one field per gate, honest evidence notes."""
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be tz-aware UTC; got naive datetime")

    entries = _gate_entries(payload)
    if not entries:
        return {
            "title": "§10 gates — no data",
            "description": "The gates payload carried no gate entries.",
            "color": COLOR_DIGEST_WARN,
            "fields": [],
            "footer": {"text": _FOOTER_TEXT},
            "timestamp": now_utc.astimezone(UTC).isoformat(),
        }

    green, red, pending = _summary_counts(entries)
    color = (
        COLOR_DIGEST_OK
        if red == 0 and pending == 0
        else (COLOR_DIGEST_FAIL if red else COLOR_DIGEST_WARN)
    )

    fields: list[dict[str, Any]] = []
    for gate in entries:
        key = str(gate.get("key") or "?")
        title = str(gate.get("title") or "")
        lines = [
            f"{gate.get('value', 'n/a')}",
            f"target: {gate.get('target', 'n/a')}",
        ]
        note = gate.get("evidence_note")
        if isinstance(note, str) and note:
            lines.append(md_italic(note))
        fields.append(
            {
                "name": f"{_status_emoji(gate.get('status'))} {key} — {title}"[:256],
                "value": "\n".join(lines)[:1024],
                "inline": False,
            }
        )

    days_live = payload.get("days_live")
    min_days = payload.get("phase_a_min_days")
    description_bits = [f"{green} green · {red} red · {pending} pending"]
    if isinstance(days_live, int) and isinstance(min_days, int):
        description_bits.append(f"day {days_live} of the {min_days}-day Phase-A minimum")
    if payload.get("scale_up_eligible"):
        description_bits.append(
            "**All mechanical gates green + minimum days met.** Operator-attested "
            "evidence (A1 statement match, A3 drills) still applies before scaling."
        )

    return {
        "title": "§10 acceptance gates — C1 → C2",
        "description": " · ".join(description_bits)[:4096],
        "color": color,
        "fields": fields,
        "footer": {"text": _FOOTER_TEXT},
        "timestamp": now_utc.astimezone(UTC).isoformat(),
    }


__all__ = [
    "build_gates_embed",
    "format_gates_summary_line",
]
