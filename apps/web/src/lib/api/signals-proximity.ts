/**
 * apps/web/src/lib/api/signals-proximity.ts — typed client for GET /api/signals/proximity.
 *
 * Wire contract: mirrors `services/api/schemas/signal_proximity.py`
 * (Pydantic v2 `BaseModel` with `extra='forbid'`). See
 * `Docs/signal-proximity-design.md` §6.4 for the canonical response shape.
 *
 * Decimal-on-wire convention: every numeric field (`last_close`,
 * gate `headroom` values) arrives as a Decimal-as-string per
 * backend-spec §4.1.6 + dev-guide §3.8. Per the design doc constraint
 * (PR-C kickoff prompt + frontend-spec §8.9 LOCKED), we DO NOT do
 * Decimal arithmetic in JS. The frontend treats the strings as opaque
 * for storage + transit. For sort-key comparison and display formatting,
 * we use `parseFloat` ONLY — values are fractional percentages bounded
 * to ~[-1, 1], which is well within IEEE 754 precision; no rounding
 * occurs. No `bignumber.js` / `decimal.js` dep is added (matching
 * `apps/web/src/lib/format.ts`'s existing convention; `decimal.js` is
 * documented as a future Phase 1 add when arithmetic becomes
 * unavoidable).
 *
 * Refresh model (design D7 + §7.2): TanStack Query with `staleTime`,
 * keyed under `['signals', 'proximity']` so the existing `signal` SSE
 * event handler in `lib/sse.ts` (which prefix-invalidates `['signals']`)
 * also refreshes this view when a market fires mid-session. Daily-
 * resolution data so a 60s `staleTime` is conservative; combined with
 * TanStack's default `refetchOnWindowFocus` + `refetchOnMount`, this
 * satisfies the "on-load + on-visibility-change refetch is sufficient"
 * note in §7.2.
 */

import { useQuery, type UseQueryResult } from '@tanstack/react-query';

import { apiCall } from '../api';

// ---------------------------------------------------------------------------
// Wire types (1:1 with services/api/schemas/signal_proximity.py)
// ---------------------------------------------------------------------------

export type GateStateLiteral = 'pass' | 'close' | 'fail';

export type GateStatusLiteral = 'ok' | 'warming_up' | 'decommissioned';

// 'hurst' retained for historic rows pre-dating the 2026-06-02 gate swap;
// new rows emit 'efficiency'.
export type ClosestGateLiteral = 'donchian' | 'trend' | 'hurst' | 'efficiency' | 'history';

export interface GateView {
  readonly state: GateStateLiteral;
  /** Decimal-as-string. `null` when warming up. */
  readonly headroom: string | null;
}

export interface MarketProximityView {
  readonly market: string;
  /** RFC 3339 UTC. The cycle this row was emitted on. */
  readonly cycle_ts_utc: string;
  /** CME session date (ET wall-clock), YYYY-MM-DD. */
  readonly session_date_et: string;
  /** Decimal-as-string. `null` when warming up. */
  readonly last_close: string | null;
  readonly long_donchian: GateView;
  readonly short_donchian: GateView;
  readonly long_trend: GateView;
  readonly short_trend: GateView;
  readonly efficiency: GateView;
  readonly overall_state: GateStateLiteral;
  readonly closest_gate: ClosestGateLiteral;
  readonly gate_status: GateStatusLiteral;
}

export interface SignalProximityResponse {
  /** RFC 3339 UTC. `null` when pre-first-cycle (empty `markets`). */
  readonly as_of_cycle_ts_utc: string | null;
  readonly markets: readonly MarketProximityView[];
}

// ---------------------------------------------------------------------------
// Pure helpers — extracted so the sort logic is unit-test-able + reusable
// ---------------------------------------------------------------------------

/**
 * Three rendering buckets for the Watching table, ordered top-to-bottom:
 *
 *   - `fired` — `overall_state === 'pass'` AND `gate_status === 'ok'`.
 *     The market cleared all three gates this cycle; a signal fired.
 *   - `watching` — `overall_state === 'close'` AND `gate_status === 'ok'`.
 *     One or more gates within the CLOSE band; about to fire.
 *   - `inactive` — `overall_state === 'fail'` AND `gate_status === 'ok'`.
 *     At least one gate firmly failing; most-likely-to-fire-next-session
 *     sorts to the top of this bucket.
 *   - `warming` — `gate_status === 'warming_up' | 'decommissioned'`.
 *     Bottom group per §7.3.
 */
export type ProximityBucket = 'fired' | 'watching' | 'inactive' | 'warming';

export function bucketOf(market: MarketProximityView): ProximityBucket {
  if (market.gate_status !== 'ok') return 'warming';
  if (market.overall_state === 'pass') return 'fired';
  if (market.overall_state === 'close') return 'watching';
  return 'inactive';
}

/**
 * Return the absolute gap on the closest_gate, used as the secondary
 * sort key within the `watching` and `inactive` buckets per §3.3.
 *
 * For directional gates (donchian, trend), we take the closer of the two
 * sides — that's the side actually driving the worst-gate state, mirroring
 * the strategy module's `_worst()` selection.
 *
 * Returns `+Infinity` for warming-up rows (`closest_gate === 'history'`)
 * so they sort to the bottom of any bucket they happen to land in (defence
 * in depth — the bucket grouping above already separates them).
 *
 * Parse via `parseFloat` per the precision note at the top of this file —
 * headroom values are fractional, well within IEEE 754 range.
 */
export function closestGateAbsGap(market: MarketProximityView): number {
  switch (market.closest_gate) {
    case 'donchian':
      return Math.min(
        absParseOrInfinity(market.long_donchian.headroom),
        absParseOrInfinity(market.short_donchian.headroom),
      );
    case 'trend':
      return Math.min(
        absParseOrInfinity(market.long_trend.headroom),
        absParseOrInfinity(market.short_trend.headroom),
      );
    case 'efficiency':
      return absParseOrInfinity(market.efficiency.headroom);
    case 'hurst':
      // Legacy rows pre-dating the 2026-06-02 gate swap carry no `efficiency`
      // headroom to compare on; sort them to the bottom of their bucket. Such
      // rows are stale by definition (a market not re-evaluated since the
      // swap) and age out after the next cycle.
      return Number.POSITIVE_INFINITY;
    case 'history':
      return Number.POSITIVE_INFINITY;
  }
}

function absParseOrInfinity(decimalStr: string | null): number {
  if (decimalStr === null || decimalStr === '') return Number.POSITIVE_INFINITY;
  const n = parseFloat(decimalStr);
  if (Number.isNaN(n)) return Number.POSITIVE_INFINITY;
  return Math.abs(n);
}

const BUCKET_RANK: Readonly<Record<ProximityBucket, number>> = {
  fired: 0,
  watching: 1,
  inactive: 2,
  warming: 3,
};

/**
 * Sort markets per design §3.3 + §7.3:
 *
 *   1. By bucket (fired > watching > inactive > warming).
 *   2. Within `watching` + `inactive` buckets: ascending by abs gap on
 *      `closest_gate` (smallest gap first = closest to firing).
 *   3. Within `fired` + `warming` buckets: alphabetical by market symbol
 *      (deterministic; no operator-meaningful order beyond the bucket).
 *
 * Returns a NEW sorted array; does not mutate input.
 */
export function sortProximityMarkets(
  markets: readonly MarketProximityView[],
): readonly MarketProximityView[] {
  const out = [...markets];
  out.sort((a, b) => {
    const bucketA = BUCKET_RANK[bucketOf(a)];
    const bucketB = BUCKET_RANK[bucketOf(b)];
    if (bucketA !== bucketB) return bucketA - bucketB;
    const bucket = bucketOf(a);
    if (bucket === 'watching' || bucket === 'inactive') {
      const gapA = closestGateAbsGap(a);
      const gapB = closestGateAbsGap(b);
      if (gapA !== gapB) return gapA - gapB;
    }
    return a.market.localeCompare(b.market);
  });
  return out;
}

// ---------------------------------------------------------------------------
// TanStack Query hook
// ---------------------------------------------------------------------------

const PROXIMITY_QUERY_KEY = ['signals', 'proximity'] as const;

export function useSignalsProximity(): UseQueryResult<SignalProximityResponse> {
  return useQuery({
    queryKey: PROXIMITY_QUERY_KEY,
    queryFn: ({ signal }) =>
      apiCall<SignalProximityResponse>('/api/signals/proximity', { signal }),
    staleTime: 60_000,
  });
}

export const SIGNALS_PROXIMITY_QUERY_KEY = PROXIMITY_QUERY_KEY;
