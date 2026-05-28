'use client';

/**
 * `/signals` — dedicated pending-signal approval surface.
 *
 * The signals UI also lives as the "Queued Signals" tile on the Today
 * page (`/`); this route is a focused view of the same data with more
 * vertical space + a page-level explanation of the approve/reject/defer
 * decision context. Used by the operator during the daily 17:30 ET
 * cycle review.
 *
 * Data source: backend-spec §4.1.2 `GET /api/signals?status=pending`
 * via the same `useSignalsPending` hook the Today tile uses. Approve /
 * reject / defer go through the same mutation hooks; the only
 * difference is the page layout (full-width Card + explanatory header
 * row + Decision Diary modal stub same as the Today tile).
 *
 * Phase 0 / Phase 1-onset behavior: when no signals are pending the
 * page renders the empty state. When LEAN's daily cycle starts emitting
 * signals (Phase 1 Pivot-PR-D + the 17:30 ET ceremony), this view
 * populates and the operator approves from here OR from the Today
 * tile — both work, both go through the same mutation path.
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
          Pending signals from the daily strategy cycle. Approve to dispatch
          to IBKR, reject with a diary entry, or defer to the next cycle. Also
          accessible from the <Link href="/" className="underline">Today</Link>{' '}
          page&apos;s Queued Signals tile.
        </p>
      </div>
      <WatchingSection />
      <QueuedSignals />
    </div>
  );
}
