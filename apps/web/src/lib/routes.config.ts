export type RouteConfig = {
  path: string;
  available_from: 0 | 1 | 2 | 3;
  hidden_in_nav: boolean;
};

export const ROUTES: readonly RouteConfig[] = [
  { path: '/', available_from: 0, hidden_in_nav: false },
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
