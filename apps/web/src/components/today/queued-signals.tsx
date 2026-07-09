'use client';

/**
 * Today's Signals table -- read-only, announce-only (crypto-pivot §3.9).
 *
 * The Approve / Reject / Defer ceremony is RETIRED (delta spec §3.8 —
 * operator mandate: no per-trade approval). The strategy worker acts on
 * its own 00:05 UTC decisions and every trade is ANNOUNCED in Discord
 * `#fills`; this table is a read-only record of the signals emitted, not
 * an inbox awaiting action. The DecisionDiaryModal wiring, busy states
 * and Actions column died with the mutations.
 */

import { useSignalsPending } from '@/lib/api/queries';
import type { SignalSummary } from '@/lib/api/types';
import { formatPrice } from '@/lib/format';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

export function QueuedSignals(): JSX.Element {
  const { data, isLoading, isError } = useSignalsPending();
  const items = data?.items ?? [];

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">
          Today&apos;s Signals{' '}
          <span className="text-text-muted">({items.length})</span>
        </CardTitle>
        <p className="text-xs text-text-muted">
          Announce-only — the strategy worker trades on its own decisions; no
          approval is required or possible. Fills are announced in Discord.
        </p>
      </CardHeader>
      <CardContent>
        {isLoading && <p className="text-sm text-text-muted">Loading…</p>}
        {isError && (
          <p className="text-sm text-severity-p1">
            Failed to load today&apos;s signals.
          </p>
        )}
        {!isLoading && !isError && items.length === 0 && (
          <p className="text-sm text-text-muted">
            No signals today. The daily decision runs at 00:05&nbsp;UTC.
          </p>
        )}
        {items.length > 0 && (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Market</TableHead>
                <TableHead>Side</TableHead>
                <TableHead>Size</TableHead>
                <TableHead>Price</TableHead>
                <TableHead>Anomaly</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.map((s) => (
                <SignalRow key={s.id} signal={s} />
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}

function SignalRow({ signal }: { signal: SignalSummary }): JSX.Element {
  const anomaly = signal.anomaly_reasons[0];
  return (
    <TableRow>
      <TableCell className="font-mono">{signal.market}</TableCell>
      <TableCell>
        <SignalSidePill signal={signal} />
      </TableCell>
      <TableCell className="font-mono tabular-nums">
        {signal.target_contracts}
      </TableCell>
      <TableCell className="font-mono tabular-nums">
        {formatPrice(signal.decision_price)}
      </TableCell>
      <TableCell>
        {anomaly !== undefined ? (
          <span
            className="inline-flex items-center rounded-md bg-severity-p1/20 px-2 py-0.5 text-xs text-severity-p1"
            title={signal.anomaly_reasons.join(', ')}
          >
            {anomaly.replaceAll('_', ' ')}
          </span>
        ) : (
          <span className="text-text-muted">—</span>
        )}
      </TableCell>
    </TableRow>
  );
}

// ---------------------------------------------------------------------------
// Side pill — green LONG / red SHORT / amber CLOSE, so the operator can read a
// signal's side at a glance. Exit signals (direction='flat') render as CLOSE
// with the prior side when available.
//
// Chip shape mirrors ExitWatchingSection's chips; colors use the health.* /
// severity.* tailwind tokens following the codebase's `bg-{token}/20
// text-{token}` convention (signals/WatchingSection).
// ---------------------------------------------------------------------------

/** True for an exit (close) signal. Keys off the dedicated discriminator, with
 *  `direction === 'flat'` as a secondary tell so the pill stays correct even
 *  if `signal_type` ever carries an unanticipated value. */
function isCloseSignal(signal: SignalSummary): boolean {
  return signal.signal_type === 'exit' || signal.direction === 'flat';
}

function SignalSidePill({ signal }: { signal: SignalSummary }): JSX.Element {
  if (isCloseSignal(signal)) {
    // Exits close a position; surface the prior side ("CLOSE · was long") when
    // it's available so the operator sees what the close acts on, not "flat".
    const prior = signal.prior_position_direction;
    return (
      <span className="inline-flex items-center gap-1 rounded-md bg-severity-p1/20 px-2 py-0.5 text-xs font-medium text-severity-p1">
        <span className="uppercase tracking-wide">CLOSE</span>
        {(prior === 'long' || prior === 'short') && (
          <span className="font-normal opacity-80">· was {prior}</span>
        )}
      </span>
    );
  }
  if (signal.direction === 'long') {
    return (
      <span className="inline-flex items-center rounded-md bg-health-green/20 px-2 py-0.5 text-xs font-medium uppercase tracking-wide text-health-green">
        LONG
      </span>
    );
  }
  if (signal.direction === 'short') {
    return (
      <span className="inline-flex items-center rounded-md bg-health-red/20 px-2 py-0.5 text-xs font-medium uppercase tracking-wide text-health-red">
        SHORT
      </span>
    );
  }
  // Defensive: entries are long/short and exits are caught above, so this is
  // unreachable today — render the raw value in a neutral pill rather than
  // silently emitting nothing if an unexpected direction ever appears.
  return (
    <span className="inline-flex items-center rounded-md bg-bg-elevated px-2 py-0.5 text-xs font-medium uppercase tracking-wide text-text-secondary">
      {signal.direction}
    </span>
  );
}
