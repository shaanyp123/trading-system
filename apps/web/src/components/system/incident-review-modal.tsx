'use client';

/**
 * Incident-review write-up modal -- frontend-spec §2.6.2 incident_review
 * resume path (the form §2.6.2 deferred to "Phase 1+"; built 2026-07-18
 * when the first live incident_review-severity halt made resume
 * unreachable from the tile).
 *
 * Collects the §3.25 write-up (>= 100 chars, mirroring the DDL CHECK),
 * POSTs it via useCreateIncidentReview, and hands the created row's id
 * back to the tile — which then runs the standard resume flow (re-auth
 * gate + resume mutation carrying incident_review_id).
 */

import { useState } from 'react';

import { useCreateIncidentReview } from '@/lib/api/mutations';
import { ApiError } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { ModalShell } from './modal-shell';

const WRITE_UP_MIN_LEN = 100;
const WRITE_UP_MAX_LEN = 10_000;

interface Props {
  readonly open: boolean;
  readonly haltReason: string | null;
  readonly onClose: () => void;
  readonly onCreated: (incidentReviewId: string) => void;
}

export function IncidentReviewModal({
  open,
  haltReason,
  onClose,
  onCreated,
}: Props): JSX.Element | null {
  const create = useCreateIncidentReview();
  const [writeUp, setWriteUp] = useState('');
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const trimmedLen = writeUp.trim().length;
  const missing = WRITE_UP_MIN_LEN - trimmedLen;

  const handleConfirm = (): void => {
    setErrorMsg(null);
    const trimmed = writeUp.trim();
    if (trimmed.length < WRITE_UP_MIN_LEN) {
      setErrorMsg(`Write-up must be at least ${WRITE_UP_MIN_LEN} characters.`);
      return;
    }
    create.mutate(
      { writeUpText: trimmed },
      {
        onSuccess: (res) => {
          setWriteUp('');
          onCreated(res.id);
        },
        onError: (err) => {
          if (err instanceof ApiError) {
            setErrorMsg(err.message);
          } else {
            setErrorMsg('Write-up failed. Try again or check logs.');
          }
        },
      },
    );
  };

  const handleClose = (): void => {
    setWriteUp('');
    setErrorMsg(null);
    onClose();
  };

  return (
    <ModalShell
      open={open}
      onClose={handleClose}
      title="Incident review write-up"
      maxWidth="md"
    >
      <div className="space-y-4">
        <p className="text-sm">
          This halt requires an incident review before resume
          {haltReason !== null && (
            <>
              {' '}
              (reason: <span className="font-mono">{haltReason}</span>)
            </>
          )}
          . Record what happened, the root cause as understood, and why
          resuming is safe. The write-up is stored in{' '}
          <span className="font-mono">incident_reviews</span> and anchored to
          the halt&apos;s audit event.
        </p>
        <label className="block text-sm">
          <span className="block text-text-muted text-xs mb-1">
            {missing > 0
              ? `Write-up (${missing} more characters required)`
              : 'Write-up'}
          </span>
          <textarea
            value={writeUp}
            onChange={(e) => setWriteUp(e.target.value.slice(0, WRITE_UP_MAX_LEN))}
            rows={7}
            className="w-full rounded-md border border-border bg-bg-elevated px-2 py-1 text-sm"
            placeholder="What happened, root cause, evidence reviewed, and why resume is safe (e.g. reference the decisions-log entry)."
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
            variant="default"
            size="sm"
            disabled={create.isPending || trimmedLen < WRITE_UP_MIN_LEN}
            onClick={handleConfirm}
          >
            {create.isPending ? 'Saving…' : 'Save write-up & continue'}
          </Button>
        </div>
      </div>
    </ModalShell>
  );
}
