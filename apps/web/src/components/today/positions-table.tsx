'use client';

/**
 * Positions Table -- frontend-spec §2.2.2 E, crypto-pivot §3.9 columns.
 *
 * Columns: Product | Asset | Contracts | Entry VWAP | Mark | uPnL |
 * Client stop | Native stop.
 *
 * Mark provenance: the header shows "Marks as of {time} ({source})" from
 * the newest per-position `mark_observed_at_utc` + its `mark_source`
 * (the backend's §3.9 mark ladder: live_ws → recon_eod →
 * fallback_avg_cost). The retired bar-lag warning + `prices_as_of`
 * tooltip died with bar_sync.
 *
 * The native-stop cell carries a warning affordance when the worker
 * reports NO venue-resting native stop for a non-flat position
 * (`native_stop_resting === false`) — the §3.1 3×ATR backstop should
 * rest within 10 s of any position-opening fill.
 */

import { usePositionsCurrent } from '@/lib/api/queries';
import type { Position } from '@/lib/api/types';
import { formatPrice, formatPnL, formatTimeETShort, pnlSignClass } from '@/lib/format';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

/** The position with the newest observed mark — drives the header's
 *  "Marks as of" line. Timestamp comparison only (no arithmetic). */
function newestMarked(positions: readonly Position[]): Position | null {
  let best: Position | null = null;
  for (const p of positions) {
    if (p.mark_observed_at_utc == null) continue;
    if (
      best?.mark_observed_at_utc == null ||
      new Date(p.mark_observed_at_utc).getTime() >
        new Date(best.mark_observed_at_utc).getTime()
    ) {
      best = p;
    }
  }
  return best;
}

export function PositionsTable(): JSX.Element {
  const { data, isLoading, isError } = usePositionsCurrent();
  const positions = data?.positions ?? [];
  const marked = newestMarked(positions);

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-baseline justify-between gap-2">
          <CardTitle className="text-base">
            Positions <span className="text-text-muted">({positions.length})</span>
          </CardTitle>
          {marked?.mark_observed_at_utc != null && (
            <span
              className="text-xs text-text-muted"
              title="When the newest mark behind these prices was observed, and which mark-ladder rung supplied it"
            >
              Marks as of {formatTimeETShort(marked.mark_observed_at_utc)}
              {marked.mark_source != null && ` (${marked.mark_source})`}
            </span>
          )}
        </div>
      </CardHeader>
      <CardContent>
        {isLoading && <p className="text-sm text-text-muted">Loading…</p>}
        {isError && (
          <p className="text-sm text-severity-p1">
            Failed to load positions.
          </p>
        )}
        {!isLoading && !isError && positions.length === 0 && (
          <p className="text-sm text-text-muted">
            No open positions. Filled trades will appear here.
          </p>
        )}
        {positions.length > 0 && (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Product</TableHead>
                <TableHead>Asset</TableHead>
                <TableHead className="text-right">Contracts</TableHead>
                <TableHead className="text-right">Entry VWAP</TableHead>
                <TableHead className="text-right">Mark</TableHead>
                <TableHead className="text-right">uPnL</TableHead>
                <TableHead className="text-right">Client stop</TableHead>
                <TableHead className="text-right">Native stop</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {positions.map((p) => (
                <TableRow key={p.instrument_id}>
                  <TableCell className="font-mono">{p.symbol}</TableCell>
                  <TableCell className="text-text-secondary">
                    {p.asset ?? '—'}
                  </TableCell>
                  <TableCell className="text-right font-mono tabular-nums">
                    {p.qty}
                  </TableCell>
                  <TableCell className="text-right font-mono tabular-nums">
                    {formatPrice(p.avg_entry_price)}
                  </TableCell>
                  <TableCell className="text-right font-mono tabular-nums">
                    {formatPrice(p.current_price)}
                  </TableCell>
                  <TableCell
                    className={`text-right font-mono tabular-nums ${
                      pnlSignClass(p.unrealized_pnl) ?? ''
                    }`}
                  >
                    {formatPnL(p.unrealized_pnl)}
                  </TableCell>
                  <TableCell className="text-right font-mono tabular-nums">
                    {formatPrice(p.client_stop_price)}
                  </TableCell>
                  <TableCell className="text-right font-mono tabular-nums">
                    <NativeStopCell position={p} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}

/**
 * Native-stop cell: the resting 3×ATR backstop price, with a warning
 * affordance when the worker tracks NO venue-resting native stop for a
 * non-flat position — a latent unprotected-position risk, not benign
 * missing data. `native_stop_resting === null` (worker never ran) stays
 * a plain "—": unknown, not alarmed.
 */
function NativeStopCell({ position }: { position: Position }): JSX.Element {
  if (position.native_stop_resting === false && position.qty !== 0) {
    return (
      <span
        className="inline-flex items-center gap-1 text-severity-p1"
        title="No native stop resting at venue — the 3×ATR backstop is missing for this position"
      >
        <span aria-hidden>⚠</span>
        <span>not resting</span>
      </span>
    );
  }
  return <>{formatPrice(position.native_stop_price)}</>;
}
