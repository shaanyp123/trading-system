'use client';

/**
 * /setup -- 4-step first-run wizard per frontend-spec §2.8.2.
 *
 *   1. Token verify  -- password-style form; POST /api/setup/verify-token
 *   2. WebAuthn      -- navigator.credentials.create() + register/verify
 *   3. TOTP          -- enroll-challenge -> QR + secret display -> setup-verify
 *   4. Backup codes  -- display 8 ABCDE-FGHIJ codes once with verbatim ack
 *
 * Day 21 (Week 6 Tue) implementation. The verbatim acknowledgment is the
 * locked UX safety per frontend-spec §5.3 -- operator types "I have saved
 * my backup codes" before the Continue button activates. Backend doesn't
 * re-validate this string; it's purely UX.
 */

import { useEffect, useMemo, useState } from 'react';
import { useRouter } from 'next/navigation';
import QRCode from 'qrcode';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  generateBackupCodes,
  getRegisterChallenge,
  postRegisterVerify,
  postTotpEnrollChallenge,
  postTotpSetupVerify,
  verifySetupToken,
} from '@/lib/auth';
import { ensureBootstrapCsrfCookie, ApiError } from '@/lib/api';
import {
  browserStartRegistration,
  describeWebAuthnError,
  isWebAuthnSupported,
} from '@/lib/webauthn';

type WizardStep = 'token' | 'webauthn' | 'totp' | 'backup' | 'done';

const VERBATIM_ACK = 'I have saved my backup codes';

export default function SetupPage(): JSX.Element {
  const router = useRouter();
  const [step, setStep] = useState<WizardStep>('token');
  const [ceremonyId, setCeremonyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    ensureBootstrapCsrfCookie();
  }, []);

  return (
    <div className="mx-auto max-w-xl px-6 py-12">
      <div className="mb-8">
        <h1 className="text-2xl font-mono">Setup</h1>
        <p className="mt-1 text-sm text-text-secondary">
          First-run enrollment: token &rarr; passkey &rarr; TOTP &rarr; backup codes.
        </p>
        <ol className="mt-4 flex gap-2 text-xs text-text-muted">
          <StepDot label="Token" active={step === 'token'} done={['webauthn', 'totp', 'backup', 'done'].includes(step)} />
          <StepDot label="Passkey" active={step === 'webauthn'} done={['totp', 'backup', 'done'].includes(step)} />
          <StepDot label="TOTP" active={step === 'totp'} done={['backup', 'done'].includes(step)} />
          <StepDot label="Backup codes" active={step === 'backup'} done={step === 'done'} />
        </ol>
      </div>

      {error !== null && (
        <div className="mb-4 rounded border border-severity-p1 bg-severity-p1/10 px-3 py-2 text-sm">
          {error}
        </div>
      )}

      {step === 'token' && (
        <TokenStep
          onSuccess={(cid) => {
            setCeremonyId(cid);
            setError(null);
            setStep('webauthn');
          }}
          onError={setError}
        />
      )}
      {step === 'webauthn' && ceremonyId !== null && (
        <WebAuthnStep
          ceremonyId={ceremonyId}
          onSuccess={() => {
            setError(null);
            setStep('totp');
          }}
          onError={setError}
        />
      )}
      {step === 'totp' && ceremonyId !== null && (
        <TotpStep
          ceremonyId={ceremonyId}
          onSuccess={() => {
            setError(null);
            setStep('backup');
          }}
          onError={setError}
        />
      )}
      {step === 'backup' && ceremonyId !== null && (
        <BackupStep
          ceremonyId={ceremonyId}
          onDone={() => {
            setError(null);
            setStep('done');
            router.push('/');
          }}
          onError={setError}
        />
      )}
      {step === 'done' && (
        <Card>
          <CardContent className="pt-6 text-sm">
            Setup complete. Redirecting to the dashboard&hellip;
          </CardContent>
        </Card>
      )}
    </div>
  );
}

function StepDot({ label, active, done }: { label: string; active: boolean; done: boolean }): JSX.Element {
  const color = done ? 'bg-health-green' : active ? 'bg-env-paper' : 'bg-surface';
  return (
    <li className="flex items-center gap-1.5">
      <span className={`inline-block h-2 w-2 rounded-full ${color}`} aria-hidden />
      <span className={active ? 'text-text-primary' : ''}>{label}</span>
    </li>
  );
}

// ---------------------------------------------------------------------------
// Step 1 -- token verify
// ---------------------------------------------------------------------------
function TokenStep({
  onSuccess,
  onError,
}: {
  onSuccess: (ceremonyId: string) => void;
  onError: (message: string) => void;
}): JSX.Element {
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    try {
      const result = await verifySetupToken(token);
      onSuccess(result.ceremony_session_id);
    } catch (e) {
      onError(e instanceof ApiError ? e.message : 'Token verification failed.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>1. Setup token</CardTitle>
      </CardHeader>
      <CardContent>
        <p className="mb-4 text-sm text-text-secondary">
          Enter the one-time token printed to the VPS console. The token is
          single-use and never appears in browser history or server logs.
        </p>
        <form onSubmit={handleSubmit} className="flex flex-col gap-3">
          <input
            type="password"
            autoComplete="off"
            autoFocus
            required
            minLength={8}
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="setup token"
            className="rounded border border-base px-3 py-2 font-mono"
          />
          <Button type="submit" disabled={busy || token.length < 8}>
            {busy ? 'Verifying…' : 'Verify'}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Step 2 -- WebAuthn registration
// ---------------------------------------------------------------------------
function WebAuthnStep({
  ceremonyId,
  onSuccess,
  onError,
}: {
  ceremonyId: string;
  onSuccess: () => void;
  onError: (message: string) => void;
}): JSX.Element {
  const supported = useMemo(() => isWebAuthnSupported(), []);
  const [nickname, setNickname] = useState('');
  const [busy, setBusy] = useState(false);

  async function handleRegister(): Promise<void> {
    if (busy) return;
    setBusy(true);
    try {
      const { options } = await getRegisterChallenge(
        ceremonyId,
        nickname.trim() === '' ? null : nickname.trim(),
      );
      const credential = await browserStartRegistration(options);
      await postRegisterVerify(ceremonyId, credential);
      onSuccess();
    } catch (e) {
      if (e instanceof ApiError) {
        onError(e.message);
      } else {
        onError(describeWebAuthnError(e));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>2. Register passkey</CardTitle>
      </CardHeader>
      <CardContent>
        {!supported ? (
          <p className="text-sm text-severity-p1">
            This browser does not support WebAuthn. Use a recent Chrome, Firefox,
            Safari, or Edge. Setup cannot continue without a passkey.
          </p>
        ) : (
          <>
            <p className="mb-4 text-sm text-text-secondary">
              Press the button below and complete the platform prompt (Touch ID,
              hardware key, etc.). User verification is required.
            </p>
            <label className="mb-3 block text-sm">
              <span className="text-text-secondary">Device label (optional)</span>
              <input
                type="text"
                maxLength={64}
                value={nickname}
                onChange={(e) => setNickname(e.target.value)}
                placeholder="e.g. iPhone Touch ID"
                className="mt-1 w-full rounded border border-base px-3 py-2"
              />
            </label>
            <Button onClick={handleRegister} disabled={busy}>
              {busy ? 'Awaiting authenticator…' : 'Register passkey'}
            </Button>
          </>
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Step 3 -- TOTP enrollment
// ---------------------------------------------------------------------------
function TotpStep({
  ceremonyId,
  onSuccess,
  onError,
}: {
  ceremonyId: string;
  onSuccess: () => void;
  onError: (message: string) => void;
}): JSX.Element {
  const [secret, setSecret] = useState<string | null>(null);
  const [qrDataUri, setQrDataUri] = useState<string | null>(null);
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [requested, setRequested] = useState(false);

  useEffect(() => {
    if (requested) return;
    setRequested(true);
    void (async () => {
      try {
        const result = await postTotpEnrollChallenge(ceremonyId);
        setSecret(result.secret_base32);
        const png = await QRCode.toDataURL(result.provisioning_uri, {
          margin: 1,
          width: 192,
        });
        setQrDataUri(png);
      } catch (e) {
        onError(e instanceof ApiError ? e.message : 'TOTP enrollment failed.');
      }
    })();
  }, [ceremonyId, requested, onError]);

  async function handleVerify(event: React.FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    try {
      await postTotpSetupVerify(ceremonyId, code);
      onSuccess();
    } catch (e) {
      onError(e instanceof ApiError ? e.message : 'TOTP verification failed.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>3. TOTP backup</CardTitle>
      </CardHeader>
      <CardContent>
        <p className="mb-4 text-sm text-text-secondary">
          Scan the QR code with your authenticator app (1Password, Authy,
          Aegis, etc.), then enter the 6-digit code it generates.
        </p>
        {qrDataUri !== null ? (
          <div className="mb-4 flex flex-col items-center gap-2">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={qrDataUri}
              alt="TOTP provisioning QR code"
              width={192}
              height={192}
              className="rounded border border-base"
            />
            {secret !== null && (
              <code className="select-all text-xs text-text-muted">{secret}</code>
            )}
          </div>
        ) : (
          <p className="text-sm text-text-muted">Generating secret&hellip;</p>
        )}
        <form onSubmit={handleVerify} className="flex flex-col gap-3">
          <input
            type="text"
            inputMode="numeric"
            pattern="\d{6}"
            maxLength={6}
            required
            value={code}
            onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))}
            placeholder="123456"
            className="rounded border border-base px-3 py-2 text-center font-mono text-lg"
          />
          <Button type="submit" disabled={busy || code.length !== 6}>
            {busy ? 'Verifying…' : 'Verify code'}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Step 4 -- backup codes
// ---------------------------------------------------------------------------
function BackupStep({
  ceremonyId,
  onDone,
  onError,
}: {
  ceremonyId: string;
  onDone: () => void;
  onError: (message: string) => void;
}): JSX.Element {
  const [codes, setCodes] = useState<readonly string[] | null>(null);
  const [ack, setAck] = useState('');
  const [requested, setRequested] = useState(false);

  useEffect(() => {
    if (requested) return;
    setRequested(true);
    void (async () => {
      try {
        const result = await generateBackupCodes(ceremonyId);
        setCodes(result.codes);
      } catch (e) {
        onError(e instanceof ApiError ? e.message : 'Backup code generation failed.');
      }
    })();
  }, [ceremonyId, requested, onError]);

  function downloadCodes(): void {
    if (codes === null) return;
    const ts = new Date().toISOString().split('T')[0] ?? 'today';
    const blob = new Blob([codes.join('\n') + '\n'], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `trd-backup-codes-${ts}.txt`;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>4. Backup codes</CardTitle>
      </CardHeader>
      <CardContent>
        <p className="mb-4 text-sm text-text-secondary">
          These 8 single-use codes recover your account if you lose both your
          passkey and your authenticator app. They are <strong>shown once</strong>
          {' '}and never re-displayed. Store them somewhere physically secure.
        </p>
        {codes === null ? (
          <p className="text-sm text-text-muted">Generating codes&hellip;</p>
        ) : (
          <>
            <ul className="mb-4 grid grid-cols-2 gap-2 font-mono text-sm">
              {codes.map((c, i) => (
                <li key={i} className="rounded border border-base px-3 py-1.5 text-center">
                  {c}
                </li>
              ))}
            </ul>
            <div className="mb-4 flex gap-2">
              <Button type="button" variant="outline" onClick={() => window.print()}>
                Print
              </Button>
              <Button type="button" variant="outline" onClick={downloadCodes}>
                Download
              </Button>
            </div>
            <label className="block text-sm">
              <span className="text-text-secondary">
                Type <code className="text-text-primary">{VERBATIM_ACK}</code> to continue.
              </span>
              <input
                type="text"
                value={ack}
                onChange={(e) => setAck(e.target.value)}
                placeholder={VERBATIM_ACK}
                className="mt-1 w-full rounded border border-base px-3 py-2"
              />
            </label>
            <Button
              className="mt-3 w-full"
              onClick={onDone}
              disabled={ack.trim() !== VERBATIM_ACK}
            >
              Continue
            </Button>
          </>
        )}
      </CardContent>
    </Card>
  );
}
