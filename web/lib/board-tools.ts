import type { BoardStroke } from "./board-types";

export type BoardTool = "pen" | "marker" | "pan" | "line" | "rect" | "ellipse" | "text" | "eraser";
export type BoardShortcutAction = BoardTool | "undo" | "redo";

export interface BoardKeyLike {
  key?: string;
  code?: string;
  ctrlKey?: boolean;
  metaKey?: boolean;
  shiftKey?: boolean;
  altKey?: boolean;
  defaultPrevented?: boolean;
}

export function isBoardSpaceKey(event: BoardKeyLike): boolean { return event.code === "Space" || event.key === " "; }

const TOOL_BY_CODE: Record<string, BoardTool> = {
  KeyP: "pen", Digit2: "pen", Numpad2: "pen", KeyH: "marker", KeyM: "marker", Digit3: "marker", Numpad3: "marker",
  KeyV: "pan", KeyL: "line", KeyR: "rect", KeyO: "ellipse", KeyT: "text", KeyE: "eraser",
};
const TOOL_BY_KEY: Record<string, BoardTool> = {
  p: "pen", з: "pen", "2": "pen", h: "marker", р: "marker", m: "marker", ь: "marker", "3": "marker",
  v: "pan", м: "pan", l: "line", д: "line", r: "rect", к: "rect", o: "ellipse", щ: "ellipse", t: "text", е: "text", e: "eraser", у: "eraser",
};

export function resolveBoardShortcut(event: BoardKeyLike): BoardShortcutAction | null {
  if (event.defaultPrevented || event.altKey) return null;
  const key = typeof event.key === "string" ? event.key.toLowerCase() : "";
  if (event.ctrlKey || event.metaKey) {
    if (event.code === "KeyZ" || key === "z" || key === "я") return event.shiftKey ? "redo" : "undo";
    if (event.code === "KeyY" || key === "y" || key === "н") return "redo";
    return null;
  }
  if (event.shiftKey) return null;
  return TOOL_BY_CODE[event.code ?? ""] ?? TOOL_BY_KEY[key] ?? null;
}

export interface BoardStrokeStyle { kind: NonNullable<BoardStroke["kind"]>; color: string; width: number; alpha: number; }
const MARKER_ALPHA = 0.42;

export function clampStrokeOpacity(value: number, fallback = 1): number {
  const safeFallback = Number.isFinite(fallback) ? Math.min(1, Math.max(0.05, fallback)) : 1;
  return Number.isFinite(value) ? Math.min(1, Math.max(0.05, value)) : safeFallback;
}

export function baselineStrokeOpacity(kind: BoardStroke["kind"]): number { return kind === "marker" ? MARKER_ALPHA : 1; }
export function strokeOpacityForPreference(kind: BoardStroke["kind"], value: number): number { return clampStrokeOpacity((Number.isFinite(value) ? value : 1) * baselineStrokeOpacity(kind), baselineStrokeOpacity(kind)); }
export function strokeOpacity(kind: BoardStroke["kind"], value?: number): number { return value == null ? baselineStrokeOpacity(kind) : clampStrokeOpacity(value, baselineStrokeOpacity(kind)); }

export function getStrokeStyle(tool: BoardTool, color: string, zoom: number, requestedWidth?: number): BoardStrokeStyle | null {
  if (tool === "pan" || tool === "text" || tool === "eraser") return null;
  const safeZoom = Number.isFinite(zoom) && zoom > 0 ? zoom : 1;
  const kind = tool === "pen" ? "freehand" : tool;
  const defaultWidth = tool === "marker" ? 18 : 3;
  const width = Number.isFinite(requestedWidth) && (requestedWidth as number) > 0 ? Math.min(48, Math.max(1, requestedWidth as number)) : defaultWidth;
  return { kind, color: typeof color === "string" && color.trim() ? color : "#242529", width: width / safeZoom, alpha: tool === "marker" ? MARKER_ALPHA : 1 };
}
