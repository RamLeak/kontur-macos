import type { BoardStroke } from "@/lib/types";

export type BoardTool = "pen" | "marker" | "pan" | "line" | "rect" | "ellipse" | "text" | "eraser";
export type BoardShortcutAction = BoardTool | "undo" | "redo" | null;

export interface BoardKeyLike {
  key?: string;
  code?: string;
  ctrlKey?: boolean;
  metaKey?: boolean;
  shiftKey?: boolean;
  altKey?: boolean;
  defaultPrevented?: boolean;
}

export const BOARD_TOOL_SHORTCUTS = {
  pen: { label: "P / 2", code: "KeyP" },
  marker: { label: "H / 3", code: "KeyH" },
  pan: { label: "V", code: "KeyV" },
  line: { label: "L", code: "KeyL" },
  rect: { label: "R", code: "KeyR" },
  ellipse: { label: "O", code: "KeyO" },
  text: { label: "T", code: "KeyT" },
  eraser: { label: "E", code: "KeyE" },
} as const satisfies Record<BoardTool, { label: string; code: string }>;

const TOOL_BY_CODE: Record<string, BoardTool> = {
  KeyP: "pen",
  Digit2: "pen",
  Numpad2: "pen",
  KeyH: "marker",
  KeyM: "marker",
  Digit3: "marker",
  Numpad3: "marker",
  KeyV: "pan",
  KeyL: "line",
  KeyR: "rect",
  KeyO: "ellipse",
  KeyT: "text",
  KeyE: "eraser",
};

// The key aliases keep shortcuts usable when the user types with a Russian
// layout and a browser does not expose KeyboardEvent.code in a test harness.
const TOOL_BY_KEY: Record<string, BoardTool> = {
  p: "pen",
  з: "pen",
  2: "pen",
  h: "marker",
  р: "marker",
  m: "marker",
  ь: "marker",
  3: "marker",
  v: "pan",
  м: "pan",
  l: "line",
  д: "line",
  r: "rect",
  к: "rect",
  o: "ellipse",
  щ: "ellipse",
  t: "text",
  е: "text",
  e: "eraser",
  у: "eraser",
};

function normalizedKey(value: string | undefined): string {
  return typeof value === "string" ? value.trim().toLowerCase() : "";
}

export function resolveBoardShortcut(event: BoardKeyLike): BoardShortcutAction {
  if (event.defaultPrevented || event.altKey) return null;
  const key = normalizedKey(event.key);
  const code = event.code ?? "";
  if (event.ctrlKey || event.metaKey) {
    if (code === "KeyZ" || key === "z" || key === "я") return event.shiftKey ? "redo" : "undo";
    return null;
  }
  if (event.shiftKey) return null;
  return TOOL_BY_CODE[code] ?? TOOL_BY_KEY[key] ?? null;
}

export function isBoardSpaceKey(event: BoardKeyLike): boolean {
  return event.code === "Space" || event.key === " ";
}

export interface BoardStrokeStyle {
  kind: NonNullable<BoardStroke["kind"]>;
  color: string;
  width: number;
  alpha: number;
}

const DEFAULT_STROKE_COLOR = "#242529";
const MARKER_ALPHA = 0.42;
const MIN_STROKE_OPACITY = 0.05;
const MAX_STROKE_OPACITY = 1;

export function clampStrokeOpacity(value: number, fallback = 1): number {
  const safeFallback = Number.isFinite(fallback)
    ? Math.min(MAX_STROKE_OPACITY, Math.max(MIN_STROKE_OPACITY, fallback))
    : 1;
  return Number.isFinite(value)
    ? Math.min(MAX_STROKE_OPACITY, Math.max(MIN_STROKE_OPACITY, value))
    : safeFallback;
}

export function baselineStrokeOpacity(kind: BoardStroke["kind"]): number {
  return kind === "marker" ? MARKER_ALPHA : 1;
}

export function strokeOpacityForPreference(kind: BoardStroke["kind"], boardOpacity: number): number {
  const baseline = baselineStrokeOpacity(kind);
  const preference = Number.isFinite(boardOpacity) ? boardOpacity : 1;
  return clampStrokeOpacity(preference * baseline, baseline);
}

export function getStrokeStyle(tool: BoardTool, color: string, zoom: number, requestedWidth?: number): BoardStrokeStyle | null {
  const safeZoom = Number.isFinite(zoom) && zoom > 0 ? zoom : 1;
  const safeColor = typeof color === "string" && color.trim() ? color : DEFAULT_STROKE_COLOR;
  if (tool === "pan" || tool === "text" || tool === "eraser") return null;
  const kind = tool === "pen" ? "freehand" : tool;
  const defaultWidth = tool === "marker" ? 18 : 3;
  const safeWidth = Number.isFinite(requestedWidth) && (requestedWidth as number) > 0
    ? Math.min(48, Math.max(1, requestedWidth as number))
    : defaultWidth;
  return {
    kind,
    color: safeColor,
    width: safeWidth / safeZoom,
    alpha: tool === "marker" ? MARKER_ALPHA : 1,
  };
}

export function strokeOpacity(kind: BoardStroke["kind"], opacity?: number): number {
  const baseline = baselineStrokeOpacity(kind);
  return opacity == null ? baseline : clampStrokeOpacity(opacity, baseline);
}
