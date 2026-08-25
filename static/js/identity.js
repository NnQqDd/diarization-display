/**
 * A stable id for this browser, sent with every save.
 *
 * The web platform exposes nothing that identifies a machine, so this is the
 * honest substitute: a random UUID generated once and kept in localStorage.
 * It stays the same for this browser profile across reloads and reboots, which
 * is usually one person on one machine -- but it resets when site data is
 * cleared, and a private window or a second browser gets its own. It groups a
 * person's saves together; it does not prove who they are.
 */

const KEY = 'diarization-annotate.clientId';

// Used when localStorage is unavailable (private browsing, blocked cookies).
// Lasts only as long as the tab, which is better than sending nothing.
let sessionFallback = null;

/** A canonical UUIDv4. The dashes matter: the server parses it as a filename. */
export function newUuid() {
  if (globalThis.crypto?.randomUUID) return crypto.randomUUID();
  // Older browsers: still 122 bits of randomness, just assembled by hand.
  const bytes = new Uint8Array(16);
  globalThis.crypto?.getRandomValues?.(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;  // version 4
  bytes[8] = (bytes[8] & 0x3f) | 0x80;  // variant 1
  const hex = [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('');
  return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16),
          hex.slice(16, 20), hex.slice(20)].join('-');
}

export function clientId() {
  try {
    const stored = localStorage.getItem(KEY);
    if (stored) return stored;
    const fresh = newUuid();
    localStorage.setItem(KEY, fresh);
    return fresh;
  } catch {
    sessionFallback = sessionFallback || newUuid();
    return sessionFallback;
  }
}

/**
 * Mirror the id into a cookie, so the plain page load carries it too.
 *
 * The header on `fetch` only reaches the API, and a browser that is refused
 * this app should be refused the page, not the page's data after it has
 * loaded. Same value, same origin, nothing else in it.
 */
export function mirrorToCookie() {
  try {
    document.cookie =
      `clientId=${encodeURIComponent(clientId())}; path=/; max-age=31536000; SameSite=Lax`;
  } catch {
    // Cookies blocked. The header still covers every request the app makes;
    // only the head start on the document request is lost.
  }
}
