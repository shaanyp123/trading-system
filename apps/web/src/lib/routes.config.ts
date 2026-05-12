export type RouteConfig = {
  path: string;
  available_from: 0 | 1 | 2 | 3;
  hidden_in_nav: boolean;
};

export const ROUTES: readonly RouteConfig[] = [
  { path: '/', available_from: 0, hidden_in_nav: false },
  { path: '/signals', available_from: 1, hidden_in_nav: false },
  { path: '/trades', available_from: 1, hidden_in_nav: false },
  { path: '/trades/:id', available_from: 1, hidden_in_nav: true },
  { path: '/performance', available_from: 1, hidden_in_nav: false },
  { path: '/research', available_from: 2, hidden_in_nav: false },
  { path: '/research/backtest/:id', available_from: 2, hidden_in_nav: true },
  { path: '/system', available_from: 1, hidden_in_nav: false },
  { path: '/system/audit/:id', available_from: 1, hidden_in_nav: true },
  { path: '/system/pr/:id', available_from: 2, hidden_in_nav: true },
  { path: '/calendar', available_from: 1, hidden_in_nav: false },
];

export const NAV_LABELS: Readonly<Record<string, string>> = {
  '/': 'Today',
  '/signals': 'Signals',
  '/trades': 'Trades',
  '/performance': 'Performance',
  '/research': 'Research',
  '/system': 'System',
  '/calendar': 'Calendar',
};

export const CURRENT_PHASE: 0 | 1 | 2 | 3 = 1;

export function navItemsForPhase(
  phase: 0 | 1 | 2 | 3 = CURRENT_PHASE,
): ReadonlyArray<{ path: string; label: string }> {
  return ROUTES.filter(
    (r) => !r.hidden_in_nav && r.available_from <= phase,
  ).map((r) => ({
    path: r.path,
    label: NAV_LABELS[r.path] ?? r.path,
  }));
}
