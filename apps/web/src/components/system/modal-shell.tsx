'use client';

/**
 * Minimal modal shell -- centered overlay + Card body.
 *
 * No Radix Dialog dependency (apps/web doesn't have @radix-ui/react-dialog
 * installed Day 27). This is a Phase-0 lightweight modal: backdrop click
 * + Escape key close, focus trap deferred to Phase 1+ when Radix Dialog
 * is added. Accessibility caveat: no aria-modal trap means screen
 * readers can wander outside; the operator's primary path is keyboard-
 * driven (Escape closes) and the small parent-page surface limits the
 * tab order risk Phase 0.
 */

import { useEffect } from 'react';

interface Props {
  readonly open: boolean;
  readonly onClose: () => void;
  readonly title: string;
  readonly children: React.ReactNode;
  readonly maxWidth?: 'sm' | 'md' | 'lg';
}

const WIDTH_CLASS: Record<NonNullable<Props['maxWidth']>, string> = {
  sm: 'max-w-sm',
  md: 'max-w-md',
  lg: 'max-w-lg',
};

export function ModalShell({
  open,
  onClose,
  title,
  children,
  maxWidth = 'md',
}: Props): JSX.Element | null {
  useEffect(() => {
    if (!open) return;
    const handleKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-bg-base/70 p-4 backdrop-blur-sm"
      onClick={onClose}
      role="presentation"
    >
      <div
        className={`w-full ${WIDTH_CLASS[maxWidth]} rounded-lg border border-border bg-bg-surface p-6 shadow-xl`}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <div className="mb-4 flex items-start justify-between">
          <h2 className="text-lg font-semibold">{title}</h2>
          <button
            type="button"
            className="text-text-muted hover:text-text-primary"
            onClick={onClose}
            aria-label="Close"
          >
            ✕
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}
