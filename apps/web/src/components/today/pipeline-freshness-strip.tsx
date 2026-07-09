'use client';

/**
 * Pipeline Freshness Strip — "did the backend run today?" at a glance
 * (crypto-pivot §3.9).
 *
 * Four stages of the 24/7 crypto daily cycle, in order:
 *
 *   Market data → 00:05 decision → Risk loop → 00:15 recon
 *
 * each with a coloured dot: green = fresh, amber = stale, grey = unknown
 * (worker never reported / no data yet).
 *
 *   - Market data ← `useSystemCycle().worker.marks_stale` (the strategy
 *     worker's own 3-minute mark-staleness watchdog; grey when the
 *     worker row is absent).
 *   - 00:05 decision ← `decision.decided_at_utc` within 26h (one daily
 *     decision + 2h grace), labelled with the decision status
 *     (completed vs skipped_*).
 *   - Risk loop ← `worker.heartbeat_fresh` (30 s loop; fresh ≤ 90 s,
 *     classified server-side).
 *   - 00:15 recon ← system-status `reconciliation_summary.last_check_utc`
 *     within 30h.
 *
 * Thresholds are UTC-anchored (the cycle is a UTC-clock creature);
 * timestamps render America/New_York via `formatDateTimeET` per [A07].
 * "now" is the server clock, so freshness is judged against backend
 * time, not the browser's.
 */

import { useSystemCycle, useSystemStatus } from '@/lib/api/queries';
import { formatDateTimeET, isWithinHours } from '@/lib/format';

type Fresh = boolean | null;

function dotClass(fresh: Fresh): string {
  if (fresh == null) return 'bg-text-muted';
  return fresh ? 'bg-health-green' : 'bg-health-yellow';
}

function FreshnessItem({
  label,
  value,
  fresh,
  title,
}: {
  readonly label: string;
  readonly value: string;
  readonly fresh: Fresh;
  readonly title: string;
}): JSX.Element {
  return (
    <div className="flex items-center gap-1.5" title={title}>
      <span className={`inline-block h-2 w-2 shrink-0 rounded-full ${dotClass(fresh)}`} aria-hidden />
      <span className="text-text-muted">{label}</span>
      <span className="font-mono tabular-nums text-text-primary">{value}</span>
    </div>
  );
}

export function PipelineFreshnessStrip(): JSX.Element {
  const { data: cycle } = useSystemCycle();
  const { data: systemStatus } = useSystemStatus();

  const worker = cycle?.worker ?? null;
  const decision = cycle?.decision ?? null;
  const cycleNow = cycle?.server_now ?? null;
  const statusNow = systemStatus?.server_now ?? null;

  // Market data: the worker's own staleness watchdog; grey until the
  // worker has ever reported.
  const marksFresh: Fresh = worker == null ? null : !worker.marks_stale;
  const marksValue = worker == null ? '—' : worker.marks_stale ? 'stale' : 'live';

  // 00:05 decision: one per UTC day; 26h = one cycle + 2h grace.
  const decidedAt = decision?.decided_at_utc ?? null;
  const decisionValue =
    decision == null
      ? '—'
      : `${decision.status} · ${formatDateTimeET(decidedAt)}`;

  // Risk loop: server-side freshness classification (30 s loop, fresh ≤ 90 s).
  const heartbeatFresh: Fresh = worker == null ? null : worker.heartbeat_fresh;

  const reconTs = systemStatus?.reconciliation_summary?.last_check_utc ?? null;

  return (
    <div className="mb-4 flex flex-wrap items-center gap-x-6 gap-y-2 rounded-md border border-border bg-bg-surface px-4 py-2 text-xs">
      <span className="font-mono uppercase tracking-wide text-text-muted">Today’s pipeline</span>
      <FreshnessItem
        label="Market data"
        value={marksValue}
        fresh={marksFresh}
        title="Coinbase WS marks — the strategy worker's 3-minute staleness watchdog"
      />
      <span className="text-text-muted" aria-hidden>
        →
      </span>
      <FreshnessItem
        label="00:05 decision"
        value={decisionValue}
        fresh={isWithinHours(decidedAt, 26, cycleNow)}
        title="The daily strategy decision at 00:05 UTC — per-asset score → target → action"
      />
      <span className="text-text-muted" aria-hidden>
        →
      </span>
      <FreshnessItem
        label="Risk loop"
        value={formatDateTimeET(worker?.risk_loop_heartbeat_utc ?? null)}
        fresh={heartbeatFresh}
        title="The 30 s risk loop — client stops, liquidation buffer, halt checks"
      />
      <span className="text-text-muted" aria-hidden>
        →
      </span>
      <FreshnessItem
        label="00:15 recon"
        value={formatDateTimeET(reconTs)}
        fresh={isWithinHours(reconTs, 30, statusNow)}
        title="Reconciliation — positions/balances checked against Coinbase at 00:15 UTC"
      />
    </div>
  );
}
