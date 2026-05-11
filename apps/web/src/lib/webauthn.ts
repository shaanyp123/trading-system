/**
 * apps/web/src/lib/webauthn.ts -- @simplewebauthn/browser wrappers.
 *
 * Day 21 (Week 6 Tue) ceremony helpers per frontend-spec §5.1.
 * The browser side of the WebAuthn ceremony reduces to two calls:
 *
 *   - `startRegistration(options)` -> wraps `navigator.credentials.create()`
 *   - `startAuthentication(options)` -> wraps `navigator.credentials.get()`
 *
 * The library handles the base64url <-> ArrayBuffer dance and surfaces a
 * JSON-friendly credential dict the backend's py_webauthn understands.
 *
 * Browser-unsupported behaviour: `isWebAuthnSupported()` returns false when
 * `window.PublicKeyCredential` is undefined or the platform-authenticator
 * APIs aren't there. Pages render an explainer + the TOTP fallback when
 * this returns false (frontend-spec §2.8.1).
 */

import {
  startAuthentication,
  startRegistration,
  type PublicKeyCredentialCreationOptionsJSON,
  type PublicKeyCredentialRequestOptionsJSON,
} from '@simplewebauthn/browser';

export function isWebAuthnSupported(): boolean {
  if (typeof window === 'undefined') return false;
  return typeof window.PublicKeyCredential !== 'undefined';
}

/** Wraps `navigator.credentials.create()`. Accepts the options dict produced
 * by `py_webauthn.generate_registration_options` (already JSON-able with
 * base64url-encoded blobs). Returns the registration response dict the
 * backend can pass straight to `webauthn.verify_registration_response`. */
export async function browserStartRegistration(
  options: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  // py_webauthn's `options_to_json` shape is compatible with
  // PublicKeyCredentialCreationOptionsJSON; the cast through `unknown` is
  // the only way to satisfy TS's strict structural check without
  // hand-mirroring the entire WebAuthn options schema here.
  const credential = await startRegistration({
    optionsJSON: options as unknown as PublicKeyCredentialCreationOptionsJSON,
  });
  return credential as unknown as Record<string, unknown>;
}

/** Wraps `navigator.credentials.get()`. Same shape contract as above. */
export async function browserStartAuthentication(
  options: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const credential = await startAuthentication({
    optionsJSON: options as unknown as PublicKeyCredentialRequestOptionsJSON,
  });
  return credential as unknown as Record<string, unknown>;
}

/** User-facing label for WebAuthn errors. The library throws subclasses of
 * `Error` with `.name = "InvalidStateError"` etc; we extract the most useful
 * bits to show in a toast. */
export function describeWebAuthnError(error: unknown): string {
  if (error instanceof Error) {
    if (error.name === 'NotAllowedError') {
      return 'The browser refused the ceremony. Try again, or use TOTP / backup code.';
    }
    if (error.name === 'InvalidStateError') {
      return 'This authenticator is already registered. Use a different device.';
    }
    if (error.name === 'AbortError') {
      return 'Ceremony cancelled. Try again.';
    }
    return error.message;
  }
  return 'Unknown WebAuthn error.';
}
