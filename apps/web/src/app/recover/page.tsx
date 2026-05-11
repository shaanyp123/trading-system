'use client';

/**
 * /recover -- backup-code recovery per frontend-spec §2.8.3 + §5.3.
 *
 * Single form: username + 10-char base32 backup code (operator may type the
 * canonical ``ABCDE-FGHIJ`` form or just the 10 chars; backend normalizes).
 * On success the operator gets a weak session and is redirected to /setup
 * (which they re-run to re-enroll WebAuthn + TOTP + a fresh batch of 8
 * codes).
 */

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { ApiError, ensureBootstrapCsrfCookie } from '@/lib/api';
import { recover } from '@/lib/auth';

export default function RecoverPage(): JSX.Element {
  const router = useRouter();
  const [username, setUsername] = useState('operator');
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    ensureBootstrapCsrfCookie();
  }, []);

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await recover(username, code);
      // Recovery -> weak session. Walk operator straight back through /setup
      // so they re-enroll WebAuthn + TOTP + new backup codes (Step 1 is
      // skipped because the account already exists; the wizard's first step
      // will fail-token and the operator instead navigates to /system for
      // the in-session re-enrollment flow when that ships in Day 22+).
      router.push('/setup');
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Recovery failed.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto flex min-h-screen max-w-md flex-col justify-center px-6">
      <Card>
        <CardHeader>
          <CardTitle className="text-center font-mono">trd Recover</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="mb-4 text-sm text-text-secondary">
            Enter your username and one of your 8 single-use backup codes. Codes
            look like <code className="font-mono">ABCDE-FGHIJ</code>. Each code
            can only be used once.
          </p>
          {error !== null && (
            <div className="mb-4 rounded border border-severity-p1 bg-severity-p1/10 px-3 py-2 text-sm">
              {error}
            </div>
          )}
          <form onSubmit={handleSubmit} className="flex flex-col gap-3">
            <label className="block text-sm">
              <span className="text-text-secondary">Username</span>
              <input
                type="text"
                autoComplete="username"
                required
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                className="mt-1 w-full rounded border border-base px-3 py-2 font-mono"
              />
            </label>
            <label className="block text-sm">
              <span className="text-text-secondary">Backup code</span>
              <input
                type="text"
                autoComplete="one-time-code"
                required
                minLength={10}
                maxLength={20}
                value={code}
                onChange={(e) => setCode(e.target.value.toUpperCase())}
                placeholder="ABCDE-FGHIJ"
                className="mt-1 w-full rounded border border-base px-3 py-2 text-center font-mono"
              />
            </label>
            <Button type="submit" disabled={busy || code.length < 10}>
              {busy ? 'Verifying…' : 'Recover'}
            </Button>
          </form>
          <p className="mt-4 text-xs text-text-muted">
            Lost all factors? Contact your <code>dba_breakglass</code> per the
            setup record.
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
