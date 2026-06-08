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


def _markdown(
    run_name: str,
    version: str,
    generated_at: datetime,
    rows: list[ReportRow],
    alerts: tuple[str, ...] = (),
) -> str:
    lines = [
        f"# Research run: {run_name}",
        "",
        f"> {_BANNER}",
        "",
    ]
    if alerts:
        lines += [
            "> 🟥 **⚠ LIQUIDATION DETECTED — LEAN issued a margin-call / forced-liquidation order:**",
            *[f"> {a}" for a in alerts],
            "",
        ]
    lines += [
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


def _html(
    run_name: str,
    version: str,
    generated_at: datetime,
    rows: list[ReportRow],
    alerts: tuple[str, ...] = (),
) -> str:
    esc = html.escape
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>Research run: {esc(run_name)}</title>",
        "<style>body{font:14px/1.5 -apple-system,system-ui,sans-serif;margin:2rem;color:#1f2328}"
        ".banner{background:#fff8c5;border:1px solid #d4a72c;padding:.75rem 1rem;border-radius:6px}"
        ".ruin{background:#ffebe9;border:2px solid #cf222e;color:#82071e;padding:.75rem 1rem;"
        "border-radius:6px;margin:1rem 0;font-weight:600}"
        "table{border-collapse:collapse;width:100%;margin:1rem 0}"
        "th,td{border:1px solid #d0d7de;padding:.4rem .6rem;text-align:right}"
        "th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}"
        ".spark{border:1px solid #d0d7de;border-radius:6px;margin:.5rem 0;padding:.25rem}"
        ".muted{color:#656d76}</style></head><body>",
        f"<h1>Research run: {esc(run_name)}</h1>",
        f"<p class='banner'>{esc(_BANNER)}</p>",
    ]
    if alerts:
        body = "⚠ LIQUIDATION DETECTED — LEAN issued a margin-call / forced-liquidation order:<br>"
        body += "<br>".join(esc(a) for a in alerts)
        parts.append(f"<div class='ruin'>{body}</div>")
    parts.append(
        f"<p class='muted'>harness <code>{esc(version)}</code> · generated "
        f"{esc(generated_at.isoformat())} · {len(rows)} instrument(s)</p>"
    )
    parts.append(
        "<table><thead><tr><th>Symbol</th><th>Strategy</th><th>Start</th><th>End</th>"
        "<th>Bars</th><th>Total return</th><th>CAGR</th><th>Max DD</th><th>P&amp;L</th>"
        "</tr></thead><tbody>"
    )
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


def _result_json(
    run_name: str,
    version: str,
    generated_at: datetime,
    rows: list[ReportRow],
    alerts: tuple[str, ...] = (),
) -> str:
    payload = {
        "run_name": run_name,
        "harness_version": version,
        "generated_at": generated_at.isoformat(),
        "resolution": "daily",
        "authoritative": bool(rows) and all(r.fill == "lean" for r in rows),
        "liquidation_alerts": list(alerts),
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
    alerts: tuple[str, ...] = (),
) -> Path:
    """Write ``report.md`` + ``report.html`` + ``result.json`` into ``run_dir``.

    ``alerts`` (e.g. LEAN-native margin-call summaries from the ``engine: lean``
    path) render an impossible-to-miss RED banner (design §6.2). Returns the path
    to ``report.html`` (the operator-facing artifact).
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.md").write_text(
        _markdown(run_name, harness_version, generated_at, rows, alerts), encoding="utf-8"
    )
    html_path = run_dir / "report.html"
    html_path.write_text(
        _html(run_name, harness_version, generated_at, rows, alerts), encoding="utf-8"
    )
    (run_dir / "result.json").write_text(
        _result_json(run_name, harness_version, generated_at, rows, alerts), encoding="utf-8"
    )
    return html_path


# =========================================================================== #
# P3 leverage / ruin report (design §6.2 + §6.5) — make liquidation un-missable
# =========================================================================== #
def _fmt_ratio(x: float) -> str:
    return f"{x:.2f}" if math.isfinite(x) else "n/a"


def _fmt_lev(x: float) -> str:
    return f"{x:.2f}x" if math.isfinite(x) else "n/a"


def _fmt_pct_unsigned(x: float) -> str:
    """Render a non-negative magnitude / probability as ``XX.XX%`` (no leading sign).

    Vols, vol-drag, and ruin probabilities are non-negative; a ``+100.00%`` risk-of-
    ruin reads oddly, so these columns drop the sign (``100.00%``). CAGR keeps its
    sign (it can be negative) via :func:`_fmt_pct`.
    """
    return f"{x * 100:.2f}%" if math.isfinite(x) else "n/a"


def _finite_curve(arr: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    """Forward-fill non-finite values so a leverage sparkline holds through a wipeout."""
    out = np.array(arr, dtype=np.float64, copy=True)
    last = 0.0
    for i in range(out.shape[0]):
        if math.isfinite(out[i]):
            last = float(out[i])
        else:
            out[i] = last
    return out


@dataclass(frozen=True, slots=True)
class LeverageRow:
    """One leverage level in a sweep: its ruin metrics + liquidation evidence.

    ``metrics`` is ``RiskMetrics.to_report_dict()``. ``liquidated`` is the daily
    intrabar-estimator verdict (``n_liquidation_flags`` flagged bars from
    ``research.risk.liquidation``); ``lean_margin_events`` counts LEAN-native
    margin-call orders (``research.lean.results``). Either ⇒ the red banner fires.
    """

    leverage_cap: float
    scheme: str
    metrics: dict[str, float]
    liquidated: bool
    first_liquidation_date: date | None
    n_liquidation_flags: int
    bars_evaluated: int
    lean_margin_events: int
    residual_uncertainty: str
    leverage_curve: npt.NDArray[np.float64] = field(repr=False)

    @property
    def flagged(self) -> bool:
        return self.liquidated or self.lean_margin_events > 0


_LEVERAGE_BANNER = (
    "P3 leverage / ruin report — RESEARCH-ONLY, NON-AUTHORITATIVE. The leverage cap "
    "binds at SIZING time; realized leverage can drift above it as equity erodes. "
    "Liquidation flags are the DAILY intrabar ESTIMATOR (a flag-for-confirmation, not "
    "a verdict) and/or LEAN-native margin-call orders. Confirm flagged days at minute "
    "resolution (design §6.3 → P5, deferred)."
)


def _ruin_banner_lines(rows: list[LeverageRow]) -> list[str]:
    """The red-banner body when any leverage level would have been liquidated."""
    flagged = [r for r in rows if r.flagged]
    if not flagged:
        return []
    lines = ["⚠ LIQUIDATION DETECTED — this leverage would have wiped the account:"]
    for r in flagged:
        first = r.first_liquidation_date.isoformat() if r.first_liquidation_date else "n/a"
        src = []
        if r.n_liquidation_flags:
            src.append(f"{r.n_liquidation_flags} intrabar-estimator day(s)")
        if r.lean_margin_events:
            src.append(f"{r.lean_margin_events} LEAN margin-call order(s)")
        lines.append(
            f"  • {r.leverage_cap:g}x ({r.scheme}): first breach {first} — {', '.join(src)}"
        )
    return lines


_LEV_COLUMNS = (
    ("leverage_cap", "Cap"),
    ("peak_leverage", "Peak lev"),
    ("cagr", "CAGR"),
    ("annualized_vol", "Ann vol"),
    ("sharpe", "Sharpe"),
    ("max_drawdown_pct", "Max DD %"),
    ("max_drawdown_dollars", "Max DD $"),
    ("volatility_drag", "Vol drag"),
    ("p_drawdown_gt_50pct", "P(dd>50%)"),
    ("risk_of_ruin", "Risk of ruin"),
    ("fractional_kelly", "Frac Kelly"),
)


def _lev_cell(row: LeverageRow, key: str) -> str:
    if key == "leverage_cap":
        return f"{row.leverage_cap:g}x"
    if key == "peak_leverage":
        return _fmt_lev(row.metrics.get("peak_leverage", float("nan")))
    if key == "fractional_kelly":
        return _fmt_lev(row.metrics.get("fractional_kelly", float("nan")))
    value = row.metrics.get(key, float("nan"))
    if key == "cagr":
        return _fmt_pct(value)  # signed — CAGR can be negative
    if key in ("annualized_vol", "volatility_drag", "p_drawdown_gt_50pct", "risk_of_ruin"):
        return _fmt_pct_unsigned(value)  # non-negative magnitudes / probabilities
    if key == "max_drawdown_pct":
        return _fmt_dd(value)
    if key == "max_drawdown_dollars":
        return _fmt_money(value)
    return _fmt_ratio(value)


def _leverage_markdown(
    run_name: str, version: str, generated_at: datetime, symbol: str, rows: list[LeverageRow]
) -> str:
    lines = [f"# Leverage / ruin sweep: {run_name}", "", f"> {_LEVERAGE_BANNER}", ""]
    banner = _ruin_banner_lines(rows)
    if banner:
        lines += ["> 🟥 **" + banner[0] + "**", *[f"> {b}" for b in banner[1:]], ""]
    lines += [
        f"- harness version: `{version}`",
        f"- generated: {generated_at.isoformat()}",
        f"- instrument: {symbol}",
        f"- leverage levels: {len(rows)}",
        "",
        "| " + " | ".join(label for _, label in _LEV_COLUMNS) + " | Liq days | Liquidated |",
        "|" + "---|" * (len(_LEV_COLUMNS) + 2),
    ]
    for r in rows:
        cells = [_lev_cell(r, key) for key, _ in _LEV_COLUMNS]
        liq = "🟥 YES" if r.flagged else "no"
        lines.append("| " + " | ".join(cells) + f" | {r.n_liquidation_flags} | {liq} |")
    if any(r.flagged for r in rows):
        lines += ["", f"> Residual uncertainty: {rows[0].residual_uncertainty}"]
    lines.append("")
    return "\n".join(lines)


def _leverage_html(
    run_name: str, version: str, generated_at: datetime, symbol: str, rows: list[LeverageRow]
) -> str:
    esc = html.escape
    banner = _ruin_banner_lines(rows)
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>Leverage sweep: {esc(run_name)}</title>",
        "<style>body{font:14px/1.5 -apple-system,system-ui,sans-serif;margin:2rem;color:#1f2328}"
        ".banner{background:#fff8c5;border:1px solid #d4a72c;padding:.75rem 1rem;border-radius:6px}"
        ".ruin{background:#ffebe9;border:2px solid #cf222e;color:#82071e;padding:.75rem 1rem;"
        "border-radius:6px;margin:1rem 0;font-weight:600}"
        "table{border-collapse:collapse;width:100%;margin:1rem 0}"
        "th,td{border:1px solid #d0d7de;padding:.4rem .6rem;text-align:right}"
        "th:first-child,td:first-child{text-align:left}"
        ".liq{color:#cf222e;font-weight:700}.spark{border:1px solid #d0d7de;border-radius:6px;"
        "margin:.5rem 0;padding:.25rem}.muted{color:#656d76}</style></head><body>",
        f"<h1>Leverage / ruin sweep: {esc(run_name)}</h1>",
        f"<p class='banner'>{esc(_LEVERAGE_BANNER)}</p>",
    ]
    if banner:
        parts.append("<div class='ruin'>" + "<br>".join(esc(b) for b in banner) + "</div>")
    parts.append(
        f"<p class='muted'>harness <code>{esc(version)}</code> · generated "
        f"{esc(generated_at.isoformat())} · {esc(symbol)} · {len(rows)} leverage level(s)</p>"
    )
    header = "".join(f"<th>{esc(label)}</th>" for _, label in _LEV_COLUMNS)
    parts.append(
        f"<table><thead><tr>{header}<th>Liq days</th><th>Liquidated</th></tr></thead><tbody>"
    )
    for r in rows:
        cells = "".join(f"<td>{esc(_lev_cell(r, key))}</td>" for key, _ in _LEV_COLUMNS)
        liq = "<span class='liq'>🟥 YES</span>" if r.flagged else "no"
        parts.append(f"<tr>{cells}<td>{r.n_liquidation_flags}</td><td>{liq}</td></tr>")
    parts.append("</tbody></table>")
    for r in rows:
        parts.append(
            f"<div class='spark'><div class='muted'>{esc(f'{r.leverage_cap:g}x')} leverage over time "
            f"(peak {_fmt_lev(r.metrics.get('peak_leverage', float('nan')))})</div>"
            f"{_sparkline_svg(_finite_curve(r.leverage_curve))}</div>"
        )
    if banner:
        parts.append(
            f"<p class='muted'>Residual uncertainty: {esc(rows[0].residual_uncertainty)}</p>"
        )
    parts.append("</body></html>")
    return "".join(parts)


def _leverage_result_json(
    run_name: str, version: str, generated_at: datetime, symbol: str, rows: list[LeverageRow]
) -> str:
    payload = {
        "run_name": run_name,
        "harness_version": version,
        "generated_at": generated_at.isoformat(),
        "report_type": "leverage_sweep",
        "authoritative": False,
        "symbol": symbol,
        "liquidation_detected": any(r.flagged for r in rows),
        "residual_uncertainty": rows[0].residual_uncertainty if rows else "",
        "levels": [
            {
                "leverage_cap": r.leverage_cap,
                "scheme": r.scheme,
                "liquidated": r.flagged,
                "first_liquidation_date": (
                    r.first_liquidation_date.isoformat() if r.first_liquidation_date else None
                ),
                "n_liquidation_flags": r.n_liquidation_flags,
                "bars_evaluated": r.bars_evaluated,
                "lean_margin_events": r.lean_margin_events,
                "metrics": {
                    k: (round(v, 8) if math.isfinite(v) else None) for k, v in r.metrics.items()
                },
            }
            for r in rows
        ],
    }
    return json.dumps(payload, indent=2)


def write_leverage_report(
    run_dir: Path,
    *,
    run_name: str,
    harness_version: str,
    generated_at: datetime,
    symbol: str,
    rows: list[LeverageRow],
) -> Path:
    """Write the P3 leverage/ruin ``report.{md,html}`` + ``result.json`` into ``run_dir``.

    A RED banner fires (impossible to miss, design §6.2) whenever any leverage level
    would have been liquidated — by the daily intrabar estimator or a LEAN-native
    margin call. Returns the path to ``report.html``.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.md").write_text(
        _leverage_markdown(run_name, harness_version, generated_at, symbol, rows), encoding="utf-8"
    )
    html_path = run_dir / "report.html"
    html_path.write_text(
        _leverage_html(run_name, harness_version, generated_at, symbol, rows), encoding="utf-8"
    )
    (run_dir / "result.json").write_text(
        _leverage_result_json(run_name, harness_version, generated_at, symbol, rows),
        encoding="utf-8",
    )
    return html_path
