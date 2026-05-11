'use client';

/**
 * Queued Signals table -- frontend-spec §2.2.2 C.
 *
 * Phase 0: signals table is empty so `items` is always []. Component renders
 * the "No signals queued." empty state until the Week 4 Wed dispatcher PR
 * starts emitting signals.
 *
 * Action buttons (Approve / Reject / Defer) call the Day 15 endpoints which
 * 501-stub with `SIGNAL_HANDLER_NOT_WIRED`; the mutation onError surfaces a
 * P2 toast explaining the handler isn't wired yet. When the dispatcher lands
 * (Week 4 Wed) the buttons start working with zero changes here.
 *
 * Reject/Defer in the full spec opens a DecisionDiaryModal (§3.1) for the
 * tag + reasoning_text. Day 22 ships a minimal inline diary entry stub --
 * `manual_judgment` tag + a constant `Deferred via Today UI` reasoning
 * string -- so the button click reaches the 501 stub end-to-end. The proper
 * modal lands Phase 1 alongside the Decision Diary Modal component.
 */

import { useSignalsPending } from '@/lib/api/queries';
import {
  useApproveSignal,
  useDeferSignal,
  useRejectSignal,
} from '@/lib/api/mutations';
import type { DecisionDiaryEntry, SignalSummary } from '@/lib/api/types';
import { formatPrice } from '@/lib/format';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

// Phase-0 placeholder diary entry -- replaced by the DecisionDiaryModal in
// Phase 1 alongside §3.1 Decision Diary surface.
const PHASE0_DIARY: DecisionDiaryEntry = {
  entry_class: 'signal_response',
  tag: 'manual_judgment',
  reasoning_text:
    'Phase-0 placeholder -- DecisionDiaryModal lands Phase 1 (frontend-spec §3.1).',
};

export function QueuedSignals(): JSX.Element {
  const { data, isLoading, isError } = useSignalsPending();
  const items = data?.items ?? [];

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">
          Queued Signals{' '}
          <span className="text-text-muted">({items.length})</span>
        </CardTitle>
      </CardHeader>
      <CardContent>
        {isLoading && <p className="text-sm text-text-muted">Loading…</p>}
        {isError && (
          <p className="text-sm text-severity-p1">
            Failed to load queued signals.
          </p>
        )}
        {!isLoading && !isError && items.length === 0 && (
          <p className="text-sm text-text-muted">No signals queued.</p>
        )}
        {items.length > 0 && (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Market</TableHead>
                <TableHead>Dir</TableHead>
                <TableHead>Size</TableHead>
                <TableHead>Price</TableHead>
                <TableHead>Anomaly</TableHead>
                <TableHead className="text-right">Actions</TableHead>
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
  const approve = useApproveSignal();
  const reject = useRejectSignal();
  const defer = useDeferSignal();
  const busy = approve.isPending || reject.isPending || defer.isPending;
  const anomaly = signal.anomaly_reasons[0];

  return (
    <TableRow>
      <TableCell className="font-mono">{signal.market}</TableCell>
      <TableCell>{signal.direction}</TableCell>
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
      <TableCell>
        <div className="flex justify-end gap-1">
          <Button
            size="sm"
            variant="default"
            disabled={busy}
            onClick={() => approve.mutate({ signalId: signal.id })}
          >
            Approve
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() => reject.mutate({ signalId: signal.id, diary: PHASE0_DIARY })}
          >
            Reject
          </Button>
          <Button
            size="sm"
            variant="ghost"
            disabled={busy}
            onClick={() => defer.mutate({ signalId: signal.id, diary: PHASE0_DIARY })}
          >
            Defer
          </Button>
        </div>
      </TableCell>
    </TableRow>
  );
}
