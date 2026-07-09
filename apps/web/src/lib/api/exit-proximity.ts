/**
 * apps/web/src/lib/api/exit-proximity.ts — typed client for
 * GET /api/today/exit-proximity (the Today page's "Stops" view).
 *
 * Crypto-pivot §3.9 rewrite: the exit-side view is now per-open-position
 * STOP proximity — distance to the client 2×ATR soft stop (enforced by
 * the strategy worker's 30 s risk loop) and the native 3×ATR venue
 * backstop — not the retired CME exit-trigger table. Wire contract
 * mirrors `services/api/schemas/exit_proximity.py` (Pydantic v2
 * `BaseModel` with `extra='forbid'`) FIELD-BY-FIELD.
 *
 * Decimal-on-wire convention (A05 / dev-guide §3.8): every numeric field
 * (`entry_vwap`, `mark`, stop prices, headroom fractions) arrives as a
 * Decimal-as-string. Per frontend-spec §8.9 (LOCKED) we DO NOT do
 * Decimal arithmetic in JS — the headroom fractions are computed
 * SERVER-SIDE; this file only classifies rows on booleans and formats
 * the provided strings at the display layer (`parseFloat` on bounded
 * fractional values only).
 *
 * ⚠️ TWO THINGS CARRIED OVER FROM THE PRE-PIVOT CLIENT — read before editing:
 *
 *   1. NO CLIENT-SIDE SORT. The endpoint returns `positions` ALREADY
 *      ordered closest-to-stop (stopped/unprotected rows first, then
 *      ascending min headroom, server-side). We render in RECEIPT ORDER.
 *
 *   2. INVERTED COLOR VALENCE. green = HOLDING = "position SAFE",
 *      red = TRIGGERED = "stop fired / unprotected" — the OPPOSITE
 *      valence of the entry side. We keep a SEPARATE state→token map
 *      (`exitStateChipClass`) so a shared chip component can never paint
 *      a stopped position green.
 *
 * Refresh model: TanStack Query with `staleTime`, keyed under
 * `['positions', 'exit-proximity']` so the existing `position` AND `fill`
 * SSE event handlers in `lib/sse.ts` (both prefix-invalidate
 * `['positions']`) also refresh this view. No new SSE event type is
 * introduced (forbidden by CLAUDE.md locked rules).
 */

import { useQuery, type UseQueryResult } from '@tanstack/react-query';

import { apiCall } from '../api';

// ---------------------------------------------------------------------------
// Wire types (1:1 with services/api/schemas/exit_proximity.py)
// ---------------------------------------------------------------------------

export type DirectionLiteral = 'long' | 'short';

export interface PositionStopProximityView {
  /** CDE product id (e.g. `BIP-20DEC30-CDE`). */
  readonly product_id: string;
  /** Base-asset label (BTC/ETH); `null` without contracts enrichment. */
  readonly asset: string | null;
  readonly direction: DirectionLiteral;
  /** Signed net contracts held in this product (never 0 — flat rows are
   *  filtered server-side). */
  readonly contracts: number;
  /** Decimal-as-string. Quantity-weighted average entry price. */
  readonly entry_vwap: string | null;
  /** Decimal-as-string. Live WS mark; `null` when the mark store is absent
   *  or the mark exceeds the 3-minute staleness policy. */
  readonly mark: string | null;
  /** The worker's marks-stale flag (true — conservative — when the worker
   *  has never reported). */
  readonly marks_stale: boolean;
  /** Decimal-as-string. Client-side 2×ATR soft stop (risk-loop enforced). */
  readonly client_stop_price: string | null;
  /** Decimal-as-string fraction, SERVER-computed: (mark−stop)/mark for a
   *  long, (stop−mark)/mark for a short. `null` when mark or stop missing. */
  readonly client_stop_headroom_frac: string | null;
  /** Decimal-as-string. Venue-resting 3×ATR stop-limit backstop price. */
  readonly native_stop_price: string | null;
  readonly native_stop_headroom_frac: string | null;
  /** True when the worker tracks a venue-resting native stop order id.
   *  False with non-zero contracts = UNPROTECTED (see `isUnprotected`). */
  readonly native_stop_resting: boolean;
  /** True when the engine's stop fired on today's UTC bar-day. */
  readonly stopped_today: boolean;
}

export interface ExitProximityResponse {
  readonly positions: readonly PositionStopProximityView[];
  readonly server_now: string;
}

// ---------------------------------------------------------------------------
// Pure helpers — semantic predicates + the inverted state→token map.
// Extracted so the meaning lives in one place and the component stays a
// thin renderer. (No client-side sort — see the file header.)
// ---------------------------------------------------------------------------

/** Row-level stop status. INVERTED valence vs the entry side — see the
 *  file header. `holding` = protected, `near` = attention (marks stale),
 *  `triggered` = stop fired today. */
export type ExitStateLiteral = 'holding' | 'near' | 'triggered';

/**
 * Tailwind chip classes per exit state — the INVERTED-valence map.
 * HOLDING = green ("safe"), NEAR = yellow ("attention"), TRIGGERED = red.
 * Deliberately NOT shared with the entry-side chip map, whose green means
 * the opposite ("firing").
 */
export function exitStateChipClass(state: ExitStateLiteral): string {
  switch (state) {
    case 'holding':
      return 'bg-health-green/20 text-health-green';
    case 'near':
      return 'bg-health-yellow/20 text-health-yellow';
    case 'triggered':
      return 'bg-health-red/20 text-health-red';
  }
}

/**
 * A held position with NO native stop resting at the venue. The client
 * 2×ATR stop may still be armed (risk-loop enforced) but the venue-side
 * backstop is missing — a latent POSITION_UNPROTECTED risk (delta spec
 * §3.1 requires the 3×ATR backstop within 10 s of any position-opening
 * fill), so the row renders alert styling, not a calm green chip.
 */
export function isUnprotected(p: PositionStopProximityView): boolean {
  return p.native_stop_resting === false && p.contracts !== 0;
}

/**
 * Row status without inventing client-side thresholds: `triggered` when
 * the engine's stop fired today, `near` when the mark feed is stale
 * (headroom can't be trusted), else `holding`. The unprotected case is
 * surfaced separately via `isUnprotected` (alert-row styling).
 */
export function stopRowStatus(p: PositionStopProximityView): ExitStateLiteral {
  if (p.stopped_today) return 'triggered';
  if (p.marks_stale) return 'near';
  return 'holding';
}

// ---------------------------------------------------------------------------
// TanStack Query hook
// ---------------------------------------------------------------------------

const EXIT_PROXIMITY_QUERY_KEY = ['positions', 'exit-proximity'] as const;

export function useExitProximity(): UseQueryResult<ExitProximityResponse> {
  return useQuery({
    queryKey: EXIT_PROXIMITY_QUERY_KEY,
    queryFn: ({ signal }) =>
      apiCall<ExitProximityResponse>('/api/today/exit-proximity', { signal }),
    staleTime: 60_000,
  });
}

export const EXIT_PROXIMITY_KEY = EXIT_PROXIMITY_QUERY_KEY;
