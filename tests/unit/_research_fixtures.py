"""LEAN on-disk fixture writers for research unit tests.

Writes the exact byte layout ``services/data/bar_sync.py`` produces so the
research daily loader is exercised against the real format — WITHOUT importing
``bar_sync`` (it pulls ``ib_async``, absent from the local/CI venv). Not a test
module (leading underscore ⇒ pytest never collects it); imported as
``tests.unit._research_fixtures``.
"""

from __future__ import annotations

import zipfile
from datetime import date
from pathlib import Path

# (session_date, open, high, low, close, volume)
DailyBar = tuple[date, float, float, float, float, float]


def _stem(symbol: str) -> str:
    return symbol.lstrip("/").lower()


def _write_zip(path: Path, member: str, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(member, body)


def write_equity_daily(data_root: Path, symbol: str, bars: list[DailyBar]) -> Path:
    """Write ``equity/usa/daily/<stem>.zip`` (deci-cent integer-scaled prices)."""
    stem = _stem(symbol)
    lines = [
        f"{d:%Y%m%d} 00:00,{round(o * 10000)},{round(h * 10000)},"
        f"{round(low * 10000)},{round(c * 10000)},{int(v)}"
        for (d, o, h, low, c, v) in bars
    ]
    path = data_root / "equity" / "usa" / "daily" / f"{stem}.zip"
    _write_zip(path, f"{stem}.csv", "\n".join(lines) + "\n")
    return path


def write_futures_daily(
    data_root: Path,
    symbol: str,
    expiry: str,
    bars: list[DailyBar],
    *,
    market_dir: str,
) -> Path:
    """Write ``future/<market_dir>/daily/<stem>_trade.zip`` (raw float prices).

    Appends the ``<stem>_trade_<expiry>.csv`` member, preserving any existing
    members so multiple expiries can be written into the same zip.
    """
    stem = _stem(symbol)
    member = f"{stem}_trade_{expiry}.csv"
    body = (
        "\n".join(f"{d:%Y%m%d} 00:00,{o},{h},{low},{c},{int(v)}" for (d, o, h, low, c, v) in bars)
        + "\n"
    )
    path = data_root / "future" / market_dir / "daily" / f"{stem}_trade.zip"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, str] = {}
    if path.exists():
        with zipfile.ZipFile(path, "r") as zf:
            existing = {n: zf.read(n).decode("utf-8") for n in zf.namelist() if n != member}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in existing.items():
            zf.writestr(name, content)
        zf.writestr(member, body)
    return path
