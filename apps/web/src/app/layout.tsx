import './globals.css';

import type { Metadata, Viewport } from 'next';

import { Providers } from '@/components/providers';

export const metadata: Metadata = {
  title: 'Trading',
  description: 'Solo-operator algorithmic trading system',
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  themeColor: '#0a0a0a',
};

type Props = { children: React.ReactNode };

export default function RootLayout({ children }: Props) {
  return (
    <html lang="en" className="dark">
      <body>
        <Providers>
          <main className="min-h-screen">{children}</main>
        </Providers>
      </body>
    </html>
  );
}
