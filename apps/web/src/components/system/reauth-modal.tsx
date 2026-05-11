'use client';

/**
 * Re-auth modal -- frontend-spec §5.6 LOCKED 5-min UV re-auth window.
 *
 * Risk-loosening actions (kill-switch RESUME, backup-code regenerate,
 * tax-election toggle, parameter-range change, deploy approval,
 * environment-tag override) must be preceded by a successful WebAuthn
 * UV within the last 5 minutes. This modal is the canonical UX:
 *
 *   1. Parent (e.g. KillSwitchTile) reads useAuthMe().last_uv_at.
 *   2. If stale (now - last_uv_at > 5min - 30s buffer), parent opens
 *      this modal BEFORE firing the mutation.
 *   3. Operator clicks "Re-verify with passkey".
 *   4. Modal calls the WebAuthn login challenge + verify pair against
 *      `/api/auth/webauthn/challenge` + `/api/auth/webauthn/verify`
 *      (Day 21 endpoints). On success, the server issues a fresh strong
 *      session whose `last_uv_at` is `now`.
 *   5. Modal invalidates the `auth-me` query, awaits the refetch, then
 *      calls `onAuthorized()` so the parent can fire the original action.
 *   6. On Escape / backdrop click / failure → modal closes WITHOUT
 *      calling `onAuthorized` (the parent stays inert).
 *
 * Phase 0 reality: the API session middleware is a stub that returns
 * `auth_strength="strong"` + `last_uv_at=now-1min` so the modal is
 * almost never triggered during local dev / paper testing. When real
 * WebAuthn middleware lands Week 6+ this modal becomes the only path to
 * risk-loosening — and the existing webauthn login endpoints already
 * mint a session with `last_uv_at` stamped, which is the cheap way to
 * implement re-auth without a dedicated `/api/auth/reauth` endpoint.
 *
 * Browser-unsupported (`window.PublicKeyCredential === undefined`)
 * surfaces an explainer + Cancel button. The TOTP fallback for re-auth
 * is NOT shown — TOTP-only sessions are `auth_strength="weak"` per spec
 * §5.4 and CANNOT perform any re-auth-required action by construction.
 */

import { useState } from 'react';

import { apiCall, ApiError } from '@/lib/api';
import { useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/api/queries';
import {
  browserStartAuthentication,
  describeWebAuthnError,
  isWebAuthnSupported,
} from '@/lib/webauthn';
import { Button } from '@/components/ui/button';
import { ModalShell } from './modal-shell';

interface ChallengeResponse {
  readonly ceremony_id: string;
  readonly options: Record<string, unknown>;
}

interface Props {
  readonly open: boolean;
  readonly onClose: () => void;
  /** Called on a successful re-auth so the parent can fire the original action. */
  readonly onAuthorized: () => void;
  /** Operator-readable label for the gated action, e.g. "Resume kill switch". */
  readonly actionLabel: string;
}

export function ReauthModal({
  open,
  onClose,
  onAuthorized,
  actionLabel,
}: Props): JSX.Element | null {
  const [status, setStatus] = useState<'idle' | 'pending' | 'error'>('idle');
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const queryClient = useQueryClient();

  const supported = isWebAuthnSupported();

  const handleReauth = async (): Promise<void> => {
    setStatus('pending');
    setErrorMsg(null);
    try {
      const challenge = await apiCall<ChallengeResponse>(
        '/api/auth/webauthn/challenge',
        { method: 'POST', body: {} },
      );
      const credential = await browserStartAuthentication(challenge.options);
      await apiCall<{ readonly success: true }>('/api/auth/webauthn/verify', {
        method: 'POST',
        body: {
          ceremony_id: challenge.ceremony_id,
          credential,
        },
      });
      // Force /api/auth/me refetch so the parent component sees the
      // freshly-stamped last_uv_at before firing the gated mutation.
      await queryClient.invalidateQueries({ queryKey: QUERY_KEYS.authMe });
      onAuthorized();
      onClose();
    } catch (err) {
      setStatus('error');
      if (err instanceof ApiError) {
        setErrorMsg(err.message);
      } else {
        setErrorMsg(describeWebAuthnError(err));
      }
    }
  };

  const handleClose = (): void => {
    setStatus('idle');
    setErrorMsg(null);
    onClose();
  };

  return (
    <ModalShell open={open} onClose={handleClose} title="Re-verify" maxWidth="sm">
      <div className="space-y-4">
        <p className="text-sm">
          <span className="font-medium">{actionLabel}</span> requires a recent
          passkey verification (within the last 5 minutes per security policy).
        </p>
        {!supported && (
          <div className="rounded-md border border-severity-p1/40 bg-severity-p1/10 p-3 text-sm text-severity-p1">
            This browser does not support WebAuthn. Risk-loosening actions are
            web-only and require a passkey-capable browser.
          </div>
        )}
        {status === 'error' && errorMsg !== null && (
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
            disabled={!supported || status === 'pending'}
            onClick={() => {
              void handleReauth();
            }}
          >
            {status === 'pending' ? 'Verifying…' : 'Re-verify with passkey'}
          </Button>
        </div>
      </div>
    </ModalShell>
  );
}
