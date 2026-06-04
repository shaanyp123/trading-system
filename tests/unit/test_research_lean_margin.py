"""LEAN parser: array-shape equity (the real-engine regression) + margin events.

The committed fixtures cover the dict ``{x,y}`` / ``{x,…,close}`` point shapes; the
operator's ``quantconnect/lean:latest`` image actually emits ARRAY points
``[ts, o, h, l, c]`` (which broke the §6.6 parity rail on first real run). These
tests pin both the array shape and the LEAN-native margin-call surfacing (design
§6.2) without needing Docker/LEAN.
"""

from __future__ import annotations

import json
from pathlib import Path

from research.lean.results import parse_lean_result, parse_margin_events


def _write_result(tmp_path: Path, obj: dict[str, object]) -> Path:
    out = tmp_path / "output"
    out.mkdir()
    (out / "Algo.json").write_text(json.dumps(obj), encoding="utf-8")
    return out


# 2025-07-01 / 2025-07-02 in unix seconds.
_TS0, _TS1 = 1751342400, 1751428800


def test_parses_array_form_candlestick_equity(tmp_path: Path) -> None:
    obj = {
        "charts": {
            "Strategy Equity": {
                "series": {
                    "Equity": {
                        # [ts, open, high, low, close] — the real LEAN 2026 shape.
                        "values": [
                            [_TS0, 100000.0, 100000.0, 100000.0, 100000.0],
                            [_TS1, 100000.0, 100000.0, 95000.0, 98000.0],
                        ]
                    }
                }
            }
        },
        "orders": {},
        "statistics": {},
    }
    parsed = parse_lean_result(
        _write_result(tmp_path, obj), symbol="TLT", multiplier=1.0, strategy_name="t"
    )
    # close (last array element) is the equity value.
    assert parsed.result.equity_curve.tolist() == [100000.0, 98000.0]
    assert parsed.margin_events == ()
    assert parsed.liquidated is False


def test_parses_array_form_line_equity(tmp_path: Path) -> None:
    obj = {
        "charts": {
            "Strategy Equity": {
                "series": {"Equity": {"values": [[_TS0, 100000.0], [_TS1, 101000.0]]}}
            }
        }
    }
    parsed = parse_lean_result(
        _write_result(tmp_path, obj), symbol="TLT", multiplier=1.0, strategy_name="t"
    )
    assert parsed.result.equity_curve.tolist() == [100000.0, 101000.0]


def test_margin_call_order_surfaces_as_event() -> None:
    obj = {
        "orders": {
            "1": {
                "status": 3,
                "symbol": {"value": "TLT"},
                "quantity": 10,
                "lastFillTime": "2025-07-01T20:00:00Z",
                "tag": "",
            },
            "2": {
                "status": 3,
                "symbol": {"value": "TLT"},
                "quantity": -10,
                "lastFillTime": "2025-07-02T20:00:00Z",
                "tag": "Margin Call",
            },
        }
    }
    events = parse_margin_events(obj)
    assert len(events) == 1
    assert events[0].market == "TLT"
    assert events[0].quantity == -10
    assert "margin call" in events[0].tag.lower()


def test_no_margin_events_on_clean_run() -> None:
    obj = {
        "orders": {
            "1": {
                "status": 3,
                "symbol": {"value": "TLT"},
                "quantity": 1,
                "tag": "",
                "time": "2025-07-01T20:00:00Z",
            }
        }
    }
    assert parse_margin_events(obj) == ()
