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
 */

import * as React from 'react';
import { QueryClientProvider } from '@tanstack/react-query';

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
