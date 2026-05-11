'use client';

/**
 * Active Alerts -- frontend-spec §2.2.2 G.
 *
 * Sorted P0 → P1 → P2 (Phase 2 includes P2). Per-row: severity pill |
 * category | message | timestamp ET | "open detail" link to audit row.
 *
 * Phase 0 returns alerts=[] -- the webhook_pusher service was wired Day 10
 * (PR #44) but nothing has yet produced an alert (no kill-switch transitions,
 * no recon breaks, no audit-write failures). The empty state is "No active
 * alerts." per spec.
 *
 * Audit-row deep-link (`/system/audit/:id`) lands when the System page does
 * (Week 7 Wed); Day 22 just shows the audit_event_uuid as a code span.
 */

import { useActiveAlerts } from '@/lib/api/queries';
import { formatTimeETShort } from '@/lib/format';
import type { AlertSeverity } from '@/lib/api/types';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';

const SEVERITY_BG: Readonly<Record<AlertSeverity, string>> = {
  P0: 'bg-severity-p0 text-white',
  P1: 'bg-severity-p1 text-black',
  P2: 'bg-severity-p2 text-white',
};

const SEVERITY_RANK: Readonly<Record<AlertSeverity, number>> = {
  P0: 0,
  P1: 1,
  P2: 2,
};

export function ActiveAlerts(): JSX.Element {
  const { data, isLoading, isError } = useActiveAlerts();
  const alerts = [...(data?.alerts ?? [])].sort(
    (a, b) => SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity],
  );

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">
          Active Alerts <span className="text-text-muted">({alerts.length})</span>
        </CardTitle>
      </CardHeader>
      <CardContent>
        {isLoading && <p className="text-sm text-text-muted">Loading…</p>}
        {isError && (
          <p className="text-sm text-severity-p1">Failed to load alerts.</p>
        )}
        {!isLoading && !isError && alerts.length === 0 && (
          <p className="text-sm text-text-muted">No active alerts.</p>
        )}
        {alerts.length > 0 && (
          <ul className="space-y-2">
            {alerts.map((a) => (
              <li
                key={a.alert_uuid}
                className="flex flex-wrap items-center gap-3 rounded border border-border bg-bg-base p-3"
              >
                <span
                  className={`inline-flex items-center rounded px-2 py-0.5 text-xs font-semibold ${
                    SEVERITY_BG[a.severity]
                  }`}
                >
                  {a.severity}
                </span>
                <span className="text-xs font-mono text-text-secondary">
                  {a.category}
                </span>
                <span className="flex-1 text-sm">{a.title}</span>
                <span className="font-mono text-xs text-text-muted">
                  {formatTimeETShort(a.fired_at)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
