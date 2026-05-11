/**
 * apps/web/src/lib/auth.ts -- typed wrappers for the `/api/auth/**` endpoints.
 *
 * Day 21 surface. Each function maps 1:1 to a backend-spec §4.1.1 endpoint:
 *
 *   - verifySetupToken            -> POST /api/setup/verify-token
 *   - getRegisterChallenge        -> POST /api/auth/webauthn/register/challenge
 *   - postRegisterVerify          -> POST /api/auth/webauthn/register/verify
 *   - getLoginChallenge           -> POST /api/auth/webauthn/challenge
 *   - postLoginVerify             -> POST /api/auth/webauthn/verify
 *   - postTotpEnrollChallenge     -> POST /api/auth/totp/enroll-challenge
 *   - postTotpSetupVerify         -> POST /api/auth/totp/setup-verify
 *   - postTotpLoginVerify         -> POST /api/auth/totp/verify
 *   - generateBackupCodes         -> POST /api/auth/backup-codes/generate
 *   - recover                     -> POST /api/auth/recover
 *
 * Types are hand-typed mirrors of `services/api/schemas/auth.py`; when
 * `packages/api-types/` codegen lands (Phase 1+) these types are replaced
 * with the codegen'd versions and this file becomes a thin re-export layer.
 */

import { apiCall } from './api';

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------
export interface SetupTokenVerifyResponse {
  readonly valid: true;
  readonly intended_role: 'owner' | 'reader';
  readonly ceremony_session_id: string;
}

export async function verifySetupToken(token: string): Promise<SetupTokenVerifyResponse> {
  return apiCall<SetupTokenVerifyResponse>('/api/setup/verify-token', {
    method: 'POST',
    body: { token },
  });
}

// ---------------------------------------------------------------------------
// WebAuthn -- registration ceremony
// ---------------------------------------------------------------------------
export interface RegisterChallengeResponse {
  readonly options: Record<string, unknown>;
}

export async function getRegisterChallenge(
  ceremonySessionId: string,
  nickname: string | null,
): Promise<RegisterChallengeResponse> {
  const body: Record<string, unknown> = { ceremony_session_id: ceremonySessionId };
  if (nickname !== null) body['nickname'] = nickname;
  return apiCall<RegisterChallengeResponse>('/api/auth/webauthn/register/challenge', {
    method: 'POST',
    body,
  });
}

export interface RegisterVerifyResponse {
  readonly success: true;
  readonly credential_id_b64: string;
}

export async function postRegisterVerify(
  ceremonySessionId: string,
  credential: Record<string, unknown>,
): Promise<RegisterVerifyResponse> {
  return apiCall<RegisterVerifyResponse>('/api/auth/webauthn/register/verify', {
    method: 'POST',
    body: { ceremony_session_id: ceremonySessionId, credential },
  });
}

// ---------------------------------------------------------------------------
// WebAuthn -- login ceremony
// ---------------------------------------------------------------------------
export interface LoginChallengeResponse {
  readonly ceremony_id: string;
  readonly options: Record<string, unknown>;
}

export async function getLoginChallenge(
  targetUrl: string | null,
): Promise<LoginChallengeResponse> {
  const body: Record<string, unknown> = {};
  if (targetUrl !== null) body['target_url'] = targetUrl;
  return apiCall<LoginChallengeResponse>('/api/auth/webauthn/challenge', {
    method: 'POST',
    body,
  });
}

export interface LoginVerifyResponse {
  readonly success: true;
  readonly target_url: string | null;
}

export async function postLoginVerify(
  ceremonyId: string,
  credential: Record<string, unknown>,
): Promise<LoginVerifyResponse> {
  return apiCall<LoginVerifyResponse>('/api/auth/webauthn/verify', {
    method: 'POST',
    body: { ceremony_id: ceremonyId, credential },
  });
}

// ---------------------------------------------------------------------------
// TOTP -- enrollment + login fallback
// ---------------------------------------------------------------------------
export interface TotpEnrollChallengeResponse {
  readonly secret_base32: string;
  readonly provisioning_uri: string;
}

export async function postTotpEnrollChallenge(
  ceremonySessionId: string,
): Promise<TotpEnrollChallengeResponse> {
  return apiCall<TotpEnrollChallengeResponse>('/api/auth/totp/enroll-challenge', {
    method: 'POST',
    body: { ceremony_session_id: ceremonySessionId },
  });
}

export async function postTotpSetupVerify(
  ceremonySessionId: string,
  totpCode: string,
): Promise<{ readonly success: true }> {
  return apiCall<{ readonly success: true }>('/api/auth/totp/setup-verify', {
    method: 'POST',
    body: { ceremony_session_id: ceremonySessionId, totp_code: totpCode },
  });
}

export interface TotpLoginVerifyResponse {
  readonly success: true;
  readonly auth_strength: 'weak';
}

export async function postTotpLoginVerify(
  username: string,
  totpCode: string,
): Promise<TotpLoginVerifyResponse> {
  return apiCall<TotpLoginVerifyResponse>('/api/auth/totp/verify', {
    method: 'POST',
    body: { username, totp_code: totpCode },
  });
}

// ---------------------------------------------------------------------------
// Backup codes
// ---------------------------------------------------------------------------
export interface BackupCodesResponse {
  readonly codes: readonly string[];
  readonly generation_id: string;
}

export async function generateBackupCodes(
  ceremonySessionId: string,
): Promise<BackupCodesResponse> {
  return apiCall<BackupCodesResponse>('/api/auth/backup-codes/generate', {
    method: 'POST',
    body: { ceremony_session_id: ceremonySessionId },
  });
}

export interface RecoverResponse {
  readonly success: true;
  readonly auth_strength: 'weak';
}

export async function recover(username: string, backupCode: string): Promise<RecoverResponse> {
  return apiCall<RecoverResponse>('/api/auth/recover', {
    method: 'POST',
    body: { username, backup_code: backupCode },
  });
}
