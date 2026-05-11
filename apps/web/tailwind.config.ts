import type { Config } from 'tailwindcss';

const config: Config = {
  darkMode: 'class',
  content: ['./src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        bg: {
          base: '#0a0a0a',
          surface: '#171717',
          elevated: '#262626',
        },
        border: {
          DEFAULT: '#404040',
        },
        text: {
          primary: '#fafafa',
          secondary: '#a3a3a3',
          muted: '#737373',
        },
        pnl: {
          positive: '#10b981',
          negative: '#f43f5e',
        },
        severity: {
          p0: '#ef4444',
          p1: '#f59e0b',
          p2: '#0ea5e9',
        },
        stale: '#eab308',
        paused: '#6366f1',
        health: {
          green: '#10b981',
          yellow: '#f59e0b',
          red: '#ef4444',
        },
        env: {
          paper: '#0ea5e9',
          'live-small': '#f59e0b',
          'live-scale': '#10b981',
        },
      },
      fontFamily: {
        mono: ['JetBrains Mono', 'Inconsolata', 'monospace'],
        sans: ['Inter', 'system-ui', 'sans-serif'],
      },
      fontSize: {
        xs: ['0.75rem', { lineHeight: '1rem' }],
        sm: ['0.875rem', { lineHeight: '1.25rem' }],
        base: ['1rem', { lineHeight: '1.5rem' }],
        lg: ['1.125rem', { lineHeight: '1.75rem' }],
        xl: ['1.25rem', { lineHeight: '1.75rem' }],
        '2xl': ['1.5rem', { lineHeight: '2rem' }],
      },
      transitionDuration: {
        fast: '150ms',
      },
      transitionTimingFunction: {
        smooth: 'cubic-bezier(0.4, 0, 0.2, 1)',
      },
    },
  },
  plugins: [require('@tailwindcss/forms'), require('@tailwindcss/typography')],
};

export default config;
