'use client';

/**
 * `/signals` — announce-only signal visibility surface (crypto-pivot §3.9).
 *
 * The per-trade approval ceremony is RETIRED (delta spec §3.8 — operator
 * mandate: no approve/reject/defer). The strategy worker trades on its
 * own 00:05 UTC daily decisions; this page is pure visibility:
 *
 *   - `WatchingSection` — per-asset hysteresis-flip proximity (how close
 *     BTC / ETH are to a direction change at the next daily decision).
 *   - `QueuedSignals` — today's announced signals, read-only.
 *
 * The same signals table also renders on the Today page (`/`); this
 * route gives it vertical space + the Watching context.
 */

import Link from 'next/link';

import { QueuedSignals } from '@/components/today/queued-signals';
import { WatchingSection } from '@/components/signals/WatchingSection';

export default function SignalsPage(): JSX.Element {
  return (
    <div className="container mx-auto max-w-7xl space-y-4 p-4 md:p-6">
      <div className="space-y-1">
        <h1 className="font-mono text-2xl">Signals</h1>
        <p className="text-sm text-text-muted">
          Announce-only: the strategy worker trades its own 00:05&nbsp;UTC
          daily decision on Coinbase and announces fills in Discord — there is
          no approval step. Below: how close each asset is to a direction
          flip, and the signals announced today. Also visible on the{' '}
          <Link href="/" className="underline">Today</Link> page.
        </p>
      </div>
      <WatchingSection />
      <QueuedSignals />
    </div>
  );
}
