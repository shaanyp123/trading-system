'use client';

/**
 * Top-level client-component wrapper hosting TanStack Query + Radix Toast.
 *
 * Mounted from src/app/layout.tsx around `{children}` so every post-auth page
 * inherits the QueryClient + Toaster without each route having to opt in.
 *
 * Single QueryClient per browser session: TanStack docs recommend a stable
 * client across renders. We use the module-level instance from
 * `@/lib/queryClient` rather than `useState(() => new QueryClient())` because
 * the Provider remounts only on full page reload and the module singleton
 * pattern is friendlier to test fixtures (a future Vitest setup can swap the
 * import for a per-test instance via dependency injection in `Providers`).
 *
 * CSRF bootstrap (2026-05-28 fix): also runs `ensureBootstrapCsrfCookie()`
 * on mount so every page — not just /login, /setup, /recover — boots with
 * a CSRF cookie. Pre-fix, operators landing directly on /system in a fresh
 * browser session had no `__Host-csrf_token` cookie, so the first
 * state-changing POST (Resume kill switch) was rejected by the backend
 * CSRF middleware. The backend `SessionStubMiddleware` independently mints
 * a server-issued cookie on the first response — both defences land in
 * the same PR. See `Docs/decisions-log.md` 2026-05-28 entry for the
 * incident that surfaced this.
 */

import * as React from 'react';
import { QueryClientProvider } from '@tanstack/react-query';

import { ensureBootstrapCsrfCookie } from '@/lib/api';
import { queryClient } from '@/lib/queryClient';
import { useToasts } from '@/lib/toast';
import {
  Toast,
  ToastClose,
  ToastDescription,
  ToastProvider,
  ToastTitle,
  ToastViewport,
} from '@/components/ui/toast';

interface Props {
  readonly children: React.ReactNode;
}

export function Providers({ children }: Props): JSX.Element {
  // Run before paint via useLayoutEffect so the cookie is present before
  // any child component fires its first POST mutation. useEffect would
  // also work in practice (TanStack mutations are user-event-driven so
  // there's plenty of time), but useLayoutEffect tightens the guarantee:
  // even a Mount-time auto-fire mutation gets the cookie. Falls back to a
  // no-op during SSR via the `typeof document === 'undefined'` guard
  // inside `ensureBootstrapCsrfCookie`.
  React.useLayoutEffect(() => {
    ensureBootstrapCsrfCookie();
  }, []);
  return (
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        {children}
        <Toaster />
        <ToastViewport />
      </ToastProvider>
    </QueryClientProvider>
  );
}

function Toaster(): JSX.Element {
  const { toasts, dismiss } = useToasts();
  return (
    <>
      {toasts.map((t) => (
        <Toast
          key={t.id}
          duration={t.duration}
          variant={t.severity === 'p0' ? 'destructive' : 'default'}
          onOpenChange={(open) => {
            if (!open) dismiss(t.id);
          }}
        >
          <div className="grid gap-1">
            <ToastTitle>{t.title}</ToastTitle>
            {t.description !== undefined && (
              <ToastDescription>{t.description}</ToastDescription>
            )}
          </div>
          <ToastClose />
        </Toast>
      ))}
    </>
  );
}
