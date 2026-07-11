'use client';

/**
 * False-positive adjudication modal -- 2026-07-11 operator amendment.
 *
 * Opens from the kill-switch tile's CONVALESCENT branch (after the
 * re-auth gate has been satisfied, mirroring the RESUME flow). The
 * operator asserts the halt that put the system into CONVALESCENT was
 * caused by a system defect, supplies the rationale AND a reference to
 * the defect artifact (e.g. "PR #375"), and the system graduates to
 * NORMAL immediately instead of serving the 3-clean-day probation.
 *
 * Risk-LOOSENING: the mutation only fires behind require_recent_uv on
 * the API side; Discord deliberately has no equivalent command
 * (frontend-spec §6.1 — Discord is risk-tightening only).
 */

import { useState } from 'react';

import { useFalsePositiveGraduation } from '@/lib/api/mutations';
import { ApiError } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { ModalShell } from './modal-shell';

const REASON_MAX_LEN = 500;
const REFERENCE_MAX_LEN = 200;

interface Props {
  readonly open: boolean;
  readonly onClose: () => void;
}

export function FalsePositiveModal({ open, onClose }: Props): JSX.Element | null {
  const adjudicate = useFalsePositiveGraduation();
  const [reason, setReason] = useState('');
  const [defectReference, setDefectReference] = useState('');
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const handleConfirm = (): void => {
    setErrorMsg(null);
    const trimmedReason = reason.trim();
    const trimmedRef = defectReference.trim();
    if (trimmedReason.length === 0) {
      setErrorMsg('Reason is required.');
      return;
    }
    if (trimmedRef.length === 0) {
      setErrorMsg('Defect reference is required (e.g. "PR #375").');
      return;
    }
    adjudicate.mutate(
      { reason: trimmedReason, defectReference: trimmedRef },
      {
        onSuccess: () => {
          setReason('');
          setDefectReference('');
          onClose();
        },
        onError: (err) => {
          if (err instanceof ApiError) {
            setErrorMsg(err.message);
          } else {
            setErrorMsg('Adjudication failed. Try again or check logs.');
          }
        },
      },
    );
  };

  const handleClose = (): void => {
    setReason('');
    setDefectReference('');
    setErrorMsg(null);
    onClose();
  };

  return (
    <ModalShell
      open={open}
      onClose={handleClose}
      title="Mark halt as false positive"
      maxWidth="md"
    >
      <div className="space-y-4">
        <p className="text-sm">
          This graduates the system from CONVALESCENT to NORMAL{' '}
          <strong>immediately</strong>, skipping the remaining half-size
          probation days. Only do this when the halt was caused by a system
          defect (not a genuine risk trigger) and the defect has a concrete
          fix you can reference.
        </p>
        <label className="block text-sm">
          <span className="block text-text-muted text-xs mb-1">
            Why was this halt a false positive?
          </span>
          <textarea
            value={reason}
            onChange={(e) => setReason(e.target.value.slice(0, REASON_MAX_LEN))}
            rows={3}
            className="w-full rounded-md border border-border bg-bg-elevated px-2 py-1 text-sm"
            placeholder="e.g. recon compared asset keys against venue product ids; positions actually matched"
          />
        </label>
        <label className="block text-sm">
          <span className="block text-text-muted text-xs mb-1">
            Defect fix reference (required)
          </span>
          <input
            type="text"
            value={defectReference}
            onChange={(e) =>
              setDefectReference(e.target.value.slice(0, REFERENCE_MAX_LEN))
            }
            className="w-full rounded-md border border-border bg-bg-elevated px-2 py-1 text-sm"
            placeholder='e.g. "PR #375"'
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
            disabled={
              adjudicate.isPending ||
              reason.trim().length === 0 ||
              defectReference.trim().length === 0
            }
            onClick={handleConfirm}
          >
            {adjudicate.isPending ? 'Graduating…' : 'Confirm — return to NORMAL'}
          </Button>
        </div>
      </div>
    </ModalShell>
  );
}
