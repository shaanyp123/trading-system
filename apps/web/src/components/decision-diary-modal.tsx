'use client';

/**
 * DecisionDiaryModal — collects `{entry_class, tag, reasoning_text}`
 * from the operator (frontend-spec §3.1).
 *
 * Crypto-pivot §3.9: the `signal_reject` / `signal_defer` context kinds
 * are RETIRED with the per-trade approval ceremony (delta spec §3.8 —
 * announce-only; the reject/defer mutations and their backend endpoints
 * are gone). What remains is the `standalone` path — the operator-
 * authored general diary entry (Phase 2 surface) that makes Trades
 * queryable by tag and full-text searchable by reasoning_text.
 *
 * Validation matches spec §3.1:
 *   - reasoning_text: 10-2000 chars
 *   - tag: one of the 5 locked values
 *   - entry_class: 'general' for the standalone path
 *
 * Phase-0 ergonomics: backed by ModalShell which lacks a focus trap (no
 * @radix-ui/react-dialog dep yet). Acceptable for a single-operator
 * keyboard-driven surface; upgrade lands when Radix Dialog is added.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { ModalShell } from '@/components/system/modal-shell';
import { Button } from '@/components/ui/button';
import type { DecisionDiaryEntry } from '@/lib/api/types';

export type DecisionDiaryContextKind = 'standalone';

export interface DecisionDiaryContext {
  readonly kind: DecisionDiaryContextKind;
  readonly signalId?: string;
  readonly subjectLabel?: string;
}

interface Props {
  readonly open: boolean;
  readonly context: DecisionDiaryContext;
  readonly onSubmit: (entry: DecisionDiaryEntry) => Promise<void> | void;
  readonly onClose: () => void;
  readonly isSubmitting?: boolean;
}

type Tag = DecisionDiaryEntry['tag'];

const TAGS: ReadonlyArray<{ value: Tag; label: string }> = [
  { value: 'data_concern', label: 'data_concern' },
  { value: 'regime_concern', label: 'regime_concern' },
  { value: 'size_concern', label: 'size_concern' },
  { value: 'manual_judgment', label: 'manual_judgment' },
  { value: 'other', label: 'other' },
];

const MIN_LEN = 10;
const MAX_LEN = 2000;

function titleFor(context: DecisionDiaryContext): string {
  const subject = context.subjectLabel?.trim();
  return subject ? `New diary entry: ${subject}` : 'New diary entry';
}

function entryClassFor(
  kind: DecisionDiaryContextKind,
): DecisionDiaryEntry['entry_class'] {
  void kind; // single 'standalone' kind post-pivot; kept for the Phase-2 surface
  return 'general';
}

function submitLabelFor(kind: DecisionDiaryContextKind): string {
  void kind;
  return 'Save';
}

export function DecisionDiaryModal({
  open,
  context,
  onSubmit,
  onClose,
  isSubmitting = false,
}: Props): JSX.Element {
  const [tag, setTag] = useState<Tag>('manual_judgment');
  const [reasoning, setReasoning] = useState<string>('');
  const [touched, setTouched] = useState<boolean>(false);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    if (open) {
      setTag('manual_judgment');
      setReasoning('');
      setTouched(false);
      const id = window.setTimeout(() => textareaRef.current?.focus(), 50);
      return () => window.clearTimeout(id);
    }
    return undefined;
  }, [open, context.kind, context.signalId]);

  const trimmed = reasoning.trim();
  const len = trimmed.length;
  const tooShort = len < MIN_LEN;
  const tooLong = len > MAX_LEN;
  const invalid = tooShort || tooLong;

  const handleSubmit = useCallback(
    async (e: React.FormEvent<HTMLFormElement>): Promise<void> => {
      e.preventDefault();
      setTouched(true);
      if (invalid || isSubmitting) return;
      await onSubmit({
        entry_class: entryClassFor(context.kind),
        tag,
        reasoning_text: trimmed,
      });
    },
    [invalid, isSubmitting, onSubmit, context.kind, tag, trimmed],
  );

  const counterColor = tooLong
    ? 'text-severity-p0'
    : tooShort
      ? 'text-text-muted'
      : 'text-text-secondary';

  const errorMsg = !touched
    ? null
    : tooShort
      ? `Reasoning must be at least ${MIN_LEN} characters.`
      : tooLong
        ? `Reasoning must be at most ${MAX_LEN} characters.`
        : null;

  return (
    <ModalShell open={open} onClose={onClose} title={titleFor(context)} maxWidth="lg">
      <form onSubmit={handleSubmit} className="space-y-4">
        <fieldset className="space-y-2">
          <legend className="text-sm text-text-secondary">Tag</legend>
          <div className="grid grid-cols-1 gap-1 sm:grid-cols-2">
            {TAGS.map((t) => (
              <label
                key={t.value}
                className="flex cursor-pointer items-center gap-2 rounded px-2 py-1 font-mono text-sm hover:bg-bg-elevated"
              >
                <input
                  type="radio"
                  name="diary-tag"
                  value={t.value}
                  checked={tag === t.value}
                  onChange={() => setTag(t.value)}
                  disabled={isSubmitting}
                  className="accent-text-primary"
                />
                <span>{t.label}</span>
              </label>
            ))}
          </div>
        </fieldset>

        <div className="space-y-1">
          <label htmlFor="diary-reasoning" className="block text-sm text-text-secondary">
            Reasoning ({MIN_LEN}–{MAX_LEN} chars)
          </label>
          <textarea
            id="diary-reasoning"
            ref={textareaRef}
            rows={6}
            value={reasoning}
            onChange={(e) => setReasoning(e.target.value)}
            onBlur={() => setTouched(true)}
            disabled={isSubmitting}
            maxLength={MAX_LEN + 100}
            aria-invalid={touched && invalid ? 'true' : 'false'}
            aria-describedby="diary-reasoning-counter"
            className="w-full rounded border border-border bg-bg-elevated p-2 font-mono text-sm text-text-primary focus:border-text-primary focus:outline-none"
          />
          <div className="flex items-baseline justify-between">
            <span
              role={errorMsg !== null ? 'alert' : undefined}
              className="text-xs text-severity-p1"
            >
              {errorMsg}
            </span>
            <span
              id="diary-reasoning-counter"
              className={`font-mono text-xs tabular-nums ${counterColor}`}
            >
              {len} / {MAX_LEN}
            </span>
          </div>
        </div>

        <div className="flex justify-end gap-2 pt-2">
          <Button
            type="button"
            variant="ghost"
            onClick={onClose}
            disabled={isSubmitting}
          >
            Cancel
          </Button>
          <Button
            type="submit"
            variant="default"
            disabled={invalid || isSubmitting}
          >
            {isSubmitting ? 'Submitting…' : submitLabelFor(context.kind)}
          </Button>
        </div>
      </form>
    </ModalShell>
  );
}
