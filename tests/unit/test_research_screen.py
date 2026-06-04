"""Optional vectorbt seam degrades gracefully (design D2)."""

from __future__ import annotations

import pytest

from research.screen.vbt_screen import daily_sweep, require_vectorbt, vectorbt_available


def test_availability_is_bool() -> None:
    assert isinstance(vectorbt_available(), bool)


def test_require_matches_availability() -> None:
    if vectorbt_available():
        require_vectorbt()  # no raise
    else:
        with pytest.raises(RuntimeError, match="vectorbt is not installed"):
            require_vectorbt()


def test_daily_sweep_is_gated() -> None:
    # Absent ⇒ RuntimeError (install hint); present ⇒ NotImplementedError (P4).
    with pytest.raises((RuntimeError, NotImplementedError)):
        daily_sweep()
