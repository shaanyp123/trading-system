'use client';

/**
 * `/` -- Today landing dashboard.
 *
 * 12-col grid per frontend-spec §2.2.1:
 *   - Health Score (col-span-6) | P&L Summary (col-span-6)
 *   - Queued Signals (col-span-6) | Exposure (col-span-6)
 *   - Positions (col-span-6) | Recent Fills (col-span-6)
 *   - Active Alerts (col-span-12)
 *
 * Below 1024px (tablet/mobile) per spec §2.2.1: "Use desktop or Discord →"
 * banner + Discord deep-link button. Day 22 ships the all-12 stacked layout
 * for narrow viewports; the Discord-prompt banner lands once the bot's
 * invite URL is operator-configured (Day 23 Discord bot session).
 *
 * SSE: `useSSE` opens the multiplexed connection on mount; the pill in the
 * page header reflects status. Each section component reads via TanStack
 * Query so cache invalidation from SSE events flows naturally to re-fetch.
 *
 * Day 20 placeholder is replaced by this composition; the Day-20 layout
 * "Today" heading is preserved in the page header so smoke checks expecting
 * that string still pass.
 */

import { useSSE } from '@/lib/sse';
import { ActiveAlerts } from '@/components/today/active-alerts';
import { ExitWatchingSection } from '@/components/today/ExitWatchingSection';
import { ExposureBreakdown } from '@/components/today/exposure-breakdown';
import { HealthScoreTile } from '@/components/today/health-score-tile';
import { PnLSummary } from '@/components/today/pnl-summary';
import { PositionsTable } from '@/components/today/positions-table';
import { QueuedSignals } from '@/components/today/queued-signals';
import { RecentFills } from '@/components/today/recent-fills';
import { SSEStatusPill } from '@/components/today/sse-status-pill';

export default function TodayPage(): JSX.Element {
  const { status, lastSequenceNo } = useSSE();

  return (
    <div className="container mx-auto max-w-7xl p-4 md:p-6">
      <header className="mb-6 flex items-center justify-between">
        <h1 className="font-mono text-2xl">Today</h1>
        <SSEStatusPill status={status} lastSequenceNo={lastSequenceNo} />
      </header>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
        <div className="lg:col-span-6">
          <HealthScoreTile />
        </div>
        <div className="lg:col-span-6">
          <PnLSummary />
        </div>
        <div className="lg:col-span-6">
          <QueuedSignals />
        </div>
        <div className="lg:col-span-6">
          <ExposureBreakdown />
        </div>
        <div className="lg:col-span-6">
          <PositionsTable />
        </div>
        <div className="lg:col-span-6">
          <RecentFills />
        </div>
        <div className="lg:col-span-12">
          <ExitWatchingSection />
        </div>
        <div className="lg:col-span-12">
          <ActiveAlerts />
        </div>
      </div>
    </div>
  );
}
