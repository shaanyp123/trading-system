'use client';

/**
 * Queued Signals table -- frontend-spec §2.2.2 C.
 *
 * Reject/Defer open the DecisionDiaryModal (spec §3.1) which collects
 * `{entry_class, tag, reasoning_text}` from the operator. Approve is a
 * direct mutation (no diary required). When the Week 4 Wed dispatcher PR
 * wires the real handler, both paths work end-to-end with no changes
 * here.
 *
 * Phase 0: signals table is empty so `items` is always []. Component
 * renders the "No signals queued." empty state until the dispatcher
 * starts emitting.
 */

import { useState } from 'react';

import { useSignalsPending } from '@/lib/api/queries';
import {
  useApproveSignal,
  useDeferSignal,
  useRejectSignal,
} from '@/lib/api/mutations';
import type { DecisionDiaryEntry, SignalSummary } from '@/lib/api/types';
import { formatPrice } from '@/lib/format';
import {
  DecisionDiaryModal,
  type DecisionDiaryContextKind,
} from '@/components/decision-diary-modal';
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
                <TableHead>Side</TableHead>
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

type DiaryModalState =
  | { open: false }
  | { open: true; kind: Extract<DecisionDiaryContextKind, 'signal_reject' | 'signal_defer'> };

function SignalRow({ signal }: { signal: SignalSummary }): JSX.Element {
  const approve = useApproveSignal();
  const reject = useRejectSignal();
  const defer = useDeferSignal();
  const [modal, setModal] = useState<DiaryModalState>({ open: false });
  const busy = approve.isPending || reject.isPending || defer.isPending;
  const anomaly = signal.anomaly_reasons[0];
  // Human side for the decision-diary modal title. Exits carry
  // direction='flat'; surface "close" there instead of the confusing "flat".
  const sideText = isCloseSignal(signal) ? 'close' : signal.direction;
  const subjectLabel = `${signal.market} ${sideText}`;

  const handleDiarySubmit = async (entry: DecisionDiaryEntry): Promise<void> => {
    if (!modal.open) return;
    const args = { signalId: signal.id, diary: entry };
    try {
      if (modal.kind === 'signal_reject') {
        await reject.mutateAsync(args);
      } else {
        await defer.mutateAsync(args);
      }
      setModal({ open: false });
    } catch {
      // Toast surfaced by mutation onError; leave modal open so the
      // operator can adjust + retry without retyping reasoning_text.
    }
  };

  return (
    <>
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
              onClick={() => setModal({ open: true, kind: 'signal_reject' })}
            >
              Reject
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={busy}
              onClick={() => setModal({ open: true, kind: 'signal_defer' })}
            >
              Defer
            </Button>
          </div>
        </TableCell>
      </TableRow>
      <DecisionDiaryModal
        open={modal.open}
        context={
          modal.open
            ? { kind: modal.kind, signalId: signal.id, subjectLabel }
            : { kind: 'signal_reject', signalId: signal.id, subjectLabel }
        }
        onSubmit={handleDiarySubmit}
        onClose={() => setModal({ open: false })}
        isSubmitting={reject.isPending || defer.isPending}
      />
    </>
  );
}

// ---------------------------------------------------------------------------
// Side pill — green LONG / red SHORT / amber CLOSE, so the operator can read a
// signal's side at a glance. The old plain-text "Dir" cell had no color + the
// same weight as every other cell (easy to miss), and exit signals (PR-A2
// indicator exits, #312 — direction='flat') rendered as a confusing bare
// "flat" with no entry/exit distinction.
//
// Chip shape mirrors ExitWatchingSection's StateChip; colors use the
// health.* / severity.* tailwind tokens (tailwind.config.ts) following the
// codebase's `bg-{token}/20 text-{token}` convention (signals/WatchingSection).
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
