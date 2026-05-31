'use client';

/**
 * `ExitWatchingSection` — the daily exit-proximity table for the Today page
 * ("Watching → Exits"), rendered beside the Positions table (design Q3).
 *
 * For each OPEN position it shows how close that position is to being CLOSED
 * by each of the four V1 exit triggers (Stop / Trend-flip / Reversal /
 * Decommission), plus a "Closest exit" summary. It is the exit-side mirror
 * of `signals/WatchingSection.tsx` (entry-side, #286) — but read the two
 * deliberate differences below.
 *
 * Data source: `GET /api/today/exit-proximity` (PR-B #309), via
 * `useExitProximity`. The handler writes one row per open position each LEAN
 * cycle (~21:30 UTC) and enriches the stop dimension from the working
 * `stop_market` order (Q1 = (B)).
 *
 * ⚠️ DIFFERENCES FROM THE ENTRY-SIDE WATCHING TABLE:
 *
 *   1. RECEIPT ORDER. The endpoint already returns `positions` ordered
 *      closest-to-closing (TRIGGERED → NEAR-asc-by-headroom → HOLDING, design
 *      §3.3, done server-side in PR-B). We render `data.positions` AS-IS — no
 *      client-side re-sort (which could disagree with the canonical order).
 *
 *   2. INVERTED COLOR VALENCE (design Q4). HOLDING = green ("position safe"),
 *      NEAR = yellow, TRIGGERED = red ("closing this cycle") — the opposite of
 *      the entry side. Colors come from `exitStateChipClass`, a SEPARATE map.
 *
 * Refresh model (design ED8): TanStack Query staleTime + the existing
 * `position` / `fill` SSE handlers in `lib/sse.ts` prefix-invalidate
 * `['positions']`, which transitively refetches this hook's
 * `['positions', 'exit-proximity']` key. No new SSE event type (forbidden by
 * CLAUDE.md locked rules).
 *
 * Decimal handling: headroom / last_close / stop_price arrive as Decimal-
 * strings; we never do arithmetic on them. `parseFloat` is used ONLY at the
 * display layer where values are fractional + bounded — see the top-of-file
 * note in `lib/api/exit-proximity.ts`.
 */

import {
  closestExitTrigger,
  exitStateChipClass,
  isDecommissionArmed,
  isStopUnprotected,
  useExitProximity,
  type ClosestExitLiteral,
  type ExitStateLiteral,
  type ExitTriggerView,
  type PositionExitProximityView,
} from '@/lib/api/exit-proximity';
import { usePositionsCurrent } from '@/lib/api/queries';
import { formatPrice, formatTimeETShort } from '@/lib/format';
import { cn } from '@/lib/utils';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

export function ExitWatchingSection(): JSX.Element {
  const { data, isLoading, isError } = useExitProximity();
  // Open-positions data (shared cache with the Positions tile, key
  // `['positions']`) — used ONLY to disambiguate the two empty states below.
  const { data: positionsData } = usePositionsCurrent();
  const hasOpenPositions = (positionsData?.positions.length ?? 0) > 0;

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">
          Exits{' '}
          {data !== undefined && (
            <span className="text-text-muted">({data.positions.length})</span>
          )}
        </CardTitle>
        <p className="text-xs text-text-muted">
          How close each open position is to closing on the next LEAN cycle
          (~21:30&nbsp;UTC). HOLDING&nbsp;= safe, NEAR&nbsp;= within the
          trigger&apos;s band, TRIGGERED&nbsp;= closing this cycle. Ordered
          closest-to-closing.
        </p>
      </CardHeader>
      <CardContent>
        {isLoading && <p className="text-sm text-text-muted">Loading…</p>}
        {isError && (
          <p className="text-sm text-severity-p1">
            Failed to load exit proximity.
          </p>
        )}
        {!isLoading && !isError && data !== undefined && (
          <ExitWatchingBody response={data} hasOpenPositions={hasOpenPositions} />
        )}
      </CardContent>
    </Card>
  );
}

function ExitWatchingBody({
  response,
  hasOpenPositions,
}: {
  response: {
    as_of_cycle_ts_utc: string | null;
    positions: readonly PositionExitProximityView[];
  };
  hasOpenPositions: boolean;
}): JSX.Element {
  if (response.positions.length === 0) {
    // Two distinct empty states (design §7), disambiguated by the Positions
    // tile's data (the endpoint returns the same {null, []} for both):
    //   - open positions exist but no exit-proximity row yet → waiting for
    //     the first LEAN cycle to evaluate them this session.
    //   - genuinely no open positions → nothing to watch for exits.
    if (hasOpenPositions) {
      return (
        <p className="text-sm text-text-muted">
          Waiting for first LEAN cycle today (next at 21:30&nbsp;UTC).
        </p>
      );
    }
    return (
      <p className="text-sm text-text-muted">
        No open positions. Exit proximity appears here once a position is open.
      </p>
    );
  }

  // Render in RECEIPT ORDER — the endpoint already sorted closest-to-closing.
  const decommissioned = response.positions.some(isDecommissionArmed);
  return (
    <div className="space-y-3">
      {decommissioned && <DecommissionBanner />}
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Market</TableHead>
            <TableHead>Held</TableHead>
            <TableHead>Stop</TableHead>
            <TableHead>Trend-flip</TableHead>
            <TableHead>Reversal</TableHead>
            <TableHead>Decommission</TableHead>
            <TableHead>Closest exit</TableHead>
            <TableHead className="text-right">Last close · cycle</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {response.positions.map((p) => (
            <ExitRow key={p.market} position={p} />
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

function ExitRow({ position }: { position: PositionExitProximityView }): JSX.Element {
  const warming = position.gate_status === 'warming_up';
  return (
    <TableRow className={cn(warming && 'opacity-60')}>
      <TableCell className="font-mono">
        <div className="flex items-center gap-2">
          <span>{position.market}</span>
          <DirectionBadge direction={position.direction} />
        </div>
      </TableCell>
      <TableCell>
        <HeldDaysCell
          heldDays={position.held_days}
          heldDaysHeadroom={position.held_days_headroom}
        />
      </TableCell>
      <TableCell>
        <StopCell position={position} />
      </TableCell>
      <TableCell>
        <TrendFlipCell position={position} warming={warming} />
      </TableCell>
      <TableCell>
        <StateChip state={position.reversal.state} />
      </TableCell>
      <TableCell>
        <StateChip state={position.decommission.state} />
      </TableCell>
      <TableCell>
        <ClosestExitCell position={position} />
      </TableCell>
      <TableCell className="text-right text-xs text-text-muted">
        <div className="font-mono tabular-nums">
          {position.last_close === null ? '—' : formatPrice(position.last_close)}
        </div>
        <div className="font-mono">{formatTimeETShort(position.cycle_ts_utc)}</div>
      </TableCell>
    </TableRow>
  );
}

// ---------------------------------------------------------------------------
// Cells + sub-components
// ---------------------------------------------------------------------------

const CLOSEST_EXIT_LABEL: Readonly<Record<ClosestExitLiteral, string>> = {
  stop: 'Stop',
  reversal: 'Reversal',
  trend_flip: 'Trend-flip',
  decommission: 'Decommission',
};

function DirectionBadge({ direction }: { direction: 'long' | 'short' }): JSX.Element {
  return (
    <span className="rounded-md bg-bg-elevated px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-text-secondary">
      {direction}
    </span>
  );
}

function DecommissionBanner(): JSX.Element {
  return (
    <div className="rounded-md border border-severity-p0 bg-severity-p0/10 px-3 py-2 text-sm text-severity-p0">
      Strategy decommissioned — closing next cycle.
    </div>
  );
}

/**
 * Held days vs the MIN_HOLDING floor, derived from `held_days_headroom`
 * (`MIN_HOLDING_DAYS − held_days`). A positive headroom means the trend-flip
 * exit is still blocked by the floor (N days left); `<= 0` means the floor
 * has cleared and a trend-flip can fire.
 */
function HeldDaysCell({
  heldDays,
  heldDaysHeadroom,
}: {
  heldDays: number | null;
  heldDaysHeadroom: number | null;
}): JSX.Element {
  if (heldDays === null) {
    return (
      <span className="text-xs text-text-muted" title="holding-days unknown">
        —
      </span>
    );
  }
  const blocked = heldDaysHeadroom !== null && heldDaysHeadroom > 0;
  return (
    <div className="leading-tight">
      <div className="font-mono tabular-nums text-sm">{heldDays}d</div>
      {heldDaysHeadroom !== null && (
        <div
          className={cn(
            'text-[10px]',
            blocked ? 'text-health-yellow' : 'text-text-muted',
          )}
        >
          {blocked
            ? `${heldDaysHeadroom}d to MIN_HOLDING`
            : 'MIN_HOLDING cleared'}
        </div>
      )}
    </div>
  );
}

/**
 * Stop dimension. The common case is a state chip + the mark-vs-stop pct.
 * The exception is "no stop on record" (design §3.4 / §11): the position has
 * no working protective stop, which is a latent POSITION_UNPROTECTED risk —
 * not benign missing data — so we render a distinct warning chip that
 * cross-links the Active Alerts surface (anchored on the same Today page),
 * rather than a calm green HOLDING chip.
 */
function StopCell({ position }: { position: PositionExitProximityView }): JSX.Element {
  if (isStopUnprotected(position)) {
    return (
      <a
        href="#active-alerts"
        title="No working protective stop on record — latent POSITION_UNPROTECTED risk. See Active Alerts."
        className="inline-flex items-center gap-1 rounded-md border border-severity-p1 px-2 py-0.5 text-xs text-severity-p1 underline decoration-dotted underline-offset-2 hover:bg-severity-p1/10"
      >
        <span aria-hidden>⚠</span>
        <span>no stop on record</span>
      </a>
    );
  }
  return <StateChip state={position.stop.state} headroom={position.stop.headroom} pct />;
}

/**
 * Trend-flip dimension. Combines the holding-days gate + the deterioration
 * (close vs MA_FAST) gate; the server has already collapsed them into one
 * `state` + a deterioration `headroom`. When warming up the indicators
 * aren't computed, so we render a muted placeholder rather than a green
 * HOLDING chip. When MIN_HOLDING blocks the flip, we annotate the chip.
 */
function TrendFlipCell({
  position,
  warming,
}: {
  position: PositionExitProximityView;
  warming: boolean;
}): JSX.Element {
  if (warming) {
    return <MutedChip label="warming up" />;
  }
  const blocked = position.gate_status === 'min_holding_blocked';
  const title = blocked
    ? `Trend-flip blocked by MIN_HOLDING${
        position.held_days_headroom !== null
          ? ` (${position.held_days_headroom}d left)`
          : ''
      }`
    : undefined;
  return (
    <StateChip
      state={position.trend_flip.state}
      headroom={position.trend_flip.headroom}
      pct
      title={title}
    />
  );
}

/**
 * "Closest exit" summary — the limiting trigger (design §3.3) named + colored
 * by `overall_state`, with the trigger's own headroom where it has one
 * (stop / trend_flip carry a pct; the binary reversal / decommission do not).
 */
function ClosestExitCell({
  position,
}: {
  position: PositionExitProximityView;
}): JSX.Element {
  const trigger = closestExitTrigger(position);
  const label = CLOSEST_EXIT_LABEL[position.closest_exit];
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs',
        exitStateChipClass(position.overall_state),
      )}
    >
      <span>{label}</span>
      {trigger.headroom !== null && (
        <span className="font-mono tabular-nums">{formatPct(trigger.headroom)}</span>
      )}
    </span>
  );
}

/**
 * A state-colored chip (the inverted-valence map). Optionally trails a
 * numeric headroom; `pct` formats it as a signed percent (stop / trend_flip),
 * otherwise it is dropped (the binary triggers pass no headroom).
 */
function StateChip({
  state,
  headroom,
  pct = false,
  title,
}: {
  state: ExitStateLiteral;
  headroom?: ExitTriggerView['headroom'];
  pct?: boolean;
  title?: string | undefined;
}): JSX.Element {
  const showHeadroom = pct && headroom !== null && headroom !== undefined;
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-md px-2 py-0.5 font-mono text-xs',
        exitStateChipClass(state),
      )}
      title={title}
    >
      <span className="uppercase tracking-wide">{state}</span>
      {showHeadroom && <span className="tabular-nums">{formatPct(headroom)}</span>}
    </span>
  );
}

function MutedChip({ label }: { label: string }): JSX.Element {
  return (
    <span className="inline-flex items-center rounded-md bg-bg-elevated px-2 py-0.5 font-mono text-xs text-text-muted">
      {label}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Formatter — Decimal-string in, display-string out. parseFloat per the
// precision note in exit-proximity.ts (fractional values, no rounding).
// ---------------------------------------------------------------------------

/** Format a fractional Decimal-string (e.g. "0.0084") as a signed percent
 *  ("+0.84%"). Used for the stop + trend-flip headroom values. */
function formatPct(decimalStr: string | null): string {
  if (decimalStr === null || decimalStr === '') return '—';
  const n = parseFloat(decimalStr);
  if (Number.isNaN(n)) return '—';
  const pct = n * 100;
  const sign = pct > 0 ? '+' : pct < 0 ? '−' : '';
  return `${sign}${Math.abs(pct).toFixed(2)}%`;
}
