/**
 * apps/web/src/lib/api/exit-proximity.ts — typed client for
 * GET /api/today/exit-proximity (the Today page's "Exits" view).
 *
 * Wire contract: mirrors `services/api/schemas/exit_proximity.py`
 * (Pydantic v2 `BaseModel` with `extra='forbid'`) FIELD-BY-FIELD. See
 * `Docs/exit-proximity-design.md` §6 + §7 for the canonical shape, and
 * PR-B (#309) for the implementation. This is the exit-side mirror of
 * `signals-proximity.ts` (entry-side, #286).
 *
 * Decimal-on-wire convention (A05 / dev-guide §3.8): every numeric field
 * (`last_close`, `stop_price`, each trigger's `headroom`) arrives as a
 * Decimal-as-string. Per the design constraint + frontend-spec §8.9
 * (LOCKED) we DO NOT do Decimal arithmetic in JS — the strings are opaque
 * for storage + transit. For display formatting we use `parseFloat` ONLY,
 * and only on the bounded fractional headroom/percent values (well within
 * IEEE 754 precision; no rounding). No `bignumber.js` / `decimal.js` dep is
 * added (matching `apps/web/src/lib/format.ts`).
 *
 * ⚠️ TWO THINGS DIFFER FROM THE ENTRY-SIDE CLIENT — read before editing:
 *
 *   1. NO CLIENT-SIDE SORT. The endpoint returns `positions` ALREADY
 *      ordered closest-to-closing (PR-B does the canonical
 *      TRIGGERED → NEAR-asc-by-headroom → HOLDING sort server-side, design
 *      §3.3). We render in RECEIPT ORDER. `signals-proximity.ts` exported a
 *      `sortProximityMarkets`; this file deliberately does NOT — re-sorting
 *      here would risk disagreeing with the server's canonical order.
 *
 *   2. INVERTED COLOR VALENCE (design Q4). The exit states are
 *      HOLDING / NEAR / TRIGGERED, and green = HOLDING = "position SAFE",
 *      red = TRIGGERED = "CLOSING this cycle" — the OPPOSITE valence of the
 *      entry side (where green = PASS = "firing"). We keep a SEPARATE
 *      state→token map (`exitStateChipClass`) so a shared chip component can
 *      never paint a closing position green.
 *
 * Refresh model (design ED8 + §7): TanStack Query with `staleTime`, keyed
 * under `['positions', 'exit-proximity']` so the existing `position` AND
 * `fill` SSE event handlers in `lib/sse.ts` (both prefix-invalidate
 * `['positions']`) also refresh this view when a position opens / closes or
 * a fill lands. No new SSE event type is introduced (forbidden by CLAUDE.md
 * locked rules). Daily-resolution data so a 60s `staleTime` is conservative
 * (matches `useHealthScore`).
 */

import { useQuery, type UseQueryResult } from '@tanstack/react-query';

import { apiCall } from '../api';

// ---------------------------------------------------------------------------
// Wire types (1:1 with services/api/schemas/exit_proximity.py)
// ---------------------------------------------------------------------------

/** Per-trigger exit state. Mirrors `exit_proximity.ExitState`. INVERTED
 *  valence vs the entry-side `GateStateLiteral` — see the file header. */
export type ExitStateLiteral = 'holding' | 'near' | 'triggered';

/** Which exit trigger drives the overall state. */
export type ClosestExitLiteral = 'stop' | 'reversal' | 'trend_flip' | 'decommission';

/** Position-level discriminator. `min_holding_blocked` = the trend-flip
 *  exit cannot fire yet (held < MIN_HOLDING); `decommissioned` = operator
 *  kill switch armed. */
export type ExitGateStatusLiteral =
  | 'active'
  | 'warming_up'
  | 'min_holding_blocked'
  | 'decommissioned';

export type DirectionLiteral = 'long' | 'short';

export interface ExitTriggerView {
  readonly state: ExitStateLiteral;
  /**
   * Decimal-as-string. `null` for the binary reversal / decommission
   * triggers, when warming up, or (for `stop`) when no working stop is on
   * record. `trend_flip` carries the deterioration pct (close vs MA_FAST);
   * `stop` carries `(mark − stop)/mark` for a long (mirrored for a short).
   */
  readonly headroom: string | null;
}

export interface PositionExitProximityView {
  readonly market: string;
  readonly direction: DirectionLiteral;
  /** RFC 3339 UTC. The LEAN cycle this row was emitted on. */
  readonly cycle_ts_utc: string;
  /** CME session date (ET wall-clock), YYYY-MM-DD. */
  readonly session_date_et: string;
  /** Calendar days held. `null` when `opened_at` is unknown. */
  readonly held_days: number | null;
  /**
   * `MIN_HOLDING_DAYS − held_days`. `> 0` = the trend-flip exit is still
   * blocked by the MIN_HOLDING floor (N days left); `<= 0` = the floor has
   * cleared. `null` when `held_days` is unknown.
   */
  readonly held_days_headroom: number | null;
  /** Decimal-as-string. `null` when warming up. */
  readonly last_close: string | null;
  /**
   * Decimal-as-string. The joined working `stop_market` order's stop price
   * (Q1 = (B)). `null` when no working stop is on record — a latent
   * POSITION_UNPROTECTED signal (render "no stop on record"; see
   * `isStopUnprotected`).
   */
  readonly stop_price: string | null;
  readonly trend_flip: ExitTriggerView;
  readonly stop: ExitTriggerView;
  readonly reversal: ExitTriggerView;
  readonly decommission: ExitTriggerView;
  readonly overall_state: ExitStateLiteral;
  readonly closest_exit: ClosestExitLiteral;
  readonly gate_status: ExitGateStatusLiteral;
}

export interface ExitProximityResponse {
  /** RFC 3339 UTC. `null` when `positions` is empty (no open positions /
   *  pre-first-cycle empty state — design §7). */
  readonly as_of_cycle_ts_utc: string | null;
  readonly positions: readonly PositionExitProximityView[];
}

// ---------------------------------------------------------------------------
// Pure helpers — semantic predicates + the inverted state→token map.
// Extracted so the meaning lives in one place and the component stays a
// thin renderer. (No client-side sort — see the file header.)
// ---------------------------------------------------------------------------

/**
 * Tailwind chip classes per exit state — the INVERTED-valence map (design
 * Q4). HOLDING = green ("safe"), NEAR = yellow ("attention"), TRIGGERED =
 * red ("closing this cycle"). Deliberately NOT shared with the entry-side
 * `GateStateLiteral` map, whose green means the opposite ("firing").
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
 * A held position with no working protective stop on record. The stop
 * dimension comes back HOLDING (LEAN's NULL-state placeholder survived the
 * api join because no working `stop_market` order was found) AND
 * `stop_price` is null. This is NOT benign "missing data" — it is a latent
 * POSITION_UNPROTECTED risk (design §3.4 / §11), so the stop chip renders
 * "no stop on record" and cross-links the alert.
 */
export function isStopUnprotected(p: PositionExitProximityView): boolean {
  return p.stop.state === 'holding' && p.stop_price === null;
}

/**
 * The strategy kill switch is armed for this position — close next cycle
 * regardless of indicators (design §1 (d)). Either the position-level
 * `gate_status` is `decommissioned` or the decommission trigger itself
 * fired; both are checked because the combiner sets the gate_status while
 * the trigger carries the state.
 */
export function isDecommissionArmed(p: PositionExitProximityView): boolean {
  return p.gate_status === 'decommissioned' || p.decommission.state === 'triggered';
}

/** The `ExitTriggerView` named by `closest_exit` — the limiting trigger
 *  whose state == `overall_state` (design §3.3). */
export function closestExitTrigger(p: PositionExitProximityView): ExitTriggerView {
  switch (p.closest_exit) {
    case 'stop':
      return p.stop;
    case 'reversal':
      return p.reversal;
    case 'trend_flip':
      return p.trend_flip;
    case 'decommission':
      return p.decommission;
  }
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
