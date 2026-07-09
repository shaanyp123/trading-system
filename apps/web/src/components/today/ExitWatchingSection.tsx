'use client';

/**
 * `ExitWatchingSection` — the "Stops" table for the Today page
 * (crypto-pivot §3.9), rendered beside the Positions table.
 *
 * For each OPEN position it shows how much room remains before the two
 * protective stops: the client 2×ATR soft stop (enforced by the strategy
 * worker's 30 s risk loop) and the native 3×ATR backstop resting at the
 * venue. Headroom fractions are computed SERVER-side (frontend-spec §8.9
 * LOCKED — no Decimal arithmetic in JS); this component only formats.
 *
 * ⚠️ TWO DELIBERATE PROPERTIES — read before editing:
 *
 *   1. RECEIPT ORDER. The endpoint returns `positions` ALREADY ordered
 *      closest-to-stop (stopped/unprotected first, then ascending min
 *      headroom, server-side). No client-side re-sort.
 *
 *   2. INVERTED COLOR VALENCE. Green = protected ("position safe"),
 *      red = stopped today / unprotected — the opposite of the entry
 *      side. Colors come from `exitStateChipClass`, a SEPARATE map.
 *
 * A position with NO native stop resting at the venue renders an alert
 * row ("no native stop resting at venue") — a latent unprotected-position
 * risk, not benign missing data.
 *
 * Refresh model: TanStack Query staleTime + the existing `position` /
 * `fill` SSE handlers in `lib/sse.ts` prefix-invalidate `['positions']`,
 * which transitively refetches this hook's `['positions',
 * 'exit-proximity']` key. No new SSE event type (forbidden by CLAUDE.md
 * locked rules).
 */

import {
  exitStateChipClass,
  isUnprotected,
  stopRowStatus,
  useExitProximity,
  type PositionStopProximityView,
} from '@/lib/api/exit-proximity';
import { formatPrice } from '@/lib/format';
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

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">
          Stops{' '}
          {data !== undefined && (
            <span className="text-text-muted">({data.positions.length})</span>
          )}
        </CardTitle>
        <p className="text-xs text-text-muted">
          Room to each protective stop per open position. The client
          2×ATR&nbsp;stop is enforced by the 30&nbsp;s risk loop; the native
          3×ATR&nbsp;stop rests at the venue as a backstop. Ordered
          closest-to-stop.
        </p>
      </CardHeader>
      <CardContent>
        {isLoading && <p className="text-sm text-text-muted">Loading…</p>}
        {isError && (
          <p className="text-sm text-severity-p1">
            Failed to load stop proximity.
          </p>
        )}
        {!isLoading && !isError && data !== undefined && (
          <ExitWatchingBody positions={data.positions} />
        )}
      </CardContent>
    </Card>
  );
}

function ExitWatchingBody({
  positions,
}: {
  positions: readonly PositionStopProximityView[];
}): JSX.Element {
  if (positions.length === 0) {
    return (
      <p className="text-sm text-text-muted">
        No open positions. Stop proximity appears here once a position is open.
      </p>
    );
  }

  // Render in RECEIPT ORDER — the endpoint already sorted closest-to-stop.
  const anyUnprotected = positions.some(isUnprotected);
  return (
    <div className="space-y-3">
      {anyUnprotected && <UnprotectedBanner />}
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Product</TableHead>
            <TableHead>Dir</TableHead>
            <TableHead className="text-right">Contracts</TableHead>
            <TableHead className="text-right">Mark</TableHead>
            <TableHead>Client stop (2×ATR)</TableHead>
            <TableHead>Native stop (3×ATR)</TableHead>
            <TableHead>Status</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {positions.map((p) => (
            <ExitRow key={p.product_id} position={p} />
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

function ExitRow({ position }: { position: PositionStopProximityView }): JSX.Element {
  const unprotected = isUnprotected(position);
  return (
    <TableRow className={cn(unprotected && 'bg-severity-p1/5')}>
      <TableCell className="font-mono">
        <div className="flex items-center gap-2">
          <span>{position.product_id}</span>
          {position.asset != null && (
            <span className="rounded-md bg-bg-elevated px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-text-secondary">
              {position.asset}
            </span>
          )}
        </div>
      </TableCell>
      <TableCell>
        <DirectionBadge direction={position.direction} />
      </TableCell>
      <TableCell className="text-right font-mono tabular-nums">
        {position.contracts}
      </TableCell>
      <TableCell className="text-right font-mono tabular-nums">
        <span title={position.marks_stale ? 'Mark feed is stale (worker watchdog)' : undefined}>
          {formatPrice(position.mark)}
          {position.marks_stale && (
            <span className="ml-1 text-health-yellow" aria-hidden>
              ⚠
            </span>
          )}
        </span>
      </TableCell>
      <TableCell>
        <StopLevelCell price={position.client_stop_price} headroom={position.client_stop_headroom_frac} />
      </TableCell>
      <TableCell>
        {unprotected ? (
          <span
            className="inline-flex items-center gap-1 rounded-md border border-severity-p1 px-2 py-0.5 text-xs text-severity-p1"
            title="No native stop resting at venue — the 3×ATR backstop is missing for this position"
          >
            <span aria-hidden>⚠</span>
            <span>no native stop resting at venue</span>
          </span>
        ) : (
          <StopLevelCell
            price={position.native_stop_price}
            headroom={position.native_stop_headroom_frac}
          />
        )}
      </TableCell>
      <TableCell>
        <StatusChip position={position} />
      </TableCell>
    </TableRow>
  );
}

// ---------------------------------------------------------------------------
// Cells + sub-components
// ---------------------------------------------------------------------------

function DirectionBadge({ direction }: { direction: 'long' | 'short' }): JSX.Element {
  return (
    <span className="rounded-md bg-bg-elevated px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-text-secondary">
      {direction}
    </span>
  );
}

function UnprotectedBanner(): JSX.Element {
  return (
    <div className="rounded-md border border-severity-p1 bg-severity-p1/10 px-3 py-2 text-sm text-severity-p1">
      One or more positions have no native stop resting at the venue.
    </div>
  );
}

/** Stop price + its server-computed headroom fraction, rendered as a
 *  signed percent. "—" when the stop or the headroom input is missing. */
function StopLevelCell({
  price,
  headroom,
}: {
  price: string | null;
  headroom: string | null;
}): JSX.Element {
  if (price === null) {
    return <span className="text-xs text-text-muted">—</span>;
  }
  return (
    <div className="leading-tight">
      <div className="font-mono tabular-nums text-sm">{formatPrice(price)}</div>
      <div className="text-[10px] font-mono text-text-muted">
        {headroom === null ? 'headroom —' : `headroom ${formatPct(headroom)}`}
      </div>
    </div>
  );
}

/**
 * Row status chip (INVERTED valence): red "stopped today" when the
 * engine's stop fired on today's UTC bar-day, yellow "marks stale" when
 * the feed is degraded, green "protected" otherwise. The unprotected
 * case renders in the Native-stop column, not here.
 */
function StatusChip({ position }: { position: PositionStopProximityView }): JSX.Element {
  const status = stopRowStatus(position);
  const label =
    status === 'triggered' ? 'stopped today' : status === 'near' ? 'marks stale' : 'protected';
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-md px-2 py-0.5 font-mono text-xs uppercase tracking-wide',
        exitStateChipClass(status),
      )}
    >
      {label}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Formatter — Decimal-string in, display-string out. parseFloat per the
// precision note in exit-proximity.ts (fractional values, no rounding).
// ---------------------------------------------------------------------------

/** Format a fractional Decimal-string (e.g. "0.0084") as a signed percent
 *  ("+0.84%"). Used for the server-computed stop headroom fractions. */
function formatPct(decimalStr: string | null): string {
  if (decimalStr === null || decimalStr === '') return '—';
  const n = parseFloat(decimalStr);
  if (Number.isNaN(n)) return '—';
  const pct = n * 100;
  const sign = pct > 0 ? '+' : pct < 0 ? '−' : '';
  return `${sign}${Math.abs(pct).toFixed(2)}%`;
}
