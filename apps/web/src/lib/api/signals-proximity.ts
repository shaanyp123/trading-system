/**
 * apps/web/src/lib/api/signals-proximity.ts — typed client for GET /api/signals/proximity.
 *
 * Crypto-pivot §3.9 rewrite: the entry-side "Watching" view is now
 * per-ASSET hysteresis-flip proximity (how close BTC / ETH are to a
 * direction flip at the next 00:05 UTC daily decision), not the retired
 * CME per-gate table. Wire contract mirrors
 * `services/api/schemas/signal_proximity.py` (Pydantic v2 `BaseModel`
 * with `extra='forbid'`) FIELD-BY-FIELD.
 *
 * Decimal-on-wire convention (A05 / dev-guide §3.8): `trend_score` and
 * `mark` arrive as Decimal-as-strings. Per frontend-spec §8.9 (LOCKED)
 * we DO NOT do Decimal arithmetic in JS — every derived number
 * (days_to_flip, lockout_days_left) is computed server-side; this file
 * only buckets/sorts on the provided integers and formats strings at
 * the display layer via `parseFloat` (bounded fractional values, well
 * within IEEE 754 precision).
 *
 * Refresh model: TanStack Query with `staleTime`, keyed under
 * `['signals', 'proximity']` so the existing `signal` SSE event handler
 * in `lib/sse.ts` (which prefix-invalidates `['signals']`) also
 * refreshes this view. No new SSE event type is introduced (forbidden
 * by CLAUDE.md locked rules). Daily-resolution data (the engine state
 * changes at the 00:05 UTC decision) so a 60s `staleTime` is
 * conservative.
 */

import { useQuery, type UseQueryResult } from '@tanstack/react-query';

import { apiCall } from '../api';

// ---------------------------------------------------------------------------
// Wire types (1:1 with services/api/schemas/signal_proximity.py)
// ---------------------------------------------------------------------------

export interface AssetProximityView {
  readonly asset: string;
  /** Decimal-as-string. S1 trend score at the last decision; `null` while
   *  warming up or before the first decision. */
  readonly trend_score: string | null;
  /** Applied direction the engine is holding: -1 / 0 / +1. */
  readonly current_dir: number;
  /** Direction awaiting hysteresis confirmation; `null` = none pending. */
  readonly pending_dir: number | null;
  readonly pending_count: number;
  /** hysteresis_days − pending_count while a flip is pending; `null` = steady.
   *  Server-computed — never derive it client-side. */
  readonly days_to_flip: number | null;
  readonly hysteresis_days: number;
  readonly lockout_active: boolean;
  readonly lockout_days_left: number | null;
  readonly vol_blocked: boolean;
  /** Decimal-as-string. Mark recorded at the last 00:05 UTC decision
   *  (informational — NOT a live tick). */
  readonly mark: string | null;
  /** ISO date (UTC). */
  readonly last_decision_date: string | null;
  /** RFC 3339 UTC — when the worker last persisted this engine state. */
  readonly as_of_utc: string;
}

export interface SignalProximityResponse {
  readonly assets: readonly AssetProximityView[];
  readonly server_now: string;
}

// ---------------------------------------------------------------------------
// Pure helpers — bucket + sort (flipping-soon → blocked → steady ordering)
// ---------------------------------------------------------------------------

/**
 * Three rendering buckets for the Watching table, ordered top-to-bottom:
 *
 *   - `flipping` — a hysteresis flip is pending (`days_to_flip !== null`);
 *     the operator-relevant rows sort to the top, soonest flip first.
 *   - `blocked` — no flip pending but entries are gated (post-stop
 *     lockout active or vol-block latched).
 *   - `steady` — direction confirmed, nothing pending.
 */
export type ProximityBucket = 'flipping' | 'blocked' | 'steady';

export function bucketOf(asset: AssetProximityView): ProximityBucket {
  if (asset.days_to_flip !== null) return 'flipping';
  if (asset.lockout_active || asset.vol_blocked) return 'blocked';
  return 'steady';
}

const BUCKET_RANK: Readonly<Record<ProximityBucket, number>> = {
  flipping: 0,
  blocked: 1,
  steady: 2,
};

/**
 * Sort assets flipping-soon-first:
 *
 *   1. By bucket (flipping > blocked > steady).
 *   2. Within `flipping`: ascending `days_to_flip` (soonest flip first).
 *   3. Alphabetical by asset for a stable, deterministic order.
 *
 * Returns a NEW sorted array; does not mutate input.
 */
export function sortProximityAssets(
  assets: readonly AssetProximityView[],
): readonly AssetProximityView[] {
  const out = [...assets];
  out.sort((a, b) => {
    const bucketA = BUCKET_RANK[bucketOf(a)];
    const bucketB = BUCKET_RANK[bucketOf(b)];
    if (bucketA !== bucketB) return bucketA - bucketB;
    if (a.days_to_flip !== null && b.days_to_flip !== null && a.days_to_flip !== b.days_to_flip) {
      return a.days_to_flip - b.days_to_flip;
    }
    return a.asset.localeCompare(b.asset);
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
