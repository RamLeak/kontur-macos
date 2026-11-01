const UNSAFE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

/**
 * Read only the non-HttpOnly CSRF cookie. Owner/session cookies are never
 * inspected here and no credential is copied into a public environment value.
 */
export function csrfTokenFromCookie(cookieHeader: string): string | null {
  for (const part of cookieHeader.split(";")) {
    const separator = part.indexOf("=");
    if (separator < 0 || part.slice(0, separator).trim() !== "ege_csrf") continue;
    const raw = part.slice(separator + 1).trim();
    if (!raw) return null;
    try {
      const token = decodeURIComponent(raw);
      if (!token || /[\u0000-\u001f\u007f]/.test(token)) return null;
      return token;
    } catch {
      return null;
    }
  }
  return null;
}

/**
 * Add CSRF only to state-changing requests and only when the browser exposed
 * the dedicated cookie. The returned Headers object preserves caller headers.
 */
export function withCsrfHeader(
  method: string | undefined,
  headers: HeadersInit | undefined,
  cookieHeader: string,
): Headers {
  const result = new Headers(headers);
  if (!UNSAFE_METHODS.has((method ?? "GET").toUpperCase())) return result;
  const token = csrfTokenFromCookie(cookieHeader);
  if (token) result.set("X-CSRF-Token", token);
  return result;
}

