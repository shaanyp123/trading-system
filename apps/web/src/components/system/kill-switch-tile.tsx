'use client';

/**
 * Kill-switch tile -- frontend-spec §2.6.2.
 *
 * State display + INVOKE button + RESUME button per spec:
 *
 *   - NORMAL          → green pill + "Invoke kill switch" button
 *   - HALT_NEW routine        → amber banner + RESUME (re-auth)
 *   - HALT_NEW defensive      → amber banner + RESUME (re-auth)
 *   - HALT_NEW incident_review → RED banner + RESUME opens the
 *                                 incident-review write-up modal first
 *                                 (§3.25 row created via
 *                                 POST /api/system/incident-reviews;
 *                                 its id rides the resume mutation —
 *                                 built 2026-07-18 when the first live
 *                                 incident_review halt hit the deferred
 *                                 form)
 *   - CONVALESCENT    → amber banner + countdown (SSE-driven)
 *
 * RESUME re-auth flow (dev-guide §1.5 LOCKED 5-min UV window):
 *   1. Operator clicks RESUME.
 *   2. Tile checks useAuthMe().last_uv_at.
 *   3. If stale (> 5min - 30s buffer): opens ReauthModal.
 *   4. ReauthModal completes WebAuthn → invalidates auth-me → calls back
 *      with a fresh last_uv_at.
 *   5. Tile fires useResumeKillSwitch() mutation.
 *   6. Backend's require_recent_uv() succeeds → state transitions to
 *      CONVALESCENT → SSE risk_state envelope lands → tile auto-refreshes
 *      via the mutation's onSettled invalidation.
 *
 * INVOKE flow (no re-auth — risk-tightening):
 *   1. Operator clicks INVOKE.
 *   2. InvokeKillSwitchModal opens with reason text field.
 *   3. Confirm → mutation fires → backend writes state_transition audit
 *      row + UPSERTs risk_state → SSE → tile refreshes.
 */

import { useState } from 'react';

import {
  useAuthMe,
  useKillSwitchStatus,
} from '@/lib/api/queries';
import { useResumeKillSwitch } from '@/lib/api/mutations';
import { FalsePositiveModal } from './false-positive-modal';
import { IncidentReviewModal } from './incident-review-modal';
import type { KillSwitchStatus } from '@/lib/api/types';
import { formatDateTimeET } from '@/lib/format';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { InvokeKillSwitchModal } from './invoke-kill-switch-modal';
import { ReauthModal } from './reauth-modal';

// Dev-guide §1.5: 5-minute UV window. Front-end uses a 4.5-min threshold so
// the modal opens BEFORE the server-side gate would reject — the operator
// never sees a confusing 401 mid-flow.
const UV_FRESHNESS_MS = 4.5 * 60 * 1000;

function isUvFresh(lastUvAt: string | null): boolean {
  if (lastUvAt === null) return false;
  const ts = new Date(lastUvAt).getTime();
  if (Number.isNaN(ts)) return false;
  return Date.now() - ts < UV_FRESHNESS_MS;
}

interface SeverityStyle {
  readonly banner: string;
  readonly pill: string;
  readonly label: string;
}

const SEVERITY_STYLES: Record<string, SeverityStyle> = {
  NORMAL: {
    banner: 'border-health-green/40 bg-health-green/10 text-health-green',
    pill: 'bg-health-green/20 text-health-green',
    label: 'Normal',
  },
  HALT_NEW: {
    banner: 'border-stale/40 bg-stale/10 text-stale',
    pill: 'bg-stale/20 text-stale',
    label: 'Halted',
  },
  CONVALESCENT: {
    banner: 'border-paused/40 bg-paused/10 text-paused',
    pill: 'bg-paused/20 text-paused',
    label: 'Convalescent',
  },
};

const INCIDENT_REVIEW_BANNER = 'border-health-red/40 bg-health-red/10 text-health-red';

export function KillSwitchTile(): JSX.Element {
  const ks = useKillSwitchStatus();
  const me = useAuthMe();
  const resume = useResumeKillSwitch();

  const [invokeOpen, setInvokeOpen] = useState(false);
  const [falsePositiveOpen, setFalsePositiveOpen] = useState(false);
  const [reauthOpen, setReauthOpen] = useState(false);
  const [reauthPurpose, setReauthPurpose] = useState<
    'resume' | 'false-positive' | null
  >(null);
  // incident_review resume path (2026-07-18): the §3.25 write-up row must
  // exist before resume; its id rides the resume mutation.
  const [incidentReviewOpen, setIncidentReviewOpen] = useState(false);
  const [pendingIncidentReviewId, setPendingIncidentReviewId] = useState<
    string | null
  >(null);

  const startResume = (incidentReviewId: string | null): void => {
    const lastUvAt = me.data?.last_uv_at ?? null;
    if (isUvFresh(lastUvAt)) {
      resume.mutate(
        incidentReviewId !== null ? { incidentReviewId } : {},
      );
      return;
    }
    setReauthPurpose('resume');
    setReauthOpen(true);
  };

  const handleResumeClick = (): void => {
    const needsWriteUp =
      ks.data?.risk_state === 'HALT_NEW' &&
      ks.data?.severity === 'incident_review' &&
      pendingIncidentReviewId === null;
    if (needsWriteUp) {
      setIncidentReviewOpen(true);
      return;
    }
    startResume(pendingIncidentReviewId);
  };

  const handleIncidentReviewCreated = (id: string): void => {
    setPendingIncidentReviewId(id);
    setIncidentReviewOpen(false);
    startResume(id);
  };

  const handleFalsePositiveClick = (): void => {
    const lastUvAt = me.data?.last_uv_at ?? null;
    if (isUvFresh(lastUvAt)) {
      setFalsePositiveOpen(true);
      return;
    }
    setReauthPurpose('false-positive');
    setReauthOpen(true);
  };

  const handleReauthAuthorized = (): void => {
    if (reauthPurpose === 'resume') {
      resume.mutate(
        pendingIncidentReviewId !== null
          ? { incidentReviewId: pendingIncidentReviewId }
          : {},
      );
    }
    if (reauthPurpose === 'false-positive') {
      setFalsePositiveOpen(true);
    }
    setReauthPurpose(null);
  };

  return (
    <>
      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-baseline justify-between">
            <CardTitle className="text-base">Kill Switch</CardTitle>
            {ks.data && (
              <span
                className={`inline-flex items-center gap-2 rounded-full px-3 py-1 text-xs ${
                  ks.data.risk_state === 'HALT_NEW' &&
                  ks.data.severity === 'incident_review'
                    ? 'bg-health-red/20 text-health-red'
                    : SEVERITY_STYLES[ks.data.risk_state]?.pill ?? ''
                }`}
              >
                <span className="inline-block h-2 w-2 rounded-full bg-current" />
                {SEVERITY_STYLES[ks.data.risk_state]?.label ?? ks.data.risk_state}
              </span>
            )}
          </div>
        </CardHeader>
        <CardContent>
          {ks.isLoading && <p className="text-sm text-text-muted">Loading…</p>}
          {ks.isError && (
            <p className="text-sm text-severity-p1">
              Failed to load kill-switch state.
            </p>
          )}
          {!ks.isLoading && !ks.isError && ks.data && (
            <KillSwitchBody
              status={ks.data}
              onInvokeClick={() => setInvokeOpen(true)}
              onResumeClick={handleResumeClick}
              onFalsePositiveClick={handleFalsePositiveClick}
              resumeBusy={resume.isPending}
              incidentReviewAuthored={pendingIncidentReviewId !== null}
            />
          )}
        </CardContent>
      </Card>

      <InvokeKillSwitchModal
        open={invokeOpen}
        onClose={() => setInvokeOpen(false)}
      />
      <IncidentReviewModal
        open={incidentReviewOpen}
        haltReason={ks.data?.halt_reason ?? null}
        onClose={() => setIncidentReviewOpen(false)}
        onCreated={handleIncidentReviewCreated}
      />
      <FalsePositiveModal
        open={falsePositiveOpen}
        onClose={() => setFalsePositiveOpen(false)}
      />
      <ReauthModal
        open={reauthOpen}
        onClose={() => {
          setReauthOpen(false);
          setReauthPurpose(null);
        }}
        onAuthorized={handleReauthAuthorized}
        actionLabel={
          reauthPurpose === 'resume'
            ? 'Resume kill switch'
            : reauthPurpose === 'false-positive'
              ? 'Mark halt as false positive'
              : 'Risk-loosening action'
        }
      />
    </>
  );
}

function KillSwitchBody({
  status,
  onInvokeClick,
  onResumeClick,
  onFalsePositiveClick,
  resumeBusy,
  incidentReviewAuthored,
}: {
  status: KillSwitchStatus;
  onInvokeClick: () => void;
  onResumeClick: () => void;
  onFalsePositiveClick: () => void;
  resumeBusy: boolean;
  incidentReviewAuthored: boolean;
}): JSX.Element {
  if (status.risk_state === 'NORMAL') {
    return (
      <div className="space-y-3">
        <div
          className={`rounded-md border ${SEVERITY_STYLES.NORMAL?.banner ?? ''} px-3 py-2 text-sm`}
        >
          System is in NORMAL state. Signals can be approved + filled per the
          standard pipeline.
        </div>
        <Button variant="destructive" size="sm" onClick={onInvokeClick}>
          Invoke kill switch
        </Button>
      </div>
    );
  }

  if (status.risk_state === 'HALT_NEW') {
    const isIncidentReview = status.severity === 'incident_review';
    const bannerClass = isIncidentReview
      ? INCIDENT_REVIEW_BANNER
      : SEVERITY_STYLES.HALT_NEW?.banner ?? '';
    const bannerCopy = isIncidentReview
      ? incidentReviewAuthored
        ? 'Incident review saved. Resume proceeds with it attached.'
        : 'Incident review required before resume — Resume opens the write-up form first.'
      : `HALT_NEW (${status.severity ?? 'routine'}) — auto-resume after recovery conditions.`;
    return (
      <div className="space-y-3">
        <div className={`rounded-md border ${bannerClass} px-3 py-2 text-sm`}>
          {bannerCopy}
        </div>
        {status.halt_reason !== null && (
          <p className="text-sm">
            <span className="text-text-muted">Reason: </span>
            <span className="font-mono">{status.halt_reason}</span>
          </p>
        )}
        {status.last_transition_utc !== null && (
          <p className="text-sm">
            <span className="text-text-muted">Halted at: </span>
            <span className="font-mono tabular-nums">
              {formatDateTimeET(status.last_transition_utc)}
            </span>
          </p>
        )}
        {status.last_transition_audit_event_uuid !== null && (
          <p className="text-xs text-text-muted">
            Audit event{' '}
            <span className="font-mono">
              {status.last_transition_audit_event_uuid.slice(0, 8)}…
            </span>
          </p>
        )}
        <Button
          variant="default"
          size="sm"
          disabled={resumeBusy}
          onClick={onResumeClick}
          title={
            isIncidentReview && !incidentReviewAuthored
              ? 'Opens the incident-review write-up, then re-verify with passkey to resume.'
              : 'Re-verify with passkey, then transition to CONVALESCENT.'
          }
        >
          {resumeBusy
            ? 'Resuming…'
            : isIncidentReview && !incidentReviewAuthored
              ? 'Write incident review & resume'
              : 'Resume (re-auth required)'}
        </Button>
      </div>
    );
  }

  // CONVALESCENT
  return (
    <div className="space-y-3">
      <div
        className={`rounded-md border ${SEVERITY_STYLES.CONVALESCENT?.banner ?? ''} px-3 py-2 text-sm`}
      >
        CONVALESCENT mode — position sizing halved until 3 clean UTC days
        elapse (2026-07-09 amendment), then auto-returns to NORMAL.
      </div>
      <p className="text-sm text-text-muted">
        The state advances automatically at the 00:15 UTC tick after 3 clean
        days. If this halt was caused by a system defect (not a real risk
        trigger), you can adjudicate it as a false positive and return to
        NORMAL immediately.
      </p>
      <Button
        variant="outline"
        size="sm"
        onClick={onFalsePositiveClick}
        title="Re-verify with passkey, then graduate to NORMAL immediately."
      >
        Mark halt as false positive (re-auth required)
      </Button>
    </div>
  );
}
