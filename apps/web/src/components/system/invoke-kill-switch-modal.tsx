'use client';

/**
 * Invoke kill-switch modal -- frontend-spec §2.6.2 Invoke section.
 *
 * "Halting will cancel pending working orders and pause new entries.
 * Continue?" + reason text field (max 200 chars) + Cancel/Confirm
 * buttons. Operator-initiated invocations from web use
 * `trigger="manual_judgment"`; the other 15 enum values (per spec
 * §3.30 + state_machine.py) are reserved for automated callers wired
 * Phase 1+.
 *
 * No re-auth required for invoke (risk-TIGHTENING). The mutation
 * surfaces ALREADY_HALTED / NO_ACTIVE_ACCOUNT as inline error banners
 * + the toast pathway in mutations.ts.
 */

import { useState } from 'react';

import { useInvokeKillSwitch } from '@/lib/api/mutations';
import { ApiError } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { ModalShell } from './modal-shell';

const REASON_MAX_LEN = 200;

interface Props {
  readonly open: boolean;
  readonly onClose: () => void;
}

export function InvokeKillSwitchModal({ open, onClose }: Props): JSX.Element | null {
  const invoke = useInvokeKillSwitch();
  const [reason, setReason] = useState('');
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const handleConfirm = (): void => {
    setErrorMsg(null);
    const trimmed = reason.trim();
    if (trimmed.length === 0) {
      setErrorMsg('Reason is required.');
      return;
    }
    invoke.mutate(
      { trigger: 'manual_judgment', reason: trimmed },
      {
        onSuccess: () => {
          setReason('');
          onClose();
        },
        onError: (err) => {
          if (err instanceof ApiError) {
            setErrorMsg(err.message);
          } else {
            setErrorMsg('Invoke failed. Try again or check logs.');
          }
        },
      },
    );
  };

  const handleClose = (): void => {
    setReason('');
    setErrorMsg(null);
    onClose();
  };

  const remaining = REASON_MAX_LEN - reason.length;

  return (
    <ModalShell
      open={open}
      onClose={handleClose}
      title="Invoke kill switch"
      maxWidth="md"
    >
      <div className="space-y-4">
        <p className="text-sm">
          Halting will cancel pending working orders and pause new entries.
          Continue?
        </p>
        <label className="block text-sm">
          <span className="block text-text-muted text-xs mb-1">
            Reason ({remaining} chars remaining)
          </span>
          <textarea
            value={reason}
            onChange={(e) => setReason(e.target.value.slice(0, REASON_MAX_LEN))}
            rows={3}
            className="w-full rounded-md border border-border bg-bg-elevated px-2 py-1 text-sm"
            placeholder="e.g. observed unusual broker latency; halting pending investigation"
          />
        </label>
        {errorMsg !== null && (
          <div className="rounded-md border border-severity-p0/40 bg-severity-p0/10 p-3 text-sm text-severity-p0">
            {errorMsg}
          </div>
        )}
        <div className="flex justify-end gap-2">
          <Button variant="outline" size="sm" onClick={handleClose}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            size="sm"
            disabled={invoke.isPending || reason.trim().length === 0}
            onClick={handleConfirm}
          >
            {invoke.isPending ? 'Halting…' : 'Confirm halt'}
          </Button>
        </div>
      </div>
    </ModalShell>
  );
}
