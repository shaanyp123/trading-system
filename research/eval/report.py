"""Operator-legible report writer → ``research/runs/<ts>/`` (design §4.4).

Zero-dependency: emits ``report.md`` (plain text), ``report.html`` (self-contained,
with inline-SVG equity sparklines — no matplotlib/JS), and ``result.json`` (machine
summary, stamped with the harness version so a report traces back to its code).
P1 reports the daily spine; the banner is explicit that this is research-only,
that LEAN is the authority (P2), and that leverage/ruin modeling lands in P3 — the
report must never read as more than it is (design §6: surface the truth, including
limits).
"""

from __future__ import annotations

import html
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import numpy as np
import numpy.typing as npt


@dataclass(frozen=True, slots=True)
class ReportRow:
    """One instrument's line in a run report."""

    symbol: str
    strategy_name: str
    start: date
    end: date
    bars: int
    start_price: float
    end_price: float
    price_treatment: str
    fill: str
    metrics: dict[str, float]
    equity_curve: npt.NDArray[np.float64] = field(repr=False)


_BANNER = (
    "P1 daily spine — RESEARCH-ONLY, NON-AUTHORITATIVE. LEAN is the engine of "
    "record; this report is reproduced + parity-checked in LEAN at P2. Leverage, "
    "margin, liquidation, and ruin metrics land in P3. Daily resolution only "
    "(intraday deferred per the 2026-06-03 sign-off)."
)


def _fmt_pct(x: float) -> str:
    if not math.isfinite(x):
        return "n/a"
    x = x + 0.0  # normalize -0.0 → 0.0 so an exact-zero return shows "+0.00%"
    return f"{x * 100:+.2f}%"


def _fmt_dd(x: float) -> str:
    """Render a drawdown magnitude as a loss (e.g. ``-4.55%``)."""
    if not math.isfinite(x) or x == 0.0:
        return "0.00%"
    return f"-{x * 100:.2f}%"


def _fmt_money(x: float) -> str:
    if not math.isfinite(x):
        return "n/a"
    return f"${x:,.2f}"


def _sparkline_svg(equity: npt.NDArray[np.float64], *, width: int = 600, height: int = 60) -> str:
    """Inline SVG polyline of the equity curve (pure string; no dependency)."""
    n = equity.size
    if n == 0:
        return ""
    lo = float(np.min(equity))
    hi = float(np.max(equity))
    span = hi - lo
    pad = 4
    inner_h = height - 2 * pad

    def _y(v: float) -> float:
        if span <= 0.0:
            return height / 2.0
        return pad + inner_h * (1.0 - (v - lo) / span)

    if n == 1:
        pts = f"0,{_y(float(equity[0])):.1f} {width},{_y(float(equity[0])):.1f}"
    else:
        step = width / (n - 1)
        pts = " ".join(f"{i * step:.1f},{_y(float(equity[i])):.1f}" for i in range(n))
    up = equity[-1] >= equity[0]
    color = "#1a7f37" if up else "#cf222e"
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'preserveAspectRatio="none" role="img">'
        f'<polyline fill="none" stroke="{color}" stroke-width="1.5" points="{pts}"/>'
        f"</svg>"
    )


def _markdown(run_name: str, version: str, generated_at: datetime, rows: list[ReportRow]) -> str:
    lines = [
        f"# Research run: {run_name}",
        "",
        f"> {_BANNER}",
        "",
        f"- harness version: `{version}`",
        f"- generated: {generated_at.isoformat()}",
        f"- instruments: {len(rows)}",
        "",
        "| Symbol | Strategy | Start | End | Bars | Total return | CAGR | Max DD | P&L |",
        "|---|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        m = r.metrics
        lines.append(
            f"| {r.symbol} | {r.strategy_name} | {r.start} | {r.end} | {r.bars} | "
            f"{_fmt_pct(m['total_return'])} | {_fmt_pct(m['cagr'])} | "
            f"{_fmt_dd(m['max_drawdown_pct'])} | {_fmt_money(m['pnl'])} |"
        )
    lines.append("")
    return "\n".join(lines)


def _html(run_name: str, version: str, generated_at: datetime, rows: list[ReportRow]) -> str:
    esc = html.escape
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>Research run: {esc(run_name)}</title>",
        "<style>body{font:14px/1.5 -apple-system,system-ui,sans-serif;margin:2rem;color:#1f2328}"
        ".banner{background:#fff8c5;border:1px solid #d4a72c;padding:.75rem 1rem;border-radius:6px}"
        "table{border-collapse:collapse;width:100%;margin:1rem 0}"
        "th,td{border:1px solid #d0d7de;padding:.4rem .6rem;text-align:right}"
        "th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}"
        ".spark{border:1px solid #d0d7de;border-radius:6px;margin:.5rem 0;padding:.25rem}"
        ".muted{color:#656d76}</style></head><body>",
        f"<h1>Research run: {esc(run_name)}</h1>",
        f"<p class='banner'>{esc(_BANNER)}</p>",
        f"<p class='muted'>harness <code>{esc(version)}</code> · generated "
        f"{esc(generated_at.isoformat())} · {len(rows)} instrument(s)</p>",
        "<table><thead><tr><th>Symbol</th><th>Strategy</th><th>Start</th><th>End</th>"
        "<th>Bars</th><th>Total return</th><th>CAGR</th><th>Max DD</th><th>P&amp;L</th>"
        "</tr></thead><tbody>",
    ]
    for r in rows:
        m = r.metrics
        parts.append(
            f"<tr><td>{esc(r.symbol)}</td><td>{esc(r.strategy_name)}</td>"
            f"<td>{r.start}</td><td>{r.end}</td><td>{r.bars}</td>"
            f"<td>{_fmt_pct(m['total_return'])}</td><td>{_fmt_pct(m['cagr'])}</td>"
            f"<td>{_fmt_dd(m['max_drawdown_pct'])}</td><td>{_fmt_money(m['pnl'])}</td></tr>"
        )
    parts.append("</tbody></table>")
    for r in rows:
        parts.append(
            f"<div class='spark'><div class='muted'>{esc(r.symbol)} equity "
            f"({esc(r.price_treatment)} prices, {esc(r.fill)} fill)</div>"
            f"{_sparkline_svg(r.equity_curve)}</div>"
        )
    parts.append("</body></html>")
    return "".join(parts)


def _result_json(run_name: str, version: str, generated_at: datetime, rows: list[ReportRow]) -> str:
    payload = {
        "run_name": run_name,
        "harness_version": version,
        "generated_at": generated_at.isoformat(),
        "resolution": "daily",
        "authoritative": False,
        "instruments": [
            {
                "symbol": r.symbol,
                "strategy": r.strategy_name,
                "start": r.start.isoformat(),
                "end": r.end.isoformat(),
                "bars": r.bars,
                "start_price": r.start_price,
                "end_price": r.end_price,
                "price_treatment": r.price_treatment,
                "fill": r.fill,
                "metrics": {
                    k: (round(v, 8) if math.isfinite(v) else None) for k, v in r.metrics.items()
                },
            }
            for r in rows
        ],
    }
    return json.dumps(payload, indent=2)


def write_report(
    run_dir: Path,
    *,
    run_name: str,
    harness_version: str,
    generated_at: datetime,
    rows: list[ReportRow],
) -> Path:
    """Write ``report.md`` + ``report.html`` + ``result.json`` into ``run_dir``.

    Returns the path to ``report.html`` (the operator-facing artifact).
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.md").write_text(
        _markdown(run_name, harness_version, generated_at, rows), encoding="utf-8"
    )
    html_path = run_dir / "report.html"
    html_path.write_text(_html(run_name, harness_version, generated_at, rows), encoding="utf-8")
    (run_dir / "result.json").write_text(
        _result_json(run_name, harness_version, generated_at, rows), encoding="utf-8"
    )
    return html_path
