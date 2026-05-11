'use client';

/**
 * Account section -- frontend-spec §2.6.10 (Phase 1 minimal).
 *
 * Day 27 ships the Regenerate Backup Codes flow (re-auth required per
 * spec). Click "Regenerate" → re-auth modal opens (driven by parent
 * /system page) → on success, posts to /api/auth/backup-codes/regenerate
 * → renders the 8 new codes once with print + download + verbatim
 * acknowledgment per spec §5.3.
 *
 * Phase 2 adds: revoke all sessions, manage TOTP enrollment, view auth
 * audit log, invite reader.
 */

import { useState } from 'react';

import { useRegenerateBackupCodes } from '@/lib/api/mutations';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';

const VERBATIM_ACK = 'I have saved my backup codes';

interface Props {
  readonly onRequireReauth: (onAuthorized: () => void) => void;
}

export function AccountSection({ onRequireReauth }: Props): JSX.Element {
  const regenerate = useRegenerateBackupCodes();
  const [codes, setCodes] = useState<readonly string[] | null>(null);
  const [ack, setAck] = useState('');
  const [confirmed, setConfirmed] = useState(false);

  const handleRegenerate = (): void => {
    onRequireReauth(() => {
      regenerate.mutate(undefined, {
        onSuccess: (resp) => {
          setCodes(resp.codes);
          setAck('');
          setConfirmed(false);
        },
      });
    });
  };

  const handlePrint = (): void => {
    if (typeof window !== 'undefined') window.print();
  };

  const handleDownload = (): void => {
    if (codes === null) return;
    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    const blob = new Blob(
      [
        'Trading system — backup codes (single-use)\n',
        `Generated at ${new Date().toISOString()}\n\n`,
        codes.join('\n'),
        '\n',
      ],
      { type: 'text/plain;charset=utf-8' },
    );
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `trd-backup-codes-${stamp}.txt`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const ackMatches = ack.trim() === VERBATIM_ACK;

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">Account</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {codes === null && (
          <div className="space-y-2">
            <div>
              <p className="text-sm font-medium">Backup codes</p>
              <p className="text-sm text-text-muted">
                Regenerating invalidates the previous 8 codes. Re-auth required.
              </p>
            </div>
            <Button
              variant="outline"
              size="sm"
              disabled={regenerate.isPending}
              onClick={handleRegenerate}
            >
              {regenerate.isPending ? 'Generating…' : 'Regenerate backup codes'}
            </Button>
          </div>
        )}
        {codes !== null && !confirmed && (
          <div className="space-y-3">
            <p className="text-sm font-medium text-health-green">
              New backup codes generated. Print or download now — these are shown
              ONLY ONCE.
            </p>
            <ol className="grid grid-cols-2 gap-2 rounded-md border border-border bg-bg-base p-3 text-sm">
              {codes.map((code, idx) => (
                <li key={code} className="font-mono tabular-nums">
                  <span className="mr-2 text-text-muted">{idx + 1}.</span>
                  {code}
                </li>
              ))}
            </ol>
            <div className="flex gap-2">
              <Button variant="outline" size="sm" onClick={handlePrint}>
                Print
              </Button>
              <Button variant="outline" size="sm" onClick={handleDownload}>
                Download
              </Button>
            </div>
            <label className="block text-sm">
              <span className="block text-text-muted text-xs mb-1">
                Type <code className="text-text-primary">{VERBATIM_ACK}</code> to
                confirm
              </span>
              <input
                type="text"
                value={ack}
                onChange={(e) => setAck(e.target.value)}
                className="w-full rounded-md border border-border bg-bg-elevated px-2 py-1 text-sm"
              />
            </label>
            <Button
              variant="default"
              size="sm"
              disabled={!ackMatches}
              onClick={() => setConfirmed(true)}
            >
              I have saved my codes
            </Button>
          </div>
        )}
        {codes !== null && confirmed && (
          <div className="space-y-2">
            <p className="text-sm text-health-green">Backup codes saved.</p>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                setCodes(null);
                setAck('');
                setConfirmed(false);
              }}
            >
              Done
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
