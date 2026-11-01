"use client";

/**
 * Small, browser-local navigation helpers used by the shell's Escape handler.
 *
 * This is deliberately independent from Next's router.  The shell is mounted
 * once per route and can therefore keep a bounded route stack in sessionStorage
 * without coupling navigation to task/session data.
 */

export const NAVIGATION_HISTORY_KEY = "kontur:ui:navigation:v1";
const NAVIGATION_HISTORY_LIMIT = 64;
const APP_ORIGIN = "https://kontur.invalid";
const ALLOWED_UI_ROUTES = new Set([
  "/",
  "/tasks",
  "/workspace",
  "/settings",
  "/analytics",
  "/bookmarks",
  "/chat",
  "/errors",
  "/exams",
  "/skills",
]);

export type NavigationLocation = {
  pathname: string;
  search?: string;
};

export type NavigationStorage = Pick<Storage, "getItem" | "setItem">;

export function normalizeInternalNavigationPath(value: string | null | undefined): string | null {
  if (typeof value !== "string" || !value.startsWith("/") || value.startsWith("//")) return null;
  if (/[\u0000-\u001f\\]/.test(value)) return null;
  // Reject encoded path separators before URL normalization.  Query/hash
  // values may contain them, but a return path must not smuggle another route
  // through a decoded slash or backslash.
  const rawPath = value.split(/[?#]/, 1)[0] ?? "";
  if (/%2f|%5c/i.test(rawPath)) return null;
  const canonicalRawPath = canonicalUiPathname(rawPath);
  if (!ALLOWED_UI_ROUTES.has(canonicalRawPath)) return null;
  try {
    const url = new URL(value, APP_ORIGIN);
    if (url.origin !== APP_ORIGIN) return null;
    const pathname = canonicalUiPathname(url.pathname);
    if (!ALLOWED_UI_ROUTES.has(pathname)) return null;
    return `${pathname}${url.search}${url.hash}`;
  } catch {
    return null;
  }
}

export function navigationLocation(location: NavigationLocation): string | null {
  const pathname = location.pathname || "/";
  const search = location.search || "";
  return normalizeInternalNavigationPath(`${pathname}${search}`);
}

export function getEscapeNavigationFallback(location: NavigationLocation): string {
  const pathname = canonicalUiPathname(location.pathname || "/");
  if (pathname === "/workspace") return "/tasks";
  if (pathname === "/settings") {
    return getExplicitSettingsReturn(location) ?? "/tasks";
  }
  // The root and task-bank pages are the stable app landing surfaces.  Keep
  // Escape inside the app even when no previous route has been recorded.
  if (pathname === "/") return "/";
  return "/";
}

export function readNavigationHistory(storage?: NavigationStorage): string[] {
  const source = storage ?? getSessionStorage();
  if (!source) return [];
  try {
    const parsed: unknown = JSON.parse(source.getItem(NAVIGATION_HISTORY_KEY) || "[]");
    if (!Array.isArray(parsed)) return [];
    return parsed
      .map((entry) => normalizeInternalNavigationPath(typeof entry === "string" ? entry : null))
      .filter((entry): entry is string => entry !== null)
      .slice(-NAVIGATION_HISTORY_LIMIT);
  } catch {
    return [];
  }
}

export function rememberNavigationEntry(path: string, storage?: NavigationStorage): void {
  const current = normalizeInternalNavigationPath(path);
  if (!current) return;
  const source = storage ?? getSessionStorage();
  if (!source) return;
  const history = readNavigationHistory(source);
  // Route transitions made by links are real history entries even when the
  // same path appears again (A → B → A).  Only avoid a duplicate when this
  // route is already the current tail; consumeNavigationEntry removes the
  // popped tail before the target route mounts.
  const next = history.at(-1) === current ? history : [...history, current];
  try {
    source.setItem(NAVIGATION_HISTORY_KEY, JSON.stringify(next.slice(-NAVIGATION_HISTORY_LIMIT)));
  } catch {
    // Navigation remains safe through the deterministic path fallbacks when
    // sessionStorage is unavailable or quota-limited.
  }
}

export function resolveEscapeNavigationTarget(location: NavigationLocation, history: readonly string[] = []): string {
  const current = navigationLocation(location);
  const normalizedHistory = history
    .map((entry) => normalizeInternalNavigationPath(entry))
    .filter((entry): entry is string => entry !== null);
  const currentIndex = current ? normalizedHistory.lastIndexOf(current) : -1;
  const previous = findPreviousRoute(normalizedHistory, currentIndex, location.pathname);
  // An explicit settings return path is authoritative: tabs use replaceState
  // and therefore do not create a browser-history entry of their own.  When it
  // is absent/invalid, retain the recorded previous route instead of jumping
  // directly to the generic settings fallback.
  if (canonicalUiPathname(location.pathname) === "/settings") {
    const explicit = getExplicitSettingsReturn(location);
    if (explicit) return explicit;
  }
  return previous ?? getEscapeNavigationFallback(location);
}

function getExplicitSettingsReturn(location: NavigationLocation): string | null {
  const pathname = canonicalUiPathname(location.pathname);
  if (pathname !== "/settings") return null;
  let returnTo: string | null = null;
  try {
    returnTo = new URLSearchParams(location.search || "").get("returnTo");
  } catch {
    returnTo = null;
  }
  const explicit = normalizeInternalNavigationPath(returnTo);
  const current = navigationLocation(location);
  if (!explicit || explicit === current) return null;
  try {
    if (canonicalUiPathname(new URL(explicit, APP_ORIGIN).pathname) === pathname) return null;
  } catch {
    return null;
  }
  return explicit;
}

export function consumeNavigationEntry(location: NavigationLocation, storage?: NavigationStorage): string {
  const source = storage ?? getSessionStorage();
  const history = readNavigationHistory(source);
  const current = navigationLocation(location);
  const target = resolveEscapeNavigationTarget(location, history);
  if (!source || !current) return target;
  const index = history.lastIndexOf(current);
  const next = index >= 0
    ? history.slice(0, index)
    : history.slice(0, history.findLastIndex((entry) => navigationPathname(entry) !== canonicalUiPathname(location.pathname)) + 1);
  try {
    source.setItem(NAVIGATION_HISTORY_KEY, JSON.stringify(next.slice(-NAVIGATION_HISTORY_LIMIT)));
  } catch {
    // The returned target is still safe and deterministic.
  }
  return target;
}

function findPreviousRoute(history: readonly string[], currentIndex: number, currentPathname: string): string | null {
  const canonicalCurrentPathname = canonicalUiPathname(currentPathname);
  let index = currentIndex >= 0 ? currentIndex - 1 : history.length - 1;
  while (index >= 0) {
    const candidate = history[index];
    if (navigationPathname(candidate) !== canonicalCurrentPathname) return candidate;
    index -= 1;
  }
  return null;
}

function navigationPathname(path: string): string {
  try {
    return canonicalUiPathname(new URL(path, APP_ORIGIN).pathname);
  } catch {
    return "";
  }
}

function canonicalUiPathname(pathname: string): string {
  if (pathname.length > 1 && pathname.endsWith("/")) return pathname.slice(0, -1);
  return pathname || "/";
}

export function isPlainEscapeEvent(event: Pick<KeyboardEvent, "key" | "repeat" | "ctrlKey" | "altKey" | "shiftKey" | "metaKey"> & { isComposing?: boolean; keyCode?: number }): boolean {
  return event.key === "Escape"
    && !event.repeat
    && !event.ctrlKey
    && !event.altKey
    && !event.shiftKey
    && !event.metaKey
    && event.isComposing !== true
    && event.keyCode !== 229;
}

export type EscapeAction = "ignore" | "close-drawer" | "close-panel" | "close-focus" | "navigate";

export type EscapeActionInput = {
  plainEscape: boolean;
  defaultPrevented: boolean;
  fullscreen: boolean;
  activeEscapeLayer: boolean;
  ownedTarget: boolean;
  protectedTarget: boolean;
  compact: boolean;
  navigationOpen: boolean;
  panelVisible: boolean;
  focused: boolean;
};

/**
 * Resolve shell Escape priority without touching DOM or router state.  Layers
 * first, then shell-owned drawers/panels, then focused editor/input guards,
 * and only finally page navigation.
 */
export function resolveEscapeAction(input: EscapeActionInput): EscapeAction {
  if (!input.plainEscape || input.defaultPrevented || input.fullscreen || input.activeEscapeLayer) return "ignore";
  // CodeEditor owns Escape→Tab, and the recorder owns Escape-to-cancel.  Do
  // not let shell focus/panel handling steal either event.
  if (input.ownedTarget) return "ignore";
  if (input.compact && input.navigationOpen) return "close-drawer";
  if (input.panelVisible) return "close-panel";
  if (input.focused) return "close-focus";
  if (input.protectedTarget) return "ignore";
  return "navigate";
}

export function isEscapeProtectedTarget(target: unknown): boolean {
  const element = asElement(target);
  if (!element) return false;
  const tagName = element.tagName?.toLowerCase();
  if (tagName === "input" || tagName === "textarea" || tagName === "select") return true;
  if (element.isContentEditable || hasClosest(element, "[contenteditable]")) return true;
  // CodeEditor intentionally owns Escape→Tab, and shortcut recorder owns
  // Escape to cancel capture.  Keep both out of page navigation.
  return isEscapeOwnedTarget(element);
}

export function isEscapeOwnedTarget(target: unknown): boolean {
  const element = asElement(target);
  return Boolean(element && hasClosest(element, "textarea.code-textarea, [data-code-editor], [data-shortcut-recorder]"));
}

export function hasActiveEscapeLayer(root: ParentNode = document): boolean {
  const query = (selector: string): Element[] => Array.from(root.querySelectorAll?.(selector) ?? []);
  const visible = (element: Element): boolean => {
    if (element.hasAttribute("hidden") || element.getAttribute("aria-hidden") === "true" || element.closest?.("[hidden], [inert], [aria-hidden='true']")) return false;
    return true;
  };
  if (query("[role='dialog'][aria-modal='true'], [role='menu'], [popover]").some(visible)) return true;

  for (const trigger of query("[aria-expanded='true']")) {
    if (!visible(trigger)) continue;
    const popupId = trigger.getAttribute("aria-controls");
    const popup = popupId && "getElementById" in root
      ? (root as Document).getElementById(popupId)
      : null;
    const popupHint = `${popup?.id ?? ""} ${typeof popup?.className === "string" ? popup.className : ""}`.toLowerCase();
    if (trigger.hasAttribute("aria-haspopup") || popup?.getAttribute("role") === "menu" || /menu|popover|palette|positions|brush/.test(popupHint)) return true;
  }

  // The profile menu is a native details disclosure with its own Escape
  // handler.  Only count it while focus is inside that open disclosure.
  const active = typeof document !== "undefined" ? document.activeElement : null;
  return Boolean(active && (active as Element).closest?.("details.profile-menu[open]"));
}

function getSessionStorage(): NavigationStorage | undefined {
  if (typeof window === "undefined") return undefined;
  try {
    return window.sessionStorage;
  } catch {
    return undefined;
  }
}

function asElement(value: unknown): (Element & { isContentEditable?: boolean }) | null {
  if (!value || typeof value !== "object") return null;
  return value as Element & { isContentEditable?: boolean };
}

function hasClosest(element: Element, selector: string): boolean {
  return Boolean(element.closest?.(selector));
}
