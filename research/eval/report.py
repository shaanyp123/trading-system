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
from typing import TYPE_CHECKING

import numpy as np
import numpy.typing as npt

from research.data.contract_specs import (
    COSTS_AS_OF,
    COSTS_TOLERANCE_PER_SIDE,
    ETF_COMMISSION_MIN_PER_ORDER,
    ETF_COMMISSION_PER_SHARE,
    FILL_CONVENTION,
    SPECS,
)

if TYPE_CHECKING:
    from research.eval.compare import ComparisonRow
    from research.eval.sweep import SweepReport


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


def _cost_assumption_lines() -> list[str]:
    """The cost/fill assumptions behind every reported number (charter PR B).

    Rendered from ``research/data/contract_specs.py`` (the canonical cost
    table) so the surfaced assumptions can never drift from what the engines
    actually charge. Applies to LEAN-fill rows; numpy-screen rows (fill=close)
    carry no cost model and say so.
    """
    futures = [s for s in SPECS.values() if s.asset_class == "future"]
    commissions = ", ".join(
        f"{s.symbol} ${s.commission_per_side}"
        for s in sorted(futures, key=lambda s: s.symbol)
        if s.commission_per_side is not None
    )
    slippage = ", ".join(
        f"{s.symbol} {s.slippage_ticks} tick (${s.slippage_per_side})"
        for s in sorted(futures, key=lambda s: s.symbol)
    )
    return [
        f"cost model (lean-fill rows; as of {COSTS_AS_OF}, "
        f"±${COSTS_TOLERANCE_PER_SIDE}/side): futures all-in commission/side "
        f"{commissions}; ETFs ${ETF_COMMISSION_PER_SHARE}/share "
        f"min ${ETF_COMMISSION_MIN_PER_ORDER}/order (LEAN IB model, "
        f"probe-verified)",
        f"slippage (adverse, per side): futures {slippage}; ETFs none",
        f"fill convention: futures — {FILL_CONVENTION['future']}; ETFs — {FILL_CONVENTION['etf']}",
        "numpy-screen rows (fill=close) model NO costs — screen only, never authority",
    ]


def _cost_model_json() -> dict[str, object]:
    """Machine view of :func:`_cost_assumption_lines` for ``result.json``."""
    futures = sorted(
        (s for s in SPECS.values() if s.asset_class == "future"), key=lambda s: s.symbol
    )
    return {
        "as_of": COSTS_AS_OF,
        "tolerance_per_side_usd": str(COSTS_TOLERANCE_PER_SIDE),
        "futures_commission_per_side_usd": {s.symbol: str(s.commission_per_side) for s in futures},
        "futures_slippage_ticks": {s.symbol: s.slippage_ticks for s in futures},
        "etf_commission_per_share_usd": str(ETF_COMMISSION_PER_SHARE),
        "etf_commission_min_per_order_usd": str(ETF_COMMISSION_MIN_PER_ORDER),
        "fill_convention": dict(FILL_CONVENTION),
        "applies_to": "lean-fill rows (numpy-screen rows model no costs)",
    }


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
        # Wording covers BOTH ruin signals (run.lean_ruin_alert): a tagged LEAN
        # margin-call order and/or an equity wipe with no tagged order.
        lines += [
            "> 🟥 **⚠ LIQUIDATION DETECTED — LEAN margin-call order and/or account "
            "equity wipe detected:**",
            *[f"> {a}" for a in alerts],
            "",
        ]
    lines += [
        f"- harness version: `{version}`",
        f"- generated: {generated_at.isoformat()}",
        f"- instruments: {len(rows)}",
        *[f"- {line}" for line in _cost_assumption_lines()],
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
        # Same both-signals wording as the markdown banner (margin-call order
        # and/or equity wipe — see run.lean_ruin_alert).
        body = (
            "⚠ LIQUIDATION DETECTED — LEAN margin-call order and/or account equity "
            "wipe detected:<br>"
        )
        body += "<br>".join(esc(a) for a in alerts)
        parts.append(f"<div class='ruin'>{body}</div>")
    parts.append(
        f"<p class='muted'>harness <code>{esc(version)}</code> · generated "
        f"{esc(generated_at.isoformat())} · {len(rows)} instrument(s)</p>"
    )
    parts.append(
        "<p class='muted'>" + "<br>".join(esc(line) for line in _cost_assumption_lines()) + "</p>"
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
        "cost_model": _cost_model_json(),
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


# =========================================================================== #
# P4 validity report (design §7) — rank on OOS, show degradation + the MT haircut
# =========================================================================== #
_VALIDITY_BANNER = (
    "P4 validity report — RESEARCH-ONLY, NON-AUTHORITATIVE. Combos are ranked on "
    "WALK-FORWARD OUT-OF-SAMPLE performance (never in-sample); IS→OOS degradation and "
    "a multiple-testing reality-check are shown so a lucky/overfit winner is visible. "
    "Confirm the top candidate in LEAN (the authority) before acting (design D1)."
)


def _fmt_score(x: float) -> str:
    return f"{x:.3f}" if math.isfinite(x) else "n/a"


def _mt_lines(sweep: SweepReport) -> list[str]:
    """The multiple-testing reality-check sentences (design §7)."""
    best = sweep.best
    best_oos = _fmt_score(best.oos_score) if best else "n/a"
    null = _fmt_score(sweep.expected_max_under_null)
    verdict = (
        "⚠ the best OOS score is WITHIN what random chance would produce over this many "
        "trials — treat it as likely overfit, not an edge."
        if sweep.likely_overfit
        else "the best OOS score EXCEEDS the multiple-testing null — a tentative edge "
        "(still confirm in LEAN)."
    )
    return [
        f"Tried {sweep.n_trials} combo(s) over {sweep.n_oos_obs} OOS bar(s). "
        f"Best OOS {sweep.metric_name} = {best_oos}; a pure-noise winner over "
        f"{sweep.n_trials} trials would be expected to post ≈ {null}.",
        verdict,
    ]


def _validity_markdown(
    run_name: str,
    version: str,
    generated_at: datetime,
    symbol: str,
    sweep: SweepReport,
    comparison: tuple[ComparisonRow, ...],
) -> str:
    lines = [f"# Validity / walk-forward sweep: {run_name}", "", f"> {_VALIDITY_BANNER}", ""]
    if sweep.likely_overfit:
        lines += ["> 🟥 **LIKELY OVERFIT** — " + _mt_lines(sweep)[0], ""]
    lines += [
        f"- harness version: `{version}`",
        f"- generated: {generated_at.isoformat()}",
        f"- instrument: {symbol}",
        f"- ranked on: walk-forward OOS {sweep.metric_name}",
        "",
        "## Multiple-testing reality-check",
        "",
        *[f"- {line}" for line in _mt_lines(sweep)],
        "",
        f"## Candidates (ranked on OOS {sweep.metric_name})",
        "",
        f"| Rank | Params | IS {sweep.metric_name} | OOS {sweep.metric_name} | IS→OOS drop | Folds |",
        "|---:|---|---:|---:|---:|---:|",
    ]
    for rank, row in enumerate(sweep.rows, start=1):
        lines.append(
            f"| {rank} | {row.label} | {_fmt_score(row.is_score)} | {_fmt_score(row.oos_score)} | "
            f"{_fmt_score(row.degradation)} | {row.n_folds} |"
        )
    if comparison:
        lines += [
            "",
            "## Vs benchmarks (full sample, normalized)",
            "",
            "| Strategy | Role | Total return | CAGR | Sharpe | Max DD |",
            "|---|---|---:|---:|---:|---:|",
        ]
        for c in comparison:
            lines.append(
                f"| {c.label} | {c.role} | {_fmt_pct(c.total_return)} | {_fmt_pct(c.cagr)} | "
                f"{_fmt_score(c.sharpe)} | {_fmt_dd(c.max_drawdown_pct)} |"
            )
    lines.append("")
    return "\n".join(lines)


def _validity_html(
    run_name: str,
    version: str,
    generated_at: datetime,
    symbol: str,
    sweep: SweepReport,
    comparison: tuple[ComparisonRow, ...],
) -> str:
    esc = html.escape
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>Validity sweep: {esc(run_name)}</title>",
        "<style>body{font:14px/1.5 -apple-system,system-ui,sans-serif;margin:2rem;color:#1f2328}"
        ".banner{background:#fff8c5;border:1px solid #d4a72c;padding:.75rem 1rem;border-radius:6px}"
        ".ruin{background:#ffebe9;border:2px solid #cf222e;color:#82071e;padding:.75rem 1rem;"
        "border-radius:6px;margin:1rem 0;font-weight:600}"
        ".mt{background:#ddf4ff;border:1px solid #54aeff;padding:.5rem 1rem;border-radius:6px;margin:1rem 0}"
        "table{border-collapse:collapse;width:100%;margin:1rem 0}"
        "th,td{border:1px solid #d0d7de;padding:.4rem .6rem;text-align:right}"
        "th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}"
        ".muted{color:#656d76}</style></head><body>",
        f"<h1>Validity / walk-forward sweep: {esc(run_name)}</h1>",
        f"<p class='banner'>{esc(_VALIDITY_BANNER)}</p>",
    ]
    if sweep.likely_overfit:
        parts.append("<div class='ruin'>LIKELY OVERFIT — " + esc(_mt_lines(sweep)[0]) + "</div>")
    parts.append(
        f"<p class='muted'>harness <code>{esc(version)}</code> · generated "
        f"{esc(generated_at.isoformat())} · {esc(symbol)} · ranked on walk-forward OOS "
        f"{esc(sweep.metric_name)}</p>"
    )
    parts.append(
        "<div class='mt'>" + "<br>".join(esc(line) for line in _mt_lines(sweep)) + "</div>"
    )
    header = (
        f"<th>Rank</th><th>Params</th><th>IS {esc(sweep.metric_name)}</th>"
        f"<th>OOS {esc(sweep.metric_name)}</th><th>IS&rarr;OOS drop</th><th>Folds</th>"
    )
    parts.append(f"<table><thead><tr>{header}</tr></thead><tbody>")
    for rank, row in enumerate(sweep.rows, start=1):
        parts.append(
            f"<tr><td>{rank}</td><td>{esc(row.label)}</td><td>{_fmt_score(row.is_score)}</td>"
            f"<td>{_fmt_score(row.oos_score)}</td><td>{_fmt_score(row.degradation)}</td>"
            f"<td>{row.n_folds}</td></tr>"
        )
    parts.append("</tbody></table>")
    if comparison:
        parts.append("<h2>Vs benchmarks (full sample, normalized)</h2>")
        parts.append(
            "<table><thead><tr><th>Strategy</th><th>Role</th><th>Total return</th>"
            "<th>CAGR</th><th>Sharpe</th><th>Max DD</th></tr></thead><tbody>"
        )
        for c in comparison:
            parts.append(
                f"<tr><td>{esc(c.label)}</td><td>{esc(c.role)}</td><td>{_fmt_pct(c.total_return)}</td>"
                f"<td>{_fmt_pct(c.cagr)}</td><td>{_fmt_score(c.sharpe)}</td>"
                f"<td>{_fmt_dd(c.max_drawdown_pct)}</td></tr>"
            )
        parts.append("</tbody></table>")
    parts.append("</body></html>")
    return "".join(parts)


def _validity_result_json(
    run_name: str,
    version: str,
    generated_at: datetime,
    symbol: str,
    sweep: SweepReport,
    comparison: tuple[ComparisonRow, ...],
) -> str:
    payload = {
        "run_name": run_name,
        "harness_version": version,
        "generated_at": generated_at.isoformat(),
        "report_type": "validity_walk_forward",
        "authoritative": False,
        "symbol": symbol,
        "ranked_on": f"walk_forward_oos_{sweep.metric_name}",
        "multiple_testing": {
            "n_trials": sweep.n_trials,
            "n_oos_obs": sweep.n_oos_obs,
            "expected_max_under_null": (
                round(sweep.expected_max_under_null, 6)
                if math.isfinite(sweep.expected_max_under_null)
                else None
            ),
            "likely_overfit": sweep.likely_overfit,
        },
        "ranking": [
            {
                "rank": rank,
                "params": row.label,
                "is_score": round(row.is_score, 6) if math.isfinite(row.is_score) else None,
                "oos_score": round(row.oos_score, 6) if math.isfinite(row.oos_score) else None,
                "degradation": (
                    round(row.degradation, 6) if math.isfinite(row.degradation) else None
                ),
                "n_folds": row.n_folds,
            }
            for rank, row in enumerate(sweep.rows, start=1)
        ],
        "benchmarks": [
            {
                "label": c.label,
                "role": c.role,
                "total_return": round(c.total_return, 8),
                "cagr": round(c.cagr, 8) if math.isfinite(c.cagr) else None,
                "sharpe": round(c.sharpe, 6) if math.isfinite(c.sharpe) else None,
                "max_drawdown_pct": round(c.max_drawdown_pct, 8),
            }
            for c in comparison
        ],
    }
    return json.dumps(payload, indent=2)


def write_validity_report(
    run_dir: Path,
    *,
    run_name: str,
    harness_version: str,
    generated_at: datetime,
    symbol: str,
    sweep: SweepReport,
    comparison: tuple[ComparisonRow, ...] = (),
) -> Path:
    """Write the P4 validity ``report.{md,html}`` + ``result.json`` into ``run_dir``.

    Candidates are ranked on walk-forward OOS; a blue multiple-testing note (and a RED
    "LIKELY OVERFIT" banner when the best OOS score is within the null) keeps the
    reader honest (design §7). Returns the path to ``report.html``.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.md").write_text(
        _validity_markdown(run_name, harness_version, generated_at, symbol, sweep, comparison),
        encoding="utf-8",
    )
    html_path = run_dir / "report.html"
    html_path.write_text(
        _validity_html(run_name, harness_version, generated_at, symbol, sweep, comparison),
        encoding="utf-8",
    )
    (run_dir / "result.json").write_text(
        _validity_result_json(run_name, harness_version, generated_at, symbol, sweep, comparison),
        encoding="utf-8",
    )
    return html_path
