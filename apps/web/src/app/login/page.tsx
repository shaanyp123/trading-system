'use client';

/**
 * /login -- WebAuthn primary + TOTP fallback per frontend-spec §2.8.1 + §5.1.
 *
 * Reads `?to=<targetUrl>` query param; on success router.push to it (default
 * "/"). The backend's WebAuthn ceremony also returns `target_url` from the
 * server-side ceremony state, so the round-trip is consistent if the
 * frontend's stored value drifts.
 */

import { Suspense, useEffect, useMemo, useState } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  getLoginChallenge,
  postLoginVerify,
  postTotpLoginVerify,
} from '@/lib/auth';
import { ApiError, ensureBootstrapCsrfCookie } from '@/lib/api';
import {
  browserStartAuthentication,
  describeWebAuthnError,
  isWebAuthnSupported,
} from '@/lib/webauthn';

export default function LoginPage(): JSX.Element {
  return (
    <Suspense fallback={<div className="p-8 text-text-muted">Loading…</div>}>
      <LoginInner />
    </Suspense>
  );
}

function LoginInner(): JSX.Element {
  const router = useRouter();
  const search = useSearchParams();
  const supported = useMemo(() => isWebAuthnSupported(), []);
  const targetUrl = search.get('to') ?? '/';
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [showTotp, setShowTotp] = useState(false);

  useEffect(() => {
    ensureBootstrapCsrfCookie();
  }, []);

  async function handleWebAuthn(): Promise<void> {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const { ceremony_id, options } = await getLoginChallenge(targetUrl);
      const credential = await browserStartAuthentication(options);
      const result = await postLoginVerify(ceremony_id, credential);
      router.push(result.target_url ?? targetUrl);
    } catch (e) {
      if (e instanceof ApiError) {
        setError(e.message);
      } else {
        setError(describeWebAuthnError(e));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto flex min-h-screen max-w-md flex-col justify-center px-6">
      <Card>
        <CardHeader>
          <CardTitle className="text-center font-mono">trd</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {error !== null && (
            <div className="rounded border border-severity-p1 bg-severity-p1/10 px-3 py-2 text-sm">
              {error}
            </div>
          )}

          {supported ? (
            <Button onClick={handleWebAuthn} disabled={busy} className="w-full">
              {busy ? 'Awaiting authenticator…' : 'Sign in with passkey'}
            </Button>
          ) : (
            <div className="rounded border border-severity-p1 bg-severity-p1/10 px-3 py-2 text-sm">
              WebAuthn requires Chrome, Firefox, Safari, or Edge. Use a TOTP
              code below as fallback.
            </div>
          )}

          <div className="flex items-center gap-2 text-xs text-text-muted">
            <div className="h-px flex-1 bg-base" />
            <span>or</span>
            <div className="h-px flex-1 bg-base" />
          </div>

          {showTotp ? (
            <TotpForm
              busy={busy}
              setBusy={setBusy}
              setError={setError}
              onSuccess={() => router.push(targetUrl)}
            />
          ) : (
            <Button
              variant="outline"
              className="w-full"
              onClick={() => setShowTotp(true)}
            >
              Use authenticator code
            </Button>
          )}

          <p className="text-center text-xs text-text-muted">
            Lost your passkey?{' '}
            <a href="/recover" className="underline">
              Use a backup code
            </a>
          </p>
        </CardContent>
      </Card>
    </div>
  );
}

function TotpForm({
  busy,
  setBusy,
  setError,
  onSuccess,
}: {
  busy: boolean;
  setBusy: (v: boolean) => void;
  setError: (v: string | null) => void;
  onSuccess: () => void;
}): JSX.Element {
  const [username, setUsername] = useState('operator');
  const [code, setCode] = useState('');

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await postTotpLoginVerify(username, code);
      onSuccess();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'TOTP login failed.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-3">
      <input
        type="text"
        autoComplete="username"
        required
        value={username}
        onChange={(e) => setUsername(e.target.value)}
        placeholder="username"
        className="rounded border border-base px-3 py-2 font-mono"
      />
      <input
        type="text"
        inputMode="numeric"
        pattern="\d{6}"
        maxLength={6}
        required
        autoComplete="one-time-code"
        value={code}
        onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))}
        placeholder="6-digit code"
        className="rounded border border-base px-3 py-2 text-center font-mono text-lg"
      />
      <p className="text-xs text-text-muted">
        TOTP-only login is reduced-privilege. Add a passkey from /system once
        signed in.
      </p>
      <Button type="submit" disabled={busy || code.length !== 6}>
        {busy ? 'Verifying…' : 'Sign in'}
      </Button>
    </form>
  );
}
