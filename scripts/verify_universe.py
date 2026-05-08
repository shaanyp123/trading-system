"""Verify which V1 candidate markets pass Stage 0 at a given equity tier.

Usage:
    python3 scripts/verify_universe.py --equity 15000
    python3 scripts/verify_universe.py --equity 25000 --include-mes-override
    python3 scripts/verify_universe.py --equity 50000 --json

Prints a table of every V1 candidate market with its 1-contract notional, the
50%-threshold at the requested equity, and PASS/FAIL per the Stage 0 rule
(backend-spec §2.4.1). Also prints a one-line summary: ``N markets pass at
$<equity>``.

Implementation notes:
- The candidate universe comes from ``strategies.v1_trend_following.parameters.
  V1_CANDIDATE_UNIVERSE`` (LOCKED 2026-05-05 — TLT/IEF/SHY/TIP for bond
  exposure, NOT /ZN/ZB/ZF/ZT as the original ``implementation-guide.md`` Day 6
  11:00 prompt suggested; see ``Docs/decisions-log.md`` Day 2 entry).
- 1-contract notionals are HARDCODED recent values matching the rationale
  comments in ``parameters.py``. Fetching live notionals from QC bundled data
  is a Phase 1 deliverable that depends on the QC adapter (Week 5+).
- The Stage 0 rule is invoked via ``services.risk.sizing._stage_0_universe_filter``
  so the verification logic is exactly what the live signal pipeline uses
  (single source of truth). The ``--include-mes-override`` flag activates the
  /MES single-contract override (parameters.py comment: 50%-override at $20k+).

Exit code: 0 always (this is a reporting tool, not a gate). Caller / CI can
parse the JSON output to assert "≥ 4 markets pass at $15k" if needed.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

# Allow direct invocation as ``python3 scripts/verify_universe.py`` (the form
# documented in implementation-guide.md §11 Day 7) by prepending the repo root
# to sys.path. ``python3 -m scripts.verify_universe`` works without this hack.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from services.risk.sizing import (  # noqa: E402 — sys.path bootstrap above
    STAGE_0_NOTIONAL_PCT,
    ContractMetadata,
    _stage_0_universe_filter,
)
from strategies.v1_trend_following.parameters import (  # noqa: E402
    V1_CANDIDATE_UNIVERSE,
)

# Hardcoded 1-contract notionals matching parameters.py's V1_CANDIDATE_UNIVERSE
# rationale comments. These are mid-2026 recent values; refresh when the QC
# adapter is wired (Phase 1 Week 5+) so the script can hit live data.
V1_NOTIONALS_USD: dict[str, Decimal] = {
    "/MES": Decimal("26000"),  # 5235 x $5/index = ~$26k
    "/MNQ": Decimal("36000"),  # 18000 x $2/index = ~$36k
    "/MYM": Decimal("20000"),  # 40000 x $0.50/index = ~$20k
    "/M2K": Decimal("10000"),  # 2000 x $5/index = ~$10k
    "/MGC": Decimal("24000"),  # $2400/oz x 10 oz = ~$24k
    "/MCL": Decimal("8000"),  # $80/bbl x 100 bbl = ~$8k
    "/MBT": Decimal("10000"),  # $100k/BTC x 0.1 BTC = ~$10k
    "TLT": Decimal("90"),  # per share
    "IEF": Decimal("90"),  # per share
    "SHY": Decimal("80"),  # per share
    "TIP": Decimal("100"),  # per share
}

V1_CLUSTERS: dict[str, str] = {
    "/MES": "equity_index",
    "/MNQ": "equity_index",
    "/MYM": "equity_index",
    "/M2K": "equity_index",
    "/MGC": "commodity",
    "/MCL": "commodity",
    "/MBT": "crypto",
    "TLT": "rates",
    "IEF": "rates",
    "SHY": "rates",
    "TIP": "rates",
}

# Default override map — /MES bypasses Stage 0 at equity >= $20k per the locked
# decision in parameters.py (backend-spec §2.4.1 Stage 2 50%-override).
DEFAULT_SINGLE_CONTRACT_OVERRIDES: dict[str, Decimal] = {
    "/MES": Decimal("20000"),
}


def _build_metadata() -> dict[str, ContractMetadata]:
    out: dict[str, ContractMetadata] = {}
    for market in V1_CANDIDATE_UNIVERSE:
        if market not in V1_NOTIONALS_USD:
            raise SystemExit(
                f"missing notional for locked V1 market {market}; update "
                f"V1_NOTIONALS_USD in scripts/verify_universe.py"
            )
        out[market] = ContractMetadata(
            market=market,
            single_contract_notional=V1_NOTIONALS_USD[market],
            cluster=V1_CLUSTERS[market],
            min_lot=1,
        )
    return out


def _format_decimal_currency(value: Decimal) -> str:
    """Format a Decimal as ``$N,NNN`` with comma separators (no decimals)."""
    return f"${int(value):,}"


def _print_text_table(
    *,
    equity: Decimal,
    metadata: dict[str, ContractMetadata],
    active: tuple[str, ...],
    overrides_applied: tuple[str, ...],
) -> None:
    threshold = STAGE_0_NOTIONAL_PCT * equity
    active_set = frozenset(active)
    override_set = frozenset(overrides_applied)

    print(f"\nStage 0 verification at equity = {_format_decimal_currency(equity)}")
    print(
        f"Threshold (50% x equity) = {_format_decimal_currency(threshold)}; "
        f"single-contract overrides: {sorted(override_set) or 'none'}\n"
    )

    header = f"{'Market':<8}  {'1-Contract':>12}  {'50%-Thresh':>12}  {'Override':>9}  Result"
    print(header)
    print("-" * len(header))

    for market in V1_CANDIDATE_UNIVERSE:
        meta = metadata[market]
        notional_str = _format_decimal_currency(meta.single_contract_notional)
        threshold_str = _format_decimal_currency(threshold)
        override_flag = "yes" if market in override_set else "no"
        if market in active_set:
            result = "PASS"
        else:
            result = "FAIL"
        print(f"{market:<8}  {notional_str:>12}  {threshold_str:>12}  {override_flag:>9}  {result}")

    n_pass = len(active_set)
    n_total = len(V1_CANDIDATE_UNIVERSE)
    print(f"\nSummary: {n_pass}/{n_total} markets pass at {_format_decimal_currency(equity)}")


def _print_json_output(
    *,
    equity: Decimal,
    metadata: dict[str, ContractMetadata],
    active: tuple[str, ...],
    overrides_applied: tuple[str, ...],
) -> None:
    threshold = STAGE_0_NOTIONAL_PCT * equity
    active_set = frozenset(active)
    override_set = frozenset(overrides_applied)
    rows = []
    for market in V1_CANDIDATE_UNIVERSE:
        meta = metadata[market]
        rows.append(
            {
                "market": market,
                "single_contract_notional": str(meta.single_contract_notional),
                "threshold_50pct": str(threshold),
                "cluster": meta.cluster,
                "override_applied": market in override_set,
                "stage_0_pass": market in active_set,
            }
        )
    payload = {
        "equity": str(equity),
        "threshold_50pct": str(threshold),
        "single_contract_overrides_active": sorted(override_set),
        "n_pass": len(active_set),
        "n_total": len(V1_CANDIDATE_UNIVERSE),
        "markets": rows,
    }
    print(json.dumps(payload, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage 0 universe filter verification at a given equity tier",
        epilog=(
            "Examples:\n"
            "  python3 scripts/verify_universe.py --equity 15000\n"
            "  python3 scripts/verify_universe.py --equity 20000 --include-mes-override\n"
            "  python3 scripts/verify_universe.py --equity 50000 --json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--equity",
        type=Decimal,
        required=True,
        help="Equity in USD (e.g. 15000, 25000, 50000, 100000)",
    )
    parser.add_argument(
        "--include-mes-override",
        action="store_true",
        help=(
            "Activate the /MES single-contract override (parameters.py: 50%% "
            "override at $20k+). Off by default; on in production where the "
            "V1 strategy ships with this override mapped."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of the human-readable table",
    )
    args = parser.parse_args(argv)

    if args.equity <= 0:
        print("error: --equity must be positive", file=sys.stderr)
        return 2

    overrides = DEFAULT_SINGLE_CONTRACT_OVERRIDES if args.include_mes_override else {}

    metadata = _build_metadata()
    s0 = _stage_0_universe_filter(
        candidate_markets=tuple(V1_CANDIDATE_UNIVERSE),
        equity=args.equity,
        contracts_metadata=metadata,
        single_contract_overrides=overrides,
        evaluated_at_utc="",
    )

    if args.json:
        _print_json_output(
            equity=args.equity,
            metadata=metadata,
            active=s0.active_markets,
            overrides_applied=s0.overrides_applied,
        )
    else:
        _print_text_table(
            equity=args.equity,
            metadata=metadata,
            active=s0.active_markets,
            overrides_applied=s0.overrides_applied,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
