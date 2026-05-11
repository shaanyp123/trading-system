/**
 * apps/web/src/lib/toast.ts -- toast hook + store.
 *
 * Lightweight wrapper around the Radix Toast primitives already shipped in
 * src/components/ui/toast.tsx (Day 20 scaffold). The store is module-level
 * (single Toaster mount) and exposes a `useToast` hook for components +
 * imperative `toast(...)` helpers for non-component callers (e.g. mutation
 * onError handlers).
 *
 * Severity mapping matches spec §4.7 + §8.5 toast UX:
 *   - p0 -> destructive variant (red); auto-dismiss 60s
 *   - p1 -> default variant; 10s
 *   - p2 -> default variant; 5s
 *   - info -> default variant; 4s
 */

import { useSyncExternalStore } from 'react';

export type ToastSeverity = 'p0' | 'p1' | 'p2' | 'info';

export interface ToastEntry {
  readonly id: string;
  readonly title: string;
  readonly description?: string;
  readonly severity: ToastSeverity;
  readonly duration: number;
}

interface ToastInput {
  readonly title: string;
  readonly description?: string;
  readonly severity?: ToastSeverity;
  readonly duration?: number;
}

const DEFAULT_DURATIONS: Record<ToastSeverity, number> = {
  p0: 60_000,
  p1: 10_000,
  p2: 5_000,
  info: 4_000,
};

let counter = 0;
let toasts: readonly ToastEntry[] = [];
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function snapshot(): readonly ToastEntry[] {
  return toasts;
}

function serverSnapshot(): readonly ToastEntry[] {
  return [];
}

export function toast(input: ToastInput): string {
  counter += 1;
  const id = `t-${counter}`;
  const severity = input.severity ?? 'info';
  const duration = input.duration ?? DEFAULT_DURATIONS[severity];
  const entry: ToastEntry = input.description !== undefined
    ? { id, title: input.title, description: input.description, severity, duration }
    : { id, title: input.title, severity, duration };
  toasts = [...toasts, entry];
  emit();
  return id;
}

export function dismissToast(id: string): void {
  toasts = toasts.filter((t) => t.id !== id);
  emit();
}

/**
 * React hook returning the current toast queue + a stable `dismiss` callback.
 * Uses `useSyncExternalStore` for correct concurrent-mode behavior.
 */
export function useToasts(): {
  toasts: readonly ToastEntry[];
  dismiss: (id: string) => void;
} {
  const current = useSyncExternalStore(subscribe, snapshot, serverSnapshot);
  return { toasts: current, dismiss: dismissToast };
}
