"""services/api/exposure.py — V1 universe exposure breakdown helpers.

Backend-spec §4.1.5b ``TodayDigestResponse.exposure``. Phase 1 (post-2026-05-27):
the home-page Exposure widget reads the real per-cluster notional + gross/net
NAV percentages computed from ``positions_current`` rows + the latest
``balances`` NAV row.

The market→(cluster, multiplier) mapping is sourced from the locked V1 universe:

* Cluster names mirror :data:`services.api.schemas.today.ExposureCluster`.
* Multipliers mirror the retired IBKR adapter's contract multipliers
  (the adapter itself was deleted in crypto-pivot C0-B2b; this whole V1
  metadata surface is retired by the §3.4 sizing PR).
* The ``contracts`` table is structurally meant to carry per-expiry rows
  (and has a ``multiplier`` column) but is empty today + has no ``cluster``
  column. Until that table is populated and extended, the static lookup
  below is the single source of truth for cluster + multiplier in the API.

Sidelined markets (``V1_SIDELINED_MARKETS`` per
``strategies/v1_trend_following/parameters.py``) are intentionally included
in the lookup so that re-enabling /MCL doesn't bit-rot the exposure path.

When a market lands in ``positions_current`` that is NOT in the lookup, the
fallback is ``equity_index`` cluster + ``multiplier=1`` + a structlog
``exposure_unknown_market`` warning so the operator can surface it for a
metadata fix.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

import structlog

from services.api.schemas.today import ExposureBreakdown, ExposureCluster

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class MarketExposureMeta:
    """Per-market exposure metadata: cluster + dollar-multiplier-per-point.

    For futures, ``multiplier`` is the point value (e.g., ``5`` for /MES
    means each index point is worth $5). For ETFs, ``multiplier = 1`` (one
    share quoted in $).
    """

    cluster: ExposureCluster
    multiplier: Decimal


#: Locked V1 universe → exposure metadata. Keys mirror
#: ``strategies.v1_trend_following.parameters.V1_CANDIDATE_UNIVERSE`` plus
#: the sidelined ``/MCL`` (preserved for re-enable per
#: ``V1_SIDELINED_MARKETS``).
V1_EXPOSURE_METADATA: Final[dict[str, MarketExposureMeta]] = {
    # CME / CBOT / COMEX micro futures
    "/MES": MarketExposureMeta(cluster="equity_index", multiplier=Decimal("5")),
    "/MNQ": MarketExposureMeta(cluster="equity_index", multiplier=Decimal("2")),
    "/MYM": MarketExposureMeta(cluster="equity_index", multiplier=Decimal("0.50")),
    "/M2K": MarketExposureMeta(cluster="equity_index", multiplier=Decimal("5")),
    "/MGC": MarketExposureMeta(cluster="commodity", multiplier=Decimal("10")),
    "/MCL": MarketExposureMeta(cluster="commodity", multiplier=Decimal("100")),
    "/MBT": MarketExposureMeta(cluster="crypto", multiplier=Decimal("0.1")),
    # NYSE bond ETFs — duration spectrum
    "TLT": MarketExposureMeta(cluster="rates_bonds", multiplier=Decimal("1")),
    "IEF": MarketExposureMeta(cluster="rates_bonds", multiplier=Decimal("1")),
    "SHY": MarketExposureMeta(cluster="rates_bonds", multiplier=Decimal("1")),
    "TIP": MarketExposureMeta(cluster="rates_bonds", multiplier=Decimal("1")),
}


_ZERO_CLUSTERS: Final[dict[ExposureCluster, Decimal]] = {
    "equity_index": Decimal("0"),
    "commodity": Decimal("0"),
    "rates_bonds": Decimal("0"),
    "crypto": Decimal("0"),
    "fx": Decimal("0"),
}


def _fallback_meta(market: str) -> MarketExposureMeta:
    """Emit a structured warning + return an ``equity_index``/``1`` default.

    Unknown markets in ``positions_current`` should never happen in steady
    state (the order-placement worker rejects markets outside
    ``V1_CANDIDATE_UNIVERSE``) but if one does land — e.g., a hand-INSERT
    during an incident — we'd rather render *something* than crash the
    home page. The structlog warning gives the operator a trail.
    """
    log.warning(
        "exposure_unknown_market",
        market=market,
        cluster_fallback="equity_index",
        multiplier_fallback="1",
    )
    return MarketExposureMeta(cluster="equity_index", multiplier=Decimal("1"))


def compute_exposure_breakdown(
    positions_rows: Sequence[Mapping[str, object]],
    nav: Decimal | None,
) -> ExposureBreakdown:
    """Aggregate ``positions_current`` rows into a cluster-bucketed
    percent-of-NAV breakdown + gross/net NAV percentages.

    Phase 1 simplification per the design brief: uses ``avg_cost`` as the
    mark for every position (live MTM lands separately). The math:

    * ``notional_per_position`` = ``|qty| * avg_cost * multiplier``
    * ``signed_notional`` = ``sign(qty) * notional_per_position``
    * ``by_cluster[c]`` = ``(sum of notional_per_position in cluster c) /
      NAV * 100`` (percent units, quantized to 0.01)
    * ``gross_exposure_pct_nav`` = ``sum_of_notional / NAV * 100``
      (percent units, quantized to 0.01)
    * ``net_exposure_pct_nav`` = ``sum_of_signed_notional / NAV * 100``
      (percent units, quantized to 0.01)

    All three response fields share the same percent-units (0-100) scale
    so the frontend's ``ExposureBar`` math (``filled = current / limit
    * 100`` against cluster limits stated in percent like ``60`` for
    equity-index) renders correctly. Returning dollar notionals here
    causes the Today-page widget to saturate with bogus values like
    "14625% / 60% cap" -- see the 2026-05-27 fix.

    When ``nav`` is ``None`` or non-positive, ALL THREE fields fall back
    to ``Decimal("0")``. Because by_cluster is now also a fraction of
    NAV, the cluster breakdown is undefined when NAV is unavailable;
    zeroing matches the gross/net behavior.
    """
    by_cluster_notional: dict[ExposureCluster, Decimal] = dict(_ZERO_CLUSTERS)
    gross_notional = Decimal("0")
    net_notional = Decimal("0")

    for row in positions_rows:
        market = str(row["market"])
        qty_raw = row["quantity"]
        avg_cost_raw = row["avg_cost"]

        qty = qty_raw if isinstance(qty_raw, int) else int(str(qty_raw))
        if isinstance(avg_cost_raw, Decimal):
            avg_cost = avg_cost_raw
        else:
            avg_cost = Decimal(str(avg_cost_raw))

        meta = V1_EXPOSURE_METADATA.get(market) or _fallback_meta(market)
        notional = abs(Decimal(qty)) * avg_cost * meta.multiplier
        signed_notional = Decimal(qty) * avg_cost * meta.multiplier

        by_cluster_notional[meta.cluster] = by_cluster_notional[meta.cluster] + notional
        gross_notional += notional
        net_notional += signed_notional

    pct_quantum = Decimal("0.01")
    if nav is not None and nav > 0:
        by_cluster_pct: dict[ExposureCluster, Decimal] = {
            cluster: (cluster_notional / nav * Decimal("100")).quantize(pct_quantum)
            for cluster, cluster_notional in by_cluster_notional.items()
        }
        gross_pct = (gross_notional / nav * Decimal("100")).quantize(pct_quantum)
        net_pct = (net_notional / nav * Decimal("100")).quantize(pct_quantum)
    else:
        by_cluster_pct = dict(_ZERO_CLUSTERS)
        gross_pct = Decimal("0")
        net_pct = Decimal("0")

    return ExposureBreakdown(
        by_cluster=by_cluster_pct,
        gross_exposure_pct_nav=gross_pct,
        net_exposure_pct_nav=net_pct,
    )


__all__ = [
    "V1_EXPOSURE_METADATA",
    "MarketExposureMeta",
    "compute_exposure_breakdown",
]
