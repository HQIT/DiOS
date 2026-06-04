const STORAGE_KEY = "dios_access_token";

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
  sessionStorage.removeItem(STORAGE_KEY);
}

export function authHeaders(extra?: HeadersInit): HeadersInit {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  const token = getAccessToken();
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
  return headers;
}
