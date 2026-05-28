/**
 * apps/web/src/lib/api.ts -- minimal typed fetch wrapper for `/api/auth/**`.
 *
 * Reads the API base from `NEXT_PUBLIC_API_BASE` (set per env via the
 * docker-compose `.env` file; empty string for same-origin SSR + reverse-
 * proxied apex). Attaches the CSRF token from the `__Host-csrf_token`
 * cookie to every state-changing request (per backend-spec §8.5.4 double-
 * submit pattern).
 *
 * Day 21 (Week 6 Tue) ships only the surface the auth pages use: error
 * envelope parsing, base URL resolution, CSRF cookie reading. A richer
 * TanStack-Query-backed client lands when the Today page wires real data
 * (Day 22 Wed per IG line 377).
 */

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? '';

const CSRF_COOKIE = '__Host-csrf_token';
const CSRF_HEADER = 'X-CSRF-Token';

export class ApiError extends Error {
  readonly status: number;
  readonly errorCode: string;
  readonly details: Record<string, unknown> | null;

  constructor(
    status: number,
    errorCode: string,
    message: string,
    details: Record<string, unknown> | null,
  ) {
    super(message);
    this.status = status;
    this.errorCode = errorCode;
    this.details = details;
  }
}

function readCookie(name: string): string | null {
  if (typeof document === 'undefined') return null;
  const cookies = document.cookie.split('; ');
  for (const c of cookies) {
    const [k, v] = c.split('=');
    if (k === name && v !== undefined) return decodeURIComponent(v);
  }
  return null;
}

interface ApiCallOptions {
  readonly method?: 'GET' | 'POST';
  readonly body?: unknown;
  readonly signal?: AbortSignal;
}

export async function apiCall<T>(
  path: string,
  opts: ApiCallOptions = {},
): Promise<T> {
  const { method = 'GET', body, signal } = opts;
  const url = `${API_BASE}${path}`;
  const headers: Record<string, string> = {
    Accept: 'application/json',
  };
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
  }
  if (method !== 'GET') {
    const csrf = readCookie(CSRF_COOKIE);
    if (csrf !== null) {
      headers[CSRF_HEADER] = csrf;
    }
  }
  const init: RequestInit = {
    method,
    credentials: 'include',
    headers,
  };
  if (body !== undefined) {
    init.body = JSON.stringify(body);
  }
  if (signal !== undefined) {
    init.signal = signal;
  }
  const res = await fetch(url, init);
  let parsed: unknown = null;
  const contentType = res.headers.get('content-type') ?? '';
  if (contentType.includes('application/json')) {
    parsed = await res.json().catch(() => null);
  }
  if (!res.ok) {
    const envelope = parsed as
      | { error_code?: string; message?: string; details?: Record<string, unknown> | null }
      | null;
    throw new ApiError(
      res.status,
      envelope?.error_code ?? `HTTP_${res.status}`,
      envelope?.message ?? res.statusText,
      envelope?.details ?? null,
    );
  }
  return parsed as T;
}

/** Bootstrap-CSRF helper: if the CSRF cookie doesn't exist yet, set a temporary
 * value so the first state-changing call succeeds. The server overwrites the
 * cookie on the first successful login response with a server-minted value.
 *
 * Day 21 routes that are exempt from CSRF (`/api/setup/verify-token`) don't
 * trigger this -- but the WebAuthn / TOTP / recover endpoints DO require the
 * double-submit cookie. The bootstrap CSRF value is acceptable because the
 * server uses it ONLY for the constant-time comparison; replacing it on the
 * server response defends against a fixated value persisting beyond the
 * authentication boundary.
 *
 * 2026-05-28 fix: previously only the /login, /setup, /recover pages called
 * this. Operators visiting /system (or any post-auth page) directly in a
 * fresh browser session had no CSRF cookie, so the first state-changing POST
 * (Resume kill switch, regenerate backup codes) failed with `CSRF_REJECTED`.
 * The function is now also invoked from `<Providers>` so every page mounts
 * with a CSRF cookie. The backend `SessionStubMiddleware` independently
 * mints a server-issued cookie on the first response — the two defences
 * are belt-and-braces; either alone closes the bug.
 *
 * Max-Age: 24h (86400s) — matches the server's `session_absolute_seconds`
 * so the bootstrap cookie persists across browser restarts the same way a
 * server-issued cookie would. Pre-fix the cookie was a session cookie
 * (no Max-Age), so closing the browser deleted it.
 */
const CSRF_BOOTSTRAP_MAX_AGE_SECONDS = 86400;

export function ensureBootstrapCsrfCookie(): void {
  if (typeof document === 'undefined') return;
  if (readCookie(CSRF_COOKIE) !== null) return;
  // 16-byte random token (base64 ~22 chars). secure flag set on https origin.
  const random = crypto.getRandomValues(new Uint8Array(16));
  const b64 = btoa(String.fromCharCode(...random))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '');
  const secure = location.protocol === 'https:' ? '; Secure' : '';
  document.cookie =
    `${CSRF_COOKIE}=${b64}; path=/; SameSite=Strict; ` +
    `Max-Age=${CSRF_BOOTSTRAP_MAX_AGE_SECONDS}${secure}`;
}
