'use client';

/**
 * `/system` page -- frontend-spec §2.6.
 *
 * Phase 1 surface lands Day 27 (Week 7 Wed):
 *   - Kill-switch tile (state display + INVOKE + RESUME with re-auth)
 *   - Risk envelope tile (read-only Phase 1 view of the parameter set)
 *   - Audit log table (filterable by event_type + env + date window)
 *   - Reconciliation status tile
 *   - Account section (regenerate backup codes — re-auth gated)
 *
 * The watchdog status tile was removed 2026-07-09 with the external
 * watchdog retirement (hosted uptime monitor replaced it; see
 * Docs/decisions-log.md 2026-07-09).
 *
 * Phase 2 surfaces from spec §2.6 (NOT in scope today):
 *   - Operating cost dashboard (§2.6.7)
 *   - Agent activity feed (§2.6.8)
 *   - PR review surface at /system/pr/:id (§2.6.9)
 *   - Risk-envelope [Propose change] buttons
 *   - Audit detail page at /system/audit/:event_uuid
 *
 * Layout: 2-column grid on lg screens; stacks vertically below `lg:`.
 * Kill switch + risk envelope on the top row (operationally critical
 * pair). Reconciliation on row 2. Audit log full-width on row 3.
 * Account section full-width on row 4.
 *
 * Re-auth flow: the Account section's "Regenerate backup codes" button
 * receives an `onRequireReauth` callback that opens the page-level
 * ReauthModal. The KillSwitchTile manages its own ReauthModal because
 * its re-auth path (RESUME) is tightly coupled to the resume mutation
 * — splitting it out into the page level would require lifting the
 * mutation state which is more churn than the duplication saves.
 */

import { useState } from 'react';

import { AccountSection } from '@/components/system/account-section';
import { AuditLogTable } from '@/components/system/audit-log-table';
import { KillSwitchTile } from '@/components/system/kill-switch-tile';
import { ReauthModal } from '@/components/system/reauth-modal';
import { ReconciliationTile } from '@/components/system/reconciliation-tile';
import { RiskEnvelopeTile } from '@/components/system/risk-envelope-tile';
import { useAuthMe } from '@/lib/api/queries';

const UV_FRESHNESS_MS = 4.5 * 60 * 1000;

function isUvFresh(lastUvAt: string | null): boolean {
  if (lastUvAt === null) return false;
  const ts = new Date(lastUvAt).getTime();
  if (Number.isNaN(ts)) return false;
  return Date.now() - ts < UV_FRESHNESS_MS;
}

export default function SystemPage(): JSX.Element {
  const me = useAuthMe();
  const [reauthOpen, setReauthOpen] = useState(false);
  // Callback the re-auth modal fires on successful UV. Captured at request
  // time so multiple re-auth-gated buttons can share the same modal.
  const [pendingAction, setPendingAction] = useState<(() => void) | null>(null);

  const handleRequireReauth = (onAuthorized: () => void): void => {
    if (isUvFresh(me.data?.last_uv_at ?? null)) {
      onAuthorized();
      return;
    }
    setPendingAction(() => onAuthorized);
    setReauthOpen(true);
  };

  const handleAuthorized = (): void => {
    if (pendingAction !== null) pendingAction();
    setPendingAction(null);
  };

  return (
    <div className="min-h-screen bg-bg-base p-4 md:p-8">
      <div className="mx-auto max-w-7xl space-y-6">
        <header>
          <h1 className="text-2xl font-semibold">System</h1>
          <p className="mt-1 text-sm text-text-muted">
            Kill switch, risk envelope, audit log, reconciliation, and
            account management.
          </p>
        </header>

        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <KillSwitchTile />
          <RiskEnvelopeTile />
        </div>

        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <ReconciliationTile />
        </div>

        <AuditLogTable />

        <AccountSection onRequireReauth={handleRequireReauth} />

        <ReauthModal
          open={reauthOpen}
          onClose={() => {
            setReauthOpen(false);
            setPendingAction(null);
          }}
          onAuthorized={handleAuthorized}
          actionLabel="Regenerate backup codes"
        />
      </div>
    </div>
  );
}
