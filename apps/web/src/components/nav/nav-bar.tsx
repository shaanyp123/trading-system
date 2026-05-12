'use client';

/**
 * Global top nav per frontend-spec §1.3 LOCKED architecture: single
 * horizontal bar, persistent across post-auth pages, active route
 * highlighted. Phase 1 ships the LINKS half; the SSE-bound right-side
 * badges (strategy version / health / P&L / agent / env+state) are a
 * Phase 1+ follow-up under frontend-spec §1.6 — Phase 0 those are
 * insufficient-data placeholders. We surface just the environment +
 * state pill today (sourced from GET /api/today/digest, which carries
 * both `environment` and `state` and is already cached app-wide via
 * the Today-page tile) so the operator can see paper/live + the
 * risk_state at a glance from every page.
 *
 * Hidden on the four auth-flow paths (login/setup/recover) per spec
 * §1.3 "persistent across all post-auth pages" — those screens are
 * single-purpose and should not leak the navigation IA before
 * authentication completes.
 *
 * Phase-gated nav items come from routes.config.ts navItemsForPhase()
 * so adding a new page only requires registering it there.
 */

import Link from 'next/link';
import { usePathname } from 'next/navigation';

import { useTodayDigest } from '@/lib/api/queries';
import { navItemsForPhase } from '@/lib/routes.config';
import { cn } from '@/lib/utils';

const AUTH_PATHS: ReadonlySet<string> = new Set(['/login', '/setup', '/recover']);

function isActive(pathname: string, target: string): boolean {
  if (target === '/') return pathname === '/';
  return pathname === target || pathname.startsWith(`${target}/`);
}

function EnvStatePill(): JSX.Element {
  const { data, isLoading, isError } = useTodayDigest();

  if (isLoading) {
    return (
      <span className="rounded border border-border bg-bg-surface px-2 py-0.5 font-mono text-xs text-text-muted">
        loading…
      </span>
    );
  }
  if (isError || !data) {
    return (
      <span className="rounded border border-border bg-bg-surface px-2 py-0.5 font-mono text-xs text-text-muted">
        offline
      </span>
    );
  }

  const env = data.environment;
  const state = data.state;
  const envColor =
    env === 'paper'
      ? 'text-env-paper'
      : env === 'live-small'
        ? 'text-env-live-small'
        : env === 'live-scale'
          ? 'text-env-live-scale'
          : 'text-text-secondary';
  const stateColor =
    state === 'NORMAL'
      ? 'text-text-primary'
      : state === 'CONVALESCENT' || state === 'VACATION'
        ? 'text-paused'
        : 'text-severity-p0';

  return (
    <span
      className="rounded border border-border bg-bg-surface px-2 py-0.5 font-mono text-xs"
      title={`environment=${env} state=${state}`}
    >
      <span className={envColor}>{env}</span>
      <span className="mx-1 text-text-muted">·</span>
      <span className={stateColor}>{state}</span>
    </span>
  );
}

export function NavBar(): JSX.Element | null {
  const pathname = usePathname() ?? '/';
  if (AUTH_PATHS.has(pathname)) return null;

  const items = navItemsForPhase();

  return (
    <nav
      className="top-bar sticky top-0 z-40 border-b border-border bg-bg-base/95 backdrop-blur"
      aria-label="Primary"
    >
      <div className="container mx-auto flex max-w-7xl items-center gap-4 px-4 py-2 md:px-6">
        <Link
          href="/"
          className="font-mono text-sm font-semibold tracking-tight text-text-primary"
          aria-label="Trading — home"
        >
          <span className="text-pnl-positive">●</span>
          <span className="ml-1">trd</span>
        </Link>
        <ul className="flex items-center gap-1 md:gap-2" role="list">
          {items.map((item) => {
            const active = isActive(pathname, item.path);
            return (
              <li key={item.path}>
                <Link
                  href={item.path}
                  aria-current={active ? 'page' : undefined}
                  className={cn(
                    'rounded px-2 py-1 font-mono text-sm transition-colors',
                    active
                      ? 'text-text-primary underline decoration-text-primary decoration-2 underline-offset-4'
                      : 'text-text-secondary hover:bg-bg-elevated hover:text-text-primary',
                  )}
                >
                  {item.label}
                </Link>
              </li>
            );
          })}
        </ul>
        <div className="ml-auto flex items-center gap-2">
          <EnvStatePill />
        </div>
      </div>
    </nav>
  );
}
