'use client';

/**
 * Health Score Tile -- frontend-spec §2.2.2 A.
 *
 * Renders the composite 0-100 score with traffic-light color. Phase 0 returns
 * `insufficient_data=true` with all components at score=null -- spec rule is
 * to show a gray "—" + tooltip "insufficient data — track record under
 * construction". Click expands to a per-component bar list (cached payload;
 * no extra fetch).
 *
 * Primary source: GET /api/health-score (returns the FULL component breakdown
 * for the click-expand). The Today digest endpoint denormalizes the same body
 * inline for first-paint, but the standalone hook is what the tile reads --
 * mirrors the per-resource staleTime budget in spec §8.1.
 */

import { useState } from 'react';

import { useHealthScore } from '@/lib/api/queries';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';

const TRAFFIC_LIGHT_BG: Readonly<Record<'green' | 'yellow' | 'red', string>> = {
  green: 'bg-health-green',
  yellow: 'bg-health-yellow',
  red: 'bg-health-red',
};

export function HealthScoreTile(): JSX.Element {
  const { data, isLoading, isError } = useHealthScore();
  const [expanded, setExpanded] = useState(false);

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center justify-between text-base">
          <span>Health Score</span>
          {data !== undefined && !data.insufficient_data && (
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              className="text-xs text-text-secondary hover:text-text-primary"
              aria-expanded={expanded}
            >
              {expanded ? 'Hide components' : 'Show components'}
            </button>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent>
        {isLoading && <p className="text-sm text-text-muted">Loading…</p>}
        {isError && (
          <p className="text-sm text-severity-p1">
            Failed to load health score.
          </p>
        )}
        {data !== undefined && (
          <>
            <div className="flex items-center gap-3">
              <span
                className={`inline-block h-3 w-3 rounded-full ${
                  TRAFFIC_LIGHT_BG[data.traffic_light]
                }`}
                aria-hidden="true"
              />
              <span
                className="font-mono text-3xl tabular-nums"
                title={
                  data.insufficient_data
                    ? 'insufficient data — track record under construction'
                    : undefined
                }
              >
                {data.insufficient_data ? '—' : data.composite}
              </span>
              <span className="text-sm uppercase tracking-wide text-text-secondary">
                {data.traffic_light}
              </span>
            </div>
            {data.insufficient_data && (
              <p className="mt-2 text-xs text-text-muted">
                Track record under construction. Components fill in as live
                trading history accumulates.
              </p>
            )}
            {expanded && (
              <ul className="mt-4 space-y-2" aria-label="Health score components">
                {data.components.map((c) => (
                  <li key={c.name} className="flex items-center justify-between text-sm">
                    <span className="text-text-secondary">
                      {c.name.replaceAll('_', ' ')}
                    </span>
                    <span className="font-mono tabular-nums">
                      {c.insufficient_data || c.score === null ? '—' : c.score}{' '}
                      <span className="text-text-muted">({c.weight_pct}%)</span>
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}
