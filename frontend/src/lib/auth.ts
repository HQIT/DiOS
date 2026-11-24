const STORAGE_KEY = "dios_access_token";
let browserSession = false;
export const SESSION_EXPIRED_EVENT = "dios:session-expired";

export function setBrowserSession(value: boolean): void {
  browserSession = value;
  if (value) clearAccessToken();
}

export function sessionExpired(): void {
  if (browserSession) window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT));
}

export function getAccessToken(): string | null {
  try {
    return sessionStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function setAccessToken(token: string): void {
  sessionStorage.setItem(STORAGE_KEY, token.trim());
}

export function clearAccessToken(): void {
  try { sessionStorage.removeItem(STORAGE_KEY); } catch { /* Cookie SSO needs no web storage. */ }
}

export function authHeaders(extra?: HeadersInit): HeadersInit {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  const token = browserSession ? null : getAccessToken();
  if (token) {
    headers["X-DiOS-Access-Token"] = token;
  }
  if (extra instanceof Headers) {
    extra.forEach((v, k) => {
      headers[k] = v;
    });
  } else if (Array.isArray(extra)) {
    for (const [k, v] of extra) headers[k] = v;
  } else if (extra) {
    Object.assign(headers, extra);
  }
  if (browserSession) headers["X-CSRF-Token"] = "1";
  return headers;
}
