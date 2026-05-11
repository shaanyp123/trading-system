'use client';

/**
 * Risk envelope tile -- frontend-spec §2.6.3.
 *
 * Phase 1 read-only view of the active parameter set. Renders the 15
 * locked rows: 5 portfolio-level limits, 5 cluster caps, 2 correlation
 * thresholds, 3 drawdown ceilings.
 *
 * Phase 2 adds per-row [Propose change] button per spec §2.6.3 (gated on
 * 5-min re-auth + opens parameter-change PR modal).
 *
 * Values arrive as Decimal-string per dev-guide §3.8. `unit="percent"`
 * with a 0..1 value renders as a percent (multiply by 100, add `%`).
 * `unit="ratio"` renders the raw 2-decimal number (e.g. correlation
 * thresholds 0.70, 0.85).
 */

import type { RiskParameterItem } from '@/lib/api/types';
import { useRiskEnvelope } from '@/lib/api/queries';
import { formatDateTimeET } from '@/lib/format';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';

function formatValue(item: RiskParameterItem): string {
  const n = Number(item.value);
  if (Number.isNaN(n)) return item.value;
  if (item.unit === 'percent') {
    const pct = n * 100;
    const sign = pct > 0 ? '' : pct < 0 ? '-' : '';
    return `${sign}${Math.abs(pct).toFixed(2)}%`;
  }
  return n.toFixed(2);
}

export function RiskEnvelopeTile(): JSX.Element {
  const { data, isLoading, isError } = useRiskEnvelope();

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-baseline justify-between">
          <CardTitle className="text-base">Risk Envelope</CardTitle>
          {data && (
            <span className="text-xs text-text-muted">
              {data.source === 'parameters_table' ? 'Active' : 'Spec defaults'}
            </span>
          )}
        </div>
      </CardHeader>
      <CardContent>
        {isLoading && <p className="text-sm text-text-muted">Loading…</p>}
        {isError && (
          <p className="text-sm text-severity-p1">Failed to load risk envelope.</p>
        )}
        {!isLoading && !isError && data && <RiskEnvelopeBody envelope={data} />}
      </CardContent>
    </Card>
  );
}

function RiskEnvelopeBody({
  envelope,
}: {
  envelope: NonNullable<ReturnType<typeof useRiskEnvelope>['data']>;
}): JSX.Element {
  const portfolio = envelope.items.filter(
    (i) =>
      i.cluster === null &&
      !i.key.startsWith('correlation_') &&
      !i.key.includes('drawdown') &&
      !i.key.includes('loss_limit'),
  );
  const clusters = envelope.items.filter((i) => i.cluster !== null);
  const correlation = envelope.items.filter((i) => i.key.startsWith('correlation_'));
  const drawdowns = envelope.items.filter(
    (i) => i.key.includes('drawdown') || i.key.includes('loss_limit'),
  );

  return (
    <div className="space-y-4">
      <Group title="Portfolio limits" items={portfolio} />
      <Group title="Cluster caps" items={clusters} />
      <Group title="Correlation" items={correlation} />
      <Group title="Drawdown ceilings" items={drawdowns} />
      <div className="border-t border-border pt-3 text-xs text-text-muted">
        {envelope.parameter_set_hash !== null ? (
          <p>
            Parameter set:{' '}
            <span className="font-mono">
              {envelope.parameter_set_hash.slice(0, 12)}…
            </span>{' '}
            (active since{' '}
            {envelope.parameter_set_active_since_utc !== null
              ? formatDateTimeET(envelope.parameter_set_active_since_utc)
              : 'unknown'}
            )
          </p>
        ) : (
          <p>
            Phase 0 — parameters table not yet seeded; showing backend-spec §2.4
            LOCKED defaults. The dispatcher writes the first row Week 4 Wed.
          </p>
        )}
      </div>
    </div>
  );
}

function Group({
  title,
  items,
}: {
  title: string;
  items: readonly RiskParameterItem[];
}): JSX.Element | null {
  if (items.length === 0) return null;
  return (
    <div>
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-text-muted">
        {title}
      </h3>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
        {items.map((item) => (
          <div key={item.key} className="contents">
            <dt className="text-text-secondary">{item.label}</dt>
            <dd className="font-mono tabular-nums text-right">
              {formatValue(item)}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}
