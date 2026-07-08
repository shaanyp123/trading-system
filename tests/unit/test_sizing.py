"""Unit tests for the crypto nano-contract sizing pipeline (C0-B4, §3.4).

Pure-function tests: no DB, no mocks (A22 N/A). The pipeline is the
risk-side Decimal re-check of the signal engine's targets; in normal
operation every stage is a no-op — these tests pin both the no-op path
and each clamp stage.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from services.risk.sizing import (
    DEFAULT_GROSS_CAP_FRAC,
    DEFAULT_PER_ASSET_CAP_FRAC,
    SIZING_TRACE_SCHEMA_VERSION,
    CryptoProductSpec,
    SizingError,
    SizingInputs,
    run_sizing_pipeline,
)

BTC = CryptoProductSpec(asset="BTC", product_id="BIP-TEST-CDE", contract_size=Decimal("0.01"))
ETH = CryptoProductSpec(asset="ETH", product_id="EIP-TEST-CDE", contract_size=Decimal("0.10"))


def _inputs(**over: object) -> SizingInputs:
    base: dict[str, object] = dict(
        equity_usd=Decimal("6000"),
        m_combined=Decimal("1"),
        engine_targets={"BTC": 3, "ETH": -5},
        marks={"BTC": Decimal("100000"), "ETH": Decimal("3500")},
        specs={"BTC": BTC, "ETH": ETH},
    )
    base.update(over)
    return SizingInputs(**base)  # type: ignore[arg-type]


class TestNoOpPath:
    def test_within_caps_passes_through_unclamped(self) -> None:
        result = run_sizing_pipeline(_inputs())
        assert result.target_contracts == {"BTC": 3, "ETH": -5}
        assert result.clamped is False
        assert result.sizing_trace["schema_version"] == SIZING_TRACE_SCHEMA_VERSION
        assert result.sizing_trace["final_targets"] == {"BTC": 3, "ETH": -5}
        # Decimals as strings in the trace ([A05])
        assert result.sizing_trace["equity_usd"] == "6000"
        assert result.sizing_trace["marks"]["BTC"] == "100000"

    def test_flat_book_is_flat(self) -> None:
        result = run_sizing_pipeline(_inputs(engine_targets={"BTC": 0, "ETH": 0}))
        assert result.target_contracts == {"BTC": 0, "ETH": 0}
        assert result.clamped is False


class TestStageM:
    def test_m_combined_scales_and_floors(self) -> None:
        result = run_sizing_pipeline(_inputs(m_combined=Decimal("0.5")))
        # 3 -> floor(1.5)=1; -5 -> -floor(2.5)=-2
        assert result.target_contracts == {"BTC": 1, "ETH": -2}
        assert result.clamped is True
        assert result.sizing_trace["stages"]["m_combined"] == {"BTC": 1, "ETH": -2}


class TestStageA:
    def test_per_asset_cap_clamps(self) -> None:
        # 20 BTC contracts * 0.01 * 100000 = $20,000 > 1.4*6000 = $8,400
        result = run_sizing_pipeline(_inputs(engine_targets={"BTC": 20, "ETH": 0}))
        # floor(8400 / 1000) = 8
        assert result.target_contracts["BTC"] == 8
        assert result.clamped is True

    def test_short_side_clamps_symmetrically(self) -> None:
        result = run_sizing_pipeline(_inputs(engine_targets={"BTC": -20, "ETH": 0}))
        assert result.target_contracts["BTC"] == -8


class TestStageG:
    def test_gross_cap_scales_proportionally(self) -> None:
        # BTC 8 * $1000 = 8000; ETH -20 * $350 = 7000 → gross 15000 > 3*6000=18000? no.
        # Use tighter equity so the cap binds: equity 4000 → gross cap 12000.
        result = run_sizing_pipeline(
            _inputs(equity_usd=Decimal("4000"), engine_targets={"BTC": 5, "ETH": -16})
        )
        # per-asset cap = 5600: BTC 5*1000=5000 ok; ETH 16*350=5600 ok.
        # gross = 10600 ≤ 12000 → no clamp.
        assert result.target_contracts == {"BTC": 5, "ETH": -16}
        result2 = run_sizing_pipeline(
            _inputs(equity_usd=Decimal("3000"), engine_targets={"BTC": 4, "ETH": -12})
        )
        # per-asset cap = 4200: BTC 4000 ok, ETH 4200 ok. gross = 8200 > 9000? no (3*3000=9000).
        assert result2.clamped is False
        result3 = run_sizing_pipeline(
            _inputs(equity_usd=Decimal("2000"), engine_targets={"BTC": 2, "ETH": -8})
        )
        # per-asset cap = 2800: BTC 2000 ok, ETH 2800 ok. gross 4800 ≤ 6000 → ok.
        assert result3.clamped is False

    def test_gross_cap_actually_binds(self) -> None:
        inputs = _inputs(
            equity_usd=Decimal("1000"),
            engine_targets={
                "BTC": 1,
                "ETH": -8,
            },  # per-asset cap 1400: BTC 1000 ok, ETH... 2800>1400
        )
        result = run_sizing_pipeline(inputs)
        # ETH clamped by stage A to floor(1400/350)=4 → gross = 1000+1400=2400 ≤ 3000
        assert result.target_contracts == {"BTC": 1, "ETH": -4}
        # force gross binding: raise caps so only gross binds
        result2 = run_sizing_pipeline(
            _inputs(
                equity_usd=Decimal("1000"),
                engine_targets={"BTC": 2, "ETH": -8},
                per_asset_cap_frac=Decimal("10"),
            )
        )
        # notionals: BTC 2000, ETH 2800 → gross 4800 > 3000 → scale 0.625
        # BTC floor(2*0.625)=1; ETH floor(8*0.625)=5
        assert result2.target_contracts == {"BTC": 1, "ETH": -5}
        assert result2.clamped is True


class TestValidation:
    def test_rejects_non_positive_equity(self) -> None:
        with pytest.raises(SizingError, match="equity"):
            run_sizing_pipeline(_inputs(equity_usd=Decimal("0")))

    def test_rejects_bad_m_combined(self) -> None:
        with pytest.raises(SizingError, match="m_combined"):
            run_sizing_pipeline(_inputs(m_combined=Decimal("1.5")))
        with pytest.raises(SizingError, match="m_combined"):
            run_sizing_pipeline(_inputs(m_combined=Decimal("0")))

    def test_rejects_missing_mark_for_nonzero_target(self) -> None:
        with pytest.raises(SizingError, match="mark"):
            run_sizing_pipeline(_inputs(marks={"BTC": Decimal("100000")}))

    def test_missing_mark_ok_for_flat_target(self) -> None:
        result = run_sizing_pipeline(
            _inputs(engine_targets={"BTC": 3, "ETH": 0}, marks={"BTC": Decimal("100000")})
        )
        assert result.target_contracts == {"BTC": 3, "ETH": 0}

    def test_rejects_missing_spec(self) -> None:
        with pytest.raises(SizingError, match="spec"):
            run_sizing_pipeline(_inputs(specs={"BTC": BTC}))


class TestLockedConstants:
    def test_amendment_b_cap_fractions(self) -> None:
        assert DEFAULT_PER_ASSET_CAP_FRAC == Decimal("1.4")
        assert DEFAULT_GROSS_CAP_FRAC == Decimal("3.0")

    def test_caps_match_canonical_parameters(self) -> None:
        from services.risk.crypto_parameters import AMENDMENT_B_CANONICAL_PARAMETERS as P

        assert Decimal(P["PER_ASSET_CAP"]) == DEFAULT_PER_ASSET_CAP_FRAC
        assert Decimal(P["GROSS_CAP"]) == DEFAULT_GROSS_CAP_FRAC
